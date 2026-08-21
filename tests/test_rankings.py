"""Tests for personal rankings.

These run against a real in-memory SQLite database rather than mocks, because the
behaviour worth protecting is the persistence: that saving replaces rather than
accumulates, that ranks stay dense, and that an untouched league still falls back to
the market board.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from backend.analysis import rankings as rankings_service
from backend.db import (
    Base,
    League,
    PersonalRank,
    Player,
    Projection,
    make_engine,
    make_session_factory,
)

PPR_SLOTS = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF", "BN"]


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
        name="Test League",
        total_rosters=12,
        scoring_settings={"rec": 1.0},
        roster_positions=PPR_SLOTS,
    )
    session.add(lg)
    session.flush()

    # Six ranked players is enough to prove ordering without obscuring it.
    for i in range(1, 7):
        sleeper_id = f"p{i}"
        session.add(
            Player(
                sleeper_id=sleeper_id,
                full_name=f"Player {i}",
                position="RB",
                pro_team="KC",
                active=True,
            )
        )
        session.add(
            Projection(
                sleeper_id=sleeper_id,
                league_id=lg.id,
                season="2026",
                week=0,
                source="espn",
                points=300.0 - i,
                board_rank=float(i),
                raw_stats={},
            )
        )
    session.commit()
    return lg


# --- defaults --------------------------------------------------------------


def test_untouched_league_follows_the_market_board(session, league):
    board = rankings_service.ranking_board(session, league.id)
    assert board["isCustom"] is False
    assert [p["name"] for p in board["players"]] == [f"Player {i}" for i in range(1, 7)]
    # Nothing is stored until the user actually reorders.
    assert not rankings_service.has_custom_ranks(session, league.id)


def test_default_order_has_no_disagreement(session, league):
    board = rankings_service.ranking_board(session, league.id)
    assert all(p["delta"] == 0 for p in board["players"])


# --- saving ----------------------------------------------------------------


def test_saving_an_order_persists_it(session, league):
    order = ["p3", "p1", "p2", "p4", "p5", "p6"]
    assert rankings_service.save_order(session, league.id, order) == 6

    board = rankings_service.ranking_board(session, league.id)
    assert board["isCustom"] is True
    assert [p["sleeperId"] for p in board["players"]] == order
    assert [p["myRank"] for p in board["players"]] == [1, 2, 3, 4, 5, 6]


def test_delta_reports_disagreement_with_the_market(session, league):
    """Positive delta means you rank a player higher than the market does."""
    rankings_service.save_order(session, league.id, ["p3", "p1", "p2", "p4", "p5", "p6"])
    board = {p["sleeperId"]: p for p in rankings_service.ranking_board(session, league.id)["players"]}

    assert board["p3"]["myRank"] == 1 and board["p3"]["marketSlot"] == 3
    assert board["p3"]["delta"] == 2   # moved up two spots
    assert board["p1"]["delta"] == -1  # pushed down one
    assert board["p6"]["delta"] == 0


def test_saving_twice_replaces_rather_than_accumulates(session, league):
    rankings_service.save_order(session, league.id, ["p1", "p2", "p3", "p4", "p5", "p6"])
    rankings_service.save_order(session, league.id, ["p6", "p5", "p4", "p3", "p2", "p1"])

    rows = session.execute(
        select(PersonalRank).where(PersonalRank.league_id == league.id)
    ).scalars().all()
    assert len(rows) == 6  # not 12
    assert sorted(r.rank for r in rows) == [1, 2, 3, 4, 5, 6]


def test_duplicate_ids_are_ignored(session, league):
    """A duplicate would otherwise violate the unique constraint."""
    saved = rankings_service.save_order(
        session, league.id, ["p1", "p1", "p2", "p2", "p3"]
    )
    assert saved == 3
    board = rankings_service.ranking_board(session, league.id)
    assert [p["sleeperId"] for p in board["players"][:3]] == ["p1", "p2", "p3"]


def test_a_partial_save_keeps_the_rest_in_market_order(session, league):
    """Players absent from a saved list fall in behind, still market-ordered."""
    rankings_service.save_order(session, league.id, ["p5", "p6"])
    board = rankings_service.ranking_board(session, league.id)
    ordered = [p["sleeperId"] for p in board["players"]]
    assert ordered[:2] == ["p5", "p6"]
    assert ordered[2:] == ["p1", "p2", "p3", "p4"]


def test_saving_to_an_unknown_league_is_rejected(session):
    with pytest.raises(ValueError):
        rankings_service.save_order(session, 999, ["p1"])


# --- reset -----------------------------------------------------------------


def test_reset_reverts_to_the_market_board(session, league):
    rankings_service.save_order(session, league.id, ["p6", "p5", "p4", "p3", "p2", "p1"])
    assert rankings_service.has_custom_ranks(session, league.id)

    removed = rankings_service.reset(session, league.id)
    assert removed == 6
    assert not rankings_service.has_custom_ranks(session, league.id)

    board = rankings_service.ranking_board(session, league.id)
    assert board["isCustom"] is False
    assert [p["sleeperId"] for p in board["players"]] == [f"p{i}" for i in range(1, 7)]


def test_reset_on_a_clean_league_is_harmless(session, league):
    assert rankings_service.reset(session, league.id) == 0


# --- isolation between leagues ---------------------------------------------


def test_rankings_do_not_leak_between_leagues(session, league):
    """A superflex board and a PPR board need separate lists, so they must not share."""
    other = League(
        platform="sleeper",
        platform_league_id="2",
        season="2026",
        name="Other League",
        total_rosters=12,
        scoring_settings={"rec": 1.0},
        roster_positions=PPR_SLOTS,
    )
    session.add(other)
    session.flush()

    rankings_service.save_order(session, league.id, ["p3", "p2", "p1"])

    assert rankings_service.has_custom_ranks(session, league.id)
    assert not rankings_service.has_custom_ranks(session, other.id)
    assert rankings_service.personal_ranks(session, other.id) == {}


# --- pick alignment --------------------------------------------------------


def test_ranking_rows_are_lit_by_the_users_own_picks(session, league):
    board = rankings_service.ranking_board(session, league.id, picks=[3])
    by_rank = {p["myRank"]: p for p in board["players"]}
    assert by_rank[3]["onMyPick"] is True
    assert by_rank[3]["targetBand"] == "prime"
    assert by_rank[1]["onMyPick"] is False


# --- durability ------------------------------------------------------------


def test_rankings_survive_a_projection_resync(session, league):
    """A sync must never cost the user their rankings.

    Projections are deleted and rewritten on every sync. Personal ranks reference
    players, not projections, so they have to survive that - this pins the behaviour
    so a future change to the ingest cannot quietly wipe them.
    """
    from sqlalchemy import delete

    rankings_service.save_order(session, league.id, ["p4", "p3", "p2", "p1"])

    # Exactly what sync_projections does before rewriting.
    session.execute(
        delete(Projection).where(
            Projection.league_id == league.id,
            Projection.week == 0,
            Projection.season == "2026",
        )
    )
    session.flush()
    for i in range(1, 7):
        session.add(
            Projection(
                sleeper_id=f"p{i}",
                league_id=league.id,
                season="2026",
                week=0,
                source="espn",
                points=310.0 - i,
                board_rank=float(i),
                raw_stats={},
            )
        )
    session.commit()

    assert rankings_service.has_custom_ranks(session, league.id)
    board = rankings_service.ranking_board(session, league.id)
    assert board["isCustom"] is True
    assert [p["sleeperId"] for p in board["players"][:4]] == ["p4", "p3", "p2", "p1"]


def test_rankings_persist_across_sessions(session, league):
    """The point of the DB: close the app, reopen it, the order is still there."""
    rankings_service.save_order(session, league.id, ["p2", "p1"])
    session.expunge_all()

    reloaded = rankings_service.personal_ranks(session, league.id)
    assert reloaded["p2"] == 1
    assert reloaded["p1"] == 2


def test_an_unranked_new_player_does_not_disturb_saved_order(session, league):
    """A player who becomes available later slots in behind, order intact."""
    rankings_service.save_order(session, league.id, ["p3", "p1", "p2"])

    session.add(
        Player(sleeper_id="p99", full_name="New Guy", position="WR", pro_team="KC", active=True)
    )
    session.add(
        Projection(
            sleeper_id="p99",
            league_id=league.id,
            season="2026",
            week=0,
            source="espn",
            points=299.0,
            board_rank=1.5,
            raw_stats={},
        )
    )
    session.commit()

    board = rankings_service.ranking_board(session, league.id)
    ordered = [p["sleeperId"] for p in board["players"]]
    assert ordered[:3] == ["p3", "p1", "p2"]
    assert "p99" in ordered[3:]
