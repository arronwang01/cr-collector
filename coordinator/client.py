"""Volunteer client. Asks the coordinator for work, reads RoyaleAPI, sends battles back.

Deliberate differences from `scrape.py`, which is an operator tool and not safe to hand to
strangers:

  * It never reads another browser's cookies. It drives its own Chromium profile in
    ~/.cr-scraper, and the volunteer logs into RoyaleAPI there once. No password is ever
    seen, typed or stored by this program.
  * It takes its card rules from the coordinator, so the operator changes what everybody
    collects without anyone reinstalling anything.
  * It saves progress after every unit and retries forever on network failure, so closing
    the lid, losing wifi or rebooting costs at most the unit in flight.

    python3 coordinator/client.py --server https://example.org --token SECRET
"""
from __future__ import annotations

import argparse
import json
import platform
import time
import urllib.error
import urllib.request
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from royale import pipeline                                   # noqa: E402
from royale.transport import Client, Pages                    # noqa: E402

HOME = Path.home() / ".cr-scraper"
PROFILE = HOME / "browser"
STATE = HOME / "state.json"

# A well-known active player, used only to prove the login works by fetching one replay.
PROBE_PLAYER = "C2J2V29GJ"


class OwnProfilePages(Pages):
    """Pages, but backed by a persistent profile this program owns.

    The base class copies a session cookie out of a browser the volunteer already uses. That
    is the one behaviour that makes a downloadable tool indistinguishable from malware, so
    this replaces it: one Chromium profile in ~/.cr-scraper, the volunteer signs in once, and
    the session lives there like it would in any browser.
    """

    def __init__(self):
        from playwright.sync_api import sync_playwright
        PROFILE.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self._browser = None
        ctx = self._pw.chromium.launch_persistent_context(
            str(PROFILE), headless=False,
            args=["--disable-blink-features=AutomationControlled"])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        self._ctx = ctx
        self._solve(page, ctx)
        self.anon = page
        self.auth = page                 # one logged-in profile serves both

    def close(self):
        try:
            self._ctx.close()
        finally:
            self._pw.stop()


def post(server, token, path, body, retries=0):
    """POST json, retrying forever by default - a volunteer's wifi is not our problem."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"{server.rstrip('/')}{path}", data=data,
                                 headers={"Content-Type": "application/json",
                                          "X-Token": token})
    delay, tries = 3, 0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise SystemExit("the coordinator rejected the token - check --token")
            raise
        except Exception as exc:                              # noqa: BLE001
            tries += 1
            if retries and tries >= retries:
                raise
            print(f"  coordinator unreachable ({exc}); retrying in {delay}s", flush=True)
            time.sleep(delay)
            delay = min(delay * 2, 120)


def get(server, path):
    with urllib.request.urlopen(f"{server.rstrip('/')}{path}", timeout=60) as r:
        return json.loads(r.read())


def load_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:                                          # noqa: BLE001
        return {}


def save_state(s):
    HOME.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, indent=1))


def wanted(cards, allow, block):
    cards = set(cards or ())
    if cards & set(block):
        return False
    return not allow or bool(cards & set(allow))


def ensure_login(client, pages):
    """Open the login page and wait until a real replay fetch succeeds.

    Every attempt to *detect* login was wrong in a different way: navigating to /me threw
    away what the person was typing, the session cookie is set for anonymous visitors too,
    and Cloudflare 403s the request API whether signed in or not.

    So this does not detect anything. It tries the actual work - fetching one replay, which
    is the only login-gated call - and treats success as proof. A test that IS the work
    cannot report a state the work does not have.
    """
    try:
        pages.auth.goto("https://royaleapi.com/login", wait_until="domcontentloaded",
                        timeout=60_000)
    except Exception:                                          # noqa: BLE001
        pass
    print("\n  Log in to RoyaleAPI in the browser window that opened.")
    print("  Take as long as you need - nothing here will interrupt you.")
    print("  This program never sees your password; you are typing it into RoyaleAPI.\n",
          flush=True)

    waited = 0
    while waited < 3600:
        time.sleep(15)
        waited += 15
        try:
            rows = pipeline.player_battles(client, PROBE_PLAYER, max_pages=1)
            rows = [r for r in rows if r.get("replay_tag")]
            if not rows:
                continue                      # nothing to test with; try again shortly
            pipeline.fetch_replay(client, rows[0])
            print("  logged in, thanks. working.\n", flush=True)
            return True
        except Exception:                                      # noqa: BLE001
            if waited % 60 == 0:
                print(f"  still waiting for login ({waited // 60} min)", flush=True)
    return False


class CoordinatorSink:
    """Collects battles from pipeline.replays and posts them in batches.

    pipeline.replays fetches a slice of 24 replays in parallel, which is what makes the
    original scraper roughly four times faster than fetching them one at a time. Reusing it
    rather than reimplementing the loop keeps that speed and its retry behaviour.
    """

    def __init__(self, server, token, name, unit, batch=25):
        self.server, self.token, self.name = server, token, name
        self.unit, self.batch = unit, batch
        self.buf, self.kept = [], 0

    def write(self, b, stats, plays):
        from royale import parse
        battle = {**b, **stats, "plays": plays}
        battle["cards"] = sorted(parse.base_cards(b.get("team_deck", "")))
        if self.unit.get("rating") is not None:
            battle["rating"] = self.unit["rating"]
        self.buf.append(battle)
        if len(self.buf) >= self.batch:
            self.flush()
        return True

    def flush(self, unit_id=None):
        if not self.buf and unit_id is None:
            return
        res = post(self.server, self.token, "/submit",
                   {"worker": self.name, "unit_id": unit_id, "battles": self.buf})
        self.kept += res["kept"]
        if self.buf:
            print(f"    sent {len(self.buf)}: kept {res['kept']}, dup {res['duplicate']}, "
                  f"filtered {res['filtered']}", flush=True)
        self.buf = []


def do_unit(client, server, token, unit, cfg, name):
    """One player's history: list battles, drop what is unwanted or already collected,
    fetch the replay timelines for the rest in parallel, hand them back."""
    tag = unit["target"].lstrip("#")
    allow, block = cfg.get("allow") or [], cfg.get("block") or []

    from royale import parse
    rows = pipeline.player_battles(client, tag)
    keep = [b for b in rows
            if b.get("replay_tag") and wanted(parse.base_cards(b.get("team_deck", "")),
                                              allow, block)]
    tags = [b["replay_tag"] for b in keep]
    known = set(post(server, token, "/known", {"tags": tags, "worker": name})["known"])
    todo = [b for b in keep if b["replay_tag"] not in known]
    print(f"  {tag}: {len(rows)} battles, {len(keep)} match your rules, "
          f"{len(todo)} not yet collected", flush=True)

    sink = CoordinatorSink(server, token, name, unit)
    pipeline.replays(client, todo, sink=sink,
                     on_error=lambda b, e: None)     # one dead replay never stops the rest
    sink.flush(unit_id=unit["unit_id"])              # last batch closes the unit
    print(f"  {tag} done: {sink.kept} new battles collected", flush=True)
    return sink.kept


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--name", default="")
    args = ap.parse_args()

    state = load_state()
    name = args.name or state.get("name") or f"{platform.node()}-{int(time.time()) % 100000}"
    state["name"] = name
    save_state(state)

    print(f"\n  Clash Royale replay collector\n  volunteer id: {name}")
    print(f"  server: {args.server}\n  profile: {PROFILE}\n")

    pages = OwnProfilePages()
    client = Client(pages)
    try:
        if not ensure_login(client, pages):
            raise SystemExit("not logged in - restart when you are ready")
        print("  logged in. working.\n", flush=True)

        total = state.get("collected", 0)
        while True:
            cfg = {}
            try:
                cfg = get(args.server, "/config")
            except Exception:                                  # noqa: BLE001
                pass
            units = post(args.server, args.token, "/claim",
                         {"worker": name, "n": 1})["units"]
            if not units:
                print("  no work available; checking again in 60s", flush=True)
                time.sleep(60)
                continue
            for u in units:
                try:
                    total += do_unit(client, args.server, args.token, u, cfg, name)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:                       # noqa: BLE001
                    print(f"  unit failed ({type(exc).__name__}: {exc}); "
                          f"it returns to the queue automatically", flush=True)
                state["collected"] = total
                save_state(state)
            print(f"  total collected by you: {total}\n", flush=True)
    except KeyboardInterrupt:
        print("\n  stopped. run it again any time - it picks up where it left off.")
    finally:
        try:
            pages.close()
        except Exception:                                      # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
