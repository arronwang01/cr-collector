"""Pure HTML -> data. No network, no state: every function here takes a string.

This is the seam new features hang off -- add a parser, feed it a page the
transport already knows how to fetch.
"""

from __future__ import annotations

import re
from typing import Iterable

from bs4 import BeautifulSoup

TPS = 20  # Clash Royale simulation ticks per second; replay data-t is in ticks

# A card slug carries its upgrade in the suffix: cannon-ev1, musketeer-hero.
# Strip it and you have the base card, which is what "same deck" means here.
VARIANT = re.compile(r"-(?:ev\d+|hero)$")


def base_cards(deck: str) -> frozenset[str]:
    """Deck slug -> its cards with evolution/hero suffixes stripped."""
    return frozenset(VARIANT.sub("", c) for c in deck.split(",") if c)


def is_variation(deck: str, seed: str) -> bool:
    """True if deck is seed with only evo/hero swaps -- no substituted cards."""
    return base_cards(deck) == base_cards(seed)


def has_card(deck: str, card: str) -> bool:
    """True if deck plays `card`, whatever evolution or hero form it is in."""
    return VARIANT.sub("", card) in base_cards(deck)


def plays_exact(deck: str, slug: str) -> bool:
    """True if deck plays exactly this card slug, evolution form and all.

    Deliberately not has_card: that strips -ev1/-hero to ask "which card is
    this", whereas banning evolved Elite Barbarians has to distinguish
    elite-barbarians-ev1 from plain elite-barbarians.
    """
    return slug in deck.split(",")


def has_cards(deck: str, cards: Iterable[str]) -> bool:
    """True if deck plays every one of `cards`, in any evolution or hero form.

    An archetype is often a pair rather than a single card -- PEKKA bridge spam
    is PEKKA *and* Battle Ram; either alone is a different deck.
    """
    return all(has_card(deck, c) for c in cards)


def decks_with_cards(html: str, cards: Iterable[str]) -> list[str]:
    """Deck slugs linked from a /card/<name> page that play all of `cards`.

    The page links plenty of decks; this keeps the ones matching the whole
    signature, so an archetype can be crawled as "whatever people build around
    these" rather than as one fixed eight-card list.
    """
    cards = list(cards)
    found = dict.fromkeys(re.findall(r"/decks/stats/([a-z0-9,\-]+)", html))
    return [d for d in found if has_cards(d, cards)]


def similar_decks(html: str, seed: str) -> list[str]:
    """Deck slugs linked from a /similar page, seed first, order preserved."""
    found = dict.fromkeys(re.findall(r"/decks/stats/([a-z0-9,\-]+)/similar", html))
    found.pop(seed, None)
    return [seed, *found]


def rated_players(html: str) -> list[dict]:
    """Player rows off a /players/ratings page."""
    out = []
    for pc in BeautifulSoup(html, "lxml").select("div.player_container"):
        link = pc.select_one("a[href^='/player/']")
        clan = pc.select_one("a[href^='/clan/']")
        grid = pc.find_parent("div", class_="grid")
        col = grid.select_one("div.four.wide") if grid else None
        # The right column reads "<rating> <wins in 7d>" with icons between.
        nums = re.findall(r"\d+", col.get_text(" ", strip=True)) if col else []
        out.append({
            "rank": pc.select_one("div.rank.item").get_text(strip=True),
            "player_tag": link["href"].rsplit("/", 1)[1],
            "player_name": link.get_text(strip=True),
            "clan_tag": clan["href"].rsplit("/", 1)[1] if clan else "",
            "rating": nums[0] if nums else "",
            "wins_7d": nums[1] if len(nums) > 1 else "",
        })
    return out


def battles(html: str) -> list[dict]:
    """One row per battle that has a replay button."""
    out = []
    for row in BeautifulSoup(html, "lxml").select("div.battle_list_battle"):
        rb = row.select_one("button.replay_button")
        if not rb:
            continue  # no replay recorded for this battle
        mb = row.select_one("button.matchup_button")
        ts = row.select_one(".battle-timestamp-popup")
        classes = set(row.get("class", []))
        out.append({
            "replay_tag": rb["data-replay"],
            "battle_time": ts.get("data-content", "") if ts else "",
            "battle_timestamp": _epoch(row.get("data-timestamp", "")),
            "battle_type": next((c[len("battletype-"):] for c in classes
                                 if c.startswith("battletype-")), ""),
            # RoyaleAPI colours the row: blue win, red loss, otherwise a draw.
            "result": "win" if "blue" in classes else "loss" if "red" in classes else "draw",
            "team_tags": rb.get("data-team-tags", ""),
            "opponent_tags": rb.get("data-opponent-tags", ""),
            "team_crowns": rb.get("data-team-crowns", ""),
            "opponent_crowns": rb.get("data-opponent-crowns", ""),
            "team_deck": mb.get("data-team-deck", "") if mb else "",
            "opponent_deck": mb.get("data-opponent-deck", "") if mb else "",
        })
    return out


def _epoch(raw: str) -> int:
    """data-timestamp is a float string like '1787922986.0'."""
    try:
        return int(float(raw))
    except ValueError:
        return 0


def next_history_page(html: str) -> str | None:
    """The 'older battles' link on a /battles/history page.

    The pager is two anchors distinguished only by their chevron icon; the right
    one carries ?before=<oldest battle, epoch ms>. Absent on the last page.
    """
    for a in BeautifulSoup(html, "lxml").select("a[href*='battles/history?before=']"):
        icon = a.select_one("i.icon")
        if icon and "right" in icon.get("class", []):
            return a["href"]
    return None


ELIXIR_ROWS = ("total", "troop", "building", "spell")

# Placement coordinates are thousandths of a tile on an 18x32 arena.
TILE = 1000


def _elixir(table) -> dict:
    """Rows read 'Total 35 87', 'Troop 24 58', ..., 'Leaked 0.37'.
    Column 1 is the card count, column 2 the elixir spent; keep both for Total,
    since the card count is what the placement parse gets validated against."""
    vals = {}
    for tr in table.select("tr"):
        cells = [td.get_text(strip=True) for td in tr.select("td, th")]
        if len(cells) < 2:
            continue
        key = cells[0].strip().lower()
        if key in ELIXIR_ROWS:
            vals[key] = cells[2] if len(cells) > 2 else cells[1]
            if key == "total" and len(cells) > 2:
                vals["cards"] = cells[1]
        elif key == "leaked":
            vals["leaked"] = cells[1]
    return vals


def _tile(raw: str) -> str:
    """data-x/data-y are thousandths of a tile, or the string 'None' on the
    hero-ability rows, which are events rather than placements."""
    try:
        return f"{int(raw) / TILE:.3f}"
    except (TypeError, ValueError):
        return ""


def markers(d) -> list:
    """The map's placement markers, one per card played.

    Class-list selectors, not an exact class match: real markers come through as
    'blue marker', 'red marker tl raised' and other combinations, and pinning
    the whole attribute silently drops the decorated ones.
    """
    return d.select("div.marker.blue, div.marker.red")


def replay(html: str) -> tuple[dict, list[dict]]:
    """(battle-level stats, one row per card play) from a /data/replay payload.

    Position comes off the map markers (data-x/data-y/data-c/data-t) and the
    hero-ability flag off the timeline cards (data-ability); they are the same
    plays twice over, so they join on (side, card, tick). If a replay ever
    arrives without markers the timeline alone still yields rows, minus
    coordinates.
    """
    d = BeautifulSoup(html, "lxml")
    root = d.select_one(".battle_replay")
    tag = root.get("data-tag", "") if root else ""

    # Same play, two elements: the timeline card carries the ability flag, the
    # marker carries the position. Keyed as a list because a side can replay the
    # same card on the same tick.
    abilities: dict[tuple, list[str]] = {}
    for c in d.select(".replay_timeline .replay_card"):
        key = (c.get("data-s", ""), c.get("data-card", ""), c.get("data-t", "0"))
        abilities.setdefault(key, []).append(c.get("data-ability", ""))

    def row(side: str, card: str, raw_t: str, x: str = "", y: str = "") -> dict:
        tick = int(raw_t or 0)
        got = abilities.get((side, card, raw_t))
        return {
            "replay_tag": tag,
            "play_index": 0,  # filled in below, once the timeline is ordered
            "tick": tick,
            "seconds": round(tick / TPS, 2),
            "side": side,     # blue = the player whose log this is
            "card": card,
            "ability": got.pop(0) if got else "",
            "x": _tile(x),
            "y": _tile(y),
            "x_raw": x if x.lstrip("-").isdigit() else "",
            "y_raw": y if y.lstrip("-").isdigit() else "",
        }

    found = markers(d)
    if found:
        plays = [row("blue" if "blue" in m.get("class", []) else "red",
                     m.get("data-c", ""), m.get("data-t", "0"),
                     m.get("data-x", ""), m.get("data-y", ""))
                 for m in found]
    else:  # no map in this payload: timeline only, no coordinates
        plays = [row(c.get("data-s", ""), c.get("data-card", ""), c.get("data-t", "0"))
                 for c in d.select(".replay_timeline .replay_card")]

    # DOM order groups by lane, not by time: sort so the CSV reads as a timeline.
    plays.sort(key=lambda p: p["tick"])
    for i, p in enumerate(plays):
        p["play_index"] = i

    stats = {}
    for prefix, table in zip(("team", "oppo"), d.select("table.replay_elixir_table")):
        v = _elixir(table)
        for k in (*ELIXIR_ROWS, "leaked"):
            stats[f"{prefix}_elixir_{k}"] = v.get(k, "")
        stats[f"{prefix}_cards"] = v.get("cards", "")
    return stats, plays
