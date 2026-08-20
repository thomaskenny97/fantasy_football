"""Draft analysis: who to take next, and why.

Ranking by raw projected points is the naive answer and it is wrong: in a league that
starts one quarterback, the QB1 overall can be worth less than the RB12, because the
quarterback you would otherwise stream is nearly as good while the running back's
alternative is not. So players are ranked by **value over replacement** - projected
points minus what the best freely available player at that position would score.

Replacement level depends on the league. It is derived from that league's own roster
slots and team count, with FLEX slots filled greedily from the actual projection pool
rather than by a hardcoded positional split, so a superflex league correctly values
quarterbacks far higher than a single-QB league does.

Every recommendation carries the reasons behind it, because a draft assistant that
cannot explain itself is one you should not trust while on the clock.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from backend.scoring.rules import SLOT_ELIGIBILITY

log = logging.getLogger(__name__)

BENCH_SLOTS = {"BN", "IR", "TAXI"}

# How strongly an unmet starting requirement pulls a player up the board. Kept mild:
# value over replacement should dominate, with need breaking near-ties rather than
# overriding a clearly better player.
NEED_MULTIPLIER = {
    "forced": 1.0,  # value ordering within a forced set is still by VORP
    "urgent": 1.25,
    "needed": 1.10,
    "depth": 1.0,
    "surplus": 0.88,
}


@dataclass
class PlayerProjection:
    """One player's projection within a single league's scoring."""

    sleeper_id: str
    name: str
    position: str
    pro_team: str | None
    points: float
    adp: float | None = None
    auction_value: float | None = None


@dataclass
class Recommendation:
    player: PlayerProjection
    vorp: float
    score: float
    need_tier: str
    adp_delta: float | None
    reasons: list[str] = field(default_factory=list)


def slot_requirements(
    roster_positions: Sequence[str],
) -> tuple[Counter, Counter]:
    """Split a league's lineup into dedicated slots and multi-position flex slots."""
    dedicated: Counter = Counter()
    flex: Counter = Counter()
    for slot in roster_positions or []:
        if slot in BENCH_SLOTS:
            continue
        eligible = SLOT_ELIGIBILITY.get(slot.upper())
        if eligible and len(eligible) > 1:
            flex[slot.upper()] += 1
        elif eligible:
            # Count against the POSITION the slot accepts, not the slot's own name.
            # ESPN calls its defense slot "D/ST" while players are position "DEF", so
            # keying on the slot name left defenses with no startable spot at all and
            # collapsed their replacement level to the single best defense.
            dedicated[next(iter(eligible))] += 1
        else:
            dedicated[slot.upper()] += 1
    return dedicated, flex


def replacement_levels(
    roster_positions: Sequence[str],
    team_count: int,
    players_by_position: dict[str, list[float]],
) -> tuple[dict[str, float], dict[str, int]]:
    """Replacement-level points per position, and how many start league-wide.

    Dedicated slots are straightforward: a 12-team league starting two running backs
    consumes 24 of them, so the 25th is replacement level. FLEX slots are then filled
    greedily from whichever eligible players actually project highest, which is what
    makes a superflex league push quarterback replacement level so much deeper.
    """
    dedicated, flex = slot_requirements(roster_positions)

    counts: dict[str, int] = defaultdict(int)
    for position, per_team in dedicated.items():
        counts[position] = per_team * team_count

    for slot, per_team in flex.items():
        eligible = SLOT_ELIGIBILITY.get(slot, frozenset())
        pool: list[tuple[float, str]] = []
        for position in eligible:
            ranked = players_by_position.get(position, [])
            # Only players below their position's current baseline are still available
            # to fill a flex spot.
            for points in ranked[counts[position] :]:
                pool.append((points, position))
        pool.sort(key=lambda item: item[0], reverse=True)
        for _, position in pool[: per_team * team_count]:
            counts[position] += 1

    replacement: dict[str, float] = {}
    for position, ranked in players_by_position.items():
        index = counts.get(position, 0)
        if not ranked:
            replacement[position] = 0.0
        elif index < len(ranked):
            replacement[position] = ranked[index]
        else:
            replacement[position] = ranked[-1]

    return replacement, dict(counts)


def positional_need(
    my_roster_positions: Iterable[str],
    roster_positions: Sequence[str],
) -> dict[str, str]:
    """Classify each position by how badly the roster still needs one.

    Flex slots count toward need for every position eligible to fill them, so a team
    with two running backs in a league starting RB, RB, FLEX is not yet done at the
    position.
    """
    dedicated, flex = slot_requirements(roster_positions)
    have = Counter(p for p in my_roster_positions if p)

    # Total startable spots a position could occupy, including flex.
    capacity: Counter = Counter(dedicated)
    for slot, count in flex.items():
        for position in SLOT_ELIGIBILITY.get(slot, frozenset()):
            capacity[position] += count

    tiers: dict[str, str] = {}
    for position, required in dedicated.items():
        owned = have.get(position, 0)
        if owned == 0 and required > 0:
            tiers[position] = "urgent"
        elif owned < required:
            tiers[position] = "urgent"
        elif owned < capacity.get(position, required):
            tiers[position] = "needed"
        else:
            tiers[position] = "depth"

    # Positions with no dedicated slot but flex eligibility (rare, e.g. FLEX-only).
    for position, cap in capacity.items():
        if position not in tiers:
            tiers[position] = "needed" if have.get(position, 0) < cap else "depth"

    # A position stocked well beyond what can ever start is a surplus.
    for position, tier in list(tiers.items()):
        if tier == "depth" and have.get(position, 0) > capacity.get(position, 0) + 1:
            tiers[position] = "surplus"

    return tiers


def _format_points(value: float) -> str:
    return f"{value:.0f}"


def unfilled_starting_slots(
    my_roster_positions: Iterable[str], roster_positions: Sequence[str]
) -> list[str]:
    """Mandatory starting positions this roster still cannot fill."""
    dedicated, _ = slot_requirements(roster_positions)
    have = Counter(p for p in my_roster_positions if p)
    missing: list[str] = []
    for position, required in dedicated.items():
        for _ in range(max(0, required - have.get(position, 0))):
            missing.append(position)
    return missing


def recommend(
    available: Sequence[PlayerProjection],
    roster_positions: Sequence[str],
    team_count: int,
    my_roster_positions: Iterable[str],
    current_pick: int | None = None,
    limit: int = 12,
    picks_remaining: int | None = None,
    baseline_pool: Sequence[PlayerProjection] | None = None,
) -> tuple[list[Recommendation], dict[str, float]]:
    """Rank the best available players for the team that is on the clock.

    When `picks_remaining` drops to the number of mandatory starting slots still
    unfilled, the board narrows to only those positions. Without that constraint a
    pure value-over-replacement ranking never takes a kicker or a defense - a bench
    receiver always out-values the best kicker - and the draft ends with an illegal
    lineup. Real drafters take those positions late for exactly this reason.

    `baseline_pool` should be the whole draftable universe, not just who is left.
    Replacement level is a property of the league's structure and the player pool as
    a whole; deriving it from the shrinking available list makes it collapse as the
    draft proceeds and inflates value absurdly. In a dynasty league where most
    players are already rostered it produced a +255 quarterback.
    """
    my_roster_positions = list(my_roster_positions)

    missing = unfilled_starting_slots(my_roster_positions, roster_positions)
    forced_positions: set[str] = set()
    if (
        picks_remaining is not None
        and picks_remaining > 0
        and missing
        and picks_remaining <= len(missing)
    ):
        forced_positions = set(missing)
        narrowed = [p for p in available if p.position in forced_positions]
        if narrowed:
            available = narrowed

    baseline_source = baseline_pool if baseline_pool is not None else available
    by_position: dict[str, list[float]] = defaultdict(list)
    for player in baseline_source:
        by_position[player.position].append(player.points)
    for ranked in by_position.values():
        ranked.sort(reverse=True)

    replacement, _ = replacement_levels(roster_positions, team_count, by_position)
    tiers = positional_need(my_roster_positions, roster_positions)

    # Rank within position, so a recommendation can say "RB4 of those left".
    position_rank: dict[str, int] = defaultdict(int)
    ordered = sorted(available, key=lambda p: p.points, reverse=True)
    ranks: dict[str, int] = {}
    for player in ordered:
        position_rank[player.position] += 1
        ranks[player.sleeper_id] = position_rank[player.position]

    recommendations: list[Recommendation] = []
    for player in available:
        baseline = replacement.get(player.position, 0.0)
        vorp = player.points - baseline
        tier = (
            "forced"
            if player.position in forced_positions
            else tiers.get(player.position, "depth")
        )
        multiplier = NEED_MULTIPLIER.get(tier, 1.0)

        adp_delta = None
        if player.adp is not None and current_pick:
            # Positive means the player has lasted past their average draft slot.
            adp_delta = player.adp - current_pick

        score = vorp * multiplier
        if adp_delta is not None and adp_delta > 0:
            # A modest nudge, capped so market value never outweighs real value.
            score += min(adp_delta * 0.25, 12.0)

        reasons: list[str] = []
        rank = ranks.get(player.sleeper_id)
        if rank:
            reasons.append(f"{player.position}{rank} of those left")
        reasons.append(
            f"{_format_points(vorp)} pts over replacement "
            f"({_format_points(baseline)})"
        )
        if tier == "forced":
            reasons.append(
                f"must fill {player.position}: "
                f"{picks_remaining} picks left, {len(missing)} slots open"
            )
        elif tier == "urgent":
            reasons.append(f"you have no starting {player.position} yet")
        elif tier == "needed":
            reasons.append(f"{player.position} still fills a starting spot")
        elif tier == "surplus":
            reasons.append(f"you are already deep at {player.position}")
        if adp_delta is not None and adp_delta >= 8:
            reasons.append(f"lasted {adp_delta:.0f} picks past ADP")
        elif adp_delta is not None and adp_delta <= -8:
            reasons.append(f"a reach of {abs(adp_delta):.0f} picks vs ADP")

        recommendations.append(
            Recommendation(
                player=player,
                vorp=round(vorp, 1),
                score=round(score, 1),
                need_tier=tier,
                adp_delta=round(adp_delta, 1) if adp_delta is not None else None,
                reasons=reasons,
            )
        )

    recommendations.sort(key=lambda r: r.score, reverse=True)
    return recommendations[:limit], replacement
