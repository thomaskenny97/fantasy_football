"""Draft ingestion for both platforms.

Sleeper exposes a draft's full state through the public REST API, including a status
field (pre_draft / drafting / complete) that lets a live board poll for new picks.
Each Sleeper pick also carries denormalized player metadata, so a live board does not
need to join against the 14 MB player dump.

ESPN exposes nothing until a draft actually starts: before then mDraftDetail returns
drafted=false, inProgress=false and an empty pick list.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.clients.espn import EspnClient
from backend.clients.sleeper import SleeperClient
from backend.db import (
    PLATFORM_ESPN,
    PLATFORM_SLEEPER,
    Draft,
    DraftPick,
    League,
)
from backend.resolve.player_matching import PlayerRegistry

log = logging.getLogger(__name__)

STATUS_PRE_DRAFT = "pre_draft"
STATUS_DRAFTING = "drafting"
STATUS_COMPLETE = "complete"


def _upsert_draft(
    session: Session,
    league: League,
    platform: str,
    platform_draft_id: str,
    status: str | None,
    draft_type: str | None,
    rounds: int | None,
    season: str,
    start_time: datetime | None = None,
) -> Draft:
    draft = session.execute(
        select(Draft).where(
            Draft.platform == platform,
            Draft.platform_draft_id == platform_draft_id,
        )
    ).scalar_one_or_none()

    if draft is None:
        draft = Draft(
            platform=platform,
            platform_draft_id=platform_draft_id,
            league_id=league.id,
            season=season,
        )
        session.add(draft)

    draft.status = status
    draft.draft_type = draft_type
    draft.rounds = rounds
    draft.start_time = start_time
    session.flush()
    return draft


def _replace_picks(session: Session, draft: Draft, picks: list[dict[str, Any]]) -> None:
    """Picks are rewritten wholesale; during a live draft this runs on every poll."""
    for existing in session.execute(
        select(DraftPick).where(DraftPick.draft_id == draft.id)
    ).scalars():
        session.delete(existing)
    session.flush()

    for pick in picks:
        session.add(DraftPick(draft_id=draft.id, **pick))
    session.flush()


# --- Sleeper ---------------------------------------------------------------


def _sleeper_draft_order(
    client: SleeperClient, league: League, raw: dict[str, Any]
) -> dict[str, str]:
    """Map draft slot to roster id.

    Sleeper keys draft_order by user id, but every other table in this app keys teams
    by roster id, so it is translated here rather than at every read site.
    """
    order = raw.get("draft_order") or {}
    if not order:
        return {}

    owner_to_roster = {
        str(roster.get("owner_id")): str(roster.get("roster_id"))
        for roster in client.rosters(league.platform_league_id)
        if roster.get("owner_id") and roster.get("roster_id") is not None
    }

    resolved: dict[str, str] = {}
    for user_id, slot in order.items():
        roster_id = owner_to_roster.get(str(user_id))
        if roster_id is not None:
            resolved[str(slot)] = roster_id
    return resolved


def sync_sleeper_drafts(
    session: Session,
    client: SleeperClient,
    league: League,
) -> list[Draft]:
    """Pull every draft attached to a Sleeper league."""
    drafts: list[Draft] = []
    for raw in client.league_drafts(league.platform_league_id):
        start_time = None
        if raw.get("start_time"):
            # Sleeper reports start_time in epoch milliseconds.
            start_time = datetime.fromtimestamp(
                int(raw["start_time"]) / 1000, tz=timezone.utc
            ).replace(tzinfo=None)

        draft = _upsert_draft(
            session,
            league=league,
            platform=PLATFORM_SLEEPER,
            platform_draft_id=str(raw["draft_id"]),
            status=raw.get("status"),
            draft_type=raw.get("type"),
            rounds=(raw.get("settings") or {}).get("rounds"),
            season=str(raw.get("season") or league.season),
            start_time=start_time,
        )

        order = _sleeper_draft_order(client, league, raw)
        if order:
            draft.draft_order = order
            session.flush()

        picks = []
        for pick in client.draft_picks(draft.platform_draft_id):
            picks.append(
                {
                    "pick_no": pick.get("pick_no"),
                    "round": pick.get("round"),
                    "draft_slot": pick.get("draft_slot"),
                    "platform_team_id": (
                        str(pick["roster_id"]) if pick.get("roster_id") else None
                    ),
                    "sleeper_id": pick.get("player_id"),
                    "platform_player_id": pick.get("player_id"),
                    "is_keeper": bool(pick.get("is_keeper")),
                    "bid_amount": (pick.get("metadata") or {}).get("amount"),
                }
            )
        _replace_picks(session, draft, picks)
        drafts.append(draft)
        log.info(
            "Sleeper draft %s: status=%s, %d picks",
            draft.platform_draft_id,
            draft.status,
            len(picks),
        )

    session.commit()
    return drafts


# --- ESPN ------------------------------------------------------------------


def sync_espn_draft(
    session: Session,
    client: EspnClient,
    league: League,
    registry: PlayerRegistry,
    espn_players: dict[int, dict[str, Any]] | None = None,
) -> Draft | None:
    """Pull an ESPN league's draft board.

    Returns None before the draft has started, when ESPN exposes no picks at all.

    ESPN's draft payload identifies players by a bare playerId. Resolving those on
    espn_id alone would only reach the ~23% of players Sleeper has an espn_id for, so
    `espn_players` supplies the full ESPN player objects (id, name, position, team)
    and lifts pick resolution to the same ~99% the roster path achieves.
    """
    payload = client.draft(league.platform_league_id, league.season)
    detail = payload.get("draftDetail") or {}
    all_picks = detail.get("picks") or []

    # ESPN pre-populates the entire draft grid before anyone picks: a 12-team, 16-round
    # league returns 192 "picks" that all carry playerId -1. Those describe the pick
    # order, not selections, and must not be stored as real picks - but their teamIds
    # are the draft order, which is exactly what a slot-based board needs.
    order = _espn_draft_order(all_picks)

    raw_picks = [pick for pick in all_picks if (pick.get("playerId") or -1) > 0]

    if detail.get("inProgress"):
        status = STATUS_DRAFTING
    elif detail.get("drafted"):
        status = STATUS_COMPLETE
    else:
        status = STATUS_PRE_DRAFT

    draft = _upsert_draft(
        session,
        league=league,
        platform=PLATFORM_ESPN,
        # ESPN has no separate draft id; the league identifies it.
        platform_draft_id=f"{league.platform_league_id}-{league.season}",
        status=status,
        draft_type="auction" if any(p.get("bidAmount") for p in raw_picks) else "snake",
        rounds=_espn_rounds(all_picks, raw_picks),
        season=league.season,
    )
    if order:
        draft.draft_order = order
        session.flush()

    if not raw_picks and status == STATUS_PRE_DRAFT:
        # Record the draft as pending and clear anything stale, so a board left over
        # from an earlier sync cannot linger.
        _replace_picks(session, draft, [])
        session.commit()
        log.info("ESPN league %s has not drafted yet", league.platform_league_id)
        return draft

    picks = []
    for pick in raw_picks:
        espn_player_id = pick.get("playerId")
        # ESPN's draft payload carries only a playerId, so resolution goes through
        # the alias table rather than a name.
        sleeper_id = _resolve_espn_player_id(
            registry, espn_player_id, espn_players
        )
        picks.append(
            {
                "pick_no": pick.get("overallPickNumber"),
                "round": pick.get("roundId"),
                "draft_slot": pick.get("roundPickNumber"),
                "platform_team_id": (
                    str(pick["teamId"]) if pick.get("teamId") is not None else None
                ),
                "sleeper_id": sleeper_id,
                "platform_player_id": (
                    str(espn_player_id) if espn_player_id is not None else None
                ),
                "is_keeper": bool(pick.get("keeper")),
                "bid_amount": pick.get("bidAmount") or None,
            }
        )
    _replace_picks(session, draft, picks)
    session.commit()

    log.info(
        "ESPN draft for league %s: status=%s, %d picks",
        league.platform_league_id,
        status,
        len(picks),
    )
    return draft


def _espn_draft_order(picks: list[dict[str, Any]]) -> dict[str, str]:
    """Map draft slot to team id, read from round one of ESPN's grid.

    Round one's pick order *is* the draft order, whether the entries are real picks or
    the placeholders ESPN publishes beforehand. That makes a slot known before a draft
    starts, which is when planning around it actually matters.
    """
    order: dict[str, str] = {}
    for pick in picks:
        if pick.get("roundId") != 1:
            continue
        slot = pick.get("roundPickNumber")
        team_id = pick.get("teamId")
        if slot is None or team_id is None:
            continue
        order[str(slot)] = str(team_id)
    return order


def _espn_rounds(
    all_picks: list[dict[str, Any]], raw_picks: list[dict[str, Any]]
) -> int | None:
    """Total rounds, preferring the full grid so it is known before the draft."""
    source = all_picks or raw_picks
    if not source:
        return None
    return max((p.get("roundId") or 0) for p in source) or None


def _resolve_espn_player_id(
    registry: PlayerRegistry,
    espn_player_id: Any,
    espn_players: dict[int, dict[str, Any]] | None = None,
) -> str | None:
    """Map a bare ESPN playerId onto a Sleeper id.

    Prefers the full ESPN player object when available, since name+position+team
    resolves far more players than espn_id alone.
    """
    if espn_player_id is None:
        return None

    if espn_players:
        player = espn_players.get(int(espn_player_id))
        if player:
            return registry.match_espn_player(player).sleeper_id

    result = registry.match(name=None, position=None, team=None, espn_id=espn_player_id)
    return result.sleeper_id


def build_espn_player_index(
    client: EspnClient, league: League, limit: int = 1500
) -> dict[int, dict[str, Any]]:
    """ESPN player objects keyed by ESPN id, for resolving draft picks."""
    players = client.players(league.platform_league_id, league.season, limit=limit)
    index: dict[int, dict[str, Any]] = {}
    for entry in players:
        player = entry.get("player") or {}
        if player.get("id") is not None:
            index[int(player["id"])] = player
    return index
