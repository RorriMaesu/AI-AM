"""Loopback web service for AI-AM's four-function cognitive runtime.

Importing this module does not create state, load a model, or start a server.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path as FilePath
from typing import Literal
from urllib.parse import urlsplit

if __package__ in (None, ""):
    sys.path.insert(0, str(FilePath(__file__).resolve().parents[1]))

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Path, Query, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr, field_validator

LOG = logging.getLogger("aiam.service")
ROOT = FilePath(__file__).resolve().parents[1]
ROLES = ("manas", "chitta", "ahamkara", "buddhi")
MAX_BODY_BYTES = 65536


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InputRequest(RequestModel):
    text: StrictStr

    @field_validator("text")
    @classmethod
    def validate_text(cls, value):
        value = value.strip()
        if not value or len(value) > 8000:
            raise ValueError("Text must contain 1 to 8000 characters.")
        return value


class ReflectionRequest(RequestModel):
    topic: StrictStr = ""

    @field_validator("topic")
    @classmethod
    def validate_topic(cls, value):
        if len(value) > 2000:
            raise ValueError("Reflection topics are limited to 2000 characters.")
        return value.strip()


class RetryRequest(RequestModel):
    event_id: StrictStr

    @field_validator("event_id")
    @classmethod
    def validate_id(cls, value):
        if not value or len(value) > 128:
            raise ValueError("Invalid event ID.")
        return value


class ModelRequest(RequestModel):
    role: Literal["all", "manas", "chitta", "ahamkara", "buddhi"]
    model: StrictStr

    @field_validator("model")
    @classmethod
    def validate_model(cls, value):
        if not value.strip() or len(value) > 200:
            raise ValueError("Invalid model name.")
        return value.strip()


class ControlRequest(RequestModel):
    action: Literal["pause", "resume"]


class BackgroundRequest(RequestModel):
    enabled: StrictBool


class LocalBoundaryMiddleware:
    """Reject foreign browser origins, non-loopback hosts, and oversized bodies."""

    def __init__(self, app, max_body_bytes=MAX_BODY_BYTES):
        self.app = app
        self.max_body_bytes = max_body_bytes

    @staticmethod
    def _allowed(scope, headers):
        host = headers.get("host", "")
        try:
            authority = urlsplit("//" + host)
            if authority.hostname not in ("127.0.0.1", "localhost", "::1"):
                return False
            if authority.username or authority.password or authority.path:
                return False
            scheme = "https" if scope.get("scheme") in ("https", "wss") else "http"
            port = authority.port or (443 if scheme == "https" else 80)
            origin = headers.get("origin")
            if origin:
                parsed = urlsplit(origin)
                if (
                    parsed.scheme != scheme
                    or parsed.hostname != authority.hostname
                    or (parsed.port or (443 if parsed.scheme == "https" else 80)) != port
                    or parsed.username or parsed.password
                    or parsed.path or parsed.query or parsed.fragment
                ):
                    return False
            if headers.get("sec-fetch-site", "").lower() == "cross-site":
                return False
            return True
        except (ValueError, TypeError):
            return False

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = {key.decode("latin1").lower(): value.decode("latin1")
                   for key, value in scope.get("headers", [])}
        if not self._allowed(scope, headers):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await JSONResponse({"detail": "This interface accepts local, same-origin requests only."},
                                   status_code=403)(scope, receive, send)
            return
        if scope["type"] == "websocket":
            return await self.app(scope, receive, send)

        async def secure_send(message):
            if message["type"] == "http.response.start":
                response_headers = list(message.get("headers", []))
                response_headers.extend([
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                ])
                if scope.get("path", "").startswith("/api/"):
                    response_headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": response_headers}
            await send(message)

        if scope.get("method") in ("POST", "PUT", "PATCH"):
            raw_length = headers.get("content-length")
            if raw_length is not None:
                try:
                    length = int(raw_length)
                except ValueError:
                    length = -1
                if length < 0:
                    return await JSONResponse({"detail": "Invalid Content-Length."}, status_code=400)(
                        scope, receive, secure_send)
                if length > self.max_body_bytes:
                    return await JSONResponse({"detail": "Request body is too large."}, status_code=413)(
                        scope, receive, secure_send)
            chunks = []
            size = 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                if message["type"] != "http.request":
                    continue
                chunk = message.get("body", b"")
                size += len(chunk)
                if size > self.max_body_bytes:
                    return await JSONResponse({"detail": "Request body is too large."}, status_code=413)(
                        scope, receive, secure_send)
                chunks.append(chunk)
                if not message.get("more_body", False):
                    break
            body = b"".join(chunks)
            delivered = False

            async def bounded_receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            return await self.app(scope, bounded_receive, secure_send)
        return await self.app(scope, receive, secure_send)


async def invoke(operation):
    """Expose useful operational failures without returning internal stack traces."""
    try:
        return await operation()
    except HTTPException:
        raise
    except (asyncio.QueueFull, OverflowError) as exc:
        raise HTTPException(429, "The input queue is full. Please try again shortly.") from exc
    except KeyError as exc:
        raise HTTPException(404, "The requested record does not exist.") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(504, "The operation exceeded its time limit.") from exc
    except Exception as exc:
        LOG.exception("Runtime operation failed")
        raise HTTPException(503, "The operation failed. The event remains available for review or retry.") from exc


def create_app(engine=None, config=None, *, start_runtime=True):
    """Construct an application; tests inject an engine and disable its worker."""
    if engine is None:
        from core.config import load_config
        from core.engine import MindEngine
        from database.db_manager import MindStore
        config = config if config is not None else load_config()
        engine = MindEngine(config, MindStore(config["db_path"]))
    config = config if config is not None else engine.config

    @asynccontextmanager
    async def lifespan(app):
        task = None
        readiness = None
        try:
            if start_runtime:
                task = asyncio.create_task(engine.run_forever(), name="ai-am-runtime")
                app.state.runtime_task = task
                readiness = asyncio.create_task(engine.ready.wait(), name="ai-am-startup")
                await asyncio.wait((task, readiness), return_when=asyncio.FIRST_COMPLETED)
                if task.done():
                    await task  # Preserve the startup exception, if there is one.
                    raise RuntimeError("AI-AM runtime exited before it became ready.")
                if engine.startup_error is not None:
                    raise engine.startup_error
            yield
        finally:
            app.state.stopping = True
            if readiness is not None:
                readiness.cancel()
                await asyncio.gather(readiness, return_exceptions=True)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(engine.stop(), timeout=5)
            if task is not None:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    app = FastAPI(title="AI-AM · Cognitive Observatory", version="2.0.0", lifespan=lifespan)
    app.add_middleware(LocalBoundaryMiddleware)
    app.state.engine = engine
    app.state.runtime_task = None
    app.state.shutdown_callback = None
    app.state.stopping = False

    async def require_runtime():
        task = app.state.runtime_task
        if app.state.stopping or (start_runtime and (task is None or task.done())):
            raise HTTPException(503, "The cognitive runtime has stopped. Restart AI-AM before submitting more work.")

    @app.get("/api/health")
    async def health():
        task = app.state.runtime_task
        snapshot = engine.snapshot()
        return {
            "identity": "ai-am", "version": "2.0.0",
            "status": snapshot.get("status", "idle"),
            "backend": config.get("backend", "ollama"),
            "runtime_running": bool(task and not task.done()),
        }

    @app.get("/api/state")
    async def state():
        return engine.snapshot()

    @app.get("/api/events")
    async def events(limit: int = Query(50, ge=1, le=200)):
        return {"events": await asyncio.to_thread(engine.store.list_events, limit)}

    @app.get("/api/events/{event_id}")
    async def event(event_id: str = Path(..., min_length=1, max_length=128)):
        result = await asyncio.to_thread(engine.store.get_event, event_id)
        if result is None:
            raise HTTPException(404, "Event not found.")
        return result

    @app.post("/api/input", status_code=202, dependencies=[Depends(require_runtime)])
    async def submit(data: InputRequest):
        return await invoke(lambda: engine.submit(data.text))

    @app.post("/api/retry", status_code=202, dependencies=[Depends(require_runtime)])
    async def retry(data: RetryRequest):
        return await invoke(lambda: engine.retry(data.event_id))

    @app.get("/api/memories")
    async def memories(query: str = Query("", max_length=8000),
                       limit: int = Query(30, ge=1, le=100)):
        return {"memories": await asyncio.to_thread(engine.store.retrieve, query, limit)}

    @app.get("/api/memories/{memory_id}")
    async def memory(memory_id: str = Path(..., min_length=1, max_length=128)):
        result = await asyncio.to_thread(engine.store.get_memory, memory_id)
        if result is None:
            raise HTTPException(404, "Memory not found.")
        return result

    @app.get("/api/models")
    async def models():
        try:
            installed = await engine.backend.list_models()
            return {"models": installed, "roles": engine.snapshot().get("roles", {})}
        except Exception:
            LOG.warning("The local model inventory is unavailable", exc_info=True)
            return {"models": [], "roles": engine.snapshot().get("roles", {}),
                    "error": "Cannot reach the local model service. Start Ollama and check the configured endpoint."}

    @app.post("/api/model/select", dependencies=[Depends(require_runtime)])
    async def model_select(data: ModelRequest):
        async def change():
            installed = await engine.backend.list_models()
            if data.model not in installed:
                raise ValueError("That model is not installed in the configured local service.")
            selected_roles = ROLES if data.role == "all" else (data.role,)
            await engine.set_models({role: data.model for role in selected_roles})
            return {"status": "updated", "roles": engine.snapshot().get("roles", {})}
        return await invoke(change)

    @app.post("/api/reflect", status_code=202, dependencies=[Depends(require_runtime)])
    async def reflect(data: ReflectionRequest):
        return await invoke(lambda: engine.reflect(data.topic))

    @app.post("/api/consolidate", dependencies=[Depends(require_runtime)])
    async def consolidate():
        return await invoke(engine.consolidate)

    @app.post("/api/control", dependencies=[Depends(require_runtime)])
    async def control(data: ControlRequest):
        await invoke(engine.pause if data.action == "pause" else engine.resume)
        return {"status": engine.snapshot().get("status"), "state": engine.snapshot()}

    @app.post("/api/background", dependencies=[Depends(require_runtime)])
    async def background(data: BackgroundRequest):
        await invoke(lambda: engine.set_background(data.enabled))
        return {"enabled": data.enabled, "state": engine.snapshot()}

    @app.post("/api/stop")
    async def stop(tasks: BackgroundTasks):
        app.state.stopping = True
        await invoke(engine.stop)
        if app.state.shutdown_callback is not None:
            tasks.add_task(app.state.shutdown_callback)
        return {"status": "stopping", "identity": "ai-am"}

    @app.websocket("/ws")
    async def websocket_stream(websocket: WebSocket):
        await websocket.accept()
        queue = engine.subscribe()
        try:
            await websocket.send_json({"type": "state", "data": engine.snapshot()})

            async def sender():
                while True:
                    await websocket.send_json(await queue.get())

            async def receiver():
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    if message["type"] == "websocket.receive":
                        # The WebSocket is an observation channel. Mutations use validated HTTP.
                        await websocket.close(code=1008, reason="Use the HTTP input endpoint.")
                        return

            send_task = asyncio.create_task(sender())
            receive_task = asyncio.create_task(receiver())
            try:
                await asyncio.wait((send_task, receive_task), return_when=asyncio.FIRST_COMPLETED)
            finally:
                send_task.cancel()
                receive_task.cancel()
                await asyncio.gather(send_task, receive_task, return_exceptions=True)
        finally:
            engine.unsubscribe(queue)

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(ROOT / "ui" / "index.html", headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; font-src 'self'; "
                "connect-src 'self' ws://127.0.0.1:* ws://localhost:*; "
                "base-uri 'none'; frame-ancestors 'none'; object-src 'none'"
            ),
        })

    app.mount("/ui", StaticFiles(directory=str(ROOT / "ui")), name="ui")
    return app


def main():
    parser = argparse.ArgumentParser(description="Run AI-AM's local cognitive observatory.")
    parser.add_argument("--config", help="Optional configuration JSON file.")
    parser.add_argument("--data-dir", help="Directory for durable memory and runtime state.")
    parser.add_argument("--demo", action="store_true", help="Use a clearly labeled deterministic demonstration backend.")
    parser.add_argument("--port", type=int, default=8002)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    from core.config import load_config
    import uvicorn
    config = load_config(path=args.config, data_dir=args.data_dir, demo=args.demo)
    app = create_app(config=config)
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=args.port, log_level="info",
        ws_max_size=MAX_BODY_BYTES, timeout_graceful_shutdown=10,
    ))
    app.state.shutdown_callback = lambda: setattr(server, "should_exit", True)
    print(f"AI-AM: http://127.0.0.1:{args.port} · data: {config['data_dir']}")
    if config.get("backend") == "demo":
        print("DEMONSTRATION MODE: responses are scripted; no LLM is running.")
    server.run()


if __name__ == "__main__":
    main()
