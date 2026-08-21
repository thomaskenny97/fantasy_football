"""Stochastic mock drafts.

Two things this answers that a static board cannot: what a roster from *your* slot
actually ends up looking like, and how that changes if you open the draft differently.

The randomness model draws each player's draft position once per simulation rather than
once per pick. A real draft has a shape - this is the year Player X slid, this is the
year he did not - and re-rolling at every pick would wash that out into an average that
never happens. So each run fixes a plausible ordering and then plays it forward, which
is why two runs from the same slot can look genuinely different.

Rival teams draft the market board with noise. Your team drafts *your* rankings with
less noise, so your own opinions drive your roster while still leaving room for the
board to surprise you.
"""

from __future__ import annotations

import logging
import random
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.analysis.adp_board import (
    _board,
    BoardPlayer,
    personal_ranks,
    pick_numbers,
)
from backend.analysis.draft import slot_requirements, unfilled_starting_slots
from backend.db import Draft, League
from backend.scoring.rules import SLOT_ELIGIBILITY

log = logging.getLogger(__name__)

BALANCED = "balanced"
RB_RB = "rb_rb"
WR_WR = "wr_wr"
RB_WR = "rb_wr"

STRATEGIES: dict[str, str] = {
    BALANCED: "Balanced",
    RB_RB: "RB-RB start",
    WR_WR: "WR-WR start",
    RB_WR: "RB-WR start",
}

# What the first two rounds must produce, per strategy. Empty means no constraint.
_OPENINGS: dict[str, tuple[str, ...]] = {
    BALANCED: (),
    RB_RB: ("RB", "RB"),
    WR_WR: ("WR", "WR"),
    RB_WR: ("RB", "WR"),
}

# How many of a position a team will take before it stops. Keeps rosters plausible:
# nobody drafts four quarterbacks, and everybody takes exactly one kicker.
_EXTRA_DEPTH: dict[str, int] = {
    "QB": 1,
    "RB": 3,
    "WR": 3,
    "TE": 1,
    "K": 0,
    "DEF": 0,
}

# Spread of a player's drafted position around their board position. Widens deeper in
# the draft, where consensus genuinely breaks down.
def _spread(position: float) -> float:
    return min(max(0.22 * position, 4.0), 32.0)


# Your own ranking is followed more tightly than the market is - it is your opinion,
# so it should mostly win - but not so tightly that every run is identical.
_MY_SPREAD_FACTOR = 0.45


@dataclass
class MockPick:
    pick_no: int
    round: int
    slot: int
    sleeper_id: str
    name: str
    position: str
    pro_team: str | None
    points: float
    board_slot: int
    my_rank: int | None
    is_mine: bool


@dataclass
class MockTeam:
    slot: int
    is_mine: bool
    strategy: str
    picks: list[MockPick] = field(default_factory=list)


def _capacity(roster_positions: Sequence[str]) -> dict[str, int]:
    """Most of each position a team will roster."""
    dedicated, flex = slot_requirements(roster_positions)
    capacity: dict[str, int] = {}
    for position, count in dedicated.items():
        capacity[position] = count + _EXTRA_DEPTH.get(position, 2)
    for slot, count in flex.items():
        for position in SLOT_ELIGIBILITY.get(slot, frozenset()):
            capacity[position] = capacity.get(
                position, _EXTRA_DEPTH.get(position, 2)
            ) + count
    return capacity


def _opening_requirement(
    strategy: str, round_no: int, taken_positions: Counter
) -> set[str] | None:
    """Positions a strategy will accept this round. None means no constraint.

    A set rather than a single position, because RB-WR is order-free: in round one
    either half satisfies it, and round two owes whichever is left. Returning None for
    the ambiguous case would mean "anything goes", which let an RB-WR opening take a
    kicker and a tight end and satisfy nothing.
    """
    opening = _OPENINGS.get(strategy, ())
    if round_no > len(opening):
        return None

    needed = Counter(opening)
    for position in list(needed):
        needed[position] -= min(taken_positions.get(position, 0), needed[position])
    outstanding = {position for position, n in needed.items() if n > 0}
    return outstanding or None


def _choose(
    available: list[BoardPlayer],
    values: dict[str, float],
    roster: list[MockPick],
    roster_positions: Sequence[str],
    capacity: dict[str, int],
    picks_remaining: int,
    strategy: str,
    round_no: int,
) -> BoardPlayer | None:
    """The player this team takes, given who is left and what it still needs."""
    if not available:
        return None

    have = Counter(p.position for p in roster)

    # A roster that cannot field a legal lineup takes what it is missing. Without this
    # no simulated team ever drafts a kicker.
    missing = unfilled_starting_slots([p.position for p in roster], roster_positions)
    if missing and picks_remaining <= len(missing):
        forced = set(missing)
        pool = [p for p in available if p.position in forced]
        if pool:
            return min(pool, key=lambda p: values[p.sleeper_id])

    # Strategy constrains the opening rounds only.
    required = _opening_requirement(strategy, round_no, have)
    if required:
        pool = [p for p in available if p.position in required]
        if pool:
            return min(pool, key=lambda p: values[p.sleeper_id])

    # Otherwise take the best player the roster has room for.
    pool = [
        p
        for p in available
        if have.get(p.position, 0) < capacity.get(p.position, 2)
    ]
    if not pool:
        pool = available
    return min(pool, key=lambda p: values[p.sleeper_id])


def optimal_lineup(
    picks: Sequence[MockPick], roster_positions: Sequence[str]
) -> tuple[list[dict[str, Any]], float]:
    """Best legal starting lineup from a roster, and what it projects.

    Slots are filled most-constrained first so a flex never steals the only tight end.
    """
    starters = [s for s in roster_positions if s not in ("BN", "IR", "TAXI")]
    remaining = sorted(picks, key=lambda p: p.points, reverse=True)
    used: set[str] = set()
    lineup: list[dict[str, Any]] = []
    total = 0.0

    for slot in sorted(
        starters, key=lambda s: len(SLOT_ELIGIBILITY.get(s.upper(), {"x"}))
    ):
        eligible = SLOT_ELIGIBILITY.get(slot.upper(), frozenset({slot.upper()}))
        pick = next(
            (
                p
                for p in remaining
                if p.sleeper_id not in used and p.position in eligible
            ),
            None,
        )
        if pick is None:
            lineup.append({"slot": slot, "player": None})
            continue
        used.add(pick.sleeper_id)
        total += pick.points
        lineup.append(
            {
                "slot": slot,
                "player": {
                    "name": pick.name,
                    "position": pick.position,
                    "proTeam": pick.pro_team,
                    "points": round(pick.points, 1),
                },
            }
        )

    # Keep the lineup in the league's own slot order for display.
    order = {slot: i for i, slot in enumerate(starters)}
    lineup.sort(key=lambda row: order.get(row["slot"], 99))
    return lineup, round(total, 1)


def simulate(
    board: list[BoardPlayer],
    my_ranks: dict[str, int],
    roster_positions: Sequence[str],
    team_count: int,
    rounds: int,
    draft_type: str | None,
    my_slot: int,
    strategy: str = BALANCED,
    seed: int | None = None,
    rival_strategies: dict[int, str] | None = None,
) -> list[MockTeam]:
    """Run one mock draft and return every team's roster."""
    rng = random.Random(seed)

    # One draw per player per draft: this run's version of how the board falls.
    market_value = {
        p.sleeper_id: p.board_slot + rng.gauss(0.0, _spread(p.board_slot))
        for p in board
    }
    my_value = {}
    for p in board:
        anchor = my_ranks.get(p.sleeper_id, p.board_slot)
        my_value[p.sleeper_id] = anchor + rng.gauss(
            0.0, _spread(anchor) * _MY_SPREAD_FACTOR
        )

    rivals = rival_strategies or {}
    teams = {
        slot: MockTeam(
            slot=slot,
            is_mine=(slot == my_slot),
            strategy=strategy if slot == my_slot else rivals.get(slot, BALANCED),
        )
        for slot in range(1, team_count + 1)
    }

    capacity = _capacity(roster_positions)
    available = list(board)
    linear = (draft_type or "").lower() == "linear"

    pick_no = 0
    for rnd in range(1, rounds + 1):
        order = (
            range(1, team_count + 1)
            if linear or rnd % 2 == 1
            else range(team_count, 0, -1)
        )
        for slot in order:
            pick_no += 1
            team = teams[slot]
            values = my_value if team.is_mine else market_value
            chosen = _choose(
                available,
                values,
                team.picks,
                roster_positions,
                capacity,
                picks_remaining=rounds - rnd + 1,
                strategy=team.strategy,
                round_no=rnd,
            )
            if chosen is None:
                continue
            available.remove(chosen)
            team.picks.append(
                MockPick(
                    pick_no=pick_no,
                    round=rnd,
                    slot=slot,
                    sleeper_id=chosen.sleeper_id,
                    name=chosen.name,
                    position=chosen.position,
                    pro_team=chosen.pro_team,
                    points=chosen.points,
                    board_slot=chosen.board_slot,
                    my_rank=my_ranks.get(chosen.sleeper_id),
                    is_mine=team.is_mine,
                )
            )

    return [teams[slot] for slot in range(1, team_count + 1)]


def _team_payload(
    team: MockTeam, roster_positions: Sequence[str]
) -> dict[str, Any]:
    lineup, total = optimal_lineup(team.picks, roster_positions)
    return {
        "slot": team.slot,
        "isMine": team.is_mine,
        "strategy": team.strategy,
        "strategyLabel": STRATEGIES.get(team.strategy, team.strategy),
        "starterPoints": total,
        "lineup": lineup,
        "positionCounts": [
            {"position": position, "count": count}
            for position, count in Counter(
                p.position for p in team.picks
            ).most_common()
        ],
        "picks": [
            {
                "pickNo": p.pick_no,
                "round": p.round,
                "name": p.name,
                "position": p.position,
                "proTeam": p.pro_team,
                "points": round(p.points, 1),
                "boardSlot": p.board_slot,
                "myRank": p.my_rank,
            }
            for p in team.picks
        ],
    }


def _draft_shape(session: Session, league: League) -> tuple[int, int, str | None]:
    draft = session.execute(
        select(Draft).where(Draft.league_id == league.id)
    ).scalars().first()
    team_count = league.total_rosters or 12
    rounds = (draft.rounds if draft and draft.rounds else None) or len(
        [s for s in (league.roster_positions or []) if s != "IR"]
    ) or 15
    return team_count, rounds, (draft.draft_type if draft else None)


def run_mocks(
    session: Session,
    league_id: int,
    slots: Sequence[int] | None = None,
    strategies: Sequence[str] | None = None,
    runs_per_combo: int = 1,
    seed: int | None = None,
) -> dict[str, Any]:
    """Simulate one mock per (slot, strategy) pair and return the finished teams."""
    league = session.get(League, league_id)
    if league is None:
        raise ValueError(f"no league {league_id}")

    board = _board(session, league)
    my_ranks = personal_ranks(session, league_id)
    team_count, rounds, draft_type = _draft_shape(session, league)

    slot_list = list(slots) if slots else list(range(1, team_count + 1))
    slot_list = [max(1, min(s, team_count)) for s in slot_list]
    strategy_list = [s for s in (strategies or [BALANCED]) if s in STRATEGIES] or [
        BALANCED
    ]

    master = random.Random(seed)
    results: list[dict[str, Any]] = []

    for slot in slot_list:
        for strategy in strategy_list:
            for run in range(max(1, runs_per_combo)):
                run_seed = master.randrange(1 << 30)
                rng = random.Random(run_seed)
                # A varied field: rivals do not all draft the same way.
                rivals = {
                    s: rng.choice(list(STRATEGIES))
                    for s in range(1, team_count + 1)
                    if s != slot
                }
                teams = simulate(
                    board=board,
                    my_ranks=my_ranks,
                    roster_positions=league.roster_positions or [],
                    team_count=team_count,
                    rounds=rounds,
                    draft_type=draft_type,
                    my_slot=slot,
                    strategy=strategy,
                    seed=run_seed,
                    rival_strategies=rivals,
                )
                mine = next(t for t in teams if t.is_mine)
                payload = _team_payload(mine, league.roster_positions or [])
                payload.update({"run": run + 1, "seed": run_seed})
                results.append(payload)

    return {
        "league": {
            "id": league.id,
            "name": league.name,
            "teamCount": team_count,
            "rounds": rounds,
            "draftType": draft_type or "snake",
        },
        "usingMyRanks": bool(my_ranks),
        "strategies": [
            {"key": key, "label": label} for key, label in STRATEGIES.items()
        ],
        "results": results,
    }


# --- interactive mock ------------------------------------------------------


def _restore(board: list[BoardPlayer], taken_ids: Sequence[str]) -> list[BoardPlayer]:
    taken = set(taken_ids)
    return [p for p in board if p.sleeper_id not in taken]


def advance(
    session: Session,
    league_id: int,
    my_slot: int,
    seed: int,
    taken: Sequence[dict[str, Any]],
    my_pick_id: str | None = None,
    strategy: str = BALANCED,
) -> dict[str, Any]:
    """Play an interactive mock forward to the user's next turn.

    The client holds the pick list and sends it back, so the server keeps no session
    state. Re-deriving the field's intentions from the same seed makes the rivals
    behave consistently across calls instead of changing their minds each request.
    """
    league = session.get(League, league_id)
    if league is None:
        raise ValueError(f"no league {league_id}")

    board = _board(session, league)
    my_ranks = personal_ranks(session, league_id)
    team_count, rounds, draft_type = _draft_shape(session, league)
    my_slot = max(1, min(my_slot, team_count))

    rng = random.Random(seed)
    market_value = {
        p.sleeper_id: p.board_slot + rng.gauss(0.0, _spread(p.board_slot))
        for p in board
    }
    rivals = {
        s: rng.choice(list(STRATEGIES))
        for s in range(1, team_count + 1)
        if s != my_slot
    }

    picks: list[dict[str, Any]] = [dict(p) for p in taken]
    by_id = {p.sleeper_id: p for p in board}

    # Apply the user's selection first, if they made one.
    if my_pick_id:
        if my_pick_id not in by_id:
            raise ValueError(f"unknown player {my_pick_id}")
        if any(p["sleeperId"] == my_pick_id for p in picks):
            raise ValueError("that player is already drafted")
        player = by_id[my_pick_id]
        pick_no = len(picks) + 1
        picks.append(
            {
                "pickNo": pick_no,
                "round": ((pick_no - 1) // team_count) + 1,
                "slot": _slot_for_pick(pick_no, team_count, draft_type),
                "sleeperId": player.sleeper_id,
                "name": player.name,
                "position": player.position,
                "proTeam": player.pro_team,
                "points": round(player.points, 1),
                "boardSlot": player.board_slot,
                "myRank": my_ranks.get(player.sleeper_id),
                "isMine": True,
            }
        )

    capacity = _capacity(league.roster_positions or [])
    total_picks = team_count * rounds

    # Rebuild each roster so bot decisions account for what they already own.
    def roster_for(slot: int) -> list[MockPick]:
        return [
            MockPick(
                pick_no=p["pickNo"],
                round=p["round"],
                slot=p["slot"],
                sleeper_id=p["sleeperId"],
                name=p["name"],
                position=p["position"],
                pro_team=p.get("proTeam"),
                points=p.get("points", 0.0),
                board_slot=p.get("boardSlot", 0),
                my_rank=p.get("myRank"),
                is_mine=p.get("isMine", False),
            )
            for p in picks
            if p["slot"] == slot
        ]

    while len(picks) < total_picks:
        pick_no = len(picks) + 1
        slot = _slot_for_pick(pick_no, team_count, draft_type)
        if slot == my_slot:
            break  # the user is on the clock

        rnd = ((pick_no - 1) // team_count) + 1
        available = _restore(board, [p["sleeperId"] for p in picks])
        chosen = _choose(
            available,
            market_value,
            roster_for(slot),
            league.roster_positions or [],
            capacity,
            picks_remaining=rounds - rnd + 1,
            strategy=rivals.get(slot, BALANCED),
            round_no=rnd,
        )
        if chosen is None:
            break
        picks.append(
            {
                "pickNo": pick_no,
                "round": rnd,
                "slot": slot,
                "sleeperId": chosen.sleeper_id,
                "name": chosen.name,
                "position": chosen.position,
                "proTeam": chosen.pro_team,
                "points": round(chosen.points, 1),
                "boardSlot": chosen.board_slot,
                "myRank": my_ranks.get(chosen.sleeper_id),
                "isMine": False,
            }
        )

    complete = len(picks) >= total_picks
    my_picks = [p for p in picks if p["slot"] == my_slot]
    my_roster = roster_for(my_slot)
    lineup, total = optimal_lineup(my_roster, league.roster_positions or [])

    available = _restore(board, [p["sleeperId"] for p in picks])
    on_the_clock = None if complete else len(picks) + 1

    return {
        "league": {
            "id": league.id,
            "name": league.name,
            "teamCount": team_count,
            "rounds": rounds,
            "draftType": draft_type or "snake",
        },
        "mySlot": my_slot,
        "seed": seed,
        "strategy": strategy,
        "complete": complete,
        "onTheClock": on_the_clock,
        "round": (((on_the_clock or total_picks) - 1) // team_count) + 1,
        "picks": picks,
        "myPicks": my_picks,
        "myLineup": lineup,
        "myStarterPoints": total,
        "recentPicks": picks[-12:][::-1],
        "available": [
            {
                "sleeperId": p.sleeper_id,
                "name": p.name,
                "position": p.position,
                "proTeam": p.pro_team,
                "boardSlot": p.board_slot,
                "myRank": my_ranks.get(p.sleeper_id),
                "points": round(p.points, 1),
                "vorp": p.vorp,
            }
            for p in sorted(
                available,
                key=lambda x: my_ranks.get(x.sleeper_id, x.board_slot),
            )[:60]
        ],
        "myPickNumbers": pick_numbers(my_slot, team_count, rounds, draft_type),
    }


def _slot_for_pick(pick_no: int, team_count: int, draft_type: str | None) -> int:
    """Which slot owns an overall pick number."""
    rnd = ((pick_no - 1) // team_count) + 1
    index = ((pick_no - 1) % team_count) + 1
    if (draft_type or "").lower() == "linear" or rnd % 2 == 1:
        return index
    return team_count - index + 1
