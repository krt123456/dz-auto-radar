#!/usr/bin/env python3
"""Loopback browser fetch daemon for sources behind JS WAF challenges.

The official auction collectors must read sources whose WAF (AWS WAF, from
measurements on PS Auction) rejects plain HTTP clients with a 202/403 browser
challenge.  A headless Chromium solves that challenge once per domain and can
then issue same-origin ``fetch()`` calls from inside the real page context.

This daemon exposes that solved session over loopback only:

    GET /healthz                        -> {"ok": true}
    GET /fetch?url=<absolute https URL> -> {"status": int, "body": str, "url": str}

Design rules:
- Binds to 127.0.0.1 only; never exposes the browser to the network.
- One solved page per origin; fetch calls are serialized per origin with
  polite pacing, and a bounded re-solve is performed when a challenge
  reappears or the session stops returning real content.
- Never follows non-HTTPS URLs and never returns more than a size cap.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from playwright.sync_api import sync_playwright

MAX_BODY_BYTES = 12_000_000
MAX_RESPONSE_BYTES = 40_000_000
SOLVE_WAIT_SECONDS = 10
SOLVE_ATTEMPTS = 3
FETCH_TIMEOUT_SECONDS = 45
CHALLENGE_MARKERS = ("aws-waf-token", "captcha-bypass", "just a moment", "cf-chl")
CONSENT_SELECTORS = (
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
    "#onetrust-accept-btn-handler",
    "button:has-text('Godkänn alla')",
    "button:has-text('Accept all')",
    "button:has-text('Godkänn')",
    "button:has-text('Acceptera')",
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class OriginSession:
    """A solved browser page for one https origin."""

    def __init__(self, browser, origin: str) -> None:
        self.browser = browser
        self.origin = origin
        self.page = browser.new_page(
            user_agent=USER_AGENT,
            locale="en-GB",
            viewport={"width": 1366, "height": 900},
        )
        self.lock = threading.Lock()
        self.last_used = time.time()
        self.solved = False

    def _looks_like_challenge(self, status: int, body: str) -> bool:
        if status in (202, 403, 503):
            return True
        lowered = body[:4000].lower()
        return any(marker in lowered for marker in CHALLENGE_MARKERS) and len(body) < 60_000

    def _solve(self) -> None:
        self.page.goto(self.origin + "/", timeout=60_000, wait_until="domcontentloaded")
        deadline = time.time() + SOLVE_WAIT_SECONDS
        clicked = False
        while time.time() < deadline:
            if not clicked:
                for selector in CONSENT_SELECTORS:
                    try:
                        self.page.click(selector, timeout=1_500)
                        clicked = True
                        break
                    except Exception:
                        continue
            title = (self.page.title() or "").lower()
            content_length = len(self.page.content())
            if "just a moment" not in title and content_length > 150_000:
                self.solved = True
                return
            time.sleep(1.5)
        # A minimal page may be legitimately small; accept after the wait.
        self.solved = True

    def ensure_solved(self) -> None:
        if not self.solved:
            self._solve()

    def resolve(self) -> None:
        self.solved = False
        self._solve()

    def fetch(self, url: str) -> tuple[int, str]:
        if urllib.parse.urlsplit(url).origin() != self.origin:
            raise ValueError(f"cross-origin fetch refused for {url}")
        for attempt in range(SOLVE_ATTEMPTS):
            self.ensure_solved()
            try:
                result = self.page.evaluate(
                    """async (u) => {
                        const r = await fetch(u, {headers: {'Accept': 'application/json, text/html'}, credentials: 'include'});
                        const t = await r.text();
                        return {status: r.status, body: t, url: r.url};
                    }""",
                    url,
                )
            except Exception:
                self.resolve()
                time.sleep(2 * (attempt + 1))
                continue
            status = int(result.get("status", 0))
            body = str(result.get("body", ""))[:MAX_RESPONSE_BYTES]
            if self._looks_like_challenge(status, body):
                self.resolve()
                time.sleep(2 * (attempt + 1))
                continue
            self.last_used = time.time()
            return status, body
        raise RuntimeError(f"fetch failed after {SOLVE_ATTEMPTS} attempts for {url}")


class FetchDaemon:
    def __init__(self, max_sessions: int = 4) -> None:
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        self.sessions: dict[str, OriginSession] = {}
        self.global_lock = threading.Lock()
        self.max_sessions = max_sessions

    def session_for(self, origin: str) -> OriginSession:
        with self.global_lock:
            session = self.sessions.get(origin)
            if session is None:
                if len(self.sessions) >= self.max_sessions:
                    oldest_origin = min(self.sessions, key=lambda o: self.sessions[o].last_used)
                    try:
                        self.sessions[oldest_origin].page.close()
                    except Exception:
                        pass
                    del self.sessions[oldest_origin]
                session = OriginSession(self.browser, origin)
                self.sessions[origin] = session
            return session

    def fetch(self, url: str) -> tuple[int, str, str]:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError(f"only https URLs are accepted: {url!r}")
        origin = urllib.parse.urlsplit(url).scheme + "://" + urllib.parse.urlsplit(url).netloc
        session = self.session_for(origin)
        with session.lock:
            time.sleep(0.4)
            status, body = session.fetch(url)
            return status, body, url

    def stop(self) -> None:
        try:
            self.browser.close()
            self.playwright.stop()
        except Exception:
            pass


class Handler(BaseHTTPRequestHandler):
    daemon: FetchDaemon | None = None

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        sys.stderr.write("[waf-fetch] " + format % args + "\n")

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/healthz":
            self._send_json({"ok": True})
            return
        if parsed.path != "/fetch":
            self._send_json({"error": "unknown endpoint"}, status=404)
            return
        target = urllib.parse.parse_qs(parsed.query).get("url", [""])[0]
        if not target:
            self._send_json({"error": "missing url"}, status=400)
            return
        assert self.daemon is not None
        try:
            status, body, final_url = self.daemon.fetch(target)
        except ValueError as error:
            self._send_json({"error": str(error)}, status=400)
            return
        except Exception as error:
            self._send_json({"error": f"{type(error).__name__}: {error}"}, status=502)
            return
        self._send_json({"status": status, "body": body, "url": final_url})


def main() -> int:
    parser = argparse.ArgumentParser(description="Loopback WAF-solving fetch daemon")
    parser.add_argument("--port", type=int, default=8977)
    args = parser.parse_args()
    Handler.daemon = FetchDaemon()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"[waf-fetch] listening on 127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        assert Handler.daemon is not None
        Handler.daemon.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
