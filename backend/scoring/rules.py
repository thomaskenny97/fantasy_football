"""League-specific scoring.

The core idea of this app: pull raw stat projections once, then apply each league's
own scoring rules to them. That is what makes a superflex PPR Sleeper league and a
standard ESPN league directly comparable, instead of comparing two vendors'
incompatible point totals.

Sleeper's scoring_settings vocabulary is used as the canonical rule language because
it is explicit, granular, and exposed verbatim by the API. ESPN's raw stat lines are
translated into that vocabulary by STAT_BRIDGE below.

Fidelity note: ESPN reports made field goals in three buckets (under 40, 40-49, 50+)
while Sleeper prices four (0-19, 20-29, 30-39, 40-49, 50+). Sub-40 kicks therefore
cannot be split exactly - see _score_sub_40_field_goals for how that is handled and
what it costs.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping

log = logging.getLogger(__name__)

# Sleeper scoring key -> the ESPN stat name(s) that feed it. Multiple names are summed.
STAT_BRIDGE: dict[str, tuple[str, ...]] = {
    # Passing
    "pass_yd": ("passingYards",),
    "pass_td": ("passingTouchdowns",),
    "pass_int": ("passingInterceptions",),
    "pass_2pt": ("passing2PtConversions",),
    "pass_att": ("passingAttempts",),
    "pass_cmp": ("passingCompletions",),
    "pass_inc": ("passingIncompletions",),
    "pass_sack": ("passingTimesSacked",),
    # Rushing
    "rush_yd": ("rushingYards",),
    "rush_td": ("rushingTouchdowns",),
    "rush_2pt": ("rushing2PtConversions",),
    "rush_att": ("rushingAttempts",),
    # Receiving
    "rec": ("receivingReceptions",),
    "rec_yd": ("receivingYards",),
    "rec_td": ("receivingTouchdowns",),
    "rec_2pt": ("receiving2PtConversions",),
    "rec_tgt": ("receivingTargets",),
    # Fumbles
    "fum": ("fumbles",),
    "fum_lost": ("lostFumbles",),
    "fum_rec_td": ("fumbleRecoveredForTD",),
    # Kicking
    "xpm": ("madeExtraPoints",),
    "xpmiss": ("missedExtraPoints",),
    "fgm": ("madeFieldGoals",),
    "fgmiss": ("missedFieldGoals",),
    "fgm_40_49": ("madeFieldGoalsFrom40To49",),
    # fgm_50p / fgm_50_59 / fgm_60p are absent by design - ESPN nests its 60+ bucket
    # inside its 50+ bucket, so they are derived in _score_long_field_goals.
    # Team defense / special teams
    "def_td": ("defensiveTouchdowns",),
    "sack": ("defensiveSacks",),
    "int": ("defensiveInterceptions",),
    "ff": ("defensiveForcedFumbles",),
    "fum_rec": ("defensiveFumbles",),
    "safe": ("defensiveSafeties",),
    "blk_kick": ("defensiveBlockedKicks",),
    "st_td": ("kickoffReturnTouchdowns", "puntReturnTouchdowns"),
    # def_st_td, def_st_ff and def_st_fum_rec are deliberately absent. Sleeper defines
    # them as separate keys, but they describe the same ESPN stats already claimed by
    # def_td, ff and fum_rec. Real leagues price both sets (the sampled league defines
    # ff, def_st_ff and st_ff simultaneously), so mapping both would double-count.
    # The invariant is enforced by test_no_duplicate_espn_sources.
    # Long-touchdown bonuses. ESPN's 50+ variants nest inside the 40+ variants the
    # same way Sleeper's do, so these map across directly.
    "pass_td_40p": ("passing40PlusYardTD",),
    "pass_td_50p": ("passing50PlusYardTD",),
    "rush_td_40p": ("rushing40PlusYardTD",),
    "rush_td_50p": ("rushing50PlusYardTD",),
    "rec_td_40p": ("receiving40PlusYardTD",),
    "rec_td_50p": ("receiving50PlusYardTD",),
    "def_2pt": ("defensive2PtReturns",),
    # Yardage bonuses
    "bonus_pass_yd_300": ("passing300To399YardGame",),
    "bonus_pass_yd_400": ("passing400PlusYardGame",),
    "bonus_rush_yd_100": ("rushing100To199YardGame",),
    "bonus_rush_yd_200": ("rushing200PlusYardGame",),
    "bonus_rec_yd_100": ("receiving100To199YardGame",),
    "bonus_rec_yd_200": ("receiving200PlusYardGame",),
}

# Sleeper points-allowed buckets, as (scoring_key, low, high) with high inclusive.
POINTS_ALLOWED_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("pts_allow_0", 0, 0),
    ("pts_allow_1_6", 1, 6),
    ("pts_allow_7_13", 7, 13),
    ("pts_allow_14_20", 14, 20),
    ("pts_allow_21_27", 21, 27),
    ("pts_allow_28_34", 28, 34),
    ("pts_allow_35p", 35, float("inf")),
)

# Sleeper yards-allowed buckets, as (scoring_key, low, high) with high inclusive.
# ESPN reports defensiveYardsAllowed as a raw number, so the bucket is derived.
YARDS_ALLOWED_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("yds_allow_0_100", 0, 99),
    ("yds_allow_100_199", 100, 199),
    ("yds_allow_200_299", 200, 299),
    ("yds_allow_300_349", 300, 349),
    ("yds_allow_350_399", 350, 399),
    ("yds_allow_400_449", 400, 449),
    ("yds_allow_450_499", 450, 499),
    ("yds_allow_500_549", 500, 549),
    ("yds_allow_550p", 550, float("inf")),
)

# Sleeper's sub-40 field goal buckets, which ESPN reports as a single number.
_SUB_40_KEYS = ("fgm_0_19", "fgm_20_29", "fgm_30_39")

# Distance-bucketed field goal keys. A league that prices any of these is pricing by
# distance, so the flat `fgm` total must be ignored to avoid counting kicks twice.
_FG_BUCKET_KEYS = _SUB_40_KEYS + ("fgm_40_49", "fgm_50p", "fgm_50_59", "fgm_60p")

# Long field goal keys, derived rather than mapped. Sleeper leagues price these two
# ways: a single inclusive fgm_50p, or a disjoint fgm_50_59 plus fgm_60p.
_LONG_FG_KEYS = ("fgm_50p", "fgm_50_59", "fgm_60p")


def espn_to_sleeper_stats(named_stats: Mapping[str, float]) -> dict[str, float]:
    """Translate ESPN named stats into Sleeper's scoring vocabulary.

    Input comes from resolve.stat_map.named_stats. Unmapped categories are dropped;
    they are overwhelmingly punting and IDP detail that no standard league prices.
    """
    out: dict[str, float] = {}
    for sleeper_key, espn_names in STAT_BRIDGE.items():
        total = 0.0
        found = False
        for name in espn_names:
            value = named_stats.get(name)
            if value is not None:
                total += float(value)
                found = True
        if found:
            out[sleeper_key] = total

    # Points allowed is a raw number in ESPN and a bucket in Sleeper.
    points_allowed = named_stats.get("defensivePointsAllowed")
    if points_allowed is not None:
        out["_pts_allow"] = float(points_allowed)

    yards_allowed = named_stats.get("defensiveYardsAllowed")
    if yards_allowed is not None:
        out["_yds_allow"] = float(yards_allowed)

    # Preserve the un-split sub-40 make count for _score_sub_40_field_goals.
    sub_40 = named_stats.get("madeFieldGoalsFromUnder40")
    if sub_40 is not None:
        out["_fgm_under_40"] = float(sub_40)

    # Long makes are carried raw; ESPN's 50+ count includes its 60+ count.
    fifty_plus = named_stats.get("madeFieldGoalsFrom50Plus")
    if fifty_plus is not None:
        out["_fgm_50_plus"] = float(fifty_plus)
    sixty_plus = named_stats.get("madeFieldGoalsFrom60Plus")
    if sixty_plus is not None:
        out["_fgm_60_plus"] = float(sixty_plus)

    return out


def _score_sub_40_field_goals(
    stats: Mapping[str, float], settings: Mapping[str, float]
) -> float:
    """Price sub-40 field goals when the source cannot split them by distance.

    ESPN reports one madeFieldGoalsFromUnder40 count; Sleeper prices 0-19, 20-29, and
    30-39 separately. Without kick-level distances the split is unknowable, so the
    average of whichever sub-40 rates the league defines is used.

    In practice the error is small: leagues almost always price all sub-40 kicks
    identically (3.0 in the league sampled), which makes the average exact. It only
    drifts for leagues that vary rates by distance, and then by well under a point.
    """
    made = stats.get("_fgm_under_40")
    if not made:
        return 0.0

    rates = [float(settings[key]) for key in _SUB_40_KEYS if key in settings]
    if not rates:
        # League prices only a flat fgm; that is handled by the main loop.
        return 0.0

    if len(set(rates)) > 1:
        log.debug(
            "League prices sub-40 field goals unevenly (%s); using their average",
            rates,
        )
    return made * (sum(rates) / len(rates))


def _score_long_field_goals(
    stats: Mapping[str, float], settings: Mapping[str, float]
) -> float:
    """Price 50+ yard field goals, respecting how the league splits them.

    ESPN reports madeFieldGoalsFrom50Plus inclusive of madeFieldGoalsFrom60Plus, while
    Sleeper leagues price long kicks either as one inclusive fgm_50p bucket or as a
    disjoint fgm_50_59 plus fgm_60p pair. Both of the user's leagues use the disjoint
    form, so 50-59 is derived by subtraction.
    """
    fifty_plus = stats.get("_fgm_50_plus", 0.0)
    sixty_plus = stats.get("_fgm_60_plus", 0.0)
    if not fifty_plus and not sixty_plus:
        return 0.0

    total = 0.0
    prices_split = "fgm_50_59" in settings or "fgm_60p" in settings

    if prices_split:
        # Guard against a malformed payload reporting more 60+ makes than 50+ makes.
        fifty_to_fifty_nine = max(fifty_plus - sixty_plus, 0.0)
        total += fifty_to_fifty_nine * float(settings.get("fgm_50_59", 0.0))
        total += sixty_plus * float(settings.get("fgm_60p", 0.0))
    else:
        total += fifty_plus * float(settings.get("fgm_50p", 0.0))
    return total


def _score_points_allowed(
    stats: Mapping[str, float], settings: Mapping[str, float]
) -> float:
    """Convert a raw points-allowed figure into its Sleeper bucket value."""
    if "_pts_allow" not in stats:
        return 0.0
    allowed = stats["_pts_allow"]
    for key, low, high in POINTS_ALLOWED_BUCKETS:
        if low <= allowed <= high:
            return float(settings.get(key, 0.0))
    return 0.0


def _score_yards_allowed(
    stats: Mapping[str, float], settings: Mapping[str, float]
) -> float:
    """Convert a raw yards-allowed figure into its Sleeper bucket value."""
    if "_yds_allow" not in stats:
        return 0.0
    allowed = stats["_yds_allow"]
    for key, low, high in YARDS_ALLOWED_BUCKETS:
        if low <= allowed <= high:
            return float(settings.get(key, 0.0))
    return 0.0


def score_stats(
    stats: Mapping[str, float], scoring_settings: Mapping[str, float]
) -> float:
    """Apply a league's scoring settings to a stat line in Sleeper's vocabulary.

    Args:
        stats: stat line keyed by Sleeper scoring keys, from espn_to_sleeper_stats
            or directly from Sleeper.
        scoring_settings: the league's scoring_settings dict.

    Returns:
        Fantasy points under that league's rules.
    """
    prices_fg_by_distance = any(
        key in scoring_settings for key in _FG_BUCKET_KEYS
    )

    total = 0.0
    for key, value in stats.items():
        if key.startswith("_"):
            continue  # internal carriers, handled separately below
        if key in _SUB_40_KEYS or key in _LONG_FG_KEYS:
            continue  # handled by the dedicated field goal scorers
        if key == "fgm" and prices_fg_by_distance:
            # League prices kicks by distance; the flat total would double-count.
            continue
        rate = scoring_settings.get(key)
        if rate is None:
            continue
        total += float(value) * float(rate)

    total += _score_sub_40_field_goals(stats, scoring_settings)
    total += _score_long_field_goals(stats, scoring_settings)
    total += _score_points_allowed(stats, scoring_settings)
    total += _score_yards_allowed(stats, scoring_settings)
    return round(total, 2)


def score_espn_stats(
    named_stats: Mapping[str, float], scoring_settings: Mapping[str, float]
) -> float:
    """Score an ESPN stat line under a league's rules, in one step."""
    return score_stats(espn_to_sleeper_stats(named_stats), scoring_settings)


# Scoring keys that Sleeper reports natively but ESPN's stat lines cannot express.
# These score correctly from Sleeper data and simply contribute nothing to a
# projection built from ESPN stats.
SLEEPER_ONLY_KEYS = frozenset(
    {
        "st_ff",
        "st_fum_rec",
        "st_td",
        "def_st_ff",
        "def_st_fum_rec",
        "def_st_td",
        "idp_ff",
        "idp_fum_rec",
        "idp_int",
        "idp_sack",
        "idp_tkl",
        "idp_tkl_ast",
        "idp_tkl_solo",
    }
)


def score_espn_raw(
    raw_stats: Mapping[str, float], scoring: Mapping[str, float]
) -> float:
    """Score an ESPN stat line using ESPN's own statId vocabulary.

    ESPN leagues are scored this way rather than by translating their rules into
    Sleeper's keys, because the two vocabularies genuinely disagree. ESPN buckets
    points allowed as 14-17 / 18-21 / 22-27 where Sleeper uses 14-20 / 21-27, so any
    translation loses fidelity. Worse, ESPN prices statIds that are absent from the
    vendored name map (198 and 209 among them), which a name-based translation drops
    silently.

    Multiplying the raw stat line by the league's own per-statId rates reproduces
    ESPN's arithmetic exactly, and needs no name mapping at all. Verified against
    ESPN's own appliedTotal.

    Args:
        raw_stats: {statId: value}, straight from an ESPN stats entry.
        scoring: {statId: points}, from the league's scoringItems.
    """
    total = 0.0
    for stat_id, value in raw_stats.items():
        rate = scoring.get(str(stat_id))
        if rate:
            total += float(value) * float(rate)
    return round(total, 2)


def unmapped_scoring_keys(scoring_settings: Mapping[str, float]) -> list[str]:
    """League scoring rules that cannot be satisfied from ESPN stat lines.

    Surfaced during sync so an unsupported rule is visible rather than silently
    scoring zero. Two exclusions keep the signal meaningful:

      - Keys rated 0 are ignored; they cannot affect a result.
      - SLEEPER_ONLY_KEYS are ignored; they score correctly from Sleeper's own stats
        and only go missing from ESPN-sourced projections.
    """
    known = set(STAT_BRIDGE) | set(_SUB_40_KEYS) | set(_LONG_FG_KEYS)
    known |= set(SLEEPER_ONLY_KEYS)
    known |= {key for key, _, _ in POINTS_ALLOWED_BUCKETS}
    known |= {key for key, _, _ in YARDS_ALLOWED_BUCKETS}
    return sorted(
        key
        for key, rate in scoring_settings.items()
        if key not in known and float(rate) != 0.0
    )


def lineup_slots(roster_positions: Iterable[str]) -> list[str]:
    """Startable slots from a league's roster_positions, bench and IR removed."""
    ignored = {"BN", "IR", "TAXI"}
    return [slot for slot in roster_positions if slot not in ignored]


# Which real positions may fill each lineup slot.
SLOT_ELIGIBILITY: dict[str, frozenset[str]] = {
    "QB": frozenset({"QB"}),
    "RB": frozenset({"RB"}),
    "WR": frozenset({"WR"}),
    "TE": frozenset({"TE"}),
    "K": frozenset({"K"}),
    "DEF": frozenset({"DEF"}),
    "DST": frozenset({"DEF"}),
    # ESPN spells the slot "D/ST"; without this it looks like an unknown slot and
    # defenses end up with no startable spot, so their replacement level collapses.
    "D/ST": frozenset({"DEF"}),
    "FLEX": frozenset({"RB", "WR", "TE"}),
    # ESPN spells its flex slots out longhand.
    "RB/WR/TE": frozenset({"RB", "WR", "TE"}),
    "RB/WR": frozenset({"RB", "WR"}),
    "WR/TE": frozenset({"WR", "TE"}),
    "OP": frozenset({"QB", "RB", "WR", "TE"}),
    "WRRB_FLEX": frozenset({"RB", "WR"}),
    "REC_FLEX": frozenset({"WR", "TE"}),
    "SUPER_FLEX": frozenset({"QB", "RB", "WR", "TE"}),
    "IDP_FLEX": frozenset({"DL", "LB", "DB"}),
}


def slot_accepts(slot: str, position: str | None) -> bool:
    """Whether a player of `position` may start in lineup slot `slot`."""
    if not position:
        return False
    eligible = SLOT_ELIGIBILITY.get(slot.upper())
    if eligible is None:
        # Unknown slot: fall back to an exact position match.
        return slot.upper() == position.upper()
    return position.upper() in eligible
