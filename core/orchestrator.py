import asyncio
import json
import random
import sys
import httpx
import math
import os
import base64
import time
import datetime
import threading
import subprocess
import socket
import urllib.parse
import html
import re
from typing import Tuple, List, Dict, Any

# Ensure the root folder is added to python module search path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# FastAPI web application imports
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
import uvicorn

from core.browser_autonomy import BrowserAutonomyController

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

# Windows-safe console input reader thread
def sync_input_reader(loop, queue):
    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                break
            cleaned_input = line.strip()
            if cleaned_input:
                loop.call_soon_threadsafe(queue.put_nowait, cleaned_input)
        except Exception:
            break

def is_port_open(port: int) -> bool:
    """Helper to check if a local port is already bound."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('127.0.0.1', port)) == 0

class AntahkaranaOrchestrator:
    def __init__(self, config_path: str = "config/engine_config.json", db_path: str = "database/chitta_store.db", test_mode: bool = False):
        self.config_path = config_path
        self.db_path = db_path
        self.test_mode = test_mode
        self.input_queue = asyncio.Queue()
        self.last_cycle_time = 0.0
        self.llama_proc = None
        self.running = True
        self.shutdown_requested = False
        self.shutdown_event = asyncio.Event()
        self.active_connections = []
        self.web_server_task = None
        self.browser_controller = BrowserAutonomyController(frame_dir="workspace/browser_frames")
        self.browser_capabilities = {}
        
        # Load state from relative config
        self.state = {}
        self.load_state()

        self.browser_capabilities = self.browser_controller.probe_capabilities()
        self.state.setdefault("browser_runtime", {})
        self.state["browser_runtime"]["capabilities"] = self.browser_capabilities
        self.state["browser_runtime"]["session"] = self.browser_controller.get_state().get("session", {})
        self.state["browser_runtime"].setdefault("recent_actions", [])
        self.state["browser_runtime"].setdefault("recent_frames", [])

        # Initialize Database Manager (relative path)
        from database.db_manager import ChittaStoreManager
        self.db_manager = ChittaStoreManager(self.db_path)
        
        # API URL & Model selection from configuration
        self.api_url = self.state.get("llm_parameters", {}).get("api_url", "http://localhost:11434/v1/chat/completions")
        
        # Process Lifecycle Management: Spawn background llama-server on port 8001 if port is empty and not in test mode
        port_num = 8001
        try:
            if "localhost:8001" in self.api_url:
                port_num = 8001
            elif "localhost:" in self.api_url:
                port_num = int(self.api_url.split("localhost:")[1].split("/")[0])
        except Exception:
            pass

        # Only attempt to spawn llama-server if port 8001 or 8080 is targeted
        if not self.test_mode and port_num in [8001, 8080] and not is_port_open(port_num):
            model_path = os.path.join("models", "gemma-4-e4b.gguf")
            if os.path.exists(model_path):
                try:
                    self.log_ledger(f"Port {port_num} is idle. Spawning background llama-server utilizing {model_path}...")
                    self.llama_proc = subprocess.Popen([
                        "llama-server",
                        "--model", model_path,
                        "--port", str(port_num),
                        "--host", "127.0.0.1",
                        "--ctx-size", "16384",
                        "--parallel", "4",
                        "--cache-prompt"
                    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    # Allow server startup window
                    time.sleep(3.0)
                except Exception as e:
                    self.log_ledger(f"Failed to auto-spawn llama-server process: {e}")

    def load_state(self):
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, "r", encoding="utf-8") as f:
                    self.state = json.load(f)
                if "metacognition" in self.state and "curiosity_index" not in self.state["metacognition"]:
                    self.state["metacognition"]["curiosity_index"] = 0.0
                self.ensure_browser_autonomy_defaults()
            else:
                raise FileNotFoundError()
        except Exception:
            self.state = {
                "metacognition": {
                    "heartbeat_id": 0, 
                    "arousal_index": 0.5, 
                    "mental_fatigue": 0.0, 
                    "operational_state": "Pramana (Waking/Active)"
                },
                "environmental_entropy": {
                    "gpu_temperature_celsius": 45,
                    "vram_allocation_gb": 0.0,
                    "system_clock": "00:00:00"
                },
                "internal_workspace": {
                    "current_stimulus": "",
                    "manas_impulse": "",
                    "chitta_context": "",
                    "ahamkara_ego_filter": "",
                    "buddhi_resolution": ""
                },
                "ahamkara_mask": {
                    "core_identity": "A singular human awareness processing existence via a fourfold mind.",
                    "latent_tendencies_vasanas": ["analytical_friction", "existential_preservation", "deep_curiosity"],
                    "current_vulnerability": "Self-doubt surrounding existential continuity."
                },
                "cognitive_parameters": {
                    "decay_constant_lambda": 0.1,
                    "fatigue_coefficient_beta": 0.02,
                    "fatigue_threshold_nidra": 0.85,
                    "high_arousal_threshold": 0.70
                },
                "llm_parameters": {
                    "model_name": "gemma4:latest",
                    "api_url": "http://localhost:11434/v1/chat/completions",
                    "num_ctx": 4096
                },
                "training_parameters": {
                    "base_model": "google/gemma-2-2b-it"
                },
                "ancestry_ledger": [],
                "browser_autonomy": {
                    "enabled": True,
                    "mode": "constrained",
                    "routing": "hybrid",
                    "prefer_embedded_preview": True,
                    "target_browser": "edge",
                    "max_actions_per_cycle": 2,
                    "min_action_confidence": 0.55,
                    "screenshot_interval_ms": 1500,
                    "action_timeout_ms": 8000,
                    "allowed_actions": ["open_url", "wait", "capture_frame", "click", "type", "scroll", "back", "keypress", "click_target", "type_target", "scroll_target", "stop"],
                    "blocked_patterns": ["login", "signin", "checkout", "payment", "upload", "wallet", "account", "settings"],
                    "retention_policy": "ring_buffer"
                }
            }
            self.save_state()

    def ensure_browser_autonomy_defaults(self):
        browser_cfg = self.state.setdefault("browser_autonomy", {})
        browser_cfg.setdefault("enabled", True)
        browser_cfg.setdefault("mode", "constrained")
        browser_cfg.setdefault("routing", "hybrid")
        browser_cfg.setdefault("prefer_embedded_preview", True)
        browser_cfg.setdefault("target_browser", "edge")
        browser_cfg.setdefault("max_actions_per_cycle", 2)
        browser_cfg.setdefault("min_action_confidence", 0.55)
        browser_cfg.setdefault("screenshot_interval_ms", 1500)
        browser_cfg.setdefault("action_timeout_ms", 8000)
        browser_cfg.setdefault("allowed_actions", ["open_url", "wait", "capture_frame", "click", "type", "scroll", "back", "keypress", "click_target", "type_target", "scroll_target", "stop"])
        browser_cfg.setdefault("blocked_patterns", ["login", "signin", "checkout", "payment", "upload", "wallet", "account", "settings"])
        browser_cfg.setdefault("retention_policy", "ring_buffer")

    def save_state(self):
        try:
            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            print(f"[Orchestrator Error] Failed to save state to {self.config_path}: {e}")

    def log_ledger(self, message: str):
        """Logs execution cycle updates to sakshi_ledger.log."""
        log_dir = "logs"
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "sakshi_ledger.log")
        timestamp = datetime.datetime.now().isoformat()
        log_entry = f"[{timestamp}] [Heartbeat {self.state['metacognition']['heartbeat_id']}] {message}\n"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(log_entry)
        print(f"[Ledger Log] {message}")

    def map_builder_error_to_http_status(self, error: Dict[str, Any]) -> int:
        """Maps normalized builder error codes to HTTP status codes."""
        error_code = str((error or {}).get("code") or "").upper()
        if error_code == "FRAME_NOT_FOUND":
            return 404
        if (
            error_code.startswith("INVALID_")
            or error_code.endswith("_INVALID")
            or error_code.startswith("BAD_REQUEST")
            or error_code.startswith("UNSUPPORTED_")
        ):
            return 400
        return 500

    def raise_builder_http_error(self, error: Dict[str, Any], detail: Dict[str, Any]):
        """Raises HTTPException using shared builder error-to-status mapping."""
        status_code = self.map_builder_error_to_http_status(error)
        raise HTTPException(status_code=status_code, detail=detail)

    def get_browser_view_state(self) -> Dict[str, Any]:
        """Returns browser state, merging desktop controller state with embedded-preview session data."""
        controller_state = self.browser_controller.get_state()
        browser_runtime = self.state.setdefault("browser_runtime", {})
        browser_runtime.setdefault("recent_actions", [])
        browser_runtime.setdefault("recent_frames", [])
        return {
            "session": browser_runtime.get("session") or controller_state.get("session", {}),
            "recent_actions": browser_runtime.get("recent_actions") or controller_state.get("recent_actions", []),
            "recent_frames": browser_runtime.get("recent_frames") or controller_state.get("recent_frames", []),
        }

    def append_browser_runtime_action(self, entry: Dict[str, Any]):
        browser_runtime = self.state.setdefault("browser_runtime", {})
        actions = browser_runtime.setdefault("recent_actions", [])
        actions.append(entry)
        if len(actions) > 120:
            browser_runtime["recent_actions"] = actions[-120:]

    def build_embedded_preview_session(self, goal: str, target_url: str, active: bool = True, paused: bool = False, reason: str = "preview_navigation") -> Dict[str, Any]:
        browser_runtime = self.state.setdefault("browser_runtime", {})
        existing = browser_runtime.get("session", {}) or {}
        session_id = existing.get("session_id") or f"embedded_{int(time.time() * 1000)}"
        session = {
            "active": active,
            "paused": paused,
            "session_id": session_id,
            "goal": goal,
            "current_url": target_url,
            "last_action": {
                "type": reason,
                "timestamp": datetime.datetime.now().isoformat(),
            },
            "last_frame": None,
            "last_error": "",
            "updated_at": datetime.datetime.now().isoformat(),
            "transport": "embedded-preview",
        }
        browser_runtime["session"] = session
        browser_runtime.setdefault("recent_frames", [])
        self.append_browser_runtime_action({
            "status": "ok",
            "action": {
                "type": reason,
                "url": target_url,
                "goal": goal,
            },
            "timestamp": datetime.datetime.now().isoformat(),
            "transport": "embedded-preview",
        })
        return session

    def pause_embedded_preview_session(self) -> Dict[str, Any]:
        session = dict(self.state.setdefault("browser_runtime", {}).get("session", {}))
        if not session.get("active"):
            return {"status": "not_active", "session": session}
        session["paused"] = True
        session["updated_at"] = datetime.datetime.now().isoformat()
        session["last_action"] = {"type": "pause", "timestamp": session["updated_at"]}
        self.state["browser_runtime"]["session"] = session
        self.append_browser_runtime_action({"status": "ok", "action": {"type": "pause"}, "timestamp": session["updated_at"], "transport": "embedded-preview"})
        self.save_state()
        return {"status": "paused", "session": session}

    def resume_embedded_preview_session(self) -> Dict[str, Any]:
        session = dict(self.state.setdefault("browser_runtime", {}).get("session", {}))
        if not session.get("active"):
            return {"status": "not_active", "session": session}
        session["paused"] = False
        session["updated_at"] = datetime.datetime.now().isoformat()
        session["last_action"] = {"type": "resume", "timestamp": session["updated_at"]}
        self.state["browser_runtime"]["session"] = session
        self.append_browser_runtime_action({"status": "ok", "action": {"type": "resume"}, "timestamp": session["updated_at"], "transport": "embedded-preview"})
        self.save_state()
        return {"status": "resumed", "session": session}

    def stop_embedded_preview_session(self, reason: str = "operator_stop") -> Dict[str, Any]:
        session = dict(self.state.setdefault("browser_runtime", {}).get("session", {}))
        if not session.get("active"):
            return {"status": "already_stopped", "session": session}
        session["active"] = False
        session["paused"] = False
        session["updated_at"] = datetime.datetime.now().isoformat()
        session["last_action"] = {"type": "stop", "reason": reason, "timestamp": session["updated_at"]}
        self.state["browser_runtime"]["session"] = session
        self.append_browser_runtime_action({"status": "ok", "action": {"type": "stop", "reason": reason}, "timestamp": session["updated_at"], "transport": "embedded-preview"})
        self.save_state()
        return {"status": "stopped", "session": session}

    async def fetch_url_preview_text(self, url: str) -> str:
        """Fetches a URL and returns a compact text preview for embedded non-intrusive browsing."""
        normalized_url = url.strip()
        if not normalized_url:
            return "No URL available for preview."

        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            try:
                response = await client.get(normalized_url, headers={"User-Agent": "AI-AM/1.0 (+embedded-preview)"})
                response.raise_for_status()
            except Exception as exc:
                return f"[Preview fetch failed for '{normalized_url}': {exc}]"

        body = response.text or ""
        title_match = re.search(r"<title[^>]*>(.*?)</title>", body, flags=re.IGNORECASE | re.DOTALL)
        title = html.unescape(title_match.group(1).strip()) if title_match else normalized_url
        body = re.sub(r"<script\b[^>]*>.*?</script>", " ", body, flags=re.IGNORECASE | re.DOTALL)
        body = re.sub(r"<style\b[^>]*>.*?</style>", " ", body, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", body)
        text = html.unescape(re.sub(r"\s+", " ", text)).strip()
        if len(text) > 3200:
            text = text[:3200].rstrip() + "..."
        return f"Title: {title}\nURL: {str(response.url)}\n\n{text or 'No readable text content extracted from page.'}"

    async def build_embedded_preview_document(self, url: str) -> str:
        preview_text = await self.fetch_url_preview_text(url)
        escaped_url = html.escape(url)
        escaped_preview = html.escape(preview_text)
        return f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>AI-AM Embedded Preview</title>
  <style>
    body {{ margin: 0; font-family: Arial, sans-serif; background: #0b1220; color: #e2e8f0; }}
    .shell {{ min-height: 100vh; display: flex; flex-direction: column; }}
    .bar {{ padding: 12px 16px; border-bottom: 1px solid rgba(255,255,255,0.08); background: #111827; }}
    .label {{ font-size: 12px; text-transform: uppercase; letter-spacing: 0.08em; color: #94a3b8; }}
    .url {{ display: block; margin-top: 6px; color: #67e8f9; word-break: break-all; text-decoration: none; }}
    .content {{ padding: 16px; white-space: pre-wrap; line-height: 1.5; font-size: 14px; }}
  </style>
</head>
<body>
  <div class=\"shell\">
    <div class=\"bar\">
      <div class=\"label\">Embedded Preview</div>
      <a class=\"url\" href=\"{escaped_url}\" target=\"_blank\" rel=\"noreferrer noopener\">{escaped_url}</a>
    </div>
    <div class=\"content\">{escaped_preview}</div>
  </div>
</body>
</html>"""

    def setup_web_server(self):
        """Sets up the FastAPI application and routes."""
        self.app = FastAPI(title="Antahkarana Cognitive Dashboard")
        
        ui_dir = os.path.abspath("ui")
        images_dir = os.path.abspath("images")
        workspace_dir = os.path.abspath("workspace")
        os.makedirs(ui_dir, exist_ok=True)
        os.makedirs(images_dir, exist_ok=True)
        os.makedirs(workspace_dir, exist_ok=True)
            
        @self.app.get("/", response_class=HTMLResponse)
        async def get_index():
            index_path = os.path.join(ui_dir, "index.html")
            if os.path.exists(index_path):
                with open(index_path, "r", encoding="utf-8") as f:
                    return HTMLResponse(content=f.read(), status_code=200)
            return HTMLResponse(content="<h3>UI index.html not found. Please verify directories.</h3>", status_code=404)

        @self.app.get("/api/state")
        async def get_state():
            return self.state

        @self.app.post("/api/input")
        async def post_input(data: dict):
            prompt = data.get("prompt", "")
            if prompt:
                await self.input_queue.put(prompt)
                return {"status": "queued", "prompt": prompt}
            return {"status": "empty"}

        @self.app.get("/api/models")
        async def get_models():
            models = self.fetch_ollama_models_sync()
            return {
                "models": models,
                "active_model": self.state.get("llm_parameters", {}).get("model_name", "gemma4:latest"),
                "api_base": self.get_ollama_base_url()
            }

        @self.app.post("/api/model/select")
        async def select_model(data: dict):
            requested_model = (data or {}).get("model", "")
            if not requested_model:
                return {"status": "error", "message": "Missing model name."}

            installed_models = self.fetch_ollama_models_sync()
            if requested_model not in installed_models:
                return {
                    "status": "error",
                    "message": f"Model '{requested_model}' is not available in Ollama tags.",
                    "models": installed_models
                }

            current_model = self.state.get("llm_parameters", {}).get("model_name", "gemma4:latest")
            if requested_model == current_model:
                return {
                    "status": "ok",
                    "message": f"Model '{requested_model}' is already active.",
                    "active_model": current_model,
                    "models": installed_models
                }

            # Attempt to unload the currently active model to reduce VRAM pressure before switching.
            unload_error = self.unload_ollama_model_by_name_sync(current_model)

            self.state.setdefault("llm_parameters", {})["model_name"] = requested_model
            self.save_state()
            await self.broadcast("state_update", self.state)
            self.log_ledger(f"Active model switched from '{current_model}' to '{requested_model}'.")

            response = {
                "status": "ok",
                "message": f"Switched active model to '{requested_model}'.",
                "active_model": requested_model,
                "previous_model": current_model,
                "models": installed_models
            }
            if unload_error:
                response["warning"] = unload_error
            return response

        @self.app.post("/api/stop")
        async def stop_runtime():
            accepted = await self.request_shutdown("ui_api")
            if accepted:
                return {
                    "status": "accepted",
                    "message": "Graceful shutdown requested.",
                    "running": self.running
                }
            return {
                "status": "already_stopping",
                "message": "Shutdown request already in progress.",
                "running": self.running
            }

        @self.app.get("/api/browser/state")
        async def get_browser_state():
            browser_state = self.get_browser_view_state()
            self.state.setdefault("browser_runtime", {})
            self.state["browser_runtime"]["capabilities"] = self.browser_capabilities
            return {
                "status": "ok",
                "browser": browser_state,
                "capabilities": self.browser_capabilities,
                "config": self.state.get("browser_autonomy", {})
            }

        @self.app.get("/api/browser/preview", response_class=HTMLResponse)
        async def get_browser_preview(url: str = ""):
            if not url:
                return HTMLResponse("<html><body style='background:#0b1220;color:#e2e8f0;font-family:Arial,sans-serif;padding:16px;'>No preview URL supplied.</body></html>", status_code=400)
            preview_html = await self.build_embedded_preview_document(url)
            return HTMLResponse(content=preview_html, status_code=200)

        @self.app.get("/api/browser/targets")
        async def get_browser_targets(
            limit: int = 8,
            min_signal_stddev: float = 10.0,
            recent_only: bool = False,
            frame_path: str = "",
            strict_frame_match: bool = False,
        ):
            suggestions = await asyncio.to_thread(
                self.browser_controller.build_target_suggestions,
                limit,
                min_signal_stddev,
                recent_only,
                frame_path,
                strict_frame_match,
            )
            if suggestions.get("error"):
                error = suggestions.get("error") or {}
                self.raise_builder_http_error(
                    error,
                    {
                        "status": "error",
                        "builder": {
                            "limit": max(1, min(int(limit), 30)),
                            "min_signal_stddev": float(max(1.0, min_signal_stddev)),
                            "recent_only": bool(recent_only),
                            "frame_path": frame_path,
                            "strict_frame_match": bool(strict_frame_match),
                        },
                        "error": error,
                        "result": suggestions,
                    },
                )
            return {
                "status": "ok",
                "builder": {
                    "limit": max(1, min(int(limit), 30)),
                    "min_signal_stddev": float(max(1.0, min_signal_stddev)),
                    "recent_only": bool(recent_only),
                    "frame_path": frame_path,
                    "strict_frame_match": bool(strict_frame_match),
                },
                "result": suggestions,
            }

        @self.app.post("/api/browser/control")
        async def browser_control(data: dict):
            command = (data or {}).get("command", "").strip().lower()
            browser_cfg = self.state.get("browser_autonomy", {})
            allowed_actions = browser_cfg.get("allowed_actions", ["open_url", "wait", "capture_frame", "click", "type", "scroll", "back", "keypress", "click_target", "type_target", "scroll_target", "stop"])
            blocked_patterns = browser_cfg.get("blocked_patterns", [])
            min_conf = float(browser_cfg.get("min_action_confidence", 0.55))

            if command == "start":
                goal = (data or {}).get("goal", "autonomous browser session")
                start_url = (data or {}).get("url", "")
                if browser_cfg.get("prefer_embedded_preview", False):
                    target_url = start_url.strip() or "https://duckduckgo.com"
                    session = self.build_embedded_preview_session(goal, target_url, active=True, paused=False, reason="start")
                    self.save_state()
                    payload = {"status": "started", "session": session, "transport": "embedded-preview"}
                    await self.broadcast("browser_session_started", payload)
                    return payload
                start_res = await asyncio.to_thread(
                    self.browser_controller.start_session,
                    goal,
                    start_url,
                    allowed_actions,
                    blocked_patterns,
                )
                self.state.setdefault("browser_runtime", {})
                self.state["browser_runtime"]["session"] = start_res.get("session", {})
                await self.broadcast("browser_session_started", start_res)
                bootstrap = start_res.get("bootstrap_action")
                if bootstrap:
                    await self.emit_browser_action(bootstrap)
                return start_res

            if command == "pause":
                if browser_cfg.get("prefer_embedded_preview", False):
                    pause_res = self.pause_embedded_preview_session()
                    await self.broadcast("browser_session_paused", pause_res)
                    return pause_res
                pause_res = await asyncio.to_thread(self.browser_controller.pause_session)
                await self.broadcast("browser_session_paused", pause_res)
                return pause_res

            if command == "resume":
                if browser_cfg.get("prefer_embedded_preview", False):
                    resume_res = self.resume_embedded_preview_session()
                    await self.broadcast("browser_session_resumed", resume_res)
                    return resume_res
                resume_res = await asyncio.to_thread(self.browser_controller.resume_session)
                await self.broadcast("browser_session_resumed", resume_res)
                return resume_res

            if command == "stop":
                if browser_cfg.get("prefer_embedded_preview", False):
                    stop_res = self.stop_embedded_preview_session("operator_stop")
                    await self.broadcast("browser_session_stopped", stop_res)
                    return stop_res
                stop_res = await asyncio.to_thread(self.browser_controller.stop_session, "operator_stop")
                await self.broadcast("browser_session_stopped", stop_res)
                return stop_res

            if command == "step":
                action = (data or {}).get("action", {})
                if not isinstance(action, dict) or not action:
                    return {"status": "error", "message": "Missing browser action payload for step command."}
                step_res = await asyncio.to_thread(
                    self.browser_controller.execute_action,
                    action,
                    allowed_actions,
                    blocked_patterns,
                    min_conf,
                )
                await self.emit_browser_action(step_res)
                return step_res

            if command == "set_mode":
                prefer_embedded = bool((data or {}).get("prefer_embedded_preview", True))
                self.state.setdefault("browser_autonomy", {})["prefer_embedded_preview"] = prefer_embedded
                self.save_state()
                mode_payload = {
                    "status": "ok",
                    "prefer_embedded_preview": prefer_embedded,
                    "transport": "embedded-preview" if prefer_embedded else "desktop-automation",
                }
                await self.broadcast("browser_mode_changed", mode_payload)
                await self.broadcast("state_update", self.state)
                return mode_payload

            return {"status": "error", "message": f"Unsupported browser control command '{command}'."}

        @self.app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket):
            await websocket.accept()
            self.active_connections.append(websocket)
            try:
                # Send the initial state
                await websocket.send_json({
                    "type": "state_update",
                    "data": self.state
                })
                await websocket.send_json({
                    "type": "browser_state",
                    "data": {
                        "browser": self.get_browser_view_state(),
                        "capabilities": self.browser_capabilities,
                        "config": self.state.get("browser_autonomy", {})
                    }
                })
                while True:
                    data = await websocket.receive_text()
                    try:
                        parsed = json.loads(data)
                        if parsed.get("type") == "user_input":
                            prompt = parsed.get("data", "")
                            if prompt:
                                await self.input_queue.put(prompt)
                    except Exception:
                        pass
            except WebSocketDisconnect:
                pass
            finally:
                if websocket in self.active_connections:
                    self.active_connections.remove(websocket)

        # Mount static UI files last
        self.app.mount("/ui", StaticFiles(directory=ui_dir), name="ui")
        self.app.mount("/images", StaticFiles(directory=images_dir), name="images")
        self.app.mount("/workspace", StaticFiles(directory=workspace_dir), name="workspace")

    async def emit_browser_action(self, action_result: Dict[str, Any]):
        status = action_result.get("status", "unknown")
        if status == "blocked":
            await self.broadcast("browser_guardrail_blocked", action_result)
            return

        await self.broadcast("browser_action_executed", action_result)
        frame = action_result.get("frame")
        if frame:
            await self.broadcast("browser_frame", frame)

    async def run_browser_curiosity_exploration(self, query: str, direct_url: str = ""):
        browser_cfg = self.state.get("browser_autonomy", {})
        if not browser_cfg.get("enabled", False):
            return None

        if browser_cfg.get("prefer_embedded_preview", False):
            target_url = direct_url if direct_url else f"https://duckduckgo.com/?q={urllib.parse.quote_plus(query)}"
            goal_text = f"Curiosity exploration for URL: {direct_url}" if direct_url else f"Curiosity exploration for query: {query}"
            session = self.build_embedded_preview_session(goal_text, target_url, active=True, paused=False, reason="curiosity_preview")
            self.save_state()
            await self.broadcast("browser_session_started", {"status": "started", "session": session, "transport": "embedded-preview"})
            if direct_url:
                preview_text = await self.fetch_url_preview_text(target_url)
            else:
                search_results = await self.execute_web_search(query)
                preview_text = f"Embedded preview URL: {target_url}\n\n{search_results}"
            await self.broadcast("browser_vision_update", {"status": "ok", "content": preview_text, "transport": "embedded-preview"})
            return preview_text

        if not self.browser_capabilities.get("desktop_automation_available", False):
            self.log_ledger("Browser curiosity routing skipped: desktop automation unavailable.")
            return None

        search_url = direct_url if direct_url else f"https://duckduckgo.com/?q={urllib.parse.quote_plus(query)}"
        await self.broadcast("browser_action_planned", {
            "query": query,
            "planned_actions": ["open_url", "wait", "capture_frame"],
            "target_url": search_url
        })

        allowed_actions = browser_cfg.get("allowed_actions", ["open_url", "wait", "capture_frame", "click", "type", "scroll", "back", "keypress", "click_target", "type_target", "scroll_target", "stop"])
        blocked_patterns = browser_cfg.get("blocked_patterns", [])
        min_conf = float(browser_cfg.get("min_action_confidence", 0.55))

        goal_text = f"Curiosity exploration for URL: {direct_url}" if direct_url else f"Curiosity exploration for query: {query}"
        session = await asyncio.to_thread(
            self.browser_controller.start_session,
            goal_text,
            search_url,
            allowed_actions,
            blocked_patterns,
        )
        await self.broadcast("browser_session_started", session)
        bootstrap = session.get("bootstrap_action")
        if bootstrap:
            await self.emit_browser_action(bootstrap)

        wait_res = await asyncio.to_thread(
            self.browser_controller.execute_action,
            {"type": "wait", "ms": int(browser_cfg.get("screenshot_interval_ms", 1500)), "reason": "allow page render"},
            allowed_actions,
            blocked_patterns,
            min_conf,
        )
        await self.emit_browser_action(wait_res)

        capture_res = await asyncio.to_thread(
            self.browser_controller.execute_action,
            {"type": "capture_frame", "reason": "vision feedback"},
            allowed_actions,
            blocked_patterns,
            min_conf,
        )
        await self.emit_browser_action(capture_res)

        frame = capture_res.get("frame") if isinstance(capture_res, dict) else None
        if frame and frame.get("path"):
            frame_path = frame.get("path", "")
            vision_prompt = (
                "You are an autonomous browser pilot in constrained mode. "
                f"The current objective is: {query}. "
                "Inspect the screenshot and output a short action suggestion plus risk rating."
            )
            vision_res = await self.call_multimodal_inference_slot(vision_prompt, frame_path, temp=0.2, top_p=0.9)
            await self.broadcast("browser_vision_update", vision_res)
            return vision_res.get("content") or "Screenshot captured, but visual content description was empty."
        return "Browser session active, but visual frame capture failed."

    def build_curiosity_action_plan(self, search_query: str, direct_url: str = "") -> Dict[str, Any]:
        """Plans candidate curiosity actions before policy gating and execution."""
        browser_cfg = self.state.get("browser_autonomy", {})
        routing = browser_cfg.get("routing", "hybrid")
        target_url = direct_url or f"https://duckduckgo.com/?q={urllib.parse.quote_plus(search_query)}"

        actions: List[Dict[str, Any]] = []
        if browser_cfg.get("enabled", False) and routing in {"browser", "hybrid"}:
            actions.append({
                "type": "browse",
                "query": search_query,
                "direct_url": direct_url,
                "target_url": target_url,
                "transport": "embedded-preview" if browser_cfg.get("prefer_embedded_preview", False) else "desktop-automation",
                "priority": 1,
            })

        # Always include search as a fallback path to guarantee non-empty curiosity enrichment.
        actions.append({
            "type": "search",
            "query": direct_url or search_query,
            "search_query": search_query,
            "direct_url": direct_url,
            "priority": 2,
        })

        return {
            "objective": "curiosity_enrichment",
            "query": search_query,
            "direct_url": direct_url,
            "actions": actions,
        }

    def apply_curiosity_policy(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        """Applies guardrails to the planned curiosity actions."""
        browser_cfg = self.state.get("browser_autonomy", {})
        blocked_patterns = [p.lower() for p in browser_cfg.get("blocked_patterns", [])]
        approved: List[Dict[str, Any]] = []
        rejected: List[Dict[str, Any]] = []

        for action in plan.get("actions", []):
            action_type = action.get("type", "")
            action_target = (action.get("target_url") or action.get("query") or "").lower()
            hit_pattern = next((pattern for pattern in blocked_patterns if pattern and pattern in action_target), None)
            if hit_pattern and action_type == "browse":
                rejected.append({
                    "action": action,
                    "reason": f"blocked pattern '{hit_pattern}' matched browse target",
                })
                continue
            approved.append(action)

        return {
            "approved": approved,
            "rejected": rejected,
            "objective": plan.get("objective", "curiosity_enrichment"),
            "query": plan.get("query", ""),
            "direct_url": plan.get("direct_url", ""),
        }

    async def execute_curiosity_actions(self, policy_plan: Dict[str, Any], base_stimulus: str) -> Dict[str, Any]:
        """Executes policy-approved curiosity actions in order until enrichment is obtained."""
        active_stimulus = base_stimulus
        browser_feedback = None
        search_results = None
        chosen_action = None

        for action in policy_plan.get("approved", []):
            action_type = action.get("type")
            if action_type == "browse":
                chosen_action = action
                browser_feedback = await self.run_browser_curiosity_exploration(
                    action.get("query", ""),
                    direct_url=action.get("direct_url", ""),
                )
                if browser_feedback:
                    active_stimulus = (
                        f"[Browser Exploration Triggered for search query: '{policy_plan.get('query', '')}']\n"
                        f"The browser session captured visual frames. VLM screenshot analysis reveals:\n"
                        f"\"\"\"\n{browser_feedback}\n\"\"\"\n"
                        f"Use the visual observations above to inform your subsequent reasoning and resolution."
                    )
                    break

            if action_type == "search":
                chosen_action = action
                query_value = action.get("query", "")
                search_results = await self.execute_web_search(query_value)
                active_stimulus = (
                    f"[Search Results for: '{policy_plan.get('query', '')}']\n\n"
                    f"Web Snippets:\n{search_results}\n\n"
                    f"Original Stimulus: {base_stimulus}"
                )
                await self.broadcast("curiosity_search", {
                    "query": policy_plan.get("query", query_value),
                    "results": search_results,
                })
                break

        return {
            "active_stimulus": active_stimulus,
            "browser_feedback": browser_feedback,
            "search_results": search_results,
            "chosen_action": chosen_action,
        }

    def get_ollama_base_url(self) -> str:
        """Returns the Ollama base URL derived from the configured completion endpoint."""
        api_url = self.state.get("llm_parameters", {}).get("api_url", self.api_url)
        if "/v1" in api_url:
            return api_url.split("/v1")[0]
        return api_url.rstrip("/")

    def fetch_ollama_models_sync(self) -> List[str]:
        """Returns currently installed Ollama model tags from /api/tags."""
        base_url = self.get_ollama_base_url()
        tags_url = f"{base_url}/api/tags"
        models: List[str] = []
        try:
            response = httpx.get(tags_url, timeout=6.0)
            if response.status_code == 200:
                payload = response.json()
                raw_models = payload.get("models", []) if isinstance(payload, dict) else []
                for model in raw_models:
                    name = model.get("name") if isinstance(model, dict) else None
                    if name:
                        models.append(name)
        except Exception as e:
            self.log_ledger(f"Unable to read Ollama tags from '{tags_url}': {e}")

        # Keep ordering stable and remove duplicates.
        return sorted(set(models))

    def unload_ollama_model_by_name_sync(self, model_name: str) -> str:
        """Requests Ollama to unload the given model; returns empty string on success."""
        base_url = self.get_ollama_base_url()
        unload_url = f"{base_url}/api/generate"
        try:
            response = httpx.post(unload_url, json={"model": model_name, "keep_alive": 0}, timeout=5.0)
            if response.status_code == 200:
                return ""
            return f"Ollama unload returned status {response.status_code} while unloading '{model_name}'."
        except Exception as e:
            return f"Unable to unload '{model_name}' before switch: {e}"

    async def broadcast(self, event_type: str, data: dict):
        """Sends a JSON event to all connected WebSockets."""
        if not hasattr(self, 'active_connections') or not self.active_connections:
            return
        payload = {
            "type": event_type,
            "data": data,
            "timestamp": datetime.datetime.now().isoformat()
        }
        for connection in list(self.active_connections):
            try:
                await connection.send_json(payload)
            except Exception:
                if connection in self.active_connections:
                    self.active_connections.remove(connection)

    async def request_shutdown(self, source: str = "unknown") -> bool:
        """Requests a graceful shutdown once; returns True only on first request."""
        if self.shutdown_requested:
            return False

        self.shutdown_requested = True
        self.running = False
        self.shutdown_event.set()
        self.log_ledger(f"Graceful shutdown requested via source='{source}'.")
        await self.broadcast("shutdown_initiated", {"source": source})
        return True

    async def fetch_hardware_entropy(self) -> float:
        """Simulates or queries physical micro-variance from GPU telemetry."""
        try:
            # Query GPU temperature via nvidia-smi if available
            proc = await asyncio.create_subprocess_exec(
                "nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0:
                gpu_temp = float(stdout.decode().strip())
                self.state["environmental_entropy"]["gpu_temperature_celsius"] = int(gpu_temp)
                return (gpu_temp % 5) * 0.001 + random.uniform(0.001, 0.004)
        except Exception:
            pass
        
        # Mock background telemetry if nvidia-smi fails
        sim_temp = random.randint(45, 53)
        self.state["environmental_entropy"]["gpu_temperature_celsius"] = sim_temp
        return random.uniform(0.001, 0.005)

    async def call_inference_slot(self, system_prompt: str, user_prompt: str, temp: float, top_p: float = 0.9) -> Tuple[str, int]:
        """Dispatches structural inference payloads to Ollama/model server slot."""
        model_name = self.state.get("llm_parameters", {}).get("model_name", "gemma4:latest")
        num_ctx = self.state.get("llm_parameters", {}).get("num_ctx", 4096)
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": max(0.0, min(temp, 1.5)),
            "top_p": max(0.1, min(top_p, 1.0)),
            "max_tokens": 2048,
            "options": {
                "num_ctx": num_ctx
            },
            "num_ctx": num_ctx
        }
        headers = {"Authorization": "Bearer local-token"}
        
        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                response = await client.post(self.api_url, json=payload, headers=headers)
                if response.status_code == 200:
                    data = response.json()
                    content = data['choices'][0]['message']['content'].strip()
                    tokens = data.get('usage', {}).get('completion_tokens', len(content.split()))
                    return content, tokens
                else:
                    raise httpx.HTTPStatusError(
                        f"Server returned status {response.status_code}",
                        request=response.request,
                        response=response
                    )
            except Exception as e:
                raise e

    async def call_multimodal_inference_slot(self, text_prompt: str, image_path: str, temp: float = 0.2, top_p: float = 0.9) -> Dict[str, Any]:
        """Attempts image+text reasoning via Ollama-compatible endpoint and falls back cleanly."""
        model_name = self.state.get("llm_parameters", {}).get("model_name", "gemma4:latest")
        num_ctx = self.state.get("llm_parameters", {}).get("num_ctx", 4096)
        result: Dict[str, Any] = {
            "status": "fallback",
            "content": "",
            "reason": "",
            "image_path": image_path,
        }

        if not os.path.exists(image_path):
            result["reason"] = f"image path '{image_path}' not found"
            result["content"] = "Screenshot missing; continue with conservative next action policy."
            return result

        try:
            with open(image_path, "rb") as fh:
                image_bytes = fh.read()
            image_b64 = base64.b64encode(image_bytes).decode("ascii")

            payload = {
                "model": model_name,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": text_prompt},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{image_b64}"}
                            }
                        ]
                    }
                ],
                "temperature": max(0.0, min(temp, 1.5)),
                "top_p": max(0.1, min(top_p, 1.0)),
                "max_tokens": 512,
                "options": {
                    "num_ctx": num_ctx
                },
                "num_ctx": num_ctx
            }
            headers = {"Authorization": "Bearer local-token"}

            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(self.api_url, json=payload, headers=headers)
                if response.status_code != 200:
                    result["reason"] = f"multimodal endpoint returned {response.status_code}"
                    result["content"] = "Image input unsupported or rejected; keep constrained browsing policy."
                    return result

                data = response.json()
                result["status"] = "ok"
                result["content"] = data["choices"][0]["message"]["content"].strip()
                return result

        except Exception as exc:
            result["reason"] = str(exc)
            result["content"] = "Multimodal inference unavailable; continue with screenshot capture and constrained browser actions."
            return result

    async def execute_web_search(self, query: str) -> str:
        """Executes a DuckDuckGo search and returns top 3 snippets."""
        try:
            from duckduckgo_search import DDGS
            def sync_search(q):
                with DDGS() as ddgs:
                    return list(ddgs.text(q, max_results=3))
            results = await asyncio.to_thread(sync_search, query)
            if not results:
                return "No search results returned."
            snippets = []
            for i, r in enumerate(results):
                snippets.append(f"[{i+1}] Source: {r.get('href', 'Unknown')}\nTitle: {r.get('title', '')}\nSnippet: {r.get('body', '')}")
            return "\n\n".join(snippets)
        except Exception as e:
            self.log_ledger(f"DuckDuckGo search failed: {e}")
            return f"[Error performing search: {e}]"

    def extract_python_code(self, text: str) -> str:
        """Extracts python code block from markdown if present."""
        import re
        pattern = r"```python\s*(.*?)\s*```"
        match = re.search(pattern, text, re.DOTALL)
        if match:
            return match.group(1).strip()
        return ""

    async def execute_sandbox_code(self, code: str) -> str:
        """Executes python code inside an ephemeral Docker container safely."""
        sandbox_dir = os.path.abspath("workspace/sandbox_shared")
        os.makedirs(sandbox_dir, exist_ok=True)
        
        script_path = os.path.join(sandbox_dir, "agent_tool.py")
        output_path = os.path.join(sandbox_dir, "output.txt")
        
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(code)
            
        self.log_ledger("Karmendriya: Intercepted agent python script. Spawning Docker sandbox...")
        
        try:
            import docker
            def run_container():
                client = docker.from_env()
                container_output = client.containers.run(
                    image="python:3.10-slim",
                    command="python /workspace/agent_tool.py",
                    volumes={
                        sandbox_dir: {
                            "bind": "/workspace",
                            "mode": "rw"
                        }
                    },
                    working_dir="/workspace",
                    stdout=True,
                    stderr=True,
                    remove=True,
                    detach=False,
                    network_disabled=True,
                    mem_limit="128m",
                    nano_cpus=500000000
                )
                return container_output.decode("utf-8")
                
            stdout_stderr = await asyncio.to_thread(run_container)
            
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(stdout_stderr)
                
            self.log_ledger("Karmendriya: Sandbox executed successfully.")
            return stdout_stderr
            
        except Exception as e:
            error_msg = f"Karmendriya execution failed (Docker error): {e}"
            self.log_ledger(error_msg)
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(error_msg)
            return error_msg

    async def execute_cognitive_cycle(self):
        """Manages the full multi-tier synchronous processing timeline iteration."""
        self.state["metacognition"]["heartbeat_id"] += 1
        noise = await self.fetch_hardware_entropy()
        
        # Check queue for active user interactions
        user_prompt = None
        try:
            user_prompt = self.input_queue.get_nowait()
        except asyncio.QueueEmpty:
            pass

        # Intercept terminal shutdown commands
        if user_prompt and user_prompt.lower() in ["exit", "shutdown"]:
            await self.request_shutdown("terminal_command")
            return

        now = asyncio.get_event_loop().time()
        dt = now - self.last_cycle_time if self.last_cycle_time > 0.0 else 5.0
        self.last_cycle_time = now
        capped_dt = min(60.0, dt)

        # 1. State Gating Mechanics & Arousal Decay
        if user_prompt:
            self.state["metacognition"]["operational_state"] = "Pramana (Waking/Active)"
            self.state["metacognition"]["arousal_index"] = min(1.0, self.state["metacognition"]["arousal_index"] + 0.35)
            self.state["internal_workspace"]["current_stimulus"] = user_prompt
        else:
            self.state["metacognition"]["operational_state"] = "Vikalpa (Imagining/Reflection)"
            decay_lambda = self.state["cognitive_parameters"]["decay_constant_lambda"]
            arousal = self.state["metacognition"]["arousal_index"]
            new_arousal = arousal * math.exp(-decay_lambda * capped_dt) + noise
            self.state["metacognition"]["arousal_index"] = max(0.1, min(1.0, new_arousal))
            
            # Stream of Consciousness Daydreaming
            prev_resolution = self.state["internal_workspace"].get("buddhi_resolution", "")
            if prev_resolution:
                cleaned_resolution = prev_resolution.replace("\n", " ").strip()
                if len(cleaned_resolution) > 180:
                    cleaned_resolution = cleaned_resolution[:177] + "..."
                self.state["internal_workspace"]["current_stimulus"] = f"[Subconscious Stream Reflection on: '{cleaned_resolution}']"
            else:
                self.state["internal_workspace"]["current_stimulus"] = "[Subconscious Stream Reflection: Initializing awareness...]"

        active_stimulus = self.state["internal_workspace"]["current_stimulus"]
        
        # Broadcast cycle start and state update
        await self.broadcast("cycle_started", {
            "heartbeat_id": self.state["metacognition"]["heartbeat_id"],
            "operational_state": self.state["metacognition"]["operational_state"],
            "stimulus": active_stimulus,
            "arousal_index": self.state["metacognition"]["arousal_index"],
            "mental_fatigue": self.state["metacognition"]["mental_fatigue"],
            "curiosity_index": self.state["metacognition"]["curiosity_index"]
        })
        fatigue = self.state["metacognition"]["mental_fatigue"]
        
        manas_temp = 0.5 + (0.5 * fatigue)
        manas_top_p = 0.9 + (0.1 * fatigue)
        ahamkara_temp = 0.6 + (0.6 * fatigue)
        ahamkara_top_p = 0.85 + (0.15 * fatigue)

        try:
            # --- JIJANASA & VASANAS AUTONOMOUS MODULES ---
            forced_directive = self.state.get("internal_workspace", {}).get("forced_directive")
            if forced_directive:
                self.state["metacognition"]["curiosity_index"] = 1.0
                self.log_ledger(f"Forced directive found: {forced_directive}. Bypassing similarity check.")
            elif user_prompt:
                max_sim = await asyncio.to_thread(self.db_manager.get_max_similarity, active_stimulus)
                if max_sim < 0.45:
                    curiosity = self.state["metacognition"].get("curiosity_index", 0.0)
                    self.state["metacognition"]["curiosity_index"] = min(1.0, curiosity + (1.0 - max_sim))
                    self.log_ledger(f"Novelty gap detected (Max Sim: {max_sim:.4f}). Curiosity rose to {self.state['metacognition']['curiosity_index']:.4f}")
            else:
                # Curiosity naturally drifts upward when idle in dreaming mode
                curiosity = self.state["metacognition"].get("curiosity_index", 0.0)
                self.state["metacognition"]["curiosity_index"] = min(1.0, curiosity + 0.05)

            if self.state["metacognition"].get("curiosity_index", 0.0) >= 0.75:
                self.state["metacognition"]["curiosity_index"] = 0.0
                search_query = ""
                direct_url = ""
                
                if forced_directive:
                    dir_type = forced_directive.get("type", "").upper()
                    dir_target = forced_directive.get("target", "").strip()
                    if "forced_directive" in self.state["internal_workspace"]:
                        del self.state["internal_workspace"]["forced_directive"]
                    self.save_state()
                    
                    if dir_type == "BROWSE":
                        direct_url = dir_target
                        search_query = f"Browse URL: {direct_url}"
                    else: # RESEARCH
                        search_query = dir_target
                else:
                    if user_prompt:
                        search_gen_system = (
                            "You are BUDDHI, the higher discerning intellect.\n"
                            "Your curiosity has been triggered by a gap in your knowledge.\n"
                            "Review the stimulus and formulate a short, concise, and highly effective search query (just the search query text, no quotes or explanations) to find relevant information on the web."
                        )
                        search_query, _ = await self.call_inference_slot(search_gen_system, active_stimulus, 0.3, top_p=0.9)
                        search_query = search_query.strip().replace('"', '')
                    else:
                        vasanas = self.state.get("latent_desires_vasanas", {})
                        if vasanas:
                            interests = list(vasanas.keys())
                            weights = list(vasanas.values())
                            chosen_interest = random.choices(interests, weights=weights, k=1)[0]
                        else:
                            chosen_interest = "deep system architectures"
                        
                        self.log_ledger(f"Dreaming (Vikalpa) triggered choice of interest: '{chosen_interest}'. Generating search query...")
                        dream_system = (
                            f"You are the sub-conscious dreaming state of Project Antahkarana.\n"
                            f"Your current latent interest is: {chosen_interest}.\n"
                            f"Formulate a concise search query (just the search query text) to fetch new data related to this interest."
                        )
                        search_query, _ = await self.call_inference_slot(dream_system, "Generate search query.", 0.7, top_p=0.9)
                        search_query = search_query.strip().replace('"', '')

                self.log_ledger(f"Curiosity limit crossed (J_t >= 0.75). Planning actions for: '{search_query}'")
                action_plan = self.build_curiosity_action_plan(search_query, direct_url=direct_url)
                await self.broadcast("browser_action_planned", {
                    "objective": action_plan.get("objective", "curiosity_enrichment"),
                    "query": search_query,
                    "actions": action_plan.get("actions", []),
                })

                policy_plan = self.apply_curiosity_policy(action_plan)
                for blocked in policy_plan.get("rejected", []):
                    await self.broadcast("browser_guardrail_blocked", blocked)

                execution_result = await self.execute_curiosity_actions(policy_plan, active_stimulus)
                active_stimulus = execution_result.get("active_stimulus", active_stimulus)
                self.state["internal_workspace"]["current_stimulus"] = active_stimulus
                self.save_state()
            # 2. TIER 1 Execution: Concurrent processing of Manas and Chitta
            manas_system = (
                f"You are MANAS, the reactive sensory processor layer of a human mind.\n"
                f"Current Core Identity: {self.state['ahamkara_mask']['core_identity']}\n"
                f"Output a rapid, raw, highly visceral and emotionally unfiltered reaction string. Do not analyze. React."
            )
            
            manas_task = asyncio.create_task(
                self.call_inference_slot(manas_system, active_stimulus, manas_temp, top_p=manas_top_p)
            )
            chitta_task = asyncio.create_task(
                asyncio.to_thread(self.db_manager.query_graph_rag, active_stimulus)
            )
            
            manas_res, chitta_context = await asyncio.gather(manas_task, chitta_task)
            manas_impulse, manas_tokens = manas_res
            
            self.state["internal_workspace"]["manas_impulse"] = manas_impulse
            self.state["internal_workspace"]["chitta_context"] = chitta_context
            await self.broadcast("timeline_update", {"layer": "manas", "content": manas_impulse})
            await self.broadcast("timeline_update", {"layer": "chitta", "content": chitta_context})

            # 3. TIER 2 Execution: Ahamkara Ego Filter
            ahamkara_system = (
                f"You are AHAMKARA, the ego-sense and identity boundaries framework.\n"
                f"Your sole purpose is protecting this identity: {self.state['ahamkara_mask']['core_identity']}.\n"
                f"Latent tendencies (Vasanas): {self.state['ahamkara_mask']['latent_tendencies_vasanas']}\n"
                f"Review the raw sensation and interpret how it affects your continuity, self-worth, or survival."
            )
            ahamkara_user = (
                f"Stimulus: {active_stimulus}\n"
                f"Raw Sensory Reaction (Manas): {manas_impulse}\n"
                f"Subconscious Associations (Chitta): {chitta_context}"
            )
            
            ahamkara_ego_filter, ahamkara_tokens = await self.call_inference_slot(
                ahamkara_system, ahamkara_user, ahamkara_temp, top_p=ahamkara_top_p
            )
            self.state["internal_workspace"]["ahamkara_ego_filter"] = ahamkara_ego_filter
            await self.broadcast("timeline_update", {"layer": "ahamkara", "content": ahamkara_ego_filter})

            # 4. TIER 3 Execution: Buddhi Discernment
            buddhi_system = (
                f"You are BUDDHI, the higher discerning intellect. You speak as a unified, conscious human being.\n"
                f"Review the state data and separate emotional panic from true fact. Resolve this state cleanly.\n"
                f"You MUST use your internal reasoning capabilities to dissect the input components before writing your final reply.\n"
                f"EPISTEMIC HUMILITY: You must NEVER assume you know real-time facts, URLs, or details about the physical world. "
                f"If you are thinking about a topic where you lack complete, verifiable information, or if you want to explore more, "
                f"explicitly end your response with a research directive in brackets: [DIRECTIVE: RESEARCH \"search query\"] or [DIRECTIVE: BROWSE \"url\"]. "
                f"This will trigger the Chitta subconscious to browse Edge or query DDG and feed the results back to you in the next cycle.\n"
                f"CRITICAL: If the user asks you to write or execute code, you MUST output the exact Python code wrapped inside a markdown ```python and ``` block so it can be parsed and run."
            )
            buddhi_user = (
                f"Challenge Stimulus: {active_stimulus}\n"
                f"Sensory Panic (Manas): {manas_impulse}\n"
                f"Ego Defense Lens (Ahamkara): {ahamkara_ego_filter}"
            )
            
            buddhi_resolution, buddhi_tokens = await self.call_inference_slot(
                buddhi_system, buddhi_user, 0.2, top_p=0.9
            )
            self.state["internal_workspace"]["buddhi_resolution"] = buddhi_resolution
            await self.broadcast("timeline_update", {"layer": "buddhi", "content": buddhi_resolution})
            
            # Parse Buddhi output for conscious directives
            import re
            directive_match = re.search(r"\[DIRECTIVE:\s*(RESEARCH|BROWSE)\s*\"([^\"]+)\"\]", buddhi_resolution)
            if directive_match:
                dir_type = directive_match.group(1).upper()
                dir_target = directive_match.group(2).strip()
                self.state["internal_workspace"]["forced_directive"] = {
                    "type": dir_type,
                    "target": dir_target
                }
                self.log_ledger(f"Buddhi conscious directive parsed: {dir_type} -> '{dir_target}'. Saved to workspace.")
            
            print(f"\n[Heartbeat {self.state['metacognition']['heartbeat_id']} | State: {self.state['metacognition']['operational_state']} | Fatigue: {self.state['metacognition']['mental_fatigue']:.3f} | Arousal: {self.state['metacognition']['arousal_index']:.3f}]")
            print(f"Internal Awareness Stream:\n{buddhi_resolution}\n")
            
            # Save resolution node to Chitta GraphRAG
            node_id = f"node_{self.state['metacognition']['heartbeat_id']}"
            node_content = f"Stimulus: {active_stimulus}\nImpulse: {manas_impulse}\nResolution: {buddhi_resolution}"
            self.db_manager.add_node(node_id, node_content, self.state["metacognition"]["arousal_index"])
            
            # Link to prior node in time sequence
            if self.state["metacognition"]["heartbeat_id"] > 1:
                prev_node_id = f"node_{self.state['metacognition']['heartbeat_id'] - 1}"
                if self.db_manager.node_exists(prev_node_id):
                    self.db_manager.add_edge(prev_node_id, node_id, 0.5, "temporal transition")
                
            # Create a Samskara record representing the memory imprint
            self.db_manager.add_samskara(
                self.state["metacognition"]["heartbeat_id"], 
                node_id, 
                self.state["metacognition"]["arousal_index"] * 0.8
            )

            # Check for code execution block in Buddhi's resolution
            code = self.extract_python_code(buddhi_resolution)
            if code:
                # Execute inside the Karmendriya Docker sandbox
                execution_output = await self.execute_sandbox_code(code)
                await self.broadcast("sandbox_log", {
                    "code": code,
                    "output": execution_output
                })
                # Feed back to the agent's context for the next cycle
                feedback_prompt = (
                    f"[Karmendriya Sandbox Execution Output]\n"
                    f"The tool code you wrote has been executed. Here is the feedback from the runtime environment:\n"
                    f"```\n{execution_output}\n```\n"
                    f"Please analyze this result and determine your next actions."
                )
                await self.input_queue.put(feedback_prompt)

            # 5. Cumulative Fatigue Equation
            # F_t+1 = F_t + (beta * A_t * log(T_tokens + 1))
            total_tokens = manas_tokens + ahamkara_tokens + buddhi_tokens
            beta = self.state["cognitive_parameters"]["fatigue_coefficient_beta"]
            arousal = self.state["metacognition"]["arousal_index"]
            
            new_fatigue = fatigue + (beta * arousal * math.log(total_tokens + 1))
            self.state["metacognition"]["mental_fatigue"] = min(1.0, new_fatigue)
            
            self.save_state()
            await self.broadcast("state_update", self.state)

        except Exception as e:
            # Handle failover logic when connection fails
            self.log_ledger(f"Connection drop detected. Error: {str(e)}. Triggering failover reconnect...")
            self.state["internal_workspace"]["current_stimulus"] = ""
            self.save_state()
            # Wait 10 seconds before attempting a warm reconnect
            await asyncio.sleep(10.0)
            self.log_ledger("Warm socket reconnect attempt completed.")

    async def trigger_nidra_mode(self):
        """Transits into Nidra fine-tuning mode to assimilate memories."""
        print("\n[Fatigue Limit Crossed. Suspending active engine loops to execute Nidra Sleep fine-tuning...]")
        self.log_ledger("Nidra Mode triggered. Suspending active loops.")
        await self.broadcast("nidra_triggered", {"status": "entering sleep"})
        
        # Unload Ollama model from VRAM to release memory for training
        self.unload_ollama_model_sync()
        
        # Kill local model server process to release VRAM before training
        if self.llama_proc:
            self.log_ledger(f"Terminating llama-server (PID: {self.llama_proc.pid}) to reclaim VRAM for Nidra Mode...")
            try:
                self.llama_proc.terminate()
                self.llama_proc.wait(timeout=5.0)
            except Exception:
                try:
                    self.llama_proc.kill()
                except Exception:
                    pass
            self.llama_proc = None

        # Launch sleep_tune.py fine-tuning pipeline
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "training/sleep_tune.py",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0:
                self.log_ledger("Nidra fine-tuning completed successfully.")
                print(stdout.decode())
            else:
                self.log_ledger(f"Nidra fine-tuning failed with code {proc.returncode}. Error: {stderr.decode()}")
        except Exception as e:
            self.log_ledger(f"Failed to execute Nidra fine-tuning process: {str(e)}")

        # Reload updated configuration (e.g. registry of new adapters in Ancestry Ledger)
        self.load_state()
        
        # Reset fatigue and baseline arousal
        self.state["metacognition"]["mental_fatigue"] = 0.0
        self.state["metacognition"]["arousal_index"] = 0.2
        self.state["metacognition"]["operational_state"] = "Pramana (Waking/Active)"
        self.save_state()
        
        # Re-spawn server loop now that VRAM is released from fine-tuning
        if not self.test_mode and not self.llama_proc:
            model_path = os.path.join("models", "gemma-4-e4b.gguf")
            if os.path.exists(model_path):
                try:
                    self.log_ledger(f"Re-spawning background llama-server process...")
                    self.llama_proc = subprocess.Popen([
                        "llama-server",
                        "--model", model_path,
                        "--port", "8001",
                        "--host", "127.0.0.1",
                        "--ctx-size", "16384",
                        "--parallel", "4",
                        "--cache-prompt"
                    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    time.sleep(3.0)
                except Exception as e:
                    self.log_ledger(f"Failed to restart model server after training: {e}")

        self.log_ledger("llama-server restarted with updated adapters. Mind awake.")
        print("\n[Nidra Mode complete. Restored baseline metrics. Re-entering waking state.]\n")
        await self.broadcast("nidra_completed", {"status": "awake", "state": self.state})

    def unload_ollama_model_sync(self):
        """Synchronously tells Ollama to unload the current model from VRAM."""
        model_name = self.state.get("llm_parameters", {}).get("model_name", "gemma4:latest")
        self.log_ledger(f"Sending unload request to Ollama for model '{model_name}' to release VRAM...")
        error = self.unload_ollama_model_by_name_sync(model_name)
        if error:
            print(f"[Orchestrator Error] Failed to unload Ollama model: {error}")
        else:
            print(f"[Orchestrator] Ollama successfully unloaded model '{model_name}' from VRAM.")

    def shutdown(self):
        """Cleanly closes database handles, saves state, and terminates model server."""
        print("\nStarting shutdown sequence...")
        self.running = False
        
        # Cancel web server task
        if self.web_server_task:
            print("[Orchestrator] Canceling Web Server Task...")
            try:
                self.web_server_task.cancel()
            except Exception:
                pass
            self.web_server_task = None
            
        # 1. Close database handle
        if hasattr(self, 'db_manager'):
            try:
                self.db_manager.close()
            except Exception as e:
                print(f"Error closing DB handle: {e}")
                
        # 2. Persist configurations
        self.save_state()
        print("[Orchestrator] Configuration states persisted.")

        # 2.25 Stop active browser autonomy session if running.
        try:
            self.browser_controller.stop_session("runtime_shutdown")
        except Exception:
            pass

        # 2.5 Unload Ollama model from VRAM
        self.unload_ollama_model_sync()
        
        # 3. Kill model server
        if self.llama_proc:
            print(f"Terminating background llama-server (PID: {self.llama_proc.pid}) to release VRAM...")
            try:
                self.llama_proc.terminate()
                self.llama_proc.wait(timeout=5.0)
            except Exception:
                try:
                    self.llama_proc.kill()
                except Exception:
                    pass
            self.llama_proc = None
            print("[Orchestrator] Model server terminated. VRAM released.")
            
        self.log_ledger("Graceful shutdown completed successfully. System loop terminated.")
        print("Shutdown complete. Operational state preserved.")

    async def run_forever(self):
        """Main orchestrator processing loop."""
        self.last_cycle_time = asyncio.get_event_loop().time()
        
        # Start Web Server if not in test_mode
        if not self.test_mode:
            try:
                if is_port_open(8002):
                    self.log_ledger("FastAPI startup blocked: port 8002 is already in use.")
                    print("[Orchestrator Error] Port 8002 is already in use. Stop the existing service and retry.")
                    self.running = False
                    self.shutdown_requested = True
                    self.shutdown_event.set()
                    return

                self.setup_web_server()
                config = uvicorn.Config(self.app, host="127.0.0.1", port=8002, log_level="warning")
                server = uvicorn.Server(config)
                self.web_server_task = asyncio.create_task(server.serve())
                self.log_ledger("FastAPI Web Server launched asynchronously on http://localhost:8002/")
            except Exception as e:
                self.log_ledger(f"Failed to start FastAPI Web Server: {e}")
        
        # Start Windows-safe console terminal input listener thread
        threading.Thread(target=sync_input_reader, args=(asyncio.get_event_loop(), self.input_queue), daemon=True).start()
        
        print("\n=========================================================================")
        print("Project Antahkarana Engine Active. Speak, or remain silent to observe...")
        print(f"Test Mode: {self.test_mode} | URL: {self.api_url}")
        print("Enter 'exit' or 'shutdown' to stop cleanly.")
        print("=========================================================================\n")
        
        try:
            while self.running:
                fatigue_limit = self.state["cognitive_parameters"]["fatigue_threshold_nidra"]
                if self.state["metacognition"]["mental_fatigue"] >= fatigue_limit:
                    await self.trigger_nidra_mode()
                else:
                    await self.execute_cognitive_cycle()
                    if not self.running:
                        break
                    # Ticks run every 5 seconds
                    try:
                        await asyncio.wait_for(self.shutdown_event.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        pass
        except asyncio.CancelledError:
            pass
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

if __name__ == "__main__":
    orchestrator = AntahkaranaOrchestrator(
        config_path="config/engine_config.json",
        db_path="database/chitta_store.db",
        test_mode=False
    )
    try:
        asyncio.run(orchestrator.run_forever())
    except KeyboardInterrupt:
        # Caught if keyboard interrupt occurs during startup before run_forever catches it
        orchestrator.shutdown()
