"""Work coordinator for distributed replay collection.

One person runs this. Volunteers run the client, which asks for work, fetches from its own
IP, and posts results back. The coordinator owns three things the clients cannot:

  * the set of replay tags already collected, so the same battle is never fetched twice
  * the deck filter, so the operator decides what is worth collecting without redeploying
  * the leases, so a volunteer that disappears does not strand its work

Standard library only - sqlite3 and http.server - so running it needs no install.

    python3 coordinator/server.py --db work.db --token SECRET --port 8787

Volunteers are untrusted. Every submission is validated and attributed, a shared token keeps
strangers from writing to the queue at all, and nothing a client sends is executed.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LEASE_SECONDS = 900
IDLE_GAP = 600           # a gap over this is treated as "the machine was off", not working          # a volunteer that goes quiet for 15 minutes loses its unit

SCHEMA = """
CREATE TABLE IF NOT EXISTS unit (
  id          INTEGER PRIMARY KEY,
  kind        TEXT NOT NULL,              -- 'player' or 'deck'
  target      TEXT NOT NULL,              -- player tag, or deck signature
  rating      INTEGER,                    -- known rating, so rating filters work per player
  state       TEXT NOT NULL DEFAULT 'open',   -- open | leased | done
  leased_by   TEXT,
  leased_at   REAL,
  attempts    INTEGER NOT NULL DEFAULT 0,
  UNIQUE(kind, target)
);
CREATE INDEX IF NOT EXISTS unit_open ON unit(state, attempts);

-- The dedup ledger. A replay tag here is never handed out or accepted again.
CREATE TABLE IF NOT EXISTS seen (
  replay_tag  TEXT PRIMARY KEY,
  worker      TEXT,
  at          REAL
);

CREATE TABLE IF NOT EXISTS battle (
  replay_tag  TEXT PRIMARY KEY,
  payload     TEXT NOT NULL,
  worker      TEXT,
  at          REAL
);

-- Operator-controlled filter. Empty allow list means "allow everything".
CREATE TABLE IF NOT EXISTS deck_rule (
  card        TEXT PRIMARY KEY,
  rule        TEXT NOT NULL               -- 'allow' or 'block'
);

-- Operator knobs, adjustable while everything is running. Clients read them at the start
-- of every unit, so a change reaches every volunteer within a minute or two.
CREATE TABLE IF NOT EXISTS setting (
  key         TEXT PRIMARY KEY,
  value       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS worker (
  name        TEXT PRIMARY KEY,
  first_seen  REAL,
  last_seen   REAL,
  submitted   INTEGER NOT NULL DEFAULT 0,
  rejected    INTEGER NOT NULL DEFAULT 0,
  -- Time actually spent working, not wall-clock since first contact. A volunteer who runs
  -- an hour a night for a week would otherwise look a hundred times slower than they are.
  active_secs REAL NOT NULL DEFAULT 0,
  units_done  INTEGER NOT NULL DEFAULT 0
);
"""


class Store:
    def __init__(self, path):
        # RLock, not Lock: submit() holds it and then calls wanted() -> deck_rules(),
        # which takes it again. A plain Lock deadlocks there.
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        # CREATE TABLE IF NOT EXISTS leaves an existing table alone, so a database made by
        # an older build keeps its old columns. Add anything missing rather than making the
        # operator rebuild and lose the dedup ledger.
        have = {r[1] for r in self.db.execute("PRAGMA table_info(unit)")}
        if "rating" not in have:
            self.db.execute("ALTER TABLE unit ADD COLUMN rating INTEGER")
        havew = {r[1] for r in self.db.execute("PRAGMA table_info(worker)")}
        for col, decl in (("active_secs", "REAL NOT NULL DEFAULT 0"),
                          ("units_done", "INTEGER NOT NULL DEFAULT 0")):
            if col not in havew:
                self.db.execute(f"ALTER TABLE worker ADD COLUMN {col} {decl}")
        self.db.commit()

    def add_units(self, kind, targets, ratings=None):
        ratings = ratings or {}
        with self.lock:
            self.db.executemany(
                "INSERT OR IGNORE INTO unit(kind, target, rating) VALUES (?, ?, ?)",
                [(kind, t, ratings.get(t)) for t in targets])
            self.db.commit()
            return self.db.total_changes

    def settings(self):
        with self.lock:
            rows = self.db.execute("SELECT key, value FROM setting").fetchall()
        return {k: v for k, v in rows}

    def set_setting(self, key, value):
        with self.lock:
            if value in ("", None):
                self.db.execute("DELETE FROM setting WHERE key=?", (key,))
            else:
                self.db.execute("INSERT OR REPLACE INTO setting(key,value) VALUES (?,?)",
                                (key, str(value)))
            self.db.commit()

    def claim(self, worker, n=1):
        """Hand out open units, reclaiming any whose lease has expired."""
        cutoff = time.time() - LEASE_SECONDS
        with self.lock:
            self.db.execute(
                "UPDATE unit SET state='open', leased_by=NULL "
                "WHERE state='leased' AND leased_at < ?", (cutoff,))
            minr = self.settings().get("min_rating")
            if minr:
                rows = self.db.execute(
                    "SELECT id, kind, target, rating FROM unit WHERE state='open' "
                    "AND attempts < 5 AND (rating IS NULL OR rating >= ?) "
                    "ORDER BY attempts, id LIMIT ?", (int(minr), n)).fetchall()
            else:
                rows = self.db.execute(
                    "SELECT id, kind, target, rating FROM unit "
                    "WHERE state='open' AND attempts < 5 ORDER BY attempts, id LIMIT ?",
                    (n,)).fetchall()
            now = time.time()
            for uid, _, _, _ in rows:
                self.db.execute(
                    "UPDATE unit SET state='leased', leased_by=?, leased_at=?, "
                    "attempts=attempts+1 WHERE id=?", (worker, now, uid))
            self.db.execute(
                "INSERT INTO worker(name, first_seen, last_seen) VALUES (?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET last_seen=excluded.last_seen",
                (worker, now, now))
            self.db.commit()
        return [{"unit_id": r[0], "kind": r[1], "target": r[2], "rating": r[3]}
                for r in rows]

    def known(self, tags):
        """Which of these replay tags have already been collected."""
        with self.lock:
            out = set()
            for i in range(0, len(tags), 500):
                chunk = tags[i:i + 500]
                q = ",".join("?" * len(chunk))
                out.update(r[0] for r in self.db.execute(
                    f"SELECT replay_tag FROM seen WHERE replay_tag IN ({q})", chunk))
            return out

    def deck_rules(self):
        with self.lock:
            rows = self.db.execute("SELECT card, rule FROM deck_rule").fetchall()
        allow = {c for c, r in rows if r == "allow"}
        block = {c for c, r in rows if r == "block"}
        return allow, block

    def wanted(self, battle):
        """Every operator filter, in one place. Returns None to keep, or a reason to drop.

        `submit` is the authority - a client that ignores the rules, or is running an old
        copy of them, still cannot put unwanted battles in the dataset.
        """
        st = self.settings()
        allow, block = self.deck_rules()

        cards = set(battle.get("cards") or ())
        if cards & block:
            return "blocked card"
        if allow and not (cards & allow):
            return "no allowed card"

        since = st.get("since_timestamp")
        if since:
            try:
                if int(battle.get("battle_timestamp") or 0) < int(since):
                    return "too old"
            except (TypeError, ValueError):
                pass

        minr = st.get("min_rating")
        if minr:
            try:
                if int(battle.get("rating") or 0) < int(minr):
                    return "rating too low"
            except (TypeError, ValueError):
                pass

        minp = st.get("min_plays")
        if minp:
            # Top-ladder matches where nobody deploys teach nothing and waste a conversion
            # slot, so they are dropped here rather than discovered by the engine later.
            if len(battle.get("plays") or ()) < int(minp):
                return "too few plays"

        types = st.get("battle_types")
        if types:
            ok = {t.strip() for t in types.split(",") if t.strip()}
            if (battle.get("battle_type") or "") not in ok:
                return "wrong battle type"
        return None

    def submit(self, worker, unit_id, battles):
        kept = dup = filtered = 0
        now = time.time()
        with self.lock:
            for b in battles:
                tag = (b or {}).get("replay_tag")
                if not isinstance(tag, str) or not tag:
                    continue
                if self.db.execute("SELECT 1 FROM seen WHERE replay_tag=?",
                                   (tag,)).fetchone():
                    dup += 1
                    continue
                if self.wanted(b) is not None:
                    filtered += 1
                    # Deliberately NOT marked as seen. Marking filtered battles saves a
                    # refetch, but it also means a wrong or later-relaxed filter loses them
                    # permanently - which is exactly what a rating bug did here once.
                    continue
                self.db.execute(
                    "INSERT OR IGNORE INTO seen(replay_tag, worker, at) VALUES (?,?,?)",
                    (tag, worker, now))
                self.db.execute(
                    "INSERT OR REPLACE INTO battle(replay_tag, payload, worker, at) "
                    "VALUES (?,?,?,?)", (tag, json.dumps(b), worker, now))
                kept += 1
            if unit_id is not None:
                self.db.execute("UPDATE unit SET state='done' WHERE id=?", (unit_id,))
                self.db.execute(
                    "UPDATE worker SET units_done=units_done+1 WHERE name=?", (worker,))
            # A gap longer than IDLE_GAP means the machine was off or asleep; anything
            # shorter counts as time spent collecting.
            prev = self.db.execute("SELECT last_seen FROM worker WHERE name=?",
                                   (worker,)).fetchone()
            gap = now - prev[0] if prev and prev[0] else 0.0
            add = gap if 0 < gap <= IDLE_GAP else 0.0
            self.db.execute(
                "UPDATE worker SET submitted=submitted+?, last_seen=?, "
                "active_secs=active_secs+? WHERE name=?", (kept, now, add, worker))
            self.db.commit()
        return {"kept": kept, "duplicate": dup, "filtered": filtered}

    def workers(self):
        """Per-device totals and rate, so the operator can see who is collecting what."""
        with self.lock:
            rows = self.db.execute(
                "SELECT name, submitted, units_done, active_secs, first_seen, last_seen "
                "FROM worker ORDER BY submitted DESC").fetchall()
        out = []
        for name, sub, units, active, first, last in rows:
            hours = (active or 0) / 3600.0
            out.append({
                "device": name,
                "battles": sub,
                "players_done": units,
                "active_hours": round(hours, 2),
                "battles_per_hour": round(sub / hours, 1) if hours > 0.01 else None,
                "first_seen": first,
                "last_seen": last,
            })
        return out

    def stats(self):
        with self.lock:
            row = lambda q: self.db.execute(q).fetchone()[0]
            return {
                "units_open": row("SELECT COUNT(*) FROM unit WHERE state='open'"),
                "units_leased": row("SELECT COUNT(*) FROM unit WHERE state='leased'"),
                "units_done": row("SELECT COUNT(*) FROM unit WHERE state='done'"),
                "battles": row("SELECT COUNT(*) FROM battle"),
                "seen": row("SELECT COUNT(*) FROM seen"),
                "workers": row("SELECT COUNT(*) FROM worker"),
            }


def make_handler(store, token):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):                  # quiet; the stats endpoint is the log
            pass

        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _auth(self):
            if self.headers.get("X-Token") == token:
                return True
            self._send(401, {"error": "bad token"})
            return False

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 64 * 1024 * 1024:                # a volunteer cannot exhaust memory
                raise ValueError("payload too large")
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            if self.path == "/stats":
                return self._send(200, store.stats())
            if self.path == "/workers":
                return self._send(200, {"workers": store.workers()})
            if self.path == "/config":
                # Clients fetch this before working, so the operator's card rules are applied
                # BEFORE a replay is fetched rather than after it is submitted. Changing the
                # rules here changes what every volunteer collects, with nothing to reinstall.
                allow, block = store.deck_rules()
                cfg = dict(store.settings())
                cfg["allow"] = sorted(allow)
                cfg["block"] = sorted(block)
                return self._send(200, cfg)
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._auth():
                return
            try:
                body = self._body()
            except Exception as exc:
                return self._send(400, {"error": f"bad body: {exc}"})

            worker = str(body.get("worker") or "anon")[:64]

            if self.path == "/claim":
                n = max(1, min(int(body.get("n") or 1), 20))
                return self._send(200, {"units": store.claim(worker, n)})

            if self.path == "/known":
                tags = [t for t in (body.get("tags") or []) if isinstance(t, str)][:5000]
                return self._send(200, {"known": sorted(store.known(tags))})

            if self.path == "/queue":
                # Operator-only in practice: it needs the token, same as everything else.
                rows = body.get("players") or []
                if not isinstance(rows, list) or len(rows) > 20000:
                    return self._send(400, {"error": "players must be a list under 20000"})
                tags, ratings = [], {}
                for r in rows:
                    t = (r or {}).get("tag") if isinstance(r, dict) else r
                    if not isinstance(t, str) or not t:
                        continue
                    t = t.lstrip("#").upper()
                    tags.append(t)
                    if isinstance(r, dict) and r.get("rating") is not None:
                        try:
                            ratings[t] = int(r["rating"])
                        except (TypeError, ValueError):
                            pass
                before = store.stats()["units_open"] + store.stats()["units_done"] \
                    + store.stats()["units_leased"]
                store.add_units("player", tags, ratings)
                after = store.stats()["units_open"] + store.stats()["units_done"] \
                    + store.stats()["units_leased"]
                return self._send(200, {"added": after - before, "seen_in_request": len(tags)})

            if self.path == "/submit":
                battles = body.get("battles") or []
                if not isinstance(battles, list) or len(battles) > 5000:
                    return self._send(400, {"error": "battles must be a list under 5000"})
                unit = body.get("unit_id")
                return self._send(200, store.submit(worker, unit, battles))

            self._send(404, {"error": "not found"})
    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="work.db")
    ap.add_argument("--token", required=True, help="shared secret volunteers must send")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--add-players", default="", help="file of player tags, one per line")
    ap.add_argument("--allow", default="", help="comma-separated cards to collect")
    ap.add_argument("--block", default="", help="comma-separated cards to never collect")
    ap.add_argument("--stats", action="store_true", help="print stats and exit")
    ap.add_argument("--since", default="", help="only battles on/after this date, YYYY-MM-DD")
    ap.add_argument("--min-rating", default="", help="skip players below this rating")
    ap.add_argument("--min-plays", default="",
                    help="drop battles with fewer card plays than this - use 4 or so to "
                         "throw away the empty top-ladder matches where nobody deploys")
    ap.add_argument("--battle-types", default="",
                    help="comma-separated battle_type values to keep; empty keeps all")
    ap.add_argument("--show", action="store_true", help="print the current filters and exit")
    ap.add_argument("--rates", action="store_true",
                    help="print per-device collection rates and exit")
    args = ap.parse_args()

    store = Store(args.db)

    for spec, rule in ((args.allow, "allow"), (args.block, "block")):
        cards = [c.strip() for c in spec.split(",") if c.strip()]
        if cards:
            with store.lock:
                store.db.executemany(
                    "INSERT OR REPLACE INTO deck_rule(card, rule) VALUES (?,?)",
                    [(c, rule) for c in cards])
                store.db.commit()
            print(f"{rule}: {', '.join(cards)}")

    import datetime as _dt
    if args.since:
        ts = int(_dt.datetime.strptime(args.since, "%Y-%m-%d").timestamp())
        store.set_setting("since_timestamp", ts)
        print(f"since: {args.since} ({ts})")
    for key, val in (("min_rating", args.min_rating), ("min_plays", args.min_plays),
                     ("battle_types", args.battle_types)):
        if val:
            store.set_setting(key, val)
            print(f"{key}: {val}")

    if args.rates:
        import datetime as _d
        rows = store.workers()
        if not rows:
            print("no devices have collected anything yet")
            return
        print(f"{'device':32s} {'battles':>8s} {'players':>8s} {'hours':>7s} {'per hour':>9s}"
              f"  last seen")
        for w in rows:
            seen = _d.datetime.fromtimestamp(w["last_seen"]).strftime("%m-%d %H:%M") \
                if w["last_seen"] else "-"
            rate = f"{w['battles_per_hour']:.0f}" if w["battles_per_hour"] else "-"
            print(f"{w['device'][:32]:32s} {w['battles']:8d} {w['players_done']:8d} "
                  f"{w['active_hours']:7.2f} {rate:>9s}  {seen}")
        tot = sum(w["battles"] for w in rows)
        hrs = sum(w["active_hours"] for w in rows)
        print(f"\n{tot} battles from {len(rows)} devices over {hrs:.1f} device-hours"
              + (f" - {tot/hrs:.0f}/hour combined" if hrs > 0.01 else ""))
        return

    if args.show:
        st = dict(store.settings())
        a, b = store.deck_rules()
        if "since_timestamp" in st:
            st["since_readable"] = _dt.datetime.fromtimestamp(
                int(st["since_timestamp"])).strftime("%Y-%m-%d")
        st["allow"] = sorted(a) or "(everything)"
        st["block"] = sorted(b) or "(nothing)"
        print(json.dumps(st, indent=1))
        return

    if args.add_players:
        tags = [t.strip() for t in open(args.add_players) if t.strip()]
        store.add_units("player", tags)
        print(f"queued {len(tags)} player units")

    if args.stats:
        print(json.dumps(store.stats(), indent=1))
        return

    srv = ThreadingHTTPServer((args.host, args.port), make_handler(store, args.token))
    print(f"coordinator on {args.host}:{args.port}  db={args.db}")
    print(json.dumps(store.stats()))
    srv.serve_forever()


if __name__ == "__main__":
    main()
