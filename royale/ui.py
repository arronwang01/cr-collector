"""Rich TUI: the whole program is this interactive flow. No flags, no argv.

Deck is fixed (SEED) -- the tool exists to mine that archetype, so it is not
asked for. What you choose is which of the players found on the deck and its
evo/hero variations get crawled, and how deep to walk their history.

Speed is not a prompt: the limiter ramps up on real crawl traffic until
RoyaleAPI answers 429, then settles just under that, and the live figure sits on
the end of the progress bar.
"""

from __future__ import annotations

import hashlib
import readline
import signal
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from . import parse, pipeline
from .cookies import Session, find_sessions
from .limiter import Limiter
from .transport import AuthError, Client, Pages

SEED = "cannon-ev1,fireball,hog-rider,ice-golem,ice-spirit,musketeer,skeletons,the-log"
OUTDIR = Path(".")

# Players never crawled, however they are picked -- 'all' included. Names are
# compared exactly (case-folded); tags are the reliable key once you can see one
# in the roster table, since display names carry emoji and invisible characters.
EXCLUDE_NAMES: tuple[str, ...] = ("Ahmed\u2728\u5b89\u4e4b",)
EXCLUDE_TAGS: tuple[str, ...] = ()

# Cards banned outright: a battle is dropped when either side played one, since
# a banned card shapes the match whoever brought it. Matched in exact evolution
# form, so plain elite-barbarians is unaffected by banning elite-barbarians-ev1.
EXCLUDE_CARDS: tuple[str, ...] = ("elite-barbarians-ev1",)

# Ranked ladder, as it appears in RoyaleAPI's battletype-* row class -- currently
# "pathOfLegend". Matched as lower-cased substrings, so casing and any _1v1 style
# suffix are both tolerated; the mode tally after the crawl shows what was on offer.
RANKED_TYPES = frozenset({"ranked", "pathoflegend", "path_of_legend"})

console = Console()


def _progress(limiter: Limiter) -> Progress:
    """Progress bar with the live request rate glued on the end."""
    return Progress(
        TextColumn("[bold blue]{task.description}"), BarColumn(), MofNCompleteColumn(),
        TimeElapsedColumn(), TextColumn("[dim]{task.fields[rate]}"), console=console)


def _rate_field(limiter: Limiter) -> dict:
    return {"rate": str(limiter)}


# ---------------------------------------------------------------- session
def pick_session() -> Session:
    """Which local browser holds a RoyaleAPI login."""
    with console.status("[cyan]reading browser cookie jars..."):
        found = find_sessions()
    if not found:
        raise AuthError(
            "no RoyaleAPI session found. On a desktop, log in at "
            "https://royaleapi.com/login and rerun. On a server with no browser "
            "profile, copy the __royaleapi_session_v2 cookie out of a desktop "
            "browser and export ROYALEAPI_SESSION=<value>.")
    if len(found) == 1:
        console.print(f"[green]session[/] {found[0]}")
        return found[0]

    t = Table("#", "browser", "session", title="RoyaleAPI logins found")
    for i, s in enumerate(found, 1):
        t.add_row(str(i), s.browser, s.value[:12] + "...")
    console.print(t)
    while True:
        raw = console.input("[bold]use which[/] [dim](1)[/] ").strip() or "1"
        if raw.isdigit() and 1 <= int(raw) <= len(found):
            return found[int(raw) - 1]
        console.print("[yellow]pick one of the listed numbers[/]")


# ---------------------------------------------------------------- roster
def roster(client: Client, seed: str, floor: int | None = None, show: bool = True,
           decks: list[str] | None = None,
           ) -> tuple[dict[str, dict], dict[str, str], list[str]]:
    """Every player listed on the seed deck and all its variations.

    `floor` given means unattended: apply it instead of asking. `show` prints the
    roster table, which is noise in a headless run over hundreds of players.
    """
    if decks is None:
        with console.status("[cyan]finding deck variations..."):
            decks = pipeline.similar_decks(client, seed)
    console.print(f"[green]{len(decks)} decks[/] (seed + {len(decks) - 1} variations)")

    with _progress(client.limiter) as prog:
        t = prog.add_task("rating boards", total=len(decks), **_rate_field(client.limiter))
        players, found_on = pipeline.rated_players(
            client, decks,
            tick=lambda: prog.update(t, advance=1, **_rate_field(client.limiter)),
            on_error=lambda d, e: console.print(f"[yellow]skip[/] board {d}: {e}"))
    if not players:
        raise AuthError("no rated players on any of those decks")

    dropped = [t_ for t_, p in players.items() if _excluded(p)]
    for t_ in dropped:
        console.print(f"[yellow]excluded[/] {players[t_]['player_name']} [dim]#{t_}[/]")
        players.pop(t_)
    if (EXCLUDE_NAMES or EXCLUDE_TAGS) and not dropped:
        console.print("[yellow]note[/] nothing matched the exclusion list -- if a name looks "
                      "right in the table below, exclude by tag instead (EXCLUDE_TAGS in "
                      "royale/ui.py)")

    # Only Ultimate Champion players carry a rating at all -- below UC the ranked
    # ladder tracks steps -- so this board is already UC-only, and the floor just
    # picks how far down it you want to go.
    rated = sorted((int(p["rating"] or 0) for p in players.values()), reverse=True)
    if rated:
        console.print(f"[dim]ratings {rated[0]} high, {rated[-1]} low, "
                      f"median {rated[len(rated) // 2]}[/]")
    if floor is None:
        floor = _ask_int("minimum rating (0 = keep every UC player)", 0, lo=0, hi=10_000)
    if floor:
        for t_ in [t_ for t_, p in players.items() if int(p["rating"] or 0) < floor]:
            players.pop(t_)
        if not players:
            raise AuthError(f"no players left at rating >= {floor}")
        console.print(f"[green]{len(players)} players[/] at rating >= {floor}")

    # Best players first: the roster is a leaderboard, so read it like one.
    order = sorted(players, key=lambda t_: -int(players[t_]["rating"] or 0))
    if not show:
        return players, found_on, order
    seed_cards = set(seed.split(","))
    table = Table("#", "player", "tag", "rating", "wins 7d", "clan", "variation",
                  title=f"{len(order)} players on this archetype")
    for i, tag in enumerate(order, 1):
        p = players[tag]
        variant = found_on.get(tag, "")
        table.add_row(str(i), p["player_name"], tag, p["rating"], p["wins_7d"], p["clan_tag"],
                      "seed" if variant == seed else ",".join(
                          c for c in variant.split(",") if c not in seed_cards))
    console.print(table)
    return players, found_on, order


def _rejector(cards: tuple[str, ...]):
    """A veto over whole battles: either deck playing a banned card drops it."""
    if not cards:
        return None
    return lambda b: any(parse.plays_exact(b["team_deck"], c)
                         or parse.plays_exact(b["opponent_deck"], c) for c in cards)


def shard_of(tag: str, n: int) -> int:
    """Which of `n` miners owns this player.

    Hashed rather than sliced off the roster order, so every miner agrees on the
    split without coordinating and the answer does not move when RoyaleAPI
    reorders its boards or a rating changes.
    """
    return int(hashlib.sha1(tag.encode()).hexdigest(), 16) % n


def _excluded(p: dict) -> bool:
    """True for players on the exclusion list, by tag or by exact display name."""
    tags = {t.lstrip("#").upper() for t in EXCLUDE_TAGS}
    names = {n.strip().casefold() for n in EXCLUDE_NAMES}
    return p["player_tag"].lstrip("#").upper() in tags or p["player_name"].strip().casefold() in names


# ---------------------------------------------------------------- picker
def _completer(options: list[str]):
    def complete(text: str, state: int):
        low = text.lower().lstrip()
        hits = [o for o in options if o.lower().startswith(low)]
        if not hits:  # fall back to substring so a bare tag or surname completes
            hits = [o for o in options if low in o.lower()]
        return hits[state] if state < len(hits) else None
    return complete


def pick_players(players: dict[str, dict], order: list[str]) -> list[str]:
    """Type names or tags with TAB completion, numbers, ranges, or 'all'."""
    labels = {f"{players[tag]['player_name']} #{tag}".strip(): tag for tag in order}
    options = sorted(labels) + sorted(f"#{t}" for t in order) + ["all"]

    readline.set_completer_delims(",")
    readline.set_completer(_completer(options))
    readline.parse_and_bind("tab: complete")

    console.print(Panel(
        "[bold]TAB[/] completes names and tags  ·  numbers and ranges work "
        "([cyan]1,4,7-9[/])  ·  [cyan]all[/] takes everyone\n"
        "add as many lines as you like, [bold]empty line[/] starts the crawl",
        title="pick players", border_style="cyan", expand=False))

    chosen: list[str] = []
    while True:
        raw = input("players> ").strip()
        if not raw:
            if chosen:
                return chosen
            console.print("[yellow]nothing picked yet[/]")
            continue
        for token in (t.strip() for t in raw.split(",") if t.strip()):
            hits = _resolve(token, players, order, labels)
            if not hits:
                console.print(f"[yellow]no match[/] {token!r}")
            for tag in hits:
                if tag not in chosen:
                    chosen.append(tag)
                    console.print(f"  [green]+[/] {players[tag]['player_name']} "
                                  f"[dim]#{tag}[/] [dim]({len(chosen)} picked)[/]")


def _resolve(token: str, players: dict[str, dict], order: list[str],
             labels: dict[str, str]) -> list[str]:
    """One token -> player tags. Accepts 'all', 3, 7-9, '#TAG', a label, a name."""
    low = token.lower()
    if low == "all":
        return list(order)
    if token in labels:
        return [labels[token]]

    lo, _, hi = token.partition("-")
    if lo.isdigit() and (hi.isdigit() or not hi):
        rng = range(int(lo), int(hi or lo) + 1)
        return [order[n - 1] for n in rng if 1 <= n <= len(order)]

    tag = token.lstrip("#").upper()
    if tag in players:
        return [tag]
    hits = [t for t in order if low in players[t]["player_name"].lower()]
    if len(hits) > 1:
        console.print(f"[yellow]{token!r} matches {len(hits)} players[/] -- TAB to disambiguate")
        return []
    return hits


# ---------------------------------------------------------------- settings
def pick_settings(limiter: Limiter) -> int:
    """Max parallel requests and the rate ceiling. Kept low by default --
    RoyaleAPI's 429 is per source IP, so more browser tabs would not help;
    this is the one knob that actually trades speed for risk of being throttled.
    """
    console.print(Panel(
        f"[bold]batch size[/] -- requests overlapped per round trip, default "
        f"[cyan]{pipeline.DEFAULT_BATCH}[/]\n"
        f"[bold]rate ceiling[/] -- req/s the limiter won't probe past, default "
        f"[cyan]{limiter.ceiling:.0f}[/]\n"
        "the limiter still self-tunes under this ceiling and still backs off on 429",
        title="settings", border_style="cyan", expand=False))
    batch = _ask_int("batch size", pipeline.DEFAULT_BATCH, lo=1, hi=32)
    limiter.ceiling = _ask_int("rate ceiling (req/s)", int(limiter.ceiling), lo=1, hi=60)
    return batch


def _ask_yes(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{prompt} [{hint}]> ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes", "n", "no"):
            return raw.startswith("y")
        console.print("[yellow]y or n, or blank for the default[/]")


def _ask_int(prompt: str, default: int, lo: int, hi: int) -> int:
    while True:
        raw = input(f"{prompt} [{default}]> ").strip()
        if not raw:
            return default
        if raw.isdigit() and lo <= int(raw) <= hi:
            return int(raw)
        console.print(f"[yellow]a number from {lo} to {hi}, or blank for {default}[/]")


# ---------------------------------------------------------------- depth
def pick_depth() -> int:
    """History pages per player. 0 means walk the archive to its end."""
    console.print(Panel(
        "[bold]blank[/] or [cyan]0[/] walks each player's history all the way back\n"
        "otherwise a page count, [cyan]10[/] battles per page  ·  "
        "[bold]Ctrl-C[/] stops the crawl and still writes the CSVs",
        title="how deep", border_style="cyan", expand=False))
    while True:
        raw = input("pages> ").strip()
        if not raw:
            return 0
        if raw.isdigit():
            return int(raw)
        console.print("[yellow]a number, or blank for everything[/]")


# ---------------------------------------------------------------- crawl
GROUP = 25  # players walked, replayed and checkpointed as one unit


def crawl(client: Client, seed: str, players: dict[str, dict], found_on: dict[str, str],
          max_pages: int, ranked_only: bool = True, group: int = GROUP,
          outdir: Path = OUTDIR, keep_deck=None, refresh: bool = False,
          exclude_cards: tuple[str, ...] = EXCLUDE_CARDS) -> int:
    """Walk the roster in groups, writing and checkpointing after each one.

    A full-depth crawl of a large roster runs for hours, so nothing is allowed
    to live only in memory: each group's battles are fetched, replayed, written
    and recorded before the next group starts. Ctrl-C stops after the group in
    flight; a crash costs at most that group. Rerunning in the same directory
    skips finished players outright and any replay already on disk.
    """
    lim = client.limiter
    skipped = 0
    stop = False

    def skip(what: str, e: Exception) -> None:
        nonlocal skipped
        skipped += 1
        console.print(f"[yellow]skip[/] {what}: {e}")

    def on_sigint(*_):
        """First Ctrl-C asks for a clean stop; the second must actually stop.

        Handing the handler back to Python is what makes that true -- otherwise
        this swallows every SIGINT and the only way out is to kill the process.
        """
        nonlocal stop
        stop = True
        signal.signal(signal.SIGINT, prev)
        console.print("\n[yellow]stopping after this player[/] -- everything written so far is "
                      "safe; press Ctrl-C again to stop right now")

    pipeline.write_players(outdir, players, found_on)
    # A refresh re-walks everyone looking for games played since last time. The
    # Sink still refuses battles already on disk, so only genuinely new ones cost
    # a replay fetch -- which is what makes a repeating overnight run cheap.
    ledger = pipeline.Ledger(outdir, resume=not refresh)
    todo = [t for t in players if t not in ledger.done]
    if len(todo) < len(players):
        console.print(f"[green]resuming[/] {len(players) - len(todo)} players already finished, "
                      f"{len(todo)} to go")
    if not todo:
        console.print("[green]nothing left to do[/] -- every player in this roster is finished")
        return 0

    groups = [todo[i:i + group] for i in range(0, len(todo), group)]
    modes_all: dict[str, int] = {}
    total = 0
    prev = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, on_sigint)
    try:
        with pipeline.Sink(outdir) as sink:
            dead_logins = 0

            def landed(tag: str, kept: list[dict]) -> None:
                """One player's archive is done: replay and write it now.

                Doing this per player rather than per wave is what keeps a
                deep-archive crawl honest -- rows reach disk continuously, and the
                log gains a line per player instead of going quiet for an hour.
                """
                nonlocal total, dead_logins
                # A battle between two roster players is reached from both
                # histories; fetch it once, from whichever got there first.
                fresh, queued = [], set()
                for r in kept:
                    if r["replay_tag"] in sink.done or r["replay_tag"] in queued:
                        continue
                    queued.add(r["replay_tag"])
                    fresh.append(r)
                got = pipeline.replays(
                    client, fresh, on_error=lambda b, e: skip(f"replay {b['replay_tag']}", e),
                    sink=sink)
                total += got
                ledger.finish([tag], seed=seed, max_pages=max_pages, ranked_only=ranked_only)
                dead_logins = dead_logins + 1 if (fresh and not got) else 0
                if stop:
                    raise KeyboardInterrupt
                console.print(
                    f"[green]{players[tag]['player_name'][:20]}[/] [dim]#{tag}[/] "
                    f"r{players[tag]['rating']} · {len(kept)} kept · {got} replays · "
                    f"[bold]{sink.battles} battles / {sink.plays} plays[/] · [dim]{lim}[/]")

            for gi, tags in enumerate(groups, 1):
                head = f"group {gi}/{len(groups)}"
                console.print(f"[cyan]{head}[/] walking {len(tags)} players")
                try:
                    rows, dropped, modes = pipeline.battles(
                        client, {t: players[t] for t in tags}, found_on, seed, max_pages,
                        on_error=lambda t, e: skip(f"battles {t}", e),
                        keep_types=RANKED_TYPES if ranked_only else None,
                        on_done=landed, keep_deck=keep_deck,
                        reject=_rejector(exclude_cards))
                except KeyboardInterrupt:
                    console.print("[yellow]stopped[/]")
                    break
                for k, v in modes.items():
                    modes_all[k] = modes_all.get(k, 0) + v
                console.print(f"[dim]{head} done · {dropped['deck']} other deck, "
                              f"{dropped['mode']} other mode, "
                              f"{dropped['card']} banned card[/]")
                # Consecutive players whose every replay failed means the login died,
                # not bad luck: /data/replay is the only gated call, and an expired
                # session fails all of them. Stop rather than burn the night.
                if dead_logins >= 3:
                    console.print("[red]three players in a row had every replay fail[/] -- the "
                                  "RoyaleAPI session has probably expired. Log in again at "
                                  "https://royaleapi.com and rerun; finished players are "
                                  "recorded, so it picks up here.")
                    break
                if stop:
                    console.print("[yellow]stopped by request[/]")
                    break
            written, n_plays = sink.battles, sink.plays
    finally:
        signal.signal(signal.SIGINT, prev)

    if modes_all:
        mt = Table("battle_type", "deck-matching battles", "kept",
                   title="game modes seen on this deck")
        for name, n in sorted(modes_all.items(), key=lambda kv: -kv[1]):
            kept = not ranked_only or any(k in name.lower() for k in RANKED_TYPES)
            mt.add_row(name, str(n), "[green]yes[/]" if kept else "[dim]no[/]")
        console.print(mt)
    if not written and ranked_only and modes_all:
        console.print("[yellow]nothing kept[/] -- the ranked battle_type is one of the names "
                      "above; add it to RANKED_TYPES in royale/ui.py")

    table = Table("output", "rows written this run", title=f"done ({skipped} skipped)")
    table.add_row(str(outdir / "battles.csv"), str(written))
    table.add_row(str(outdir / "plays.csv"), str(n_plays))
    table.add_row(str(outdir / "players.csv"), str(len(players)))
    console.print(table)
    console.print(f"[dim]{lim.sent} requests · settled at {lim.rate:.1f}/s · "
                  f"peak {lim.peak:.1f}/s · {lim.hits} x 429[/]")
    return written


# ---------------------------------------------------------------- app
def auto(seed: str = SEED, min_rating: int = 0, max_pages: int = 0, group: int = GROUP,
         outdir: Path = OUTDIR, ranked_only: bool = True, variations_only: bool = True,
         refresh: bool = False, card: str = "", shard: tuple[int, int] | None = None,
         exclude_cards: tuple[str, ...] = EXCLUDE_CARDS) -> int:
    """The whole flow with no prompts, for a long unattended run.

    `card` swaps the archetype definition: instead of one eight-card list and
    its evo/hero variations, the roster comes from every deck RoyaleAPI lists
    for that card and a battle counts if it played the card at all.
    """
    if card:
        scope = f"any deck with {card}"
    elif variations_only:
        scope = "seed variations"
    else:
        scope = "any"
    console.print(Panel(
        f"[dim]deck[/] {card or seed}\n[dim]min rating[/] {min_rating or 'none'}   "
        f"[dim]pages[/] {max_pages or 'all'}   [dim]group[/] {group}   "
        f"[dim]ranked only[/] {ranked_only}   [dim]decks[/] {scope}\n"
        f"[dim]out[/] {outdir.resolve()}"
        + (f"\n[dim]banned[/] {', '.join(exclude_cards)}" if exclude_cards else "")
        + (f"   [dim]shard[/] {shard[0]} of {shard[1]}" if shard else "")
        + ("   [dim]refresh[/] re-walking every player" if refresh else ""),
        title="unattended crawl", border_style="cyan", expand=False))
    session = pick_session()
    with console.status("[cyan]starting browser, clearing Cloudflare..."):
        pages = Pages(session)
    try:
        client = Client(pages)
        if not client.logged_in():
            raise AuthError(f"the {session.browser} session is not logged in to RoyaleAPI")
        console.print("[green]logged in[/]")
        decks = None
        keep_deck = (lambda d: parse.is_variation(d, seed)) if variations_only else None
        if card:
            cards = [c.strip() for c in card.split(",") if c.strip()]
            with console.status(f"[cyan]finding decks that play {' + '.join(cards)}..."):
                decks = pipeline.card_decks(client, cards)
            if not decks:
                raise AuthError(f"RoyaleAPI lists no decks playing {' + '.join(cards)}")
            console.print(f"[green]{len(decks)} decks[/] play {' + '.join(cards)}")
            keep_deck = lambda d: parse.has_cards(d, cards)
            seed = decks[0]
        players, found_on, order = roster(client, seed, floor=min_rating, show=False, decks=decks)
        if shard:
            i, n = shard
            mine = [t for t in order if shard_of(t, n) == i]
            console.print(f"[green]shard {i}/{n}[/] takes {len(mine)} of {len(order)} players")
            players = {t: players[t] for t in mine}
            order = mine
            if not order:
                raise AuthError(f"shard {i} of {n} is empty for this roster")
        console.print(f"[green]{len(order)} players[/] queued")
        return crawl(client, seed, players, found_on, max_pages, ranked_only, group, outdir,
                     keep_deck, refresh, exclude_cards)
    finally:
        pages.close()


def app(seed: str = SEED) -> int:
    console.print(Panel(
        f"[dim]deck[/] {seed}\n"
        f"[dim]base[/] {', '.join(sorted(parse.base_cards(seed)))}\n"
        "the browser holds the Cloudflare pass and issues every request",
        title="RoyaleAPI replay scraper", border_style="cyan", expand=False))
    session = pick_session()

    with console.status("[cyan]starting browser, clearing Cloudflare..."):
        pages = Pages(session)
    try:
        client = Client(pages)
        with console.status("[cyan]checking login..."):
            ok = client.logged_in()
        if not ok:
            raise AuthError(f"the {session.browser} session is not logged in to RoyaleAPI -- "
                            "log in at https://royaleapi.com/login, then rerun")
        console.print("[green]logged in[/]")

        client.batch = pick_settings(client.limiter)
        players, found_on, order = roster(client, seed)
        chosen = pick_players(players, order)
        console.print(f"[green]{len(chosen)} player(s)[/] queued")
        max_pages = pick_depth()
        ranked_only = _ask_yes("ranked ladder battles only (skip 2v2, challenges, friendlies)",
                               default=True)
        return crawl(client, seed, {t: players[t] for t in chosen}, found_on, max_pages,
                     ranked_only, keep_deck=lambda d: parse.is_variation(d, seed))
    finally:
        pages.close()
