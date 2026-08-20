"""SQLite schema and session management.

Sleeper's player_id is the canonical player identity across the whole app; ESPN
players reach it through the player_alias table, which caches the output of
resolve.player_matching so matches are computed once and stay stable.

Each league stores its own scoring_settings and roster_positions verbatim, because
projections must be recomputed under each league's own rules - a superflex PPR league
and a standard league produce genuinely different numbers from the same raw stats.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)

from backend.config import DB_PATH

PLATFORM_SLEEPER = "sleeper"
PLATFORM_ESPN = "espn"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class League(Base):
    __tablename__ = "league"
    __table_args__ = (
        UniqueConstraint("platform", "platform_league_id", "season", name="uq_league"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(16), index=True)
    platform_league_id: Mapped[str] = mapped_column(String(64), index=True)
    season: Mapped[str] = mapped_column(String(8), index=True)
    name: Mapped[str] = mapped_column(String(200))
    total_rosters: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Stored verbatim so projections can be re-scored per league.
    scoring_settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    roster_positions: Mapped[list[str]] = mapped_column(JSON, default=list)

    synced_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    teams: Mapped[list["Team"]] = relationship(
        back_populates="league", cascade="all, delete-orphan"
    )
    drafts: Mapped[list["Draft"]] = relationship(
        back_populates="league", cascade="all, delete-orphan"
    )


class Team(Base):
    __tablename__ = "team"
    __table_args__ = (
        UniqueConstraint("league_id", "platform_team_id", name="uq_team"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("league.id"), index=True)
    platform_team_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))
    owner_name: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Marks the user's own team, so the app knows whose lineup to advise on.
    is_mine: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    ties: Mapped[int] = mapped_column(Integer, default=0)
    points_for: Mapped[float] = mapped_column(Float, default=0.0)
    points_against: Mapped[float] = mapped_column(Float, default=0.0)

    league: Mapped[League] = relationship(back_populates="teams")
    roster: Mapped[list["RosterSlot"]] = relationship(
        back_populates="team", cascade="all, delete-orphan"
    )


class Player(Base):
    """The canonical player universe, keyed by Sleeper player_id."""

    __tablename__ = "player"

    sleeper_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    full_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    search_name: Mapped[str | None] = mapped_column(String(200), index=True)
    position: Mapped[str | None] = mapped_column(String(16), index=True)
    pro_team: Mapped[str | None] = mapped_column(String(8), index=True)

    espn_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)

    status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    injury_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    years_exp: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class PlayerAlias(Base):
    """Cached cross-platform identity resolution.

    Populated by resolve.player_matching. Persisted so a match is computed once and
    does not drift between syncs.
    """

    __tablename__ = "player_alias"
    __table_args__ = (
        UniqueConstraint("platform", "platform_player_id", name="uq_alias"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(16), index=True)
    platform_player_id: Mapped[str] = mapped_column(String(64), index=True)
    sleeper_id: Mapped[str] = mapped_column(ForeignKey("player.sleeper_id"), index=True)

    method: Mapped[str] = mapped_column(String(32))
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    resolved_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class RosterSlot(Base):
    __tablename__ = "roster_slot"

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("team.id"), index=True)
    sleeper_id: Mapped[str | None] = mapped_column(
        ForeignKey("player.sleeper_id"), index=True, nullable=True
    )

    # The platform's own player id, kept so an unresolved player is still visible
    # rather than vanishing from the roster entirely.
    platform_player_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    slot: Mapped[str | None] = mapped_column(String(16), nullable=True)
    is_starter: Mapped[bool] = mapped_column(Boolean, default=False)

    team: Mapped[Team] = relationship(back_populates="roster")


class Matchup(Base):
    __tablename__ = "matchup"
    __table_args__ = (
        UniqueConstraint("league_id", "week", "team_id", name="uq_matchup"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("league.id"), index=True)
    week: Mapped[int] = mapped_column(Integer, index=True)
    matchup_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("team.id"), index=True)
    points: Mapped[float] = mapped_column(Float, default=0.0)


class Draft(Base):
    __tablename__ = "draft"
    __table_args__ = (
        UniqueConstraint("platform", "platform_draft_id", name="uq_draft"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("league.id"), index=True)
    platform: Mapped[str] = mapped_column(String(16))
    platform_draft_id: Mapped[str] = mapped_column(String(64), index=True)

    # Sleeper: pre_draft / drafting / complete. ESPN exposes only drafted/inProgress.
    status: Mapped[str | None] = mapped_column(String(32), index=True)
    draft_type: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # {slot: platform_team_id}. ESPN publishes this in its pre-draft placeholder grid
    # and Sleeper in draft_order, so a draft slot is known before any pick is made.
    draft_order: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    rounds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    season: Mapped[str] = mapped_column(String(8))
    start_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    league: Mapped[League] = relationship(back_populates="drafts")
    picks: Mapped[list["DraftPick"]] = relationship(
        back_populates="draft", cascade="all, delete-orphan"
    )


class DraftPick(Base):
    __tablename__ = "draft_pick"
    __table_args__ = (UniqueConstraint("draft_id", "pick_no", name="uq_pick"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("draft.id"), index=True)

    pick_no: Mapped[int] = mapped_column(Integer, index=True)
    round: Mapped[int | None] = mapped_column(Integer, nullable=True)
    draft_slot: Mapped[int | None] = mapped_column(Integer, nullable=True)

    platform_team_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sleeper_id: Mapped[str | None] = mapped_column(
        ForeignKey("player.sleeper_id"), index=True, nullable=True
    )
    platform_player_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    is_keeper: Mapped[bool] = mapped_column(Boolean, default=False)
    bid_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)

    draft: Mapped[Draft] = relationship(back_populates="picks")


class Projection(Base):
    """Per-league projected points, plus the raw stat line they came from.

    Keyed by league because the same raw stats score differently under different
    rules. league_id is nullable for source-native projections not tied to a league.
    """

    __tablename__ = "projection"
    __table_args__ = (
        UniqueConstraint(
            "sleeper_id", "season", "week", "source", "league_id", name="uq_projection"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    sleeper_id: Mapped[str] = mapped_column(ForeignKey("player.sleeper_id"), index=True)
    league_id: Mapped[int | None] = mapped_column(
        ForeignKey("league.id"), index=True, nullable=True
    )

    season: Mapped[str] = mapped_column(String(8), index=True)
    week: Mapped[int] = mapped_column(Integer, index=True)  # 0 means full season
    source: Mapped[str] = mapped_column(String(32), default="espn")

    points: Mapped[float] = mapped_column(Float, default=0.0)
    raw_stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    # Draft-relevant market data, carried alongside the projection.
    # board_rank is this league's own ordering: ESPN's SUPERFLEX rank for a superflex
    # league, otherwise STANDARD blended toward PPR by the league's reception value.
    # ADP cannot serve as the board spine - it saturates around pick 170, where 295
    # players share a single value.
    board_rank: Mapped[float | None] = mapped_column(Float, index=True, nullable=True)
    adp: Mapped[float | None] = mapped_column(Float, nullable=True)
    auction_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    percent_owned: Mapped[float | None] = mapped_column(Float, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


def make_engine(db_path: str | None = None, echo: bool = False):
    path = db_path or str(DB_PATH)
    return create_engine(f"sqlite:///{path}", echo=echo, future=True)


# Columns added after the first release. SQLAlchemy's create_all creates missing
# tables but never alters existing ones, and this project has no migration tool, so
# they are added by hand when absent.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("projection", "board_rank", "FLOAT"),
    ("draft", "draft_order", "JSON"),
)


def ensure_columns(engine) -> list[str]:
    """Add columns introduced after a database was first created.

    Returns the columns actually added, so a sync can report the migration.
    """
    added: list[str] = []
    with engine.begin() as connection:
        for table, column, column_type in _ADDED_COLUMNS:
            existing = {
                row[1]
                for row in connection.exec_driver_sql(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            }
            if not existing:
                continue  # table not created yet; create_all will include the column
            if column in existing:
                continue
            connection.exec_driver_sql(
                f"ALTER TABLE {table} ADD COLUMN {column} {column_type}"
            )
            added.append(f"{table}.{column}")
    return added


def init_db(engine=None):
    engine = engine or make_engine()
    Base.metadata.create_all(engine)
    ensure_columns(engine)
    return engine


def make_session_factory(engine=None):
    engine = engine or make_engine()
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
