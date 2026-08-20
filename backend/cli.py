"""Command line entry point.

    python -m backend.cli sync      pull everything from both platforms
    python -m backend.cli status    show what is currently in the database
    python -m backend.cli check     verify credentials without writing anything
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from sqlalchemy import select

from backend.clients.espn import EspnAuthError, EspnClient, EspnError
from backend.clients.sleeper import SleeperClient, SleeperError
from backend.config import Config, ConfigError, ensure_dirs
from backend.db import (
    PLATFORM_ESPN,
    PLATFORM_SLEEPER,
    Draft,
    League,
    Player,
    RosterSlot,
    Team,
    init_db,
    make_session_factory,
)
from backend.ingest import drafts as drafts_ingest
from backend.ingest import leagues as leagues_ingest
from backend.ingest import players as players_ingest
from backend.ingest import projections as projections_ingest
from backend.analysis.board import draft_state
from backend.resolve.player_matching import PlayerRegistry

log = logging.getLogger("fantasy")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
    )
    # httpx logs every request at INFO, which drowns out everything else.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _resolve_season(config: Config, client: SleeperClient) -> str:
    """The season to operate on, from config or Sleeper's own NFL state."""
    if config.season:
        return config.season
    state = client.nfl_state()
    return str(state.get("season"))


def cmd_check(config: Config) -> int:
    """Verify credentials for both platforms without writing anything."""
    ok = True

    with SleeperClient() as sleeper:
        try:
            username = config.require_sleeper_username()
            user_id = sleeper.user_id(username)
            season = _resolve_season(config, sleeper)
            found = sleeper.leagues(user_id, season)
            print(f"  Sleeper  OK   {username} -> {len(found)} league(s) in {season}")
        except (ConfigError, SleeperError) as exc:
            print(f"  Sleeper  FAIL {exc}")
            ok = False
            season = config.season or "2026"

    if not config.espn.league_ids:
        print("  ESPN     --   no ESPN_LEAGUE_IDS configured, skipping")
    else:
        cookie_state = "with cookies" if config.espn.has_cookies else "no cookies"
        with EspnClient(config.espn.swid, config.espn.s2) as espn:
            for league_id in config.espn.league_ids:
                try:
                    espn.verify_credentials(league_id, season)
                    print(f"  ESPN     OK   league {league_id} ({cookie_state})")
                except EspnAuthError as exc:
                    print(f"  ESPN     FAIL league {league_id}: {exc}")
                    ok = False
                except EspnError as exc:
                    print(f"  ESPN     FAIL league {league_id}: {exc}")
                    ok = False

    return 0 if ok else 1


def cmd_sync(config: Config, skip_players: bool = False) -> int:
    """Pull leagues, rosters, and drafts from both platforms."""
    ensure_dirs()
    engine = init_db()
    Session = make_session_factory(engine)

    exit_code = 0

    with SleeperClient() as sleeper, Session() as session:
        season = _resolve_season(config, sleeper)
        log.info("Syncing season %s", season)

        if not skip_players:
            log.info("Refreshing player universe...")
            players_ingest.sync_players(session, sleeper)

        registry = PlayerRegistry(players_ingest.load_registry_source(sleeper))

        # --- Sleeper ---
        try:
            username = config.require_sleeper_username()
            sleeper_leagues = leagues_ingest.sync_sleeper_leagues(
                session, sleeper, username, season
            )
            for league in sleeper_leagues:
                log.info("  Sleeper league: %s", league.name)
                drafts_ingest.sync_sleeper_drafts(session, sleeper, league)
        except (ConfigError, SleeperError) as exc:
            log.error("Sleeper sync failed: %s", exc)
            exit_code = 1

        # --- ESPN ---
        if not config.espn.league_ids:
            log.info("No ESPN_LEAGUE_IDS configured, skipping ESPN")
        else:
            with EspnClient(config.espn.swid, config.espn.s2) as espn:
                for league_id in config.espn.league_ids:
                    try:
                        espn.verify_credentials(league_id, season)
                        league, report = leagues_ingest.sync_espn_league(
                            session,
                            espn,
                            league_id,
                            season,
                            registry,
                            my_team_id=config.espn.team_id,
                        )
                        log.info("  ESPN league: %s", league.name)
                        _print_report(report)

                        espn_players = drafts_ingest.build_espn_player_index(
                            espn, league
                        )
                        drafts_ingest.sync_espn_draft(
                            session, espn, league, registry, espn_players
                        )
                    except EspnAuthError as exc:
                        log.error("ESPN credentials problem: %s", exc)
                        exit_code = 1
                    except EspnError as exc:
                        log.error("ESPN league %s failed: %s", league_id, exc)
                        exit_code = 1

        # Projections come last: they need every league's scoring settings loaded,
        # and one ESPN player pool is scored under all of them.
        if config.espn.league_ids:
            try:
                _sync_projections(session, config, season, registry)
            except EspnError as exc:
                log.error("Projection sync failed: %s", exc)
                exit_code = 1

    if exit_code == 0:
        log.info("Sync complete.")
    return exit_code


def _sync_projections(session, config: Config, season: str, registry) -> None:
    """Score one ESPN player pool under every synced league's own rules."""
    leagues = session.execute(select(League).where(League.season == season)).scalars().all()
    if not leagues:
        return

    log.info("Fetching projections...")
    with EspnClient(config.espn.swid, config.espn.s2) as espn:
        players = projections_ingest.fetch_player_pool(
            espn, config.espn.league_ids[0], season
        )
    log.info("  scoring %d players under %d league(s)", len(players), len(leagues))
    projections_ingest.sync_projections(
        session, leagues, players, registry, season
    )


def _print_report(report) -> None:
    """Surface unresolved players; a silent mismatch corrupts everything downstream."""
    if report.total == 0:
        return
    if report.is_clean:
        log.info("    All %d rostered players resolved.", report.total)
    else:
        for line in report.summary().splitlines():
            log.warning("    %s", line)



POSITION_WIDTH = 4


def _refresh_draft(session, config: Config, league: League, registry) -> int:
    """Pull only this league's draft picks. Fast enough to poll while on the clock."""
    if league.platform == PLATFORM_SLEEPER:
        with SleeperClient() as sleeper:
            drafts = drafts_ingest.sync_sleeper_drafts(session, sleeper, league)
        return sum(len(d.picks) for d in drafts)

    with EspnClient(config.espn.swid, config.espn.s2) as espn:
        espn_players = drafts_ingest.build_espn_player_index(espn, league)
        draft = drafts_ingest.sync_espn_draft(
            session, espn, league, registry, espn_players
        )
    return len(draft.picks) if draft else 0


def _print_board(state: dict) -> None:
    league = state["league"]
    header = (
        f"{league['name']}  |  pick {state['round']}."
        f"{state['currentPick']:02d}  |  {state['picksMade']} made  |  "
        f"{state['availableCount']} available"
    )
    print()
    print(header)
    print("-" * len(header))

    if state.get("isComplete"):
        print("Draft complete.")
        return

    need = ", ".join(
        f"{n['position']} {n['have']}/{n['starters']}"
        for n in state["need"]
        if n["tier"] in ("urgent", "needed")
    )
    if need:
        print(f"still need: {need}")
    if state.get("picksRemaining") is not None:
        print(f"your picks left: {state['picksRemaining']}")
    print()

    for i, rec in enumerate(state["recommendations"], 1):
        marker = ">>" if i == 1 else "  "
        print(
            f"{marker} {i:>2}. {rec['name'][:24]:24} "
            f"{rec['position']:<{POSITION_WIDTH}} "
            f"{rec['points']:6.1f} pts   +{rec['vorp']:6.1f} vorp"
        )
        print(f"        {' / '.join(rec['reasons'])}")

    recent = state.get("recentPicks") or []
    if recent:
        print()
        print("last picks:")
        for pick in recent[:5]:
            player = pick.get("player")
            name = player["name"] if player else "?"
            mine = " <= you" if pick.get("isMine") else ""
            print(f"   {pick['round']}.{pick['pickNo']:02d} {name}{mine}")


def cmd_draft(
    config: Config,
    league_id: int | None = None,
    interval: int = 20,
    once: bool = False,
) -> int:
    """Watch a draft and keep recommending the next pick.

    Polls the platform's public draft endpoint rather than its websocket, which is
    undocumented. That costs a few seconds of lag - fine for deciding a pick, and far
    more reliable.
    """
    ensure_dirs()
    engine = init_db()
    Session = make_session_factory(engine)

    with Session() as session:
        leagues = session.execute(select(League)).scalars().all()
        if not leagues:
            print("No leagues synced. Run: python -m backend.cli sync")
            return 1

        if league_id is not None:
            league = session.get(League, league_id)
            if league is None:
                print(f"No league with id {league_id}. Run status to list them.")
                return 1
        else:
            # Prefer a draft that has not finished; that is the one being watched.
            league = next(
                (
                    lg
                    for lg in leagues
                    if any(d.status != "complete" for d in lg.drafts)
                ),
                leagues[0],
            )
            print(f"Watching {league.name}. Use --league to pick another.")

        registry = PlayerRegistry(players_ingest.load_registry_source())

        seen = -1
        try:
            while True:
                try:
                    _refresh_draft(session, config, league, registry)
                except (EspnError, SleeperError) as exc:
                    log.warning("Could not refresh picks: %s", exc)

                state = draft_state(session, league.id, limit=8)
                if state["picksMade"] != seen:
                    seen = state["picksMade"]
                    _print_board(state)
                    if state.get("isComplete"):
                        return 0
                if once:
                    return 0
                time.sleep(interval)
        except KeyboardInterrupt:
            print()
            print("Stopped watching.")
            return 0


def cmd_serve(config: Config, port: int = 8000, reload: bool = False) -> int:
    """Serve the dashboard.

    FastAPI serves the built React bundle from the same origin, so this is the only
    process needed to use the app.
    """
    import uvicorn

    from backend.api.routes import FRONTEND_DIST

    if not FRONTEND_DIST.is_dir():
        log.warning(
            "The frontend has not been built, so only the API will respond. "
            "Build it with:  cd frontend && npm install && npm run build"
        )
    else:
        log.info("Dashboard:  http://127.0.0.1:%d", port)

    uvicorn.run(
        "backend.api.routes:app",
        host="127.0.0.1",
        port=port,
        reload=reload,
        log_level="warning",
    )
    return 0


def cmd_teams(config: Config) -> int:
    """List ESPN teams and their ids, so ESPN_TEAM_ID can be set by hand."""
    if not config.espn.league_ids:
        print("No ESPN_LEAGUE_IDS configured.")
        return 1

    with SleeperClient() as sleeper:
        season = _resolve_season(config, sleeper)

    with EspnClient(config.espn.swid, config.espn.s2) as espn:
        for league_id in config.espn.league_ids:
            try:
                payload = espn.league(
                    league_id, season, views=("mTeam", "mSettings")
                )
            except EspnError as exc:
                print(f"league {league_id}: {exc}")
                return 1

            members = {
                str(m.get("id")).upper(): m.get("displayName")
                for m in (payload.get("members") or [])
            }
            settings = payload.get("settings") or {}
            print()
            print(f"{settings.get('name', league_id)}  (league {league_id})")

            my_swid = (config.espn.swid or "").upper()
            configured = config.espn.team_id
            for team in sorted(
                payload.get("teams") or [], key=lambda t: t.get("id") or 0
            ):
                owners = [str(o).upper() for o in (team.get("owners") or [])]
                owner_names = ", ".join(members.get(o, "?") for o in owners)
                marks = []
                if my_swid and my_swid in owners:
                    marks.append("matches your SWID")
                if configured and str(team.get("id")) == str(configured):
                    marks.append("ESPN_TEAM_ID")
                mark = f"   <== {' + '.join(marks)}" if marks else ""
                print(
                    f"  id={team.get('id'):<3} {str(team.get('name')):<30} "
                    f"{owner_names}{mark}"
                )

            if not configured and not any(
                my_swid and my_swid in [str(o).upper() for o in (t.get("owners") or [])]
                for t in (payload.get("teams") or [])
            ):
                print()
                print(
                    "  Your SWID matches none of these. Add the id of your team to "
                    ".env as ESPN_TEAM_ID, then re-run sync."
                )
    return 0


def cmd_status(config: Config) -> int:
    """Show what is currently stored."""
    engine = init_db()
    Session = make_session_factory(engine)

    with Session() as session:
        players = len(session.execute(select(Player.sleeper_id)).all())
        leagues = session.execute(select(League)).scalars().all()

        print(f"Players: {players}")
        if not leagues:
            print("No leagues synced yet. Run: python -m backend.cli sync")
            return 0

        print(f"Leagues: {len(leagues)}")
        for league in leagues:
            teams = session.execute(
                select(Team).where(Team.league_id == league.id)
            ).scalars().all()
            mine = next((t for t in teams if t.is_mine), None)
            drafts = session.execute(
                select(Draft).where(Draft.league_id == league.id)
            ).scalars().all()

            label = "Sleeper" if league.platform == PLATFORM_SLEEPER else "ESPN"
            print(f"\n  [{label}] {league.name}  ({league.season})")
            print(f"    teams: {len(teams)}   slots: {', '.join(league.roster_positions[:12])}")
            if mine:
                roster = session.execute(
                    select(RosterSlot).where(RosterSlot.team_id == mine.id)
                ).scalars().all()
                starters = sum(1 for r in roster if r.is_starter)
                print(
                    f"    my team: {mine.name} "
                    f"({mine.wins}-{mine.losses}-{mine.ties}, {mine.points_for} PF) "
                    f"- {len(roster)} players, {starters} starting"
                )
            else:
                print("    my team: not identified")
            for draft in drafts:
                picks = len(draft.picks)
                print(
                    f"    draft {draft.platform_draft_id}: "
                    f"{draft.status}, {draft.draft_type}, {picks} picks"
                )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fantasy", description="Multi-platform fantasy football tool"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync", help="pull leagues, rosters, and drafts")
    sync.add_argument(
        "--skip-players",
        action="store_true",
        help="reuse the cached player universe instead of refreshing it",
    )
    sub.add_parser("status", help="show what is stored")
    sub.add_parser("teams", help="list ESPN teams and ids")
    draft = sub.add_parser("draft", help="watch a draft and recommend picks")
    draft.add_argument("--league", type=int, default=None, help="league id")
    draft.add_argument(
        "--interval", type=int, default=20, help="seconds between polls"
    )
    draft.add_argument(
        "--once", action="store_true", help="print the board once and exit"
    )

    serve = sub.add_parser("serve", help="run the dashboard")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument(
        "--reload", action="store_true", help="restart on code changes"
    )
    sub.add_parser("check", help="verify credentials without writing")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    config = Config.load()

    if args.command == "sync":
        return cmd_sync(config, skip_players=args.skip_players)
    if args.command == "status":
        return cmd_status(config)
    if args.command == "check":
        return cmd_check(config)
    if args.command == "teams":
        return cmd_teams(config)
    if args.command == "draft":
        return cmd_draft(
            config,
            league_id=args.league,
            interval=args.interval,
            once=args.once,
        )
    if args.command == "serve":
        return cmd_serve(config, port=args.port, reload=args.reload)
    return 1


if __name__ == "__main__":
    sys.exit(main())
