"""Tests for the draft ADP board and availability heat map.

The pick-math cases are checked against ESPN's own published draft grid for a real
12-team league, not against a formula written from memory.
"""

from __future__ import annotations

import pytest

from backend.analysis.adp_board import (
    availability,
    board_rank_from,
    heat_band,
    pick_numbers,
    rank_type_for,
    target_band,
    target_score,
)


class FakeLeague:
    """Minimal stand-in; only the fields the board actually reads."""

    def __init__(self, roster_positions, scoring_settings=None):
        self.roster_positions = roster_positions
        self.scoring_settings = scoring_settings or {}


STANDARD = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF", "BN"]
SUPERFLEX = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "SUPER_FLEX", "BN"]


# --- pick math -------------------------------------------------------------


def test_snake_picks_match_espns_real_grid():
    """Verified against the live pre-draft grid for a 12-team ESPN league.

    Slot 12 drafts in pairs at the turn, which is the case most worth getting right.
    """
    assert pick_numbers(12, 12, 8, "snake") == [12, 13, 36, 37, 60, 61, 84, 85]


def test_snake_from_the_top_never_picks_back_to_back():
    assert pick_numbers(1, 12, 6, "snake") == [1, 24, 25, 48, 49, 72]


def test_snake_middle_slot():
    assert pick_numbers(6, 12, 4, "snake") == [6, 19, 30, 43]


def test_linear_repeats_the_same_slot_every_round():
    """Sleeper rookie drafts are linear, not snake."""
    assert pick_numbers(5, 10, 4, "linear") == [5, 15, 25, 35]


def test_team_count_changes_everything():
    """A 10-team and a 12-team league put different players at your pick."""
    assert pick_numbers(3, 10, 3, "snake") == [3, 18, 23]
    assert pick_numbers(3, 12, 3, "snake") == [3, 22, 27]


def test_slot_is_clamped_to_the_league_size():
    assert pick_numbers(99, 12, 1, "snake") == [12]
    assert pick_numbers(0, 12, 1, "snake") == [1]


def test_degenerate_inputs_return_nothing():
    assert pick_numbers(1, 0, 5, "snake") == []
    assert pick_numbers(1, 12, 0, "snake") == []


# --- availability model ----------------------------------------------------


@pytest.mark.parametrize(
    "rank,pick,low,high",
    [
        (5, 12, 0.00, 0.10),   # long gone
        (13, 12, 0.55, 0.65),  # probably there
        (24, 12, 0.95, 1.00),  # will be there
        (13, 13, 0.45, 0.55),  # true coin flip
    ],
)
def test_availability_reference_points(rank, pick, low, high):
    assert low <= availability(rank, pick) <= high


def test_availability_falls_as_the_draft_moves_past_a_player():
    """The same player grows less likely to survive as picks tick by.

    A rank-30 player is essentially certain to be there at pick 10 and essentially
    certain to be gone by pick 50.
    """
    assert availability(30, 10) > 0.95
    assert availability(30, 50) < 0.05
    assert availability(30, 10) > availability(30, 30) > availability(30, 50)


def test_availability_spread_widens_deeper_in_the_draft():
    """Round 1 is near-certain; late rounds are a lottery.

    A player 5 ranks past the current pick should be far more certain to have gone
    early than an equivalently-placed player late.
    """
    assert availability(5, 10) < availability(150, 155)


def test_availability_is_bounded():
    assert availability(1, 400) == pytest.approx(0.0, abs=1e-6)
    assert 0.0 <= availability(500, 1) <= 1.0
    assert availability(0, 10) == 0.0


def test_heat_bands():
    assert heat_band(0.95) == "likely"
    assert heat_band(0.60) == "probable"
    assert heat_band(0.45) == "coinflip"
    assert heat_band(0.20) == "unlikely"
    assert heat_band(0.05) == "gone"


# --- board selection -------------------------------------------------------


def test_superflex_league_uses_the_superflex_board():
    base, blend = rank_type_for(FakeLeague(SUPERFLEX, {"rec": 1.0}))
    assert base == "SUPERFLEX"


def test_ppr_league_blends_fully_to_ppr():
    base, blend = rank_type_for(FakeLeague(STANDARD, {"rec": 1.0}))
    assert base == "STANDARD"
    assert blend == 1.0


def test_half_ppr_lands_halfway():
    """Half-PPR is neither of ESPN's boards, so it interpolates between them."""
    _, blend = rank_type_for(FakeLeague(STANDARD, {"rec": 0.5}))
    assert blend == 0.5


def test_espn_scoring_is_keyed_by_statid():
    """ESPN leagues store rules as statIds; 53 is receptions."""
    _, blend = rank_type_for(FakeLeague(STANDARD, {"53": 1.0}))
    assert blend == 1.0


def test_board_rank_picks_the_superflex_rank():
    ranks = {
        "STANDARD": {"rank": 1},
        "PPR": {"rank": 1},
        "SUPERFLEX": {"rank": 7},
    }
    # Jahmyr Gibbs really is STANDARD 1 and SUPERFLEX 7 in the live data.
    assert board_rank_from(ranks, FakeLeague(SUPERFLEX)) == 7.0
    assert board_rank_from(ranks, FakeLeague(STANDARD, {"rec": 1.0})) == 1.0


def test_board_rank_interpolates_for_half_ppr():
    ranks = {"STANDARD": {"rank": 20}, "PPR": {"rank": 10}}
    assert board_rank_from(ranks, FakeLeague(STANDARD, {"rec": 0.5})) == 15.0
    assert board_rank_from(ranks, FakeLeague(STANDARD, {"rec": 1.0})) == 10.0
    assert board_rank_from(ranks, FakeLeague(STANDARD, {"rec": 0.0})) == 20.0


def test_board_rank_falls_back_to_whichever_rank_exists():
    assert board_rank_from({"PPR": {"rank": 4}}, FakeLeague(STANDARD)) == 4.0
    assert board_rank_from({"STANDARD": {"rank": 9}}, FakeLeague(STANDARD)) == 9.0


def test_board_rank_handles_missing_data():
    assert board_rank_from(None, FakeLeague(STANDARD)) is None
    assert board_rank_from({}, FakeLeague(STANDARD)) is None
    assert board_rank_from({"PPR": {}}, FakeLeague(STANDARD)) is None


# --- target scoring for the all-players list -------------------------------


def test_linear_slot_12_lights_up_every_twelfth_pick():
    """The intuition the feature was asked for, on a linear draft.

    At slot 12 of a 12-team linear draft you pick at 12, 24, 36, 48 - so players
    ranked there are exactly the ones you can get.
    """
    picks = pick_numbers(12, 12, 6, "linear")
    assert picks[:4] == [12, 24, 36, 48]
    for rank in (12, 24, 36, 48):
        score, best = target_score(rank, picks)
        assert score == pytest.approx(1.0, abs=1e-6)
        assert best == rank
        assert target_band(score) == "prime"


def test_snake_slot_12_creates_a_real_dead_zone():
    """The same seat on a snake behaves completely differently.

    Slot 12 snakes to 12, 13, 36, 37 - so a player ranked around 24 lines up with
    nothing: a reach at 13 and long gone by 36. That gap is real, and showing it is
    the point of the view.
    """
    picks = pick_numbers(12, 12, 6, "snake")
    assert picks[:4] == [12, 13, 36, 37]

    on_pick, _ = target_score(12, picks)
    dead, _ = target_score(24, picks)

    assert target_band(on_pick) == "prime"
    assert target_band(dead) == "dead"
    assert dead < on_pick


def test_target_score_peaks_exactly_on_a_pick():
    picks = [12, 13, 36, 37]
    peak, _ = target_score(36, picks)
    near, _ = target_score(33, picks)
    far, _ = target_score(25, picks)
    assert peak > near > far


def test_target_window_widens_later_in_the_draft():
    """Deep picks are less predictable, so the band around them is more forgiving."""
    early_off_by_five, _ = target_score(17, [12])
    late_off_by_five, _ = target_score(125, [120])
    assert late_off_by_five > early_off_by_five


def test_target_score_reports_which_pick_it_matched():
    picks = [12, 13, 36, 37, 60, 61]
    _, best = target_score(59, picks)
    assert best == 60


def test_target_score_handles_empty_inputs():
    assert target_score(10, []) == (0.0, None)
    assert target_score(0, [12]) == (0.0, None)


def test_target_bands_cover_the_range():
    assert target_band(0.95) == "prime"
    assert target_band(0.40) == "good"
    assert target_band(0.10) == "fringe"
    assert target_band(0.01) == "dead"
