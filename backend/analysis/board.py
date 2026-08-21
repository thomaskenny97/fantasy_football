"""Live draft board state.

Assembles everything the assistant needs for one league: who is gone, who is left,
what the user already has, and who they should take next. Reads only from the
database, so a page refresh is cheap; pulling fresh picks is an explicit sync.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.analysis.draft import (
    PlayerProjection,
    positional_need,
    recommend,
    slot_requirements,
)
from backend.db import (
    Draft,
    DraftPick,
    League,
    Player,
    Projection,
    RosterSlot,
    Team,
)

log = logging.getLogger(__name__)

# Positions worth drafting. IDP and practice-squad noise would only crowd the board.
DRAFTABLE = {"QB", "RB", "WR", "TE", "K", "DEF"}


def _my_team(session: Session, league_id: int) -> Team | None:
    return session.execute(
        select(Team).where(Team.league_id == league_id, Team.is_mine.is_(True))
    ).scalar_one_or_none()


def _taken_player_ids(session: Session, league: League, draft: Draft | None) -> set[str]:
    """Players unavailable in this league: drafted, or already on a roster.

    Rosters matter for keeper and dynasty leagues, where most of the player pool is
    locked up before the draft even starts.
    """
    taken: set[str] = set()

    if draft is not None:
        for pick in session.execute(
            select(DraftPick.sleeper_id).where(DraftPick.draft_id == draft.id)
        ):
            if pick[0]:
                taken.add(pick[0])

    team_ids = [
        row[0]
        for row in session.execute(select(Team.id).where(Team.league_id == league.id))
    ]
    if team_ids:
        for row in session.execute(
            select(RosterSlot.sleeper_id).where(RosterSlot.team_id.in_(team_ids))
        ):
            if row[0]:
                taken.add(row[0])

    return taken


def _available_players(
    session: Session, league: League, taken: set[str]
) -> list[PlayerProjection]:
    rows = session.execute(
        select(Projection, Player)
        .join(Player, Player.sleeper_id == Projection.sleeper_id)
        .where(
            Projection.league_id == league.id,
            Projection.week == 0,
            Projection.season == league.season,
        )
    ).all()

    available: list[PlayerProjection] = []
    for projection, player in rows:
        if player.sleeper_id in taken:
            continue
        if player.position not in DRAFTABLE:
            continue
        available.append(
            PlayerProjection(
                sleeper_id=player.sleeper_id,
                name=player.full_name or player.sleeper_id,
                position=player.position or "",
                pro_team=player.pro_team,
                points=projection.points,
                adp=projection.adp,
                auction_value=projection.auction_value,
            )
        )
    return available


def _roster_positions_of(session: Session, team: Team | None) -> list[str]:
    if team is None:
        return []
    positions: list[str] = []
    for slot in session.execute(
        select(RosterSlot).where(RosterSlot.team_id == team.id)
    ).scalars():
        if not slot.sleeper_id:
            continue
        player = session.get(Player, slot.sleeper_id)
        if player and player.position:
            positions.append(player.position)
    return positions


def draft_state(session: Session, league_id: int, limit: int = 12) -> dict[str, Any]:
    """Everything the draft assistant shows for one league."""
    league = session.get(League, league_id)
    if league is None:
        raise ValueError(f"no league {league_id}")

    draft = session.execute(
        select(Draft).where(Draft.league_id == league_id)
    ).scalars().first()

    from backend.analysis.adp_board import personal_ranks

    team = _my_team(session, league_id)
    my_ranks = personal_ranks(session, league_id)
    taken = _taken_player_ids(session, league, draft)
    full_pool = _available_players(session, league, set())
    available = [p for p in full_pool if p.sleeper_id not in taken]

    picks = []
    if draft is not None:
        picks = (
            session.execute(
                select(DraftPick)
                .where(DraftPick.draft_id == draft.id)
                .order_by(DraftPick.pick_no.desc())
            )
            .scalars()
            .all()
        )

    team_count = league.total_rosters or 12
    current_pick = len(picks) + 1

    my_positions = _roster_positions_of(session, team)
    my_drafted = [
        p for p in picks if team and p.platform_team_id == team.platform_team_id
    ]
    for pick in my_drafted:
        if pick.sleeper_id:
            player = session.get(Player, pick.sleeper_id)
            if player and player.position:
                my_positions.append(player.position)

    # How many picks this team still has. Drives the must-fill rule that stops a
    # draft ending without a kicker or defense.
    total_rounds = (draft.rounds if draft and draft.rounds else None) or len(
        [slot for slot in (league.roster_positions or []) if slot != "IR"]
    )
    # A finished draft has no picks left regardless of how many this team used;
    # traded picks mean a team's count rarely equals the round count.
    draft_complete = bool(draft and draft.status == "complete")
    picks_remaining = (
        0
        if draft_complete
        else (max(total_rounds - len(my_drafted), 0) if total_rounds else None)
    )

    recommendations, replacement = recommend(
        available=available,
        roster_positions=league.roster_positions or [],
        team_count=team_count,
        my_roster_positions=my_positions,
        current_pick=current_pick,
        limit=limit,
        picks_remaining=picks_remaining,
        baseline_pool=full_pool,
    )

    dedicated, flex = slot_requirements(league.roster_positions or [])
    tiers = positional_need(my_positions, league.roster_positions or [])

    teams_by_platform_id = {
        t.platform_team_id: t
        for t in session.execute(
            select(Team).where(Team.league_id == league_id)
        ).scalars()
    }

    def pick_payload(pick: DraftPick) -> dict[str, Any]:
        player = session.get(Player, pick.sleeper_id) if pick.sleeper_id else None
        owner = teams_by_platform_id.get(pick.platform_team_id)
        return {
            "pickNo": pick.pick_no,
            "round": pick.round,
            "team": owner.name if owner else pick.platform_team_id,
            "isMine": bool(owner and owner.is_mine),
            "bidAmount": pick.bid_amount,
            "isKeeper": pick.is_keeper,
            "player": (
                {
                    "name": player.full_name,
                    "position": player.position,
                    "proTeam": player.pro_team,
                }
                if player
                else None
            ),
        }

    return {
        "league": {
            "id": league.id,
            "name": league.name,
            "platform": league.platform,
            "season": league.season,
            "teamCount": team_count,
            "rosterPositions": league.roster_positions,
        },
        "draft": (
            {
                "id": draft.id,
                "status": draft.status,
                "type": draft.draft_type,
                "rounds": draft.rounds,
            }
            if draft
            else None
        ),
        "currentPick": current_pick,
        "round": ((current_pick - 1) // team_count) + 1 if team_count else 1,
        "picksMade": len(picks),
        "picksRemaining": picks_remaining,
        "isComplete": draft_complete,
        "totalRounds": total_rounds,
        "availableCount": len(available),
        "poolSize": len(full_pool),
        "myTeam": (
            {
                "id": team.id,
                "name": team.name,
                "positions": sorted(my_positions),
            }
            if team
            else None
        ),
        "need": [
            {
                "position": position,
                "tier": tiers.get(position, "depth"),
                "have": my_positions.count(position),
                "starters": dedicated.get(position, 0),
            }
            for position in ("QB", "RB", "WR", "TE", "K", "DEF")
            if dedicated.get(position, 0) or tiers.get(position) == "needed"
        ],
        "replacement": {k: round(v, 1) for k, v in replacement.items()},
        "recommendations": [
            {
                "sleeperId": r.player.sleeper_id,
                "name": r.player.name,
                "position": r.player.position,
                "proTeam": r.player.pro_team,
                "points": round(r.player.points, 1),
                "vorp": r.vorp,
                "score": r.score,
                "needTier": r.need_tier,
                "adp": r.player.adp,
                "adpDelta": r.adp_delta,
                "myRank": my_ranks.get(r.player.sleeper_id),
                "auctionValue": r.player.auction_value,
                "reasons": r.reasons,
            }
            for r in recommendations
        ],
        "recentPicks": [pick_payload(p) for p in picks[:15]],
    }
