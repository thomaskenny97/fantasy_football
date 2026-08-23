"""Tests for database setup."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from backend.db import League, init_db, make_engine, make_session_factory


def test_engine_creates_a_missing_database_directory(tmp_path):
    """A fresh clone has no data/ directory and SQLite will not make one.

    data/ holds only gitignored files, so git does not carry it. Before this,
    status, serve and check all died with "unable to open database file" until a
    sync happened to run first.
    """
    target = tmp_path / "nested" / "deeper" / "fantasy.db"
    assert not target.parent.exists()

    engine = make_engine(str(target))
    init_db(engine)

    assert target.parent.is_dir()
    assert target.exists()


def test_a_brand_new_database_is_queryable(tmp_path):
    """The path a first-run `status` takes: open it, ask a question, get nothing."""
    engine = init_db(make_engine(str(tmp_path / "data" / "fantasy.db")))
    Session = make_session_factory(engine)
    with Session() as session:
        assert session.execute(select(League)).scalars().all() == []


def test_in_memory_engine_needs_no_directory():
    """Tests use :memory:, which must not be treated as a path to create."""
    engine = init_db(make_engine(":memory:"))
    Session = make_session_factory(engine)
    with Session() as session:
        assert session.execute(select(League)).scalars().all() == []
    assert not Path(":memory:").exists()
