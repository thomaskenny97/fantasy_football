"""Sleeper API client.

Sleeper's read API is fully public — no key, no cookies, no auth of any kind.
Everything hangs off a username, which resolves to a user_id.

Sleeper asks callers to stay under roughly 1000 requests/minute. Normal use here
is a few dozen per sync, so no throttling is implemented, but the player dump is
14 MB and is cached to disk because it must never be fetched in a request path.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from backend.config import CACHE_DIR

log = logging.getLogger(__name__)

BASE_URL = "https://api.sleeper.app/v1"
PLAYERS_CACHE_MAX_AGE_S = 24 * 60 * 60  # Sleeper updates this dump about once a day


class SleeperError(RuntimeError):
    """Raised when Sleeper returns an unusable response."""


class SleeperClient:
    def __init__(self, timeout: float = 30.0) -> None:
        self._client = httpx.Client(
            base_url=BASE_URL,
            timeout=timeout,
            headers={"User-Agent": "fantasy-football-app/0.1"},
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SleeperClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, path: str) -> Any:
        resp = self._client.get(path)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        if not resp.content:
            return None
        return resp.json()

    # ---- state -------------------------------------------------------------

    def nfl_state(self) -> dict[str, Any]:
        """Current season, week, and season type (pre/regular/post)."""
        state = self._get("/state/nfl")
        if not state:
            raise SleeperError("Sleeper returned no NFL state")
        return state

    # ---- users and leagues -------------------------------------------------

    def user(self, username_or_id: str) -> dict[str, Any] | None:
        """Look up a user. Sleeper accepts either a username or a numeric user_id."""
        return self._get(f"/user/{username_or_id}")

    def user_id(self, username: str) -> str:
        user = self.user(username)
        if not user or not user.get("user_id"):
            raise SleeperError(
                f"Sleeper has no user named {username!r}. "
                "Check SLEEPER_USERNAME — it is your username, not your display name."
            )
        return str(user["user_id"])

    def leagues(self, user_id: str, season: str) -> list[dict[str, Any]]:
        return self._get(f"/user/{user_id}/leagues/nfl/{season}") or []

    def league(self, league_id: str) -> dict[str, Any] | None:
        """Full league object, including scoring_settings and roster_positions."""
        return self._get(f"/league/{league_id}")

    def rosters(self, league_id: str) -> list[dict[str, Any]]:
        return self._get(f"/league/{league_id}/rosters") or []

    def league_users(self, league_id: str) -> list[dict[str, Any]]:
        return self._get(f"/league/{league_id}/users") or []

    def matchups(self, league_id: str, week: int) -> list[dict[str, Any]]:
        return self._get(f"/league/{league_id}/matchups/{week}") or []

    def transactions(self, league_id: str, week: int) -> list[dict[str, Any]]:
        return self._get(f"/league/{league_id}/transactions/{week}") or []

    # ---- drafts ------------------------------------------------------------

    def user_drafts(self, user_id: str, season: str) -> list[dict[str, Any]]:
        return self._get(f"/user/{user_id}/drafts/nfl/{season}") or []

    def league_drafts(self, league_id: str) -> list[dict[str, Any]]:
        return self._get(f"/league/{league_id}/drafts") or []

    def draft(self, draft_id: str) -> dict[str, Any] | None:
        """Draft object. `status` is one of pre_draft / drafting / complete."""
        return self._get(f"/draft/{draft_id}")

    def draft_picks(self, draft_id: str) -> list[dict[str, Any]]:
        """Picks made so far.

        Each pick carries denormalized player metadata (name, position, team), so
        a live draft board does not need to join against the 14 MB player dump.
        """
        return self._get(f"/draft/{draft_id}/picks") or []

    # ---- player universe ---------------------------------------------------

    def players(self, force_refresh: bool = False) -> dict[str, dict[str, Any]]:
        """All NFL players keyed by Sleeper player_id.

        ~14 MB / ~12k players. Cached to disk and refreshed at most daily.
        """
        cache_path = Path(CACHE_DIR) / "sleeper_players.json"

        if not force_refresh and cache_path.exists():
            age = time.time() - cache_path.stat().st_mtime
            if age < PLAYERS_CACHE_MAX_AGE_S:
                log.debug("Using cached Sleeper players (%.1f h old)", age / 3600)
                return json.loads(cache_path.read_text(encoding="utf-8"))

        log.info("Fetching Sleeper player dump (~14 MB)...")
        data = self._get("/players/nfl")
        if not data:
            # Fall back to a stale cache rather than failing the whole sync.
            if cache_path.exists():
                log.warning("Player fetch failed; falling back to stale cache")
                return json.loads(cache_path.read_text(encoding="utf-8"))
            raise SleeperError("Sleeper returned no player data and no cache exists")

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(data), encoding="utf-8")
        log.info("Cached %d players", len(data))
        return data
