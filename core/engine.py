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
        result["recent_events"] = self.store.list_events(limit=50)
        result["memory_stats"] = self.store.stats()
        result["backend"] = self.config["backend"]
        result["paused"] = self._paused
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
        self._running = False
        self._wake.set()
        if self._active_task and not self._active_task.done():
            self._active_task.cancel()
            try:
                await self._active_task
            except asyncio.CancelledError:
                pass
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

    def _payload(self, event, memories, workspace, round_number):
        recent = self.store.list_events(limit=200)
        history = [{"id": e["id"], "kind": e["kind"], "source": e["source"], "content": e["content"][:1000]}
                   for e in recent if e["id"] != event["id"] and e["status"] == "complete"
                   and e["kind"] in ("user", "assistant")][-6:]
        return {
            "current_event": {k: event[k] for k in ("id", "kind", "source", "content")},
            "state_version": self.state["state_version"], "round": round_number,
            "round_limit": self.config["max_rounds"],
            "self_model": copy.deepcopy(self.state["self_model"]), "goals": self.state.get("goals", []),
            "history": history, "memories": memories, "workspace": copy.deepcopy(workspace),
            "rules": {
                "one_agent": True, "reflection_is_not_observation": True,
                "available_actions": ["reply", "retrieve", "reconsider", "calculate"],
                "evidence_ids": [event["id"]] + [m["id"] for m in memories] + [e["id"] for e in history],
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
                await self._emit("error", {"event_id": event["id"], "message": message, "retryable": True})
                await self._publish()
                return
            # Notifications are outside the transaction's failure handler.
            # A disconnected observatory must never roll back a completed mind.
            try:
                await self._emit("cycle_completed", {"event": event, "reply": reply, "state": self.snapshot()})
                await self._publish()
            except Exception:
                LOG.exception("Cycle committed; observatory can recover from its next snapshot")

    async def _cognitive_cycle(self, event, trace, started):
        workspace = {"observations": [], "interpretations": [], "memories": [], "uncertainties": [],
                     "self_assessment": {}, "decision": {}, "tool_results": []}
        query = event["content"]
        seen_requests = set()
        final = None
        known_memory_ids = set()
        known_event_ids = {event["id"]}
        known_memories = {}
        for round_number in range(1, self.config["max_rounds"] + 1):
            initial_memories = self.store.retrieve(query, limit=6)
            payload = self._payload(event, initial_memories, workspace, round_number)
            manas = await self._role("manas", payload, trace)
            workspace.update(observations=manas["observations"], interpretations=manas["interpretations"],
                             uncertainties=manas["uncertainties"])
            workspace["manas"] = manas
            memory_query = query if round_number > 1 else (manas["retrieval_query"] or query)
            candidates = self.store.retrieve(memory_query, limit=6)
            merged = {m["id"]: m for m in initial_memories + candidates}
            memories = list(merged.values())[:8]
            # Bound the handoff; original evidence remains in durable storage.
            memories = [{**m, "content": m["content"][:1600]} for m in memories]
            workspace["memories"] = memories
            payload = self._payload(event, memories, workspace, round_number)
            chitta = await self._role("chitta", payload, trace)
            available_ids = {m["id"] for m in memories}
            if not set(chitta["selected_memory_ids"]).issubset(available_ids):
                raise ValueError("Chitta referenced a memory that was not retrieved.")
            workspace["chitta"] = chitta
            payload = self._payload(event, memories, workspace, round_number)
            ahamkara = await self._role("ahamkara", payload, trace)
            workspace["self_assessment"] = ahamkara
            payload = self._payload(event, memories, workspace, round_number)
            # Direct memory evidence reaches Buddhi, regardless of Ahamkara's interpretation.
            decision = await self._role("buddhi", payload, trace)
            workspace["decision"] = decision
            known_memory_ids.update(available_ids)
            known_memories.update({m["id"]: m for m in memories})
            known_event_ids.update(e["id"] for e in payload["history"])
            for m in memories:
                known_event_ids.update(m.get("source_event_ids", []))
            tool_events = [r["event_id"] for r in workspace["tool_results"]]
            known_event_ids.update(tool_events)
            allowed_evidence = known_memory_ids | known_event_ids
            if not set(decision["evidence_ids"]).issubset(allowed_evidence):
                raise ValueError("Buddhi cited evidence outside this cognitive episode.")
            action = decision["action"]
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
                value = calculate(expression)
                tool_event = self.store.append_event(
                    "tool", f"{expression} = {value}", source="calculator", episode_id=event["id"],
                    metadata={"expression": expression, "result": value})
                workspace["tool_results"].append({"event_id": tool_event["id"], "expression": expression, "result": value})
                known_event_ids.add(tool_event["id"])
                query = event["content"]
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
        if event["kind"] == "user":
            updates.append({"content": event["content"], "kind": "user_statement", "source": "user",
                            "source_event_ids": [event["id"]], "confidence": 1.0, "supersedes": None})
        for proposal in final["memory_updates"]:
            sources = proposal["source_event_ids"]
            if not sources or not set(sources).issubset(known_event_ids):
                raise ValueError("Memory updates must reference evidence actually seen in this episode.")
            source_events = [self.store.get_event(i) for i in sources]
            if any(e is None for e in source_events):
                raise ValueError("A memory source no longer exists.")
            kind = proposal["kind"]
            if event["kind"] == "reflection" and kind in ("user_statement", "observation", "commitment"):
                kind = "reflection"
            if kind == "observation" and any(e["kind"] != "tool" for e in source_events):
                raise ValueError("An observation requires an actual tool outcome.")
            if kind == "user_statement" and any(e["kind"] != "user" for e in source_events):
                raise ValueError("A user statement requires a user event.")
            if kind in ("user_statement", "observation"):
                normalized = " ".join(proposal["content"].split())
                if not any(normalized == " ".join(e["content"].split()) for e in source_events):
                    # A source ID proves attribution, not the faithfulness of a
                    # paraphrase. Preserve generated summaries as inferences.
                    kind = "inference"
            supersedes = proposal.get("supersedes")
            if supersedes and supersedes not in known_memory_ids:
                raise ValueError("A correction must identify a memory that was retrieved.")
            if supersedes and event["kind"] != "user":
                raise ValueError("Background reflection cannot supersede established memories.")
            if supersedes and event["id"] not in sources:
                raise ValueError("A correction requires evidence from the current user turn.")
            if (supersedes and known_memories[supersedes]["kind"] in ("user_statement", "observation")
                    and kind not in ("user_statement", "observation")):
                raise ValueError("An inferred revision cannot replace a recorded statement or observation.")
            # Avoid storing the automatically captured user statement twice.
            if kind == "user_statement" and proposal["content"].strip() == event["content"].strip() and not supersedes:
                continue
            updates.append({**proposal, "kind": kind, "source": "buddhi"})
        goals = copy.deepcopy(self.state.get("goals", []))
        for change in final["goal_updates"]:
            existing = next((g for g in goals if g["description"].casefold() == change["description"].casefold()), None)
            if existing:
                existing.update(status=change["status"], updated_at=timestamp())
            else:
                goals.append({"id": uuid.uuid4().hex, **change, "created_at": timestamp()})
        self.state["goals"] = goals[-40:]
        self.state["self_model"]["commitments"] = workspace["self_assessment"].get("commitments", [])[:12]
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
                "uncertainty": max(0.7, decision["uncertainty"]), "memory_updates": [], "goal_updates": []}
