"""Positional dropoff curves.

Projected points against positional rank, one line per position. This is the picture
behind value over replacement: what matters at a pick is not how many points a player
scores but how much better he is than the next one at his position, and that gap is a
slope you can see.

A position with a cliff early (elite tight ends, and quarterbacks in superflex) rewards
taking one before it. A position that decays gently is one to wait on, because the
twentieth is nearly the twelfth.

Curves are per league for the same reason everything else is: the same raw stat line
scores differently under different rules, so the shape of the cliff differs too.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from backend.analysis.adp_board import _board
from backend.analysis.draft import replacement_levels, slot_requirements
from backend.db import League

log = logging.getLogger(__name__)

# The four positions worth plotting. Kickers and defenses are flat by comparison and
# only compress the vertical scale for the positions the draft is actually decided on.
CURVE_POSITIONS = ("QB", "RB", "WR", "TE")

DEFAULT_DEPTH = 40


def dropoff_curves(
    session: Session, league_id: int, depth: int = DEFAULT_DEPTH
) -> dict[str, Any]:
    """One series per position: projected points by positional rank."""
    league = session.get(League, league_id)
    if league is None:
        raise ValueError(f"no league {league_id}")

    board = _board(session, league)
    team_count = league.total_rosters or 12

    by_position: dict[str, list[Any]] = {}
    for player in board:
        if player.position in CURVE_POSITIONS:
            by_position.setdefault(player.position, []).append(player)
    for players in by_position.values():
        players.sort(key=lambda p: p.points, reverse=True)

    # Replacement level needs the whole pool, not the truncated view.
    points_by_position = {
        position: [p.points for p in players]
        for position, players in by_position.items()
    }
    replacement, starters = replacement_levels(
        league.roster_positions or [], team_count, points_by_position
    )
    dedicated, _ = slot_requirements(league.roster_positions or [])

    series: list[dict[str, Any]] = []
    for position in CURVE_POSITIONS:
        players = by_position.get(position, [])[:depth]
        if not players:
            continue
        series.append(
            {
                "position": position,
                # Points where the position stops being startable league-wide. The
                # cliff relative to this line is what a pick is really buying.
                "replacement": round(replacement.get(position, 0.0), 1),
                "startersLeagueWide": starters.get(position, 0),
                "startersPerTeam": dedicated.get(position, 0),
                "points": [
                    {
                        "rank": index,
                        "points": round(player.points, 1),
                        "name": player.name,
                        "proTeam": player.pro_team,
                        "boardSlot": player.board_slot,
                    }
                    for index, player in enumerate(players, start=1)
                ],
            }
        )

    # A single shared y-scale: the whole point is comparing slopes between positions,
    # which a per-series axis would destroy.
    all_points = [p["points"] for s in series for p in s["points"]]

    return {
        "league": {
            "id": league.id,
            "name": league.name,
            "teamCount": team_count,
            "platform": league.platform,
        },
        "depth": depth,
        "yMin": min(all_points) if all_points else 0,
        "yMax": max(all_points) if all_points else 0,
        "series": series,
    }
