"""Local observatory and bounded AI-AM minds for a private Minecraft server."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Literal
import uuid

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from core.config import load_config
from core.engine import MindEngine
from core.minecraft_inference import MinecraftOllamaBackend
from core.orchestrator import InputRequest, LocalBoundaryMiddleware, RequestModel, invoke
from core.workspace import focus_context
from database.db_manager import MindStore

WEB = Path(__file__).resolve().parents[1] / "minecraft" / "web"
MAX_STEPS = 30
SKILL_TIMEOUT_MS = 20_000
TERMINAL = {"succeeded", "failed", "cancelled", "outcome_unknown"}
SENSORY_FIELDS = ("id", "observation_id", "connected", "alive", "health", "food",
                  "position", "inventory", "visible", "nearby", "chat", "time", "enabled",
                  "actions", "active_action", "reflexes", "recipes", "death", "viewer_error",
                  "oxygen", "dimension", "game_mode", "error")


class BodyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: StrictStr = Field(pattern=r"^[a-z][a-z0-9-]{0,23}$")
    username: StrictStr = Field(pattern=r"^[a-zA-Z0-9_]{3,16}$")
    viewer_port: StrictInt = Field(ge=1024, le=65535)

    @property
    def viewer_url(self):
        return f"http://127.0.0.1:{self.viewer_port}/"


class FishbowlConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    server_host: Literal["127.0.0.1"] = "127.0.0.1"
    server_port: StrictInt = Field(default=25565, ge=1024, le=65535)
    version: Literal["1.21.4"] = "1.21.4"
    bridge_port: StrictInt = Field(default=8766, ge=1024, le=65535)
    agents: list[BodyConfig] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def unique_endpoints(self):
        ports = [self.server_port, self.bridge_port, *(agent.viewer_port for agent in self.agents)]
        if len(ports) != len(set(ports)):
            raise ValueError("Server, bridge and viewer ports must be distinct.")
        if len({agent.id for agent in self.agents}) != len(self.agents):
            raise ValueError("Agent IDs must be distinct.")
        if len({agent.username.casefold() for agent in self.agents}) != len(self.agents):
            raise ValueError("Player names must be distinct.")
        return self


class GoalRequest(RequestModel):
    goal: StrictStr = Field(min_length=1, max_length=2000)
    steps: StrictInt = Field(ge=1, le=MAX_STEPS)


def data_path(path):
    resolved = Path(path).expanduser().resolve()
    if os.name == "nt" and (not resolved.drive or resolved.drive.casefold() == "c:"):
        raise ValueError("Choose a data directory on a drive other than C:.")
    return resolved


def empty_run():
    return {"id": None, "active": False, "status": "idle", "goal": "", "remaining_steps": 0}


class Bridge:
    def __init__(self, port, token):
        if len(token) < 32:
            raise ValueError("Set AIAM_MINECRAFT_TOKEN to the bridge session secret.")
        self.client = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False, follow_redirects=False,
            timeout=5.0, headers={"Authorization": "Bearer " + token},
        )

    async def request(self, method, path, body=None):
        timeout = 20.0 if path.endswith("/start") else 5.0
        response = await self.client.request(method, path, json=body, timeout=timeout)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise RuntimeError("The Minecraft bridge returned an invalid response.")
        return result

    async def aclose(self):
        await self.client.aclose()


class MinecraftMind(MindEngine):
    environment_tool = "minecraft"
    environment_label = "Minecraft"
    environment_result_statuses = {"succeeded", "failed", "cancelled"}

    def __init__(self, config, store, body, bridge, backend=None, inference_slot=None):
        super().__init__(config, store, backend or MinecraftOllamaBackend(config))
        self.body = body
        self.bridge = bridge
        self.path = f"/agents/{body.id}"
        self.observation = {}
        self.cycle_observation = None
        self.sensory_event = None
        self.bridge_error = None
        self.control = asyncio.Lock()
        self.control_generation = 0
        self.inference_slot = inference_slot or asyncio.Lock()
        self.state["background_enabled"] = False
        self.config["background_enabled"] = False

    async def _role(self, role, payload, trace):
        async with self.inference_slot:
            return await super()._role(role, payload, trace)

    def _experiment_envelope(self, create=False, seed=0):
        envelope = self.store.load_experiment()
        if envelope["snapshot"] is None and create:
            return self.store.save_experiment({"run": empty_run()}, envelope["revision"])
        return envelope

    def _is_observation_source(self, event):
        return super()._is_observation_source(event) or (
            event["kind"] == "observation" and event["source"] == "minecraft")

    def experiment_snapshot(self, evaluator=False):
        # Base engine's grid-world model has a separate observation contract.
        return {"initialized": False}

    def view(self):
        run = (self._experiment_envelope()["snapshot"] or {}).get("run", empty_run())
        observation = {**self.observation, "viewer_url": self.body.viewer_url}
        return {
            "id": self.body.id, "name": self.body.username, "observation": observation,
            "run": run, "focus": focus_context(self.state.get("workspace", {})),
            "ledger": self._public_actions(limit=12),
            "recent_events": self.store.list_events(limit=20),
            "active_role": self.state.get("active_role"), "status": self.state["status"],
            "error": self.bridge_error or observation.get("error"),
        }

    async def refresh(self):
        try:
            value = await self.bridge.request("GET", self.path + "/observation")
            if value.get("id") != self.body.id or type(value.get("connected")) is not bool:
                raise RuntimeError("The bridge observation did not match this character.")
        except (httpx.HTTPError, ValueError, RuntimeError):
            self.bridge_error = "Minecraft bridge unavailable."
            self.observation = {"id": self.body.id, "connected": False, "alive": False}
        else:
            self.bridge_error = None
            self.observation = {key: value[key] for key in SENSORY_FIELDS if key in value}
        return self.observation

    async def poll_body(self):
        await self.refresh()
        run = (self._experiment_envelope()["snapshot"] or {}).get("run", empty_run())
        if (self._running and run["active"] and not self._experiment_stop_requested
                and self.observation.get("alive")):
            try:
                await self.bridge.request("POST", self.path + "/heartbeat", {})
            except (httpx.HTTPError, ValueError, RuntimeError):
                self.bridge_error = "The Minecraft body lease could not be renewed."

    async def stop_body(self):
        try:
            await self.bridge.request("POST", self.path + "/stop", {})
        except (httpx.HTTPError, ValueError, RuntimeError):
            self.bridge_error = "Could not confirm that the Minecraft body stopped."

    def cancel_pending_start(self):
        self.control_generation += 1
        self._experiment_stop_requested = True

    async def experiment_pause(self):
        await super().experiment_pause()
        await self.stop_body()

    async def start_goal(self, goal, steps):
        goal = goal.strip()
        if not goal or len(goal) > 2000 or type(steps) is not int or not 1 <= steps <= MAX_STEPS:
            raise ValueError(f"Provide a goal and 1 to {MAX_STEPS} decisions.")
        if not self._running:
            raise RuntimeError("The mind worker is not ready.")
        generation = self.control_generation
        async with self.control:
            await self.experiment_pause()
            try:
                async with self._lock:
                    if generation != self.control_generation or not self._running:
                        raise RuntimeError("The requested start was cancelled.")
                    await self.bridge.request("POST", self.path + "/start", {})
                    observation = await self.refresh()
                    if generation != self.control_generation:
                        raise RuntimeError("The requested start was cancelled.")
                    if not observation.get("connected") or not observation.get("alive"):
                        raise RuntimeError("The character has not spawned. Check the local server, then start again.")
                    envelope = self._experiment_envelope(create=True)
                    run = {"id": uuid.uuid4().hex, "active": True, "status": "running",
                           "goal": goal, "remaining_steps": steps, "initial_steps": steps}
                    event = self.store.append_event(
                        "experiment", goal, source="user", status="pending",
                        metadata={"run_id": run["id"], "domain": "minecraft"})
                    run["pending_event_id"] = event["id"]
                    self.store.save_experiment({"run": run}, envelope["revision"])
                    self._experiment_stop_requested = False
                    self._wake.set()
                    return event
            except BaseException:
                await self.stop_body()
                raise

    def _payload(self, event, memories, workspace, round_number):
        payload = super()._payload(event, memories, workspace, round_number)
        run = (self._experiment_envelope()["snapshot"] or {}).get("run", empty_run())
        observation = self.cycle_observation or self.observation
        enabled = bool(event["kind"] == "experiment" and run["active"]
                       and not self._experiment_stop_requested
                       and event.get("metadata", {}).get("run_id") == run["id"]
                       and observation.get("connected") and observation.get("alive"))
        senses = {key: value for key, value in observation.items() if key not in {"viewer_error", "error"}}
        payload["experiment"] = {"domain": "minecraft", "initialized": bool(observation),
                                 "observation": senses, "run": run}
        payload["rules"]["environment_enabled"] = enabled
        if enabled:
            payload["rules"]["available_actions"].append("environment")
        if self.sensory_event:
            identifier = self.sensory_event["id"]
            payload["experiment"]["observation_event_id"] = identifier
            payload["history"].append({"id": identifier, "kind": "observation", "source": "minecraft",
                                       "content": "The Minecraft observation in this handoff."})
            for field in ("event_ids", "evidence_ids"):
                payload["rules"][field].append(identifier)
        return payload

    async def process_event(self, event):
        try:
            self.cycle_observation = copy.deepcopy(await self.refresh())
            if event["kind"] == "experiment" and not (
                self.cycle_observation.get("connected") and self.cycle_observation.get("alive")
            ):
                self.store.fail_event(event["id"], "The Minecraft body disconnected or died.")
                self._finish_experiment_event(event)
                return
            if self.cycle_observation.get("observation_id"):
                self.sensory_event = self.store.append_event(
                    "observation", json.dumps(self.cycle_observation, ensure_ascii=False),
                    source="minecraft", episode_id=event["id"])
            await super().process_event(event)
        except Exception:
            if event["kind"] == "experiment":
                self._experiment_stop_requested = True
                await self.stop_body()
            raise
        finally:
            self.cycle_observation = None
            self.sensory_event = None
            if event["kind"] == "experiment":
                run = (self._experiment_envelope()["snapshot"] or {}).get("run", empty_run())
                if not run["active"]:
                    await self.stop_body()

    async def _execute_environment_decision(self, event, decision):
        envelope = self._experiment_envelope()
        run = envelope["snapshot"]["run"]
        if self._experiment_stop_requested or not run["active"] or event["metadata"]["run_id"] != run["id"]:
            raise ValueError("The Minecraft run has stopped.")
        skill = decision["environment_action"]
        observation = self.cycle_observation
        if not observation or skill not in observation.get("actions", []):
            raise ValueError("The Minecraft body did not offer that skill.")
        target = decision["target"]
        action_id = uuid.uuid5(uuid.NAMESPACE_URL, f"aiam:minecraft:{self.body.id}:{event['id']}").hex
        sources = [event["id"]]
        if self.sensory_event:
            sources.append(self.sensory_event["id"])
        record = self.store.get_action(action_id)
        if record:
            previous = self._recorded_action(record)
            if previous:
                return previous
            raise RuntimeError("A Minecraft attempt already exists for this decision.")
        record = self.store.propose_action(
            action_id, event["id"], "minecraft", {"action": skill, "target": target},
            prediction={"statement": decision["prediction"],
                        "reported_confidence": decision["prediction_confidence"]},
            source_event_ids=sources)
        if record["status"] == "proposed":
            record = self.store.transition_action(action_id, "authorized")
        if record["status"] != "authorized":
            raise RuntimeError("The previous Minecraft attempt has no confirmed outcome.")
        self.store.transition_action(action_id, "attempted")
        path = self.path + "/actions"
        try:
            try:
                outcome = await self.bridge.request("POST", path, {
                    "action_id": action_id, "skill": skill, "target": target,
                    "observation_id": observation["observation_id"], "timeout_ms": SKILL_TIMEOUT_MS})
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in {400, 409, 422}:
                    raise
                data = exc.response.json()
                rejection = data.get("error") if isinstance(data, dict) else None
                if not isinstance(rejection, str) or "reused" in rejection:
                    raise
                outcome = {"id": action_id, "status": "failed", "success": False,
                           "reason": rejection, "executed": False}
            deadline = time.monotonic() + SKILL_TIMEOUT_MS / 1000 + 5
            while outcome.get("status") == "running" and time.monotonic() < deadline:
                await asyncio.sleep(.25)
                outcome = await self.bridge.request("GET", path + "/" + action_id)
            if (outcome.get("id") != action_id or outcome.get("status") not in TERMINAL
                    or not isinstance(outcome.get("reason"), str)):
                raise RuntimeError("The bridge did not confirm the outcome.")
        except asyncio.CancelledError:
            await self.stop_body()
            self.store.commit_action_outcome(action_id, "outcome_unknown", None,
                                             error="Run interrupted while the skill was in flight.")
            raise
        except (httpx.HTTPError, ValueError, RuntimeError, KeyError) as exc:
            await self.stop_body()
            self.store.commit_action_outcome(action_id, "outcome_unknown", None,
                                             error="Minecraft skill outcome could not be confirmed.")
            raise RuntimeError("Minecraft skill outcome could not be confirmed. Check the world before retrying.") from exc
        status = outcome["status"]
        result = {**outcome, "action": skill, "reason": outcome["reason"][:400]}
        committed = self.store.commit_action_outcome(action_id, status, result)
        # External game effects already happened. Store their receipt independently
        # of the later conversation and run-budget update.
        run["remaining_steps"] -= 1
        interrupted_by_reflex = status == "cancelled" and result["reason"].startswith("reflex_")
        uncertain = status == "outcome_unknown" or (status == "cancelled" and not interrupted_by_reflex)
        if uncertain:
            run.update(active=False, status=status)
        elif run["remaining_steps"] == 0:
            run.update(active=False, status="budget_reached")
        self.store.save_experiment({"run": run}, envelope["revision"])
        if uncertain:
            raise RuntimeError("The Minecraft skill ended without a confirmed completion.")
        return committed["event"], committed["action"]


def backup_mind(source, directory):
    directory = data_path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"mind-{uuid.uuid4().hex}.db"
    with contextlib.closing(sqlite3.connect(source)) as original:
        with contextlib.closing(sqlite3.connect(target)) as backup:
            original.backup(backup)
    return target


def create_app(config, root, token, mind_config=None, *, bridge=None, backend_factory=None):
    root = data_path(root)
    minds = {}
    online = False

    async def poll():
        nonlocal online
        while True:
            await asyncio.gather(*(mind.poll_body() for mind in minds.values()))
            online = any(mind.bridge_error is None for mind in minds.values())
            await asyncio.sleep(1)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        client = bridge or Bridge(config.bridge_port, token)
        tasks = []
        inference_slot = asyncio.Lock()
        poller = None
        try:
            for body in config.agents:
                directory = data_path(root / body.id)
                if not directory.is_relative_to(root):
                    raise ValueError("Agent directory resolves outside the selected data drive.")
                settings = load_config(mind_config, data_dir=directory, demo=False)
                db_path = data_path(directory / "minecraft-chitta.db")
                if db_path.parent != directory:
                    raise ValueError("The mind database resolves outside its character directory.")
                settings.update(backend="ollama", background_enabled=False, legacy_db_path=None,
                                experiment_enabled=False, db_path=str(db_path))
                settings["self_model"]["name"] = body.username
                settings["self_model"]["capabilities"] += ["bounded Minecraft skills during an explicit run"]
                store = MindStore(settings["db_path"])
                try:
                    backend = backend_factory(settings) if backend_factory else None
                    mind = MinecraftMind(settings, store, body, client, backend, inference_slot)
                except BaseException:
                    store.close()
                    raise
                minds[body.id] = mind
                tasks.append(asyncio.create_task(mind.run_forever()))
            await asyncio.gather(*(mind.ready.wait() for mind in minds.values()))
            for mind in minds.values():
                if mind.startup_error:
                    raise mind.startup_error
            poller = asyncio.create_task(poll())
            yield
        finally:
            if poller:
                poller.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await poller
            await asyncio.gather(*(mind.stop_body() for mind in minds.values()), return_exceptions=True)
            await asyncio.gather(*(mind.stop() for mind in minds.values()), return_exceptions=True)
            await asyncio.gather(*tasks, return_exceptions=True)
            for mind in minds.values():
                mind.store.close()
            minds.clear()
            await client.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(LocalBoundaryMiddleware, max_body_bytes=16_384)

    def get_mind(agent_id):
        if agent_id not in minds:
            raise HTTPException(404, "Unknown Minecraft character.")
        return minds[agent_id]

    @app.get("/api/fishbowl")
    async def fishbowl():
        return {"agents": [mind.view() for mind in minds.values()],
                "bridge_online": online, "defaults": {"max_steps": MAX_STEPS}}

    @app.post("/api/agents/{agent_id}/run", status_code=202)
    async def run(agent_id: str, request: GoalRequest):
        mind = get_mind(agent_id)
        return await invoke(lambda: mind.start_goal(request.goal, request.steps))

    @app.post("/api/agents/{agent_id}/stop")
    async def stop(agent_id: str, request: RequestModel):
        mind = get_mind(agent_id)
        mind.cancel_pending_start()
        async with mind.control:
            await mind.experiment_pause()
        if mind.bridge_error:
            raise HTTPException(503, mind.bridge_error)
        return mind.view()

    @app.post("/api/agents/{agent_id}/message", status_code=202)
    async def message(agent_id: str, request: InputRequest):
        mind = get_mind(agent_id)
        mind.cancel_pending_start()
        async with mind.control:
            return await invoke(lambda: mind.submit(request.text))

    @app.post("/api/agents/{agent_id}/checkpoint")
    async def checkpoint(agent_id: str, request: RequestModel):
        mind = get_mind(agent_id)
        directory = data_path(root / agent_id / "checkpoints")
        if not directory.is_relative_to(root):
            raise HTTPException(400, "Checkpoint directory resolves outside the data directory.")
        path = await asyncio.to_thread(backup_mind, mind.store.db_path, directory)
        return {"checkpoint": str(path), "scope": "mind"}

    @app.get("/")
    async def index():
        return FileResponse(WEB / "index.html", headers={
            "Content-Security-Policy": "default-src 'self'; frame-src http://127.0.0.1:*; "
                                       "object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        })

    app.mount("/static", StaticFiles(directory=WEB, check_dir=False), name="static")
    return app


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--mind-config", type=Path)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    config = FishbowlConfig.model_validate_json(args.config.read_text(encoding="utf-8-sig"))
    if not 1024 <= args.port <= 65535 or args.port in {
        config.server_port, config.bridge_port, *(body.viewer_port for body in config.agents)
    }:
        parser.error("Choose a distinct supervisor port between 1024 and 65535.")
    token = os.environ.get("AIAM_MINECRAFT_TOKEN", "")
    if len(token) < 32:
        parser.error("Set AIAM_MINECRAFT_TOKEN to the bridge session secret.")
    app = create_app(config, args.data_dir, token, args.mind_config)
    uvicorn.run(app, host="127.0.0.1", port=args.port, proxy_headers=False)


if __name__ == "__main__":
    main()
