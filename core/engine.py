"""One persistent agent, four functional perspectives, bounded recurrent decisions."""
from __future__ import annotations

import asyncio
import copy
import datetime as dt
import json
import logging
import os
from pathlib import Path
import time
import uuid

from core.config import validate_model_name
from core.contracts import ROLE_SCHEMAS, ROLES
from core.inference import DemoBackend, InferenceError, OllamaBackend
from core.actions import calculate
from core.workspace import initialize_workspace, update_attention, broadcast, focus_context
from core.environment import (new_environment, observe_environment, step_environment,
                              intervene_environment, evaluator_environment)
from core.world_model import new_world_model, predict_action, update_world_model

LOG = logging.getLogger("aiam.engine")

def timestamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()

class RuntimeLease:
    """An OS-owned lock: a second runtime cannot silently share the same mind."""
    def __init__(self, path):
        self.path = str(path) + ".runtime.lock"
        self.handle = None

    def acquire(self):
        self.handle = open(self.path, "a+b")
        try:
            # Windows enforces byte locks on reads too. Inspect file size without
            # reading the byte another process may already own.
            if os.fstat(self.handle.fileno()).st_size == 0:
                self.handle.write(b"0")
                self.handle.flush()
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            self.handle.close()
            self.handle = None
            raise RuntimeError("This memory database is already owned by another AI-AM runtime.")

    def release(self):
        if self.handle:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.handle.close()
                self.handle = None

class MindEngine:
    environment_tool = "simulator"
    environment_label = "Simulation"
    environment_result_statuses = {"succeeded"}

    def __init__(self, config, store, backend=None):
        self.config = copy.deepcopy(config)
        self.store = store
        self.backend = backend or (DemoBackend(config) if config["backend"] == "demo" else OllamaBackend(config))
        self._subscribers = set()
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._running = False
        self.ready = asyncio.Event()
        self.startup_error = None
        self._paused = False
        self._active_task = None
        self._active_event = None
        self._last_activity = time.monotonic()
        self._experiment_stop_requested = True
        self._lease = RuntimeLease(config["db_path"])
        saved = store.load_state()
        if saved is not None and (not isinstance(saved, dict) or saved.get("schema_version") != 2):
            raise ValueError("Unsupported saved mind state; preserve this database and migrate it explicitly.")
        self.state = saved or {
            "schema_version": 2, "state_version": 0, "cycle": 0,
            "self_model": copy.deepcopy(config["self_model"]), "goals": [],
            "workspace": {}, "metrics": {"cycles": 0, "tokens": 0, "latency_ms": 0, "errors": 0},
        }
        # Restored identity/history is durable; transient running flags are not.
        self.state.update(status="idle", active_role=None, current_event=None, backend=config["backend"])
        saved_roles = self.state.get("roles", {})
        for role in ROLES:
            prior = saved_roles.get(role, {}).get("model")
            if prior and config["backend"] != "demo":
                validate_model_name(prior)
                self.config["roles"][role]["model"] = prior
        self.state["roles"] = copy.deepcopy(self.config["roles"])
        self.state["background_enabled"] = bool(self.state.get("background_enabled", config["background_enabled"]))
        self.config["background_enabled"] = self.state["background_enabled"]
        if hasattr(self.backend, "config"):
            self.backend.config = self.config

    def subscribe(self):
        queue = asyncio.Queue(maxsize=128)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue):
        self._subscribers.discard(queue)

    async def _emit(self, event_type, data):
        envelope = {"type": event_type, "data": copy.deepcopy(data), "timestamp": timestamp()}
        for queue in tuple(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(envelope)

    def snapshot(self):
        result = copy.deepcopy(self.state)
        result["recent_events"] = [e for e in self.store.list_events(limit=50)
                                   if not e.get("metadata", {}).get("evaluator_only")]
        result["memory_stats"] = self.store.stats()
        result["backend"] = self.config["backend"]
        result["paused"] = self._paused
        result["action_ledger"] = self._public_actions(limit=20)
        result["experiment"] = self.experiment_snapshot()
        return result

    async def _publish(self):
        await self._emit("state", self.snapshot())

    async def submit(self, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 8000:
            raise ValueError("Enter a message between 1 and 8000 characters.")
        if self.state["status"] == "stopped":
            raise RuntimeError("The runtime is stopped. Restart AI-AM to continue.")
        if self.store.stats().get("pending", 0) >= self.config["max_pending"]:
            raise OverflowError("The input queue is full; wait for pending messages to finish.")
        if self._experiment_envelope()["snapshot"] and self._experiment_envelope()["snapshot"]["run"]["active"]:
            await self.experiment_pause()
        event = self.store.append_event("user", text.strip(), source="user", status="pending")
        self._last_activity = time.monotonic()
        # Foreground input has priority over a low-priority reflection.
        if self._active_task and self._active_event and self._active_event["kind"] == "reflection":
            self._active_task.cancel()
        self._wake.set()
        await self._publish()
        return event

    async def reflect(self, topic=""):
        if not isinstance(topic, str) or len(topic) > 2000:
            raise ValueError("A reflection topic must contain at most 2000 characters.")
        if self.state["status"] == "stopped":
            raise RuntimeError("The runtime is stopped.")
        if self.store.stats().get("pending", 0) >= self.config["max_pending"]:
            raise OverflowError("The input queue is full.")
        if not topic.strip():
            active = [g["description"] for g in self.state.get("goals", []) if g["status"] == "active"]
            questions = self.state.get("workspace", {}).get("uncertainties", [])
            if not active and not questions:
                raise ValueError("Choose a reflection topic or establish a goal first.")
            topic = (active + questions)[0]
        event = self.store.append_event(
            "reflection", topic.strip(), source="scheduler",
            metadata={"imagined": True, "priority": "background"}, status="pending")
        self._wake.set()
        await self._publish()
        return event

    async def retry(self, event_id):
        if self.state["status"] == "stopped":
            raise RuntimeError("Restart the stopped runtime to continue.")
        if self.store.stats().get("pending", 0) >= self.config["max_pending"]:
            raise OverflowError("The input queue is full.")
        self.store.retry_event(event_id)
        self._wake.set()
        await self._publish()
        return self.store.get_event(event_id)

    async def pause(self):
        if self.state["status"] == "stopped":
            raise RuntimeError("Restart the stopped runtime to continue.")
        self._paused = True
        if self._experiment_envelope()["snapshot"] and self._experiment_envelope()["snapshot"]["run"]["active"]:
            await self.experiment_pause()
        if self._active_task and self._active_event and self._active_event["kind"] == "reflection":
            self._active_task.cancel()
        if self.state["status"] != "thinking":
            self.state["status"] = "paused"
        await self._publish()

    async def resume(self):
        if self.state["status"] == "stopped":
            raise RuntimeError("Restart the stopped runtime to continue.")
        self._paused = False
        if self.state["status"] != "thinking":
            self.state["status"] = "idle"
        self._wake.set()
        await self._publish()

    async def set_background(self, enabled):
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        async with self._lock:
            self.config["background_enabled"] = enabled
            self.state["background_enabled"] = enabled
            self.store.save_state(self.state)
        self._wake.set()
        await self._publish()

    async def set_models(self, mapping):
        if not isinstance(mapping, dict) or not mapping or any(r not in ROLES for r in mapping):
            raise ValueError("Choose a valid cognitive role.")
        if self.config["backend"] == "demo":
            raise ValueError("Demo mode uses a scripted provider. Restart without --demo to select local models.")
        for model in mapping.values():
            validate_model_name(model)
        installed = await self.backend.list_models()
        if any(model not in installed for model in mapping.values()):
            raise ValueError("The selected model is not installed locally.")
        async with self._lock:
            for role, model in mapping.items():
                self.config["roles"][role]["model"] = model
            self.state["roles"] = copy.deepcopy(self.config["roles"])
            self.store.save_state(self.state)
        await self._publish()

    async def consolidate(self):
        async with self._lock:
            result = await asyncio.to_thread(self.store.consolidate)
            self.state["last_consolidation"] = {"at": timestamp(), **result, "weights_trained": False}
            self.store.save_state(self.state)
        await self._emit("memory_updated", result)
        await self._publish()
        return result

    async def stop(self):
        self._experiment_stop_requested = True
        self._running = False
        self._wake.set()
        if self._active_task and not self._active_task.done():
            self._active_task.cancel()
            try:
                await self._active_task
            except asyncio.CancelledError:
                pass
        async with self._lock:
            envelope = self._experiment_envelope()
            if envelope["snapshot"]:
                snapshot = copy.deepcopy(envelope["snapshot"])
                snapshot["run"].update(active=False, status="stopped")
                self.store.save_experiment(snapshot, envelope["revision"])
            self._cancel_experiment_events("Runtime stopped; start a new simulation run after restarting.")
        self.state["status"] = "stopped"
        self.state["active_role"] = None
        await self._publish()

    async def run_forever(self):
        owns_lease = False
        try:
            self._lease.acquire()
            owns_lease = True
            legacy = self.config.get("legacy_db_path")
            if (self.config["backend"] != "demo" and legacy and Path(legacy).is_file()
                    and Path(legacy).resolve() != Path(self.config["db_path"]).resolve()):
                await asyncio.to_thread(self.store.import_legacy, legacy)
            self.store.recover_pending()
            envelope = self._experiment_envelope()
            if envelope["snapshot"]:
                snapshot = copy.deepcopy(envelope["snapshot"])
                snapshot["run"].update(active=False, status="paused_after_restart")
                self.store.save_experiment(snapshot, envelope["revision"])
                self._cancel_experiment_events("Restart paused simulation autonomy; start an explicit new run.")
            self._running = True
            await self._publish()
            self.ready.set()
            while self._running:
                if not self._paused:
                    event = self.store.claim_next()
                    if event:
                        self._active_event = event
                        self._active_task = asyncio.create_task(self.process_event(event))
                        try:
                            await self._active_task
                        except asyncio.CancelledError:
                            if not self._running:
                                break
                        finally:
                            self._active_event = None
                            self._active_task = None
                            self._last_activity = time.monotonic()
                        continue
                    if (self.config["background_enabled"] and
                        time.monotonic() - self._last_activity >= self.config["background_interval"]):
                        self._last_activity = time.monotonic()
                        try:
                            await self.reflect()
                        except ValueError:
                            pass
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
        except BaseException as exc:
            if not self.ready.is_set():
                self.startup_error = exc
            raise
        finally:
            self._running = False
            self.ready.set()
            try:
                if self._active_task and not self._active_task.done():
                    self._active_task.cancel()
                    try:
                        await self._active_task
                    except asyncio.CancelledError:
                        pass
                self.state.update(status="stopped", active_role=None, current_event=None)
                if owns_lease:
                    self.store.save_state(self.state)
            finally:
                try:
                    if hasattr(self.backend, "aclose"):
                        await self.backend.aclose()
                finally:
                    self._lease.release()


    def _experiment_available(self):
        if not self.config.get("experiment_enabled", True):
            raise ValueError("The simulation lab is disabled in configuration.")
        if self.state.get("status") == "stopped":
            raise RuntimeError("Restart the stopped runtime to use the simulation.")

    def _experiment_envelope(self, create=False, seed=0):
        envelope = self.store.load_experiment()
        if envelope["snapshot"] is None and create:
            world = new_environment(seed=seed)
            observation = observe_environment(world)
            snapshot = {"world": world, "model": new_world_model(observation),
                        "run": {"active": False, "status": "idle", "goal": "",
                                "remaining_steps": 0, "id": None},
                        "seed": seed, "world_id": uuid.uuid4().hex}
            envelope = self.store.save_experiment(snapshot, envelope["revision"])
        return envelope

    def _public_actions(self, event=None, limit=20):
        records = []
        for action in self.store.list_actions(limit=100):
            if action["tool"] not in {"calculator", self.environment_tool}:
                continue
            # An actual past action remains reportable even if its conversation
            # later failed. Future queued instructions are never action evidence.
            if event and action.get("created_at", "") > event["created_at"] and action["episode_id"] != event["id"]:
                continue
            record = {key: copy.deepcopy(action.get(key)) for key in
                      ("id", "episode_id", "tool", "actor", "parameters", "prediction",
                       "status", "result", "error", "source_event_ids", "outcome_event_id", "created_at")}
            record["event_id"] = action.get("outcome_event_id")
            records.append(record)
            if len(records) >= limit:
                break
        return records

    def _prediction_records(self):
        results = []
        for action in self._public_actions(limit=30):
            if action["tool"] != "simulator":
                continue
            prediction = action.get("prediction") or {}
            forecast = prediction.get("model") or {}
            outcome = action.get("result") or {}
            results.append({"action": action["parameters"].get("action"),
                            "expected": forecast.get("predicted_pose"),
                            "actual": outcome.get("reason"),
                            "confidence": forecast.get("confidence"),
                            "statement": prediction.get("statement"),
                            "status": action["status"], "actor": action["actor"],
                            "event_id": action.get("event_id"), "basis": forecast.get("basis")})
            if len(results) >= 12:
                break
        return results

    def experiment_snapshot(self, evaluator=False):
        envelope = self._experiment_envelope()
        snapshot = envelope["snapshot"]
        empty_run = {"active": False, "status": "idle", "goal": "", "remaining_steps": 0, "id": None}
        if not snapshot:
            return {"enabled": self.config.get("experiment_enabled", True),
                    "initialized": False, "revision": envelope["revision"],
                    "observation": None, "model": {}, "beliefs": {}, "predictions": [],
                    "metrics": {}, "run": empty_run, "actions": [], "focus": []}
        model = copy.deepcopy(snapshot["model"])
        # Never re-observe private world state on a GET. An evaluator may have
        # moved an unseen object; the agent's last observation must remain intact.
        observation = copy.deepcopy(model.get("last_observation"))
        result = {"enabled": self.config.get("experiment_enabled", True),
                  "initialized": True, "revision": envelope["revision"], "world_id": snapshot.get("world_id"),
                  "observation": observation, "model": model,
                  "beliefs": copy.deepcopy(model.get("beliefs", {})),
                  "predictions": self._prediction_records(),
                  "metrics": copy.deepcopy(model.get("calibration", {})),
                  "run": copy.deepcopy(snapshot.get("run", empty_run)),
                  "actions": [a for a in self._public_actions(limit=30) if a["tool"] == "simulator"],
                  "focus": focus_context(self.state.get("workspace", {})),
                  "regulation": {"remaining_steps": snapshot.get("run", {}).get("remaining_steps", 0),
                                 "energy": (observation or {}).get("energy"),
                                 "mode": "bounded simulated resources, not feelings"}}
        if evaluator:
            result["world"] = evaluator_environment(snapshot["world"])
            result["evaluator_only"] = True
        return result

    def _cancel_experiment_events(self, reason):
        for event in self.store.list_events(limit=1000):
            if event["kind"] == "experiment" and event["status"] in {"pending", "processing"}:
                self.store.fail_event(event["id"], reason)

    async def experiment_pause(self):
        # Set the stop signal before waiting for the inference lock.
        self._experiment_stop_requested = True
        task = self._active_task
        if task and self._active_event and self._active_event["kind"] == "experiment" and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        async with self._lock:
            envelope = self._experiment_envelope()
            if envelope["snapshot"]:
                snapshot = copy.deepcopy(envelope["snapshot"])
                snapshot["run"].update(active=False, status="paused")
                self.store.save_experiment(snapshot, envelope["revision"])
            self._cancel_experiment_events("Simulation run paused; start an explicit new run to continue.")
        await self._publish()
        return self.experiment_snapshot()

    async def experiment_reset(self, seed=0):
        self._experiment_available()
        if type(seed) is not int or not 0 <= seed <= 1_000_000:
            raise ValueError("seed must be an integer from 0 to 1000000.")
        await self.experiment_pause()
        async with self._lock:
            envelope = self._experiment_envelope()
            world = new_environment(seed=seed)
            observation = observe_environment(world)
            snapshot = {"world": world, "model": new_world_model(observation),
                        "run": {"active": False, "status": "idle", "goal": "",
                                "remaining_steps": 0, "id": None}, "seed": seed, "world_id": uuid.uuid4().hex}
            self.store.save_experiment(snapshot, envelope["revision"])
            self.store.append_event("experiment_control", "The simulation was reset by the user.",
                                    source="user", metadata={"seed": seed, "simulated": True})
        await self._publish()
        return self.experiment_snapshot()

    def _prepare_action(self, action_id, event, tool, parameters, actor="self", prediction=None):
        record = self.store.propose_action(action_id, event["id"], tool, parameters,
                                           actor=actor, prediction=prediction,
                                           source_event_ids=[event["id"]])
        if record["status"] in {"succeeded", "failed", "cancelled", "outcome_unknown"}:
            return record
        if record["status"] == "proposed":
            record = self.store.transition_action(action_id, "authorized")
        if record["status"] == "authorized":
            record = self.store.transition_action(action_id, "attempted")
        else:
            raise RuntimeError("An attempted action has no confirmed outcome; it cannot be executed again.")
        return record

    def _recorded_action(self, record):
        if record["status"] == "succeeded":
            return self.store.get_event(record["outcome_event_id"]), record
        if record["status"] in {"failed", "cancelled", "outcome_unknown"}:
            raise RuntimeError("This action already ended as " + record["status"] + "; use a new instruction.")
        return None

    def _calculate_action(self, event, expression):
        action_id = uuid.uuid5(uuid.NAMESPACE_URL, "aiam:" + event["id"] + ":calculator:" + expression).hex
        record = self._prepare_action(action_id, event, "calculator", {"expression": expression})
        previous = self._recorded_action(record)
        if previous:
            return previous
        try:
            value = calculate(expression)
        except Exception as exc:
            self.store.commit_action_outcome(action_id, "failed", None, error=str(exc)[:1000])
            raise
        committed = self.store.commit_action_outcome(action_id, "succeeded", value)
        return committed["event"], committed["action"]

    def _environment_action(self, event, action, target="", prediction="", confidence=0.5, actor="self"):
        self._experiment_available()
        if action not in {"look", "turn_left", "turn_right", "move_forward", "inspect", "pick_up", "drop", "wait"}:
            raise ValueError("Unknown simulation action.")
        if not isinstance(target, str) or len(target) > 32:
            raise ValueError("target must be at most 32 characters.")
        if not isinstance(prediction, str) or len(prediction) > 2000:
            raise ValueError("prediction must be at most 2000 characters.")
        if isinstance(confidence, bool) or not isinstance(confidence, (float, int)) or not 0 <= confidence <= 1:
            raise ValueError("confidence must be between zero and one.")
        if actor not in {"self", "user"}:
            raise ValueError("Simulation actions must be attributed to the agent or user.")
        envelope = self._experiment_envelope(create=True)
        snapshot = copy.deepcopy(envelope["snapshot"])
        run = snapshot["run"]
        if actor == "self":
            if (event["kind"] != "experiment" or self._experiment_stop_requested or not run["active"]
                    or event.get("metadata", {}).get("run_id") != run["id"] or run["remaining_steps"] <= 0):
                raise ValueError("No explicit active simulation run authorizes this action.")
        action_id = uuid.uuid5(uuid.NAMESPACE_URL, "aiam:" + event["id"] + ":simulator:" + action + ":" + target).hex
        existing = self.store.get_action(action_id)
        if existing:
            previous = self._recorded_action(existing)
            if previous:
                return previous
            if existing["status"] == "attempted":
                raise RuntimeError("The previous simulation action outcome is unconfirmed.")
        forecast = predict_action(snapshot["model"], action, target or None)
        record = self._prepare_action(action_id, event, "simulator", {"action": action, "target": target, "world_id": snapshot.get("world_id")},
                                      actor=actor, prediction={"model": forecast, "statement": prediction,
                                                              "reported_confidence": float(confidence)})
        previous = self._recorded_action(record)
        if previous:
            return previous
        try:
            stepped = step_environment(snapshot["world"], action, target or None)
            outcome = copy.deepcopy(stepped["outcome"])
            outcome["actor"] = actor
            snapshot["world"] = stepped["state"]
            snapshot["model"] = update_world_model(snapshot["model"], stepped["observation"],
                                                   action=action, outcome=outcome, prediction=forecast)
            if actor == "self":
                run["remaining_steps"] -= 1
                run["last_action_id"] = action_id
                if run["remaining_steps"] <= 0:
                    run.update(active=False, status="budget_reached")
            # Executing an attempted movement is a completed simulator step even
            # when the observed environment outcome says it was blocked.
            committed = self.store.commit_action_outcome(action_id, "succeeded", outcome,
                                                        experiment_snapshot=snapshot,
                                                        expected_revision=envelope["revision"])
        except Exception as exc:
            current = self.store.get_action(action_id)
            if current and current["status"] == "attempted":
                self.store.commit_action_outcome(action_id, "failed", None, error=str(exc)[:1000])
            raise
        return committed["event"], committed["action"]

    async def experiment_step(self, action, target="", prediction="", confidence=0.5, actor="user"):
        self._experiment_available()
        if actor != "user":
            raise ValueError("Manual controls must be attributed to the user.")
        await self.experiment_pause()
        async with self._lock:
            event = self.store.append_event("experiment_command", "User requested simulation action " + str(action),
                                           source="user", metadata={"simulated": True})
            self._environment_action(event, action, target, prediction, confidence, actor="user")
        await self._publish()
        return self.experiment_snapshot()

    async def experiment_intervene(self, change):
        self._experiment_available()
        await self.experiment_pause()
        async with self._lock:
            envelope = self._experiment_envelope(create=True)
            snapshot = copy.deepcopy(envelope["snapshot"])
            snapshot["world"] = intervene_environment(snapshot["world"], change)
            # Deliberately preserve model/last_observation. Interventions are
            # evaluator data and must not tell the agent what happened unseen.
            self.store.save_experiment(snapshot, envelope["revision"])
            self.store.append_event("experiment_intervention", "The evaluator changed the simulated environment.",
                                    source="evaluator", metadata={"evaluator_only": True, "change": change})
        await self._publish()
        return self.experiment_snapshot()

    async def experiment_run(self, goal, steps=5):
        self._experiment_available()
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 2000:
            raise ValueError("Provide a simulation goal of 1 to 2000 characters.")
        maximum = min(self.config.get("experiment_max_steps", 10), self.config.get("experiment_budget", 30))
        if type(steps) is not int or not 1 <= steps <= maximum:
            raise ValueError(f"Choose between 1 and {maximum} simulation steps.")
        await self.experiment_pause()
        async with self._lock:
            if self.store.stats().get("pending", 0) >= self.config["max_pending"]:
                raise OverflowError("The input queue is full.")
            envelope = self._experiment_envelope(create=True)
            snapshot = copy.deepcopy(envelope["snapshot"])
            run_id = uuid.uuid4().hex
            snapshot["run"] = {"id": run_id, "active": True, "status": "running",
                               "goal": goal.strip(), "remaining_steps": steps, "initial_steps": steps}
            event = self.store.append_event("experiment", goal.strip(), source="user",
                                           metadata={"run_id": run_id, "simulated": True}, status="pending")
            snapshot["run"]["pending_event_id"] = event["id"]
            self.store.save_experiment(snapshot, envelope["revision"])
            self._experiment_stop_requested = False
        self._wake.set()
        await self._publish()
        return event

    def _finish_experiment_event(self, event):
        if event["kind"] != "experiment":
            return
        envelope = self._experiment_envelope()
        if not envelope["snapshot"]:
            return
        snapshot = copy.deepcopy(envelope["snapshot"])
        run = snapshot["run"]
        if event.get("metadata", {}).get("run_id") != run["id"]:
            return
        record = self.store.get_event(event["id"])
        executed = [a for a in self.store.list_actions(limit=100)
                    if a["episode_id"] == event["id"] and a["tool"] == self.environment_tool
                    and a["status"] in self.environment_result_statuses]
        if record["status"] != "complete":
            run.update(active=False, status="failed")
        elif not executed:
            run.update(active=False, status="completed")
        elif self._experiment_stop_requested:
            run.update(active=False, status="paused")
        elif run["active"] and run["remaining_steps"] > 0:
            next_event = self.store.append_event("experiment", run["goal"], source="scheduler",
                                                metadata={"run_id": run["id"], "simulated": True}, status="pending")
            run["pending_event_id"] = next_event["id"]
            self._wake.set()
        self.store.save_experiment(snapshot, envelope["revision"])

    def _apply_commitments(self, final, assessment, event, known_event_ids):
        records = copy.deepcopy(self.state["self_model"].get("commitment_records", []))
        if not records:
            for description in self.state["self_model"].get("commitments", []):
                records.append({"id": uuid.uuid5(uuid.NAMESPACE_URL, "aiam:commitment:" + description).hex,
                                "description": description, "status": "active", "source_event_ids": [],
                                "created_at": timestamp(), "legacy": True})
        # Faculty assessments cannot create promises. Only explicit, sourced
        # commitment operations below may change the durable record.
        for update in final.get("commitment_updates", []):
            sources = update["source_event_ids"]
            if not sources or not set(sources).issubset(known_event_ids):
                raise ValueError("A commitment update must cite actual source events.")
            if event["kind"] != "user" or event["id"] not in sources:
                raise ValueError("Commitment changes require evidence from the current user turn.")
            operation = update["operation"]
            prior = next((r for r in records if r["id"] == update.get("id")), None)
            if operation == "create":
                if any(r["description"].casefold() == update["description"].casefold() and r["status"] == "active"
                       for r in records):
                    continue
                records.append({"id": uuid.uuid4().hex, "description": update["description"], "status": "active",
                                "source_event_ids": sources, "created_at": timestamp()})
            else:
                if prior is None:
                    raise ValueError("A commitment update must identify an existing commitment.")
                if operation == "update":
                    prior["description"] = update["description"]
                else:
                    prior["status"] = "completed" if operation == "complete" else "abandoned"
                prior.update(source_event_ids=sources, updated_at=timestamp())
        if len([r for r in records if r["status"] == "active"]) > 24:
            raise ValueError("Resolve an existing commitment before adding more than 24 active commitments.")
        active = [r for r in records if r["status"] == "active"]
        closed = [r for r in records if r["status"] != "active"][-40:]
        self.state["self_model"]["commitment_records"] = active + closed
        self.state["self_model"]["commitments"] = [r["description"] for r in active]


    def _payload(self, event, memories, workspace, round_number):
        recent = self.store.list_events(limit=200)
        completed = [e for e in recent if e["id"] != event["id"] and e["status"] == "complete"
                     and (e["created_at"] <= event["created_at"] or e["episode_id"] == event["id"])
                     and not e.get("metadata", {}).get("evaluator_only")]
        conversational = [e for e in completed if e["kind"] in ("user", "assistant")][-6:]
        tools = [e for e in completed if e["kind"] == "tool"
                 and e["source"] in {"calculator", self.environment_tool}][-4:]
        history = [{"id": e["id"], "kind": e["kind"], "source": e["source"], "content": e["content"][:1000]}
                   for e in sorted(conversational + tools, key=lambda e: e["created_at"])]
        ledger = self._public_actions(event=event, limit=6)
        event_ids = {event["id"]} | {e["id"] for e in history}
        event_ids.update(i for m in memories for i in m.get("source_event_ids", []))
        event_ids.update(r["event_id"] for r in workspace.get("tool_results", []) if r.get("event_id"))
        for record in ledger:
            if record.get("event_id"):
                event_ids.add(record["event_id"])
            # Keep source texts bounded; action outcomes, not unsupported prose,
            # are the basis of agency claims.
            record.pop("created_at", None)
            record.pop("source_event_ids", None)
            if isinstance(record.get("prediction"), dict):
                record["prediction"] = {k: record["prediction"].get(k) for k in ("statement", "reported_confidence")}
        focus = focus_context(workspace)
        compact_workspace = copy.deepcopy(workspace)
        compact_workspace.pop("active_goals", None)
        if "attention" in compact_workspace:
            attention = compact_workspace["attention"]
            compact_workspace["attention"] = {k: attention[k] for k in
                                               ("tick", "capacity", "selected_ids", "broadcast_version") if k in attention}
        experiment = self.experiment_snapshot()
        if experiment["initialized"]:
            model = experiment["model"]
            experiment = {"initialized": True, "world_id": experiment.get("world_id"), "observation": experiment["observation"],
                          "beliefs": list(model.get("beliefs", {}).values())[:12],
                          "discrepancies": model.get("discrepancies", [])[-4:],
                          "calibration": model.get("calibration", {}),
                          "action_predictions": [
                              {key: forecast.get(key) for key in
                               ("action", "predicted_pose", "confidence", "samples", "basis")}
                              for forecast in [predict_action(model, action) for action in
                                               ("turn_left", "turn_right", "move_forward")]],
                          "recent_transitions": model.get("history", [])[-4:],
                          "run": experiment["run"]}
        else:
            experiment = {"initialized": False}
        run = experiment.get("run", {})
        enabled = bool(self.config.get("experiment_enabled", True) and event["kind"] == "experiment"
                       and run.get("active") and not self._experiment_stop_requested
                       and event.get("metadata", {}).get("run_id") == run.get("id"))
        evidence_ids = event_ids | {m["id"] for m in memories} | {a["id"] for a in ledger}
        self_model = copy.deepcopy(self.state["self_model"])
        self_model["commitment_records"] = self_model.get("commitment_records", [])[:24]
        if self.config.get("experiment_enabled", True):
            self_model["capabilities"] = list(dict.fromkeys(
                self_model.get("capabilities", []) + ["bounded actions in a local simulated room; no physical robot"]))

        return {
            "current_event": {k: event[k] for k in ("id", "kind", "source", "content")},
            "state_version": self.state["state_version"], "round": round_number,
            "round_limit": self.config["max_rounds"], "self_model": self_model,
            "goals": self.state.get("goals", []), "history": history, "memories": memories,
            "workspace": compact_workspace, "focus": focus, "action_ledger": ledger,
            "experiment": experiment,
            "rules": {
                "one_agent": True, "reflection_is_not_observation": True,
                "available_actions": ["reply", "retrieve", "reconsider", "calculate"] + (["environment"] if enabled else []),
                "environment_enabled": enabled,
                "event_ids": sorted(event_ids), "evidence_ids": sorted(evidence_ids),
                "attention_ids": [f["id"] for f in focus],
            },
        }

    async def _role(self, role, payload, trace):
        self.state["active_role"] = role
        await self._emit("role_started", {"role": role, "round": payload["round"]})
        started = time.monotonic()
        result, tokens = await self.backend.infer(role, payload)
        result = ROLE_SCHEMAS[role].model_validate(result).model_dump()
        elapsed = round((time.monotonic() - started) * 1000)
        entry = {"role": role, "round": payload["round"], "result": result, "elapsed_ms": elapsed,
                 "tokens": max(0, int(tokens))}
        trace.append(entry)
        await self._emit("role_completed", entry)
        return result

    async def process_event(self, event):
        async with self._lock:
            if event["kind"] == "experiment":
                envelope = self._experiment_envelope()
                run = (envelope["snapshot"] or {}).get("run", {})
                if (self._experiment_stop_requested or not run.get("active")
                        or event.get("metadata", {}).get("run_id") != run.get("id")):
                    self.store.fail_event(event["id"], "Simulation run is paused or no longer current.")
                    return
            before = copy.deepcopy(self.state)
            started = time.monotonic()
            self.state.update(status="thinking", current_event=event, active_role=None)
            self.state["cycle"] += 1
            trace = []
            try:
                await self._publish()
                reply = await asyncio.wait_for(self._cognitive_cycle(event, trace, started), self.config["cycle_timeout"])
            except asyncio.CancelledError:
                if self.store.get_event(event["id"])["status"] == "complete":
                    self.state = self.store.load_state()
                    raise
                self.state = before
                self.state.update(status="paused" if self._paused else "idle", active_role=None, current_event=None)
                self.store.fail_event(event["id"], "Processing interrupted; this event can be retried.")
                self._finish_experiment_event(event)
                await self._publish()
                raise
            except Exception as exc:
                # A cancellation/error at the completion boundary cannot undo a
                # successful SQLite commit or make its input retryable.
                if self.store.get_event(event["id"])["status"] == "complete":
                    self.state = self.store.load_state()
                    LOG.exception("Cycle committed but completion notification failed")
                    return
                self.state = before
                self.state["metrics"]["errors"] += 1
                self.state.update(status="paused" if self._paused else "idle", active_role=None, current_event=None)
                message = "The cognitive cycle exceeded its time budget." if isinstance(exc, asyncio.TimeoutError) else str(exc)
                message = (message or type(exc).__name__)[:1000]
                self.store.fail_event(event["id"], message)
                self.store.save_state(self.state)
                self._finish_experiment_event(event)
                await self._emit("error", {"event_id": event["id"], "message": message, "retryable": True})
                await self._publish()
                return
            self._finish_experiment_event(event)
            # Notifications are outside the transaction's failure handler.
            # A disconnected observatory must never roll back a completed mind.
            try:
                await self._emit("cycle_completed", {"event": event, "reply": reply, "state": self.snapshot()})
                await self._publish()
            except Exception:
                LOG.exception("Cycle committed; observatory can recover from its next snapshot")

    def _is_observation_source(self, event):
        return event["kind"] == "tool"

    async def _execute_environment_decision(self, event, decision):
        return self._environment_action(
            event, decision["environment_action"], decision.get("target", ""),
            decision.get("prediction", ""), decision.get("prediction_confidence", .5))

    async def _cognitive_cycle(self, event, trace, started):
        capacity = self.config.get("workspace_capacity", 6)
        workspace = initialize_workspace(self.state.get("workspace"), capacity=capacity)
        workspace["active_goals"] = copy.deepcopy(self.state.get("goals", []))
        if event["kind"] == "experiment":
            workspace["active_goals"].append({"description": event["content"], "status": "active"})
        # Retained attention is a retrieval cue; underlying records are fetched
        # again and checked so corrections cannot leave stale evidence in focus.
        stale = set()
        for item in workspace["attention"]["items"]:
            memory_id = item.get("memory_id")
            if memory_id:
                record = self.store.get_memory(memory_id)
                if not record or record.get("superseded_by"):
                    stale.add(item["id"])
        if stale:
            workspace["attention"]["items"] = [i for i in workspace["attention"]["items"] if i["id"] not in stale]
            workspace["attention"]["selected_ids"] = [i for i in workspace["attention"]["selected_ids"] if i not in stale]
        query = event["content"]
        seen_requests = set()
        final = None
        known_memory_ids = set()
        known_event_ids = {event["id"]}
        known_memories = {}
        last_action = None
        for round_number in range(1, self.config["max_rounds"] + 1):
            # Arithmetic feedback needs agency and a new decision, not a second
            # perception/retrieval pass over the same user message.
            compact_feedback = (round_number > 1 and last_action == "calculate"
                                and self.config.get("selective_recurrence", True))
            if not compact_feedback:
                initial_memories = self.store.retrieve(query, limit=6)
                if round_number == 1:
                    focus_query = " ".join(f["text"] for f in focus_context(workspace)[:2])[:600]
                    if focus_query:
                        extra = self.store.retrieve(query + " " + focus_query, limit=3)
                        initial_memories = list({m["id"]: m for m in initial_memories + extra}.values())[:8]
                payload = self._payload(event, initial_memories, workspace, round_number)
                manas = await self._role("manas", payload, trace)
                workspace = update_attention(workspace, event, manas, initial_memories, capacity=capacity)
                workspace = broadcast(workspace, "manas", manas)
                memory_query = query if round_number > 1 else (manas["retrieval_query"] or query)
                candidates = self.store.retrieve(memory_query, limit=6)
                merged = {m["id"]: m for m in initial_memories + candidates}
                focused_ids = [f["memory_id"] for f in focus_context(workspace) if f.get("memory_id")]
                ranked = [merged[i] for i in focused_ids if i in merged]
                ranked.extend(m for i, m in merged.items() if i not in focused_ids)
                candidates = [{**m, "content": m["content"][:1600]} for m in ranked[:8]]
                workspace["memories"] = candidates
                workspace = update_attention(workspace, event, manas, candidates, capacity=capacity)
                payload = self._payload(event, candidates, workspace, round_number)
                chitta = await self._role("chitta", payload, trace)
                available_ids = {m["id"] for m in candidates}
                if not set(chitta["selected_memory_ids"]).issubset(available_ids):
                    raise ValueError("Chitta referenced a memory that was not retrieved.")
                workspace = broadcast(workspace, "chitta", chitta)
                # Chitta's selection now changes the evidence handed onward.
                # Explicit contradiction detection keeps the competing evidence
                # available for Buddhi to resolve rather than hiding it.
                selected = set(chitta["selected_memory_ids"])
                memories = candidates if chitta["contradictions"] else [m for m in candidates if m["id"] in selected]
                workspace["memories"] = memories
            available_ids = {m["id"] for m in memories}
            payload = self._payload(event, memories, workspace, round_number)
            ahamkara = await self._role("ahamkara", payload, trace)
            workspace = broadcast(workspace, "ahamkara", ahamkara)
            # The attention model has an operational consequence: a specific
            # uncertainty can spend one bounded evidence probe before deciding.
            probe = ahamkara.get("uncertainty_to_check", "").strip()
            if probe and not workspace.get("attention_probe"):
                additions = self.store.retrieve(probe, limit=2)
                workspace["attention_probe"] = {"query": probe, "memory_ids": [m["id"] for m in additions]}
                memories = list({m["id"]: m for m in additions + memories}.values())[:8]
                memories = [{**m, "content": m["content"][:1600]} for m in memories]
                workspace["memories"] = memories
                available_ids = {m["id"] for m in memories}
            payload = self._payload(event, memories, workspace, round_number)
            decision = await self._role("buddhi", payload, trace)
            workspace = broadcast(workspace, "buddhi", decision)
            known_memory_ids.update(available_ids)
            known_memories.update({m["id"]: m for m in memories})
            known_event_ids.update(e["id"] for e in payload["history"])
            for m in memories:
                known_event_ids.update(m.get("source_event_ids", []))
            tool_events = [r["event_id"] for r in workspace["tool_results"]]
            known_event_ids.update(tool_events)
            known_event_ids.update(payload["rules"]["event_ids"])
            allowed_evidence = known_memory_ids | known_event_ids | {a["id"] for a in payload["action_ledger"]}
            if not set(decision["evidence_ids"]).issubset(allowed_evidence):
                raise ValueError("Buddhi cited evidence outside this cognitive episode.")
            action = decision["action"]
            last_action = action
            if action == "reply":
                if not decision["reply"].strip():
                    raise ValueError("Buddhi returned an empty final reply.")
                final = decision
                break
            signature = (action, decision.get("query", ""), decision.get("expression", ""))
            if signature in seen_requests:
                final = self._bounded_reply(decision, "Further processing repeated the same request.")
                break
            seen_requests.add(signature)
            if action == "calculate":
                expression = decision["expression"]
                tool_event, record = self._calculate_action(event, expression)
                value = record["result"]
                workspace["tool_results"].append({"event_id": tool_event["id"], "action_id": record["id"],
                                                   "tool": "calculator", "expression": expression, "result": value})
                known_event_ids.add(tool_event["id"])
                query = event["content"]
            elif action == "environment":
                if not payload["rules"]["environment_enabled"]:
                    raise ValueError("Simulation actions require an explicit bounded simulation run.")
                tool_event, record = await self._execute_environment_decision(event, decision)
                outcome = record["result"]
                workspace["tool_results"].append({"event_id": tool_event["id"], "action_id": record["id"],
                                                   "tool": self.environment_tool, "result": outcome})
                known_event_ids.add(tool_event["id"])
                observed = {"observations": [tool_event["content"][:1600]], "interpretations": [],
                            "uncertainties": [], "retrieval_query": event["content"], "salience": .9}
                workspace = update_attention(workspace, tool_event, observed, memories, capacity=capacity)
                # The next autonomous event sees the new observation and can
                # revise perception/plans. One action per event keeps budget,
                # cancellation and action attribution explicit.
                final = {**decision, "action": "reply",
                         "reply": f'{self.environment_label} {outcome["action"]}: {outcome["reason"]}',
                         "evidence_ids": [tool_event["id"]], "memory_updates": [], "goal_updates": [],
                         "commitment_updates": []}
                break
            elif action in ("retrieve", "reconsider"):
                query = decision["query"].strip()
                if not query:
                    raise ValueError("A further inquiry must specify what information is needed.")
                workspace["uncertainties"] = [query]
            if round_number == self.config["max_rounds"]:
                final = self._bounded_reply(decision, "The deliberation budget was reached.")
        if final is None:
            raise ValueError("No decision was produced.")
        # Stored facts keep their provenance. Model inference cannot masquerade as direct observation.
        updates = []
        captured_by_correction = False
        for proposal in final["memory_updates"]:
            sources = proposal["source_event_ids"]
            if not sources or not set(sources).issubset(known_event_ids):
                raise ValueError("Memory updates must reference evidence actually seen in this episode.")
            source_events = [self.store.get_event(i) for i in sources]
            if any(e is None for e in source_events):
                raise ValueError("A memory source no longer exists.")
            supersedes = proposal.get("supersedes")
            if supersedes and supersedes not in known_memory_ids:
                raise ValueError("A correction must identify a memory that was retrieved.")
            if supersedes and event["kind"] != "user":
                raise ValueError("Background reflection cannot supersede established memories.")
            if supersedes and event["id"] not in sources:
                raise ValueError("A correction requires evidence from the current user turn.")
            kind = proposal["kind"]
            source = "buddhi"
            if (supersedes and kind == "user_statement"
                    and known_memories[supersedes]["kind"] == "user_statement"):
                # The model identifies what is corrected; the immutable user
                # event supplies the replacement. Never store its paraphrase as
                # a raw quote, or duplicate the automatic capture of this turn.
                proposal = {**proposal, "content": event["content"],
                            "source_event_ids": [event["id"]], "confidence": 1.0}
                sources = proposal["source_event_ids"]
                source_events = [event]
                source = "user"
                captured_by_correction = True
            if event["kind"] == "reflection" and kind in ("user_statement", "observation", "commitment"):
                kind = "reflection"
            if kind == "observation" and any(not self._is_observation_source(e) for e in source_events):
                raise ValueError("An observation requires an actual tool outcome.")
            if kind == "user_statement" and any(e["kind"] != "user" for e in source_events):
                raise ValueError("A user statement requires a user event.")
            if kind in ("user_statement", "observation"):
                normalized = " ".join(proposal["content"].split())
                if not any(normalized == " ".join(e["content"].split()) for e in source_events):
                    # A source ID proves attribution, not the faithfulness of a
                    # paraphrase. Preserve generated summaries as inferences.
                    kind = "inference"
            if (supersedes and known_memories[supersedes]["kind"] in ("user_statement", "observation")
                    and kind not in ("user_statement", "observation")):
                raise ValueError("An inferred revision cannot replace a recorded statement or observation.")
            # Avoid storing the automatically captured user statement twice.
            if kind == "user_statement" and proposal["content"].strip() == event["content"].strip() and not supersedes:
                continue
            updates.append({**proposal, "kind": kind, "source": source})
        if event["kind"] == "user" and not captured_by_correction:
            updates.insert(0, {"content": event["content"], "kind": "user_statement", "source": "user",
                               "source_event_ids": [event["id"]], "confidence": 1.0, "supersedes": None})
        for outcome in workspace.get("tool_results", []):
            if outcome.get("tool") == self.environment_tool:
                source_event = self.store.get_event(outcome["event_id"])
                updates.append({"content": source_event["content"], "kind": "observation",
                                "source": self.environment_tool, "source_event_ids": [source_event["id"]],
                                "confidence": 1.0, "supersedes": None})
        goals = copy.deepcopy(self.state.get("goals", []))
        for change in final["goal_updates"]:
            existing = next((g for g in goals if g["description"].casefold() == change["description"].casefold()), None)
            if existing:
                existing.update(status=change["status"], updated_at=timestamp())
            else:
                goals.append({"id": uuid.uuid4().hex, **change, "created_at": timestamp()})
        self.state["goals"] = goals[-40:]
        self._apply_commitments(final, workspace["self_assessment"], event, known_event_ids)
        self.state["self_model"]["last_agency_assessment"] = workspace["self_assessment"].get("agency", "")
        workspace["decision"] = final
        self.state["workspace"] = workspace
        self.state["state_version"] += 1
        self.state["metrics"]["cycles"] += 1
        self.state["metrics"]["tokens"] += sum(t["tokens"] for t in trace)
        self.state["metrics"]["latency_ms"] = round((time.monotonic() - started) * 1000)
        self.state.update(status="paused" if self._paused else "idle", current_event=None, active_role=None)
        self.state["last_trace"] = trace
        return self.store.commit_cycle(event["id"], self.state, final["reply"], updates, trace)

    @staticmethod
    def _bounded_reply(decision, reason):
        return {**decision, "action": "reply", "reply":
                "I need more evidence before I can finish this reliably. " + reason +
                (" The unresolved question is: " + decision["query"] if decision.get("query") else ""),
                "uncertainty": max(0.7, decision["uncertainty"]), "memory_updates": [], "goal_updates": [], "commitment_updates": []}
