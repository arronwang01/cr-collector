"""Crawl stages and CSV output. UI-free: each stage takes an optional `tick`
callback so the TUI can drive a progress bar without this module knowing about
one. Stages are independent -- call only the ones a new feature needs.

Requests run inside the browser (see transport), which is pinned to one thread,
so there is no thread pool here. Fan-out is a batch handed to Client.get_many
and overlapped by the page; the Limiter still sets the pace on top.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Callable, Iterable

from . import parse
from .transport import BASE, Client, url

Tick = Callable[[], None]
DEFAULT_BATCH = 6   # requests overlapped per round trip into the page
HISTORY_SLICE = 50  # players whose next page is held in memory at once


def _noop() -> None:
    pass


def _report(items: list, results: list, tick: Tick, on_error) -> list[tuple]:
    """Pair items with their results, reporting and dropping the failures."""
    out = []
    for item, res in zip(items, results):
        tick()
        if isinstance(res, Exception):
            if on_error:
                on_error(item, res)
        else:
            out.append((item, res))
    return out


# ---------------------------------------------------------------- stages
def similar_decks(client: Client, seed: str, limit: int = 25) -> list[str]:
    return parse.similar_decks(client.get(f"/decks/stats/{seed}/similar"), seed)[:limit]


def card_decks(client: Client, cards: list[str], limit: int = 40) -> list[str]:
    """Decks RoyaleAPI lists for the rarest-looking of `cards` that play all of
    them -- the roster source for an archetype defined by a card signature
    rather than by a fixed eight.

    Only one card page is fetched: any deck playing the whole signature is
    listed on every member's page, so the others would add nothing.
    """
    html = client.get(f"/card/{cards[0]}")
    return parse.decks_with_cards(html, cards)[:limit]


def rated_players(client: Client, decks: Iterable[str], per_deck: int | None = None,
                  tick: Tick = _noop, on_error=None,
                  ) -> tuple[dict[str, dict], dict[str, str]]:
    """Ratings boards for every deck -> (tag -> player, tag -> deck found on).

    A player can top the boards of several variations; keep the first hit so the
    battle crawl visits each player exactly once.
    """
    decks = list(decks)
    got = client.get_many([f"/decks/stats/{d}/players/ratings" for d in decks])
    fetched = _report(decks, got, tick, on_error)

    players: dict[str, dict] = {}
    found_on: dict[str, str] = {}
    for deck, html in fetched:
        for p in parse.rated_players(html)[:per_deck]:
            players.setdefault(p["player_tag"], p)
            found_on.setdefault(p["player_tag"], deck)
    return players, found_on


def player_battles(client: Client, tag: str, max_pages: int = 0,
                   out: list[dict] | None = None) -> list[dict]:
    """One player's battle history, following the pager's 'older' link.

    /battles/history serves 10 battles a page and its next link carries
    ?before=<oldest battle, epoch ms>, so this walks back through the whole
    archive -- far deeper than the battles page's infinite scroll. Sequential by
    necessity: page N+1's cursor is only known once page N is parsed, which is
    why the crawl overlaps players instead (see `battles`).
    """
    path = f"/player/{tag}/battles/history"
    rows = [] if out is None else out
    seen = {b["replay_tag"] for b in rows}
    visited = {path}
    page = 0
    while not max_pages or page < max_pages:
        html = client.get(path)
        # A page can hold nothing usable -- battles whose replay was never
        # recorded parse to nothing -- and that says nothing about whether the
        # archive continues. Only the pager decides when to stop; a repeated
        # cursor is the guard against walking in a circle.
        for b in parse.battles(html):
            if b["replay_tag"] not in seen:
                seen.add(b["replay_tag"])
                rows.append(b)
        page += 1
        nxt = parse.next_history_page(html)
        if not nxt or nxt in visited:
            break
        visited.add(nxt)
        path = nxt
    return rows


def battles(client: Client, players: dict[str, dict], found_on: dict[str, str], seed: str,
            max_pages: int = 0, tick: Tick = _noop, on_error=None,
            keep_types: frozenset[str] | None = None, on_done=None,
            keep_deck: "Callable[[str], bool] | None" = None,
            reject: "Callable[[dict], bool] | None" = None,
            ) -> tuple[list[dict], dict[str, int], dict[str, int]]:
    """Battle rows for every player, keeping only games played on a variation of
    `seed` -- same base cards, evo/hero swaps allowed, no substituted cards.

    Each player's history must be walked in order, but different players are
    independent, so this advances every player by one page per round and fetches
    that round as one batch. Slow players stay in the rotation until their
    archive runs out; finished ones drop out and tick the progress bar.

    keep_deck(team_deck) decides which battles are the archetype; None keeps
    every battle these players fought, still subject to keep_types.

    reject(battle) vetoes a battle outright, looking at either side's deck.

    keep_types narrows further to game modes: a battle survives when its
    battle_type contains any of these substrings. None keeps every mode.
    Substring, not equality, because RoyaleAPI's battletype-* class names carry
    suffixes (ranked_1v1, pathoflegend_1v1, ...) that shift between seasons.

    on_done(tag, kept) fires as each player's archive runs out, so a long crawl
    can replay and write that player straight away instead of holding hours of
    history in memory. An interrupt harvests the players still in flight.

    Returns (rows, dropped, modes) -- dropped counts what each filter removed,
    modes tallies the battle_type of every deck-matching battle seen, so the
    caller can show what the mode filter is actually choosing between.
    """
    walk = {t: {"path": f"/player/{t}/battles/history", "rows": [], "seen": set(), "page": 0,
                "visited": {f"/player/{t}/battles/history"}}
            for t in players}
    active = list(walk)
    rows: list[dict] = []
    dropped = {"deck": 0, "mode": 0, "card": 0}
    modes: dict[str, int] = {}

    def harvest(tag: str) -> None:
        """Filter one finished player's history and hand it straight on.

        Called the moment that player's archive runs out rather than after the
        whole wave, so a caller can replay and write it immediately. On a deep
        archive the difference is rows on disk within minutes instead of after
        every player in the wave has been walked.
        """
        kept = []
        for b in walk[tag]["rows"]:
            # keep_deck decides what counts as the archetype: an exact card set
            # for a fixed list, a single card for a family of them, or None to
            # keep whatever these players ran. The history pages are paid for
            # either way, so a looser test only costs the extra replay fetches.
            if keep_deck and not keep_deck(b["team_deck"]):
                dropped["deck"] += 1
                continue
            # reject() sees the whole battle, so it can veto on the opponent's
            # deck as well as the player's -- a banned card poisons the match
            # whichever side brought it.
            if reject and reject(b):
                dropped["card"] += 1
                continue
            bt = b["battle_type"] or "?"
            modes[bt] = modes.get(bt, 0) + 1
            # RoyaleAPI's class names are camelCase (pathOfLegend); match case-blind
            # so a season's rename of the casing cannot silently empty the crawl.
            if keep_types is not None and not any(k in bt.lower() for k in keep_types):
                dropped["mode"] += 1
                continue
            kept.append({"deck": found_on.get(tag, seed), **players[tag], **b})
        rows.extend(kept)
        walk[tag]["rows"] = []  # handed over; do not hold the raw history in memory
        if on_done:
            on_done(tag, kept)

    try:
        while active:
            got: list = []
            for i in range(0, len(active), HISTORY_SLICE):
                slice_ = active[i:i + HISTORY_SLICE]
                got += client.get_many([walk[t]["path"] for t in slice_])
            still: list[str] = []
            for tag, res in zip(active, got):
                w = walk[tag]
                if isinstance(res, Exception):
                    if on_error:
                        on_error(tag, res)
                    # Keep what this player already gave up. They are left out of
                    # the ledger, so the next run re-walks them from the top.
                    harvest(tag)
                    tick()
                    continue
                fresh = [b for b in parse.battles(res) if b["replay_tag"] not in w["seen"]]
                w["seen"].update(b["replay_tag"] for b in fresh)
                w["rows"] += fresh
                w["page"] += 1
                # An empty page means no replays were recorded for those battles,
                # not the end of the archive: keep following the pager. A repeated
                # cursor is what actually means stop.
                nxt = parse.next_history_page(res)
                if nxt and nxt not in w["visited"] and (not max_pages or w["page"] < max_pages):
                    w["visited"].add(nxt)
                    w["path"] = nxt
                    still.append(tag)
                else:
                    harvest(tag)  # this player's archive is done
                    tick()
            active = still
    except KeyboardInterrupt:
        for tag in active:      # partial histories are still worth keeping
            harvest(tag)
    return rows, dropped, modes


def replay_params(b: dict) -> dict:
    return {
        "tag": b["replay_tag"], "team_tags": b["team_tags"], "opponent_tags": b["opponent_tags"],
        "team_crowns": b["team_crowns"], "opponent_crowns": b["opponent_crowns"],
        "referrer_path": f"{BASE}/player/{b['team_tags'].split(',')[0]}/battles",
    }


def _parse_replay(raw: str, b: dict) -> tuple[dict, list[dict]]:
    import json
    data = json.loads(raw)
    if not data.get("success"):
        raise RuntimeError(f"replay {b['replay_tag']} refused (login expired?)")
    return parse.replay(data["html"])


def fetch_replay(client: Client, b: dict) -> tuple[dict, list[dict]]:
    return _parse_replay(client.get("/data/replay", replay_params(b), auth=True), b)


# Replay payloads are ~100KB of JSON each, so they are fetched a slice at a time
# and parsed down to rows before the next slice is asked for. Wide enough that the
# client still gets to pick its own burst width underneath.
REPLAY_SLICE = 24


def replays(client: Client, rows: list[dict], tick: Tick = _noop, on_error=None,
            sink: "Sink | None" = None) -> int:
    """Replay timelines. The only login-gated stage.

    Rows go to `sink` as each slice lands rather than being accumulated, so a
    crash, a killed terminal or a dead laptop battery costs the current slice
    and nothing else. Returns the number of battles written.
    """
    n = 0
    try:
        for i in range(0, len(rows), REPLAY_SLICE):
            chunk = rows[i:i + REPLAY_SLICE]
            got = client.get_many(
                [url("/data/replay", replay_params(b)) for b in chunk], auth=True, tick=tick)
            for b, raw in zip(chunk, got):
                try:
                    if isinstance(raw, Exception):
                        raise raw
                    stats, plays = _parse_replay(raw, b)
                except Exception as e:
                    if on_error:
                        on_error(b, e)
                    continue
                if sink and not sink.write(b, stats, plays):
                    continue  # same battle reached from the other player's history
                n += 1
    except KeyboardInterrupt:
        pass  # everything already written stays written
    return n


# ---------------------------------------------------------------- output
BATTLE_FIELDS = [
    "replay_tag", "deck", "player_tag", "player_name", "clan_tag", "rating", "rank", "wins_7d",
    "battle_time", "battle_timestamp", "battle_type", "result", "team_tags", "opponent_tags",
    "team_crowns", "opponent_crowns", "team_deck", "opponent_deck",
    "team_elixir_total", "team_elixir_troop", "team_elixir_building", "team_elixir_spell",
    "team_elixir_leaked", "oppo_elixir_total", "oppo_elixir_troop", "oppo_elixir_building",
    "oppo_elixir_spell", "oppo_elixir_leaked", "team_cards", "oppo_cards", "plays",
]
# x/y are tiles on the 18x32 arena; *_raw are the source thousandths. Both blank
# on hero-ability rows, which are events rather than placements.
PLAY_FIELDS = ["replay_tag", "play_index", "tick", "seconds", "side", "card", "ability",
               "x", "y", "x_raw", "y_raw"]


class Ledger:
    """Which players are finished, kept on disk beside the CSVs.

    The Sink already stops a replay being fetched twice, but only after its
    battle history has been walked again. This records whole players, so a
    restarted overnight run skips their history too and picks up where it
    stopped rather than where it started.
    """

    def __init__(self, outdir: Path, name: str = "progress.json", resume: bool = True):
        self.path = outdir / name
        self.done: set[str] = set()
        self.meta: dict = {}
        if resume and self.path.exists():
            try:
                blob = json.loads(self.path.read_text())
                self.done = set(blob.get("players_done", []))
                self.meta = blob.get("meta", {})
            except (ValueError, OSError):
                pass  # a half-written ledger just means starting over, not crashing

    def finish(self, tags: Iterable[str], **meta) -> None:
        self.done.update(tags)
        self.meta.update(meta)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"players_done": sorted(self.done), "meta": self.meta}, indent=1))
        tmp.replace(self.path)  # atomic: a kill mid-write cannot corrupt the ledger


PLAYER_FIELDS = ["player_tag", "player_name", "clan_tag", "rating", "rank", "wins_7d", "deck"]


def write_players(outdir: Path, players: dict[str, dict], found_on: dict[str, str]) -> Path:
    """The roster as crawled, so a later run can be reconciled against it."""
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / "players.csv"
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, PLAYER_FIELDS, extrasaction="ignore")
        w.writeheader()
        for tag, p in players.items():
            w.writerow({**p, "deck": found_on.get(tag, "")})
    return path


class Sink:
    """battles.csv + plays.csv, joinable on replay_tag, written as rows land.

    Opening an existing pair in append mode makes a rerun resume rather than
    start over: `done` is every replay_tag already on disk, so the caller can
    drop those before spending requests on them. Each battle is flushed as it is
    written, which is what makes an interrupted overnight run cost nothing.
    """

    def __init__(self, outdir: Path, resume: bool = True):
        outdir.mkdir(parents=True, exist_ok=True)
        self.battles_csv = outdir / "battles.csv"
        self.plays_csv = outdir / "plays.csv"
        self.done: set[str] = set()
        # An empty file is a run that died before writing anything, not a resume:
        # appending to it would skip the header and leave headerless CSVs.
        append = resume and all(f.exists() and f.stat().st_size > 0
                                for f in (self.battles_csv, self.plays_csv))
        if append:
            with self.battles_csv.open(newline="") as f:
                self.done = {r["replay_tag"] for r in csv.DictReader(f) if r.get("replay_tag")}
        self._bf = self.battles_csv.open("a" if append else "w", newline="")
        self._pf = self.plays_csv.open("a" if append else "w", newline="")
        self._bw = csv.DictWriter(self._bf, BATTLE_FIELDS, extrasaction="ignore")
        self._pw = csv.DictWriter(self._pf, PLAY_FIELDS, extrasaction="ignore")
        if not append:
            self._bw.writeheader()
            self._pw.writeheader()
        self.battles = 0
        self.plays = 0

    def write(self, b: dict, stats: dict, plays: list[dict]) -> bool:
        """False if this replay_tag is already on disk. Two roster players who
        fought each other yield the same battle from both sides, so writing has
        to be idempotent or the CSVs gain duplicate keys and plays.csv
        double-counts that battle's placements."""
        if b["replay_tag"] in self.done:
            return False
        self._bw.writerow({**b, **stats, "plays": len(plays)})
        self._pw.writerows(plays)
        self._bf.flush()
        self._pf.flush()
        self.battles += 1
        self.plays += len(plays)
        self.done.add(b["replay_tag"])
        return True

    def close(self) -> None:
        self._bf.close()
        self._pf.close()

    def __enter__(self) -> "Sink":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
