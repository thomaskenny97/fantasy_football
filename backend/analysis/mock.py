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
board to surprise you - or, when a study asks for it, drafts the projections instead,
so you can see which conclusions are your board's and which are the projections'.
"""

from __future__ import annotations

import logging
import random
import statistics
import time
from datetime import datetime, timezone
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.analysis.adp_board import (
    _board,
    BoardPlayer,
    detect_slot,
    personal_ranks,
    pick_numbers,
)
from backend.analysis.draft import slot_requirements, unfilled_starting_slots
from backend.db import Draft, League, SimStudy
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

# Spread of a player's drafted position around their board position, in picks. It
# widens deeper into the draft, where consensus genuinely breaks down.
#
# Tuned against measurement rather than taste. The first version used 0.22 with a
# 4-32 clamp, which let 18.6% of draftable players fall a full round or more - a
# third-rounder routinely lasting into the fourth, which made every simulated roster
# look better than a real one. At 0.07 that figure is 3.2%, with a median slide of
# three picks: rare enough to be a break, common enough to still be a draft.
_SPREAD_COEF = 0.07
_SPREAD_MIN = 2.0
_SPREAD_MAX = 10.0


def _spread(position: float) -> float:
    return min(max(_SPREAD_COEF * position, _SPREAD_MIN), _SPREAD_MAX)


# Your own ranking is followed more tightly than the market is - it is your opinion,
# so it should mostly win - but not so tightly that every run is identical.
_MY_SPREAD_FACTOR = 0.45


# What your simulated team drafts off. Rivals always draft the market board; this is
# only ever the basis for your own picks.
#
# The comparison is the point. "Does slot 4 beat slot 9" has a different answer for a
# board you actually believe in than for the projections everyone can see, and a
# strategy that pays off on your board may only pay off *because* of your board. Being
# able to flip between the two says which of the two is doing the work.
BASIS_MY_RANKS = "my_ranks"
BASIS_POINTS = "points"

BASES: dict[str, str] = {
    BASIS_MY_RANKS: "My rankings",
    BASIS_POINTS: "Projected points",
}


def value_ranks(
    board: Sequence[BoardPlayer], my_ranks: dict[str, int], basis: str
) -> dict[str, int]:
    """{sleeper_id: rank} your team drafts off, for the given basis.

    Points ordering is by value over replacement, not by raw points. Raw points ranks
    every startable quarterback above every running back - a board no one drafts and
    that would make the comparison a strawman - because it ignores that the twelfth
    quarterback is nearly as good as the first while the twelfth running back is not.
    VORP is the standard way to state a projection as a draft position, so it is what
    "projected points" means here.
    """
    if basis != BASIS_POINTS:
        return dict(my_ranks)

    # board_slot breaks ties so the ordering is stable run to run.
    ordered = sorted(board, key=lambda p: (-p.vorp, p.board_slot))
    return {p.sleeper_id: rank for rank, p in enumerate(ordered, start=1)}


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


def _pick_payload(pick: MockPick) -> dict[str, Any]:
    return {
        "sleeperId": pick.sleeper_id,
        "name": pick.name,
        "position": pick.position,
        "proTeam": pick.pro_team,
        "points": round(pick.points, 1),
        "round": pick.round,
        "pickNo": pick.pick_no,
        "boardSlot": pick.board_slot,
        "myRank": pick.my_rank,
    }


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
        lineup.append({"slot": slot, "player": _pick_payload(pick)})

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
    started = {
        row["player"]["sleeperId"] for row in lineup if row["player"] is not None
    }
    bench = [p for p in team.picks if p.sleeper_id not in started]

    return {
        "slot": team.slot,
        "isMine": team.is_mine,
        "strategy": team.strategy,
        "strategyLabel": STRATEGIES.get(team.strategy, team.strategy),
        "starterPoints": total,
        "lineup": lineup,
        # Everything the lineup could not fit, in the order it was drafted.
        "bench": [_pick_payload(p) for p in sorted(bench, key=lambda x: x.pick_no)],
        "positionCounts": [
            {"position": position, "count": count}
            for position, count in Counter(
                p.position for p in team.picks
            ).most_common()
        ],
        "picks": [_pick_payload(p) for p in team.picks],
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
    include_field: bool = True,
) -> dict[str, Any]:
    """Simulate one mock per (slot, strategy) pair and return the finished teams.

    Every run drafts all twelve rosters, not just the user's. With include_field the
    other eleven come back too, so a result can be inspected as the whole draft it
    actually was rather than one team lifted out of it. Comparing many slots at once
    turns that off, because the payload is then twelve times larger than the question
    being asked.
    """
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
                if include_field:
                    payload["field"] = [
                        _team_payload(t, league.roster_positions or [])
                        for t in teams
                        if not t.is_mine
                    ]
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


# --- studies ---------------------------------------------------------------

STUDY_SLOT = "slot"
STUDY_STRATEGY = "strategy"

# Ceilings chosen from measurement: one full 12-team draft costs about 27ms, so
# these keep the worst case around twenty seconds rather than minutes.
MAX_SLOT_RUNS = 60
MAX_STRATEGY_RUNS = 200


def _summarise(label: str, key: str, totals: list[float], **extra: Any) -> dict[str, Any]:
    """Mean plus its uncertainty.

    A bar chart of simulation means invites over-reading, so the spread travels with
    the number: with enough runs the standard error is what says whether two bars
    actually differ.
    """
    mean = statistics.mean(totals)
    stdev = statistics.pstdev(totals) if len(totals) > 1 else 0.0
    return {
        "label": label,
        "key": key,
        "mean": round(mean, 1),
        "stdev": round(stdev, 1),
        "stderr": round(stdev / (len(totals) ** 0.5), 1) if totals else 0.0,
        "min": round(min(totals), 1),
        "max": round(max(totals), 1),
        "n": len(totals),
        **extra,
    }


def _study_context(session: Session, league_id: int):
    league = session.get(League, league_id)
    if league is None:
        raise ValueError(f"no league {league_id}")
    board = _board(session, league)
    my_ranks = personal_ranks(session, league_id)
    team_count, rounds, draft_type = _draft_shape(session, league)
    return league, board, my_ranks, team_count, rounds, draft_type


def _one_total(
    board, my_ranks, league, team_count, rounds, draft_type, slot, strategy, seed
) -> float:
    teams = simulate(
        board=board,
        my_ranks=my_ranks,
        roster_positions=league.roster_positions or [],
        team_count=team_count,
        rounds=rounds,
        draft_type=draft_type,
        my_slot=slot,
        strategy=strategy,
        seed=seed,
        rival_strategies={
            s: random.Random(seed * 97 + s).choice(list(STRATEGIES))
            for s in range(1, team_count + 1)
            if s != slot
        },
    )
    mine = next(t for t in teams if t.is_mine)
    _, total = optimal_lineup(mine.picks, league.roster_positions or [])
    return total


def study_slots(
    session: Session,
    league_id: int,
    runs: int = 20,
    strategy: str = BALANCED,
    seed: int | None = None,
    basis: str = BASIS_MY_RANKS,
) -> dict[str, Any]:
    """Average starting-lineup value from every draft slot.

    Strategy is held constant across slots so the only thing varying is the seat.
    """
    league, board, my_ranks, team_count, rounds, draft_type = _study_context(
        session, league_id
    )
    basis = basis if basis in BASES else BASIS_MY_RANKS
    ranks = value_ranks(board, my_ranks, basis)
    runs = max(1, min(runs, MAX_SLOT_RUNS))
    master = random.Random(seed)
    started = time.time()

    rows: list[dict[str, Any]] = []
    for slot in range(1, team_count + 1):
        totals = [
            _one_total(
                board, ranks, league, team_count, rounds, draft_type,
                slot, strategy, master.randrange(1 << 30),
            )
            for _ in range(runs)
        ]
        rows.append(_summarise(f"Slot {slot}", str(slot), totals, slot=slot))

    return {
        "kind": STUDY_SLOT,
        "runs": runs,
        "drafts": runs * team_count,
        "strategy": strategy,
        "strategyLabel": STRATEGIES.get(strategy, strategy),
        "basis": basis,
        "basisLabel": BASES[basis],
        # False means a "my rankings" run had nothing of yours to use and fell back to
        # the market board, which the chart should say rather than quietly imply.
        "hasMyRanks": bool(my_ranks),
        "teamCount": team_count,
        "elapsed": round(time.time() - started, 1),
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "results": rows,
    }


def study_strategies(
    session: Session,
    league_id: int,
    runs: int = 50,
    slot: int | None = None,
    seed: int | None = None,
    basis: str = BASIS_MY_RANKS,
) -> dict[str, Any]:
    """Average starting-lineup value for each opening strategy.

    Slot is held constant - strategy value depends on where you sit, so mixing seats
    would average the question away.
    """
    league, board, my_ranks, team_count, rounds, draft_type = _study_context(
        session, league_id
    )
    basis = basis if basis in BASES else BASIS_MY_RANKS
    ranks = value_ranks(board, my_ranks, basis)
    runs = max(1, min(runs, MAX_STRATEGY_RUNS))
    active_slot = slot or detect_slot(session, league) or 1
    active_slot = max(1, min(active_slot, team_count))

    master = random.Random(seed)
    started = time.time()

    rows: list[dict[str, Any]] = []
    for strategy, label in STRATEGIES.items():
        totals = [
            _one_total(
                board, ranks, league, team_count, rounds, draft_type,
                active_slot, strategy, master.randrange(1 << 30),
            )
            for _ in range(runs)
        ]
        rows.append(_summarise(label, strategy, totals))

    return {
        "kind": STUDY_STRATEGY,
        "runs": runs,
        "drafts": runs * len(STRATEGIES),
        "slot": active_slot,
        "basis": basis,
        "basisLabel": BASES[basis],
        "hasMyRanks": bool(my_ranks),
        "teamCount": team_count,
        "elapsed": round(time.time() - started, 1),
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "results": rows,
    }


def _study_key(kind: str, basis: str) -> str:
    """Cache key for a study.

    The basis is part of the question, not a display setting, so each one keeps its own
    remembered answer: flipping the toggle shows the last run *of that basis* instead
    of a chart built from the other one. Encoded into the existing kind column rather
    than added as a column, because the cache is keyed on (league, kind) by a unique
    constraint SQLite cannot alter in place.
    """
    return kind if basis == BASIS_MY_RANKS else f"{kind}:{basis}"


def save_study(session: Session, league_id: int, payload: dict[str, Any]) -> None:
    """Keep the latest study so a page load does not have to re-run it."""
    kind = _study_key(payload["kind"], payload.get("basis", BASIS_MY_RANKS))
    row = session.execute(
        select(SimStudy).where(
            SimStudy.league_id == league_id, SimStudy.kind == kind
        )
    ).scalar_one_or_none()
    if row is None:
        row = SimStudy(league_id=league_id, kind=kind)
        session.add(row)
    row.runs = payload.get("runs", 0)
    row.payload = payload
    row.created_at = datetime.now(timezone.utc).replace(tzinfo=None)
    session.commit()


def load_study(
    session: Session, league_id: int, kind: str, basis: str = BASIS_MY_RANKS
) -> dict[str, Any] | None:
    row = session.execute(
        select(SimStudy).where(
            SimStudy.league_id == league_id,
            SimStudy.kind == _study_key(kind, basis),
        )
    ).scalar_one_or_none()
    return row.payload if row else None


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
