"""Browser smoke against the real demo runtime; run explicitly, not by pytest.

CI installs Playwright and Chromium separately. This script never calls Ollama
or downloads a model. Every input is synthetic; artifacts contain no user data.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ROLES = ["manas", "chitta", "ahamkara", "buddhi"]
FIRST = "Remember my observatory key is violet-heron-742."
SECOND = "The violet-heron-742 key is now beside the telescope."


class DemoServer:
    def __init__(self, work: Path, artifacts: Path):
        self.work = work
        self.artifacts = artifacts
        self.process = None
        self.log = None
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"

    def start(self):
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        for name in ("TMP", "TEMP", "TMPDIR", "HF_HOME", "XDG_CACHE_HOME"):
            target = self.work / name.lower()
            target.mkdir(exist_ok=True)
            environment[name] = str(target)
        self.log = (self.artifacts / "server.log").open("a", encoding="utf-8")
        self.process = subprocess.Popen(
            [sys.executable, "-B", str(ROOT / "core" / "orchestrator.py"),
             "--demo", "--data-dir", str(self.work / "mind"), "--port", str(self.port)],
            cwd=ROOT, env=environment, stdout=self.log, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 30
        try:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError(f"Demo server exited with {self.process.returncode}; see server.log")
                try:
                    with urlopen(self.url + "/api/health", timeout=1) as response:
                        health = json.load(response)
                    if health.get("identity") == "ai-am" and health.get("runtime_running"):
                        assert health["backend"] == "demo", health
                        return
                except (URLError, TimeoutError, OSError):
                    pass
                time.sleep(0.1)
            raise RuntimeError("Demo server did not become ready; see server.log")
        except BaseException:
            self.stop()
            raise

    def stop(self):
        if self.process and self.process.poll() is None:
            try:
                request = Request(
                    self.url + "/api/stop", data=b"{}", method="POST",
                    headers={"Content-Type": "application/json", "Origin": self.url},
                )
                with urlopen(request, timeout=2):
                    pass
            except (URLError, TimeoutError, OSError):
                pass
            try:
                self.process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
        if self.log:
            self.log.close()
            self.log = None


def get_json(page, server, endpoint):
    response = page.request.get(server.url + endpoint)
    assert response.ok, f"{endpoint}: HTTP {response.status}"
    return response.json()


def wait_for_cycle(page, server, count):
    page.wait_for_function(
        """async (count) => {
            const response = await fetch('/api/state');
            const state = await response.json();
            return state.status === 'idle' && state.metrics.cycles >= count;
        }""",
        arg=count,
    )
    expect(page.locator("#conversation .message.assistant")).to_have_count(count)
    snapshot = get_json(page, server, "/api/state")
    assert snapshot["backend"] == "demo"
    assert snapshot["metrics"]["errors"] == 0, snapshot["metrics"]
    trace = snapshot["last_trace"]
    assert [entry["role"] for entry in trace] == ROLES, trace
    assert all(entry["result"] for entry in trace), trace
    for role in ROLES:
        expect(page.locator("#faculty-" + role)).to_have_class(
            re.compile(r"\bcomplete\b")
        )
        expect(page.locator("#result-" + role)).not_to_have_text("")
    return snapshot


def submit(page, text):
    page.locator("#message-input").fill(text)
    with page.expect_response(
        lambda response: response.url.endswith("/api/input")
        and response.request.method == "POST"
    ) as received:
        page.locator("#send-button").click()
    response = received.value
    assert response.status == 202, response.text()
    event = response.json()
    expect(page.locator("#message-input")).to_have_value("")
    return event


def assert_no_overflow(page):
    size = page.evaluate(
        "() => ({width: innerWidth, documentWidth: document.documentElement.scrollWidth})"
    )
    assert size["documentWidth"] <= size["width"] + 1, size


def assert_layout(page):
    assert_no_overflow(page)
    # Check the visible document itself, not a clipped screenshot.
    measurement = page.evaluate(
        """() => {
            const input = document.querySelector('#message-input').getBoundingClientRect();
            const send = document.querySelector('#send-button').getBoundingClientRect();
            return {
                width: innerWidth, height: innerHeight,
                documentWidth: document.documentElement.scrollWidth,
                input: {left: input.left, right: input.right, top: input.top, bottom: input.bottom},
                send: {left: send.left, right: send.right, top: send.top, bottom: send.bottom}
            };
        }"""
    )
    assert measurement["documentWidth"] <= measurement["width"] + 1, measurement
    expect(page.locator("#message-input")).to_be_visible()
    expect(page.locator("#send-button")).to_be_visible()
    for name in ("input", "send"):
        box = measurement[name]
        assert box["left"] >= -1 and box["right"] <= measurement["width"] + 1, measurement
        assert box["top"] >= -1 and box["bottom"] <= measurement["height"] + 1, measurement


def check_sources(page, event):
    link = page.locator(
        '.message.assistant .evidence-link[title="' + event["id"] + '"]'
    ).first
    expect(link).to_be_visible()
    link.click()
    expect(page.locator("#detail-dialog")).to_be_visible()
    expect(page.locator("#detail-body")).to_contain_text(event["content"])
    expect(page.locator("#detail-body")).to_contain_text(event["id"])
    page.keyboard.press("Escape")
    expect(page.locator("#detail-dialog")).not_to_be_visible()


def check_memory(page, event):
    page.locator("#memory-search").fill("violet-heron-742")
    memory = page.locator("#memory-list .memory-card").filter(has_text=event["content"])
    expect(memory).to_have_count(1)
    memory.click()
    expect(page.locator("#detail-body")).to_contain_text(event["content"])
    source = page.locator('#detail-body .evidence-link[title="' + event["id"] + '"]')
    expect(source).to_have_count(1)
    source.click()
    expect(page.locator("#detail-title")).to_have_text("The original event")
    expect(page.locator("#detail-body")).to_contain_text(event["id"])
    page.keyboard.press("Escape")



def check_experiment(page, server, artifacts):
    evaluator_requests = []
    page.on("request", lambda request: evaluator_requests.append(request.url)
            if request.url.endswith("/api/experiment/evaluator") else None)
    page.locator("#experiment-button").click()
    expect(page.locator("#world-grid .world-cell")).to_have_count(63)
    expect(page.locator("#observer-toggle")).not_to_be_checked()
    assert not evaluator_requests, "Hidden evaluator truth must not be fetched by ordinary viewing"
    with page.expect_response(lambda response: response.url.endswith("/api/experiment/reset")
                              and response.request.method == "POST") as reset:
        page.locator("#experiment-reset").click()
    assert reset.value.ok, reset.value.text()
    public = get_json(page, server, "/api/experiment")
    assert "world" not in public and not public.get("evaluator_only")
    assert public["observation"]["pose"]["position"] == [2, 3]
    assert "cup" not in [item["id"] for item in public["observation"]["objects"]]
    assert page.locator('#world-grid .world-cell[data-known="false"]').count() > 0
    assert "cup" not in " ".join(page.locator("#world-grid .world-cell").evaluate_all(
        "(cells) => cells.map(cell => cell.getAttribute('aria-label'))")).lower()
    assert_no_overflow(page)
    page.screenshot(path=str(artifacts / "world-agent-view.png"), full_page=True)

    page.locator("#observer-toggle").check()
    expect(page.locator("#world-grid")).to_have_class(re.compile(r"\bevaluator\b"))
    assert evaluator_requests
    assert page.locator('#world-grid .world-cell[data-known="false"]').count() == 0
    expect(page.locator("#observer-notice")).to_be_visible()
    page.screenshot(path=str(artifacts / "world-observer-view.png"), full_page=True)
    page.locator("#observer-toggle").uncheck()
    expect(page.locator("#world-grid")).not_to_have_class(re.compile(r"\bevaluator\b"))

    page.locator(".intervention-card summary").click()
    page.locator("#intervention-x").fill("2")
    page.locator("#intervention-y").fill("2")
    with page.expect_response(lambda response: response.url.endswith("/api/experiment/intervene")
                              and response.request.method == "POST") as changed:
        page.locator("#intervention-form button").click()
    assert changed.value.ok, changed.value.text()
    after_change = get_json(page, server, "/api/experiment")
    assert after_change["observation"] == public["observation"], "An unseen intervention cannot rewrite the agent's observation"

    with page.expect_response(lambda response: response.url.endswith("/api/experiment/step")
                              and response.request.method == "POST") as stepped:
        page.locator('[data-world-action="turn_left"]').click()
    assert stepped.value.ok, stepped.value.text()
    page.wait_for_function("""async () => {
        const view = await (await fetch('/api/experiment')).json();
        return view.observation.pose.heading === 'north';
    }""")
    turned = get_json(page, server, "/api/experiment")
    assert turned["observation"]["pose"]["heading"] == "north"
    key = next(item for item in turned["observation"]["objects"] if item["id"] == "key")
    assert key["position"] == [2, 2]
    expect(page.locator("#experiment-actions")).to_contain_text("User")
    expect(page.locator("#world-status")).to_contain_text("north")
    page.screenshot(path=str(artifacts / "world-revised-belief.png"), full_page=True)

    page.locator("#experiment-goal").fill("Inspect what is currently visible.")
    page.locator("#experiment-steps").fill("1")
    with page.expect_response(lambda response: response.url.endswith("/api/experiment/run")
                              and response.request.method == "POST") as queued:
        page.locator("#experiment-run").click()
    assert queued.value.status == 202, queued.value.text()
    submitted = queued.value.request.post_data_json
    assert set(submitted) == {"goal", "steps"}, "Evaluator state must never be sent to model-run requests"
    page.wait_for_function("""async () => {
        const view = await (await fetch('/api/experiment')).json();
        return view.run && !view.run.active;
    }""", timeout=30000)
    expect(page.locator("#experiment-run")).to_be_enabled()
    return ["partial-observation world", "explicit evaluator view", "hidden intervention preserves prior observation",
            "manual action ledger and revised observation", "bounded demo experiment run"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", required=True, type=Path,
                        help="Directory for synthetic screenshots, trace, and server log.")
    args = parser.parse_args()
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    errors, external_requests, server_errors, contexts, pages = [], [], [], [], []
    checks = []
    successful = False
    temporary_parent = os.environ.get("RUNNER_TEMP") or None

    with tempfile.TemporaryDirectory(prefix="ai-am-browser-", dir=temporary_parent) as work:
        server = DemoServer(Path(work), artifacts)
        server.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                try:
                    def make_page(name, **options):
                        context = browser.new_context(service_workers="block", **options)
                        context.tracing.start(screenshots=True, snapshots=True, sources=True)
                        contexts.append((name, context))

                        def restrict_network(route):
                            if route.request.url.startswith(server.url + "/"):
                                route.continue_()
                            else:
                                external_requests.append(route.request.url)
                                route.abort()
                        context.route("**/*", restrict_network)
                        page = context.new_page()
                        pages.append((name, page))
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.on("response", lambda response: server_errors.append(
                            f"{response.status} {response.url}"
                        ) if response.status >= 500 else None)
                        page.set_default_timeout(15000)
                        page.goto(server.url, wait_until="domcontentloaded")
                        expect(page.locator("#demo-banner")).to_be_visible()
                        expect(page.locator("#pause-button")).to_be_enabled()
                        return page

                    desktop = make_page("desktop", viewport={"width": 1440, "height": 1000})
                    assert_layout(desktop)
                    first = submit(desktop, FIRST)
                    wait_for_cycle(desktop, server, 1)
                    check_sources(desktop, first)
                    check_memory(desktop, first)
                    checks.extend(["four role completion", "event evidence links", "memory search and provenance"])

                    desktop.locator("#pause-button").click()
                    expect(desktop.locator("#pause-button")).to_have_text("Resume")
                    second = submit(desktop, SECOND)
                    # Give the scheduler a chance to run: a paused queue must stay pending.
                    desktop.wait_for_timeout(350)
                    paused = get_json(desktop, server, "/api/state")
                    assert paused["status"] == "paused", paused
                    queued = get_json(desktop, server, "/api/events/" + second["id"])
                    assert queued["status"] == "pending", queued
                    expect(desktop.locator("#conversation .message.assistant")).to_have_count(1)
                    desktop.locator("#pause-button").click()
                    wait_for_cycle(desktop, server, 2)
                    checks.append("pause, queued input, and resume")

                    before = get_json(desktop, server, "/api/events?limit=100")["events"]
                    before_ids = {event["id"] for event in before}
                    assert {first["id"], second["id"]}.issubset(before_ids)
                    replies = [event for event in before if event["kind"] == "assistant"]
                    assert len(replies) == 2
                    for reply in replies:
                        assert [entry["role"] for entry in reply["metadata"]["trace"]] == ROLES

                    desktop.reload(wait_until="domcontentloaded")
                    wait_for_cycle(desktop, server, 2)
                    after_ids = {event["id"] for event in get_json(desktop, server, "/api/events?limit=100")["events"]}
                    assert before_ids == after_ids, "Reload must preserve event identities without duplication"
                    check_sources(desktop, first)
                    check_memory(desktop, first)
                    assert_layout(desktop)
                    desktop.screenshot(path=str(artifacts / "desktop.png"), full_page=True)
                    checks.append("reload preserves events, role state, memories, and evidence")

                    mobile = make_page("mobile", viewport={"width": 390, "height": 844},
                                       is_mobile=True, has_touch=True, device_scale_factor=1)
                    wait_for_cycle(mobile, server, 2)
                    assert_layout(mobile)
                    third = submit(mobile, "Can you recall the violet-heron-742 key?")
                    mobile_state = wait_for_cycle(mobile, server, 3)
                    chitta = next(entry["result"] for entry in mobile_state["last_trace"]
                                  if entry["role"] == "chitta")
                    assert chitta["selected_memory_ids"], "A related earlier memory must be retrieved"
                    latest_reply = mobile.locator("#conversation .message.assistant").last
                    expect(latest_reply).to_contain_text(FIRST)
                    expect(latest_reply).to_contain_text(SECOND)
                    check_sources(mobile, third)
                    assert_layout(mobile)
                    checks.append("mobile chat retrieves earlier source-backed memories")
                    mobile.screenshot(path=str(artifacts / "mobile-dialogue.png"), full_page=True)
                    mobile.locator('.mobile-tabs button[data-panel="identity"]').click()
                    check_memory(mobile, first)
                    assert_no_overflow(mobile)
                    mobile.screenshot(path=str(artifacts / "mobile-memory.png"), full_page=True)
                    mobile.locator('.mobile-tabs button[data-panel="workspace"]').click()
                    expect(mobile.locator("#faculty-manas")).to_be_visible()
                    assert_no_overflow(mobile)
                    mobile.screenshot(path=str(artifacts / "mobile-faculties.png"), full_page=True)
                    mobile.locator('.mobile-tabs button[data-panel="dialogue"]').click()
                    assert_layout(mobile)
                    checks.append("desktop and mobile layout with visible composer")

                    checks.extend(check_experiment(desktop, server, artifacts))
                    mobile.locator('.mobile-tabs button[data-panel="experiment"]').click()
                    expect(mobile.locator("#world-grid")).to_be_visible()
                    assert_no_overflow(mobile)
                    mobile.screenshot(path=str(artifacts / "mobile-world.png"), full_page=True)
                    checks.append("mobile world controls and layout")

                    assert not errors, f"Browser JavaScript errors: {errors}"
                    assert not server_errors, f"Server errors: {server_errors}"
                    assert not external_requests, f"UI requested external assets: {external_requests}"
                    checks.append("no JavaScript errors, server errors, or external asset requests")
                    successful = True
                finally:
                    for name, page in pages:
                        if not page.is_closed():
                            try:
                                page.screenshot(path=str(artifacts / (name + "-final.png")), full_page=True)
                            except Exception:
                                pass
                    for name, context in contexts:
                        try:
                            context.tracing.stop(path=str(artifacts / (name + "-trace.zip")))
                        finally:
                            context.close()
                    browser.close()
        finally:
            server.stop()
            (artifacts / "checks.json").write_text(
                json.dumps({"success": successful, "passed": checks, "javascript_errors": errors,
                            "server_errors": server_errors, "external_requests": external_requests},
                           indent=2) + "\n", encoding="utf-8",
            )
    print("Browser smoke passed: " + "; ".join(checks))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
