"""Configuration loaded from the gitignored .env file.

This app runs locally for a single user, so credentials live in .env rather than
in any encrypted store. Nothing here should ever be committed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
DB_PATH = DATA_DIR / "fantasy.db"

load_dotenv(PROJECT_ROOT / ".env")


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


def _split_ids(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


@dataclass
class EspnConfig:
    league_ids: list[str] = field(default_factory=list)
    swid: str | None = None
    s2: str | None = None

    # Explicit team id, used when SWID does not appear in the league's membership.
    # ESPN leagues can reference an older identity than the current browser session,
    # in which case owner matching finds nothing even though the cookies are valid.
    team_id: str | None = None

    @property
    def has_cookies(self) -> bool:
        return bool(self.swid and self.s2)

    @property
    def cookies(self) -> dict[str, str]:
        """Cookie jar for authenticated requests, empty for public leagues."""
        if not self.has_cookies:
            return {}
        return {"SWID": self.swid or "", "espn_s2": self.s2 or ""}


@dataclass
class Config:
    sleeper_username: str | None
    espn: EspnConfig
    season: str | None

    @classmethod
    def load(cls) -> "Config":
        swid = os.getenv("ESPN_SWID") or None
        # The SWID cookie is a GUID wrapped in braces. Browsers sometimes copy it
        # without them, and ESPN rejects the bare form, so normalize here.
        if swid and not swid.startswith("{"):
            swid = "{" + swid.strip("{}") + "}"

        return cls(
            sleeper_username=os.getenv("SLEEPER_USERNAME") or None,
            espn=EspnConfig(
                league_ids=_split_ids(os.getenv("ESPN_LEAGUE_IDS")),
                swid=swid,
                s2=os.getenv("ESPN_S2") or None,
                team_id=os.getenv("ESPN_TEAM_ID") or None,
            ),
            season=os.getenv("SEASON") or None,
        )

    def require_sleeper_username(self) -> str:
        if not self.sleeper_username:
            raise ConfigError(
                "SLEEPER_USERNAME is not set. Copy .env.example to .env and fill it in."
            )
        return self.sleeper_username


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
