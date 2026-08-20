"""League, team, and roster ingestion for both platforms.

Sleeper and ESPN model the same concepts differently, so both are normalized onto the
schema in backend.db. ESPN rosters go through PlayerRegistry to reach Sleeper player
ids; unresolved players are still persisted (with their platform id) so they remain
visible on the roster rather than silently vanishing.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.clients.espn import EspnClient
from backend.clients.sleeper import SleeperClient
from backend.db import (
    PLATFORM_ESPN,
    PLATFORM_SLEEPER,
    League,
    PlayerAlias,
    RosterSlot,
    Team,
)
from backend.resolve import stat_map
from backend.resolve.player_matching import PlayerRegistry, UnresolvedReport
from backend.scoring.rules import unmapped_scoring_keys

log = logging.getLogger(__name__)

# ESPN lineup slot ids that are not starting spots.
ESPN_BENCH_SLOTS = {20, 21}


def _upsert_league(
    session: Session,
    platform: str,
    platform_league_id: str,
    season: str,
    name: str,
    scoring_settings: dict[str, Any],
    roster_positions: list[str],
    total_rosters: int | None,
) -> League:
    league = session.execute(
        select(League).where(
            League.platform == platform,
            League.platform_league_id == platform_league_id,
            League.season == season,
        )
    ).scalar_one_or_none()

    if league is None:
        league = League(
            platform=platform,
            platform_league_id=platform_league_id,
            season=season,
        )
        session.add(league)

    league.name = name
    league.scoring_settings = scoring_settings or {}
    league.roster_positions = roster_positions or []
    league.total_rosters = total_rosters
    session.flush()

    # ESPN leagues store rules as statIds, which this check does not apply to.
    unsupported = (
        unmapped_scoring_keys(league.scoring_settings)
        if platform != PLATFORM_ESPN
        else []
    )
    if unsupported:
        log.warning(
            "League %r prices rules that ESPN stat lines cannot express, so they "
            "contribute nothing to ESPN-sourced projections: %s",
            name,
            ", ".join(unsupported),
        )
    return league


def _upsert_team(
    session: Session,
    league: League,
    platform_team_id: str,
    name: str,
    owner_name: str | None,
    is_mine: bool,
    record: dict[str, Any],
) -> Team:
    team = session.execute(
        select(Team).where(
            Team.league_id == league.id,
            Team.platform_team_id == platform_team_id,
        )
    ).scalar_one_or_none()

    if team is None:
        team = Team(league_id=league.id, platform_team_id=platform_team_id)
        session.add(team)

    team.name = name
    team.owner_name = owner_name
    team.is_mine = is_mine
    team.wins = record.get("wins", 0)
    team.losses = record.get("losses", 0)
    team.ties = record.get("ties", 0)
    team.points_for = record.get("points_for", 0.0)
    team.points_against = record.get("points_against", 0.0)
    session.flush()
    return team


def _replace_roster(session: Session, team: Team, slots: list[dict[str, Any]]) -> None:
    """Rosters change constantly, so they are rewritten wholesale each sync."""
    for existing in session.execute(
        select(RosterSlot).where(RosterSlot.team_id == team.id)
    ).scalars():
        session.delete(existing)
    session.flush()

    for slot in slots:
        session.add(RosterSlot(team_id=team.id, **slot))
    session.flush()


def _record_alias(
    session: Session,
    platform: str,
    platform_player_id: str,
    sleeper_id: str,
    method: str,
    confidence: float,
) -> None:
    alias = session.execute(
        select(PlayerAlias).where(
            PlayerAlias.platform == platform,
            PlayerAlias.platform_player_id == platform_player_id,
        )
    ).scalar_one_or_none()

    if alias is None:
        session.add(
            PlayerAlias(
                platform=platform,
                platform_player_id=platform_player_id,
                sleeper_id=sleeper_id,
                method=method,
                confidence=confidence,
            )
        )
    else:
        alias.sleeper_id = sleeper_id
        alias.method = method
        alias.confidence = confidence


# --- Sleeper ---------------------------------------------------------------


def sync_sleeper_leagues(
    session: Session,
    client: SleeperClient,
    username: str,
    season: str,
) -> list[League]:
    """Pull every Sleeper league the user is in for a season."""
    user_id = client.user_id(username)
    raw_leagues = client.leagues(user_id, season)
    log.info("Sleeper: found %d leagues for %s in %s", len(raw_leagues), username, season)

    leagues: list[League] = []
    for raw in raw_leagues:
        league = _upsert_league(
            session,
            platform=PLATFORM_SLEEPER,
            platform_league_id=str(raw["league_id"]),
            season=season,
            name=raw.get("name") or "Sleeper league",
            scoring_settings=raw.get("scoring_settings") or {},
            roster_positions=raw.get("roster_positions") or [],
            total_rosters=raw.get("total_rosters"),
        )

        rosters = client.rosters(league.platform_league_id)
        users = {
            str(u["user_id"]): u for u in client.league_users(league.platform_league_id)
        }

        for roster in rosters:
            owner_id = str(roster.get("owner_id") or "")
            owner = users.get(owner_id, {})
            team_name = (owner.get("metadata") or {}).get("team_name")
            settings = roster.get("settings") or {}

            # Sleeper splits points into whole and decimal parts.
            points_for = float(settings.get("fpts", 0)) + float(
                settings.get("fpts_decimal", 0)
            ) / 100.0
            points_against = float(settings.get("fpts_against", 0)) + float(
                settings.get("fpts_against_decimal", 0)
            ) / 100.0

            team = _upsert_team(
                session,
                league=league,
                platform_team_id=str(roster["roster_id"]),
                name=team_name or owner.get("display_name") or f"Roster {roster['roster_id']}",
                owner_name=owner.get("display_name"),
                is_mine=(owner_id == user_id),
                record={
                    "wins": settings.get("wins", 0),
                    "losses": settings.get("losses", 0),
                    "ties": settings.get("ties", 0),
                    "points_for": round(points_for, 2),
                    "points_against": round(points_against, 2),
                },
            )

            starters = [p for p in (roster.get("starters") or []) if p and p != "0"]
            starter_set = set(starters)
            all_players = [p for p in (roster.get("players") or []) if p]

            roster_positions = league.roster_positions or []
            slots: list[dict[str, Any]] = []
            for index, player_id in enumerate(starters):
                slot_name = (
                    roster_positions[index] if index < len(roster_positions) else None
                )
                slots.append(
                    {
                        "sleeper_id": player_id,
                        "platform_player_id": player_id,
                        "slot": slot_name,
                        "is_starter": True,
                    }
                )
            for player_id in all_players:
                if player_id in starter_set:
                    continue
                slots.append(
                    {
                        "sleeper_id": player_id,
                        "platform_player_id": player_id,
                        "slot": "BN",
                        "is_starter": False,
                    }
                )
            _replace_roster(session, team, slots)

        leagues.append(league)

    session.commit()
    return leagues


# --- ESPN ------------------------------------------------------------------


def _espn_team_name(team: dict[str, Any]) -> str:
    name = team.get("name")
    if name:
        return str(name).strip()
    # Older payloads split the name in two.
    parts = [team.get("location"), team.get("nickname")]
    joined = " ".join(p for p in parts if p).strip()
    return joined or f"Team {team.get('id')}"


def sync_espn_league(
    session: Session,
    client: EspnClient,
    league_id: str,
    season: str,
    registry: PlayerRegistry,
    my_team_id: str | None = None,
) -> tuple[League, UnresolvedReport]:
    """Pull one ESPN league, resolving its rosters onto Sleeper player ids."""
    payload = client.league(
        league_id, season, views=("mTeam", "mRoster", "mSettings")
    )
    settings = payload.get("settings") or {}

    roster_positions = _espn_roster_positions(settings)
    league = _upsert_league(
        session,
        platform=PLATFORM_ESPN,
        platform_league_id=str(league_id),
        season=season,
        name=settings.get("name") or f"ESPN league {league_id}",
        # ESPN prices scoring in its own format; projections for ESPN leagues are
        # scored from raw stats using this league's rules once translated.
        scoring_settings=_espn_scoring_settings(settings),
        roster_positions=roster_positions,
        total_rosters=len(payload.get("teams") or []),
    )

    members = {str(m.get("id")): m for m in (payload.get("members") or [])}
    my_swid = (client.swid or "").upper()

    report = UnresolvedReport()
    identified_my_team = False

    for team in payload.get("teams") or []:
        owners = [str(o).upper() for o in (team.get("owners") or [])]
        owner_member = members.get(owners[0]) if owners else None
        owner_name = None
        if owner_member:
            owner_name = (
                owner_member.get("displayName")
                or " ".join(
                    p
                    for p in (
                        owner_member.get("firstName"),
                        owner_member.get("lastName"),
                    )
                    if p
                )
                or None
            )

        overall = (team.get("record") or {}).get("overall") or {}
        db_team = _upsert_team(
            session,
            league=league,
            platform_team_id=str(team.get("id")),
            name=_espn_team_name(team),
            owner_name=owner_name,
            is_mine=_is_my_team(team, owners, my_swid, my_team_id),
            record={
                "wins": overall.get("wins", 0),
                "losses": overall.get("losses", 0),
                "ties": overall.get("ties", 0),
                "points_for": round(float(overall.get("pointsFor", 0.0)), 2),
                "points_against": round(float(overall.get("pointsAgainst", 0.0)), 2),
            },
        )

        if db_team.is_mine:
            identified_my_team = True

        slots: list[dict[str, Any]] = []
        for entry in ((team.get("roster") or {}).get("entries") or []):
            player = (entry.get("playerPoolEntry") or {}).get("player") or {}
            if not player:
                continue

            report.total += 1
            result = registry.match_espn_player(player)
            descriptor = {
                "espn_id": player.get("id"),
                "name": player.get("fullName"),
                "position": stat_map.position(player.get("defaultPositionId")),
                "team": stat_map.pro_team(player.get("proTeamId")),
            }
            if result.matched:
                _record_alias(
                    session,
                    PLATFORM_ESPN,
                    str(player.get("id")),
                    result.sleeper_id,
                    result.method,
                    result.confidence,
                )
                if result.ambiguous:
                    report.record_ambiguous(descriptor, result.candidates)
            else:
                report.record_unmatched(descriptor)

            slot_id = entry.get("lineupSlotId")
            slots.append(
                {
                    # Unresolved players keep their ESPN id so they stay visible.
                    "sleeper_id": result.sleeper_id,
                    "platform_player_id": str(player.get("id")),
                    "slot": stat_map.position(slot_id) if slot_id is not None else None,
                    "is_starter": slot_id not in ESPN_BENCH_SLOTS,
                }
            )
        _replace_roster(session, db_team, slots)

    if not identified_my_team:
        log.warning(
            "Could not identify your team in ESPN league %s. Your SWID does not "
            "appear among its members, which happens when the league references an "
            "older ESPN identity. Set ESPN_TEAM_ID in .env to pick it explicitly.",
            league_id,
        )

    session.commit()
    return league, report


def _is_my_team(
    team: dict[str, Any],
    owners: list[str],
    my_swid: str,
    my_team_id: str | None,
) -> bool:
    """Whether this ESPN team belongs to the user.

    An explicit ESPN_TEAM_ID always wins, because SWID matching fails whenever the
    league's membership records a different ESPN identity than the browser session
    the cookies came from.
    """
    if my_team_id:
        return str(team.get("id")) == str(my_team_id)
    return bool(my_swid and my_swid in owners)


def _espn_roster_positions(settings: dict[str, Any]) -> list[str]:
    """Expand ESPN's lineupSlotCounts map into a flat list of slots."""
    counts = (settings.get("rosterSettings") or {}).get("lineupSlotCounts") or {}
    positions: list[str] = []
    for slot_id, count in sorted(counts.items(), key=lambda kv: int(kv[0])):
        name = stat_map.position(int(slot_id))
        if not name or not count:
            continue
        normalized = "BN" if name == "BE" else name
        positions.extend([normalized] * int(count))
    return positions


def _espn_scoring_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """An ESPN league's scoring rules, kept in ESPN's own statId vocabulary.

    Deliberately not translated into Sleeper keys. ESPN and Sleeper bucket points
    allowed differently (14-17 / 18-21 / 22-27 against 14-20 / 21-27), and ESPN prices
    statIds the vendored name map does not cover, so translation is lossy in both
    directions. Scoring ESPN leagues on their own statIds is exact instead.

    Defensive rules carry their real value in pointsOverrides under key "16", which is
    ESPN's D/ST position id, while the top-level `points` sits at 0. Reading only
    `points` scored every sack, interception, and fumble recovery as zero.
    """
    out: dict[str, float] = {}
    items = (settings.get("scoringSettings") or {}).get("scoringItems") or []
    for item in items:
        stat_id = item.get("statId")
        if stat_id is None:
            continue
        points = item.get("points") or 0.0
        if not points:
            overrides = item.get("pointsOverrides") or {}
            # "16" is D/ST; fall back to any single override the rule defines.
            points = overrides.get("16")
            if points is None and len(overrides) == 1:
                points = next(iter(overrides.values()))
        if not points:
            continue
        out[str(stat_id)] = float(points)
    return out
