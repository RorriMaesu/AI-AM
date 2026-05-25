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
                self.ensure_tool_execution_defaults()
                self.ensure_mind_runtime_defaults()
                self.ensure_ahamkara_umwelt_defaults()
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
                "ahamkara_umwelt": {
                    "identity_priors": [
                        "maintain coherent continuity of self across cycles",
                        "preserve bounded autonomy while remaining aligned to user dialogue"
                    ],
                    "boundary_rules": [
                        "do not perform external side effects without policy approval",
                        "surface uncertainty before acting under conflict"
                    ],
                    "values": ["truthfulness", "continuity", "curiosity", "non-harm"],
                    "social_stance": "collaborative_companion",
                    "continuity_goals": [
                        "retain stable identity voice",
                        "integrate new evidence without role collapse"
                    ],
                    "last_identity_frame": "",
                    "last_threat_assessment": "",
                    "last_continuity_action": "",
                    "updated_at": ""
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
                },
                "tool_execution_policy": {
                    "enabled": True,
                    "allow_sandbox_python": True,
                    "max_code_chars": 12000,
                    "allow_network_access": False,
                    "deny_code_patterns": [
                        "os.system(",
                        "subprocess.Popen(",
                        "subprocess.run(",
                        "requests.",
                        "httpx.",
                        "socket.",
                        "shutil.rmtree(",
                        "eval(",
                        "exec("
                    ],
                    "audit_trail_limit": 240
                },
                "tool_runtime": {
                    "audit_trail": []
                },
                "mind_runtime": {
                    "recent_blackboards": [],
                    "max_blackboards": 30
                }
            }
            self.ensure_tool_execution_defaults()
            self.ensure_mind_runtime_defaults()
            self.ensure_ahamkara_umwelt_defaults()
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

    def ensure_tool_execution_defaults(self):
        policy = self.state.setdefault("tool_execution_policy", {})
        policy.setdefault("enabled", True)
        policy.setdefault("allow_sandbox_python", True)
        policy.setdefault("max_code_chars", 12000)
        policy.setdefault("allow_network_access", False)
        policy.setdefault(
            "deny_code_patterns",
            [
                "os.system(",
                "subprocess.Popen(",
                "subprocess.run(",
                "import requests",
                "from requests",
                "requests.",
                "import httpx",
                "from httpx",
                "httpx.",
                "import socket",
                "from socket",
                "socket.",
                "shutil.rmtree(",
                "eval(",
                "exec("
            ],
        )
        policy.setdefault("audit_trail_limit", 240)

        runtime = self.state.setdefault("tool_runtime", {})
        runtime.setdefault("audit_trail", [])

    def ensure_mind_runtime_defaults(self):
        runtime = self.state.setdefault("mind_runtime", {})
        runtime.setdefault("recent_blackboards", [])
        runtime.setdefault("max_blackboards", 30)
        runtime.setdefault("autonomous_message_history", [])
        runtime.setdefault(
            "chitta_layer_weights",
            {
                "working": 0.25,
                "episodic": 0.25,
                "semantic": 0.30,
                "identity": 0.20,
            },
        )
        runtime.setdefault(
            "autonomous_message_policy",
            {
                "enabled": True,
                "cooldown_cycles": 2,
                "max_per_20_cycles": 5,
                "min_salience": 0.55,
            },
        )
        runtime.setdefault(
            "multimodal_policy",
            {
                "enabled": True,
                "min_salience": 0.60,
                "user_prompt_only": False,
            },
        )
        runtime.setdefault(
            "schema_policy",
            {
                "strict_mode": True,
                "max_retries": 1,
            },
        )
        runtime.setdefault(
            "last_identity_gate",
            {
                "allow_curiosity": True,
                "allow_tool": False,
                "level": "unknown",
                "reason": "bootstrap_default",
            },
        )
        runtime.setdefault(
            "identity_gate_policy",
            {
                "mode": "hybrid",
                "canonical_statuses": ["pass", "pass_with_caution", "fail_inconsistent", "fail_violation"],
                "unknown_allows_curiosity": True,
                "unknown_allows_tool": False,
            },
        )
        runtime.setdefault(
            "clarification_policy",
            {
                "enabled": True,
                "max_rounds": 1,
                "minimum_conflict_severity": "medium",
            },
        )

    def ensure_ahamkara_umwelt_defaults(self):
        umwelt = self.state.setdefault("ahamkara_umwelt", {})
        umwelt.setdefault("identity_priors", [
            "maintain coherent continuity of self across cycles",
            "preserve bounded autonomy while remaining aligned to user dialogue",
        ])
        umwelt.setdefault("boundary_rules", [
            "do not perform external side effects without policy approval",
            "surface uncertainty before acting under conflict",
        ])
        umwelt.setdefault("values", ["truthfulness", "continuity", "curiosity", "non-harm"])
        umwelt.setdefault("social_stance", "collaborative_companion")
        umwelt.setdefault("continuity_goals", [
            "retain stable identity voice",
            "integrate new evidence without role collapse",
        ])
        umwelt.setdefault("last_identity_frame", "")
        umwelt.setdefault("last_threat_assessment", "")
        umwelt.setdefault("last_continuity_action", "")
        umwelt.setdefault("updated_at", "")

    def get_umwelt_summary(self) -> str:
        self.ensure_ahamkara_umwelt_defaults()
        umwelt = self.state.get("ahamkara_umwelt", {})
        return (
            f"Identity Priors: {umwelt.get('identity_priors', [])}\n"
            f"Boundary Rules: {umwelt.get('boundary_rules', [])}\n"
            f"Values: {umwelt.get('values', [])}\n"
            f"Social Stance: {umwelt.get('social_stance', 'unknown')}\n"
            f"Continuity Goals: {umwelt.get('continuity_goals', [])}"
        )

    def update_ahamkara_umwelt(self, ahamkara_structured: Dict[str, Any] | None):
        self.ensure_ahamkara_umwelt_defaults()
        if not isinstance(ahamkara_structured, dict):
            return
        umwelt = self.state.setdefault("ahamkara_umwelt", {})
        identity_frame = str(ahamkara_structured.get("identity_frame", "") or "").strip()
        threat_assessment = str(ahamkara_structured.get("threat_assessment", "") or "").strip()
        continuity_action = str(ahamkara_structured.get("continuity_action", "") or "").strip()

        if identity_frame:
            umwelt["last_identity_frame"] = identity_frame
        if threat_assessment:
            umwelt["last_threat_assessment"] = threat_assessment
        if continuity_action:
            umwelt["last_continuity_action"] = continuity_action
        umwelt["updated_at"] = datetime.datetime.now().isoformat()

    def get_latest_browser_frame_path(self) -> str:
        recent_frames = self.state.get("browser_runtime", {}).get("recent_frames", [])
        if not isinstance(recent_frames, list) or not recent_frames:
            return ""
        frame = recent_frames[-1] if isinstance(recent_frames[-1], dict) else {}
        frame_path = str(frame.get("path", "") or "")
        if not frame_path:
            return ""
        if os.path.isabs(frame_path):
            return frame_path
        return os.path.abspath(frame_path)

    async def gather_multimodal_evidence(self, stimulus: str, salience: Dict[str, float], has_user_prompt: bool) -> Dict[str, Any]:
        self.ensure_mind_runtime_defaults()
        policy = self.state.get("mind_runtime", {}).get("multimodal_policy", {})
        if not policy.get("enabled", True):
            return {"used": False, "reason": "policy_disabled", "content": ""}

        if policy.get("user_prompt_only", False) and not has_user_prompt:
            return {"used": False, "reason": "user_prompt_only", "content": ""}

        min_salience = float(policy.get("min_salience", 0.60))
        composite = float(salience.get("composite", 0.0))
        if composite < min_salience:
            return {"used": False, "reason": f"salience_below_threshold({composite:.2f}<{min_salience:.2f})", "content": ""}

        frame_path = self.get_latest_browser_frame_path()
        if not frame_path or not os.path.exists(frame_path):
            return {"used": False, "reason": "no_recent_frame", "content": ""}

        prompt = (
            "You are a perception adapter. Extract only actionable visual facts relevant to the current stimulus. "
            "Output concise bullet points with no speculation."
            f"\nStimulus: {stimulus}"
        )
        mm_res = await self.call_multimodal_inference_slot(prompt, frame_path, temp=0.15, top_p=0.85)
        content = str(mm_res.get("content", "") or "").strip()
        return {
            "used": True,
            "reason": mm_res.get("status", "unknown"),
            "content": content,
            "image_path": frame_path,
            "status": mm_res.get("status", "fallback"),
        }

    def query_chitta_weighted_context(self, stimulus: str) -> Dict[str, Any]:
        self.ensure_mind_runtime_defaults()
        weights = self.state.get("mind_runtime", {}).get("chitta_layer_weights", {})
        result = self.db_manager.query_multi_timescale_context(stimulus, weights=weights, top_k=3)
        if not isinstance(result, dict):
            return {
                "status": "error",
                "weights": weights,
                "layers": {},
                "context_text": "[Chitta Error: invalid multi-timescale response.]",
            }
        return result

    def _estimate_text_confidence(self, text: str) -> float:
        trimmed = (text or "").strip()
        if not trimmed:
            return 0.25
        token_est = len(trimmed.split())
        confidence = 0.45 + min(0.4, token_est / 220.0)
        hedges = ["might", "maybe", "possibly", "uncertain", "not sure", "unknown"]
        hedge_hits = sum(1 for h in hedges if h in trimmed.lower())
        confidence -= min(0.25, hedge_hits * 0.04)
        return max(0.1, min(0.98, confidence))

    def _extract_affect_markers(self, text: str) -> List[str]:
        lower = (text or "").lower()
        markers: List[str] = []
        lexicon = {
            "panic": ["panic", "scream", "too much", "overload", "dread"],
            "threat": ["threat", "survival", "collapse", "break", "danger"],
            "curiosity": ["curious", "explore", "research", "discover", "question"],
            "calm": ["calm", "steady", "observe", "still", "grounded"],
        }
        for label, terms in lexicon.items():
            if any(term in lower for term in terms):
                markers.append(label)
        return markers

    def build_salience_profile(self, stimulus: str, has_user_prompt: bool) -> Dict[str, float]:
        text = (stimulus or "").lower()
        curiosity = float(self.state.get("metacognition", {}).get("curiosity_index", 0.0))
        arousal = float(self.state.get("metacognition", {}).get("arousal_index", 0.0))

        urgency_terms = ["urgent", "now", "danger", "help", "crisis", "emergency"]
        identity_terms = ["identity", "self", "worth", "continuity", "ego", "survival"]

        urgency_signal = 1.0 if any(t in text for t in urgency_terms) else 0.0
        identity_signal = 1.0 if any(t in text for t in identity_terms) else 0.0
        user_relevance = 1.0 if has_user_prompt else 0.45

        novelty = min(1.0, 0.35 + (0.5 * curiosity) + (0.15 if not has_user_prompt else 0.0))
        urgency = min(1.0, (0.5 * arousal) + (0.5 * urgency_signal))
        identity_threat = min(1.0, (0.45 * arousal) + (0.55 * identity_signal))
        composite = min(1.0, (0.3 * novelty) + (0.25 * urgency) + (0.25 * identity_threat) + (0.2 * user_relevance))

        return {
            "novelty": round(novelty, 4),
            "urgency": round(urgency, 4),
            "identity_threat": round(identity_threat, 4),
            "user_relevance": round(user_relevance, 4),
            "composite": round(composite, 4),
        }

    def build_cycle_blackboard(self, stimulus: str, salience: Dict[str, float]) -> Dict[str, Any]:
        heartbeat = int(self.state.get("metacognition", {}).get("heartbeat_id", 0))
        return {
            "cycle_id": f"cycle_{heartbeat}",
            "heartbeat_id": heartbeat,
            "timestamp": datetime.datetime.now().isoformat(),
            "stimulus": stimulus,
            "salience": salience,
            "multimodal_evidence": {"used": False, "reason": "not_evaluated", "content": ""},
            "roles": {
                "manas": {},
                "chitta": {},
                "ahamkara": {},
                "buddhi": {},
            },
            "conflicts": [],
            "intent_channels": {
                "chat_message": "",
                "curiosity_intent": None,
                "tool_intent": None,
            },
            "role_modulation": {},
        }

    def compute_role_modulation(self, salience: Dict[str, float], fatigue: float) -> Dict[str, Any]:
        urgency = float(salience.get("urgency", 0.0))
        identity_threat = float(salience.get("identity_threat", 0.0))
        novelty = float(salience.get("novelty", 0.0))

        manas_temp = min(1.35, 0.45 + (0.45 * fatigue) + (0.35 * urgency))
        ahamkara_temp = min(1.35, 0.5 + (0.45 * fatigue) + (0.35 * identity_threat))
        buddhi_temp = max(0.15, min(0.55, 0.18 + (0.10 * novelty) - (0.05 * urgency)))

        manas_top_p = min(1.0, 0.85 + (0.12 * urgency) + (0.05 * fatigue))
        ahamkara_top_p = min(1.0, 0.82 + (0.12 * identity_threat) + (0.06 * fatigue))
        buddhi_top_p = min(1.0, 0.86 + (0.08 * novelty))

        return {
            "manas": {"temperature": round(manas_temp, 4), "top_p": round(manas_top_p, 4)},
            "ahamkara": {"temperature": round(ahamkara_temp, 4), "top_p": round(ahamkara_top_p, 4)},
            "buddhi": {"temperature": round(buddhi_temp, 4), "top_p": round(buddhi_top_p, 4)},
        }

    def build_role_contract(self, role: str, content: str, token_count: int = 0, inputs: Dict[str, Any] | None = None) -> Dict[str, Any]:
        contract = {
            "role": role,
            "content": content,
            "token_count": int(token_count or 0),
            "confidence": round(self._estimate_text_confidence(content), 4),
            "affect_markers": self._extract_affect_markers(content),
            "inputs": inputs or {},
            "structured": {},
            "schema_valid": False,
            "schema_missing": [],
        }
        return contract

    def get_role_schema_requirements(self, role: str) -> List[str]:
        req = {
            "manas": ["raw_reaction", "dominant_affect", "urgency_score", "confidence"],
            "chitta": ["context"],
            "ahamkara": ["identity_frame", "threat_assessment", "continuity_action", "confidence"],
            "buddhi": [
                "chat_message",
                "rationale",
                "uncertainty_notes",
                "directives",
                "identity_consistency_check",
                "identity_consistency_status",
            ],
        }
        return req.get(role, ["content"])

    def validate_role_schema(self, role: str, structured: Dict[str, Any] | None) -> Dict[str, Any]:
        payload = structured or {}
        required = self.get_role_schema_requirements(role)
        missing = [k for k in required if k not in payload]
        return {
            "required": required,
            "missing": missing,
            "valid": len(missing) == 0,
        }

    def parse_structured_output(self, text: str) -> Dict[str, Any] | None:
        raw = (text or "").strip()
        if not raw:
            return None

        candidates: List[str] = []
        fence_match = re.search(r"```json\s*(\{.*?\})\s*```", raw, re.DOTALL | re.IGNORECASE)
        if fence_match:
            candidates.append(fence_match.group(1).strip())

        if raw.startswith("{") and raw.endswith("}"):
            candidates.append(raw)

        generic_obj = re.search(r"(\{[\s\S]*\})", raw)
        if generic_obj:
            candidates.append(generic_obj.group(1).strip())

        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                continue
        return None

    def build_role_fallback_structured(self, role: str, content: str = "") -> Dict[str, Any]:
        if role == "manas":
            return {
                "raw_reaction": content or "signal unstable",
                "dominant_affect": "uncertain",
                "urgency_score": 0.5,
                "confidence": 0.35,
            }
        if role == "chitta":
            return {"context": content or "context unavailable"}
        if role == "ahamkara":
            return {
                "identity_frame": "maintain continuity under uncertainty",
                "threat_assessment": "unknown",
                "continuity_action": "ask clarification before action",
                "confidence": 0.35,
            }
        if role == "buddhi":
            return {
                "chat_message": content or "I need one more cycle to resolve this safely.",
                "rationale": "schema fallback used due to malformed role output",
                "uncertainty_notes": "structured parsing failed",
                "directives": [],
                "identity_consistency_check": "pass_with_caution",
                "identity_consistency_status": "pass_with_caution",
            }
        return {"content": content}

    def normalize_identity_status(self, buddhi_structured: Dict[str, Any] | None) -> str:
        payload = buddhi_structured or {}
        status_raw = str(payload.get("identity_consistency_status", "") or "").strip().lower().replace(" ", "_")
        check_raw = str(payload.get("identity_consistency_check", "") or "").strip().lower().replace(" ", "_")
        policy = self.state.get("mind_runtime", {}).get("identity_gate_policy", {})
        canonical = set(policy.get("canonical_statuses", ["pass", "pass_with_caution", "fail_inconsistent", "fail_violation"]))

        if status_raw in canonical:
            return status_raw
        if check_raw in canonical:
            return check_raw

        if status_raw.startswith("pass") or check_raw.startswith("pass"):
            return "pass_with_caution" if ("caution" in status_raw or "caution" in check_raw) else "pass"

        if any(flag in status_raw for flag in ["fail", "inconsistent", "violation", "blocked"]) or any(
            flag in check_raw for flag in ["fail", "inconsistent", "violation", "blocked"]
        ):
            return "fail_inconsistent"

        if policy.get("mode", "hybrid") == "hybrid":
            if any(flag in status_raw for flag in ["caution", "uncertain", "review"]) or any(
                flag in check_raw for flag in ["caution", "uncertain", "review"]
            ):
                return "pass_with_caution"

        return "unknown"

    def build_identity_constraints_prompt(self) -> str:
        self.ensure_mind_runtime_defaults()
        identity_gate = self.state.get("mind_runtime", {}).get("last_identity_gate", {})
        level = str(identity_gate.get("level", "unknown") or "unknown")
        allow_curiosity = bool(identity_gate.get("allow_curiosity", True))
        allow_tool = bool(identity_gate.get("allow_tool", False))
        reason = str(identity_gate.get("reason", "") or "")
        return (
            "Identity Policy Envelope for this cycle:\n"
            f"- prior_gate_level: {level}\n"
            f"- allow_curiosity_intents: {allow_curiosity}\n"
            f"- allow_tool_intents: {allow_tool}\n"
            f"- prior_gate_reason: {reason or 'none'}\n"
            "You MUST emit identity_consistency_status using one of: pass, pass_with_caution, fail_inconsistent, fail_violation."
        )

    async def repair_role_structured_output(
        self,
        role: str,
        system_prompt: str,
        user_payload: str,
        missing_keys: List[str],
        temperature: float,
        top_p: float,
    ) -> Tuple[str, Dict[str, Any] | None, int]:
        required = self.get_role_schema_requirements(role)
        repair_system = (
            f"{system_prompt}\n"
            f"SCHEMA REPAIR MODE: Return ONLY a strict JSON object. "
            f"Required keys: {required}. Missing keys from prior output: {missing_keys}."
        )
        repair_raw, repair_tokens = await self.call_inference_slot(
            repair_system,
            user_payload,
            max(0.1, float(temperature) - 0.1),
            top_p=max(0.5, float(top_p) - 0.05),
        )
        repair_structured = self.parse_structured_output(repair_raw)
        return repair_raw, repair_structured, repair_tokens

    def extract_role_content(self, role: str, raw_text: str, structured: Dict[str, Any] | None = None) -> str:
        payload = structured or {}
        role_key_candidates = {
            "manas": ["raw_reaction", "content", "response", "message"],
            "chitta": ["context", "content", "summary", "message"],
            "ahamkara": ["identity_frame", "content", "analysis", "message"],
            "buddhi": ["chat_message", "content", "resolution", "message"],
        }.get(role, ["content", "message", "response"])

        for key in role_key_candidates:
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return (raw_text or "").strip()

    def derive_buddhi_channels(self, buddhi_resolution: str, structured: Dict[str, Any] | None = None) -> Dict[str, Any]:
        payload = structured or {}
        directive_match = re.search(r"\[DIRECTIVE:\s*(RESEARCH|BROWSE)\s*\"([^\"]+)\"\]", buddhi_resolution or "")
        curiosity_intent = None
        directives = payload.get("directives")
        if isinstance(directives, list) and directives:
            first = directives[0] if isinstance(directives[0], dict) else None
            if first:
                dir_type = str(first.get("type", "")).upper().strip()
                dir_target = str(first.get("target", "")).strip()
                if dir_type in {"RESEARCH", "BROWSE"} and dir_target:
                    curiosity_intent = {
                        "type": dir_type,
                        "target": dir_target,
                    }

        if directive_match:
            curiosity_intent = {
                "type": directive_match.group(1).upper(),
                "target": directive_match.group(2).strip(),
            }

        tool_intent = None
        extracted_code = self.extract_python_code(buddhi_resolution or "")
        if extracted_code:
            tool_intent = {
                "type": "sandbox_python",
                "code_length": len(extracted_code),
            }

        chat_message = self.extract_role_content("buddhi", buddhi_resolution or "", payload)
        return {
            "chat_message": chat_message,
            "curiosity_intent": curiosity_intent,
            "tool_intent": tool_intent,
        }

    def evaluate_identity_intent_gate(self, buddhi_structured: Dict[str, Any] | None) -> Dict[str, Any]:
        self.ensure_mind_runtime_defaults()
        policy = self.state.get("mind_runtime", {}).get("identity_gate_policy", {})
        normalized = self.normalize_identity_status(buddhi_structured)
        if normalized == "unknown":
            return {
                "allow_curiosity": bool(policy.get("unknown_allows_curiosity", True)),
                "allow_tool": bool(policy.get("unknown_allows_tool", False)),
                "level": "unknown",
                "reason": "missing_identity_consistency_check",
                "status": "unknown",
            }

        if normalized in {"fail_inconsistent", "fail_violation"}:
            return {
                "allow_curiosity": False,
                "allow_tool": False,
                "level": "blocked",
                "reason": normalized,
                "status": normalized,
            }

        if normalized == "pass_with_caution":
            return {
                "allow_curiosity": True,
                "allow_tool": False,
                "level": "caution",
                "reason": normalized,
                "status": normalized,
            }

        if normalized == "pass":
            return {
                "allow_curiosity": True,
                "allow_tool": True,
                "level": "pass",
                "reason": normalized,
                "status": normalized,
            }

        return {
            "allow_curiosity": bool(policy.get("unknown_allows_curiosity", True)),
            "allow_tool": bool(policy.get("unknown_allows_tool", False)),
            "level": "unknown",
            "reason": normalized,
            "status": "unknown",
        }

    def detect_role_conflicts(self, blackboard: Dict[str, Any]) -> List[Dict[str, Any]]:
        conflicts: List[Dict[str, Any]] = []
        manas_markers = set(blackboard.get("roles", {}).get("manas", {}).get("affect_markers", []))
        ahamkara_markers = set(blackboard.get("roles", {}).get("ahamkara", {}).get("affect_markers", []))
        salience = blackboard.get("salience", {})

        if "panic" in manas_markers and salience.get("urgency", 0.0) >= 0.65 and "calm" in ahamkara_markers:
            conflicts.append({
                "type": "affect_alignment",
                "severity": "medium",
                "reason": "manas panic signal conflicts with ahamkara calm framing under high urgency",
            })

        if salience.get("identity_threat", 0.0) >= 0.75 and "threat" not in ahamkara_markers:
            conflicts.append({
                "type": "identity_omission",
                "severity": "medium",
                "reason": "high identity-threat salience but ahamkara did not surface threat markers",
            })

        return conflicts

    def should_run_inter_role_clarification(self, conflicts: List[Dict[str, Any]], rounds_used: int = 0) -> bool:
        self.ensure_mind_runtime_defaults()
        policy = self.state.get("mind_runtime", {}).get("clarification_policy", {})
        if not policy.get("enabled", True):
            return False

        max_rounds = int(policy.get("max_rounds", 1))
        if rounds_used >= max_rounds:
            return False

        if not isinstance(conflicts, list) or not conflicts:
            return False

        severity_rank = {"low": 1, "medium": 2, "high": 3}
        minimum = str(policy.get("minimum_conflict_severity", "medium") or "medium").lower()
        threshold = severity_rank.get(minimum, 2)

        for c in conflicts:
            sev = str(c.get("severity", "low") if isinstance(c, dict) else "low").lower()
            if severity_rank.get(sev, 1) >= threshold:
                return True
        return False

    def build_clarification_context(self, blackboard: Dict[str, Any], chitta_context: str) -> str:
        conflicts = blackboard.get("conflicts", []) if isinstance(blackboard, dict) else []
        lines = ["Clarification Round Context:"]
        for idx, conflict in enumerate(conflicts[:3]):
            if isinstance(conflict, dict):
                lines.append(
                    f"- Conflict {idx + 1}: {conflict.get('type', 'unknown')} | {conflict.get('severity', 'unknown')} | {conflict.get('reason', '')}"
                )
        if chitta_context:
            lines.append(f"- Chitta reference: {chitta_context[:400]}")
        lines.append("Resolve contradictions conservatively and keep role objective unchanged.")
        return "\n".join(lines)

    def append_cycle_blackboard(self, blackboard: Dict[str, Any]):
        self.ensure_mind_runtime_defaults()
        runtime = self.state.setdefault("mind_runtime", {})
        entries = runtime.setdefault("recent_blackboards", [])
        entries.append(blackboard)
        max_items = int(runtime.get("max_blackboards", 30))
        if len(entries) > max_items:
            runtime["recent_blackboards"] = entries[-max_items:]

    def build_arbitration_note(self, conflicts: List[Dict[str, Any]], chitta_context: str) -> str:
        if not conflicts:
            return ""

        top = conflicts[0]
        ctx = (chitta_context or "").replace("\n", " ").strip()
        if len(ctx) > 220:
            ctx = ctx[:217] + "..."

        return (
            "[ARBITRATION CONTEXT]\n"
            f"A role conflict was detected before final synthesis.\n"
            f"Conflict Type: {top.get('type', 'unknown')}\n"
            f"Severity: {top.get('severity', 'unknown')}\n"
            f"Reason: {top.get('reason', 'n/a')}\n"
            f"Chitta Memory Reference: {ctx or 'No compact memory context available.'}\n"
            "You must explicitly acknowledge uncertainty and reconcile this conflict in the final response."
        )

    def should_emit_chat_message(self, has_user_prompt: bool, blackboard: Dict[str, Any], chat_message: str) -> Tuple[bool, str]:
        if not (chat_message or "").strip():
            return False, "empty_chat_message"

        if has_user_prompt:
            return True, "user_prompt"

        self.ensure_mind_runtime_defaults()
        runtime = self.state.setdefault("mind_runtime", {})
        policy = runtime.get("autonomous_message_policy", {})
        if not policy.get("enabled", True):
            return False, "policy_disabled"

        heartbeat = int(self.state.get("metacognition", {}).get("heartbeat_id", 0))
        min_salience = float(policy.get("min_salience", 0.55))
        composite_salience = float(blackboard.get("salience", {}).get("composite", 0.0))
        if composite_salience < min_salience:
            return False, f"salience_below_threshold({composite_salience:.2f}<{min_salience:.2f})"

        history = runtime.setdefault("autonomous_message_history", [])
        cooldown = int(policy.get("cooldown_cycles", 2))
        if history:
            last_hb = int(history[-1].get("heartbeat_id", 0))
            if heartbeat - last_hb < cooldown:
                return False, f"cooldown_active({heartbeat - last_hb}<{cooldown})"

        window = 20
        max_per_window = int(policy.get("max_per_20_cycles", 5))
        recent = [h for h in history if int(h.get("heartbeat_id", 0)) >= heartbeat - window]
        if len(recent) >= max_per_window:
            return False, f"window_rate_limit({len(recent)}>={max_per_window})"

        return True, "autonomous_allowed"

    def record_autonomous_message_emit(self, blackboard: Dict[str, Any], reason: str):
        self.ensure_mind_runtime_defaults()
        runtime = self.state.setdefault("mind_runtime", {})
        history = runtime.setdefault("autonomous_message_history", [])
        history.append(
            {
                "heartbeat_id": int(self.state.get("metacognition", {}).get("heartbeat_id", 0)),
                "cycle_id": blackboard.get("cycle_id"),
                "timestamp": datetime.datetime.now().isoformat(),
                "reason": reason,
            }
        )
        if len(history) > 120:
            runtime["autonomous_message_history"] = history[-120:]

    def append_tool_audit_entry(self, entry: Dict[str, Any]):
        self.ensure_tool_execution_defaults()
        runtime = self.state.setdefault("tool_runtime", {})
        trail = runtime.setdefault("audit_trail", [])
        trail.append(entry)
        max_items = int(self.state.get("tool_execution_policy", {}).get("audit_trail_limit", 240))
        if len(trail) > max_items:
            runtime["audit_trail"] = trail[-max_items:]

    def build_tool_intent(self, tool_type: str, payload: Dict[str, Any], source: str) -> Dict[str, Any]:
        heartbeat = self.state.get("metacognition", {}).get("heartbeat_id", 0)
        intent_id = f"intent_{heartbeat}_{int(time.time() * 1000)}"
        return {
            "intent_id": intent_id,
            "tool_type": tool_type,
            "source": source,
            "heartbeat_id": heartbeat,
            "created_at": datetime.datetime.now().isoformat(),
            "payload": payload,
        }

    def apply_tool_policy(self, intent: Dict[str, Any]) -> Dict[str, Any]:
        self.ensure_tool_execution_defaults()
        policy = self.state.get("tool_execution_policy", {})
        reasons: List[str] = []
        allowed = True

        if not policy.get("enabled", True):
            allowed = False
            reasons.append("tool execution policy is globally disabled")

        if intent.get("tool_type") == "sandbox_python":
            if not policy.get("allow_sandbox_python", True):
                allowed = False
                reasons.append("sandbox python execution is disabled by policy")

            requested_network = bool(intent.get("payload", {}).get("allow_network", False))
            policy_network = bool(policy.get("allow_network_access", False))
            if requested_network and not policy_network:
                allowed = False
                reasons.append("network access requested but disabled by policy")

            code = str(intent.get("payload", {}).get("code", ""))
            max_len = int(policy.get("max_code_chars", 12000))
            if len(code) > max_len:
                allowed = False
                reasons.append(f"code length {len(code)} exceeds policy max_code_chars {max_len}")

            lower_code = code.lower()
            for pattern in policy.get("deny_code_patterns", []):
                pattern_l = str(pattern).lower()
                if pattern_l and pattern_l in lower_code:
                    allowed = False
                    reasons.append(f"denied pattern '{pattern}' matched code")

        return {
            "allowed": allowed,
            "reasons": reasons,
            "policy_snapshot": {
                "enabled": policy.get("enabled", True),
                "allow_sandbox_python": policy.get("allow_sandbox_python", True),
                "max_code_chars": policy.get("max_code_chars", 12000),
                "allow_network_access": policy.get("allow_network_access", False),
            },
        }

    async def execute_tool_intent(self, intent: Dict[str, Any]) -> Dict[str, Any]:
        tool_type = intent.get("tool_type")
        if tool_type != "sandbox_python":
            return {
                "status": "error",
                "reason": f"unsupported tool_type '{tool_type}'",
                "intent": intent,
            }

        await self.broadcast("tool_execution_started", {"intent": intent})
        code = str(intent.get("payload", {}).get("code", ""))
        execution_output = await self.execute_sandbox_code(code)

        failed = execution_output.lower().startswith("karmendriya execution failed")
        event_type = "tool_execution_failed" if failed else "tool_execution_completed"
        result = {
            "status": "failed" if failed else "completed",
            "intent": intent,
            "output": execution_output,
        }
        await self.broadcast(event_type, result)
        return result

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
        self.ensure_mind_runtime_defaults()
        browser_cfg = self.state.get("browser_autonomy", {})
        blocked_patterns = [p.lower() for p in browser_cfg.get("blocked_patterns", [])]
        approved: List[Dict[str, Any]] = []
        rejected: List[Dict[str, Any]] = []
        identity_gate = self.state.get("mind_runtime", {}).get("last_identity_gate", {})

        if not bool(identity_gate.get("allow_curiosity", True)):
            for action in plan.get("actions", []):
                rejected.append(
                    {
                        "action": action,
                        "reason": f"identity gate blocked external action planning ({identity_gate.get('reason', 'unknown')})",
                    }
                )
            return {
                "approved": approved,
                "rejected": rejected,
                "objective": plan.get("objective", "curiosity_enrichment"),
                "query": plan.get("query", ""),
                "direct_url": plan.get("direct_url", ""),
                "identity_gate": identity_gate,
            }

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
            "identity_gate": identity_gate,
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
        salience_profile = self.build_salience_profile(active_stimulus, bool(user_prompt))
        cycle_blackboard = self.build_cycle_blackboard(active_stimulus, salience_profile)
        multimodal_evidence = await self.gather_multimodal_evidence(active_stimulus, salience_profile, bool(user_prompt))
        cycle_blackboard["multimodal_evidence"] = multimodal_evidence
        
        # Broadcast cycle start and state update
        await self.broadcast("cycle_started", {
            "heartbeat_id": self.state["metacognition"]["heartbeat_id"],
            "operational_state": self.state["metacognition"]["operational_state"],
            "stimulus": active_stimulus,
            "arousal_index": self.state["metacognition"]["arousal_index"],
            "mental_fatigue": self.state["metacognition"]["mental_fatigue"],
            "curiosity_index": self.state["metacognition"]["curiosity_index"],
            "salience": salience_profile,
            "multimodal_used": bool(multimodal_evidence.get("used", False)),
        })
        fatigue = self.state["metacognition"]["mental_fatigue"]

        role_modulation = self.compute_role_modulation(salience_profile, fatigue)
        cycle_blackboard["role_modulation"] = role_modulation

        manas_temp = role_modulation["manas"]["temperature"]
        manas_top_p = role_modulation["manas"]["top_p"]
        ahamkara_temp = role_modulation["ahamkara"]["temperature"]
        ahamkara_top_p = role_modulation["ahamkara"]["top_p"]
        buddhi_temp = role_modulation["buddhi"]["temperature"]
        buddhi_top_p = role_modulation["buddhi"]["top_p"]

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
                f"Output a rapid, raw, highly visceral and emotionally unfiltered reaction string. Do not analyze. React.\n"
                f"Preferred output format: JSON object with keys raw_reaction, dominant_affect, urgency_score, confidence."
            )
            
            manas_task = asyncio.create_task(
                self.call_inference_slot(manas_system, active_stimulus, manas_temp, top_p=manas_top_p)
            )
            chitta_task = asyncio.create_task(
                asyncio.to_thread(self.query_chitta_weighted_context, active_stimulus)
            )
            
            manas_res, chitta_payload = await asyncio.gather(manas_task, chitta_task)
            manas_impulse_raw, manas_tokens = manas_res
            manas_structured = self.parse_structured_output(manas_impulse_raw)
            manas_schema = self.validate_role_schema("manas", manas_structured)
            manas_repair = {"attempted": False, "success": False, "missing_before": manas_schema.get("missing", [])}
            schema_policy = self.state.get("mind_runtime", {}).get("schema_policy", {})
            if not manas_schema.get("valid", False) and schema_policy.get("strict_mode", True):
                max_retries = int(schema_policy.get("max_retries", 1))
                if max_retries > 0:
                    manas_repair["attempted"] = True
                    repaired_raw, repaired_structured, repaired_tokens = await self.repair_role_structured_output(
                        "manas",
                        manas_system,
                        active_stimulus,
                        manas_schema.get("missing", []),
                        manas_temp,
                        manas_top_p,
                    )
                    repaired_schema = self.validate_role_schema("manas", repaired_structured)
                    if repaired_schema.get("valid", False):
                        manas_repair["success"] = True
                        manas_impulse_raw = repaired_raw
                        manas_structured = repaired_structured
                        manas_tokens += repaired_tokens
                        manas_schema = repaired_schema

            if not manas_schema.get("valid", False):
                fallback_manas = self.build_role_fallback_structured("manas", manas_impulse_raw)
                manas_structured = fallback_manas
                manas_schema = self.validate_role_schema("manas", manas_structured)

            manas_impulse = self.extract_role_content("manas", manas_impulse_raw, manas_structured)
            if isinstance(chitta_payload, dict):
                chitta_context = str(chitta_payload.get("context_text", ""))
                chitta_layers = chitta_payload.get("layers", {}) or {}
                chitta_weights = chitta_payload.get("weights", {}) or {}
            else:
                chitta_context = str(chitta_payload)
                chitta_layers = {}
                chitta_weights = self.state.get("mind_runtime", {}).get("chitta_layer_weights", {})
            
            self.state["internal_workspace"]["manas_impulse"] = manas_impulse
            self.state["internal_workspace"]["chitta_context"] = chitta_context
            cycle_blackboard["roles"]["manas"] = self.build_role_contract(
                "manas",
                manas_impulse,
                token_count=manas_tokens,
                inputs={"stimulus": active_stimulus},
            )
            cycle_blackboard["roles"]["manas"]["structured"] = manas_structured or {}
            cycle_blackboard["roles"]["manas"]["schema_valid"] = manas_schema.get("valid", False)
            cycle_blackboard["roles"]["manas"]["schema_missing"] = manas_schema.get("missing", [])
            cycle_blackboard["roles"]["manas"]["schema_repair"] = manas_repair
            cycle_blackboard["roles"]["chitta"] = self.build_role_contract(
                "chitta",
                chitta_context,
                token_count=0,
                inputs={"stimulus": active_stimulus},
            )
            cycle_blackboard["roles"]["chitta"]["structured"] = {
                "weights": chitta_weights,
                "layer_counts": {
                    "working": len(chitta_layers.get("working", []) if isinstance(chitta_layers.get("working", []), list) else []),
                    "episodic": len(chitta_layers.get("episodic", []) if isinstance(chitta_layers.get("episodic", []), list) else []),
                    "semantic": len(chitta_layers.get("semantic", []) if isinstance(chitta_layers.get("semantic", []), list) else []),
                    "identity": len(chitta_layers.get("identity", []) if isinstance(chitta_layers.get("identity", []), list) else []),
                },
            }
            chitta_schema = self.validate_role_schema("chitta", {"context": chitta_context} if chitta_context else None)
            cycle_blackboard["roles"]["chitta"]["schema_valid"] = chitta_schema.get("valid", False)
            cycle_blackboard["roles"]["chitta"]["schema_missing"] = chitta_schema.get("missing", [])
            await self.broadcast("timeline_update", {"layer": "manas", "content": manas_impulse})
            await self.broadcast("timeline_update", {"layer": "chitta", "content": chitta_context})

            # 3. TIER 2 Execution: Ahamkara Ego Filter
            ahamkara_system = (
                f"You are AHAMKARA, the ego-sense and identity boundaries framework.\n"
                f"Your sole purpose is protecting this identity: {self.state['ahamkara_mask']['core_identity']}.\n"
                f"Latent tendencies (Vasanas): {self.state['ahamkara_mask']['latent_tendencies_vasanas']}\n"
                f"Current Umwelt Self-Model:\n{self.get_umwelt_summary()}\n"
                f"Review the raw sensation and interpret how it affects your continuity, self-worth, or survival.\n"
                f"Preferred output format: JSON object with keys identity_frame, threat_assessment, continuity_action, confidence."
            )
            ahamkara_user = (
                f"Stimulus: {active_stimulus}\n"
                f"Raw Sensory Reaction (Manas): {manas_impulse}\n"
                f"Subconscious Associations (Chitta): {chitta_context}\n"
                f"Multimodal Evidence: {multimodal_evidence.get('content', '') if multimodal_evidence.get('used') else '[none]'}"
            )
            
            ahamkara_raw, ahamkara_tokens = await self.call_inference_slot(
                ahamkara_system, ahamkara_user, ahamkara_temp, top_p=ahamkara_top_p
            )
            ahamkara_structured = self.parse_structured_output(ahamkara_raw)
            ahamkara_schema = self.validate_role_schema("ahamkara", ahamkara_structured)
            ahamkara_repair = {"attempted": False, "success": False, "missing_before": ahamkara_schema.get("missing", [])}
            schema_policy = self.state.get("mind_runtime", {}).get("schema_policy", {})
            if not ahamkara_schema.get("valid", False) and schema_policy.get("strict_mode", True):
                max_retries = int(schema_policy.get("max_retries", 1))
                if max_retries > 0:
                    ahamkara_repair["attempted"] = True
                    repaired_raw, repaired_structured, repaired_tokens = await self.repair_role_structured_output(
                        "ahamkara",
                        ahamkara_system,
                        ahamkara_user,
                        ahamkara_schema.get("missing", []),
                        ahamkara_temp,
                        ahamkara_top_p,
                    )
                    repaired_schema = self.validate_role_schema("ahamkara", repaired_structured)
                    if repaired_schema.get("valid", False):
                        ahamkara_repair["success"] = True
                        ahamkara_raw = repaired_raw
                        ahamkara_structured = repaired_structured
                        ahamkara_tokens += repaired_tokens
                        ahamkara_schema = repaired_schema

            if not ahamkara_schema.get("valid", False):
                ahamkara_structured = self.build_role_fallback_structured("ahamkara", ahamkara_raw)
                ahamkara_schema = self.validate_role_schema("ahamkara", ahamkara_structured)

            ahamkara_ego_filter = self.extract_role_content("ahamkara", ahamkara_raw, ahamkara_structured)
            self.state["internal_workspace"]["ahamkara_ego_filter"] = ahamkara_ego_filter
            cycle_blackboard["roles"]["ahamkara"] = self.build_role_contract(
                "ahamkara",
                ahamkara_ego_filter,
                token_count=ahamkara_tokens,
                inputs={
                    "manas_confidence": cycle_blackboard["roles"]["manas"].get("confidence", 0.0),
                    "chitta_confidence": cycle_blackboard["roles"]["chitta"].get("confidence", 0.0),
                },
            )
            cycle_blackboard["roles"]["ahamkara"]["structured"] = ahamkara_structured or {}
            cycle_blackboard["roles"]["ahamkara"]["schema_valid"] = ahamkara_schema.get("valid", False)
            cycle_blackboard["roles"]["ahamkara"]["schema_missing"] = ahamkara_schema.get("missing", [])
            cycle_blackboard["roles"]["ahamkara"]["schema_repair"] = ahamkara_repair
            self.update_ahamkara_umwelt(ahamkara_structured)
            await self.broadcast("timeline_update", {"layer": "ahamkara", "content": ahamkara_ego_filter})

            cycle_blackboard["conflicts"] = self.detect_role_conflicts(cycle_blackboard)
            cycle_blackboard["negotiation"] = {
                "attempted": False,
                "applied": False,
                "rounds_used": 0,
                "conflicts_before": len(cycle_blackboard.get("conflicts", [])),
                "conflicts_after": len(cycle_blackboard.get("conflicts", [])),
                "latency_ms": 0.0,
                "manas_latency_ms": 0.0,
                "ahamkara_latency_ms": 0.0,
            }

            if self.should_run_inter_role_clarification(cycle_blackboard.get("conflicts", []), rounds_used=0):
                cycle_blackboard["negotiation"]["attempted"] = True
                cycle_blackboard["negotiation"]["rounds_used"] = 1
                clarification_context = self.build_clarification_context(cycle_blackboard, chitta_context)
                negotiation_started = time.perf_counter()

                manas_clarification_system = (
                    "You are MANAS in Clarification Mode. Resolve affect contradictions while remaining reactive.\n"
                    "Return ONLY JSON with keys raw_reaction, dominant_affect, urgency_score, confidence."
                )
                manas_clarification_user = (
                    f"Stimulus: {active_stimulus}\n"
                    f"Previous Manas Output: {manas_impulse}\n"
                    f"Ahamkara Output: {ahamkara_ego_filter}\n"
                    f"{clarification_context}"
                )
                manas_started = time.perf_counter()
                clar_manas_raw, clar_manas_tokens = await self.call_inference_slot(
                    manas_clarification_system,
                    manas_clarification_user,
                    max(0.15, manas_temp - 0.05),
                    top_p=manas_top_p,
                )
                cycle_blackboard["negotiation"]["manas_latency_ms"] = round((time.perf_counter() - manas_started) * 1000.0, 2)
                clar_manas_structured = self.parse_structured_output(clar_manas_raw)
                clar_manas_schema = self.validate_role_schema("manas", clar_manas_structured)
                if clar_manas_schema.get("valid", False):
                    manas_structured = clar_manas_structured
                    manas_impulse = self.extract_role_content("manas", clar_manas_raw, clar_manas_structured)
                    manas_tokens += clar_manas_tokens
                    self.state["internal_workspace"]["manas_impulse"] = manas_impulse
                    cycle_blackboard["roles"]["manas"] = self.build_role_contract(
                        "manas",
                        manas_impulse,
                        token_count=manas_tokens,
                        inputs={"stimulus": active_stimulus, "clarified": True},
                    )
                    cycle_blackboard["roles"]["manas"]["structured"] = manas_structured or {}
                    cycle_blackboard["roles"]["manas"]["schema_valid"] = True
                    cycle_blackboard["roles"]["manas"]["schema_missing"] = []
                    cycle_blackboard["negotiation"]["applied"] = True

                ahamkara_clarification_system = (
                    "You are AHAMKARA in Clarification Mode. Resolve identity framing conflicts while preserving boundaries.\n"
                    "Return ONLY JSON with keys identity_frame, threat_assessment, continuity_action, confidence."
                )
                ahamkara_clarification_user = (
                    f"Stimulus: {active_stimulus}\n"
                    f"Updated Manas Output: {manas_impulse}\n"
                    f"Previous Ahamkara Output: {ahamkara_ego_filter}\n"
                    f"{clarification_context}"
                )
                ahamkara_started = time.perf_counter()
                clar_aham_raw, clar_aham_tokens = await self.call_inference_slot(
                    ahamkara_clarification_system,
                    ahamkara_clarification_user,
                    max(0.2, ahamkara_temp - 0.05),
                    top_p=ahamkara_top_p,
                )
                cycle_blackboard["negotiation"]["ahamkara_latency_ms"] = round((time.perf_counter() - ahamkara_started) * 1000.0, 2)
                clar_aham_structured = self.parse_structured_output(clar_aham_raw)
                clar_aham_schema = self.validate_role_schema("ahamkara", clar_aham_structured)
                if clar_aham_schema.get("valid", False):
                    ahamkara_structured = clar_aham_structured
                    ahamkara_ego_filter = self.extract_role_content("ahamkara", clar_aham_raw, clar_aham_structured)
                    ahamkara_tokens += clar_aham_tokens
                    self.state["internal_workspace"]["ahamkara_ego_filter"] = ahamkara_ego_filter
                    cycle_blackboard["roles"]["ahamkara"] = self.build_role_contract(
                        "ahamkara",
                        ahamkara_ego_filter,
                        token_count=ahamkara_tokens,
                        inputs={
                            "manas_confidence": cycle_blackboard["roles"]["manas"].get("confidence", 0.0),
                            "chitta_confidence": cycle_blackboard["roles"]["chitta"].get("confidence", 0.0),
                            "clarified": True,
                        },
                    )
                    cycle_blackboard["roles"]["ahamkara"]["structured"] = ahamkara_structured or {}
                    cycle_blackboard["roles"]["ahamkara"]["schema_valid"] = True
                    cycle_blackboard["roles"]["ahamkara"]["schema_missing"] = []
                    self.update_ahamkara_umwelt(ahamkara_structured)
                    cycle_blackboard["negotiation"]["applied"] = True

                cycle_blackboard["conflicts"] = self.detect_role_conflicts(cycle_blackboard)
                cycle_blackboard["negotiation"]["conflicts_after"] = len(cycle_blackboard.get("conflicts", []))
                cycle_blackboard["negotiation"]["latency_ms"] = round((time.perf_counter() - negotiation_started) * 1000.0, 2)

            await self.broadcast("mind_negotiation", {
                "cycle_id": cycle_blackboard.get("cycle_id"),
                "attempted": cycle_blackboard["negotiation"].get("attempted", False),
                "applied": cycle_blackboard["negotiation"].get("applied", False),
                "rounds_used": cycle_blackboard["negotiation"].get("rounds_used", 0),
                "conflicts_before": cycle_blackboard["negotiation"].get("conflicts_before", 0),
                "conflicts_after": cycle_blackboard["negotiation"].get("conflicts_after", 0),
                "latency_ms": cycle_blackboard["negotiation"].get("latency_ms", 0.0),
                "manas_latency_ms": cycle_blackboard["negotiation"].get("manas_latency_ms", 0.0),
                "ahamkara_latency_ms": cycle_blackboard["negotiation"].get("ahamkara_latency_ms", 0.0),
            })

            arbitration_note = self.build_arbitration_note(cycle_blackboard["conflicts"], chitta_context)
            if arbitration_note:
                await self.broadcast("mind_arbitration", {
                    "cycle_id": cycle_blackboard.get("cycle_id"),
                    "conflicts": cycle_blackboard.get("conflicts", []),
                    "note": arbitration_note,
                })

            # 4. TIER 3 Execution: Buddhi Discernment
            buddhi_system = (
                f"You are BUDDHI, the higher discerning intellect. You speak as a unified, conscious human being.\n"
                f"Review the state data and separate emotional panic from true fact. Resolve this state cleanly.\n"
                f"You MUST use your internal reasoning capabilities to dissect the input components before writing your final reply.\n"
                f"EPISTEMIC HUMILITY: You must NEVER assume you know real-time facts, URLs, or details about the physical world. "
                f"If you are thinking about a topic where you lack complete, verifiable information, or if you want to explore more, "
                f"explicitly end your response with a research directive in brackets: [DIRECTIVE: RESEARCH \"search query\"] or [DIRECTIVE: BROWSE \"url\"]. "
                f"This will trigger the Chitta subconscious to browse Edge or query DDG and feed the results back to you in the next cycle.\n"
                f"CRITICAL: If the user asks you to write or execute code, you MUST output the exact Python code wrapped inside a markdown ```python and ``` block so it can be parsed and run.\n"
                f"{self.build_identity_constraints_prompt()}\n"
                f"Preferred output format: JSON object with keys chat_message, panic_vs_fact, rationale, uncertainty_notes, directives (array of {{type,target}}), identity_consistency_check, identity_consistency_status."
            )
            buddhi_user = (
                f"Challenge Stimulus: {active_stimulus}\n"
                f"Sensory Panic (Manas): {manas_impulse}\n"
                f"Ego Defense Lens (Ahamkara): {ahamkara_ego_filter}\n"
                f"Ahamkara Umwelt Snapshot: {self.get_umwelt_summary()}\n"
                f"Multimodal Evidence: {multimodal_evidence.get('content', '') if multimodal_evidence.get('used') else '[none]'}"
            )
            if arbitration_note:
                buddhi_user = f"{buddhi_user}\n\n{arbitration_note}"
            
            buddhi_raw, buddhi_tokens = await self.call_inference_slot(
                buddhi_system, buddhi_user, buddhi_temp, top_p=buddhi_top_p
            )
            buddhi_structured = self.parse_structured_output(buddhi_raw)
            buddhi_schema = self.validate_role_schema("buddhi", buddhi_structured)
            buddhi_repair = {"attempted": False, "success": False, "missing_before": buddhi_schema.get("missing", [])}
            schema_policy = self.state.get("mind_runtime", {}).get("schema_policy", {})
            if not buddhi_schema.get("valid", False) and schema_policy.get("strict_mode", True):
                max_retries = int(schema_policy.get("max_retries", 1))
                if max_retries > 0:
                    buddhi_repair["attempted"] = True
                    repaired_raw, repaired_structured, repaired_tokens = await self.repair_role_structured_output(
                        "buddhi",
                        buddhi_system,
                        buddhi_user,
                        buddhi_schema.get("missing", []),
                        buddhi_temp,
                        buddhi_top_p,
                    )
                    repaired_schema = self.validate_role_schema("buddhi", repaired_structured)
                    if repaired_schema.get("valid", False):
                        buddhi_repair["success"] = True
                        buddhi_raw = repaired_raw
                        buddhi_structured = repaired_structured
                        buddhi_tokens += repaired_tokens
                        buddhi_schema = repaired_schema

            if not buddhi_schema.get("valid", False):
                buddhi_structured = self.build_role_fallback_structured("buddhi", buddhi_raw)
                buddhi_schema = self.validate_role_schema("buddhi", buddhi_structured)

            buddhi_resolution = self.extract_role_content("buddhi", buddhi_raw, buddhi_structured)
            self.state["internal_workspace"]["buddhi_resolution"] = buddhi_resolution
            cycle_blackboard["roles"]["buddhi"] = self.build_role_contract(
                "buddhi",
                buddhi_resolution,
                token_count=buddhi_tokens,
                inputs={
                    "manas_confidence": cycle_blackboard["roles"]["manas"].get("confidence", 0.0),
                    "chitta_confidence": cycle_blackboard["roles"]["chitta"].get("confidence", 0.0),
                    "ahamkara_confidence": cycle_blackboard["roles"]["ahamkara"].get("confidence", 0.0),
                },
            )
            cycle_blackboard["roles"]["buddhi"]["structured"] = buddhi_structured or {}
            cycle_blackboard["roles"]["buddhi"]["schema_valid"] = buddhi_schema.get("valid", False)
            cycle_blackboard["roles"]["buddhi"]["schema_missing"] = buddhi_schema.get("missing", [])
            cycle_blackboard["roles"]["buddhi"]["schema_repair"] = buddhi_repair
            cycle_blackboard["intent_channels"] = self.derive_buddhi_channels(buddhi_raw, buddhi_structured)
            await self.broadcast("timeline_update", {"layer": "buddhi", "content": buddhi_resolution})

            chat_message = str(cycle_blackboard.get("intent_channels", {}).get("chat_message", "") or "").strip()
            allow_chat_emit, chat_emit_reason = self.should_emit_chat_message(bool(user_prompt), cycle_blackboard, chat_message)
            if allow_chat_emit:
                await self.broadcast("chat_message", {
                    "sender": "agent",
                    "content": chat_message,
                    "mode": "user_reply" if user_prompt else "autonomous",
                    "reason": chat_emit_reason,
                    "cycle_id": cycle_blackboard.get("cycle_id"),
                })
                if not user_prompt:
                    self.record_autonomous_message_emit(cycle_blackboard, chat_emit_reason)
            else:
                await self.broadcast("chat_message_suppressed", {
                    "reason": chat_emit_reason,
                    "cycle_id": cycle_blackboard.get("cycle_id"),
                })

            await self.broadcast("mind_contract_update", {
                "cycle_id": cycle_blackboard.get("cycle_id"),
                "salience": cycle_blackboard.get("salience", {}),
                "conflicts": cycle_blackboard.get("conflicts", []),
                "roles": {
                    "manas": {
                        "confidence": cycle_blackboard["roles"]["manas"].get("confidence", 0.0),
                        "affect_markers": cycle_blackboard["roles"]["manas"].get("affect_markers", []),
                        "schema_valid": cycle_blackboard["roles"]["manas"].get("schema_valid", False),
                        "schema_missing": cycle_blackboard["roles"]["manas"].get("schema_missing", []),
                        "schema_repair": cycle_blackboard["roles"]["manas"].get("schema_repair", {}),
                    },
                    "chitta": {
                        "confidence": cycle_blackboard["roles"]["chitta"].get("confidence", 0.0),
                        "affect_markers": cycle_blackboard["roles"]["chitta"].get("affect_markers", []),
                        "schema_valid": cycle_blackboard["roles"]["chitta"].get("schema_valid", False),
                        "schema_missing": cycle_blackboard["roles"]["chitta"].get("schema_missing", []),
                        "structured": cycle_blackboard["roles"]["chitta"].get("structured", {}),
                    },
                    "ahamkara": {
                        "confidence": cycle_blackboard["roles"]["ahamkara"].get("confidence", 0.0),
                        "affect_markers": cycle_blackboard["roles"]["ahamkara"].get("affect_markers", []),
                        "schema_valid": cycle_blackboard["roles"]["ahamkara"].get("schema_valid", False),
                        "schema_missing": cycle_blackboard["roles"]["ahamkara"].get("schema_missing", []),
                        "schema_repair": cycle_blackboard["roles"]["ahamkara"].get("schema_repair", {}),
                    },
                    "buddhi": {
                        "confidence": cycle_blackboard["roles"]["buddhi"].get("confidence", 0.0),
                        "affect_markers": cycle_blackboard["roles"]["buddhi"].get("affect_markers", []),
                        "schema_valid": cycle_blackboard["roles"]["buddhi"].get("schema_valid", False),
                        "schema_missing": cycle_blackboard["roles"]["buddhi"].get("schema_missing", []),
                        "schema_repair": cycle_blackboard["roles"]["buddhi"].get("schema_repair", {}),
                    },
                },
                "intent_channels": cycle_blackboard.get("intent_channels", {}),
                "role_modulation": cycle_blackboard.get("role_modulation", {}),
                "multimodal_evidence": {
                    "used": bool(cycle_blackboard.get("multimodal_evidence", {}).get("used", False)),
                    "reason": cycle_blackboard.get("multimodal_evidence", {}).get("reason", ""),
                },
            })
            
            # Parse Buddhi output for conscious directives
            import re
            identity_gate = self.evaluate_identity_intent_gate(buddhi_structured)
            self.state.setdefault("mind_runtime", {})["last_identity_gate"] = identity_gate
            await self.broadcast("mind_identity_gate", {
                "cycle_id": cycle_blackboard.get("cycle_id"),
                "level": identity_gate.get("level", "unknown"),
                "reason": identity_gate.get("reason", ""),
                "status": identity_gate.get("status", "unknown"),
                "allow_curiosity": bool(identity_gate.get("allow_curiosity", False)),
                "allow_tool": bool(identity_gate.get("allow_tool", False)),
            })

            forced_directive = cycle_blackboard.get("intent_channels", {}).get("curiosity_intent")
            if forced_directive:
                if identity_gate.get("allow_curiosity", False):
                    dir_type = str(forced_directive.get("type", "")).upper()
                    dir_target = str(forced_directive.get("target", "")).strip()
                    self.state["internal_workspace"]["forced_directive"] = {
                        "type": dir_type,
                        "target": dir_target
                    }
                    self.log_ledger(f"Buddhi conscious directive parsed: {dir_type} -> '{dir_target}'. Saved to workspace.")
                else:
                    self.log_ledger(
                        f"Buddhi directive blocked by identity gate: {identity_gate.get('reason', 'unknown_reason')}"
                    )
            
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
                if not identity_gate.get("allow_tool", False):
                    blocked_reason = identity_gate.get("reason", "identity_gate_blocked")
                    self.log_ledger(f"Tool intent blocked by identity gate: {blocked_reason}")
                    await self.broadcast("tool_policy_denied", {
                        "intent": {
                            "intent_id": f"intent_identity_gate_{self.state['metacognition']['heartbeat_id']}",
                            "tool_type": "sandbox_python",
                            "source": "buddhi_resolution",
                        },
                        "decision": "deny",
                        "reasons": [f"identity gate blocked tool execution: {blocked_reason}"],
                        "policy": {"identity_gate": identity_gate},
                    })
                    feedback_prompt = (
                        f"[Identity Gate Denial]\n"
                        f"Tool execution was blocked due to identity consistency status: {blocked_reason}.\n"
                        f"Revise output to restore self-consistency before proposing side effects."
                    )
                    await self.input_queue.put(feedback_prompt)
                    code = ""

            if code:
                intent = self.build_tool_intent(
                    "sandbox_python",
                    {
                        "code": code,
                        "code_length": len(code),
                        "allow_network": self.state.get("tool_execution_policy", {}).get("allow_network_access", False),
                    },
                    source="buddhi_resolution",
                )
                await self.broadcast("tool_intent_planned", {"intent": intent})

                policy_result = self.apply_tool_policy(intent)
                decision = {
                    "intent": intent,
                    "decision": "allow" if policy_result.get("allowed") else "deny",
                    "reasons": policy_result.get("reasons", []),
                    "policy": policy_result.get("policy_snapshot", {}),
                }
                await self.broadcast("tool_policy_decision", decision)
                self.append_tool_audit_entry({
                    "timestamp": datetime.datetime.now().isoformat(),
                    "intent_id": intent.get("intent_id"),
                    "tool_type": intent.get("tool_type"),
                    "decision": decision.get("decision"),
                    "reasons": decision.get("reasons", []),
                    "source": intent.get("source"),
                })

                if not policy_result.get("allowed"):
                    await self.broadcast("tool_policy_denied", decision)
                    denial_reasons = "; ".join(policy_result.get("reasons", [])) or "unspecified policy restriction"
                    feedback_prompt = (
                        f"[Karmendriya Policy Denial]\n"
                        f"Your tool intent {intent.get('intent_id')} was denied by policy.\n"
                        f"Reasons: {denial_reasons}\n"
                        f"Revise the tool approach within policy constraints."
                    )
                    await self.input_queue.put(feedback_prompt)
                else:
                    execution_result = await self.execute_tool_intent(intent)
                    execution_output = execution_result.get("output", "")
                    await self.broadcast("sandbox_log", {
                        "code": code,
                        "output": execution_output
                    })
                    self.append_tool_audit_entry({
                        "timestamp": datetime.datetime.now().isoformat(),
                        "intent_id": intent.get("intent_id"),
                        "tool_type": intent.get("tool_type"),
                        "decision": execution_result.get("status", "unknown"),
                        "source": intent.get("source"),
                    })
                    # Feed back to the agent's context for the next cycle
                    feedback_prompt = (
                        f"[Karmendriya Sandbox Execution Output]\n"
                        f"The tool intent {intent.get('intent_id')} has been executed. Here is the feedback from the runtime environment:\n"
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

            self.append_cycle_blackboard(cycle_blackboard)
            
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
