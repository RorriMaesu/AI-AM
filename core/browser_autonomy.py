import datetime
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import uuid
import ctypes
from typing import Any, Dict, List, Optional


class BrowserAutonomyController:
    """Lightweight desktop browser controller for Windows-first autonomy loops."""

    def __init__(self, frame_dir: str = "workspace/browser_frames", max_frames: int = 12):
        self.frame_dir = frame_dir
        self.max_frames = max(3, max_frames)
        self.session_state: Dict[str, Any] = {
            "active": False,
            "paused": False,
            "session_id": None,
            "goal": "",
            "current_url": "",
            "last_action": None,
            "last_frame": None,
            "last_error": "",
            "updated_at": None,
        }
        self.action_log: List[Dict[str, Any]] = []
        self.frame_ring: List[Dict[str, Any]] = []

        os.makedirs(self.frame_dir, exist_ok=True)

        # Ensure system coordinates match physical screen coordinates on High-DPI Windows displays
        if sys.platform == "win32":
            try:
                # 2 = PROCESS_PER_MONITOR_DPI_AWARE
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                try:
                    ctypes.windll.user32.SetProcessDPIAware()
                except Exception:
                    pass

    def probe_capabilities(self) -> Dict[str, Any]:
        has_image_capture = self._image_grab_available()
        edge_path = self._detect_edge_path()
        input_driver = "pyautogui" if self._pyautogui_available() else ("win32_ctypes" if sys.platform == "win32" else "none")

        return {
            "desktop_automation_available": sys.platform == "win32",
            "screenshot_capture_available": has_image_capture,
            "desktop_input_available": self._desktop_input_available(),
            "screen_region_validation_available": has_image_capture,
            "edge_detected": bool(edge_path),
            "edge_path": edge_path,
            "max_frames": self.max_frames,
            "input_driver": input_driver,
            "mode": "desktop-gui",
        }

    def get_state(self) -> Dict[str, Any]:
        return {
            "session": dict(self.session_state),
            "recent_actions": self.action_log[-25:],
            "recent_frames": list(self.frame_ring),
        }

    def start_session(self, goal: str, start_url: str = "", allowed_actions: Optional[List[str]] = None, blocked_patterns: Optional[List[str]] = None) -> Dict[str, Any]:
        if self.session_state["active"]:
            return {
                "status": "already_active",
                "session": dict(self.session_state),
            }

        self.session_state.update({
            "active": True,
            "paused": False,
            "session_id": f"browser_{uuid.uuid4().hex[:10]}",
            "goal": goal or "autonomous curiosity exploration",
            "current_url": "",
            "last_action": None,
            "last_error": "",
            "updated_at": datetime.datetime.now().isoformat(),
        })

        result = {
            "status": "started",
            "session": dict(self.session_state),
        }

        if start_url:
            open_result = self.execute_action(
                {
                    "type": "open_url",
                    "url": start_url,
                    "reason": "session bootstrap",
                },
                allowed_actions=allowed_actions,
                blocked_patterns=blocked_patterns,
            )
            result["bootstrap_action"] = open_result

        return result

    def stop_session(self, reason: str = "operator_stop") -> Dict[str, Any]:
        if not self.session_state["active"]:
            return {
                "status": "already_stopped",
                "session": dict(self.session_state),
            }

        self.session_state["active"] = False
        self.session_state["paused"] = False
        self.session_state["last_action"] = {
            "type": "stop",
            "reason": reason,
            "timestamp": datetime.datetime.now().isoformat(),
        }
        self.session_state["updated_at"] = datetime.datetime.now().isoformat()

        return {
            "status": "stopped",
            "session": dict(self.session_state),
        }

    def pause_session(self) -> Dict[str, Any]:
        if not self.session_state["active"]:
            return {"status": "not_active", "session": dict(self.session_state)}

        self.session_state["paused"] = True
        self.session_state["updated_at"] = datetime.datetime.now().isoformat()
        return {"status": "paused", "session": dict(self.session_state)}

    def resume_session(self) -> Dict[str, Any]:
        if not self.session_state["active"]:
            return {"status": "not_active", "session": dict(self.session_state)}

        self.session_state["paused"] = False
        self.session_state["updated_at"] = datetime.datetime.now().isoformat()
        return {"status": "resumed", "session": dict(self.session_state)}

    def execute_action(self, action: Dict[str, Any], allowed_actions: Optional[List[str]] = None, blocked_patterns: Optional[List[str]] = None, min_confidence: float = 0.55) -> Dict[str, Any]:
        action_type = (action or {}).get("type", "").strip().lower()
        allowed = set(allowed_actions or [
            "open_url",
            "wait",
            "capture_frame",
            "stop",
            "click",
            "type",
            "scroll",
            "back",
            "keypress",
            "click_target",
            "type_target",
            "scroll_target",
        ])
        blocked = blocked_patterns or [
            "login",
            "signin",
            "checkout",
            "payment",
            "upload",
            "wallet",
            "account",
            "settings",
        ]

        timestamp = datetime.datetime.now().isoformat()

        if not self.session_state["active"] and action_type != "open_url":
            return {
                "status": "denied",
                "reason": "browser session is not active",
                "action": action,
                "timestamp": timestamp,
            }

        if self.session_state["paused"] and action_type not in {"capture_frame", "stop"}:
            return {
                "status": "denied",
                "reason": "browser session is paused",
                "action": action,
                "timestamp": timestamp,
            }

        if action_type not in allowed:
            return {
                "status": "denied",
                "reason": f"action '{action_type}' is not allowed in constrained mode",
                "action": action,
                "timestamp": timestamp,
            }

        confidence = float(action.get("confidence", 1.0))
        if action_type in {"click", "type", "scroll", "back", "keypress", "click_target", "type_target", "scroll_target"} and confidence < float(min_confidence):
            return {
                "status": "blocked",
                "reason": f"action confidence {confidence:.2f} below threshold {float(min_confidence):.2f}",
                "action": action,
                "timestamp": timestamp,
            }

        if action_type in {"click", "type", "scroll", "back", "keypress", "click_target", "type_target", "scroll_target"} and not self._desktop_input_available():
            return {
                "status": "denied",
                "reason": "desktop input driver unavailable for control primitive",
                "action": action,
                "timestamp": timestamp,
            }

        result: Dict[str, Any] = {
            "status": "ok",
            "action": action,
            "timestamp": timestamp,
            "frame": None,
        }
        dry_run = bool(action.get("dry_run", False))
        target_resolution = None

        try:
            if action_type == "open_url":
                url = str(action.get("url", "")).strip()
                normalized = self._normalize_url(url)
                lower_url = normalized.lower()
                for pattern in blocked:
                    if pattern in lower_url:
                        return {
                            "status": "blocked",
                            "reason": f"blocked pattern '{pattern}' detected in URL",
                            "action": action,
                            "timestamp": timestamp,
                        }

                self._open_url_in_edge(normalized)
                self.session_state["current_url"] = normalized

            elif action_type == "wait":
                wait_ms = int(action.get("ms", 1500))
                time.sleep(max(100, min(wait_ms, 8000)) / 1000.0)

            elif action_type == "capture_frame":
                pass

            elif action_type == "click":
                x_raw = action.get("x")
                y_raw = action.get("y")
                if x_raw is None or y_raw is None:
                    target_resolution = self._resolve_target_point(action)
                    if not target_resolution["ok"]:
                        return {
                            "status": "blocked",
                            "reason": target_resolution["reason"],
                            "action": action,
                            "timestamp": timestamp,
                        }
                    x = int(target_resolution["x"])
                    y = int(target_resolution["y"])
                else:
                    x = int(x_raw)
                    y = int(y_raw)
                button = str(action.get("button", "left")).lower()
                clicks = max(1, int(action.get("clicks", 1)))
                if x < 0 or y < 0:
                    return {
                        "status": "denied",
                        "reason": "click action requires valid screen coordinates x,y",
                        "action": action,
                        "timestamp": timestamp,
                    }
                if not dry_run:
                    for _ in range(clicks):
                        self._click_at(x, y, button)
                        time.sleep(0.08)

            elif action_type == "click_target":
                target_resolution = self._resolve_target_point(action)
                if not target_resolution["ok"]:
                    return {
                        "status": "blocked",
                        "reason": target_resolution["reason"],
                        "action": action,
                        "timestamp": timestamp,
                    }
                x = int(target_resolution["x"])
                y = int(target_resolution["y"])
                button = str(action.get("button", "left")).lower()
                clicks = max(1, int(action.get("clicks", 1)))
                if not dry_run:
                    for _ in range(clicks):
                        self._click_at(x, y, button)
                        time.sleep(0.08)

            elif action_type == "type":
                text = str(action.get("text", ""))
                if not text:
                    return {
                        "status": "denied",
                        "reason": "type action requires non-empty text",
                        "action": action,
                        "timestamp": timestamp,
                    }

                lowered = text.lower()
                for pattern in blocked:
                    if pattern in lowered:
                        return {
                            "status": "blocked",
                            "reason": f"blocked pattern '{pattern}' detected in text payload",
                            "action": action,
                            "timestamp": timestamp,
                        }

                if not dry_run:
                    self._type_text(text)
                    if bool(action.get("press_enter", False)):
                        self._press_key("enter")

            elif action_type == "type_target":
                text = str(action.get("text", ""))
                if not text:
                    return {
                        "status": "denied",
                        "reason": "type_target action requires non-empty text",
                        "action": action,
                        "timestamp": timestamp,
                    }

                lowered = text.lower()
                for pattern in blocked:
                    if pattern in lowered:
                        return {
                            "status": "blocked",
                            "reason": f"blocked pattern '{pattern}' detected in text payload",
                            "action": action,
                            "timestamp": timestamp,
                        }

                target_resolution = self._resolve_target_point(action)
                if not target_resolution["ok"]:
                    return {
                        "status": "blocked",
                        "reason": target_resolution["reason"],
                        "action": action,
                        "timestamp": timestamp,
                    }
                x = int(target_resolution["x"])
                y = int(target_resolution["y"])
                if not dry_run:
                    self._click_at(x, y, "left")
                    time.sleep(0.1)
                    self._type_text(text)
                    if bool(action.get("press_enter", False)):
                        self._press_key("enter")

            elif action_type == "scroll":
                amount = int(action.get("amount", -400))
                if not dry_run:
                    self._scroll(amount)

            elif action_type == "scroll_target":
                target_resolution = self._resolve_target_point(action)
                if not target_resolution["ok"]:
                    return {
                        "status": "blocked",
                        "reason": target_resolution["reason"],
                        "action": action,
                        "timestamp": timestamp,
                    }
                x = int(target_resolution["x"])
                y = int(target_resolution["y"])
                amount = int(action.get("amount", -400))
                if not dry_run:
                    self._click_at(x, y, "left")
                    time.sleep(0.08)
                    self._scroll(amount)

            elif action_type == "back":
                if not dry_run:
                    self._browser_back()

            elif action_type == "keypress":
                key = str(action.get("key", "")).strip().lower()
                if not key:
                    return {
                        "status": "denied",
                        "reason": "keypress action requires key",
                        "action": action,
                        "timestamp": timestamp,
                    }
                if not dry_run:
                    self._press_key(key)

            elif action_type == "stop":
                return self.stop_session(reason=str(action.get("reason", "agent_stop")))

            frame_meta = self.capture_frame()
            result["frame"] = frame_meta
            self.session_state["last_frame"] = frame_meta
            self.session_state["last_action"] = {
                "type": action_type,
                "timestamp": timestamp,
                "reason": action.get("reason", ""),
            }
            if target_resolution:
                result["target_resolution"] = target_resolution
            self.session_state["updated_at"] = datetime.datetime.now().isoformat()
            self._append_action_log(result)
            return result

        except Exception as exc:
            err = f"browser action failure: {exc}"
            self.session_state["last_error"] = err
            self.session_state["updated_at"] = datetime.datetime.now().isoformat()
            return {
                "status": "error",
                "reason": err,
                "action": action,
                "timestamp": timestamp,
            }

    def make_duckduckgo_url(self, query: str) -> str:
        encoded = urllib.parse.quote_plus(query.strip())
        return f"https://duckduckgo.com/?q={encoded}"

    def build_target_suggestions(
        self,
        limit: int = 8,
        min_signal_stddev: float = 10.0,
        recent_only: bool = False,
        frame_path: str = "",
        strict_frame_match: bool = False,
    ) -> Dict[str, Any]:
        """Build anchor rectangle suggestions from recent frames for target-based actions."""
        limit = max(1, min(int(limit), 30))
        min_signal_stddev = float(max(1.0, min_signal_stddev))

        frames = list(self.frame_ring)
        selected_frames: List[Dict[str, Any]] = []
        normalized_requested_path = frame_path.replace("\\", "/").strip() if frame_path else ""

        if normalized_requested_path:
            selected_frames = [f for f in frames if str(f.get("path", "")).replace("\\", "/") == normalized_requested_path]
            if not selected_frames and os.path.exists(normalized_requested_path):
                selected_frames = [{"path": normalized_requested_path}]
            if strict_frame_match and not selected_frames:
                return {
                    "error": {
                        "code": "FRAME_NOT_FOUND",
                        "message": f"Requested frame_path '{normalized_requested_path}' was not found in recent frames and does not exist on disk.",
                    },
                    "suggestions": [],
                    "inspected_frames": 0,
                    "available_frames": len(frames),
                    "selected_frames": 0,
                    "recent_only": bool(recent_only),
                    "frame_path": normalized_requested_path,
                    "strict_frame_match": bool(strict_frame_match),
                    "session_id": self.session_state.get("session_id"),
                }
        elif recent_only and frames:
            selected_frames = [frames[-1]]
        else:
            selected_frames = frames

        suggestions: List[Dict[str, Any]] = []
        inspected = 0

        for frame in reversed(selected_frames):
            if len(suggestions) >= limit:
                break

            frame_path = str(frame.get("path", ""))
            if not frame_path:
                continue

            frame_suggestions = self._suggest_regions_from_frame(
                frame_path,
                per_frame_limit=max(2, min(6, limit)),
                min_signal_stddev=min_signal_stddev,
            )
            inspected += 1
            suggestions.extend(frame_suggestions)

        suggestions.sort(key=lambda s: s.get("signal_stddev", 0.0), reverse=True)
        suggestions = suggestions[:limit]

        return {
            "suggestions": suggestions,
            "inspected_frames": inspected,
            "available_frames": len(frames),
            "selected_frames": len(selected_frames),
            "recent_only": bool(recent_only),
            "frame_path": normalized_requested_path,
            "strict_frame_match": bool(strict_frame_match),
            "session_id": self.session_state.get("session_id"),
        }

    def capture_frame(self) -> Optional[Dict[str, Any]]:
        if not self._image_grab_available():
            return None

        from PIL import ImageGrab  # Imported lazily to avoid hard dependency at import time.

        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        file_name = f"frame_{ts}.png"
        file_path = os.path.join(self.frame_dir, file_name)

        image = ImageGrab.grab(all_screens=True)
        image.save(file_path)

        rel_path = file_path.replace("\\", "/")
        frame_meta = {
            "path": rel_path,
            "captured_at": datetime.datetime.now().isoformat(),
            "session_id": self.session_state.get("session_id"),
        }

        self.frame_ring.append(frame_meta)
        if len(self.frame_ring) > self.max_frames:
            self.frame_ring = self.frame_ring[-self.max_frames:]

        return frame_meta

    def _append_action_log(self, entry: Dict[str, Any]):
        self.action_log.append(entry)
        if len(self.action_log) > 200:
            self.action_log = self.action_log[-200:]

    def _suggest_regions_from_frame(self, frame_path: str, per_frame_limit: int = 4, min_signal_stddev: float = 10.0) -> List[Dict[str, Any]]:
        if not self._image_grab_available():
            return []

        try:
            from PIL import Image, ImageStat

            with Image.open(frame_path) as img:
                gray = img.convert("L")
                width, height = gray.size

                # Coarse scan windows strike a balance between precision and performance.
                tile_w = max(100, width // 5)
                tile_h = max(80, height // 5)
                step_x = max(60, tile_w // 2)
                step_y = max(50, tile_h // 2)

                candidates: List[Dict[str, Any]] = []
                y = 0
                while y + tile_h <= height:
                    x = 0
                    while x + tile_w <= width:
                        box = (x, y, x + tile_w, y + tile_h)
                        crop = gray.crop(box)
                        stddev = float(ImageStat.Stat(crop).stddev[0])
                        if stddev >= min_signal_stddev:
                            candidates.append({
                                "x": x,
                                "y": y,
                                "width": tile_w,
                                "height": tile_h,
                                "signal_stddev": stddev,
                            })
                        x += step_x
                    y += step_y

                candidates.sort(key=lambda c: c["signal_stddev"], reverse=True)
                output: List[Dict[str, Any]] = []
                for cand in candidates[:per_frame_limit]:
                    output.append({
                        "frame_path": frame_path,
                        "signal_stddev": round(cand["signal_stddev"], 2),
                        "target": {
                            "anchor": {
                                "x": cand["x"],
                                "y": cand["y"],
                                "width": cand["width"],
                                "height": cand["height"],
                            },
                            "anchor_ratio": {"x": 0.5, "y": 0.5},
                            "offset": {"dx": 0, "dy": 0},
                            "validation_region": {
                                "x": cand["x"],
                                "y": cand["y"],
                                "width": cand["width"],
                                "height": cand["height"],
                            },
                            "validation": {"min_stddev": max(4.0, min_signal_stddev * 0.6)},
                        },
                    })
                return output
        except Exception:
            return []

    def _normalize_url(self, url: str) -> str:
        if not url:
            return "https://duckduckgo.com"
        if not url.startswith("http://") and not url.startswith("https://"):
            return f"https://{url}"
        return url

    def _open_url_in_edge(self, url: str):
        edge_path = self._detect_edge_path()
        
        hwnd_prev = None
        startupinfo = None
        if sys.platform == "win32":
            try:
                hwnd_prev = ctypes.windll.user32.GetForegroundWindow()
            except Exception:
                pass
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            # 4 is SW_SHOWNOACTIVATE
            startupinfo.wShowWindow = 4

        if edge_path:
            subprocess.Popen([edge_path, url], startupinfo=startupinfo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform == "win32":
            os.startfile(url)  # type: ignore[attr-defined]
        else:
            raise RuntimeError("edge browser path not available on this platform")

        if sys.platform == "win32" and hwnd_prev:
            import threading
            def restore_focus():
                for _ in range(5):
                    time.sleep(0.2)
                    current_hwnd = ctypes.windll.user32.GetForegroundWindow()
                    if current_hwnd != hwnd_prev:
                        try:
                            ctypes.windll.user32.SetForegroundWindow(hwnd_prev)
                        except Exception:
                            pass
            threading.Thread(target=restore_focus, daemon=True).start()

    def _resolve_target_point(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Resolve an actionable point from anchor-like target metadata and validate region hints."""
        target = action.get("target") if isinstance(action.get("target"), dict) else {}
        anchor = target.get("anchor") if isinstance(target.get("anchor"), dict) else {}
        region = target.get("region") if isinstance(target.get("region"), dict) else {}
        validation_region = target.get("validation_region") if isinstance(target.get("validation_region"), dict) else {}
        offset = target.get("offset") if isinstance(target.get("offset"), dict) else {}
        anchor_ratio = target.get("anchor_ratio") if isinstance(target.get("anchor_ratio"), dict) else {}
        validation = target.get("validation") if isinstance(target.get("validation"), dict) else {}

        base = anchor if anchor else region
        if not base:
            return {"ok": False, "reason": "target action requires target.anchor or target.region"}

        norm = self._normalize_region(base)
        if not norm:
            return {"ok": False, "reason": "invalid target anchor/region geometry"}

        screen = self._get_screen_size()
        if not self._region_in_bounds(norm, screen["width"], screen["height"]):
            return {"ok": False, "reason": "target anchor/region is out of screen bounds"}

        ratio_x = float(anchor_ratio.get("x", 0.5))
        ratio_y = float(anchor_ratio.get("y", 0.5))
        ratio_x = max(0.0, min(1.0, ratio_x))
        ratio_y = max(0.0, min(1.0, ratio_y))

        dx = int(offset.get("dx", 0))
        dy = int(offset.get("dy", 0))

        x = int(norm["x"] + (norm["width"] * ratio_x) + dx)
        y = int(norm["y"] + (norm["height"] * ratio_y) + dy)

        if x < 0 or y < 0 or x >= screen["width"] or y >= screen["height"]:
            return {"ok": False, "reason": "resolved target point falls outside screen"}

        if validation_region:
            val_region = self._normalize_region(validation_region)
            if not val_region:
                return {"ok": False, "reason": "invalid target.validation_region geometry"}
            if not self._region_in_bounds(val_region, screen["width"], screen["height"]):
                return {"ok": False, "reason": "validation region is out of screen bounds"}

            min_stddev = float(validation.get("min_stddev", 4.5))
            ok, observed_stddev = self._validate_region_visual_signal(val_region, min_stddev=min_stddev)
            if not ok:
                return {
                    "ok": False,
                    "reason": f"validation region signal too weak (stddev {observed_stddev:.2f} < {min_stddev:.2f})",
                }

        return {
            "ok": True,
            "x": x,
            "y": y,
            "anchor": norm,
        }

    def _normalize_region(self, region: Dict[str, Any]) -> Optional[Dict[str, int]]:
        try:
            x = int(region.get("x", -1))
            y = int(region.get("y", -1))
            width = int(region.get("width", -1))
            height = int(region.get("height", -1))
            if x < 0 or y < 0 or width <= 0 or height <= 0:
                return None
            return {"x": x, "y": y, "width": width, "height": height}
        except Exception:
            return None

    def _region_in_bounds(self, region: Dict[str, int], screen_width: int, screen_height: int) -> bool:
        return (
            region["x"] >= 0
            and region["y"] >= 0
            and (region["x"] + region["width"]) <= screen_width
            and (region["y"] + region["height"]) <= screen_height
        )

    def _validate_region_visual_signal(self, region: Dict[str, int], min_stddev: float = 4.5) -> (bool, float):
        if not self._image_grab_available():
            return False, 0.0

        try:
            from PIL import ImageGrab, ImageStat

            bbox = (
                region["x"],
                region["y"],
                region["x"] + region["width"],
                region["y"] + region["height"],
            )
            image = ImageGrab.grab(bbox=bbox).convert("L")
            stddev = float(ImageStat.Stat(image).stddev[0])
            return stddev >= min_stddev, stddev
        except Exception:
            return False, 0.0

    def _get_screen_size(self) -> Dict[str, int]:
        if self._pyautogui_available():
            import pyautogui

            size = pyautogui.size()
            return {"width": int(size.width), "height": int(size.height)}

        if sys.platform == "win32":
            user32 = ctypes.windll.user32
            return {
                "width": int(user32.GetSystemMetrics(0)),
                "height": int(user32.GetSystemMetrics(1)),
            }

        return {"width": 1920, "height": 1080}

    def _desktop_input_available(self) -> bool:
        return self._pyautogui_available() or sys.platform == "win32"

    def _pyautogui_available(self) -> bool:
        try:
            import pyautogui  # noqa: F401
            return True
        except Exception:
            return False

    def _click_at(self, x: int, y: int, button: str):
        if self._pyautogui_available():
            import pyautogui
            pyautogui.moveTo(x, y, duration=0.05)
            pyautogui.click(x=x, y=y, button=button if button in {"left", "right", "middle"} else "left")
            return

        if sys.platform != "win32":
            raise RuntimeError("click primitive requires Windows or pyautogui")

        user32 = ctypes.windll.user32
        user32.SetCursorPos(x, y)
        if button == "right":
            down, up = 0x0008, 0x0010
        elif button == "middle":
            down, up = 0x0020, 0x0040
        else:
            down, up = 0x0002, 0x0004
        user32.mouse_event(down, 0, 0, 0, 0)
        user32.mouse_event(up, 0, 0, 0, 0)

    def _scroll(self, amount: int):
        if self._pyautogui_available():
            import pyautogui
            pyautogui.scroll(amount)
            return

        if sys.platform != "win32":
            raise RuntimeError("scroll primitive requires Windows or pyautogui")

        user32 = ctypes.windll.user32
        user32.mouse_event(0x0800, 0, 0, amount, 0)

    def _press_key(self, key: str):
        if self._pyautogui_available():
            import pyautogui
            pyautogui.press(key)
            return

        if sys.platform != "win32":
            raise RuntimeError("keypress primitive requires Windows or pyautogui")

        vk_map = {
            "enter": 0x0D,
            "tab": 0x09,
            "esc": 0x1B,
            "escape": 0x1B,
            "backspace": 0x08,
            "left": 0x25,
            "up": 0x26,
            "right": 0x27,
            "down": 0x28,
            "space": 0x20,
        }
        vk = vk_map.get(key)
        if vk is None:
            raise RuntimeError(f"unsupported key '{key}' for win32 fallback")

        user32 = ctypes.windll.user32
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, 0x0002, 0)

    def _type_text(self, text: str):
        if self._pyautogui_available():
            import pyautogui
            pyautogui.write(text, interval=0.01)
            return

        if sys.platform != "win32":
            raise RuntimeError("type primitive requires Windows or pyautogui")

        for char in text:
            self._send_unicode_char(char)
            time.sleep(0.005)

    def _send_unicode_char(self, char: str):
        user32 = ctypes.windll.user32
        vk_packet = 0xE7
        scan_code = ord(char)
        user32.keybd_event(vk_packet, scan_code, 0x0004, 0)
        user32.keybd_event(vk_packet, scan_code, 0x0004 | 0x0002, 0)

    def _browser_back(self):
        if self._pyautogui_available():
            import pyautogui
            pyautogui.hotkey("alt", "left")
            return

        if sys.platform != "win32":
            raise RuntimeError("back primitive requires Windows or pyautogui")

        user32 = ctypes.windll.user32
        vk_alt = 0x12
        vk_left = 0x25
        user32.keybd_event(vk_alt, 0, 0, 0)
        user32.keybd_event(vk_left, 0, 0, 0)
        user32.keybd_event(vk_left, 0, 0x0002, 0)
        user32.keybd_event(vk_alt, 0, 0x0002, 0)

    def _detect_edge_path(self) -> Optional[str]:
        candidates = [
            shutil.which("msedge"),
            shutil.which("microsoft-edge"),
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]

        for candidate in candidates:
            if candidate and os.path.exists(candidate):
                return candidate
        return None

    def _image_grab_available(self) -> bool:
        try:
            from PIL import ImageGrab  # noqa: F401
            return True
        except Exception:
            return False
