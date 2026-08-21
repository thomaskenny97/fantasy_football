"""Tests for stochastic mock drafts.

The properties worth protecting are the ones that make a simulated roster believable:
every team ends able to field a legal lineup, strategies actually constrain the opening,
the user's own rankings drive the user's picks, and the same seed reproduces the same
draft.
"""

from __future__ import annotations

from collections import Counter

import pytest

from backend.analysis.adp_board import BoardPlayer
from backend.analysis.mock import (
    BALANCED,
    RB_RB,
    RB_WR,
    STRATEGIES,
    WR_WR,
    MockPick,
    optimal_lineup,
    simulate,
    _slot_for_pick,
)
from backend.scoring.rules import SLOT_ELIGIBILITY

SLOTS = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF"] + ["BN"] * 7
STARTERS = [s for s in SLOTS if s != "BN"]


def _board(counts: dict[str, int] | None = None) -> list[BoardPlayer]:
    """A synthetic board deep enough for a full 12-team draft."""
    counts = counts or {"QB": 30, "RB": 70, "WR": 90, "TE": 30, "K": 20, "DEF": 20}
    players: list[BoardPlayer] = []
    for position, n in counts.items():
        for i in range(n):
            players.append(
                BoardPlayer(
                    sleeper_id=f"{position}{i}",
                    name=f"{position} Player {i}",
                    position=position,
                    pro_team="KC",
                    board_rank=0.0,
                    board_slot=0,
                    points=300.0 - i * 2.0,
                    vorp=100.0 - i,
                    adp=None,
                )
            )
    # Interleave so the board is not sorted by position, then number it.
    players.sort(key=lambda p: (-p.points, p.position))
    for index, player in enumerate(players, start=1):
        player.board_slot = index
        player.board_rank = float(index)
    return players


def _run(strategy=BALANCED, my_slot=1, seed=7, my_ranks=None, rounds=16):
    return simulate(
        board=_board(),
        my_ranks=my_ranks or {},
        roster_positions=SLOTS,
        team_count=12,
        rounds=rounds,
        draft_type="snake",
        my_slot=my_slot,
        strategy=strategy,
        seed=seed,
    )


def _can_field_lineup(picks) -> bool:
    counts = Counter(p.position for p in picks)
    for slot in sorted(
        STARTERS, key=lambda s: len(SLOT_ELIGIBILITY.get(s.upper(), {"x"}))
    ):
        eligible = SLOT_ELIGIBILITY.get(slot.upper(), frozenset({slot.upper()}))
        taken = next((p for p in eligible if counts.get(p, 0) > 0), None)
        if taken is None:
            return False
        counts[taken] -= 1
    return True


# --- structural soundness --------------------------------------------------


def test_every_team_ends_with_a_legal_lineup():
    """The must-fill rule has to survive into the simulator.

    Without it no simulated team drafts a kicker and every roster is illegal.
    """
    teams = _run()
    assert len(teams) == 12
    for team in teams:
        assert _can_field_lineup(team.picks), (
            f"slot {team.slot} cannot field a lineup: "
            f"{Counter(p.position for p in team.picks)}"
        )


def test_no_player_is_drafted_twice():
    teams = _run()
    drafted = [p.sleeper_id for team in teams for p in team.picks]
    assert len(drafted) == len(set(drafted))


def test_every_team_drafts_the_full_number_of_rounds():
    teams = _run(rounds=16)
    assert all(len(team.picks) == 16 for team in teams)


def test_rosters_stay_plausible():
    """Nobody should end up with four quarterbacks or three kickers."""
    for team in _run():
        counts = Counter(p.position for p in team.picks)
        assert counts.get("QB", 0) <= 2
        assert counts.get("K", 0) <= 1
        assert counts.get("DEF", 0) <= 1


# --- strategies ------------------------------------------------------------


@pytest.mark.parametrize(
    "strategy,expected",
    [
        (RB_RB, {"RB": 2}),
        (WR_WR, {"WR": 2}),
        (RB_WR, {"RB": 1, "WR": 1}),
    ],
)
def test_strategy_constrains_the_opening_two_rounds(strategy, expected):
    teams = _run(strategy=strategy, my_slot=4)
    mine = next(t for t in teams if t.is_mine)
    opening = Counter(p.position for p in mine.picks[:2])
    for position, count in expected.items():
        assert opening.get(position, 0) == count


def test_balanced_is_not_forced_into_a_shape():
    """Balanced should follow value, so it need not match any fixed opening."""
    teams = _run(strategy=BALANCED, my_slot=4)
    mine = next(t for t in teams if t.is_mine)
    assert len(mine.picks) == 16


def test_every_named_strategy_runs():
    for strategy in STRATEGIES:
        teams = _run(strategy=strategy, my_slot=6)
        mine = next(t for t in teams if t.is_mine)
        assert _can_field_lineup(mine.picks)


# --- the user's own rankings ------------------------------------------------


def test_my_rankings_drive_my_picks():
    """A player the user ranks first should be taken far earlier than the market.

    The board is built so this player is a late-round afterthought by consensus.
    """
    board = _board()
    late = board[120]
    my_ranks = {late.sleeper_id: 1}

    teams = simulate(
        board=board,
        my_ranks=my_ranks,
        roster_positions=SLOTS,
        team_count=12,
        rounds=16,
        draft_type="snake",
        my_slot=1,
        strategy=BALANCED,
        seed=3,
    )
    mine = next(t for t in teams if t.is_mine)
    taken = [p.sleeper_id for p in mine.picks]
    assert late.sleeper_id in taken
    # Consensus had him around pick 121; the user should not have waited that long.
    assert mine.picks[taken.index(late.sleeper_id)].pick_no < 60


def test_rivals_ignore_my_rankings():
    """My opinion should move my board, not everyone else's."""
    board = _board()
    late = board[150]
    teams = simulate(
        board=board,
        my_ranks={late.sleeper_id: 1},
        roster_positions=SLOTS,
        team_count=12,
        rounds=3,
        draft_type="snake",
        my_slot=12,
        strategy=BALANCED,
        seed=5,
    )
    rival_picks = [p.sleeper_id for t in teams if not t.is_mine for p in t.picks]
    assert late.sleeper_id not in rival_picks


# --- determinism -----------------------------------------------------------


def test_the_same_seed_reproduces_the_same_draft():
    a = _run(seed=42)
    b = _run(seed=42)
    assert [[p.sleeper_id for p in t.picks] for t in a] == [
        [p.sleeper_id for p in t.picks] for t in b
    ]


def test_different_seeds_produce_different_drafts():
    """Randomness has to actually vary, or the whole feature is a static board."""
    a = _run(seed=1)
    b = _run(seed=2)
    assert [[p.sleeper_id for p in t.picks] for t in a] != [
        [p.sleeper_id for p in t.picks] for t in b
    ]


# --- lineup optimiser ------------------------------------------------------


def _pick(sleeper_id, position, points):
    return MockPick(
        pick_no=1,
        round=1,
        slot=1,
        sleeper_id=sleeper_id,
        name=sleeper_id,
        position=position,
        pro_team="KC",
        points=points,
        board_slot=1,
        my_rank=None,
        is_mine=True,
    )


def test_optimal_lineup_fills_constrained_slots_first():
    """A flex must not swallow the only tight end."""
    roster = [
        _pick("qb", "QB", 300),
        _pick("rb1", "RB", 280),
        _pick("rb2", "RB", 250),
        _pick("wr1", "WR", 270),
        _pick("wr2", "WR", 240),
        _pick("te", "TE", 260),
        _pick("rb3", "RB", 230),
        _pick("k", "K", 130),
        _pick("def", "DEF", 120),
    ]
    lineup, total = optimal_lineup(roster, SLOTS)
    filled = {row["slot"]: row["player"] for row in lineup}
    assert filled["TE"] is not None and filled["TE"]["name"] == "te"
    assert filled["FLEX"] is not None
    assert all(row["player"] is not None for row in lineup)
    assert total == pytest.approx(sum(p.points for p in roster), abs=0.1)


def test_optimal_lineup_reports_unfilled_slots():
    lineup, total = optimal_lineup([_pick("qb", "QB", 300)], SLOTS)
    unfilled = [row["slot"] for row in lineup if row["player"] is None]
    assert "K" in unfilled and "DEF" in unfilled
    assert total == 300.0


def test_lineup_is_returned_in_league_slot_order():
    roster = [
        _pick("qb", "QB", 300),
        _pick("rb1", "RB", 280),
        _pick("rb2", "RB", 250),
    ]
    lineup, _ = optimal_lineup(roster, SLOTS)
    assert [row["slot"] for row in lineup] == STARTERS


# --- pick ownership --------------------------------------------------------


def test_slot_for_pick_snakes():
    assert _slot_for_pick(1, 12, "snake") == 1
    assert _slot_for_pick(12, 12, "snake") == 12
    assert _slot_for_pick(13, 12, "snake") == 12  # the turn
    assert _slot_for_pick(24, 12, "snake") == 1


def test_slot_for_pick_linear():
    assert _slot_for_pick(1, 10, "linear") == 1
    assert _slot_for_pick(11, 10, "linear") == 1
    assert _slot_for_pick(15, 10, "linear") == 5
