"""Player universe ingestion.

Sleeper's dump is the canonical player list: it is free, complete (~12k players), and
carries cross-platform id fields. It is ~14 MB, so SleeperClient caches it to disk and
refreshes at most daily; this module only moves it into the database.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.clients.sleeper import SleeperClient
from backend.db import Player
from backend.resolve.player_matching import (
    normalize_name,
    normalize_position,
    normalize_team,
)

log = logging.getLogger(__name__)


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _display_name(player: dict[str, Any], sleeper_id: str) -> str | None:
    """A usable name, including for defenses, which have full_name set to None."""
    full_name = player.get("full_name")
    if full_name:
        return full_name
    first, last = player.get("first_name"), player.get("last_name")
    if first and last:
        return f"{first} {last}"
    return last or first or sleeper_id


def sync_players(
    session: Session,
    client: SleeperClient | None = None,
    force_refresh: bool = False,
) -> int:
    """Upsert the whole player universe. Returns the number of rows written."""
    owns_client = client is None
    client = client or SleeperClient()
    try:
        raw = client.players(force_refresh=force_refresh)
    finally:
        if owns_client:
            client.close()

    existing = {
        row.sleeper_id: row for row in session.execute(select(Player)).scalars()
    }

    written = 0
    for sleeper_id, player in raw.items():
        position = normalize_position(player.get("position"))
        name = _display_name(player, sleeper_id)

        # Defenses have no search_full_name; fall back to the derived display name.
        search_name = player.get("search_full_name") or normalize_name(name)

        espn_id = player.get("espn_id")
        fields = {
            "full_name": name,
            "search_name": search_name,
            "position": position,
            "pro_team": normalize_team(player.get("team")),
            "espn_id": str(espn_id) if espn_id else None,
            "status": player.get("status"),
            "injury_status": player.get("injury_status") or None,
            "age": _as_int(player.get("age")),
            "years_exp": _as_int(player.get("years_exp")),
            "active": bool(player.get("active")),
        }

        row = existing.get(sleeper_id)
        if row is None:
            session.add(Player(sleeper_id=sleeper_id, **fields))
        else:
            for key, value in fields.items():
                setattr(row, key, value)
        written += 1

    session.commit()
    log.info("Synced %d players", written)
    return written


def load_registry_source(
    client: SleeperClient | None = None,
) -> dict[str, dict[str, Any]]:
    """The raw Sleeper dump, for building a PlayerRegistry.

    The registry indexes fields the database does not retain verbatim, so it is built
    from the cached dump rather than from Player rows.
    """
    owns_client = client is None
    client = client or SleeperClient()
    try:
        return client.players()
    finally:
        if owns_client:
            client.close()
