"""ESPN Fantasy Football API client.

ESPN publishes no official API. This uses the same endpoints their web app calls.
Two things differ from most guides found online, both verified against the live API:

  1. The legacy host fantasy.espn.com/apis/v3/... now 302-redirects to a marketing
     page. All requests must go to lm-api-reads.fantasy.espn.com.
  2. The documented leagueHistory path returns 404. Past seasons are reachable
     through the ordinary /seasons/<year>/ path, verified for 2022 through 2026.

Private leagues authenticate with two browser cookies, SWID and espn_s2. The espn_s2
cookie expires (roughly a year, sooner on logout or password change), so failures are
surfaced explicitly rather than being allowed to look like empty data.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"


class EspnError(RuntimeError):
    """Raised when ESPN returns an unusable response."""


class EspnAuthError(EspnError):
    """Raised when ESPN rejects the SWID/espn_s2 cookies.

    Almost always means espn_s2 has expired and needs re-copying from the browser.
    """


class EspnClient:
    def __init__(
        self,
        swid: str | None = None,
        s2: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.swid = swid
        self.s2 = s2
        cookies = {"SWID": swid, "espn_s2": s2} if swid and s2 else {}
        self._client = httpx.Client(
            timeout=timeout,
            cookies=cookies,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; fantasy-football-app/0.1)",
                "Accept": "application/json",
            },
            follow_redirects=False,  # a redirect here means the endpoint moved
        )

    @property
    def has_cookies(self) -> bool:
        return bool(self.swid and self.s2)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "EspnClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _request(
        self,
        url: str,
        views: Iterable[str] = (),
        params: dict[str, Any] | None = None,
        fantasy_filter: dict[str, Any] | None = None,
    ) -> Any:
        query: list[tuple[str, str]] = []
        for key, value in (params or {}).items():
            query.append((key, str(value)))
        # ESPN takes repeated view params rather than a comma-separated list.
        for view in views:
            query.append(("view", view))

        headers = {}
        if fantasy_filter:
            headers["x-fantasy-filter"] = json.dumps(fantasy_filter)

        resp = self._client.get(url, params=query, headers=headers)

        if resp.is_redirect:
            location = resp.headers.get("location")
            raise EspnError(
                f"ESPN redirected {url} to {location!r}. "
                "The endpoint has moved - check the base URL."
            )
        if resp.status_code == 401:
            raise EspnAuthError(
                "ESPN rejected the request (401). Your espn_s2 cookie has most likely "
                "expired. Re-copy ESPN_SWID and ESPN_S2 from your browser: "
                "F12 > Application > Cookies > https://www.espn.com"
            )
        if resp.status_code == 404:
            raise EspnError(
                f"ESPN returned 404 for {url}. Either the league ID is wrong, the "
                "league did not exist in that season, or the league is private and "
                "no cookies were supplied."
            )
        resp.raise_for_status()
        return resp.json()

    def _league_url(self, league_id: str | int, season: str | int) -> str:
        return f"{BASE_URL}/seasons/{season}/segments/0/leagues/{league_id}"

    # ---- league data -------------------------------------------------------

    def league(
        self,
        league_id: str | int,
        season: str | int,
        views: Iterable[str] = ("mTeam", "mRoster", "mSettings"),
    ) -> dict[str, Any]:
        """League payload. Compose the views you need in a single request."""
        return self._request(self._league_url(league_id, season), views=views)

    def settings(self, league_id: str | int, season: str | int) -> dict[str, Any]:
        return self.league(league_id, season, views=("mSettings",))

    def teams_and_rosters(
        self, league_id: str | int, season: str | int
    ) -> dict[str, Any]:
        return self.league(league_id, season, views=("mTeam", "mRoster"))

    def matchups(
        self, league_id: str | int, season: str | int, week: int | None = None
    ) -> dict[str, Any]:
        params = {"scoringPeriodId": week} if week else None
        return self._request(
            self._league_url(league_id, season),
            views=("mMatchup", "mMatchupScore"),
            params=params,
        )

    def draft(self, league_id: str | int, season: str | int) -> dict[str, Any]:
        """Draft board.

        Pre-draft this returns drafted=false, inProgress=false with no picks -
        ESPN exposes nothing until the draft actually starts.
        """
        return self._request(
            self._league_url(league_id, season), views=("mDraftDetail",)
        )

    # ---- player universe ---------------------------------------------------

    def players(
        self,
        league_id: str | int,
        season: str | int,
        limit: int = 500,
        rank_type: str = "PPR",
        week: int | None = None,
    ) -> list[dict[str, Any]]:
        """Players with ADP, auction values, draft ranks, and raw stat projections.

        Each returned player carries ownership.averageDraftPosition,
        draftRanksByRankType, and a stats list. Within stats, entries with
        statSourceId == 1 are projections and statSourceId == 0 are actuals; each
        entry's own stats dict holds raw per-category values keyed by ESPN statId,
        which is what allows re-scoring under any league's rules.
        """
        fantasy_filter = {
            "players": {
                "limit": limit,
                "sortDraftRanks": {
                    "sortPriority": 100,
                    "sortAsc": True,
                    "value": rank_type,
                },
            }
        }
        params = {"scoringPeriodId": week} if week else None
        payload = self._request(
            self._league_url(league_id, season),
            views=("kona_player_info",),
            params=params,
            fantasy_filter=fantasy_filter,
        )
        return payload.get("players") or []

    # ---- credential check --------------------------------------------------

    def verify_credentials(self, league_id: str | int, season: str | int) -> None:
        """Fail fast and loudly if cookies are missing or expired.

        Called at the start of a sync so an expired cookie surfaces as a clear
        message instead of silently empty rosters.
        """
        try:
            payload = self.settings(league_id, season)
        except EspnAuthError:
            raise
        except EspnError as exc:
            if not self.has_cookies:
                raise EspnAuthError(
                    f"Could not read ESPN league {league_id} and no cookies are "
                    "configured. If this league is private, set ESPN_SWID and "
                    "ESPN_S2 in .env."
                ) from exc
            raise

        if not payload.get("settings"):
            raise EspnAuthError(
                f"ESPN returned a response for league {league_id} with no settings. "
                "This usually means the cookies are stale."
            )
        name = payload.get("settings", {}).get("name", "<unnamed>")
        log.info("ESPN league %s OK: %r", league_id, name)
