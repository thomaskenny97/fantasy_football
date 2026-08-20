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
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

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
    allow_methods=["GET"],
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
