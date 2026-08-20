"""Cross-platform player identity resolution.

This is the load-bearing join of the whole application: every roster comparison,
projection, and recommendation depends on knowing that ESPN player X and Sleeper
player Y are the same human being.

The obvious approach - join on the espn_id field in Sleeper's player dump - does not
work. Measured against the live dump, espn_id is populated for only about 23% of
active fantasy-relevant players, and the gaps are not confined to rookies: Ja'Marr
Chase (5 years experience), Bucky Irving, and Brandon Aubrey all come back with
espn_id set to None. So espn_id is used when present and normalized-name matching
carries the rest.

Three data quirks discovered against the live APIs shape the code below:

  1. Sleeper's DEF entries have full_name and search_full_name set to None. They are
     keyed by team abbreviation ("HOU", "NE"), with the city in first_name and the
     nickname in last_name. Defenses must be matched by team, never by name.
  2. Team abbreviations disagree. ESPN says WSH where Sleeper says WAS, and Sleeper
     still carries legacy OAK entries. Unnormalized, this silently breaks every
     Commanders player.
  3. Names collide. There are 38 colliding normalized names among skill-position
     players - including an inactive WR "Kenneth Walker" alongside the SEA running
     back. Candidates are therefore ranked by whether they are active and rostered
     on a real NFL team, and genuinely ambiguous lookups are reported rather than
     guessed at.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

from backend.resolve import stat_map

log = logging.getLogger(__name__)

# Suffixes Sleeper keeps in search_full_name but other sources often drop.
# Ordered longest-first: "iii" must be tested before "ii", or "jamescookiii"
# gets truncated to "jamescooki" and never matches.
_SUFFIXES = ("iii", "ii", "iv", "jr", "sr", "v")

# Sleeper uses a placeholder row for unknown players; it must never match.
_PLACEHOLDER_NAMES = {"playerinvalid", "duplicateplayer"}

# ESPN abbreviation -> Sleeper abbreviation.
_TEAM_ALIASES = {
    "WSH": "WAS",
    "OAK": "LV",
    "LA": "LAR",
    "JAX": "JAC",
    "ARZ": "ARI",
    "BLT": "BAL",
    "CLV": "CLE",
    "HST": "HOU",
    "SL": "LAR",
    "SD": "LAC",
}

MATCH_ESPN_ID = "espn_id"
MATCH_NAME_POS_TEAM = "name_pos_team"
MATCH_NAME_POS = "name_pos"
MATCH_NAME_TEAM = "name_team"
MATCH_NAME = "name"
MATCH_SURNAME = "surname_pos_team"
MATCH_DEFENSE = "defense"
MATCH_NONE = "unmatched"


def normalize_team(team: str | None) -> str | None:
    """Canonicalize an NFL team abbreviation to Sleeper's spelling."""
    if not team:
        return None
    value = str(team).strip().upper()
    if value in ("", "NONE", "FA"):
        return None
    return _TEAM_ALIASES.get(value, value)


def normalize_name(name: str | None) -> str | None:
    """Lowercase and strip everything that is not a letter or digit.

    This reproduces how Sleeper builds search_full_name, verified against the live
    dump: "Amon-Ra St. Brown" becomes "amonrastbrown". Note that Sleeper does *not*
    strip suffixes - "Larry Allen Jr." becomes "larryallenjr" - which is why
    strip_suffix below exists as a separate fallback key.
    """
    if not name:
        return None
    cleaned = re.sub(r"[^a-z0-9]", "", str(name).lower())
    return cleaned or None


def strip_suffix(normalized: str | None) -> str | None:
    """Remove a trailing generational suffix from an already-normalized name.

    Lets "marvinharrisonjr" match a source that wrote "Marvin Harrison".
    """
    if not normalized:
        return None
    for suffix in _SUFFIXES:
        if normalized.endswith(suffix) and len(normalized) > len(suffix) + 2:
            return normalized[: -len(suffix)]
    return normalized


# ESPN's defaultPositionId can be a multi-position lineup slot rather than a real
# position ("RB/WR" for DeVonta Smith). These never match a Sleeper position, so they
# are treated as unknown and resolved on name plus team instead.
_COMBO_SLOTS = {"RB/WR", "WR/TE", "RB/WR/TE", "OP", "FLEX", "TQB", "DP", "DB", "DL"}


def normalize_position(position: str | None) -> str | None:
    """Canonicalize a position label across platforms.

    Returns None for ESPN combo lineup slots, which carry no real position
    information and would otherwise block every name+position lookup.
    """
    if not position:
        return None
    value = str(position).strip().upper()
    if value in ("D/ST", "DST", "DEF"):
        return "DEF"
    if value == "PK":
        return "K"
    if value in _COMBO_SLOTS:
        return None
    return value


def _surname_of(name: str | None) -> str | None:
    """Normalized last word of a name, ignoring a generational suffix."""
    if not name:
        return None
    parts = [p for p in str(name).replace(".", " ").split() if p]
    if len(parts) < 2:
        return None
    while len(parts) > 1 and normalize_name(parts[-1]) in _SUFFIXES:
        parts.pop()
    return normalize_name(parts[-1]) if len(parts) > 1 else None


@dataclass(frozen=True)
class MatchResult:
    """Outcome of resolving one external player to a Sleeper player."""

    sleeper_id: str | None
    method: str
    confidence: float
    candidates: tuple[str, ...] = ()

    @property
    def matched(self) -> bool:
        return self.sleeper_id is not None

    @property
    def ambiguous(self) -> bool:
        return len(self.candidates) > 1


@dataclass
class UnresolvedReport:
    """Everything the matcher could not confidently resolve.

    A silently wrong match corrupts every downstream number, so this is surfaced on
    every ingest rather than logged and forgotten.
    """

    unmatched: list[dict[str, Any]] = field(default_factory=list)
    ambiguous: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0

    @property
    def matched_count(self) -> int:
        return self.total - len(self.unmatched)

    @property
    def is_clean(self) -> bool:
        return not self.unmatched and not self.ambiguous

    def record_unmatched(self, descriptor: dict[str, Any]) -> None:
        self.unmatched.append(descriptor)

    def record_ambiguous(
        self, descriptor: dict[str, Any], candidates: Iterable[str]
    ) -> None:
        self.ambiguous.append({**descriptor, "candidates": list(candidates)})

    def summary(self) -> str:
        if self.total == 0:
            return "No players to resolve."
        rate = 100.0 * self.matched_count / self.total
        lines = [
            f"Resolved {self.matched_count}/{self.total} players ({rate:.1f}%)."
        ]
        if self.unmatched:
            lines.append(f"  {len(self.unmatched)} UNMATCHED:")
            for item in self.unmatched[:20]:
                lines.append(
                    f"    - {item.get('name')} "
                    f"({item.get('position')}, {item.get('team')})"
                )
            if len(self.unmatched) > 20:
                lines.append(f"    ... and {len(self.unmatched) - 20} more")
        if self.ambiguous:
            lines.append(f"  {len(self.ambiguous)} AMBIGUOUS:")
            for item in self.ambiguous[:10]:
                lines.append(
                    f"    - {item.get('name')} "
                    f"({item.get('position')}) -> {item.get('candidates')}"
                )
        if self.is_clean:
            lines.append("  All players resolved cleanly.")
        return "\n".join(lines)


class PlayerRegistry:
    """Indexes Sleeper's player universe and resolves external players against it.

    Sleeper's player_id is treated as the canonical identity because its dump is the
    most complete free source and already carries cross-platform id fields.
    """

    def __init__(self, sleeper_players: dict[str, dict[str, Any]]) -> None:
        self._players = sleeper_players
        self._by_espn_id: dict[str, str] = {}
        self._by_name_pos_team: dict[tuple[str, str, str], list[str]] = defaultdict(list)
        self._by_name_pos: dict[tuple[str, str], list[str]] = defaultdict(list)
        self._by_name: dict[str, list[str]] = defaultdict(list)
        self._by_name_team: dict[tuple[str, str], list[str]] = defaultdict(list)
        self._by_surname_pos_team: dict[tuple[str, str, str], list[str]] = defaultdict(
            list
        )
        self._by_surname_pos: dict[tuple[str, str], list[str]] = defaultdict(list)
        self._defense_by_team: dict[str, str] = {}
        self._build_indexes()

    # ---- index construction ------------------------------------------------

    def _build_indexes(self) -> None:
        for player_id, player in self._players.items():
            position = normalize_position(player.get("position"))

            if position == "DEF":
                # Defenses carry no usable name; the Sleeper key is the team abbrev.
                team = normalize_team(player.get("team") or player_id)
                if team:
                    self._defense_by_team[team] = player_id
                continue

            names = self._name_keys(player)
            if not names:
                continue

            espn_id = player.get("espn_id")
            if espn_id:
                self._by_espn_id[str(espn_id)] = player_id

            team = normalize_team(player.get("team"))
            surname = normalize_name(player.get("last_name"))
            if surname and position:
                self._by_surname_pos[(surname, position)].append(player_id)
                if team:
                    self._by_surname_pos_team[(surname, position, team)].append(
                        player_id
                    )

            for name in names:
                self._by_name[name].append(player_id)
                if team:
                    self._by_name_team[(name, team)].append(player_id)
                if position:
                    self._by_name_pos[(name, position)].append(player_id)
                    if team:
                        self._by_name_pos_team[(name, position, team)].append(player_id)

        log.debug(
            "Indexed %d players (%d with espn_id, %d defenses)",
            len(self._players),
            len(self._by_espn_id),
            len(self._defense_by_team),
        )

    def _name_keys(self, player: dict[str, Any]) -> list[str]:
        """All normalized name variants a player should be findable under."""
        primary = player.get("search_full_name") or normalize_name(
            player.get("full_name")
        )
        if not primary:
            first = player.get("first_name")
            last = player.get("last_name")
            if first and last:
                primary = normalize_name(f"{first}{last}")
        if not primary or primary in _PLACEHOLDER_NAMES:
            return []

        keys = [primary]
        stripped = strip_suffix(primary)
        if stripped and stripped != primary:
            keys.append(stripped)
        return keys

    # ---- candidate ranking -------------------------------------------------

    def _rank(self, player_id: str) -> tuple[int, int]:
        """Sort key preferring real, currently-rostered NFL players.

        Guards against the 38 known name collisions, where an inactive practice-squad
        player shares a normalized name with a starter.
        """
        player = self._players.get(player_id, {})
        active = 1 if player.get("active") else 0
        on_team = 1 if player.get("team") else 0
        return (active, on_team)

    def _resolve_candidates(
        self, candidates: list[str], method: str, confidence: float
    ) -> MatchResult:
        """Pick the best candidate, or flag ambiguity if the top two are tied."""
        if not candidates:
            return MatchResult(None, MATCH_NONE, 0.0)

        unique = list(dict.fromkeys(candidates))
        if len(unique) == 1:
            return MatchResult(unique[0], method, confidence)

        ranked = sorted(unique, key=self._rank, reverse=True)
        best, runner_up = ranked[0], ranked[1]
        if self._rank(best) == self._rank(runner_up):
            # Genuinely indistinguishable - report instead of guessing.
            return MatchResult(None, MATCH_NONE, 0.0, tuple(ranked))
        return MatchResult(best, method, confidence, tuple(ranked))

    # ---- public API --------------------------------------------------------

    def get(self, sleeper_id: str) -> dict[str, Any] | None:
        return self._players.get(sleeper_id)

    def match_espn_player(self, espn_player: dict[str, Any]) -> MatchResult:
        """Resolve one ESPN player object to a Sleeper player_id.

        Accepts the `player` sub-object from kona_player_info or from a roster entry.
        """
        position = stat_map.position(espn_player.get("defaultPositionId"))
        position = normalize_position(position)
        team = normalize_team(stat_map.pro_team(espn_player.get("proTeamId")))
        name = espn_player.get("fullName")

        return self.match(
            name=name,
            position=position,
            team=team,
            espn_id=espn_player.get("id"),
        )

    def match(
        self,
        name: str | None,
        position: str | None,
        team: str | None,
        espn_id: Any = None,
    ) -> MatchResult:
        """Resolve a player by whatever identifiers are available.

        Strategies run strongest first: espn_id, then name+position+team, then
        name+position, then bare name.
        """
        position = normalize_position(position)
        team = normalize_team(team)

        # Defenses are matched purely by team - they have no usable name in Sleeper.
        if position == "DEF":
            defense_team = team or self._defense_team_from_name(name)
            if defense_team and defense_team in self._defense_by_team:
                return MatchResult(
                    self._defense_by_team[defense_team], MATCH_DEFENSE, 1.0
                )
            return MatchResult(None, MATCH_NONE, 0.0)

        # 1. espn_id is authoritative, but covers only ~23% of relevant players.
        if espn_id is not None:
            hit = self._by_espn_id.get(str(espn_id))
            if hit:
                return MatchResult(hit, MATCH_ESPN_ID, 1.0)

        normalized = normalize_name(name)
        if not normalized:
            return MatchResult(None, MATCH_NONE, 0.0)

        variants = [normalized]
        stripped = strip_suffix(normalized)
        if stripped and stripped != normalized:
            variants.append(stripped)

        # 2. name + position + team
        if position and team:
            for variant in variants:
                found = self._by_name_pos_team.get((variant, position, team))
                if found:
                    return self._resolve_candidates(found, MATCH_NAME_POS_TEAM, 0.95)

        # 3. name + position (covers mid-week team changes)
        if position:
            for variant in variants:
                found = self._by_name_pos.get((variant, position))
                if found:
                    return self._resolve_candidates(found, MATCH_NAME_POS, 0.85)

        # 4. name + team, for players whose position ESPN reported as a combo slot
        if team:
            for variant in variants:
                found = self._by_name_team.get((variant, team))
                if found:
                    return self._resolve_candidates(found, MATCH_NAME_TEAM, 0.80)

        # 5. bare name, only when it is unambiguous
        for variant in variants:
            found = self._by_name.get(variant)
            if found:
                return self._resolve_candidates(found, MATCH_NAME, 0.70)

        # 6. Last resort: surname + position + team. Catches nickname mismatches such
        # as ESPN's "Kenneth Gainwell" against Sleeper's "Kenny Gainwell". Narrow
        # enough to be safe - it requires an exact position and team agreement - and
        # still reports ambiguity rather than guessing between two candidates.
        surname = _surname_of(name)
        if surname and position:
            if team:
                found = self._by_surname_pos_team.get((surname, position, team))
                if found:
                    return self._resolve_candidates(found, MATCH_SURNAME, 0.60)

            # Same idea without the team, for players who changed clubs between the
            # two sources' snapshots (ESPN had Gainwell on PIT while Sleeper had
            # already moved him to TB). Only accepted when exactly one plausible
            # candidate exists - _resolve_candidates reports a tie rather than
            # guessing.
            found = self._by_surname_pos.get((surname, position))
            if found:
                return self._resolve_candidates(found, MATCH_SURNAME, 0.50)

        return MatchResult(None, MATCH_NONE, 0.0)

    def _defense_team_from_name(self, name: str | None) -> str | None:
        """Recover a team from an ESPN D/ST name like "Chiefs D/ST"."""
        if not name:
            return None
        normalized = normalize_name(name.replace("D/ST", ""))
        if not normalized:
            return None
        for team, player_id in self._defense_by_team.items():
            player = self._players.get(player_id, {})
            nickname = normalize_name(player.get("last_name"))
            if nickname and nickname == normalized:
                return team
        return None

    def match_many(
        self, espn_players: Iterable[dict[str, Any]]
    ) -> tuple[dict[int, str], UnresolvedReport]:
        """Resolve a batch of ESPN players, returning a map and a report.

        The report is the point: a starter that silently fails to match is the most
        likely way this application gives confidently wrong advice.
        """
        resolved: dict[int, str] = {}
        report = UnresolvedReport()

        for espn_player in espn_players:
            report.total += 1
            result = self.match_espn_player(espn_player)
            descriptor = {
                "espn_id": espn_player.get("id"),
                "name": espn_player.get("fullName"),
                "position": stat_map.position(espn_player.get("defaultPositionId")),
                "team": stat_map.pro_team(espn_player.get("proTeamId")),
            }

            if result.matched:
                resolved[int(espn_player["id"])] = result.sleeper_id
                if result.ambiguous:
                    report.record_ambiguous(descriptor, result.candidates)
            else:
                if result.candidates:
                    report.record_ambiguous(descriptor, result.candidates)
                report.record_unmatched(descriptor)

        return resolved, report
