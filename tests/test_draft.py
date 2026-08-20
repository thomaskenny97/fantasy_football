"""Tests for draft analysis.

The simulation cases here are the ones that caught real bugs: a pure value-over-
replacement board never drafts a kicker, and slot names that do not match position
names silently zero out a whole position's value.
"""

from __future__ import annotations

from collections import Counter

import pytest

from backend.analysis.draft import (
    PlayerProjection,
    positional_need,
    recommend,
    replacement_levels,
    slot_requirements,
    unfilled_starting_slots,
)

STANDARD = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF", "BN", "BN"]
SUPERFLEX = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "SUPER_FLEX", "BN"]
ESPN_SLOTS = ["QB", "RB", "RB", "WR", "WR", "TE", "D/ST", "K", "RB/WR/TE", "BN", "IR"]


def _pool(counts: dict[str, int], top: float = 300.0, step: float = 5.0):
    """A synthetic projection pool, descending by `step` within each position."""
    players = []
    for position, n in counts.items():
        for i in range(n):
            players.append(
                PlayerProjection(
                    sleeper_id=f"{position}{i}",
                    name=f"{position} Player {i}",
                    position=position,
                    pro_team="KC",
                    points=top - i * step,
                )
            )
    return players


# --- slot parsing ----------------------------------------------------------


def test_dedicated_slots_are_keyed_by_position_not_slot_name():
    """Regression: ESPN calls the defense slot "D/ST" but players are position "DEF".

    Keying on the slot name left defenses with zero startable spots, which collapsed
    their replacement level to the single best defense and gave every defense a value
    over replacement of exactly zero.
    """
    dedicated, flex = slot_requirements(ESPN_SLOTS)
    assert dedicated["DEF"] == 1
    assert "D/ST" not in dedicated
    assert dedicated["QB"] == 1
    assert dedicated["RB"] == 2
    assert flex["RB/WR/TE"] == 1


def test_bench_and_ir_slots_are_not_starting_slots():
    dedicated, flex = slot_requirements(["QB", "BN", "BN", "IR", "TAXI"])
    assert dedicated["QB"] == 1
    assert sum(dedicated.values()) == 1
    assert not flex


# --- replacement level -----------------------------------------------------


def test_superflex_pushes_quarterback_replacement_much_deeper():
    """The core reason scoring must be per-league.

    A single-QB league starts 12 quarterbacks; a superflex league starts closer to
    24, so the 13th-best quarterback is a starter in one and a backup in the other.
    """
    pool = _pool({"QB": 40, "RB": 60, "WR": 60, "TE": 30})
    by_pos: dict[str, list[float]] = {}
    for p in pool:
        by_pos.setdefault(p.position, []).append(p.points)
    for v in by_pos.values():
        v.sort(reverse=True)

    _, single = replacement_levels(STANDARD, 12, by_pos)
    _, superflex = replacement_levels(SUPERFLEX, 12, by_pos)

    assert single["QB"] == 12
    assert superflex["QB"] > single["QB"]


def test_flex_slots_deepen_the_positions_that_fill_them():
    pool = _pool({"QB": 30, "RB": 60, "WR": 60, "TE": 30})
    by_pos: dict[str, list[float]] = {}
    for p in pool:
        by_pos.setdefault(p.position, []).append(p.points)
    for v in by_pos.values():
        v.sort(reverse=True)

    _, with_flex = replacement_levels(
        ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX"], 10, by_pos
    )
    _, without = replacement_levels(["QB", "RB", "RB", "WR", "WR", "TE"], 10, by_pos)

    # The ten flex spots have to come from somewhere.
    assert sum(with_flex.values()) == sum(without.values()) + 10


# --- positional need -------------------------------------------------------


def test_need_is_urgent_when_a_starting_slot_is_empty():
    tiers = positional_need([], STANDARD)
    assert tiers["QB"] == "urgent"
    assert tiers["RB"] == "urgent"


def test_need_softens_once_starters_are_filled():
    tiers = positional_need(["QB", "RB", "RB", "WR", "WR", "TE", "K", "DEF"], STANDARD)
    assert tiers["QB"] == "depth"
    # RB can still fill the FLEX, so it is not done yet.
    assert tiers["RB"] == "needed"


def test_unfilled_starting_slots_lists_each_missing_spot():
    missing = unfilled_starting_slots(["QB", "RB"], STANDARD)
    assert Counter(missing) == Counter(["RB", "WR", "WR", "TE", "K", "DEF"])


# --- recommendations -------------------------------------------------------


def test_ranks_by_value_over_replacement_not_raw_points():
    """A quarterback with more points can still be worth less than a running back."""
    available = [
        PlayerProjection("qb1", "Big QB", "QB", "KC", 380.0),
        PlayerProjection("rb1", "Good RB", "RB", "SF", 330.0),
    ] + _pool({"QB": 30, "RB": 40, "WR": 40, "TE": 20}, top=300.0)

    recs, _ = recommend(available, STANDARD, 12, [], limit=2)
    # In a one-QB league the running back's replacement is far worse, so despite
    # scoring 50 fewer points he should not be buried.
    names = [r.player.name for r in recs]
    assert "Good RB" in names


def test_must_fill_narrows_the_board_when_picks_run_out():
    """Regression: without this the board never takes a kicker or a defense.

    A bench receiver always out-values the best kicker on pure VORP, so a draft run
    off the raw board ends with an illegal lineup. Verified by simulating a full
    12-team draft: every team finished unable to fill K and DEF.
    """
    available = _pool({"WR": 40, "K": 10, "DEF": 10})
    my_roster = ["QB", "RB", "RB", "WR", "WR", "TE"]  # missing K and DEF

    # Two picks left, two mandatory slots open: only K and DEF should be offered.
    recs, _ = recommend(
        available, STANDARD, 12, my_roster, limit=5, picks_remaining=2
    )
    assert {r.player.position for r in recs} == {"K", "DEF"}
    assert all(r.need_tier == "forced" for r in recs)


def test_must_fill_does_not_engage_while_picks_remain():
    available = _pool({"WR": 40, "K": 10, "DEF": 10})
    my_roster = ["QB", "RB", "RB", "WR", "WR", "TE"]
    recs, _ = recommend(
        available, STANDARD, 12, my_roster, limit=5, picks_remaining=9
    )
    # Plenty of picks left, so the best available player still wins.
    assert recs[0].player.position == "WR"


def test_recommendations_explain_themselves():
    available = _pool({"QB": 20, "RB": 40, "WR": 40, "TE": 20})
    recs, _ = recommend(available, STANDARD, 12, [], current_pick=1, limit=3)
    for rec in recs:
        assert rec.reasons, "every recommendation must carry its reasoning"
        assert any("over replacement" in reason for reason in rec.reasons)


def test_adp_value_nudges_but_does_not_dominate():
    """A player lasting past ADP gets a nudge, never enough to beat real value."""
    strong = PlayerProjection("a", "Strong", "RB", "SF", 320.0, adp=1.0)
    faller = PlayerProjection("b", "Faller", "RB", "KC", 250.0, adp=90.0)
    available = [strong, faller] + _pool({"RB": 40, "WR": 40, "QB": 20, "TE": 20},
                                         top=240.0)

    recs, _ = recommend(available, STANDARD, 12, [], current_pick=10, limit=2)
    assert recs[0].player.name == "Strong"


@pytest.mark.parametrize("team_count", [8, 10, 12, 14])
def test_a_simulated_draft_fills_every_legal_lineup(team_count):
    """End-to-end: run a whole draft off the board and check the results are legal.

    This is the test that caught the missing must-fill rule.
    """
    from backend.scoring.rules import SLOT_ELIGIBILITY

    starters = [s for s in STANDARD if s not in ("BN", "IR", "TAXI")]
    rounds = len(STANDARD)
    pool = _pool(
        {"QB": 40, "RB": 90, "WR": 110, "TE": 40, "K": 30, "DEF": 30}, step=2.0
    )
    remaining = {p.sleeper_id: p for p in pool}
    rosters: dict[int, list[PlayerProjection]] = {
        t: [] for t in range(1, team_count + 1)
    }

    pick_no = 1
    for rnd in range(1, rounds + 1):
        order = (
            range(1, team_count + 1) if rnd % 2 else range(team_count, 0, -1)
        )
        for team in order:
            available = list(remaining.values())
            recs, _ = recommend(
                available,
                STANDARD,
                team_count,
                [p.position for p in rosters[team]],
                current_pick=pick_no,
                limit=1,
                picks_remaining=rounds - rnd + 1,
            )
            if not recs:
                continue
            chosen = recs[0].player
            rosters[team].append(chosen)
            del remaining[chosen.sleeper_id]
            pick_no += 1

    for team, roster in rosters.items():
        counts = Counter(p.position for p in roster)
        filled = 0
        # Fill the most constrained slots first.
        for slot in sorted(
            starters, key=lambda s: len(SLOT_ELIGIBILITY.get(s.upper(), {"x"}))
        ):
            eligible = SLOT_ELIGIBILITY.get(slot.upper(), frozenset({slot.upper()}))
            taken = next((p for p in eligible if counts.get(p, 0) > 0), None)
            if taken:
                counts[taken] -= 1
                filled += 1
        assert filled == len(starters), (
            f"team {team} could not field a legal lineup: "
            f"{Counter(p.position for p in roster)}"
        )
