"""Projection ingestion.

ESPN is the stat source for every league, including the Sleeper ones. That is not a
compromise: the raw stat line is league-independent, so the same projected rushing
yards can be scored under a superflex PPR Sleeper league's rules and a standard ESPN
league's rules and yield genuinely comparable numbers. Trusting either platform's own
point totals would not.

Season projections live in the entry with statSourceId 1 and scoringPeriodId 0.
Weekly projections use the same statSourceId with the week number.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.clients.espn import EspnClient
from backend.db import League, Projection
from backend.resolve.player_matching import PlayerRegistry
from backend.resolve.stat_map import named_stats
from backend.analysis.adp_board import board_rank_from
from backend.db import PLATFORM_ESPN
from backend.scoring.rules import (
    espn_to_sleeper_stats,
    score_espn_raw,
    score_stats,
)

log = logging.getLogger(__name__)

SOURCE_ESPN = "espn"
SEASON_WEEK = 0  # Projection.week 0 means a full-season total.

STAT_SOURCE_PROJECTED = 1


def _projection_entry(
    player: dict[str, Any], season: str, week: int
) -> dict[str, Any] | None:
    """The projected stat line for a season or a single week."""
    for entry in player.get("stats") or []:
        if entry.get("statSourceId") != STAT_SOURCE_PROJECTED:
            continue
        if str(entry.get("seasonId")) != str(season):
            continue
        if (entry.get("scoringPeriodId") or 0) != week:
            continue
        if entry.get("stats"):
            return entry
    return None


def fetch_player_pool(
    client: EspnClient,
    source_league_id: str,
    season: str,
    limit: int = 700,
    week: int | None = None,
) -> list[dict[str, Any]]:
    """The draftable player pool, ordered by ESPN's PPR draft rank.

    Any accessible league works as the source, since raw stat projections do not
    depend on league settings.
    """
    entries = client.players(source_league_id, season, limit=limit, week=week)
    return [e.get("player") or {} for e in entries if e.get("player")]


def _score_for_league(
    league: League,
    raw_by_id: dict[str, float],
    sleeper_stats: dict[str, float],
) -> float:
    """Score one stat line under one league's rules.

    ESPN leagues keep their rules in ESPN's own statId vocabulary and are scored
    directly against it; Sleeper leagues are scored in Sleeper's key vocabulary. Each
    platform is scored in the language its own rules are written in, so nothing is
    lost in translation.
    """
    settings = league.scoring_settings or {}
    if league.platform == PLATFORM_ESPN:
        return score_espn_raw(raw_by_id, settings)
    return score_stats(sleeper_stats, settings)


def sync_projections(
    session: Session,
    leagues: Iterable[League],
    players: list[dict[str, Any]],
    registry: PlayerRegistry,
    season: str,
    week: int = SEASON_WEEK,
) -> int:
    """Score one ESPN player pool under every league's own rules.

    Returns the number of projection rows written.
    """
    leagues = list(leagues)
    if not leagues:
        return 0

    # Clear this season/week's projections so a re-sync cannot leave stale rows for
    # players who dropped out of the pool.
    league_ids = [lg.id for lg in leagues]
    for stale in session.execute(
        select(Projection).where(
            Projection.season == str(season),
            Projection.week == week,
            Projection.source == SOURCE_ESPN,
            Projection.league_id.in_(league_ids),
        )
    ).scalars():
        session.delete(stale)
    session.flush()

    written = 0
    unresolved = 0

    for player in players:
        result = registry.match_espn_player(player)
        if not result.matched:
            unresolved += 1
            continue

        entry = _projection_entry(player, season, week)
        if entry is None:
            continue

        raw_by_id = entry["stats"]
        raw = named_stats(raw_by_id)
        sleeper_stats = espn_to_sleeper_stats(raw)

        # Each league gets the rank matching its own format, so a superflex league's
        # board is genuinely a different board rather than the same one relabelled.
        draft_ranks = player.get("draftRanksByRankType") or {}

        ownership = player.get("ownership") or {}
        adp = ownership.get("averageDraftPosition")
        auction = ownership.get("auctionValueAverage")
        owned = ownership.get("percentOwned")

        for league in leagues:
            points = _score_for_league(league, raw_by_id, sleeper_stats)
            session.add(
                Projection(
                    sleeper_id=result.sleeper_id,
                    league_id=league.id,
                    season=str(season),
                    week=week,
                    source=SOURCE_ESPN,
                    points=points,
                    raw_stats=raw,
                    board_rank=board_rank_from(draft_ranks, league),
                    adp=float(adp) if adp is not None else None,
                    auction_value=float(auction) if auction is not None else None,
                    percent_owned=float(owned) if owned is not None else None,
                )
            )
            written += 1

    session.commit()
    log.info(
        "Projections: %d rows across %d leagues (%d players unresolved)",
        written,
        len(leagues),
        unresolved,
    )
    return written
