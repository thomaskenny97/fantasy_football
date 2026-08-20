"""Draft ADP board and availability heat map.

The live assistant answers "who should I take now". This answers the question you plan
around: **given my slot, who will still be there at each of my picks?** Knowing that
round 2 will be thick with receivers near your pick is what tells you to take a running
back in round 1.

Three things make each league's board its own:

  1. **Format.** ESPN publishes draft ranks per format. A superflex league's board is a
     different board - seven quarterbacks sit in the top 24 against none in PPR.
  2. **Team count and draft type.** Ten teams and twelve teams put entirely different
     players at your pick, and a linear draft repeats the same slot every round while a
     snake reverses it.
  3. **Your slot.** At slot 12 of 12 you pick in pairs at the turn and then wait 22
     picks; at slot 1 you never pick back to back.

Board ordering comes from ESPN's rank rather than its ADP. ADP saturates: 295 players
share a single value around pick 170, so it cannot order a 16-round board. Rank is
strictly ordered with no ties for all 700 players. ADP is still shown where it
discriminates, as the market signal it is.
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from dataclasses import dataclass
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.analysis.board import (
    DRAFTABLE,
    _available_players,
    _my_team,
    _taken_player_ids,
)
from backend.analysis.draft import replacement_levels
from backend.db import Draft, League, Player, Projection

log = logging.getLogger(__name__)

RANK_SUPERFLEX = "SUPERFLEX"
RANK_PPR = "PPR"
RANK_STANDARD = "STANDARD"

DRAFT_TYPE_LINEAR = "linear"

# Availability bands, high to low. The 35-55% band is the one that matters: those are
# the players genuinely in play at your pick rather than certainties either way.
HEAT_BANDS: tuple[tuple[float, str], ...] = (
    (0.85, "likely"),
    (0.55, "probable"),
    (0.35, "coinflip"),
    (0.15, "unlikely"),
    (0.0, "gone"),
)


def rank_type_for(league: League) -> tuple[str, float]:
    """The rank source for a league, as (base rank type, blend toward PPR).

    A superflex league uses ESPN's SUPERFLEX board outright. Everything else starts
    from STANDARD and blends toward PPR by the league's reception value, so half-PPR
    lands halfway rather than being forced to one side.
    """
    if "SUPER_FLEX" in (league.roster_positions or []):
        return RANK_SUPERFLEX, 0.0

    reception = float((league.scoring_settings or {}).get("rec", 0.0) or 0.0)
    # ESPN league scoring is keyed by statId; 53 is receptions.
    if not reception:
        reception = float((league.scoring_settings or {}).get("53", 0.0) or 0.0)
    return RANK_STANDARD, max(0.0, min(reception, 1.0))


def board_rank_from(
    draft_ranks: dict[str, Any] | None, league: League
) -> float | None:
    """This league's board rank for one player, from ESPN's draftRanksByRankType."""
    if not draft_ranks:
        return None

    base_type, blend = rank_type_for(league)

    if base_type == RANK_SUPERFLEX:
        entry = draft_ranks.get(RANK_SUPERFLEX) or {}
        rank = entry.get("rank")
        return float(rank) if rank else None

    standard = (draft_ranks.get(RANK_STANDARD) or {}).get("rank")
    ppr = (draft_ranks.get(RANK_PPR) or {}).get("rank")
    if standard and ppr:
        return float(standard) + (float(ppr) - float(standard)) * blend
    rank = ppr or standard
    return float(rank) if rank else None


def pick_numbers(
    slot: int, team_count: int, rounds: int, draft_type: str | None
) -> list[int]:
    """Overall pick numbers for one draft slot.

    Snake drafts reverse every other round; linear drafts repeat the same slot. Verified
    against ESPN's own grid: slot 12 of 12 gives 12, 13, 36, 37, 60, 61, ...
    """
    if team_count <= 0 or rounds <= 0:
        return []
    slot = max(1, min(slot, team_count))

    linear = (draft_type or "").lower() == DRAFT_TYPE_LINEAR
    picks: list[int] = []
    for rnd in range(1, rounds + 1):
        if linear or rnd % 2 == 1:
            picks.append((rnd - 1) * team_count + slot)
        else:
            picks.append(rnd * team_count - slot + 1)
    return picks


def availability(rank: float, pick: int) -> float:
    """Probability a player at `rank` is still on the board at overall `pick`.

    Real draft position scatters around consensus rank, and the scatter widens deeper
    into the draft - the first few picks are near-certain, round 12 is a lottery. Model
    actual position as normal about the rank with a spread that grows with it.

    ESPN publishes no ADP standard deviation, so the spread is a heuristic. Calibrated
    to behave sensibly at the boundaries: rank 5 at pick 12 is 4%, rank 13 at pick 12 is
    60%, rank 24 at pick 12 is 99%.
    """
    if rank <= 0:
        return 0.0
    spread = min(max(0.20 * rank, 4.0), 30.0)
    z = (pick - rank) / (spread * math.sqrt(2.0))
    return max(0.0, min(1.0, 1.0 - 0.5 * (1.0 + math.erf(z))))


# How strongly a player's board rank has to line up with one of your picks before
# the row lights up. High to low.
TARGET_BANDS: tuple[tuple[float, str], ...] = (
    (0.60, "prime"),
    (0.25, "good"),
    (0.05, "fringe"),
    (0.0, "dead"),
)


def target_score(rank: float, picks: Sequence[int]) -> tuple[float, int | None]:
    """How well a player's board rank lines up with any of your own picks.

    Returns (score in 0..1, the pick it lines up with).

    A player is most gettable when their rank sits right on one of your picks: take
    them earlier and you reached, wait for your next pick and they are gone. Score
    therefore peaks where rank equals a pick and falls away on both sides, with the
    window widening later in the draft because deep picks are far less predictable.

    This is what exposes a slot's dead zones. At slot 12 of a 12-team snake you pick
    at 12, 13, 36 and 37, so players ranked around 24 line up with nothing you own -
    a reach at 13 and long gone by 36. That gap is real, and it is the reason the
    same board looks completely different from a different seat.
    """
    if rank <= 0 or not picks:
        return 0.0, None

    best_score = 0.0
    best_pick: int | None = None
    for pick in picks:
        width = max(3.5, 0.18 * pick)
        score = math.exp(-(((rank - pick) / width) ** 2))
        if score > best_score:
            best_score = score
            best_pick = pick
    return best_score, best_pick


def target_band(score: float) -> str:
    for threshold, name in TARGET_BANDS:
        if score >= threshold:
            return name
    return "dead"


def heat_band(probability: float) -> str:
    for threshold, name in HEAT_BANDS:
        if probability >= threshold:
            return name
    return "gone"


@dataclass
class BoardPlayer:
    sleeper_id: str
    name: str
    position: str
    pro_team: str | None
    # The player's ESPN market rank, kept for display.
    board_rank: float
    # Their position on THIS league's draftable board, which is what actually
    # predicts when they go. The two diverge sharply in keeper and dynasty leagues:
    # the best free agent may be globally ranked 150th yet go first overall here.
    board_slot: int
    points: float
    vorp: float
    adp: float | None


def detect_slot(session: Session, league: League) -> int | None:
    """The user's draft slot, from the platform's published draft order."""
    team = _my_team(session, league.id)
    if team is None:
        return None

    draft = session.execute(
        select(Draft).where(Draft.league_id == league.id)
    ).scalars().first()
    order = (draft.draft_order if draft else None) or {}

    for slot, platform_team_id in order.items():
        if str(platform_team_id) == str(team.platform_team_id):
            try:
                return int(slot)
            except (TypeError, ValueError):
                continue
    return None


def _board(session: Session, league: League) -> list[BoardPlayer]:
    """Every draftable player with a board rank, best first.

    Players already rostered are excluded, which matters in keeper and dynasty leagues
    where most of the pool is locked up before a pick is made.
    """
    rows = session.execute(
        select(Projection, Player)
        .join(Player, Player.sleeper_id == Projection.sleeper_id)
        .where(
            Projection.league_id == league.id,
            Projection.week == 0,
            Projection.season == league.season,
            Projection.board_rank.is_not(None),
        )
    ).all()

    # Players already on a roster in this league cannot be drafted. In a redraft
    # league before its draft this removes nothing; in a dynasty league it removes
    # most of the pool, which is exactly right.
    draft = session.execute(
        select(Draft).where(Draft.league_id == league.id)
    ).scalars().first()
    taken = _taken_player_ids(session, league, draft)
    pool = {p.sleeper_id: p for p in _available_players(session, league, taken)}

    by_position: dict[str, list[float]] = {}
    for projection, player in rows:
        if player.position in DRAFTABLE:
            by_position.setdefault(player.position, []).append(projection.points)
    for ranked in by_position.values():
        ranked.sort(reverse=True)

    replacement, _ = replacement_levels(
        league.roster_positions or [], league.total_rosters or 12, by_position
    )

    board: list[BoardPlayer] = []
    for projection, player in rows:
        if player.position not in DRAFTABLE:
            continue
        if player.sleeper_id not in pool:
            continue  # already rostered
        baseline = replacement.get(player.position or "", 0.0)
        board.append(
            BoardPlayer(
                sleeper_id=player.sleeper_id,
                name=player.full_name or player.sleeper_id,
                position=player.position or "",
                pro_team=player.pro_team,
                board_rank=float(projection.board_rank),
                board_slot=0,  # assigned once the board is ordered
                points=projection.points,
                vorp=round(projection.points - baseline, 1),
                adp=projection.adp,
            )
        )

    board.sort(key=lambda p: p.board_rank)
    for index, player in enumerate(board, start=1):
        player.board_slot = index
    return board


def _player_payload(player: BoardPlayer, pick: int | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "sleeperId": player.sleeper_id,
        "name": player.name,
        "position": player.position,
        "proTeam": player.pro_team,
        "boardRank": round(player.board_rank, 1),
        "points": round(player.points, 1),
        "vorp": player.vorp,
        # ADP saturates near 170, so it is only reported where it still says something.
        "adp": player.adp if (player.adp and player.adp < 165) else None,
    }
    if pick is not None:
        probability = availability(player.board_slot, pick)
        payload["availability"] = round(probability, 3)
        payload["band"] = heat_band(probability)
    return payload


def heat_map(
    session: Session,
    league_id: int,
    slot: int | None = None,
    rounds: int | None = None,
    per_round: int = 10,
) -> dict[str, Any]:
    """Per-round targets and the full projected board for one league and slot."""
    league = session.get(League, league_id)
    if league is None:
        raise ValueError(f"no league {league_id}")

    draft = session.execute(
        select(Draft).where(Draft.league_id == league_id)
    ).scalars().first()

    team_count = league.total_rosters or 12
    draft_type = draft.draft_type if draft else None
    total_rounds = rounds or (draft.rounds if draft and draft.rounds else None) or len(
        [s for s in (league.roster_positions or []) if s != "IR"]
    ) or 15

    detected = detect_slot(session, league)
    active_slot = slot or detected or 1
    active_slot = max(1, min(active_slot, team_count))

    board = _board(session, league)
    picks = pick_numbers(active_slot, team_count, total_rounds, draft_type)

    base_type, blend = rank_type_for(league)
    if base_type == RANK_SUPERFLEX:
        board_label = "superflex"
    elif blend >= 0.75:
        board_label = "PPR"
    elif blend > 0:
        board_label = f"{blend:g}-point PPR"
    else:
        board_label = "standard"

    # --- per-round targets ------------------------------------------------
    # Walk the board forward, consuming exactly the picks that occur. Between your
    # pick at 12 and your pick at 13 only one player leaves the board; between 13 and
    # 36 twenty-three do. Modelling that gap is the whole point at the turn.
    remaining = list(board)
    consumed = max(picks[0] - 1, 0) if picks else 0
    remaining = remaining[consumed:]

    round_rows: list[dict[str, Any]] = []
    for index, pick in enumerate(picks, start=1):
        targets = remaining[:per_round]

        counts = Counter(p.position for p in targets)
        round_rows.append(
            {
                "round": index,
                "pick": pick,
                "slotInRound": ((pick - 1) % team_count) + 1,
                "targets": [_player_payload(p, pick) for p in targets],
                # The point of the feature: what is realistically there, by position.
                "positionCounts": [
                    {"position": position, "count": count}
                    for position, count in counts.most_common()
                ],
            }
        )

        # Advance the board by however many picks happen before you are up again.
        next_pick = picks[index] if index < len(picks) else pick + team_count
        remaining = remaining[max(next_pick - pick, 1) :]

    # --- full grid --------------------------------------------------------
    grid: list[dict[str, Any]] = []
    for rnd in range(1, total_rounds + 1):
        cells: list[dict[str, Any]] = []
        for column in range(1, team_count + 1):
            if (draft_type or "").lower() == DRAFT_TYPE_LINEAR or rnd % 2 == 1:
                pick = (rnd - 1) * team_count + column
            else:
                pick = rnd * team_count - column + 1
            player = board[pick - 1] if pick - 1 < len(board) else None
            cells.append(
                {
                    "pick": pick,
                    "slot": column,
                    "isMine": column == active_slot,
                    "player": _player_payload(player) if player else None,
                }
            )
        grid.append({"round": rnd, "cells": cells})

    # --- flat board, every player, annotated for this slot -----------------
    # Round is derived from board position rather than the player's own rank, so the
    # dividers land every `team_count` rows exactly as a real board reads.
    pick_set = set(picks)
    all_players: list[dict[str, Any]] = []
    for player in board:
        position_on_board = player.board_slot
        score, best_pick = target_score(player.board_slot, picks)
        payload = _player_payload(player)
        payload.update(
            {
                "boardSlot": position_on_board,
                "round": ((position_on_board - 1) // team_count) + 1,
                "targetScore": round(score, 3),
                "targetBand": target_band(score),
                "bestPick": best_pick,
                "availabilityAtBestPick": (
                    round(availability(player.board_slot, best_pick), 3)
                    if best_pick
                    else None
                ),
                # True when the player sits exactly on one of your picks.
                "onMyPick": position_on_board in pick_set,
            }
        )
        all_players.append(payload)

    return {
        "league": {
            "id": league.id,
            "name": league.name,
            "platform": league.platform,
            "teamCount": team_count,
            "rosterPositions": league.roster_positions,
        },
        "boardType": board_label,
        "draftType": draft_type or "snake",
        "rounds": total_rounds,
        "slot": active_slot,
        "detectedSlot": detected,
        "myPicks": picks,
        "boardSize": len(board),
        "roundTargets": round_rows,
        "grid": grid,
        "players": all_players,
    }
