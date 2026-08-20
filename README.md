# fantasy_football

A fantasy football analytics application that pulls your teams from **Sleeper** and
**ESPN** into one place, scores both under their own league rules, and supports the
draft and the season.

Status: ingestion, player identity resolution, the scoring engine, a web dashboard,
a live draft assistant, and a per-league ADP heat map are built and verified against
the live APIs.

## Running it

```bash
.venv/Scripts/python.exe -m backend.cli sync     # pull the latest data
.venv/Scripts/python.exe -m backend.cli serve    # then open http://127.0.0.1:8000
```

`serve` runs one process: FastAPI serves the API and the built React bundle from the
same origin. Syncing stays a separate, explicit step, so loading the page can never
trigger a 14 MB player download or hammer ESPN's undocumented endpoints.

## First-time setup

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
# source .venv/bin/activate && pip install -r requirements.txt  # macOS/Linux

cd frontend && npm install && npm run build && cd ..

cp .env.example .env    # then fill it in, see below
.venv/Scripts/python.exe -m backend.cli check    # verify credentials
.venv/Scripts/python.exe -m backend.cli sync     # pull everything
```

### Working on the frontend

```bash
.venv/Scripts/python.exe -m backend.cli serve --reload   # API on :8000
cd frontend && npm run dev                               # UI on :5173, hot reload
```

Open <http://localhost:5173> during development. The dev server proxies to the API on
:8000, which is already allowed through CORS.

## Credentials

`.env` is gitignored and never leaves your machine.

### Sleeper — just your username

Sleeper's read API is completely public: no key, no cookies, no login. Your username
is all that is needed.

```
SLEEPER_USERNAME=your_username
```

Use your **username**, not your display name. If `check` reports the user does not
exist, that is usually the reason.

### ESPN — league ID, plus cookies for private leagues

```
ESPN_LEAGUE_IDS=123456          # comma-separate several
ESPN_SWID={XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}
ESPN_S2=AEB...
```

The league ID is in your league URL: `.../leagues/123456`.

Public leagues need only the ID. Private leagues need two browser cookies:

1. Log in at <https://www.espn.com> in Chrome
2. `F12` → **Application** → **Cookies** → `https://www.espn.com`
3. Copy the values of `SWID` (a GUID, keep the braces) and `espn_s2` (a long string)

`espn_s2` **expires** — roughly a year, sooner if you log out or change your password.
When it does, `check` and `sync` fail with an explicit "cookies expired" message
rather than silently returning empty rosters. Re-copy both values when that happens.

Setting `ESPN_SWID` normally lets the app identify *which* team is yours.

**If it cannot**, your league records an older ESPN identity than your current browser
session, so the SWID matches none of its members. Auth still works; only the ownership
lookup fails. Run `python -m backend.cli teams` to list the teams and their ids, then
set the id explicitly:

```
ESPN_TEAM_ID=11
```

## Commands

| Command | What it does |
| --- | --- |
| `python -m backend.cli serve` | Run the dashboard on http://127.0.0.1:8000 |
| `python -m backend.cli draft` | Watch a draft in the terminal and recommend picks |
| `python -m backend.cli draft --once` | Print the board once and exit |
| `python -m backend.cli check` | Verify both platforms' credentials, write nothing |
| `python -m backend.cli sync` | Pull leagues, rosters, and drafts into SQLite |
| `python -m backend.cli sync --skip-players` | Reuse the cached player universe (faster) |
| `python -m backend.cli teams` | List ESPN teams and ids (for `ESPN_TEAM_ID`) |
| `python -m backend.cli status` | Show stored leagues, teams, and drafts |
| `python -m pytest tests/ -q` | Run the test suite |

Data lands in `data/fantasy.db`. The Sleeper player dump is cached in `data/cache/`
and refreshed at most once a day.

## On draft day

Open the dashboard, pick your league, and switch to the **Draft** tab. While a draft
is running the board pulls new picks every 15 seconds and re-ranks after each one.

There is also a terminal version, useful as a second screen or a fallback:

```bash
.venv/Scripts/python.exe -m backend.cli draft            # watches the live draft
.venv/Scripts/python.exe -m backend.cli draft --league 3 # a specific league
```

It picks the league whose draft has not finished, so during your ESPN draft it needs
no arguments.

## Planning the draft: the ADP board

The **Board** tab answers the question you plan around: given your slot, who will still
be there at each of your picks? Two views, both per league:

- **List** — one block per round showing the players realistically available at that
  pick, each with a probability that they last that long, plus a positional summary.
- **Grid** — the full board, rounds x slots, with your snake path highlighted.

A slot slider defaults to your real slot and lets you compare any other, which is useful
before a draft order is drawn.

The positional summary is the point. At slot 12 of 12 in a 12-team PPR league:

```
  rd  pick   what is realistically there
  R1    12   RB7  WR2  TE1
  R2    13   RB7  WR2  TE1
  R6    61   WR6  RB4
  R9   108   TE4  RB3  WR3
```

Running backs are thick at the turn while receivers stay available for six more rounds,
so the scarce commodity is not the one the first two picks push you toward.

### Every league gets its own board

Three things make each board different, and all three are read from the league itself:

| | Dynasty Beasts | Crone Panthers | NYC Fellas |
|---|---|---|---|
| Board | superflex | half-PPR | PPR |
| Teams / type | 12, linear | 10, linear | 12, snake |
| Your picks | 4, 16, 28, 40 | 6, 16, 26 | 12, 13, 36, 37 |

The superflex board is a genuinely different board: Josh Allen, Jayden Daniels and Lamar
Jackson occupy its first three slots, where the PPR board opens with Jahmyr Gibbs, Bijan
Robinson and Ja'Marr Chase.

Ordering comes from ESPN's format-specific rank rather than its ADP, because ADP
saturates - 295 players share a single value near pick 170, which cannot order a
16-round board. ADP is still shown where it discriminates. Availability is modelled as a
normal distribution about a player's rank whose spread widens deeper into the draft;
ESPN publishes no ADP standard deviation, so that spread is a calibrated heuristic
rather than data.

**Sleeper publishes no ADP.** Every candidate endpoint returns 404 or 500, and its
`search_rank` field is not a draft board - 1,319 players tie at a sentinel value. So the
Sleeper leagues' boards use ESPN's market with Sleeper's format, team count, draft type
and your slot, not Sleeper drafters' actual behaviour.

### How it decides

Players are ranked by **value over replacement**, not by projected points. In a league
that starts one quarterback, the best QB can be worth less than the RB12, because the
quarterback you would otherwise stream is nearly as good while the running back's
alternative is not. Replacement level comes from each league's own roster slots and
team count, with FLEX spots filled from the actual projection pool rather than a
hardcoded split.

That is why the same player is valued differently in each of your leagues. In the
superflex Dynasty Beasts, quarterback replacement level sits at **QB25**; in the
single-quarterback leagues it sits at **QB11** and **QB13**.

Two rules sit on top of raw value:

- **Positional need** breaks near-ties, mildly. It never overrides a clearly better
  player.
- **Must-fill** narrows the board when your remaining picks equal the starting slots
  you still cannot fill. Without it a pure value ranking never takes a kicker or a
  defense - a bench receiver always out-values the best kicker - and the draft ends
  with an illegal lineup.

Every recommendation shows its reasoning, because an assistant that cannot explain
itself is not one to trust while on the clock.

## How it works

Three findings from probing the live APIs shaped the design.

**1. Player identity is the hard part.** Sleeper's player dump has an `espn_id` field
that looks like the obvious way to join the two platforms. It is populated for only
**23% of active fantasy-relevant players**, and the gaps are not just rookies —
Ja'Marr Chase, Bucky Irving, and Brandon Aubrey all come back with `espn_id: None`.
So `resolve/player_matching.py` resolves through a ladder of strategies (espn_id, then
name+position+team, then progressively weaker fallbacks), refuses to guess between
equally plausible candidates, and reports everything it could not resolve on every
sync. It currently resolves **99.7%** of ESPN's top 600 players with zero ambiguous
matches.

**2. Scoring must be per-league.** Rather than trusting either vendor's point totals,
the app pulls **raw stat lines** — ESPN exposes 44 categories per player per week —
and applies each league's own `scoring_settings` to them. That is what makes a
superflex PPR Sleeper league and a standard ESPN league genuinely comparable.

The engine reproduces Sleeper's own scores **exactly**: all 296 skill-position players
in a completed week, across PPR, standard, and half-PPR. Routed through the ESPN stat
bridge, all 217 QB/RB/WR/TE scores match too.

**3. ESPN's undocumented API has traps.** The legacy `fantasy.espn.com/apis/v3` host
now redirects to a marketing page; the documented `leagueHistory` path 404s; ESPN says
`WSH` where Sleeper says `WAS`; ESPN's 60+ yard field goal bucket is *nested* inside
its 50+ bucket, so summing them double-counts long kicks; and before a draft starts
ESPN returns a full placeholder grid (12 teams x 16 rounds = 192 "picks", every one
with `playerId: -1`) that must not be stored as real selections. Each of these is
handled and covered by a regression test.

## Layout

```
backend/
  clients/      thin HTTP clients (sleeper.py, espn.py)
  ingest/       leagues, players, drafts -> database
  resolve/      player_matching.py, stat_map.py (vendored ESPN stat ids)
  scoring/      rules.py — league scoring rules applied to raw stats
  analysis/     draft.py (VORP, need, must-fill), board.py (live state),
                adp_board.py (ADP board, pick math, availability)
  api/routes.py FastAPI, also serves the built frontend
  db.py         SQLite schema
  cli.py        command line entry point
frontend/       React + TypeScript dashboard (Vite)
tests/          regression tests, every case drawn from a real API failure
```

## Roadmap

- [x] **Phase 1** — ingestion, player matching, scoring engine
- [x] **Phase 2** — live draft assistant (best available, VORP, positional need)
- [x] **Draft ADP heat map** — per-league board, list and grid views, slot what-if
- [x] **Phase 3** — unified season dashboard (React)
- [ ] **Phase 4** — start/sit lineup optimizer
- [ ] **Phase 5** — waiver wire and trade analysis
- [ ] **Phase 6** — draft prep cheat sheets and post-draft grading
