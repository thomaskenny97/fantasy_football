import { useEffect, useState } from "react";

// In development Vite serves the UI on :5173 and FastAPI runs on :8000. In
// production FastAPI serves the built bundle itself, so same-origin works.
const API = import.meta.env.DEV ? "http://127.0.0.1:8000" : "";

type Team = {
  id: number;
  name: string;
  owner: string | null;
  isMine: boolean;
  wins: number;
  losses: number;
  ties: number;
  pointsFor: number;
  pointsAgainst: number;
};

type Player = {
  sleeperId: string;
  name: string | null;
  position: string | null;
  proTeam: string | null;
  injuryStatus: string | null;
};

type DraftSummary = {
  id: number;
  status: string | null;
  type: string | null;
  rounds: number | null;
  pickCount: number;
};

type League = {
  id: number;
  platform: string;
  name: string;
  season: string;
  teamCount: number;
  rosterPositions: string[];
  myTeam: Team | null;
  drafts: DraftSummary[];
};

type RosterEntry = {
  slot: string | null;
  isStarter: boolean;
  player: Player | null;
  platformPlayerId: string | null;
};

type Roster = { team: Team; league: { id: number; name: string }; roster: RosterEntry[] };

type Pick = {
  pickNo: number;
  round: number | null;
  team: string;
  isMine: boolean;
  bidAmount: number | null;
  isKeeper: boolean;
  player: Player | null;
};

type DraftBoard = {
  status: string | null;
  type: string | null;
  rounds: number | null;
  picks: Pick[];
};


type Recommendation = {
  sleeperId: string;
  name: string;
  position: string;
  proTeam: string | null;
  points: number;
  vorp: number;
  score: number;
  needTier: string;
  adp: number | null;
  adpDelta: number | null;
  auctionValue: number | null;
  reasons: string[];
};

type NeedRow = { position: string; tier: string; have: number; starters: number };

type DraftState = {
  league: { id: number; name: string; teamCount: number };
  draft: { status: string | null; type: string | null; rounds: number | null } | null;
  currentPick: number;
  round: number;
  picksMade: number;
  picksRemaining: number | null;
  isComplete: boolean;
  poolSize: number;
  totalRounds: number | null;
  availableCount: number;
  myTeam: { id: number; name: string } | null;
  need: NeedRow[];
  recommendations: Recommendation[];
  recentPicks: Pick[];
};

const get = <T,>(path: string): Promise<T> =>
  fetch(`${API}${path}`).then((r) => {
    if (!r.ok) throw new Error(`${r.status} ${path}`);
    return r.json() as Promise<T>;
  });

function PositionChip({ position }: { position: string | null }) {
  const pos = position ?? "--";
  return (
    <span className="chip" data-pos={pos}>
      {pos}
    </span>
  );
}

function PlayerCell({ entry }: { entry: RosterEntry }) {
  const player = entry.player;
  if (!player) {
    return (
      <div className="player">
        <PositionChip position={null} />
        <span className="player-name empty">
          {entry.platformPlayerId ? "Unresolved player" : "Empty"}
        </span>
      </div>
    );
  }
  return (
    <div className="player">
      <PositionChip position={player.position} />
      <span className="player-name">{player.name}</span>
      {player.injuryStatus ? (
        <span className="injury">{player.injuryStatus.slice(0, 3).toUpperCase()}</span>
      ) : null}
    </div>
  );
}

function Lineup({ teamId }: { teamId: number }) {
  const [roster, setRoster] = useState<Roster | null>(null);

  useEffect(() => {
    setRoster(null);
    get<Roster>(`/api/teams/${teamId}/roster`).then(setRoster).catch(() => setRoster(null));
  }, [teamId]);

  if (!roster) return <div className="notice">Loading roster…</div>;

  const starters = roster.roster.filter((e) => e.isStarter);
  const bench = roster.roster.filter((e) => !e.isStarter);

  if (roster.roster.length === 0) {
    return (
      <div className="status-strip">
        <span className="dot" />
        <div className="status-text">
          <strong>No players yet</strong>
          <span>This roster fills in once the draft happens.</span>
        </div>
      </div>
    );
  }

  return (
    <>
      <div className="block">
        <div className="block-head">
          <h2>Starting lineup</h2>
          <span className="count num">{starters.length}</span>
        </div>
        <div className="lineup">
          {starters.map((entry, i) => (
            <div className="slot-row" key={`s${i}`}>
              <span className="slot-tag">{entry.slot ?? "—"}</span>
              <PlayerCell entry={entry} />
              <span className="pro-team">{entry.player?.proTeam ?? ""}</span>
            </div>
          ))}
        </div>
      </div>

      {bench.length > 0 && (
        <div className="block">
          <div className="block-head">
            <h2>Bench</h2>
            <span className="count num">{bench.length}</span>
          </div>
          <div className="lineup">
            {bench.map((entry, i) => (
              <div className="slot-row bench" key={`b${i}`}>
                <span className="slot-tag">{entry.slot ?? "BN"}</span>
                <PlayerCell entry={entry} />
                <span className="pro-team">{entry.player?.proTeam ?? ""}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </>
  );
}

function Standings({ leagueId }: { leagueId: number }) {
  const [teams, setTeams] = useState<Team[] | null>(null);

  useEffect(() => {
    setTeams(null);
    get<Team[]>(`/api/leagues/${leagueId}/teams`).then(setTeams).catch(() => setTeams([]));
  }, [leagueId]);

  if (!teams) return <div className="notice">Loading standings…</div>;

  return (
    <div className="block">
      <div className="block-head">
        <h2>Standings</h2>
        <span className="count num">{teams.length} teams</span>
      </div>
      <table className="standings">
        <thead>
          <tr>
            <th>Team</th>
            <th>W-L</th>
            <th>PF</th>
          </tr>
        </thead>
        <tbody>
          {teams.map((team, i) => (
            <tr key={team.id} className={team.isMine ? "mine" : undefined}>
              <td className="team-name">
                <span className="rank num">{i + 1}</span> {team.name}
              </td>
              <td className="num">
                {team.wins}-{team.losses}
                {team.ties ? `-${team.ties}` : ""}
              </td>
              <td className="num">{team.pointsFor.toFixed(1)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function DraftPanel({ league }: { league: League }) {
  const draft = league.drafts[0];
  const [board, setBoard] = useState<DraftBoard | null>(null);

  useEffect(() => {
    setBoard(null);
    if (draft && draft.pickCount > 0) {
      get<DraftBoard>(`/api/drafts/${draft.id}`).then(setBoard).catch(() => setBoard(null));
    }
  }, [draft?.id, draft?.pickCount]);

  if (!draft) return null;

  // A draft that has not happened is the most useful thing on the page, so it is
  // shown as a live status rather than an empty table.
  if (draft.pickCount === 0) {
    const drafting = draft.status === "drafting";
    return (
      <div className="block">
        <div className="block-head">
          <h2>Draft</h2>
        </div>
        <div className={`status-strip${drafting ? " live" : ""}`}>
          <span className="dot" />
          <div className="status-text">
            <strong>{drafting ? "Draft in progress" : "Not drafted yet"}</strong>
            <span>
              {drafting
                ? "Run sync to pull the latest picks."
                : `${draft.type ?? "snake"} draft · picks appear here once it starts`}
            </span>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="block">
      <div className="block-head">
        <h2>Draft</h2>
        <span className="count num">
          {draft.pickCount} picks · {draft.status}
        </span>
      </div>
      {!board ? (
        <div className="notice">Loading board…</div>
      ) : (
        <div className="lineup">
          {board.picks.slice(0, 24).map((pick) => (
            <div
              className={`pick-row${pick.isMine ? " mine" : ""}`}
              key={pick.pickNo}
            >
              <span className="pick-no">
                {pick.round ?? "?"}.{String(pick.pickNo).padStart(2, "0")}
              </span>
              {pick.player ? (
                <div className="player">
                  <PositionChip position={pick.player.position} />
                  <span className="player-name">{pick.player.name}</span>
                  <span className="pro-team">{pick.player.proTeam ?? ""}</span>
                </div>
              ) : (
                <span className="player-name empty">Unresolved</span>
              )}
              <span className="bid">
                {pick.bidAmount ? `$${pick.bidAmount}` : pick.team}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}


const DRAFT_POLL_MS = 15000;

function DraftAssistant({ leagueId }: { leagueId: number }) {
  const [state, setState] = useState<DraftState | null>(null);
  const [live, setLive] = useState(false);

  useEffect(() => {
    setState(null);
    let cancelled = false;
    let timer: number | undefined;

    // While a draft is in progress the board pulls fresh picks from the platform.
    // Outside a draft it reads the database only, so nothing hits the network.
    const load = (refresh: boolean) => {
      get<DraftState>(
        `/api/leagues/${leagueId}/draft${refresh ? "?refresh=true" : ""}`
      )
        .then((next) => {
          if (cancelled) return;
          setState(next);
          const running = next.draft?.status === "drafting";
          setLive(running);
          if (running) {
            timer = window.setTimeout(() => load(true), DRAFT_POLL_MS);
          }
        })
        .catch(() => {
          if (!cancelled) setState(null);
        });
    };

    load(false);
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [leagueId]);

  if (!state) return <div className="notice">Loading draft board...</div>;

  const status = state.draft?.status ?? "no draft";

  // A finished draft has nothing to advise on; the picks themselves are the story.
  if (state.isComplete) {
    return (
      <>
        <div className="status-strip">
          <span className="dot" />
          <div className="status-text">
            <strong>Draft complete</strong>
            <span>
              {state.picksMade} picks made. Recommendations resume for your next
              draft.
            </span>
          </div>
        </div>
        <div className="block">
          <div className="block-head">
            <h2>Draft board</h2>
            <span className="count num">{state.picksMade} picks</span>
          </div>
          <div className="lineup">
            {state.recentPicks.map((pick) => (
              <div
                className={`pick-row${pick.isMine ? " mine" : ""}`}
                key={pick.pickNo}
              >
                <span className="pick-no">
                  {pick.round ?? "?"}.{String(pick.pickNo).padStart(2, "0")}
                </span>
                {pick.player ? (
                  <div className="player">
                    <PositionChip position={pick.player.position} />
                    <span className="player-name">{pick.player.name}</span>
                  </div>
                ) : (
                  <span className="player-name empty">Unresolved</span>
                )}
                <span className="bid">
                  {pick.bidAmount ? `$${pick.bidAmount}` : pick.team}
                </span>
              </div>
            ))}
          </div>
        </div>
      </>
    );
  }

  return (
    <>
      <div className="clock">
        <div className="stat">
          <span className="eyebrow">On the clock</span>
          <span className="big">
            {state.round}.{String(state.currentPick).padStart(2, "0")}
          </span>
        </div>
        <div className="stat">
          <span className="eyebrow">Picks made</span>
          <span className="val">{state.picksMade}</span>
        </div>
        <div className="stat">
          <span className="eyebrow">Your picks left</span>
          <span className="val">
            {state.picksRemaining ?? "-"}
            {state.totalRounds ? ` / ${state.totalRounds}` : ""}
          </span>
        </div>
        <div className="stat">
          <span className="eyebrow">Available</span>
          <span className="val">{state.availableCount}</span>
        </div>
        <div className="stat">
          <span className="eyebrow">Status</span>
          <span className="val">
            {live ? "live, refreshing" : status}
          </span>
        </div>
      </div>

      <div className="columns">
        <div>
          <div className="block-head">
            <h2>Take next</h2>
            <span className="count num">by value over replacement</span>
          </div>
          {state.recommendations.length === 0 ? (
            <div className="notice">
              No projections yet. Run <code>backend.cli sync</code>.
            </div>
          ) : (
            <div className="lineup">
              {state.recommendations.map((rec, i) => (
                <div className="rec" key={rec.sleeperId}>
                  <span className="rec-index num">{i + 1}</span>
                  <div className="rec-main">
                    <div className="rec-name">
                      <PositionChip position={rec.position} />
                      <span className="player-name">{rec.name}</span>
                      <span className="pro-team">{rec.proTeam ?? ""}</span>
                    </div>
                    <div className="rec-reasons">
                      {rec.reasons.map((reason, r) => (
                        <span key={r}>{reason}</span>
                      ))}
                    </div>
                  </div>
                  <div className="rec-figures">
                    <div className="rec-vorp">+{rec.vorp.toFixed(0)}</div>
                    <div className="rec-sub">
                      {rec.points.toFixed(0)} pts
                      {rec.adpDelta !== null && (
                        <>
                          {" / "}
                          <span
                            className={rec.adpDelta > 0 ? "value-up" : "value-down"}
                          >
                            {rec.adpDelta > 0 ? "+" : ""}
                            {rec.adpDelta.toFixed(0)} adp
                          </span>
                        </>
                      )}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        <div>
          <div className="block">
            <div className="block-head">
              <h2>Roster needs</h2>
              <span className="count num">{state.myTeam?.name ?? "no team"}</span>
            </div>
            <div className="need-strip">
              {state.need.map((n) => (
                <span className="need-pill" data-tier={n.tier} key={n.position}>
                  <PositionChip position={n.position} />
                  {n.have}/{n.starters}
                </span>
              ))}
            </div>
          </div>

          <div className="block">
            <div className="block-head">
              <h2>Recent picks</h2>
              <span className="count num">{state.picksMade}</span>
            </div>
            {state.recentPicks.length === 0 ? (
              <div className="status-strip">
                <span className="dot" />
                <div className="status-text">
                  <strong>No picks yet</strong>
                  <span>Run sync during the draft to pull the board.</span>
                </div>
              </div>
            ) : (
              <div className="lineup">
                {state.recentPicks.map((pick) => (
                  <div
                    className={`pick-row${pick.isMine ? " mine" : ""}`}
                    key={pick.pickNo}
                  >
                    <span className="pick-no">
                      {pick.round ?? "?"}.{String(pick.pickNo).padStart(2, "0")}
                    </span>
                    {pick.player ? (
                      <div className="player">
                        <PositionChip position={pick.player.position} />
                        <span className="player-name">{pick.player.name}</span>
                      </div>
                    ) : (
                      <span className="player-name empty">Unresolved</span>
                    )}
                    <span className="bid">
                      {pick.bidAmount ? `$${pick.bidAmount}` : pick.team}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </>
  );
}

export default function App() {
  const [leagues, setLeagues] = useState<League[] | null>(null);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<"season" | "draft">("season");

  useEffect(() => {
    get<League[]>("/api/leagues")
      .then((data) => {
        setLeagues(data);
        if (data.length) setActiveId(data[0].id);
      })
      .catch((e) => setError(String(e)));
  }, []);

  if (error) {
    return (
      <div className="shell">
        <div className="notice">
          Could not reach the API. Start it with{" "}
          <code>python -m uvicorn backend.api.routes:app --port 8000</code>
        </div>
      </div>
    );
  }

  if (!leagues) return <div className="shell"><div className="notice">Loading…</div></div>;

  if (leagues.length === 0) {
    return (
      <div className="shell">
        <div className="notice">
          No leagues yet. Run <code>python -m backend.cli sync</code> and reload.
        </div>
      </div>
    );
  }

  const active = leagues.find((l) => l.id === activeId) ?? leagues[0];
  const season = active.season;

  return (
    <div className="shell">
      <header className="masthead">
        <h1>Fantasy</h1>
        <span className="eyebrow">{season} season</span>
        <div className="view-toggle">
          <button
            aria-pressed={view === "season"}
            onClick={() => setView("season")}
          >
            Season
          </button>
          <button
            aria-pressed={view === "draft"}
            onClick={() => setView("draft")}
          >
            Draft
          </button>
        </div>
        <span className="spacer" />
        <span className="meta num">
          {leagues.length} leagues · {leagues.filter((l) => l.myTeam).length} teams
        </span>
      </header>

      <nav className="tabs" role="tablist" aria-label="Leagues">
        {leagues.map((league) => (
          <button
            key={league.id}
            role="tab"
            className="tab"
            aria-selected={league.id === active.id}
            onClick={() => setActiveId(league.id)}
          >
            <span className="tab-name">{league.name}</span>
            <span className="tab-sub">
              {league.platform} · {league.myTeam?.name ?? "no team"}
            </span>
          </button>
        ))}
      </nav>

      <main className="panel">
        {view === "draft" ? (
          <DraftAssistant leagueId={active.id} />
        ) : (
        <div className="columns">
          <div>
            {active.myTeam ? (
              <>
                <div className="block-head">
                  <h2>{active.myTeam.name}</h2>
                  <span className="count record">
                    {active.myTeam.wins}-{active.myTeam.losses}
                    {active.myTeam.ties ? `-${active.myTeam.ties}` : ""} ·{" "}
                    {active.myTeam.pointsFor.toFixed(1)} PF
                  </span>
                </div>
                <Lineup teamId={active.myTeam.id} />
              </>
            ) : (
              <div className="status-strip">
                <span className="dot" />
                <div className="status-text">
                  <strong>Your team is not identified</strong>
                  <span>
                    Set ESPN_TEAM_ID in .env, then run sync. Use{" "}
                    <code>backend.cli teams</code> to find the id.
                  </span>
                </div>
              </div>
            )}
          </div>

          <div>
            <Standings leagueId={active.id} />
            <DraftPanel league={active} />
          </div>
        </div>
        )}
      </main>
    </div>
  );
}
