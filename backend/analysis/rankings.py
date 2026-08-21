"""Personal player rankings.

The market board says what the field thinks. This is what *you* think, and the two
disagreeing is the whole point: a player you rank twenty spots above consensus is a
player you can wait on, and one you rank below is a trap at their ADP.

Rankings are per league, because a board is per league. A superflex league values
quarterbacks nothing like a PPR league does, so a single global list would be wrong for
most of your leagues at once.

Nothing is stored until you actually reorder something. An empty table means "use the
market board", so a league you have never touched still behaves sensibly.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Sequence

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from backend.analysis.adp_board import (
    _board,
    personal_ranks,
    target_band,
    target_score,
)
from backend.db import League, PersonalRank

__all__ = [
    "personal_ranks",
    "has_custom_ranks",
    "save_order",
    "reset",
    "ranking_board",
]

log = logging.getLogger(__name__)


def has_custom_ranks(session: Session, league_id: int) -> bool:
    return (
        session.execute(
            select(PersonalRank.id).where(PersonalRank.league_id == league_id).limit(1)
        ).first()
        is not None
    )


def save_order(
    session: Session, league_id: int, sleeper_ids: Sequence[str]
) -> int:
    """Replace this league's ranking with the given order.

    The client sends the complete list it is showing, so the saved ranking is always
    a full ordering rather than a patch. Replacing wholesale keeps ranks dense and
    avoids drift between what was displayed and what was stored.
    """
    league = session.get(League, league_id)
    if league is None:
        raise ValueError(f"no league {league_id}")

    session.execute(
        delete(PersonalRank).where(PersonalRank.league_id == league_id)
    )
    session.flush()

    seen: set[str] = set()
    rank = 0
    for sleeper_id in sleeper_ids:
        if not sleeper_id or sleeper_id in seen:
            continue  # a duplicate would break the unique constraint
        seen.add(sleeper_id)
        rank += 1
        session.add(
            PersonalRank(league_id=league_id, sleeper_id=sleeper_id, rank=rank)
        )

    session.commit()
    log.info("Saved %d personal ranks for league %s", rank, league_id)
    return rank


def reset(session: Session, league_id: int) -> int:
    """Drop the personal ranking, reverting the league to the market board."""
    removed = session.execute(
        delete(PersonalRank).where(PersonalRank.league_id == league_id)
    ).rowcount
    session.commit()
    return removed or 0


def ranking_board(
    session: Session,
    league_id: int,
    picks: Iterable[int] | None = None,
) -> dict[str, Any]:
    """The user's ranked list for a league, seeded from the market board.

    `picks` colours each row by how well it lines up with a pick the user owns, the
    same way the Players view does, so reordering can be done with the consequences
    in view rather than in the abstract.
    """
    league = session.get(League, league_id)
    if league is None:
        raise ValueError(f"no league {league_id}")

    board = _board(session, league)
    saved = personal_ranks(session, league_id)
    custom = bool(saved)

    if custom:
        # Anything saved keeps its rank; anything new (a player who has since become
        # available) falls in behind, still in market order.
        ordered = sorted(
            board,
            key=lambda p: (saved.get(p.sleeper_id, len(saved) + p.board_slot),),
        )
    else:
        ordered = list(board)

    pick_list = list(picks or [])
    pick_set = set(pick_list)

    players: list[dict[str, Any]] = []
    for position, player in enumerate(ordered, start=1):
        score, best_pick = target_score(position, pick_list)
        players.append(
            {
                "sleeperId": player.sleeper_id,
                "name": player.name,
                "position": player.position,
                "proTeam": player.pro_team,
                "myRank": position,
                "boardRank": round(player.board_rank, 1),
                "marketSlot": player.board_slot,
                # Positive means you rank them higher than the market does.
                "delta": player.board_slot - position,
                "points": round(player.points, 1),
                "vorp": player.vorp,
                "adp": player.adp if (player.adp and player.adp < 165) else None,
                "targetScore": round(score, 3),
                "targetBand": target_band(score),
                "bestPick": best_pick,
                "onMyPick": position in pick_set,
            }
        )

    return {
        "league": {"id": league.id, "name": league.name},
        "isCustom": custom,
        "count": len(players),
        "players": players,
    }
