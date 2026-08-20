"""Tests for cross-platform player identity resolution.

Every case here is drawn from a real failure observed against the live APIs. A silent
mismatch in this module corrupts every downstream number, so these are regression
tests in the strictest sense.
"""

from __future__ import annotations

import pytest

from backend.resolve.player_matching import (
    MATCH_DEFENSE,
    MATCH_ESPN_ID,
    PlayerRegistry,
    normalize_name,
    normalize_position,
    normalize_team,
    strip_suffix,
)


# --- normalization ---------------------------------------------------------


def test_normalize_name_matches_sleeper_search_name():
    # Verified against Sleeper's own search_full_name field.
    assert normalize_name("Amon-Ra St. Brown") == "amonrastbrown"
    assert normalize_name("Ja'Marr Chase") == "jamarrchase"
    assert normalize_name("D.K. Metcalf") == "dkmetcalf"
    assert normalize_name(None) is None
    assert normalize_name("") is None


def test_strip_suffix_checks_longest_suffix_first():
    """Regression: "iii" must be tested before "ii".

    Ordering the suffix tuple shortest-first truncated "jamescookiii" to
    "jamescooki", which matched nothing. This broke James Cook, Kenneth Walker,
    and Luther Burden simultaneously.
    """
    assert strip_suffix("jamescookiii") == "jamescook"
    assert strip_suffix("kennethwalkeriii") == "kennethwalker"
    assert strip_suffix("marvinharrisonjr") == "marvinharrison"
    assert strip_suffix("sergiobaileyii") == "sergiobailey"


def test_strip_suffix_leaves_ordinary_names_alone():
    assert strip_suffix("justinjefferson") == "justinjefferson"
    # Short names must not be eaten by an over-eager suffix strip.
    assert strip_suffix("ivy") == "ivy"


def test_normalize_team_reconciles_espn_and_sleeper():
    """ESPN says WSH where Sleeper says WAS; unnormalized this breaks every
    Commanders player."""
    assert normalize_team("WSH") == "WAS"
    assert normalize_team("OAK") == "LV"
    assert normalize_team("wsh") == "WAS"
    assert normalize_team("KC") == "KC"
    assert normalize_team(None) is None
    assert normalize_team("None") is None


def test_normalize_position_treats_combo_slots_as_unknown():
    """ESPN's defaultPositionId can be a lineup slot rather than a position.

    DeVonta Smith comes back as "RB/WR", which matches no Sleeper position and
    previously forced an ambiguous bare-name lookup.
    """
    assert normalize_position("RB/WR") is None
    assert normalize_position("WR/TE") is None
    assert normalize_position("RB") == "RB"
    assert normalize_position("D/ST") == "DEF"
    assert normalize_position("PK") == "K"


# --- registry --------------------------------------------------------------


@pytest.fixture
def registry() -> PlayerRegistry:
    """A miniature player universe reproducing the real dump's awkward shapes."""
    return PlayerRegistry(
        {
            # espn_id present - the ~23% case.
            "4034": {
                "full_name": "Patrick Mahomes",
                "search_full_name": "patrickmahomes",
                "position": "QB",
                "team": "KC",
                "espn_id": "3139477",
                "active": True,
            },
            # espn_id absent - the ~77% case that name matching must carry.
            "7564": {
                "full_name": "Ja'Marr Chase",
                "search_full_name": "jamarrchase",
                "position": "WR",
                "team": "CIN",
                "espn_id": None,
                "active": True,
            },
            # ESPN writes the suffix, Sleeper does not.
            "8138": {
                "full_name": "James Cook",
                "search_full_name": "jamescook",
                "position": "RB",
                "team": "BUF",
                "espn_id": None,
                "active": True,
            },
            # Collision: a real starter...
            "8151": {
                "full_name": "Kenneth Walker",
                "search_full_name": "kennethwalker",
                "position": "RB",
                "team": "SEA",
                "espn_id": None,
                "active": True,
            },
            # ...and an inactive namesake who must never win.
            "9999": {
                "full_name": "Kenneth Walker",
                "search_full_name": "kennethwalker",
                "position": "WR",
                "team": None,
                "espn_id": None,
                "active": False,
            },
            # Defenses carry no name at all in Sleeper.
            "KC": {
                "full_name": None,
                "search_full_name": None,
                "first_name": "Kansas City",
                "last_name": "Chiefs",
                "position": "DEF",
                "team": "KC",
                "active": True,
            },
            # Sleeper's placeholder row must never match anything.
            "0": {
                "full_name": "Player Invalid",
                "search_full_name": "playerinvalid",
                "position": "RB",
                "team": None,
                "active": False,
            },
        }
    )


def test_espn_id_wins_when_present(registry):
    result = registry.match(
        name="Patrick Mahomes", position="QB", team="KC", espn_id="3139477"
    )
    assert result.sleeper_id == "4034"
    assert result.method == MATCH_ESPN_ID


def test_matches_by_name_when_espn_id_is_missing(registry):
    """The load-bearing case: espn_id is None for ~77% of relevant players."""
    result = registry.match(
        name="Ja'Marr Chase", position="WR", team="CIN", espn_id=None
    )
    assert result.sleeper_id == "7564"


def test_matches_across_a_suffix_mismatch(registry):
    result = registry.match(name="James Cook III", position="RB", team="BUF")
    assert result.sleeper_id == "8138"


def test_prefers_the_active_rostered_player_on_a_name_collision(registry):
    """An inactive namesake must never outrank a real starter."""
    result = registry.match(name="Kenneth Walker III", position="RB", team="SEA")
    assert result.sleeper_id == "8151"


def test_matches_defense_by_team_not_name(registry):
    result = registry.match(name="Chiefs D/ST", position="D/ST", team="KC")
    assert result.sleeper_id == "KC"
    assert result.method == MATCH_DEFENSE


def test_matches_defense_when_espn_reports_wsh(registry):
    """Team abbreviation normalization must apply to defenses too."""
    reg = PlayerRegistry(
        {
            "WAS": {
                "first_name": "Washington",
                "last_name": "Commanders",
                "position": "DEF",
                "team": "WAS",
                "active": True,
            }
        }
    )
    assert reg.match(name=None, position="DEF", team="WSH").sleeper_id == "WAS"


def test_placeholder_row_never_matches(registry):
    assert not registry.match(
        name="Player Invalid", position="RB", team=None
    ).matched


def test_unknown_player_reports_unmatched(registry):
    result = registry.match(name="Nobody At All", position="WR", team="CIN")
    assert not result.matched


def test_report_counts_and_flags_unmatched(registry):
    espn_players = [
        {"id": 3139477, "fullName": "Patrick Mahomes", "defaultPositionId": 0,
         "proTeamId": 12},
        {"id": 111111, "fullName": "Nobody At All", "defaultPositionId": 4,
         "proTeamId": 12},
    ]
    resolved, report = registry.match_many(espn_players)

    assert resolved[3139477] == "4034"
    assert report.total == 2
    assert report.matched_count == 1
    assert not report.is_clean
    assert "Nobody At All" in report.summary()


# --- surname fallback ------------------------------------------------------


def test_surname_fallback_handles_a_nickname_mismatch():
    """ESPN writes "Kenneth Gainwell" where Sleeper writes "Kenny Gainwell".

    The two sources also disagreed on his team (PIT vs TB), so the fallback must
    work without team agreement.
    """
    registry = PlayerRegistry(
        {
            "7567": {
                "full_name": "Kenny Gainwell",
                "search_full_name": "kennygainwell",
                "first_name": "Kenny",
                "last_name": "Gainwell",
                "position": "RB",
                "team": "TB",
                "active": True,
            }
        }
    )
    result = registry.match(name="Kenneth Gainwell", position="RB", team="PIT")
    assert result.sleeper_id == "7567"
    assert result.method == "surname_pos_team"


def test_surname_fallback_refuses_to_guess_between_equals():
    """Two equally plausible namesakes must be reported, never picked."""
    registry = PlayerRegistry(
        {
            "1": {
                "full_name": "Mike Williams",
                "search_full_name": "mikewilliams",
                "last_name": "Williams",
                "position": "WR",
                "team": "NYJ",
                "active": True,
            },
            "2": {
                "full_name": "Marvin Williams",
                "search_full_name": "marvinwilliams",
                "last_name": "Williams",
                "position": "WR",
                "team": "NYJ",
                "active": True,
            },
        }
    )
    result = registry.match(name="Michael Williams", position="WR", team="NYJ")
    assert not result.matched
    assert result.ambiguous


def test_surname_fallback_does_not_fire_across_positions():
    registry = PlayerRegistry(
        {
            "1": {
                "full_name": "Kenny Gainwell",
                "search_full_name": "kennygainwell",
                "last_name": "Gainwell",
                "position": "RB",
                "team": "TB",
                "active": True,
            }
        }
    )
    assert not registry.match(
        name="Kenneth Gainwell", position="WR", team="TB"
    ).matched
