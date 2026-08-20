"""ESPN identifier maps.

ESPN returns raw stat lines keyed by integer statId, with no names anywhere in the
payload. This module vendors those maps so the 44-odd categories that appear in a
projection can be turned into named stats and re-scored under any league's rules.

Vendored from the espn-api package (MIT licensed, v0.46.0) rather than
reverse-engineered by hand, and rather than taken as a runtime dependency - the
library shapes its responses in ways that hide the raw stat lines this app needs.

Note that several statIds map to the same canonical name (ESPN emits both a raw and
a rounded variant of some categories, e.g. 3 and 22 both mean passingYards).
Consumers should read a category once, preferring the lowest statId present.
"""

from __future__ import annotations


# statId -> canonical stat name
STAT_ID_TO_NAME: dict[int, str] = {
    0: 'passingAttempts',
    1: 'passingCompletions',
    2: 'passingIncompletions',
    3: 'passingYards',
    4: 'passingTouchdowns',
    15: 'passing40PlusYardTD',
    16: 'passing50PlusYardTD',
    17: 'passing300To399YardGame',
    18: 'passing400PlusYardGame',
    19: 'passing2PtConversions',
    20: 'passingInterceptions',
    21: 'passingCompletionPercentage',
    22: 'passingYards',
    23: 'rushingAttempts',
    24: 'rushingYards',
    25: 'rushingTouchdowns',
    26: 'rushing2PtConversions',
    35: 'rushing40PlusYardTD',
    36: 'rushing50PlusYardTD',
    37: 'rushing100To199YardGame',
    38: 'rushing200PlusYardGame',
    39: 'rushingYardsPerAttempt',
    40: 'rushingYards',
    41: 'receivingReceptions',
    42: 'receivingYards',
    43: 'receivingTouchdowns',
    44: 'receiving2PtConversions',
    45: 'receiving40PlusYardTD',
    46: 'receiving50PlusYardTD',
    53: 'receivingReceptions',
    56: 'receiving100To199YardGame',
    57: 'receiving200PlusYardGame',
    58: 'receivingTargets',
    59: 'receivingYardsAfterCatch',
    60: 'receivingYardsPerReception',
    61: 'receivingYards',
    62: '2PtConversions',
    63: 'fumbleRecoveredForTD',
    64: 'passingTimesSacked',
    68: 'fumbles',
    72: 'lostFumbles',
    73: 'turnovers',
    74: 'madeFieldGoalsFrom50Plus',
    75: 'attemptedFieldGoalsFrom50Plus',
    76: 'missedFieldGoalsFrom50Plus',
    77: 'madeFieldGoalsFrom40To49',
    78: 'attemptedFieldGoalsFrom40To49',
    79: 'missedFieldGoalsFrom40To49',
    80: 'madeFieldGoalsFromUnder40',
    81: 'attemptedFieldGoalsFromUnder40',
    82: 'missedFieldGoalsFromUnder40',
    83: 'madeFieldGoals',
    84: 'attemptedFieldGoals',
    85: 'missedFieldGoals',
    86: 'madeExtraPoints',
    87: 'attemptedExtraPoints',
    88: 'missedExtraPoints',
    89: 'defensive0PointsAllowed',
    90: 'defensive1To6PointsAllowed',
    91: 'defensive7To13PointsAllowed',
    92: 'defensive14To17PointsAllowed',
    93: 'defensiveBlockedKickForTouchdowns',
    94: 'defensiveTouchdowns',
    95: 'defensiveInterceptions',
    96: 'defensiveFumbles',
    97: 'defensiveBlockedKicks',
    98: 'defensiveSafeties',
    99: 'defensiveSacks',
    101: 'kickoffReturnTouchdowns',
    102: 'puntReturnTouchdowns',
    103: 'interceptionReturnTouchdowns',
    104: 'fumbleReturnTouchdowns',
    105: 'defensivePlusSpecialTeamsTouchdowns',
    106: 'defensiveForcedFumbles',
    107: 'defensiveAssistedTackles',
    108: 'defensiveSoloTackles',
    109: 'defensiveTotalTackles',
    113: 'defensivePassesDefensed',
    114: 'kickoffReturnYards',
    115: 'puntReturnYards',
    118: 'puntsReturned',
    120: 'defensivePointsAllowed',
    121: 'defensive18To21PointsAllowed',
    122: 'defensive22To27PointsAllowed',
    123: 'defensive28To34PointsAllowed',
    124: 'defensive35To45PointsAllowed',
    125: 'defensive45PlusPointsAllowed',
    127: 'defensiveYardsAllowed',
    128: 'defensiveLessThan100YardsAllowed',
    129: 'defensive100To199YardsAllowed',
    130: 'defensive200To299YardsAllowed',
    131: 'defensive300To349YardsAllowed',
    132: 'defensive350To399YardsAllowed',
    133: 'defensive400To449YardsAllowed',
    134: 'defensive450To499YardsAllowed',
    135: 'defensive500To549YardsAllowed',
    136: 'defensive550PlusYardsAllowed',
    138: 'netPunts',
    139: 'puntYards',
    140: 'puntsInsideThe10',
    141: 'puntsInsideThe20',
    142: 'blockedPunts',
    145: 'puntTouchbacks',
    146: 'puntFairCatches',
    147: 'puntAverage',
    148: 'puntAverage44.0+',
    149: 'puntAverage42.0-43.9',
    150: 'puntAverage40.0-41.9',
    151: 'puntAverage38.0-39.9',
    152: 'puntAverage36.0-37.9',
    153: 'puntAverage34.0-35.9',
    154: 'puntAverage33.9AndUnder',
    155: 'teamWin',
    156: 'teamLoss',
    157: 'teamTie',
    158: 'pointsScored',
    160: 'pointsMargin',
    161: '25+pointWinMargin',
    162: '20-24pointWinMargin',
    163: '15-19pointWinMargin',
    164: '10-14pointWinMargin',
    165: '5-9pointWinMargin',
    166: '1-4pointWinMargin',
    167: '1-4pointLossMargin',
    168: '5-9pointLossMargin',
    169: '10-14pointLossMargin',
    170: '15-19pointLossMargin',
    171: '20-24pointLossMargin',
    172: '25+pointLossMargin',
    174: 'winPercentage',
    187: 'defensivePointsAllowed',
    201: 'madeFieldGoalsFrom60Plus',
    202: 'attemptedFieldGoalsFrom60Plus',
    203: 'missedFieldGoalsFrom60Plus',
    205: 'defensive2PtReturns',
    206: 'defensive2PtReturns',
}

# ESPN positionId / lineupSlotId -> position abbreviation
POSITION_MAP: dict[int, str] = {
    0: 'QB',
    1: 'TQB',
    2: 'RB',
    3: 'RB/WR',
    4: 'WR',
    5: 'WR/TE',
    6: 'TE',
    7: 'OP',
    8: 'DT',
    9: 'DE',
    10: 'LB',
    11: 'DL',
    12: 'CB',
    13: 'S',
    14: 'DB',
    15: 'DP',
    16: 'D/ST',
    17: 'K',
    18: 'P',
    19: 'HC',
    20: 'BE',
    21: 'IR',
    22: '',
    23: 'RB/WR/TE',
    24: 'ER',
    25: 'Rookie',
}

# ESPN proTeamId -> team abbreviation
PRO_TEAM_MAP: dict[int, str] = {
    0: 'None',
    1: 'ATL',
    2: 'BUF',
    3: 'CHI',
    4: 'CIN',
    5: 'CLE',
    6: 'DAL',
    7: 'DEN',
    8: 'DET',
    9: 'GB',
    10: 'TEN',
    11: 'IND',
    12: 'KC',
    13: 'LV',
    14: 'LAR',
    15: 'MIA',
    16: 'MIN',
    17: 'NE',
    18: 'NO',
    19: 'NYG',
    20: 'NYJ',
    21: 'PHI',
    22: 'ARI',
    23: 'PIT',
    24: 'LAC',
    25: 'SF',
    26: 'SEA',
    27: 'TB',
    28: 'WSH',
    29: 'CAR',
    30: 'JAX',
    33: 'BAL',
    34: 'HOU',
}

def stat_name(stat_id: int | str) -> str | None:
    """Canonical name for an ESPN statId, or None if unknown."""
    try:
        return STAT_ID_TO_NAME.get(int(stat_id))
    except (TypeError, ValueError):
        return None


def named_stats(raw: dict[str, float]) -> dict[str, float]:
    """Convert a raw ESPN {statId: value} dict into {stat_name: value}.

    Where two statIds collapse to the same name, the lower statId wins - ESPN's
    duplicate entries are rounded variants of the same underlying category.
    """
    out: dict[str, float] = {}
    for key in sorted(raw, key=lambda k: int(k)):
        name = stat_name(key)
        if name and name not in out:
            out[name] = raw[key]
    return out


def position(position_id: int | None) -> str | None:
    """Position abbreviation for an ESPN defaultPositionId."""
    if position_id is None:
        return None
    return POSITION_MAP.get(int(position_id)) or None


def pro_team(team_id: int | None) -> str | None:
    """NFL team abbreviation for an ESPN proTeamId. Returns None for free agents."""
    if team_id is None:
        return None
    return PRO_TEAM_MAP.get(int(team_id)) or None
