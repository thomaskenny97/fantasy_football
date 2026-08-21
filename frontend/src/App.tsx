import { useCallback, useEffect, useRef, useState } from "react";

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
  myRank?: number | null;
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


type BoardPlayer = {
  sleeperId: string;
  name: string;
  position: string;
  proTeam: string | null;
  boardRank: number;
  points: number;
  vorp: number;
  adp: number | null;
  availability?: number;
  band?: string;
  myRank?: number | null;
  myDelta?: number | null;
};

type RoundTarget = {
  round: number;
  pick: number;
  slotInRound: number;
  targets: BoardPlayer[];
  positionCounts: { position: string; count: number }[];
};

type GridCell = {
  pick: number;
  slot: number;
  isMine: boolean;
  player: BoardPlayer | null;
};

type ListPlayer = BoardPlayer & {
  boardSlot: number;
  round: number;
  targetScore: number;
  targetBand: string;
  bestPick: number | null;
  availabilityAtBestPick: number | null;
  onMyPick: boolean;
};

type RankedPlayer = {
  sleeperId: string;
  name: string;
  position: string;
  proTeam: string | null;
  myRank: number;
  boardRank: number;
  marketSlot: number;
  delta: number;
  points: number;
  vorp: number;
  adp: number | null;
  targetScore: number;
  targetBand: string;
  bestPick: number | null;
  onMyPick: boolean;
};

type RankingBoard = {
  league: { id: number; name: string };
  isCustom: boolean;
  count: number;
  players: RankedPlayer[];
};

type AdpBoard = {
  league: { id: number; name: string; teamCount: number };
  boardType: string;
  draftType: string;
  rounds: number;
  slot: number;
  detectedSlot: number | null;
  myPicks: number[];
  boardSize: number;
  roundTargets: RoundTarget[];
  grid: { round: number; cells: GridCell[] }[];
  players: ListPlayer[];
  hasCustomRanks: boolean;
};


type Strategy = { key: string; label: string };

type LineupRow = {
  slot: string;
  player: { name: string; position: string; proTeam: string | null; points: number } | null;
};

type MockResult = {
  slot: number;
  isMine: boolean;
  strategy: string;
  strategyLabel: string;
  starterPoints: number;
  lineup: LineupRow[];
  positionCounts: { position: string; count: number }[];
  picks: {
    pickNo: number;
    round: number;
    name: string;
    position: string;
    proTeam: string | null;
    points: number;
    boardSlot: number;
    myRank: number | null;
  }[];
  run: number;
  seed: number;
};

type MockBatch = {
  league: { id: number; name: string; teamCount: number; rounds: number; draftType: string };
  usingMyRanks: boolean;
  strategies: Strategy[];
  results: MockResult[];
};

type LivePick = {
  pickNo: number;
  round: number;
  slot: number;
  sleeperId: string;
  name: string;
  position: string;
  proTeam: string | null;
  points: number;
  boardSlot: number;
  myRank: number | null;
  isMine: boolean;
};

type LiveAvailable = {
  sleeperId: string;
  name: string;
  position: string;
  proTeam: string | null;
  boardSlot: number;
  myRank: number | null;
  points: number;
  vorp: number;
};

type LiveMock = {
  league: { id: number; name: string; teamCount: number; rounds: number };
  mySlot: number;
  seed: number;
  complete: boolean;
  onTheClock: number | null;
  round: number;
  picks: LivePick[];
  myPicks: LivePick[];
  myLineup: LineupRow[];
  myStarterPoints: number;
  recentPicks: LivePick[];
  available: LiveAvailable[];
  myPickNumbers: number[];
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
                      <MyRankTag rank={rec.myRank} />
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


const BAND_LABEL: Record<string, string> = {
  likely: "LIKELY",
  probable: "PROBABLE",
  coinflip: "COIN FLIP",
  unlikely: "UNLIKELY",
  gone: "GONE",
};


const TARGET_LABEL: Record<string, string> = {
  prime: "ON YOUR PICK",
  good: "REACHABLE",
  fringe: "LONG SHOT",
  dead: "DEAD ZONE",
};

function AllPlayersList({ board }: { board: AdpBoard }) {
  const teamCount = board.league.teamCount;

  // Group into rounds so a rule can be drawn at each boundary, the way a real
  // board reads.
  const rounds: { round: number; players: ListPlayer[] }[] = [];
  for (const player of board.players) {
    const last = rounds[rounds.length - 1];
    if (!last || last.round !== player.round) {
      rounds.push({ round: player.round, players: [player] });
    } else {
      last.players.push(player);
    }
  }

  return (
    <>
      <div className="players-list">
        {rounds.map((row) => {
          const mine = board.myPicks.filter(
            (pick) =>
              pick > (row.round - 1) * teamCount && pick <= row.round * teamCount
          );
          return (
            <div key={row.round}>
              <div className="round-rule">
                <span>Round {row.round}</span>
                <span>
                  picks {(row.round - 1) * teamCount + 1}&ndash;
                  {row.round * teamCount}
                </span>
                <span className="picks">
                  {mine.length ? (
                    <>
                      yours: <b>{mine.join(", ")}</b>
                    </>
                  ) : (
                    "no pick of yours"
                  )}
                </span>
              </div>
              {row.players.map((player) => (
                <div
                  className={`prow${player.onMyPick ? " on-pick" : ""}`}
                  data-target={player.targetBand}
                  key={player.sleeperId}
                >
                  <span className="bslot">{player.boardSlot}</span>
                  <div className="player">
                    <PositionChip position={player.position} />
                    <span className="player-name">{player.name}</span>
                    <span className="pro-team">{player.proTeam ?? ""}</span>
                    <MyRankTag rank={player.myRank} />
                  </div>
                  <span className="pfigs">
                    rank <b>{player.boardRank.toFixed(0)}</b>
                    {player.adp ? ` · adp ${player.adp.toFixed(0)}` : ""} &middot; +
                    {player.vorp.toFixed(0)} vorp
                  </span>
                  <span className="ptarget">
                    {player.bestPick && player.targetBand !== "dead"
                      ? `${TARGET_LABEL[player.targetBand]} ${player.bestPick}`
                      : TARGET_LABEL[player.targetBand]}
                  </span>
                </div>
              ))}
            </div>
          );
        })}
      </div>

      <div className="list-legend">
        <span>
          <span
            className="chipbox"
            style={{
              background: "rgba(53,201,141,0.13)",
              borderLeftColor: "var(--live)",
            }}
          />
          on a pick you own
        </span>
        <span>
          <span
            className="chipbox"
            style={{
              background: "rgba(53,201,141,0.05)",
              borderLeftColor: "rgba(53,201,141,0.4)",
            }}
          />
          reachable
        </span>
        <span>
          <span className="chipbox" style={{ background: "transparent" }} />
          long shot
        </span>
        <span style={{ opacity: 0.42 }}>
          <span className="chipbox" style={{ background: "transparent" }} />
          dead zone &mdash; gone before your next pick, a reach at your last
        </span>
        <span style={{ marginLeft: "auto" }}>
          your picks: {board.myPicks.slice(0, 8).join(", ")}
          {board.myPicks.length > 8 ? "..." : ""}
        </span>
      </div>
    </>
  );
}


function MyRankTag({ rank }: { rank?: number | null }) {
  if (rank === null || rank === undefined) return null;
  return <span className="myrank-tag">YOU {rank}</span>;
}

const send = <T,>(path: string, method: string, body?: unknown): Promise<T> =>
  fetch(`${API}${path}`, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  }).then((r) => {
    if (!r.ok) throw new Error(`${r.status} ${path}`);
    return r.json() as Promise<T>;
  });

const PAGE = 150;
const AUTOSAVE_MS = 700;

function RankingsView({ leagueId }: { leagueId: number }) {
  const [players, setPlayers] = useState<RankedPlayer[] | null>(null);
  const [isCustom, setIsCustom] = useState(false);
  const [status, setStatus] = useState<"clean" | "pending" | "saving" | "saved">(
    "clean"
  );
  const [visible, setVisible] = useState(PAGE);
  const [dragFrom, setDragFrom] = useState<number | null>(null);
  const [dragOver, setDragOver] = useState<number | null>(null);

  // Holds the order that still needs writing. Kept in a ref so the unmount
  // cleanup can flush it without going stale.
  const pending = useRef<string[] | null>(null);
  const leagueRef = useRef(leagueId);

  useEffect(() => {
    leagueRef.current = leagueId;
    setPlayers(null);
    setStatus("clean");
    setVisible(PAGE);
    pending.current = null;
    get<RankingBoard>(`/api/leagues/${leagueId}/rankings`)
      .then((data) => {
        setPlayers(data.players);
        setIsCustom(data.isCustom);
      })
      .catch(() => setPlayers(null));
  }, [leagueId]);

  const persist = useCallback((order: string[], league: number) => {
    setStatus("saving");
    return send<{ saved: number }>(`/api/leagues/${league}/rankings`, "PUT", {
      order,
    })
      .then(() => {
        pending.current = null;
        setIsCustom(true);
        setStatus("saved");
      })
      .catch(() => setStatus("pending"));
  }, []);

  // Autosave shortly after the last change. There is no Save button to forget,
  // which is what previously made a reorder look like it had not stuck.
  useEffect(() => {
    if (!players || pending.current === null) return;
    const order = pending.current;
    const league = leagueRef.current;
    const timer = window.setTimeout(() => persist(order, league), AUTOSAVE_MS);
    return () => window.clearTimeout(timer);
  }, [players, persist]);

  // Leaving the tab, switching league, or closing the page must not drop a
  // change that is still inside the debounce window. keepalive lets the request
  // outlive the component.
  useEffect(() => {
    // keepalive lets the request outlive the page or the component. sendBeacon
    // cannot be used here because it only issues POST, and this is a PUT.
    const flush = () => {
      if (!pending.current) return;
      fetch(`${API}/api/leagues/${leagueRef.current}/rankings`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ order: pending.current }),
        keepalive: true,
      }).catch(() => undefined);
    };
    window.addEventListener("beforeunload", flush);
    return () => {
      window.removeEventListener("beforeunload", flush);
      flush();
    };
  }, []);

  if (!players) return <div className="notice">Loading rankings...</div>;

  // Reordering is local first, so dragging stays instant on a 500-row list.
  const move = (from: number, to: number) => {
    if (from === to || to < 0 || to >= players.length) return;
    const next = players.slice();
    const [row] = next.splice(from, 1);
    next.splice(to, 0, row);
    const renumbered = next.map((p, i) => ({
      ...p,
      myRank: i + 1,
      delta: p.marketSlot - (i + 1),
    }));
    pending.current = renumbered.map((p) => p.sleeperId);
    setPlayers(renumbered);
    setStatus("pending");
  };

  const saveNow = () => {
    if (pending.current) persist(pending.current, leagueRef.current);
  };

  const reset = () => {
    setStatus("saving");
    pending.current = null;
    send(`/api/leagues/${leagueId}/rankings/reset`, "POST")
      .then(() => get<RankingBoard>(`/api/leagues/${leagueId}/rankings`))
      .then((data) => {
        setPlayers(data.players);
        setIsCustom(false);
        setStatus("clean");
      })
      .catch(() => setStatus("pending"));
  };

  const statusText =
    status === "saving"
      ? "saving..."
      : status === "pending"
      ? "unsaved - saving shortly"
      : status === "saved"
      ? "saved automatically"
      : isCustom
      ? "saved - your own order"
      : "following the market board";

  const shown = players.slice(0, visible);

  return (
    <>
      <div className="rank-bar">
        <span className="eyebrow">Your rankings</span>
        <span
          className={`state${status === "pending" ? " dirty" : ""}${
            status === "saved" ? " ok" : ""
          }`}
        >
          {statusText}
        </span>
        <button
          className="btn"
          onClick={saveNow}
          disabled={status !== "pending"}
        >
          Save now
        </button>
        <button
          className="btn"
          onClick={reset}
          disabled={status === "saving" || !isCustom}
        >
          Reset to market
        </button>
        <span className="board-meta">
          {players.length} players · changes save on their own
        </span>
      </div>

      <div className="rank-list">
        {shown.map((player, index) => (
          <div
            key={player.sleeperId}
            className={`rrow${dragFrom === index ? " dragging" : ""}${
              dragOver === index && dragFrom !== null && dragFrom > index
                ? " drop-above"
                : ""
            }${
              dragOver === index && dragFrom !== null && dragFrom < index
                ? " drop-below"
                : ""
            }`}
            data-target={player.targetBand}
            draggable
            onDragStart={() => setDragFrom(index)}
            onDragEnter={() => setDragOver(index)}
            onDragOver={(e) => e.preventDefault()}
            onDragEnd={() => {
              setDragFrom(null);
              setDragOver(null);
            }}
            onDrop={(e) => {
              e.preventDefault();
              if (dragFrom !== null) move(dragFrom, index);
              setDragFrom(null);
              setDragOver(null);
            }}
          >
            <span className="grip" aria-hidden="true">
              ⠿
            </span>
            <span className="myrank">{player.myRank}</span>
            <div className="player">
              <PositionChip position={player.position} />
              <span className="player-name">{player.name}</span>
              <span className="pro-team">{player.proTeam ?? ""}</span>
            </div>
            <span className="market">
              mkt {player.marketSlot}
              {player.adp ? ` · adp ${player.adp.toFixed(0)}` : ""} · +
              {player.vorp.toFixed(0)}
            </span>
            <span
              className={`delta ${
                player.delta > 0 ? "up" : player.delta < 0 ? "down" : "same"
              }`}
            >
              {player.delta === 0
                ? "—"
                : `${player.delta > 0 ? "+" : ""}${player.delta}`}
            </span>
            <span className="nudge">
              <button
                onClick={() => move(index, index - 1)}
                disabled={index === 0}
                aria-label={`Move ${player.name} up`}
              >
                ▲
              </button>
              <button
                onClick={() => move(index, index + 1)}
                disabled={index === players.length - 1}
                aria-label={`Move ${player.name} down`}
              >
                ▼
              </button>
            </span>
          </div>
        ))}
      </div>

      {visible < players.length && (
        <div className="show-more">
          <button className="btn" onClick={() => setVisible(visible + PAGE)}>
            Show {Math.min(PAGE, players.length - visible)} more
          </button>
        </div>
      )}
    </>
  );
}


const ALL_STRATEGIES: Strategy[] = [
  { key: "balanced", label: "Balanced" },
  { key: "rb_rb", label: "RB-RB start" },
  { key: "wr_wr", label: "WR-WR start" },
  { key: "rb_wr", label: "RB-WR start" },
];

function LineupCard({
  result,
  best,
}: {
  result: MockResult;
  best: boolean;
}) {
  return (
    <div className={`mock-card${best ? " best" : ""}`}>
      <header>
        <span className="slotno">slot {result.slot}</span>
        <span className="strat">{result.strategyLabel}</span>
        <span className="pts">{result.starterPoints.toFixed(0)}</span>
      </header>
      {result.lineup.map((row, i) => (
        <div className="mock-row" key={`${row.slot}-${i}`}>
          <span className="mslot">{row.slot}</span>
          {row.player ? (
            <>
              <span className="mname">
                <PositionChip position={row.player.position} /> {row.player.name}
              </span>
              <span className="mpts">{row.player.points.toFixed(0)}</span>
            </>
          ) : (
            <>
              <span className="mname player-name empty">unfilled</span>
              <span className="mpts">—</span>
            </>
          )}
        </div>
      ))}
    </div>
  );
}

function QuickSim({ leagueId, teamCount }: { leagueId: number; teamCount: number }) {
  const [strategies, setStrategies] = useState<string[]>(
    ALL_STRATEGIES.map((s) => s.key)
  );
  const [slotMode, setSlotMode] = useState<"mine" | "all">("mine");
  const [mySlot, setMySlot] = useState(1);
  const [runs, setRuns] = useState(2);
  const [batch, setBatch] = useState<MockBatch | null>(null);
  const [running, setRunning] = useState(false);

  useEffect(() => {
    get<AdpBoard>(`/api/leagues/${leagueId}/adp-board?per_round=1`)
      .then((b) => setMySlot(b.detectedSlot ?? b.slot))
      .catch(() => undefined);
    setBatch(null);
  }, [leagueId]);

  const run = () => {
    setRunning(true);
    send<MockBatch>(`/api/leagues/${leagueId}/mock/simulate`, "POST", {
      slots: slotMode === "mine" ? [mySlot] : null,
      strategies,
      runsPerCombo: runs,
    })
      .then(setBatch)
      .finally(() => setRunning(false));
  };

  const toggle = (key: string) =>
    setStrategies((current) =>
      current.includes(key)
        ? current.filter((k) => k !== key)
        : [...current, key]
    );

  const best = batch
    ? Math.max(...batch.results.map((r) => r.starterPoints))
    : 0;

  return (
    <>
      <div className="mock-controls">
        <div className="group">
          <label>Strategies</label>
          <div className="chips">
            {ALL_STRATEGIES.map((s) => (
              <button
                key={s.key}
                className="chip-toggle"
                aria-pressed={strategies.includes(s.key)}
                onClick={() => toggle(s.key)}
              >
                {s.label}
              </button>
            ))}
          </div>
        </div>

        <div className="group">
          <label>From</label>
          <div className="chips">
            <button
              className="chip-toggle"
              aria-pressed={slotMode === "mine"}
              onClick={() => setSlotMode("mine")}
            >
              my slot ({mySlot})
            </button>
            <button
              className="chip-toggle"
              aria-pressed={slotMode === "all"}
              onClick={() => setSlotMode("all")}
            >
              every slot (1-{teamCount})
            </button>
          </div>
        </div>

        <div className="group">
          <label>Runs each</label>
          <input
            type="number"
            min={1}
            max={5}
            value={runs}
            onChange={(e) => setRuns(Number(e.target.value))}
          />
        </div>

        <div className="group">
          <label>&nbsp;</label>
          <button
            className="btn primary"
            onClick={run}
            disabled={running || strategies.length === 0}
          >
            {running ? "Drafting..." : "Generate"}
          </button>
        </div>

        {batch && (
          <div className="board-meta">
            {batch.results.length} rosters
            <br />
            {batch.usingMyRanks
              ? "your rankings drive your picks"
              : "no personal rankings yet — using the market board"}
          </div>
        )}
      </div>

      {!batch ? (
        <div className="notice">
          Pick your strategies and press Generate. Each run drafts a full board, so
          two runs of the same strategy will not look the same.
        </div>
      ) : (
        <div className="mock-grid">
          {batch.results.map((r, i) => (
            <LineupCard
              key={`${r.slot}-${r.strategy}-${r.run}-${i}`}
              result={r}
              best={r.starterPoints === best}
            />
          ))}
        </div>
      )}
    </>
  );
}

function LiveMockView({ leagueId, teamCount }: { leagueId: number; teamCount: number }) {
  const [state, setState] = useState<LiveMock | null>(null);
  const [slot, setSlot] = useState(1);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    get<AdpBoard>(`/api/leagues/${leagueId}/adp-board?per_round=1`)
      .then((b) => setSlot(b.detectedSlot ?? b.slot))
      .catch(() => undefined);
    setState(null);
  }, [leagueId]);

  const start = () => {
    setBusy(true);
    send<LiveMock>(`/api/leagues/${leagueId}/mock/advance`, "POST", {
      mySlot: slot,
      seed: Math.floor(Math.random() * 1e9),
      picks: [],
    })
      .then(setState)
      .finally(() => setBusy(false));
  };

  const take = (sleeperId: string) => {
    if (!state) return;
    setBusy(true);
    send<LiveMock>(`/api/leagues/${leagueId}/mock/advance`, "POST", {
      mySlot: state.mySlot,
      seed: state.seed,
      picks: state.picks,
      myPickId: sleeperId,
    })
      .then(setState)
      .finally(() => setBusy(false));
  };

  if (!state) {
    return (
      <>
        <div className="mock-controls">
          <div className="group">
            <label>Your slot</label>
            <input
              type="number"
              min={1}
              max={teamCount}
              value={slot}
              onChange={(e) => setSlot(Number(e.target.value))}
            />
          </div>
          <div className="group">
            <label>&nbsp;</label>
            <button className="btn primary" onClick={start} disabled={busy}>
              {busy ? "Starting..." : "Start mock"}
            </button>
          </div>
          <div className="board-meta">
            Eleven computers draft the market board with noise.
            <br />
            You are on the clock at your own picks.
          </div>
        </div>
        <div className="notice">
          Start a mock to draft against the field.
        </div>
      </>
    );
  }

  return (
    <>
      <div className="clock">
        <div className="stat">
          <span className="eyebrow">
            {state.complete ? "Draft complete" : "On the clock"}
          </span>
          <span className="big">
            {state.complete
              ? "done"
              : `${state.round}.${String(state.onTheClock ?? 0).padStart(2, "0")}`}
          </span>
        </div>
        <div className="stat">
          <span className="eyebrow">Your slot</span>
          <span className="val">
            {state.mySlot} / {teamCount}
          </span>
        </div>
        <div className="stat">
          <span className="eyebrow">Picks made</span>
          <span className="val">{state.picks.length}</span>
        </div>
        <div className="stat">
          <span className="eyebrow">Your starters</span>
          <span className="val">{state.myStarterPoints.toFixed(0)}</span>
        </div>
        <div className="stat">
          <span className="eyebrow">&nbsp;</span>
          <button className="btn" onClick={start} disabled={busy}>
            Restart
          </button>
        </div>
      </div>

      <div className="live-cols">
        <div>
          <div className="block-head">
            <h2>{state.complete ? "Board" : "Take a player"}</h2>
            <span className="count num">{state.available.length} shown</span>
          </div>
          <div className="players-list">
            {state.available.map((p) => (
              <button
                className="pickable"
                key={p.sleeperId}
                onClick={() => take(p.sleeperId)}
                disabled={busy || state.complete}
              >
                <span className="rk">{p.myRank ?? p.boardSlot}</span>
                <span className="player">
                  <PositionChip position={p.position} />
                  <span className="player-name">{p.name}</span>
                  <span className="pro-team">{p.proTeam ?? ""}</span>
                </span>
                <span className="fig">mkt {p.boardSlot}</span>
                <span className="fig">+{p.vorp.toFixed(0)}</span>
              </button>
            ))}
          </div>
        </div>

        <div>
          <div className="block">
            <div className="block-head">
              <h2>Your team</h2>
              <span className="count num">{state.myPicks.length}</span>
            </div>
            <div className="lineup">
              {state.myLineup.map((row, i) => (
                <div className="slot-row" key={`${row.slot}-${i}`}>
                  <span className="slot-tag">{row.slot}</span>
                  {row.player ? (
                    <div className="player">
                      <PositionChip position={row.player.position} />
                      <span className="player-name">{row.player.name}</span>
                    </div>
                  ) : (
                    <span className="player-name empty">unfilled</span>
                  )}
                  <span className="pro-team">
                    {row.player ? row.player.points.toFixed(0) : ""}
                  </span>
                </div>
              ))}
            </div>
          </div>

          <div className="block">
            <div className="block-head">
              <h2>Recent picks</h2>
            </div>
            <div className="pick-feed">
              {state.recentPicks.map((p) => (
                <div
                  className={`feed-row${p.isMine ? " mine" : ""}`}
                  key={p.pickNo}
                >
                  <span className="fno">
                    {p.round}.{String(p.pickNo).padStart(2, "0")}
                  </span>
                  <span>
                    <PositionChip position={p.position} /> {p.name}
                    {p.isMine ? " (you)" : ""}
                  </span>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </>
  );
}

function MockView({ leagueId, teamCount }: { leagueId: number; teamCount: number }) {
  const [mode, setMode] = useState<"quick" | "live">("quick");
  return (
    <>
      <div className="view-toggle" style={{ marginLeft: 0, marginBottom: 16 }}>
        <button aria-pressed={mode === "quick"} onClick={() => setMode("quick")}>
          Quick sim
        </button>
        <button aria-pressed={mode === "live"} onClick={() => setMode("live")}>
          Draft vs computers
        </button>
      </div>
      {mode === "quick" ? (
        <QuickSim leagueId={leagueId} teamCount={teamCount} />
      ) : (
        <LiveMockView leagueId={leagueId} teamCount={teamCount} />
      )}
    </>
  );
}

function AdpBoardView({ leagueId }: { leagueId: number }) {
  const [board, setBoard] = useState<AdpBoard | null>(null);
  const [slot, setSlot] = useState<number | null>(null);
  const [mode, setMode] = useState<"list" | "grid" | "players">("list");

  // slot === null means "use whatever the platform says"; once the user drags the
  // control it pins to their choice so they can compare positions.
  useEffect(() => {
    setBoard(null);
    setSlot(null);
  }, [leagueId]);

  useEffect(() => {
    const query = slot === null ? "" : `?slot=${slot}`;
    get<AdpBoard>(`/api/leagues/${leagueId}/adp-board${query}`)
      .then(setBoard)
      .catch(() => setBoard(null));
  }, [leagueId, slot]);

  if (!board) return <div className="notice">Building board...</div>;

  const teamCount = board.league.teamCount;

  return (
    <>
      <div className="board-controls">
        <div className="field">
          <label htmlFor="slot">Draft slot</label>
          <input
            id="slot"
            type="range"
            min={1}
            max={teamCount}
            value={board.slot}
            onChange={(e) => setSlot(Number(e.target.value))}
          />
          <span className="slot-value">
            {board.slot}
            <span className="of"> / {teamCount}</span>
          </span>
        </div>

        <div className="view-toggle">
          <button aria-pressed={mode === "list"} onClick={() => setMode("list")}>
            List
          </button>
          <button aria-pressed={mode === "grid"} onClick={() => setMode("grid")}>
            Grid
          </button>
          <button
            aria-pressed={mode === "players"}
            onClick={() => setMode("players")}
          >
            Players
          </button>
        </div>

        <div className="board-meta">
          {board.boardType} board &middot; {board.draftType} &middot; {teamCount} teams
          <br />
          {board.boardSize} players
          {board.detectedSlot
            ? ` · your slot is ${board.detectedSlot}`
            : " · slot not published yet"}
        </div>
      </div>

      {mode === "players" ? (
        <AllPlayersList board={board} />
      ) : mode === "list" ? (
        <div>
          {board.roundTargets.map((round) => (
            <div className="round-block" key={round.round}>
              <div className="round-head">
                <span className="rnum">R{round.round}</span>
                <span className="pnum">
                  pick {round.pick} &middot; {round.slotInRound} of {teamCount}
                </span>
                <span className="pos-counts">
                  {round.positionCounts.map((pc) => (
                    <span className="pos-count" key={pc.position}>
                      <PositionChip position={pc.position} />
                      <b>{pc.count}</b>
                    </span>
                  ))}
                </span>
              </div>
              {round.targets.map((player) => (
                <div className="target-row" key={player.sleeperId}>
                  <div className="player">
                    <PositionChip position={player.position} />
                    <span className="player-name">{player.name}</span>
                    <span className="pro-team">{player.proTeam ?? ""}</span>
                    <MyRankTag rank={player.myRank} />
                  </div>
                  <span className="target-figs">
                    rank <b>{player.boardRank.toFixed(0)}</b>
                    {player.adp ? ` · adp ${player.adp.toFixed(0)}` : ""} &middot; +
                    {player.vorp.toFixed(0)} vorp
                  </span>
                  <span className="band" data-band={player.band}>
                    {player.availability !== undefined
                      ? `${Math.round(player.availability * 100)}%`
                      : ""}{" "}
                    {BAND_LABEL[player.band ?? ""] ?? ""}
                  </span>
                </div>
              ))}
            </div>
          ))}
        </div>
      ) : (
        <>
          <div className="grid-scroll">
            <table className="draft-grid">
              <thead>
                <tr>
                  <th />
                  {Array.from({ length: teamCount }, (_, i) => i + 1).map((n) => (
                    <th key={n} className={n === board.slot ? "slot-mine" : undefined}>
                      {n === board.slot ? `${n} YOU` : n}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {board.grid.map((row) => (
                  <tr key={row.round}>
                    <td className="rlabel">R{row.round}</td>
                    {row.cells.map((cell) => (
                      <td key={cell.pick}>
                        <div
                          className={`cell${cell.isMine ? " mine" : ""}${
                            cell.player ? "" : " empty"
                          }`}
                          data-pos={cell.player?.position}
                        >
                          <div className="cname">
                            {cell.player ? cell.player.name : "-"}
                          </div>
                          <div className="cmeta">
                            {cell.pick}
                            {cell.player ? ` · ${cell.player.position}` : ""}
                          </div>
                        </div>
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="legend">
            {["QB", "RB", "WR", "TE", "K", "DEF"].map((pos) => (
              <span key={pos}>
                <span
                  className="swatch"
                  style={{ background: `var(--pos-${pos.toLowerCase()})` }}
                />
                {pos}
              </span>
            ))}
            <span>
              <span
                className="swatch"
                style={{ background: "rgba(53,201,141,0.35)" }}
              />
              your picks
            </span>
          </div>
        </>
      )}
    </>
  );
}

export default function App() {
  const [leagues, setLeagues] = useState<League[] | null>(null);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<
    "season" | "draft" | "board" | "rankings" | "mock"
  >("season");

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
          <button
            aria-pressed={view === "board"}
            onClick={() => setView("board")}
          >
            Board
          </button>
          <button
            aria-pressed={view === "rankings"}
            onClick={() => setView("rankings")}
          >
            Rankings
          </button>
          <button
            aria-pressed={view === "mock"}
            onClick={() => setView("mock")}
          >
            Mock
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
        {view === "mock" ? (
          <MockView leagueId={active.id} teamCount={active.teamCount} />
        ) : view === "rankings" ? (
          <RankingsView leagueId={active.id} />
        ) : view === "board" ? (
          <AdpBoardView leagueId={active.id} />
        ) : view === "draft" ? (
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
