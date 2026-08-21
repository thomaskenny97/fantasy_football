"""Tests for positional dropoff curves.

The chart's whole claim is that you can compare slopes between positions, which only
holds if every series is measured the same way and shares one scale.
"""

from __future__ import annotations

import pytest

from backend.analysis.curves import CURVE_POSITIONS, dropoff_curves
from backend.db import (
    Base,
    League,
    Player,
    Projection,
    make_engine,
    make_session_factory,
)

SLOTS = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF", "BN"]


@pytest.fixture
def session():
    engine = make_engine(":memory:")
    Base.metadata.create_all(engine)
    Session = make_session_factory(engine)
    with Session() as s:
        yield s


@pytest.fixture
def league(session):
    lg = League(
        platform="espn",
        platform_league_id="1",
        season="2026",
        name="Curve League",
        total_rosters=12,
        scoring_settings={"rec": 1.0},
        roster_positions=SLOTS,
    )
    session.add(lg)
    session.flush()

    # Each position decays at a deliberately different rate, so a test can tell
    # a steep curve from a shallow one.
    decay = {"QB": 2.0, "RB": 9.0, "WR": 5.0, "TE": 12.0, "K": 1.0}
    for position, step in decay.items():
        for i in range(30):
            pid = f"{position}{i}"
            session.add(
                Player(
                    sleeper_id=pid,
                    full_name=f"{position} {i}",
                    position=position,
                    pro_team="KC",
                    active=True,
                )
            )
            session.add(
                Projection(
                    sleeper_id=pid,
                    league_id=lg.id,
                    season="2026",
                    week=0,
                    source="espn",
                    points=300.0 - i * step,
                    board_rank=float(i * 5 + list(decay).index(position)),
                    raw_stats={},
                )
            )
    session.commit()
    return lg


def test_returns_one_series_per_plotted_position(session, league):
    curves = dropoff_curves(session, league.id)
    assert [s["position"] for s in curves["series"]] == list(CURVE_POSITIONS)


def test_kickers_are_excluded(session, league):
    """K and DEF are flat and only compress the scale for the positions that matter."""
    curves = dropoff_curves(session, league.id)
    assert "K" not in [s["position"] for s in curves["series"]]


def test_each_series_is_sorted_best_first(session, league):
    for series in dropoff_curves(session, league.id)["series"]:
        points = [p["points"] for p in series["points"]]
        assert points == sorted(points, reverse=True)


def test_ranks_start_at_one_and_are_dense(session, league):
    for series in dropoff_curves(session, league.id)["series"]:
        ranks = [p["rank"] for p in series["points"]]
        assert ranks == list(range(1, len(ranks) + 1))


def test_depth_limits_each_series(session, league):
    curves = dropoff_curves(session, league.id, depth=10)
    assert all(len(s["points"]) <= 10 for s in curves["series"])


def test_steeper_decay_produces_a_steeper_curve(session, league):
    """TE falls fastest here, QB slowest - the chart must reflect that."""
    series = {s["position"]: s for s in dropoff_curves(session, league.id)["series"]}

    def fall(position: str) -> float:
        pts = series[position]["points"]
        return pts[0]["points"] - pts[9]["points"]

    assert fall("TE") > fall("RB") > fall("WR") > fall("QB")


def test_replacement_level_is_reported_per_position(session, league):
    for series in dropoff_curves(session, league.id)["series"]:
        assert series["replacement"] > 0
        assert series["startersLeagueWide"] > 0


def test_shared_scale_spans_every_series(session, league):
    """One y-scale, or slopes are not comparable between positions."""
    curves = dropoff_curves(session, league.id)
    every = [p["points"] for s in curves["series"] for p in s["points"]]
    assert curves["yMin"] == min(every)
    assert curves["yMax"] == max(every)


def test_unknown_league_is_rejected(session):
    with pytest.raises(ValueError):
        dropoff_curves(session, 999)
