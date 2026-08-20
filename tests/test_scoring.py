"""Tests for league-specific scoring.

The exact-value cases below were reconciled against Sleeper's own pts_ppr / pts_std /
pts_half_ppr for a completed week (2025 week 1), where the engine reproduced all 296
skill-position scores exactly. The ESPN bridge cases were reconciled the same way and
reproduced all 217 QB/RB/WR/TE scores exactly.
"""

from __future__ import annotations

from collections import Counter

from backend.scoring.rules import (
    STAT_BRIDGE,
    espn_to_sleeper_stats,
    score_espn_stats,
    score_stats,
    slot_accepts,
    unmapped_scoring_keys,
)

PPR = {
    "pass_yd": 0.04,
    "pass_td": 4,
    "pass_int": -1,
    "rush_yd": 0.1,
    "rush_td": 6,
    "rec": 1,
    "rec_yd": 0.1,
    "rec_td": 6,
    "fum_lost": -2,
}


# --- the bridge invariant --------------------------------------------------


def test_no_duplicate_espn_sources():
    """No ESPN stat may feed two Sleeper scoring keys.

    Sleeper defines ff / def_st_ff / st_ff as separate keys, and real leagues price
    all three at once. Mapping more than one of them to the same ESPN stat silently
    double-counts every forced fumble.
    """
    sources = Counter()
    for espn_names in STAT_BRIDGE.values():
        for name in espn_names:
            sources[name] += 1
    duplicates = {name: n for name, n in sources.items() if n > 1}
    assert not duplicates, f"ESPN stats feeding multiple keys: {duplicates}"


def test_fifty_plus_field_goals_are_not_double_counted():
    """ESPN nests madeFieldGoalsFrom60Plus inside madeFieldGoalsFrom50Plus.

    Regression: summing the two credited Chris Boswell with three long makes for two
    actual kicks in 2025 week 1, inflating him by 5 points.
    """
    espn_stats = {
        "madeFieldGoalsFrom50Plus": 2.0,
        "madeFieldGoalsFrom60Plus": 1.0,
        "madeExtraPoints": 4.0,
    }
    settings = {"fgm_50p": 5, "xpm": 1, "fgm_0_19": 3, "fgm_20_29": 3, "fgm_30_39": 3}
    # 2 long makes x 5, plus 4 extra points.
    assert score_espn_stats(espn_stats, settings) == 14.0


def test_flat_fgm_is_ignored_when_league_prices_by_distance():
    """A league pricing distance buckets must not also count the flat total."""
    espn_stats = {"madeFieldGoals": 2.0, "madeFieldGoalsFrom40To49": 2.0}
    settings = {"fgm": 3, "fgm_40_49": 4}
    assert score_espn_stats(espn_stats, settings) == 8.0


def test_flat_fgm_is_used_when_league_has_no_distance_buckets():
    espn_stats = {"madeFieldGoals": 2.0}
    assert score_espn_stats(espn_stats, {"fgm": 3}) == 6.0


# --- scoring in Sleeper's native vocabulary --------------------------------


def test_reproduces_a_real_sleeper_score():
    """Deebo Samuel, 2025 week 1. Sleeper reported pts_ppr = 22.6."""
    stats = {"rec": 7.0, "rec_yd": 77.0, "rush_yd": 19.0, "rush_td": 1.0}
    assert score_stats(stats, PPR) == 22.6


def test_ppr_standard_and_half_ppr_diverge_correctly():
    stats = {"rec": 7.0, "rec_yd": 77.0, "rush_yd": 19.0, "rush_td": 1.0}
    assert score_stats(stats, PPR) == 22.6
    assert score_stats(stats, {**PPR, "rec": 0}) == 15.6
    assert score_stats(stats, {**PPR, "rec": 0.5}) == 19.1


def test_unpriced_stats_score_nothing():
    """A league that does not price a category must ignore it entirely."""
    stats = {"rec": 7.0, "rec_tgt": 10.0}
    assert score_stats(stats, {"rec": 1}) == 7.0


def test_negative_scoring_applies():
    assert score_stats({"pass_int": 2.0}, PPR) == -2.0


# --- points allowed --------------------------------------------------------


def test_points_allowed_buckets():
    settings = {
        "pts_allow_0": 10,
        "pts_allow_1_6": 7,
        "pts_allow_7_13": 4,
        "pts_allow_14_20": 1,
        "pts_allow_35p": -4,
    }
    assert score_espn_stats({"defensivePointsAllowed": 0.0}, settings) == 10.0
    assert score_espn_stats({"defensivePointsAllowed": 6.0}, settings) == 7.0
    assert score_espn_stats({"defensivePointsAllowed": 12.0}, settings) == 4.0
    assert score_espn_stats({"defensivePointsAllowed": 18.0}, settings) == 1.0
    assert score_espn_stats({"defensivePointsAllowed": 41.0}, settings) == -4.0


def test_reproduces_a_real_defense_score():
    """Broncos D/ST, 2025 week 1. Sleeper reported pts_ppr = 16.0."""
    espn_stats = {
        "defensiveSacks": 6.0,
        "defensiveFumbles": 2.0,
        "defensiveForcedFumbles": 2.0,
        "defensivePointsAllowed": 12.0,
    }
    settings = {"sack": 1, "fum_rec": 2, "ff": 1, "pts_allow_7_13": 4}
    assert score_espn_stats(espn_stats, settings) == 16.0


# --- the ESPN bridge -------------------------------------------------------


def test_bridge_translates_espn_names_to_sleeper_keys():
    espn_stats = {
        "rushingYards": 82.3,
        "rushingTouchdowns": 1.0,
        "receivingReceptions": 4.0,
        "receivingYards": 30.0,
    }
    translated = espn_to_sleeper_stats(espn_stats)
    assert translated["rush_yd"] == 82.3
    assert translated["rush_td"] == 1.0
    assert translated["rec"] == 4.0
    assert translated["rec_yd"] == 30.0


def test_bridge_end_to_end_matches_native_scoring():
    """The same performance must score identically from either source."""
    espn_stats = {
        "receivingReceptions": 7.0,
        "receivingYards": 77.0,
        "rushingYards": 19.0,
        "rushingTouchdowns": 1.0,
    }
    native = {"rec": 7.0, "rec_yd": 77.0, "rush_yd": 19.0, "rush_td": 1.0}
    assert score_espn_stats(espn_stats, PPR) == score_stats(native, PPR) == 22.6


def test_unmapped_scoring_keys_are_reported():
    """Unsupported rules must be visible, not silently scored as zero."""
    settings = {"rec": 1, "some_exotic_idp_rule": 2.5}
    assert unmapped_scoring_keys(settings) == ["some_exotic_idp_rule"]


def test_unmapped_ignores_zero_rated_keys():
    """A rule priced at zero cannot affect results, so it is not worth flagging."""
    assert unmapped_scoring_keys({"rec": 1, "unused_rule": 0.0}) == []


# --- lineup slots ----------------------------------------------------------


def test_slot_eligibility():
    assert slot_accepts("QB", "QB")
    assert not slot_accepts("QB", "RB")
    assert slot_accepts("FLEX", "RB")
    assert slot_accepts("FLEX", "WR")
    assert slot_accepts("FLEX", "TE")
    assert not slot_accepts("FLEX", "QB")
    # Superflex is what makes a Sleeper league score so differently.
    assert slot_accepts("SUPER_FLEX", "QB")
    assert not slot_accepts("SUPER_FLEX", "K")
    assert not slot_accepts("FLEX", None)


# --- yards allowed ---------------------------------------------------------


def test_yards_allowed_buckets():
    settings = {
        "yds_allow_0_100": 5,
        "yds_allow_300_349": 1,
        "yds_allow_550p": -3,
    }
    assert score_espn_stats({"defensiveYardsAllowed": 90.0}, settings) == 5.0
    assert score_espn_stats({"defensiveYardsAllowed": 317.0}, settings) == 1.0
    assert score_espn_stats({"defensiveYardsAllowed": 600.0}, settings) == -3.0


def test_yards_allowed_ignored_when_league_does_not_price_it():
    assert score_espn_stats({"defensiveYardsAllowed": 317.0}, {"sack": 1}) == 0.0


def test_sleeper_only_keys_are_not_reported_as_unmapped():
    """st_ff and friends score fine from Sleeper stats; only ESPN lacks them.

    Reporting them as unsupported made every real league emit a misleading warning.
    """
    settings = {"st_ff": 1, "st_fum_rec": 1, "def_st_td": 6, "rec": 1}
    assert unmapped_scoring_keys(settings) == []


def test_sleeper_native_stats_score_special_teams_keys():
    """Regression: Sleeper's own pts_ppr includes st_ff / st_fum_rec.

    Kenny Gainwell and Ben Skowronek (2025 week 1) were each off by exactly 1.0
    until these were priced.
    """
    stats = {"rec": 3.0, "rush_yd": 19.0, "rec_yd": 4.0, "st_ff": 1.0}
    assert score_stats(stats, {**PPR, "st_ff": 1}) == 6.3
