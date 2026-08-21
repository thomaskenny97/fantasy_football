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
be there at each of your picks? Three views, all per league:

- **List** — one block per round showing the players realistically available at that
  pick, each with a probability that they last that long, plus a positional summary.
- **Grid** — the full board, rounds x slots, with your snake path highlighted.
- **Players** — every player in board order with a rule at each round boundary, each row
  lit by how well it lines up with a pick you actually own.

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

### Reading the Players view

A row glows when its board position sits on one of your picks: take the player earlier
and you reached, wait for your next pick and they are gone. Rows recede as they drift
away from anything you own.

That is what exposes a seat's **dead zone**. At slot 12 of a 12-team snake you pick at
12, 13, 36 and 37, so players around board position 22-24 line up with nothing - a reach
at 13, long gone by 36:

```
  12  James Cook        RB   ON YOUR PICK 12     <- your pick
  13  Ashton Jeanty     RB   ON YOUR PICK 13     <- your pick
  17  Trey McBride      TE   REACHABLE    13
  19  Derrick Henry     RB   LONG SHOT    13
  22  Omarion Hampton   RB   DEAD ZONE
  23  Breece Hall       RB   DEAD ZONE
  36  Josh Allen        QB   ON YOUR PICK 36     <- your pick
```

Move the slot slider and the whole pattern shifts, because a different seat owns
different picks. On a **linear** draft slot 12 owns 12, 24, 36 and 48, so every twelfth
player lights up and there is no dead zone at all.

Positions are scored by where a player sits on *this league's* board, not by their global
ESPN rank. The two are the same in a fresh redraft league and diverge completely in a
dynasty league, where the best available free agent may be ranked 150th overall yet go
first in this draft.

## Your own rankings

The **Rankings** tab is your board, not the market's. Drag a row to move a player, or
use the arrows on each row - the arrows exist because native drag and drop is not
keyboard accessible, and a ranking you cannot edit without a mouse is a ranking you will
not maintain.

Each row shows where the market has the player and how far you disagree:

```
  1  CeeDee Lamb        WR   mkt 9 · adp 12 · +106   +8
  2  Jahmyr Gibbs       RB   mkt 1 · adp 2  · +179   -1
```

Nothing is written until you press Save, and Reset drops your list and falls back to the
market board. A league you have never touched simply follows the market, so there is no
setup step.

Rankings are **per league**, because a board is per league - a superflex ranking and a
PPR ranking are different lists, and one global order would be wrong for most of your
leagues at once. Editing one league never touches another.

Once saved, your rank appears as a `YOU n` tag beside the market rank everywhere it
matters: the Board list, the Players list, and the live draft assistant's
recommendations. That is the point - seeing your number next to consensus is what tells
you whether a player at their ADP is a bargain or a trap.

## Positional dropoff curves

The **Graphs** tab plots projected points against rank within a position, one line for
QB, RB, WR and TE on a single shared scale. It is the picture behind value over
replacement: what a pick buys is not a player's points but the gap to the next one at
his position, and that gap is a slope.

For NYC Fellas the first twelve at each position fall like this:

```
  QB   #1 369.7   #12 288.3    drop  81
  RB   #1 364.9   #12 273.6    drop  91
  WR   #1 356.3   #12 248.6    drop 108
  TE   #1 241.9   #12 169.3    drop  73
```

A ring on each line marks where that position crosses **replacement level** - past it,
another one of them barely improves your lineup. Hovering gives a crosshair and the
player at that rank in every position at once, and a Table toggle gives the same numbers
without relying on colour.

Kickers and defenses are left off deliberately: both are nearly flat and only compress
the vertical scale for the four positions a draft is actually decided on.

### Two simulation studies

Below the curves sit two bar charts, each backed by hundreds of simulated drafts. You
choose how many drafts to run; the result is **saved server-side**, so reopening the page
shows the last answer and when it was generated rather than a blank panel and a wait.

**Value by draft slot** runs every seat with the strategy held constant, so the only
thing changing is where you sit. **Value by opening strategy** runs every strategy from
your own slot, so the seat is not a confound. Each isolates one variable on purpose.

Both carry **one standard error per bar**. Simulation means without their spread invite
over-reading, and with enough drafts the error bar is what says whether two bars actually
differ. The axis does not start at zero - these differences are a few percent and would
be invisible if it did, so the chart says so in as many words.

The bars are one series with emphasis rather than a categorical palette: every bar is the
same desaturated steel and the one that matters is picked out in the accent used for
"yours" throughout the app. Colouring bars by value would double-encode what bar length
already says.

### A note on the colours

The positional palette was **measured, not chosen**. The original set failed a
colourblind-safety check badly - the old WR blue and TE violet were 1.4 apart under
deuteranopia and 14.6 for normal vision, below the readable floor. On a chip carrying a
text label that is survivable; on four overlapping lines it is not.

The current six were searched for and validated against the chart surface, checking
every pair rather than only neighbours: worst deuteranope separation 11.0, worst
normal-vision separation 15.6, all inside the dark lightness band, all clearing 3:1
contrast. Chip text colour is set per position from measured contrast - K and DEF take
light text, the rest take dark.

## Mock drafts

The **Mock** tab runs the draft rather than describing it. Two modes.

**Quick sim** drafts a whole board and hands back the finished team - every starter and
every bench player, each labelled with the round it was taken in. Pick which strategies
to try and whether to run from your slot or all of them, press Generate, and compare the
rosters side by side. A full 12-slot batch takes well under a second.

Each result is one team lifted out of a draft that really happened, so **Full draft
board** puts it back: all twelve rosters, sixteen rounds, your column highlighted, the
rivals carrying the varied strategies they actually drafted with. Comparing every slot
at once leaves the field out, because that question is about slots and carrying eleven
extra teams per result would be twelve times the payload for it.

```
  Balanced       Jonathan Taylor(RB) + Drake London(WR)     starters 1972
  RB-RB start    De'Von Achane(RB)   + Jeremiyah Love(RB)   starters 2074
  WR-WR start    Drake London(WR)    + Rashee Rice(WR)      starters 1980
  RB-WR start    James Cook(RB)      + Drake London(WR)     starters 1997
```

**Draft vs computers** puts you on the clock. Eleven bots draft around you, you take
whoever you like, and they run forward to your next pick. The server keeps no session
state - the page holds the pick list and sends it back - so a mock survives a reload.

### How the field behaves

Each player's draft position is drawn **once per simulation**, not once per pick. A real
draft has a shape: this is the year a player slid, this is the year he did not.
Re-rolling at every pick would average those away into a draft that never happens, so
each run fixes one plausible ordering and plays it forward. That is why two runs of the
same strategy come back different.

Rivals draft the market board with noise and are handed varied strategies of their own,
so the field is not twelve copies of the same team. Your team drafts **your rankings**
with noticeably less noise - your opinions should mostly win, while still leaving room
to be surprised. Set nothing in Rankings and your picks simply follow the market.

How much noise is tuned by measurement, not taste. The first version let **18.6% of
draftable players fall a full round or more**, so a third-rounder routinely lasted into
the fourth and every simulated roster came out better than a real one. Tightening the
spread to 0.07 of board position brings that to **3.2%**, with a median slide of three
picks - rare enough to be a break, common enough to still be a draft. A regression test
holds the line.

Every simulated team obeys the same must-fill rule as the live assistant, so rosters
finish able to field a legal lineup rather than ending up without a kicker.

### What it cannot tell you

Run-to-run variance is comparable to the difference between draft slots, so a handful of
runs will not establish that one seat is better than another - across three runs a
single slot swung 135 points while the spread between slots was about the same. Use it
to compare **roster shapes** and see what a strategy actually produces, not to rank
slots. Turn the runs up if you want a stable average.

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
                adp_board.py (ADP board, pick math, availability),
                rankings.py (personal rankings), mock.py (mock drafts),
                curves.py (positional dropoff)
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
- [x] **Personal rankings** — drag-and-drop, per league, surfaced across the other views
- [x] **Mock drafts** — stochastic batch sim by strategy and slot, plus a live mock vs bots
- [x] **Positional dropoff curves** — projected points by positional rank, four series
- [x] **Simulation studies** — value by draft slot and by opening strategy, cached per league
- [x] **Phase 3** — unified season dashboard (React)
- [ ] **Phase 4** — start/sit lineup optimizer
- [ ] **Phase 5** — waiver wire and trade analysis
- [ ] **Phase 6** — draft prep cheat sheets and post-draft grading
