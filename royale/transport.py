"""How this scraper talks to RoyaleAPI.

Cloudflare fingerprints the TLS/HTTP2 handshake, so a cf_clearance cookie is
only honoured for the client that earned it. Handing the browser's clearance to
an outside HTTP client no longer works at all -- curl carrying a fresh
clearance is answered 403 on every path, public ones included. So every request
is issued as a fetch() from inside the page that holds the pass.

Pages   headful Chromium, and the fetch engine. Two contexts share one browser:
        `anon` for public pages, `auth` carrying the RoyaleAPI session cookie
        for the login-gated /data/replay. Public crawling stays anonymous, and
        the account is only ever exposed on the requests that require it.

Client  requests, retries and the shared rate Limiter. Sync Playwright is
        pinned to the thread that created it, so fetches cannot be spread over
        a thread pool; concurrency comes from Promise.all inside the page
        instead. That is why the primitive here is get_many, not get.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Callable
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright

from .cookies import SESSION_COOKIE, Session
from .limiter import Limiter

BASE = "https://royaleapi.com"
SKIP = ("image", "font", "media", "stylesheet")  # nothing we parse needs these
RETRIES = 10     # per request, all of them 429 backoffs handed to the Limiter
RENEWALS = 6     # 403s we try to fix by re-solving the challenge before giving up

# Runs in the page, same origin, so the browser attaches that context's cookies
# and uses the handshake the clearance was issued for. One failed URL must not
# sink the batch, hence the per-request catch.
_FETCH_JS = """async (urls) => Promise.all(urls.map(async (u) => {
    try {
        const r = await fetch(u, {credentials: 'include'});
        return [r.status, r.url, await r.text(), r.headers.get('retry-after') || ''];
    } catch (e) {
        return [0, u, String(e), ''];
    }
}))"""


class RateLimited(RuntimeError):
    """429 that survived every backoff."""


class ClearanceExpired(RuntimeError):
    """403: the Cloudflare pass died and re-solving it did not help."""


class AuthError(RuntimeError):
    """Cloudflare or the login gate turned us away for good."""


class Pages:
    """The browser. Single-threaded: playwright's sync API is."""

    def __init__(self, session: Session | None = None):
        if sys.platform.startswith("linux") and not (
                os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            raise AuthError("no display: the browser must run headful to clear Cloudflare")
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=False, args=["--disable-blink-features=AutomationControlled"])
        self.anon = self._context()
        # The pass is bound to IP, UA and handshake, all of which the two contexts
        # share -- so the logged-in one is handed the clearance the anonymous one
        # already earned instead of sitting through a second challenge.
        self.auth = (self._context(session, self._clearance(self.anon))
                     if session else self.anon)

    def _context(self, session: Session | None = None, clearance: list | None = None):
        ctx = self._browser.new_context()
        cookies = list(clearance or [])
        if session:
            cookies.append({"name": SESSION_COOKIE, "value": session.value,
                            "domain": ".royaleapi.com", "path": "/",
                            "secure": True, "httpOnly": True})
        if cookies:
            ctx.add_cookies(cookies)
        page = ctx.new_page()
        page.route("**/*", lambda r: r.abort() if r.request.resource_type in SKIP
                   else r.continue_())
        self._solve(page, ctx)
        return page

    @staticmethod
    def _clearance(page) -> list:
        return [c for c in page.context.cookies() if c["name"] == "cf_clearance"]

    @staticmethod
    def _solve(page, ctx, tries: int = 3) -> None:
        """Park the page on the site, clearing the challenge if one is shown.

        The interstitial usually self-solves in seconds, but under load it can
        stall or hand back a fresh one, so a stall is retried rather than fatal.
        """
        last = ""
        for attempt in range(tries):
            try:
                page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=60_000)
                if page.title().startswith("Just a moment"):
                    page.wait_for_function(
                        "() => !document.title.startsWith('Just a moment')", timeout=60_000)
                if any(c["name"] == "cf_clearance" for c in ctx.cookies()):
                    return
                last = "no cf_clearance cookie after the challenge"
            except Exception as e:  # timeout or navigation error: worth one more go
                last = str(e).split("\n")[0]
            if attempt < tries - 1:
                page.wait_for_timeout(5_000)
        raise AuthError(f"Cloudflare challenge never cleared after {tries} tries ({last}). "
                        "Wait a minute and rerun; if it persists, open royaleapi.com in "
                        "your own browser first.")

    def renew(self, auth: bool) -> None:
        """Re-solve the challenge for one context after a 403."""
        page = self.auth if auth else self.anon
        self._solve(page, page.context)

    def fetch(self, urls: list[str], auth: bool = False) -> list[list]:
        """[status, final_url, body] per URL, fetched concurrently in the page."""
        page = self.auth if auth else self.anon
        return page.evaluate(_FETCH_JS, urls)

    def close(self) -> None:
        self._browser.close()
        self._pw.stop()


def url(path: str, params: dict | None = None) -> str:
    full = path if path.startswith("http") else f"{BASE}{path}"
    return f"{full}?{urlencode(params)}" if params else full


class Client:
    """Rate-limited fetching. Call get_many for anything you have more than one
    of -- a batch is a single round trip into the page and the requests inside
    it genuinely overlap."""

    def __init__(self, pages: Pages, limiter: Limiter | None = None, batch: int = 6):
        self.pages = pages
        self.limiter = limiter or Limiter()
        self.batch = batch
        self._live = batch  # effective burst, shrunk by 429s and rebuilt slowly

    def get_many(self, paths: list[str], auth: bool = False,
                 tick: Callable[[], None] | None = None) -> list[str | Exception]:
        """Bodies in the order asked. A failed request yields its exception in
        place, so one dead page never costs the rest of the batch.

        The Limiter fixes the average rate, but a batch fires at once, and a
        burst is what /data/replay actually objects to. So the burst width is
        itself adaptive: halved on any 429, widened one slot per clean batch.
        `tick` fires once per request that reaches a final answer -- retries do
        not tick, so a progress bar counts real progress.
        """
        out: list[str | Exception] = [None] * len(paths)  # type: ignore[list-item]
        pending = list(enumerate(paths))
        renewals = 0

        def settle(idx: int, value) -> None:
            out[idx] = value
            if tick:
                tick()

        for _ in range(RETRIES):
            if not pending:
                break
            retry: list[tuple[int, str]] = []
            i = 0
            while i < len(pending):
                chunk = pending[i:i + max(1, self._live)]
                i += len(chunk)
                for _slot in chunk:
                    self.limiter.acquire()
                results = self.pages.fetch([url(p) for _, p in chunk], auth)
                blocked, wait = False, 0.0
                for (idx, path), (status, final, body, retry_after) in zip(chunk, results):
                    if status == 429:
                        blocked = True
                        wait = max(wait, float(retry_after or 0) if str(retry_after).isdigit() else 0)
                        retry.append((idx, path))
                    elif status == 403:
                        if renewals < RENEWALS:
                            renewals += 1
                            self.pages.renew(auth)
                            retry.append((idx, path))
                        else:
                            settle(idx, ClearanceExpired(f"403 on {path}"))
                    elif status == 0:
                        retry.append((idx, path))  # network wobble: try again
                    elif status != 200:
                        settle(idx, AuthError(f"HTTP {status} on {path}"))
                    elif "/login" in final and "/login" not in path:
                        settle(idx, AuthError(f"{path} redirected to the login page"))
                    else:
                        settle(idx, body)
                if blocked:
                    self._live = max(1, self._live // 2)
                    self.limiter.blocked(wait)
                else:
                    self._live = min(self.batch, self._live + 1)
                    self.limiter.ok()
            pending = retry

        for idx, path in pending:
            settle(idx, RateLimited(f"{path} never came back cleanly"))
        return out

    def get(self, path: str, params: dict | None = None, auth: bool = False) -> str:
        res = self.get_many([url(path, params)], auth)[0]
        if isinstance(res, Exception):
            raise res
        return res

    def json(self, path: str, params: dict | None = None) -> dict:
        return json.loads(self.get(path, params, auth=True))

    def logged_in(self) -> bool:
        """/me answers 200 for a real session and redirects to /login otherwise."""
        status, final, _, _ = self.pages.fetch([url("/me")], auth=True)[0]
        return status == 200 and "/login" not in final
