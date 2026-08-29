#!/usr/bin/env python3
"""Loopback browser fetch daemon for sources behind JS WAF challenges.

PS Auction (and similar sources) answer plain HTTP clients with a JS WAF
challenge.  A real headless Chromium solves that challenge once per domain and
can then issue same-origin ``fetch()`` calls from inside the real page.

This daemon exposes the solved browser session over loopback only:

    GET /healthz                        -> {"ok": true}
    GET /fetch?url=<absolute https URL> -> {"status": int, "body": str, "url": str}

Threading model: Playwright sync objects may only be touched from the thread
that created them, so ALL browser work runs on one dedicated worker thread fed
by a queue; HTTP handler threads block on a per-request result slot.
"""
from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_RESPONSE_BYTES = 40_000_000
SOLVE_WAIT_SECONDS = 12
SOLVE_ATTEMPTS = 3
POLITE_DELAY_SECONDS = 0.4
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


def looks_like_challenge(status: int, body: str) -> bool:
    if status in (202, 403, 503):
        return True
    lowered = body[:4000].lower()
    return any(marker in lowered for marker in CHALLENGE_MARKERS) and len(body) < 60_000


class BrowserWorker(threading.Thread):
    """Single thread that owns Playwright and processes queued fetch jobs."""

    def __init__(self) -> None:
        super().__init__(daemon=True, name="waf-fetch-browser")
        self.queue: "queue.Queue[tuple[str, dict]]" = queue.Queue()
        self.playwright = None
        self.browser = None
        self.contexts: dict[str, Any] = {}
        self.pages: dict[str, Any] = {}

    def run(self) -> None:
        self.playwright = sync_playwright_start()
        self.browser = self.playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        while True:
            url, result_slot = self.queue.get()
            try:
                result_slot["result"] = self._fetch(url)
            except Exception as error:  # surfaced to the HTTP handler
                result_slot["error"] = f"{type(error).__name__}: {error}"
            finally:
                result_slot["done"].set()

    def _session_for(self, origin: str):
        """Return (context, page) for an origin, creating and solving it."""
        context = self.contexts.get(origin)
        if context is None:
            context = self.browser.new_context(
                user_agent=USER_AGENT,
                locale="en-GB",
                viewport={"width": 1366, "height": 900},
            )
            self.contexts[origin] = context
            page = context.new_page()
            self.pages[origin] = page
        return context, self.pages[origin]

    def _solve(self, page, origin: str) -> None:
        page.goto(origin + "/", timeout=60_000, wait_until="domcontentloaded")
        deadline = time.time() + SOLVE_WAIT_SECONDS
        clicked = False
        while time.time() < deadline:
            if not clicked:
                for selector in CONSENT_SELECTORS:
                    try:
                        page.click(selector, timeout=1_500)
                        clicked = True
                        break
                    except Exception:
                        continue
            title = (page.title() or "").lower()
            try:
                content_length = len(page.content())
            except Exception:
                content_length = 0
            if "just a moment" not in title and content_length > 150_000:
                return
            time.sleep(1.5)

    def _fetch(self, url: str) -> tuple[int, str, str]:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError(f"only https URLs are accepted: {url!r}")
        origin = f"{parts.scheme}://{parts.netloc}"
        context, page = self._session_for(origin)
        for attempt in range(SOLVE_ATTEMPTS):
            self._solve(page, origin)
            try:
                # context.request carries the solved WAF cookies, follows
                # redirects (www/zone redirects included), and is CORS-free.
                response = context.request.get(url, headers={"Accept": "application/json, text/html"}, timeout=45_000)
                status = response.status
                body = response.text()
            except Exception:
                self._solve(page, origin)
                time.sleep(2 * (attempt + 1))
                continue
            if looks_like_challenge(status, body):
                self._solve(page, origin)
                time.sleep(2 * (attempt + 1))
                continue
            time.sleep(POLITE_DELAY_SECONDS)
            return status, body, response.url or url
        raise RuntimeError(f"fetch failed after {SOLVE_ATTEMPTS} attempts for {url}")


def sync_playwright_start():
    from playwright.sync_api import sync_playwright

    manager = sync_playwright()
    return manager.start()


def submit_fetch(worker: BrowserWorker, url: str, timeout: float) -> tuple[int, str, str]:
    result_slot: dict = {"done": threading.Event()}
    worker.queue.put((url, result_slot))
    if not result_slot["done"].wait(timeout=timeout):
        raise RuntimeError(f"browser fetch timed out after {timeout:.0f}s for {url}")
    if "error" in result_slot:
        raise RuntimeError(result_slot["error"])
    return result_slot["result"]


class Handler(BaseHTTPRequestHandler):
    worker: BrowserWorker | None = None
    fetch_timeout: float = 90.0

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
        try:
            status, body, final_url = submit_fetch(self.worker, target, self.fetch_timeout)
        except ValueError as error:
            self._send_json({"error": str(error)}, status=400)
            return
        except Exception as error:
            self._send_json({"error": f"{type(error).__name__}: {error}"}, status=502)
            return
        self._send_json({"status": status, "body": body, "url": final_url})


def main() -> int:
    parser = argparse.ArgumentParser(description="Loopback WAF-solving browser fetch daemon")
    parser.add_argument("--port", type=int, default=8977)
    parser.add_argument("--fetch-timeout", type=float, default=90.0)
    args = parser.parse_args()
    Handler.worker = BrowserWorker()
    Handler.worker.start()
    Handler.fetch_timeout = args.fetch_timeout
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"[waf-fetch] listening on 127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
