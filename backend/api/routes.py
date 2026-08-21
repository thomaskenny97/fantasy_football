"""HTTP API for the dashboard.

Reads from the SQLite database that `backend.cli sync` populates. Nothing here calls
Sleeper or ESPN directly - syncing stays an explicit action so a page load can never
trigger a 14 MB player download or burn through ESPN's undocumented endpoints.
"""

from __future__ import annotations

import logging

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.analysis import mock as mock_service
from backend.analysis import rankings as rankings_service
from backend.analysis.adp_board import detect_slot, heat_map, pick_numbers
from backend.analysis.board import draft_state
from backend.clients.espn import EspnClient, EspnError
from backend.clients.sleeper import SleeperClient, SleeperError
from backend.config import PROJECT_ROOT, Config
from backend.ingest import drafts as drafts_ingest
from backend.ingest import players as players_ingest
from backend.resolve.player_matching import PlayerRegistry
from backend.db import (
    PLATFORM_SLEEPER,
    Draft,
    DraftPick,
    League,
    Player,
    RosterSlot,
    Team,
    init_db,
    make_session_factory,
)

log = logging.getLogger(__name__)

app = FastAPI(title="Fantasy Football", version="0.1.0")

# The Vite dev server runs on a different port during development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET", "PUT", "POST"],
    allow_headers=["*"],
)

_engine = init_db()
_Session = make_session_factory(_engine)

FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"


def _session() -> Session:
    return _Session()


def _player_payload(player: Player | None) -> dict[str, Any] | None:
    if player is None:
        return None
    return {
        "sleeperId": player.sleeper_id,
        "name": player.full_name,
        "position": player.position,
        "proTeam": player.pro_team,
        "injuryStatus": player.injury_status,
    }


def _team_payload(team: Team) -> dict[str, Any]:
    return {
        "id": team.id,
        "name": team.name,
        "owner": team.owner_name,
        "isMine": team.is_mine,
        "wins": team.wins,
        "losses": team.losses,
        "ties": team.ties,
        "pointsFor": team.points_for,
        "pointsAgainst": team.points_against,
    }


@app.get("/api/health")
def health() -> dict[str, Any]:
    with _session() as session:
        leagues = len(session.execute(select(League.id)).all())
        players = len(session.execute(select(Player.sleeper_id)).all())
    return {"ok": True, "leagues": leagues, "players": players}


@app.get("/api/leagues")
def list_leagues() -> list[dict[str, Any]]:
    """Every synced league, with the user's own team summarised."""
    with _session() as session:
        payload: list[dict[str, Any]] = []
        for league in session.execute(select(League)).scalars():
            teams = (
                session.execute(select(Team).where(Team.league_id == league.id))
                .scalars()
                .all()
            )
            mine = next((t for t in teams if t.is_mine), None)
            drafts = (
                session.execute(select(Draft).where(Draft.league_id == league.id))
                .scalars()
                .all()
            )
            payload.append(
                {
                    "id": league.id,
                    "platform": league.platform,
                    "name": league.name,
                    "season": league.season,
                    "teamCount": len(teams),
                    "rosterPositions": league.roster_positions,
                    "myTeam": _team_payload(mine) if mine else None,
                    "drafts": [
                        {
                            "id": d.id,
                            "status": d.status,
                            "type": d.draft_type,
                            "rounds": d.rounds,
                            "pickCount": len(d.picks),
                        }
                        for d in drafts
                    ],
                }
            )
        return payload


@app.get("/api/leagues/{league_id}/teams")
def league_teams(league_id: int) -> list[dict[str, Any]]:
    with _session() as session:
        league = session.get(League, league_id)
        if league is None:
            raise HTTPException(status_code=404, detail="league not found")
        teams = (
            session.execute(select(Team).where(Team.league_id == league_id))
            .scalars()
            .all()
        )
        # Standings order: wins, then points scored.
        teams.sort(key=lambda t: (t.wins, t.points_for), reverse=True)
        return [_team_payload(t) for t in teams]


@app.get("/api/teams/{team_id}/roster")
def team_roster(team_id: int) -> dict[str, Any]:
    """A team's roster, starters first, in the league's own slot order."""
    with _session() as session:
        team = session.get(Team, team_id)
        if team is None:
            raise HTTPException(status_code=404, detail="team not found")

        slots = (
            session.execute(select(RosterSlot).where(RosterSlot.team_id == team_id))
            .scalars()
            .all()
        )

        entries = []
        for index, slot in enumerate(slots):
            player = session.get(Player, slot.sleeper_id) if slot.sleeper_id else None
            entries.append(
                {
                    # Insertion order reproduces the league's real lineup order
                    # (QB, RB, RB, WR, WR, TE, FLEX...). Sorting by slot name would
                    # scramble it alphabetically.
                    "order": index,
                    "slot": slot.slot,
                    "isStarter": slot.is_starter,
                    "player": _player_payload(player),
                    # Kept so an unresolved player is visible rather than missing.
                    "platformPlayerId": slot.platform_player_id,
                }
            )
        entries.sort(key=lambda e: (not e["isStarter"], e["order"]))

        return {
            "team": _team_payload(team),
            "league": {
                "id": team.league_id,
                "name": session.get(League, team.league_id).name,
            },
            "roster": entries,
        }


@app.get("/api/drafts/{draft_id}")
def draft_board(draft_id: int) -> dict[str, Any]:
    """A draft board, most recent pick first."""
    with _session() as session:
        draft = session.get(Draft, draft_id)
        if draft is None:
            raise HTTPException(status_code=404, detail="draft not found")

        picks = (
            session.execute(
                select(DraftPick)
                .where(DraftPick.draft_id == draft_id)
                .order_by(DraftPick.pick_no.desc())
            )
            .scalars()
            .all()
        )

        teams = {
            t.platform_team_id: t
            for t in session.execute(
                select(Team).where(Team.league_id == draft.league_id)
            ).scalars()
        }

        return {
            "id": draft.id,
            "status": draft.status,
            "type": draft.draft_type,
            "rounds": draft.rounds,
            "league": session.get(League, draft.league_id).name,
            "picks": [
                {
                    "pickNo": p.pick_no,
                    "round": p.round,
                    "team": (
                        teams[p.platform_team_id].name
                        if p.platform_team_id in teams
                        else p.platform_team_id
                    ),
                    "isMine": (
                        teams[p.platform_team_id].is_mine
                        if p.platform_team_id in teams
                        else False
                    ),
                    "bidAmount": p.bid_amount,
                    "isKeeper": p.is_keeper,
                    "player": _player_payload(
                        session.get(Player, p.sleeper_id) if p.sleeper_id else None
                    ),
                }
                for p in picks
            ],
        }


# Built lazily and reused: the registry indexes ~12k players and is far too
# expensive to rebuild on every poll during a live draft.
_registry: PlayerRegistry | None = None


def _player_registry() -> PlayerRegistry:
    global _registry
    if _registry is None:
        _registry = PlayerRegistry(players_ingest.load_registry_source())
    return _registry


def _pull_latest_picks(session: Session, league: League) -> None:
    """Fetch this league's draft picks from its platform.

    Only reached when the client explicitly asks to refresh, which happens while a
    draft is actually running. Ordinary page loads stay database-only.
    """
    config = Config.load()
    if league.platform == PLATFORM_SLEEPER:
        with SleeperClient() as sleeper:
            drafts_ingest.sync_sleeper_drafts(session, sleeper, league)
        return

    with EspnClient(config.espn.swid, config.espn.s2) as espn:
        index = drafts_ingest.build_espn_player_index(espn, league)
        drafts_ingest.sync_espn_draft(
            session, espn, league, _player_registry(), index
        )


@app.get("/api/leagues/{league_id}/draft")
def draft_assistant(
    league_id: int, limit: int = 12, refresh: bool = False
) -> dict[str, Any]:
    """Live draft board: what is gone, what is left, and who to take next.

    Pass refresh=true to pull new picks from the platform first. The dashboard does
    that only while a draft is in progress, so a normal page load never touches the
    network.
    """
    with _session() as session:
        league = session.get(League, league_id)
        if league is None:
            raise HTTPException(status_code=404, detail="league not found")

        refreshed = False
        if refresh:
            try:
                _pull_latest_picks(session, league)
                refreshed = True
            except (EspnError, SleeperError) as exc:
                log.warning("Draft refresh failed for league %s: %s", league_id, exc)

        try:
            state = draft_state(session, league_id, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        state["refreshed"] = refreshed
        return state


@app.get("/api/leagues/{league_id}/adp-board")
def adp_board(
    league_id: int,
    slot: int | None = None,
    rounds: int | None = None,
    per_round: int = 10,
) -> dict[str, Any]:
    """Per-round draft targets and the projected board grid.

    `slot` overrides the detected draft slot so different positions can be compared
    before an order is drawn. Database only - no network on a page load.
    """
    with _session() as session:
        try:
            return heat_map(
                session,
                league_id,
                slot=slot,
                rounds=rounds,
                per_round=max(1, min(per_round, 30)),
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


class MockRequest(BaseModel):
    """Which mocks to run. Omitting slots runs every slot in the league."""

    slots: list[int] | None = None
    strategies: list[str] | None = None
    runsPerCombo: int = Field(default=1, ge=1, le=5)
    seed: int | None = None


class MockAdvance(BaseModel):
    """One step of an interactive mock.

    The client holds the pick list and sends it back each turn, so the server keeps no
    session state and a mock survives a page reload.
    """

    mySlot: int
    seed: int
    strategy: str = mock_service.BALANCED
    picks: list[dict[str, Any]] = Field(default_factory=list)
    myPickId: str | None = None


class RankingOrder(BaseModel):
    """The complete ordered list of player ids, best first."""

    order: list[str] = Field(default_factory=list)


def _picks_for(session: Session, league: League, slot: int | None) -> list[int]:
    """The user's pick numbers, so a ranking can be coloured by what they can get."""
    draft = session.execute(
        select(Draft).where(Draft.league_id == league.id)
    ).scalars().first()
    team_count = league.total_rosters or 12
    total_rounds = (draft.rounds if draft and draft.rounds else None) or len(
        [s for s in (league.roster_positions or []) if s != "IR"]
    ) or 15
    active = slot or detect_slot(session, league) or 1
    return pick_numbers(active, team_count, total_rounds, draft.draft_type if draft else None)


@app.get("/api/leagues/{league_id}/rankings")
def get_rankings(league_id: int, slot: int | None = None) -> dict[str, Any]:
    """The user's ranking for a league, seeded from the market board if unset."""
    with _session() as session:
        league = session.get(League, league_id)
        if league is None:
            raise HTTPException(status_code=404, detail="league not found")
        picks = _picks_for(session, league, slot)
        try:
            return rankings_service.ranking_board(session, league_id, picks=picks)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.put("/api/leagues/{league_id}/rankings")
def put_rankings(league_id: int, body: RankingOrder) -> dict[str, Any]:
    """Replace the ranking with the order the client is showing."""
    if not body.order:
        raise HTTPException(status_code=400, detail="order must not be empty")
    with _session() as session:
        try:
            saved = rankings_service.save_order(session, league_id, body.order)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"saved": saved, "isCustom": True}


@app.post("/api/leagues/{league_id}/rankings/reset")
def reset_rankings(league_id: int) -> dict[str, Any]:
    """Discard the personal ranking and fall back to the market board."""
    with _session() as session:
        removed = rankings_service.reset(session, league_id)
        return {"removed": removed, "isCustom": False}


@app.get("/api/mock/strategies")
def mock_strategies() -> list[dict[str, str]]:
    return [
        {"key": key, "label": label}
        for key, label in mock_service.STRATEGIES.items()
    ]


@app.post("/api/leagues/{league_id}/mock/simulate")
def simulate_mocks(league_id: int, body: MockRequest) -> dict[str, Any]:
    """Run a batch of mock drafts and return the finished teams."""
    with _session() as session:
        try:
            return mock_service.run_mocks(
                session,
                league_id,
                slots=body.slots,
                strategies=body.strategies,
                runs_per_combo=body.runsPerCombo,
                seed=body.seed,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/leagues/{league_id}/mock/advance")
def advance_mock(league_id: int, body: MockAdvance) -> dict[str, Any]:
    """Apply the user's pick, then run the field to their next turn."""
    with _session() as session:
        try:
            return mock_service.advance(
                session,
                league_id,
                my_slot=body.mySlot,
                seed=body.seed,
                taken=body.picks,
                my_pick_id=body.myPickId,
                strategy=body.strategy,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


# --- static frontend -------------------------------------------------------
# Serving the built React app from the same origin means no CORS in production and
# one process to run. Mounted last so it never shadows an /api route.

if FRONTEND_DIST.is_dir():
    app.mount(
        "/assets",
        StaticFiles(directory=FRONTEND_DIST / "assets"),
        name="assets",
    )

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(FRONTEND_DIST / "index.html")
