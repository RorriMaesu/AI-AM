"""Chromium contract smoke for the fishbowl UI using synthetic supervisor telemetry.

This does not connect to Minecraft, load an LLM, or establish gameplay behavior.
Run: python tests/minecraft_browser_smoke.py --artifacts /path/to/test-artifacts
"""
from __future__ import annotations

import argparse
import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "minecraft" / "web"
ATTACK = '<img src=x onerror="window.__injected = true">'
LOCK = threading.Lock()
STATE = {
    "bridge_online": True,
    "defaults": {"max_steps": 30},
    "agents": [
        {
            "id": "aiam", "name": "AI-AM",
            "observation": {
                "id": "aiam", "observation_id": "fixture-observation-1",
                "connected": True, "alive": True, "health": 20, "food": 15,
                "position": {"x": 0, "y": 64, "z": -12},
                "enabled": False, "inventory": [{"name": "oak_log", "count": 2}],
                "time": {"day": 1, "time_of_day": 5000, "is_day": True},
                "active_action": None, "reflexes": [], "actions": [],
                "chat": [{"username": "companion", "message": ATTACK}],
            },
            "run": {"active": False, "status": "idle", "goal": "", "remaining_steps": 0},
            "focus": [{"id": "focus-1", "kind": "goal", "content": ATTACK}],
            "ledger": [{"id": "action-unknown", "tool": "minecraft", "parameters": {"skill": "navigate"},
                        "status": "outcome_unknown", "actor": "agent"}],
            "recent_events": [{"id": "reply-1", "kind": "assistant", "content": ATTACK}],
            "status": "idle",
        },
        {
            "id": "companion", "name": "Companion",
            "observation": {"connected": False, "alive": False, "health": 0, "food": None,
                            "position": None, "active_action": None, "reflexes": [], "enabled": False},
            "run": {"active": False, "status": "stopped", "goal": "", "remaining_steps": 0},
            "focus": [], "ledger": [], "recent_events": [{"kind": "death", "content": "Fixture death report."}],
            "status": "stopped",
        },
    ],
}
REQUESTS: list[dict] = []
API_READS = 0
OFFLINE = False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send_body(self, body: bytes, content_type="application/json", status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; frame-src http://127.0.0.1:*; object-src 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        global API_READS
        path = urlsplit(self.path).path
        if path == "/api/fishbowl":
            with LOCK:
                API_READS += 1
                if OFFLINE:
                    self.send_body(b'{"detail":"Fixture offline"}', status=503)
                else:
                    self.send_body(json.dumps(STATE).encode())
            return
        if path == "/viewer/aiam":
            self.send_body(b"<!doctype html><html><head><title>Fixture viewer</title></head><body><p>TEST FIXTURE VIEWER - NOT GAMEPLAY</p></body></html>", "text/html")
            return
        files = {"/": ("index.html", "text/html"), "/static/app.js": ("app.js", "text/javascript"),
                 "/static/style.css": ("style.css", "text/css")}
        if path in files:
            filename, content_type = files[path]
            self.send_body((WEB / filename).read_bytes(), content_type)
        elif path == "/favicon.ico":
            self.send_body(b"", "image/x-icon", status=204)
        else:
            self.send_body(b'{"detail":"Not found"}', status=404)

    def do_POST(self):
        path = urlsplit(self.path).path
        data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
        parts = path.split("/")
        if len(parts) != 5 or parts[1:3] != ["api", "agents"]:
            self.send_body(b'{"detail":"Not found"}', status=404)
            return
        with LOCK:
            agent = next(item for item in STATE["agents"] if item["id"] == parts[3])
            action = parts[4]
            REQUESTS.append({"agent": agent["id"], "action": action, "body": data})
            result = {"ok": True}
            if action == "run":
                agent["run"] = {"active": True, "status": "running", "goal": data["goal"],
                                "remaining_steps": data["steps"]}
                agent["observation"]["enabled"] = True
                agent["observation"]["active_action"] = {"id": "fixture-skill", "skill": "navigate",
                    "status": "running", "source": "mind", "target": {"x": 1, "y": 64, "z": -12}}
            elif action == "stop":
                agent["run"]["active"] = False
                agent["run"]["status"] = "stopped"
                agent["observation"]["enabled"] = False
                agent["observation"]["active_action"] = None
            elif action == "message":
                agent["run"]["active"] = False
                agent["run"]["status"] = "paused"
                agent["recent_events"].append({"id": "operator-1", "kind": "user", "content": data["text"]})
            elif action == "checkpoint":
                result["path"] = "fixture/mind-checkpoint.sqlite3"
            else:
                self.send_body(b'{"detail":"Invalid fixture action"}', status=400)
                return
        self.send_body(json.dumps(result).encode(), status=202 if action in ("run", "message") else 200)


def assert_no_overflow(page):
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "Horizontal page overflow"


def main():
    global OFFLINE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    base = f"http://127.0.0.1:{server.server_port}"
    STATE["agents"][0]["observation"]["viewer_url"] = base + "/viewer/aiam"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    errors, external_requests = [], []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport={"width": 1440, "height": 1050})
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: external_requests.append(request.url)
                    if not request.url.startswith(base + "/") else None)
            try:
                page.goto(base, wait_until="domcontentloaded")
                expect(page.locator("#connection-label")).to_have_text("Local supervisor connected")
                expect(page.locator(".agent-button")).to_have_count(2)
                expect(page.locator("#world-viewer")).to_have_count(1)
                expect(page.locator("#health-value")).to_have_text("20 / 20")
                expect(page.locator("#food-value")).to_have_text("15 / 20")
                expect(page.locator("#position-value")).to_have_text("0.0 / 64.0 / -12.0")
                expect(page.locator("#outcomes-list")).to_contain_text("Outcome not reported.")
                expect(page.locator("#focus-list")).to_contain_text(ATTACK)
                expect(page.locator("#messages-list")).to_contain_text(ATTACK)
                assert not page.locator("#focus-list img, #messages-list img").count()
                assert page.evaluate("window.__injected === undefined")
                assert not REQUESTS, "Viewing the dashboard must not start actions"
                page.evaluate("window.fixtureFrame = document.getElementById('world-viewer')")
                reads_before = API_READS
                page.wait_for_function("() => document.getElementById('observation-age').textContent === 'Just now'")
                expect(page.locator("#world-time")).to_contain_text('"day": 1')
                page.wait_for_timeout(2200)
                assert API_READS > reads_before
                assert page.evaluate("window.fixtureFrame === document.getElementById('world-viewer')"), "Polling recreated iframe"
                with LOCK:
                    STATE["agents"][0]["observation"]["body_session"] = "fixture-reconnected"
                page.locator("#refresh-button").click()
                page.wait_for_function("window.fixtureFrame !== document.getElementById('world-viewer')")
                assert_no_overflow(page)
                page.screenshot(path=str(artifacts / "minecraft-fishbowl-desktop-fixture.png"), full_page=True)

                page.locator("#goal-input").fill("Reach the visible tree.")
                page.locator("#steps-input").fill("3")
                page.locator("#run-button").click()
                expect(page.locator("#run-state")).to_contain_text("RUNNING")
                expect(page.locator("#active-skill")).to_contain_text("Navigate")
                expect(page.locator("#run-button")).to_be_disabled()
                assert REQUESTS[-1] == {"agent": "aiam", "action": "run", "body": {"goal": "Reach the visible tree.", "steps": 3}}
                page.locator("#stop-button").click()
                expect(page.locator("#body-status")).to_have_text("Body controls disabled")
                expect(page.locator("#run-button")).to_be_enabled()
                assert REQUESTS[-1] == {"agent": "aiam", "action": "stop", "body": {}}
                page.locator("#message-input").fill("Please inspect your inventory.")
                page.locator("#message-button").click()
                expect(page.locator("#messages-list")).to_contain_text("Please inspect your inventory.")
                expect(page.locator("#message-input")).to_have_value("")
                assert REQUESTS[-1]["body"] == {"text": "Please inspect your inventory."}
                page.locator("#checkpoint-button").click()
                expect(page.locator("#notice")).to_contain_text("fixture/mind-checkpoint.sqlite3")
                expect(page.locator("#notice")).to_contain_text("game server")

                page.locator('[data-agent-id="companion"]').click()
                expect(page.locator("#selected-name")).to_have_text("Companion")
                expect(page.locator("#health-value")).to_have_text("0 / 20")
                expect(page.locator("#food-value")).to_have_text("—")
                expect(page.locator("#world-viewer")).to_have_count(0)
                expect(page.locator("#run-button")).to_be_enabled()
                expect(page.locator("#run-button")).to_have_text("Reconnect and run")
                expect(page.locator("#outcomes-list")).to_contain_text("Fixture death report.")
                with LOCK:
                    STATE["agents"][1]["observation"]["viewer_url"] = "https://example.com/unsafe"
                page.locator("#refresh-button").click()
                expect(page.locator("#viewer-empty-title")).to_have_text("Viewer URL was not accepted")
                assert not external_requests

                page.locator('[data-agent-id="aiam"]').click()
                with LOCK:
                    STATE["agents"][0]["observation"]["viewer_error"] = "Fixture viewer could not bind port."
                page.locator("#refresh-button").click()
                expect(page.locator("#viewer-empty-title")).to_have_text("Viewer could not start")
                expect(page.locator("#viewer-empty-copy")).to_contain_text("could not bind port")
                expect(page.locator("#world-viewer")).to_have_count(0)
                with LOCK:
                    STATE["agents"][0]["observation"].pop("viewer_error")
                page.locator("#refresh-button").click()
                expect(page.locator("#world-viewer")).to_have_count(1)

                page.set_viewport_size({"width": 390, "height": 844})
                assert_no_overflow(page)
                expect(page.locator("#run-button")).to_be_visible()
                page.screenshot(path=str(artifacts / "minecraft-fishbowl-mobile-fixture.png"), full_page=True)
                OFFLINE = True
                page.locator("#refresh-button").click()
                expect(page.locator("#unavailable")).to_be_visible()
                expect(page.locator("#run-button")).to_be_disabled()
                expect(page.locator("#message-button")).to_be_disabled()
                expect(page.locator("#health-value")).to_have_text("20 / 20")
                assert_no_overflow(page)
                assert not errors, errors
                assert not external_requests, external_requests
                print(json.dumps({"passed": True, "mode": "mocked_supervisor_ui_only",
                                  "checks": ["telemetry", "iframe preserved", "no automatic actions", "run-stop-message-checkpoint",
                                             "agent selection", "unknown and death", "local viewer boundary", "viewer error",
                                             "literal untrusted text", "mobile layout", "offline state"]}, indent=2))
            finally:
                context.tracing.stop(path=str(artifacts / "minecraft-fishbowl-trace.zip"))
                context.close()
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
