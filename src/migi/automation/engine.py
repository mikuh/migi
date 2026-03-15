from __future__ import annotations

import ast
import base64
import contextlib
import importlib
import io
import os
import platform
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


COORDINATE_SCALE = 1000
SCREENSHOT_FORMAT = "jpeg"

DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_PERFORMANCE_PROFILE = "balanced"
DEFAULT_CAPTURE_MODE = "auto"

MAX_PIXELS = 16384 * 28 * 28
MIN_PIXELS = 100 * 28 * 28
PIXELS_PER_SCROLL_CLICK = 15

_mss_instance: Any | None = None
_httpx_clients: dict[float, Any] = {}
_last_window_region_macos: CaptureRegion | None = None


@dataclass(frozen=True)
class PerformanceTuning:
    name: str
    screenshot_quality: int
    screenshot_max_long_edge: int
    request_timeout_seconds: float
    computer_use_max_tokens: int | None
    image_max_tokens: int | None
    action_inter_step_delay: float
    action_clipboard_sync_delay: float
    action_paste_settle_delay: float
    action_pre_exec_delay: float
    action_hotkey_interval: float
    action_wait_duration: float


@dataclass(frozen=True)
class CaptureRegion:
    left: int
    top: int
    width: int
    height: int
    source: str


@dataclass(frozen=True)
class WeChatSendRequest:
    recipient: str
    content: str


PERFORMANCE_PROFILES: dict[str, PerformanceTuning] = {
    "fast": PerformanceTuning(
        name="fast",
        screenshot_quality=60,
        screenshot_max_long_edge=1366,
        request_timeout_seconds=60.0,
        computer_use_max_tokens=192,
        image_max_tokens=384,
        action_inter_step_delay=0.04,
        action_clipboard_sync_delay=0.03,
        action_paste_settle_delay=0.06,
        action_pre_exec_delay=0.04,
        action_hotkey_interval=0.03,
        action_wait_duration=1.0,
    ),
    "balanced": PerformanceTuning(
        name="balanced",
        screenshot_quality=70,
        screenshot_max_long_edge=1600,
        request_timeout_seconds=90.0,
        computer_use_max_tokens=256,
        image_max_tokens=512,
        action_inter_step_delay=0.08,
        action_clipboard_sync_delay=0.05,
        action_paste_settle_delay=0.12,
        action_pre_exec_delay=0.08,
        action_hotkey_interval=0.05,
        action_wait_duration=2.0,
    ),
    "accurate": PerformanceTuning(
        name="accurate",
        screenshot_quality=80,
        screenshot_max_long_edge=1920,
        request_timeout_seconds=120.0,
        computer_use_max_tokens=384,
        image_max_tokens=768,
        action_inter_step_delay=0.08,
        action_clipboard_sync_delay=0.05,
        action_paste_settle_delay=0.12,
        action_pre_exec_delay=0.08,
        action_hotkey_interval=0.05,
        action_wait_duration=2.0,
    ),
}

CAPTURE_MODE_CHOICES = ("auto", "screen", "window")
KNOWN_APP_ALIASES: dict[str, list[str]] = {
    "WeChat": ["wechat", "微信"],
}


class DependencyError(RuntimeError):
    pass


def _import_gui_dependencies() -> dict[str, Any]:
    try:
        import httpx
        import mss
        import pyautogui
        import pyperclip
        from PIL import Image
    except ModuleNotFoundError as exc:
        raise DependencyError(
            "Missing optional dependency for GUI automation. "
            "Install: httpx mss pyautogui pyperclip pillow"
        ) from exc
    return {
        "httpx": httpx,
        "mss": mss,
        "pyautogui": pyautogui,
        "pyperclip": pyperclip,
        "Image": Image,
    }


def _import_vision_dependencies() -> dict[str, Any]:
    try:
        import httpx
        from PIL import Image
    except ModuleNotFoundError as exc:
        raise DependencyError(
            "Missing optional dependency for image understanding. "
            "Install: httpx pillow"
        ) from exc
    return {
        "httpx": httpx,
        "Image": Image,
    }


COMPUTER_USE_PROMPT = """You are a GUI agent. You are given a task and your action history, with screenshots. You need to perform actions to complete the task.

## Output Format (STRICT - Use XML tags)
Wrap EACH action in <action></action> tags. Output actions only, NO explanations.

Single action:
<action>click(point='<point>512 384</point>')</action>

Multiple actions (same screen, no UI change between):
<action>click(point='<point>512 384</point>')</action>
<action>type(content='hello')</action>
<action>hotkey(key='enter')</action>

IMPORTANT: Only use multiple <action> tags when they can execute on CURRENT screen without waiting.

## Coordinate Rules (IMPORTANT)
- Always output point coordinates in normalized 0-1000 space.
- 0 0 is the top-left of the current screenshot.
- 1000 1000 is the bottom-right of the current screenshot.
- Prefer integers.

## Safety and Fallback Rules (STRICT)
- Before any click, verify the target is clearly visible in CURRENT screenshot.
- Never click guessed coordinates.
- When the task is to search/filter inside the current app, prefer the app's standard search shortcut first (macOS: command f, Windows/Linux: ctrl f) before clicking small search fields.
- For messaging tasks, do not type or send until the CURRENT screenshot clearly shows the exact recipient chat is selected.
- If a recipient name is provided, search and select that recipient first whenever the CURRENT screenshot does not clearly confirm the chat header or selected conversation matches the recipient.
- Do not use window-management shortcuts like close tab/window or quit app unless the user explicitly asked to close, quit, exit, or dismiss something.
- Runtime may have already attempted command-based launch (e.g., macOS `open`, Windows `Start-Process`) before this step.
- If user asks to open/launch an app and its icon is not visible, use app search instead of random clicking.
- Never rely on shell checks like `which`, `where`, or `Get-Command` for GUI app availability.
- Current OS: {os_name}; search entry: {search_entry}; preferred app-search hotkey: {search_hotkey}.
- Shortcut-first policy for app launch (macOS/Windows): try app-search hotkey first; if hotkey fails/unavailable, fallback to GUI-visible search controls.
- App-search sequence (STRICT):
  1) <action>hotkey(key='{search_hotkey}')</action>
  2) <action>type(content='AppName')</action>
  3) Confirm an application result is visible (e.g. in "Applications/应用程序") and target exactly matches app name.
  4) If the exact application result is already highlighted or first in system search, prefer <action>hotkey(key='enter')</action>.
  5) Otherwise open it by clicking the exact app result or navigating to it, then Enter if needed.
  6) If top result is not an application (e.g. message/contact/document), do NOT press Enter on it.
- If the target is still uncertain, use wait() or finished(content='target not visible') instead of clicking.
- Action history may include prior attempts. If a prior click/type did not visibly change the UI in the CURRENT screenshot, do not assume it succeeded.
- Only use finished(content='...') when the CURRENT screenshot visibly confirms the task is complete.

## Action Space
click(point='<point>x1 y1</point>')
left_double(point='<point>x1 y1</point>')
right_single(point='<point>x1 y1</point>')
drag(start_point='<point>x1 y1</point>', end_point='<point>x2 y2</point>')
hotkey(key='ctrl c')
type(content='xxx')
scroll(point='<point>x1 y1</point>', direction='down or up or right or left')
wait()
finished(content='xxx')

## User Instruction
{instruction}
"""


IMAGE_UNDERSTANDING_PROMPT = """You are a vision assistant.
Analyze the provided image and answer the user's instruction.

Rules:
- Focus on directly observable details.
- If information is uncertain, state uncertainty explicitly.
- If text is visible, transcribe key text faithfully.
- Reply in the same language as the instruction when possible.
"""


@dataclass
class AutomationResult:
    success: bool
    image_size: tuple[int, int] | None
    response: str | None
    execution_result: list[str] | None
    action_triggered: bool
    action_type: str | None
    timing: dict[str, float]
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "image_size": self.image_size,
            "response": self.response,
            "execution_result": self.execution_result,
            "action_triggered": self.action_triggered,
            "action_type": self.action_type,
            "timing": self.timing,
            "error": self.error,
        }


def safe_literal_eval(value: str) -> Any:
    return ast.literal_eval(value)


def resolve_performance_tuning(profile: str | None = None) -> PerformanceTuning:
    profile_name = (profile or DEFAULT_PERFORMANCE_PROFILE).strip().lower()
    tuning = PERFORMANCE_PROFILES.get(profile_name)
    if tuning is None:
        supported = ", ".join(sorted(PERFORMANCE_PROFILES))
        raise ValueError(f"Unsupported performance profile: {profile_name}. Use one of: {supported}.")
    return tuning


def resolve_capture_mode(capture_mode: str | None = None, app_launch_intent: bool = False) -> str:
    mode = (capture_mode or DEFAULT_CAPTURE_MODE).strip().lower()
    if mode not in CAPTURE_MODE_CHOICES:
        supported = ", ".join(CAPTURE_MODE_CHOICES)
        raise ValueError(f"Unsupported capture mode: {mode}. Use one of: {supported}.")
    if mode == "auto":
        return "screen" if app_launch_intent else "window"
    return mode


def _downscale_if_needed(image: Any, max_long_edge: int) -> Any:
    w, h = image.size
    long_edge = max(w, h)
    if long_edge <= max_long_edge:
        return image
    scale = max_long_edge / long_edge
    new_w = round(w * scale)
    new_h = round(h * scale)
    try:
        from PIL import Image as _pil
        resample = _pil.LANCZOS
    except (ImportError, AttributeError):
        resample = 1
    return image.resize((new_w, new_h), resample)


def _encode_image_from_pil(image: Any, fmt: str = "jpeg", quality: int = 80) -> str:
    buffer = io.BytesIO()
    if fmt.lower() == "jpeg":
        if image.mode not in {"RGB", "L"}:
            image = image.convert("RGB")
        image.save(buffer, format="JPEG", quality=quality)
    else:
        image.save(buffer, format=fmt.upper())
    buffer.seek(0)
    return base64.b64encode(buffer.read()).decode("utf-8")


def _get_mss_instance(mss_module: Any) -> Any:
    global _mss_instance
    if _mss_instance is None:
        _mss_instance = mss_module.mss()
    return _mss_instance


def _primary_monitor_region(sct: Any) -> CaptureRegion:
    monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
    return CaptureRegion(
        left=int(monitor.get("left", 0)),
        top=int(monitor.get("top", 0)),
        width=int(monitor["width"]),
        height=int(monitor["height"]),
        source="screen",
    )


def _front_window_region_macos(timeout_seconds: float = 0.8) -> CaptureRegion | None:
    global _last_window_region_macos
    script = """
tell application "System Events"
    set frontApp to first application process whose frontmost is true
    tell frontApp
        if (count of windows) is 0 then
            return ""
        end if
        set frontWindow to front window
        set {xPos, yPos} to position of frontWindow
        set {winWidth, winHeight} to size of frontWindow
        return (xPos as text) & tab & (yPos as text) & tab & (winWidth as text) & tab & (winHeight as text)
    end tell
end tell
"""
    try:
        completed = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except Exception:  # noqa: BLE001
        return _last_window_region_macos
    if completed.returncode != 0:
        return _last_window_region_macos
    raw = completed.stdout.strip()
    if not raw:
        return _last_window_region_macos
    parts = raw.split("\t")
    if len(parts) != 4:
        return _last_window_region_macos
    try:
        left, top, width, height = (int(part) for part in parts)
    except ValueError:
        return _last_window_region_macos
    if width <= 0 or height <= 0:
        return _last_window_region_macos
    region = CaptureRegion(left=left, top=top, width=width, height=height, source="window")
    _last_window_region_macos = region
    return region


def _clamp_region_to_monitor(region: CaptureRegion, monitor: CaptureRegion) -> CaptureRegion | None:
    left = max(region.left, monitor.left)
    top = max(region.top, monitor.top)
    right = min(region.left + region.width, monitor.left + monitor.width)
    bottom = min(region.top + region.height, monitor.top + monitor.height)
    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0:
        return None
    return CaptureRegion(left=left, top=top, width=width, height=height, source=region.source)


def capture_screenshot(
    deps: dict[str, Any],
    quality: int,
    fmt: str = SCREENSHOT_FORMAT,
    max_long_edge: int = PERFORMANCE_PROFILES[DEFAULT_PERFORMANCE_PROFILE].screenshot_max_long_edge,
    capture_mode: str = DEFAULT_CAPTURE_MODE,
    app_launch_intent: bool = False,
) -> tuple[Any, int, int, str, CaptureRegion]:
    sct = _get_mss_instance(deps["mss"])
    monitor_region = _primary_monitor_region(sct)
    effective_mode = resolve_capture_mode(capture_mode, app_launch_intent=app_launch_intent)
    capture_region = monitor_region
    if effective_mode == "window" and platform.system() == "Darwin":
        window_region = _front_window_region_macos()
        clamped_region = (
            _clamp_region_to_monitor(window_region, monitor_region)
            if window_region is not None
            else None
        )
        if clamped_region is not None:
            capture_region = clamped_region

    sct_img = sct.grab(
        {
            "left": capture_region.left,
            "top": capture_region.top,
            "width": capture_region.width,
            "height": capture_region.height,
        }
    )
    screenshot = deps["Image"].frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
    width, height = screenshot.size
    encoded = _downscale_if_needed(screenshot, max_long_edge=max_long_edge)
    base64_data = _encode_image_from_pil(encoded, fmt, quality)
    return screenshot, width, height, base64_data, capture_region


def load_image_file(
    deps: dict[str, Any],
    image_path: str | Path,
    quality: int,
    fmt: str = SCREENSHOT_FORMAT,
    max_long_edge: int = PERFORMANCE_PROFILES[DEFAULT_PERFORMANCE_PROFILE].screenshot_max_long_edge,
) -> tuple[Any, int, int, str]:
    path = Path(image_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Image file not found: {path}")
    if not path.is_file():
        raise ValueError(f"Image path is not a file: {path}")

    with deps["Image"].open(path) as image:
        loaded = image.copy()
    width, height = loaded.size
    encoded = _downscale_if_needed(loaded, max_long_edge=max_long_edge)
    base64_data = _encode_image_from_pil(encoded, fmt, quality)
    return loaded, width, height, base64_data


def _platform_search_hint() -> tuple[str, str, str]:
    system = platform.system().lower()
    if system == "darwin":
        return "macOS", "Spotlight", "command space"
    if system == "windows":
        return "Windows", "Start menu search", "win s"
    return "Linux", "desktop app search", "ctrl space"


def _format_action_history(action_history: list[str] | None) -> str | None:
    if not action_history:
        return None
    trimmed = [item.strip() for item in action_history if item.strip()]
    if not trimmed:
        return None
    recent = trimmed[-12:]
    return "[Action History]\n" + "\n".join(recent)


def _extract_message_recipient(instruction: str) -> str | None:
    text = instruction.strip()
    if not text:
        return None
    patterns = [
        r"给\s*([A-Za-z0-9_\-\u4e00-\u9fff.]+)\s*发送?微信(?:消息)?",
        r"给\s*([A-Za-z0-9_\-\u4e00-\u9fff.]+)\s*发微信(?:消息)?",
        r"\bon\s+wechat\s+to\s+([A-Za-z0-9_.\-]+)\b",
        r"\bsend\s+.*?\bto\s+([A-Za-z0-9_.\-]+)\b.*\bwechat\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            recipient = match.group(1).strip().strip("'\"`“”‘’")
            if recipient:
                return recipient
    return None


def _extract_message_content(instruction: str) -> str | None:
    text = instruction.strip()
    if not text:
        return None
    patterns = [
        r"(?:说|内容是|内容为)\s*[\"'“”‘’]?(.*?)[\"'“”‘’]?\s*$",
        r"(?:message|say|saying)\s*[\"'“”‘’]?(.*?)[\"'“”‘’]?\s*$",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        content = match.group(1).strip()
        content = content.strip("。.!！？")
        if content:
            return content
    return None


def _extract_wechat_send_request(instruction: str) -> WeChatSendRequest | None:
    if _extract_target_app_name(instruction) != "WeChat":
        return None
    recipient = _extract_message_recipient(instruction)
    content = _extract_message_content(instruction)
    if not recipient or not content:
        return None
    return WeChatSendRequest(recipient=recipient, content=content)


def build_conversation(
    instruction: str,
    base64_image: str,
    image_format: str = "jpeg",
    action_history: list[str] | None = None,
    step_index: int | None = None,
    recipient_hint: str | None = None,
) -> list[dict[str, Any]]:
    os_name, search_entry, search_hotkey = _platform_search_hint()
    system_prompt = COMPUTER_USE_PROMPT.format(
        instruction=instruction,
        os_name=os_name,
        search_entry=search_entry,
        search_hotkey=search_hotkey,
    )
    image_content: list[dict[str, Any]] = []
    if step_index is not None:
        image_content.append({"type": "text", "text": f"[Step {step_index}]"})
    if recipient_hint:
        image_content.append({"type": "text", "text": f"[Recipient]\n{recipient_hint}"})
    history_text = _format_action_history(action_history)
    if history_text:
        image_content.append({"type": "text", "text": history_text})
    image_content.extend([
        {"type": "text", "text": "[Current Screenshot]"},
        {"type": "image_url", "image_url": {"url": f"data:image/{image_format};base64,{base64_image}"}},
    ])
    return [
        {"role": "user", "content": system_prompt},
        {"role": "user", "content": image_content},
    ]


def build_image_understanding_messages(
    instruction: str,
    base64_image: str,
    image_format: str = "jpeg",
) -> list[dict[str, Any]]:
    user_instruction = instruction.strip() or "Describe this image in detail."
    user_content = [
        {"type": "text", "text": user_instruction},
        {"type": "image_url", "image_url": {"url": f"data:image/{image_format};base64,{base64_image}"}},
    ]
    return [
        {"role": "system", "content": IMAGE_UNDERSTANDING_PROMPT},
        {"role": "user", "content": user_content},
    ]


def _wechat_search_hotkey() -> str:
    return "command f" if platform.system() == "Darwin" else "ctrl f"


def _select_all_hotkey() -> str:
    return "command a" if platform.system() == "Darwin" else "ctrl a"


def _build_wechat_recipient_selection_instruction(recipient: str) -> str:
    return (
        f"You are inside WeChat. The goal is to select the chat for recipient '{recipient}'. "
        "The recipient name is already known. "
        "If the CURRENT screenshot clearly shows that the active chat header or selected conversation is exactly this recipient, "
        "output <action>finished(content='recipient_selected')</action>. "
        "Otherwise, click the exact recipient result or conversation entry for this recipient. "
        "Do not type any text. Do not send any message. Do not use close/quit shortcuts."
    )


def _normalize_chat_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if text:
                    texts.append(str(text))
        return "\n".join(texts)
    return str(content)


def _get_httpx_client(deps: dict[str, Any], timeout_seconds: float) -> Any:
    client = _httpx_clients.get(timeout_seconds)
    if client is None:
        try:
            client = deps["httpx"].Client(timeout=timeout_seconds, http2=True)
        except ImportError:
            client = deps["httpx"].Client(timeout=timeout_seconds)
        _httpx_clients[timeout_seconds] = client
    return client


def call_model_inference(
    deps: dict[str, Any],
    messages: list[dict[str, Any]],
    api_key: str,
    model_name: str | None = None,
    base_url: str | None = None,
    timeout_seconds: float = PERFORMANCE_PROFILES[DEFAULT_PERFORMANCE_PROFILE].request_timeout_seconds,
    max_tokens: int | None = None,
) -> str:
    if not api_key:
        raise ValueError("api_key is required. Run `migi setup` or provide --api-key.")
    model_name = model_name or DEFAULT_MODEL
    base_url = base_url or DEFAULT_BASE_URL

    endpoint = base_url.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint = f"{endpoint}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model_name,
        "messages": messages,
        "temperature": 0.0,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    client = _get_httpx_client(deps, timeout_seconds=timeout_seconds)
    response = client.post(endpoint, headers=headers, json=payload)
    if response.status_code >= 400 and max_tokens is not None and response.status_code in {400, 422}:
        retry_payload = dict(payload)
        retry_payload.pop("max_tokens", None)
        retry_response = client.post(endpoint, headers=headers, json=retry_payload)
        if retry_response.status_code < 400:
            response = retry_response
    if response.status_code >= 400:
        raise ValueError(
            f"Model request failed ({response.status_code}): {response.text[:500]}"
        )
    data = response.json()

    choices = data.get("choices") or []
    if not choices:
        raise ValueError(f"Model response missing choices: {str(data)[:500]}")
    message = choices[0].get("message") or {}
    content = _normalize_chat_content(message.get("content"))
    if not content:
        raise ValueError(f"Model response missing message content: {str(data)[:500]}")
    return content


KEY_MAPPING = {
    "content": "content",
    "start_box": "start_box",
    "end_box": "end_box",
    "direction": "direction",
}


def _point_to_screen_xy(
    point: list[Any] | tuple[Any, ...],
    image_width: int,
    image_height: int,
    capture_region: CaptureRegion,
    screen_width: int,
    screen_height: int,
    scale_factor: int,
) -> tuple[int, int]:
    x = float(point[0])
    y = float(point[1])
    max_x = max(0, screen_width - 1)
    max_y = max(0, screen_height - 1)

    def _clamp(xy: tuple[float, float]) -> tuple[int, int]:
        return (
            min(max(round(xy[0]), 0), max_x),
            min(max(round(xy[1]), 0), max_y),
        )

    # Support ratio coordinates from some model outputs, e.g. 0.52 0.31.
    if abs(x) <= 1 and abs(y) <= 1:
        return _clamp(
            (
                capture_region.left + (x * capture_region.width),
                capture_region.top + (y * capture_region.height),
            )
        )

    # If screenshot is low resolution (<= scale), "pixel coords" and "normalized coords"
    # can overlap; prefer treating in-image values as absolute pixels in that case.
    low_res_pixel_like = (
        image_width <= scale_factor
        and image_height <= scale_factor
        and 0 <= x <= image_width
        and 0 <= y <= image_height
    )
    if abs(x) <= scale_factor and abs(y) <= scale_factor and not low_res_pixel_like:
        return _clamp(
            (
                capture_region.left + (x * capture_region.width / scale_factor),
                capture_region.top + (y * capture_region.height / scale_factor),
            )
        )

    # Treat as absolute screenshot pixels and remap to pyautogui coordinate space.
    mapped_x = capture_region.left + (x * capture_region.width / image_width if image_width > 0 else x)
    mapped_y = capture_region.top + (y * capture_region.height / image_height if image_height > 0 else y)
    return _clamp((mapped_x, mapped_y))


def _box_to_screen_xy(
    box: list[Any] | tuple[Any, ...],
    image_width: int,
    image_height: int,
    capture_region: CaptureRegion,
    screen_width: int,
    screen_height: int,
    scale_factor: int,
) -> tuple[int, int]:
    if len(box) == 4:
        x = float((box[0] + box[2]) / 2)
        y = float((box[1] + box[3]) / 2)
        return _point_to_screen_xy(
            [x, y],
            image_width,
            image_height,
            capture_region,
            screen_width,
            screen_height,
            scale_factor,
        )
    return _point_to_screen_xy(
        box,
        image_width,
        image_height,
        capture_region,
        screen_width,
        screen_height,
        scale_factor,
    )


def execute_pyautogui_action(
    deps: dict[str, Any],
    responses: dict[str, Any] | list[dict[str, Any]],
    image_height: int,
    image_width: int,
    capture_region: CaptureRegion,
    scale_factor: int = 1000,
    tuning: PerformanceTuning | None = None,
    allow_window_management_hotkeys: bool = False,
) -> list[str]:
    tuning = tuning or resolve_performance_tuning()
    pyautogui = deps["pyautogui"]
    pyperclip = deps["pyperclip"]
    screen_width, screen_height = pyautogui.size()

    if isinstance(responses, dict):
        responses = [responses]

    result_info: list[str] = []
    for index, response in enumerate(responses):
        if index > 0:
            time.sleep(tuning.action_inter_step_delay)
        action_type = response.get("action_type")
        action_inputs = response.get("action_inputs", {})

        normalized_inputs: dict[str, Any] = {}
        for key, value in action_inputs.items():
            new_key = KEY_MAPPING.get(key, key)
            if isinstance(value, str) and "<point>" in value:
                value = safe_literal_eval(value)
            normalized_inputs[new_key] = value
        action_inputs = normalized_inputs

        if action_type == "type":
            content = action_inputs.get("content", "")
            if content:
                pyperclip.copy(content)
                time.sleep(tuning.action_clipboard_sync_delay)
                if platform.system() == "Darwin":
                    pyautogui.hotkey("command", "v", interval=tuning.action_hotkey_interval)
                else:
                    pyautogui.hotkey("ctrl", "v", interval=tuning.action_hotkey_interval)
                time.sleep(tuning.action_paste_settle_delay)
                if content.endswith("\n") or content.endswith("\\n"):
                    pyautogui.press("enter")
                result_info.append(f"type:{content[:50]}")
        elif action_type in {"drag", "select"}:
            start_box = action_inputs.get("start_box")
            end_box = action_inputs.get("end_box")
            if start_box and end_box:
                if isinstance(start_box, str):
                    start_box = safe_literal_eval(start_box)
                if isinstance(end_box, str):
                    end_box = safe_literal_eval(end_box)
                sx, sy = _box_to_screen_xy(
                    start_box,
                    image_width,
                    image_height,
                    capture_region,
                    screen_width,
                    screen_height,
                    scale_factor,
                )
                ex, ey = _box_to_screen_xy(
                    end_box,
                    image_width,
                    image_height,
                    capture_region,
                    screen_width,
                    screen_height,
                    scale_factor,
                )
                pyautogui.moveTo(sx, sy)
                pyautogui.dragTo(ex, ey, duration=1.0)
                result_info.append(f"drag:{sx},{sy}->{ex},{ey}")
        elif action_type == "scroll":
            start_box = action_inputs.get("start_box")
            direction = str(action_inputs.get("direction", "")).lower()
            x = y = None
            if start_box:
                if isinstance(start_box, str):
                    start_box = safe_literal_eval(start_box)
                x, y = _box_to_screen_xy(
                    start_box,
                    image_width,
                    image_height,
                    capture_region,
                    screen_width,
                    screen_height,
                    scale_factor,
                )
            scroll_amount = max(1, int(screen_height * 0.25) // PIXELS_PER_SCROLL_CLICK)
            if "up" in direction:
                pyautogui.scroll(scroll_amount, x=x, y=y)
                result_info.append("scroll:up")
            elif "down" in direction:
                pyautogui.scroll(-scroll_amount, x=x, y=y)
                result_info.append("scroll:down")
        elif action_type in {"click", "left_single", "left_double", "right_single", "hover"}:
            start_box = action_inputs.get("start_box")
            if start_box:
                if isinstance(start_box, str):
                    start_box = safe_literal_eval(start_box)
                x, y = _box_to_screen_xy(
                    start_box,
                    image_width,
                    image_height,
                    capture_region,
                    screen_width,
                    screen_height,
                    scale_factor,
                )
                if action_type in {"left_single", "click"}:
                    pyautogui.click(x, y)
                    result_info.append(f"click:{x},{y}")
                elif action_type == "left_double":
                    pyautogui.doubleClick(x, y)
                    result_info.append(f"double_click:{x},{y}")
                elif action_type == "right_single":
                    pyautogui.click(x, y, button="right")
                    result_info.append(f"right_click:{x},{y}")
                elif action_type == "hover":
                    pyautogui.moveTo(x, y)
                    result_info.append(f"hover:{x},{y}")
        elif action_type == "hotkey":
            key_combo = action_inputs.get("content", "") or action_inputs.get("key", "")
            keys = [item for item in re.split(r"[+\s,]+", str(key_combo).lower().strip()) if item]
            if keys:
                is_macos = platform.system() == "Darwin"
                meta_key = "command" if is_macos else "winleft"
                key_map = {
                    "ctrl": "ctrl",
                    "control": "ctrl",
                    "cmd": "command" if is_macos else meta_key,
                    "meta": meta_key,
                    "super": meta_key,
                    "win": "winleft",
                    "windows": "winleft",
                    "command": "command" if is_macos else meta_key,
                    "alt": "alt",
                    "option": "alt",
                    "shift": "shift",
                    "enter": "enter",
                    "return": "enter",
                    "tab": "tab",
                    "esc": "escape",
                    "escape": "escape",
                    "space": "space",
                    "backspace": "backspace",
                    "delete": "delete",
                }
                mapped = [key_map.get(item, item) for item in keys]
                supported_keys = set(getattr(pyautogui, "KEYBOARD_KEYS", []))
                if supported_keys:
                    unknown = [item for item in mapped if item not in supported_keys]
                    if unknown:
                        result_info.append(f"hotkey_skipped:unknown={'+'.join(unknown)}")
                        continue
                try:
                    combo_signature = "+".join(mapped)
                    blocked_hotkeys = {"command+w", "ctrl+w", "command+q", "alt+f4"}
                    if not allow_window_management_hotkeys and combo_signature in blocked_hotkeys:
                        result_info.append(f"hotkey_skipped:blocked={combo_signature}")
                        continue
                    if len(mapped) == 1:
                        pyautogui.press(mapped[0])
                    else:
                        pyautogui.hotkey(*mapped, interval=tuning.action_hotkey_interval)
                    result_info.append(f"hotkey:{combo_signature}")
                except Exception as exc:  # noqa: BLE001
                    result_info.append(f"hotkey_failed:{type(exc).__name__}")
        elif action_type == "finished":
            result_info.append("finished")
            return result_info
        elif action_type == "wait":
            time.sleep(tuning.action_wait_duration)
            result_info.append(f"wait:{tuning.action_wait_duration}s")
    return result_info


def _split_multi_actions(response: str) -> list[str]:
    actions: list[str] = []
    xml_pattern = re.compile(r"<action>(.*?)</action>", re.IGNORECASE | re.DOTALL)
    xml_matches = xml_pattern.findall(response)
    if xml_matches:
        for match in xml_matches:
            action_str = match.strip()
            if action_str:
                actions.append(f"Action: {action_str}")
        return actions

    action_funcs = [
        "click",
        "left_double",
        "right_single",
        "drag",
        "hotkey",
        "type",
        "scroll",
        "wait",
        "finished",
        "left_single",
        "hover",
        "select",
    ]
    pattern = re.compile(r"(?:Action:\s*)?(" + "|".join(action_funcs) + r")\s*\([^)]*\)", re.IGNORECASE)
    for match in pattern.finditer(response):
        action_str = match.group(0)
        if action_str.lower().startswith("action:"):
            action_str = action_str[7:].strip()
        actions.append(f"Action: {action_str}")
    return actions if actions else [f"Action: {response.strip()}"]


def _extract_call_parts(action_str: str) -> tuple[str, str]:
    raw = action_str.strip()
    if raw.lower().startswith("action:"):
        raw = raw[7:].strip()
    match = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\((.*)\)$", raw, re.DOTALL)
    if not match:
        return raw.lower(), ""
    return match.group(1).lower(), match.group(2).strip()


def _extract_quoted_arg(args: str, name: str) -> str | None:
    pattern = re.compile(rf"{name}\s*=\s*'([^']*)'")
    match = pattern.search(args)
    if match:
        return match.group(1)
    pattern = re.compile(rf'{name}\s*=\s*"([^"]*)"')
    match = pattern.search(args)
    if match:
        return match.group(1)
    return None


def _looks_like_app_launch_instruction(instruction: str) -> bool:
    text = instruction.strip()
    if not text:
        return False
    if re.search(r"\b(open|launch|start)\b", text, re.IGNORECASE):
        return True
    return bool(re.search(r"(打开|启动|运行)\s*\S+", text))


def _extract_target_app_name(instruction: str) -> str | None:
    text = instruction.strip()
    if not text:
        return None
    lowered = text.lower()
    for canonical_name, aliases in KNOWN_APP_ALIASES.items():
        for alias in aliases:
            if alias.isascii():
                if re.search(rf"\b{re.escape(alias)}\b", lowered, re.IGNORECASE):
                    return canonical_name
            elif alias in text:
                return canonical_name
    return None


def _is_launch_only_instruction(instruction: str) -> bool:
    text = instruction.strip()
    if not _looks_like_app_launch_instruction(text):
        return False
    if re.search(r"\b(and|then|after|search|type|click|send|message)\b", text, re.IGNORECASE):
        return False
    if re.search(r"(然后|再|并|给|发送|点击|输入|搜索|消息)", text):
        return False
    return True


def _extract_app_name_from_instruction(instruction: str) -> str | None:
    text = instruction.strip()
    if not text:
        return None
    first_clause = re.split(r"[。.!！？\n,，;；]", text, maxsplit=1)[0].strip()
    if not first_clause:
        return None

    quoted = re.search(r"[\"'“”‘’`]\s*([^\"'“”‘’`]{1,80})\s*[\"'“”‘’`]", first_clause)
    if quoted:
        candidate = quoted.group(1)
    else:
        eng = re.search(
            r"\b(?:open|launch|start)\b\s+(?:the\s+)?([A-Za-z0-9][\w .+\-]{0,80})",
            first_clause,
            re.IGNORECASE,
        )
        zh = re.search(r"(?:打开|启动|运行)\s*([A-Za-z0-9\u4e00-\u9fff._\- ]{1,80})", first_clause)
        candidate = eng.group(1) if eng else (zh.group(1) if zh else "")

    cleaned = candidate.strip().strip("'\"`“”‘’")
    cleaned = re.sub(r"\s+(app|application)$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"(应用程序|应用)$", "", cleaned)
    cleaned = cleaned.strip(" -:_")
    return cleaned or _extract_target_app_name(instruction)


def _dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in values:
        key = item.strip()
        if not key:
            continue
        norm = key.lower()
        if norm in seen:
            continue
        seen.add(norm)
        result.append(key)
    return result


def _app_name_candidates(app_name: str) -> list[str]:
    base = app_name.strip()
    key = base.lower()
    candidates = [base]
    for canonical_name, alias_group in KNOWN_APP_ALIASES.items():
        values = [canonical_name, *alias_group]
        if key in {value.lower() for value in values}:
            candidates.extend(values)
    return _dedupe_keep_order(candidates)


def _powershell_start_process_command(file_path: str, expand_env: bool = False) -> list[str]:
    if expand_env:
        script = f'Start-Process -FilePath "{file_path}" -ErrorAction Stop'
    else:
        escaped = file_path.replace("'", "''")
        script = f"Start-Process -FilePath '{escaped}' -ErrorAction Stop"
    return ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]


def _build_direct_launch_commands(app_name: str) -> list[tuple[str, list[str]]]:
    system = platform.system().lower()
    candidates = _app_name_candidates(app_name)
    commands: list[tuple[str, list[str]]] = []

    def _add(label: str, command: list[str]) -> None:
        signature = " ".join(command)
        if any(" ".join(existing) == signature for _, existing in commands):
            return
        commands.append((label, command))

    if system == "darwin":
        for item in candidates:
            if item.endswith(".app") or "/" in item:
                _add(f"mac-open-path:{item}", ["open", item])
            _add(f"mac-open-a:{item}", ["open", "-a", item])
            if "/" not in item and not item.endswith(".app"):
                app_path = str(Path("/Applications") / f"{item}.app")
                _add(f"mac-open-path:{app_path}", ["open", app_path])
        return commands

    if system == "windows":
        for item in candidates:
            _add(f"win-start-process:{item}", _powershell_start_process_command(item))
            if not item.lower().endswith(".exe"):
                _add(
                    f"win-start-process:{item}.exe",
                    _powershell_start_process_command(f"{item}.exe"),
                )
        if any(name.lower() in {"wechat", "微信"} for name in candidates):
            known_paths = [
                r"C:\Program Files\Tencent\WeChat\WeChat.exe",
                r"C:\Program Files (x86)\Tencent\WeChat\WeChat.exe",
                r"$env:LOCALAPPDATA\Tencent\WeChat\WeChat.exe",
            ]
            for path in known_paths:
                _add(
                    f"win-known-path:{path}",
                    _powershell_start_process_command(path, expand_env=path.startswith("$env:")),
                )
        return commands

    return commands


def _run_launch_command(command: list[str], timeout_seconds: float = 8.0) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"exception:{type(exc).__name__}"

    if completed.returncode == 0:
        return True, "ok"

    detail = (completed.stderr or completed.stdout or "").strip().replace("\n", " ")
    if not detail:
        detail = f"rc={completed.returncode}"
    return False, detail[:120]


def _try_direct_app_launch(instruction: str, app_name: str | None = None) -> tuple[bool, list[str]]:
    app_name = app_name or _extract_app_name_from_instruction(instruction)
    if not app_name:
        return False, ["direct_launch_skipped:no_app_name"]

    commands = _build_direct_launch_commands(app_name)
    if not commands:
        return False, ["direct_launch_skipped:unsupported_os"]

    steps: list[str] = [f"direct_launch_target:{app_name}"]
    for label, command in commands:
        ok, detail = _run_launch_command(command)
        if ok:
            steps.append(f"direct_launch_ok:{label}")
            return True, steps
        steps.append(f"direct_launch_fail:{label}:{detail}")
    return False, steps


def _has_hotkey_failure(execution_result: list[str] | None) -> bool:
    if not execution_result:
        return False
    return any(
        item.startswith("hotkey_failed:") or item.startswith("hotkey_skipped:")
        for item in execution_result
    )


def _build_app_launch_fallback_instruction(instruction: str) -> str:
    return (
        f"{instruction}\n\n"
        "[Fallback mode] Previous app-search hotkey execution failed. "
        "Do not use app-search hotkey now. "
        "Use visible GUI controls to open search, then select the exact application result before opening."
    )


def _instruction_allows_window_management_hotkeys(instruction: str) -> bool:
    text = instruction.strip()
    if not text:
        return False
    if re.search(r"\b(close|quit|exit|dismiss|hide|minimize)\b", text, re.IGNORECASE):
        return True
    return bool(re.search(r"(关闭|退出|最小化|隐藏|收起|关掉)", text))


def _extract_point_arg(args: str, name: str) -> list[float] | None:
    value = _extract_quoted_arg(args, name)
    if not value:
        return None
    cleaned = value.replace("<point>", "").replace("</point>", "").strip()
    parts = re.split(r"\s+", cleaned)
    if len(parts) < 2:
        return None
    try:
        return [float(parts[0]), float(parts[1])]
    except ValueError:
        return None


def _parse_builtin_action(action_str: str) -> dict[str, Any] | None:
    action_type, args = _extract_call_parts(action_str)
    if action_type in {"click", "left_single", "left_double", "right_single", "hover"}:
        point = _extract_point_arg(args, "point") or _extract_point_arg(args, "start_point")
        if not point:
            return None
        return {"action_type": action_type, "action_inputs": {"start_box": point}}
    if action_type in {"drag", "select"}:
        start = _extract_point_arg(args, "start_point")
        end = _extract_point_arg(args, "end_point")
        if not start or not end:
            return None
        return {"action_type": action_type, "action_inputs": {"start_box": start, "end_box": end}}
    if action_type == "scroll":
        direction = _extract_quoted_arg(args, "direction") or "down"
        point = _extract_point_arg(args, "point")
        inputs: dict[str, Any] = {"direction": direction}
        if point:
            inputs["start_box"] = point
        return {"action_type": action_type, "action_inputs": inputs}
    if action_type == "type":
        content = _extract_quoted_arg(args, "content")
        if content is None:
            return None
        return {"action_type": action_type, "action_inputs": {"content": content}}
    if action_type == "hotkey":
        key_combo = _extract_quoted_arg(args, "key") or _extract_quoted_arg(args, "content")
        if key_combo is None:
            return None
        return {"action_type": action_type, "action_inputs": {"key": key_combo}}
    if action_type == "wait":
        return {"action_type": action_type, "action_inputs": {}}
    if action_type == "finished":
        content = _extract_quoted_arg(args, "content")
        inputs = {"content": content} if content else {}
        return {"action_type": action_type, "action_inputs": inputs}
    return None


def _load_custom_action_parser(callable_path: str) -> Callable[[str, int, int, int], Any]:
    if ":" not in callable_path:
        raise ValueError(
            "Custom action parser must be in 'module:function' format, "
            f"got: {callable_path}"
        )
    module_name, func_name = callable_path.split(":", 1)
    module = importlib.import_module(module_name)
    parser = getattr(module, func_name)
    if not callable(parser):
        raise ValueError(f"Custom action parser is not callable: {callable_path}")
    return parser


def parse_and_execute_action(
    deps: dict[str, Any],
    response: str,
    img_height: int,
    img_width: int,
    capture_region: CaptureRegion,
    action_parser: str = "builtin",
    action_parser_callable: str | None = None,
    scale_factor: int = 1000,
    tuning: PerformanceTuning | None = None,
    allow_window_management_hotkeys: bool = False,
) -> tuple[bool, list[str], str | None]:
    action_strings = _split_multi_actions(response)
    parsed_all: list[dict[str, Any]] = []
    first_type: str | None = None
    parser_name = (action_parser or "builtin").strip().lower()

    if parser_name == "custom":
        if not action_parser_callable:
            raise ValueError(
                "action_parser is set to 'custom' but action_parser_callable is not configured."
            )
        parser = _load_custom_action_parser(action_parser_callable)
        parsed = parser(response, img_width, img_height, scale_factor)
        if isinstance(parsed, dict):
            parsed_all = [parsed]
        elif isinstance(parsed, list):
            parsed_all = [item for item in parsed if isinstance(item, dict)]
    elif parser_name == "ui_tars":
        try:
            from ui_tars.action_parser import parse_action_to_structure_output
        except ModuleNotFoundError as exc:
            raise DependencyError(
                "Action parser 'ui_tars' requires dependency: ui-tars-sdk"
            ) from exc
        for action_str in action_strings:
            normalized = action_str.strip()
            if "Action:" not in normalized:
                normalized = f"Action: {normalized}"
            try:
                with open(os.devnull, "w", encoding="utf-8") as devnull:
                    with contextlib.redirect_stdout(devnull), contextlib.redirect_stderr(devnull):
                        parsed = parse_action_to_structure_output(
                            normalized,
                            1,
                            img_width,
                            img_height,
                            model_type="openai",
                            max_pixels=MAX_PIXELS,
                            min_pixels=MIN_PIXELS,
                        )
                if isinstance(parsed, dict):
                    parsed_all.append(parsed)
                elif isinstance(parsed, list):
                    parsed_all.extend(item for item in parsed if isinstance(item, dict))
            except ValueError:
                continue
    else:
        for action_str in action_strings:
            parsed = _parse_builtin_action(action_str)
            if parsed:
                parsed_all.append(parsed)

    if parsed_all:
        first_type = parsed_all[0].get("action_type")
    if not parsed_all:
        return False, [], None
    result = execute_pyautogui_action(
        deps,
        parsed_all,
        img_height,
        img_width,
        capture_region,
        scale_factor,
        tuning=tuning,
        allow_window_management_hotkeys=allow_window_management_hotkeys,
    )
    return True, result, first_type


def _run_wechat_send_flow(
    deps: dict[str, Any],
    request: WeChatSendRequest,
    api_key: str,
    model_name: str | None,
    base_url: str | None,
    action_parser: str,
    action_parser_callable: str | None,
    performance_profile: str,
    max_steps: int,
) -> AutomationResult:
    timing: dict[str, float] = {}
    total_start = time.perf_counter()
    tuning = resolve_performance_tuning(performance_profile)
    max_attempts = max(1, min(max_steps, 3))
    action_history: list[str] = []
    response_chunks: list[str] = []
    execution_result: list[str] = []
    img_width = img_height = 0
    capture_region = CaptureRegion(left=0, top=0, width=0, height=0, source="screen")

    def _capture_window(label: str) -> tuple[int, int, str, CaptureRegion]:
        nonlocal img_width, img_height, capture_region
        step = time.perf_counter()
        _, img_width, img_height, base64_image, capture_region = capture_screenshot(
            deps,
            quality=tuning.screenshot_quality,
            max_long_edge=tuning.screenshot_max_long_edge,
            capture_mode="window",
            app_launch_intent=False,
        )
        timing[label] = timing.get(label, 0.0) + (time.perf_counter() - step) * 1000
        return img_width, img_height, base64_image, capture_region

    try:
        step = time.perf_counter()
        direct_ok, direct_steps = _try_direct_app_launch(
            f"打开微信并准备给 {request.recipient} 发消息",
            app_name="WeChat",
        )
        timing["direct_launch_ms"] = (time.perf_counter() - step) * 1000
        execution_result.extend(direct_steps)
        if direct_ok:
            action_history.append("WeChat brought to foreground by direct launch.")

        timing["wechat_search_screenshot_ms"] = 0.0
        step = time.perf_counter()
        search_actions = [
            {"action_type": "hotkey", "action_inputs": {"key": _wechat_search_hotkey()}},
            {"action_type": "hotkey", "action_inputs": {"key": _select_all_hotkey()}},
            {"action_type": "type", "action_inputs": {"content": request.recipient}},
            {"action_type": "hotkey", "action_inputs": {"key": "enter"}},
        ]
        search_steps = execute_pyautogui_action(
            deps=deps,
            responses=search_actions,
            image_height=max(img_height, 1),
            image_width=max(img_width, 1),
            capture_region=capture_region,
            tuning=tuning,
            allow_window_management_hotkeys=False,
        )
        timing["wechat_search_execution_ms"] = (time.perf_counter() - step) * 1000
        execution_result.extend(search_steps)
        action_history.append(
            "WeChat deterministic search steps: "
            + (", ".join(search_steps) if search_steps else "no_action")
        )

        recipient_selected = False
        for attempt in range(1, max_attempts + 1):
            _, _, base64_image, current_region = _capture_window(f"wechat_select_step_{attempt}_screenshot_ms")
            selection_instruction = _build_wechat_recipient_selection_instruction(request.recipient)
            step = time.perf_counter()
            messages = build_conversation(
                selection_instruction,
                base64_image,
                SCREENSHOT_FORMAT,
                action_history=action_history,
                step_index=attempt,
                recipient_hint=request.recipient,
            )
            timing[f"wechat_select_step_{attempt}_build_conv_ms"] = (time.perf_counter() - step) * 1000

            step = time.perf_counter()
            response = call_model_inference(
                deps,
                messages,
                api_key=api_key,
                model_name=model_name,
                base_url=base_url,
                timeout_seconds=tuning.request_timeout_seconds,
                max_tokens=tuning.computer_use_max_tokens,
            )
            timing[f"wechat_select_step_{attempt}_inference_ms"] = (time.perf_counter() - step) * 1000
            response_chunks.append(f"# WeChat Select {attempt}\n{response}")
            action_history.append(f"WeChat select attempt {attempt} response: {response}")

            step = time.perf_counter()
            triggered, step_result, step_type = parse_and_execute_action(
                deps=deps,
                response=response,
                img_height=img_height,
                img_width=img_width,
                capture_region=current_region,
                action_parser=action_parser,
                action_parser_callable=action_parser_callable,
                scale_factor=COORDINATE_SCALE,
                tuning=tuning,
                allow_window_management_hotkeys=False,
            )
            timing[f"wechat_select_step_{attempt}_execution_ms"] = (time.perf_counter() - step) * 1000
            execution_result.extend(step_result)
            action_history.append(
                "WeChat select attempt "
                f"{attempt} execution: "
                + (", ".join(step_result) if step_result else "no_action")
            )

            if step_type == "finished" or any(item == "finished" for item in step_result):
                recipient_selected = True
                break
            if not triggered:
                break

        if not recipient_selected:
            timing["total_ms"] = (time.perf_counter() - total_start) * 1000
            return AutomationResult(
                success=False,
                image_size=(img_width, img_height) if img_width and img_height else None,
                response="\n\n".join(response_chunks) if response_chunks else None,
                execution_result=execution_result,
                action_triggered=bool(execution_result),
                action_type="wechat_send",
                timing=timing,
                error=f"WeChat recipient '{request.recipient}' was not confirmed before sending.",
            )

        timing["wechat_send_screenshot_ms"] = 0.0
        step = time.perf_counter()
        send_steps = execute_pyautogui_action(
            deps=deps,
            responses=[
                {"action_type": "type", "action_inputs": {"content": request.content}},
                {"action_type": "hotkey", "action_inputs": {"key": "enter"}},
            ],
            image_height=max(img_height, 1),
            image_width=max(img_width, 1),
            capture_region=capture_region,
            tuning=tuning,
            allow_window_management_hotkeys=False,
        )
        timing["wechat_send_execution_ms"] = (time.perf_counter() - step) * 1000
        execution_result.extend(send_steps)
        action_history.append(
            "WeChat send execution: " + (", ".join(send_steps) if send_steps else "no_action")
        )

        timing["total_ms"] = (time.perf_counter() - total_start) * 1000
        return AutomationResult(
            success=True,
            image_size=(img_width, img_height),
            response="\n\n".join(response_chunks) if response_chunks else "finished(content='wechat_send_flow_complete')",
            execution_result=execution_result,
            action_triggered=True,
            action_type="wechat_send",
            timing=timing,
            error=None,
        )
    except Exception as exc:  # noqa: BLE001
        timing["total_ms"] = (time.perf_counter() - total_start) * 1000
        return AutomationResult(
            success=False,
            image_size=(img_width, img_height) if img_width and img_height else None,
            response="\n\n".join(response_chunks) if response_chunks else None,
            execution_result=execution_result or None,
            action_triggered=bool(execution_result),
            action_type="wechat_send",
            timing=timing,
            error=str(exc),
        )


def auto_image_understanding(
    instruction: str,
    image_path: str | Path,
    api_key: str,
    model_name: str | None = None,
    base_url: str | None = None,
    performance_profile: str = DEFAULT_PERFORMANCE_PROFILE,
) -> AutomationResult:
    timing: dict[str, float] = {}
    total_start = time.perf_counter()
    try:
        deps = _import_vision_dependencies()
        tuning = resolve_performance_tuning(performance_profile)

        step = time.perf_counter()
        _, img_width, img_height, base64_image = load_image_file(
            deps,
            image_path,
            quality=tuning.screenshot_quality,
            max_long_edge=tuning.screenshot_max_long_edge,
        )
        timing["image_load_ms"] = (time.perf_counter() - step) * 1000

        step = time.perf_counter()
        messages = build_image_understanding_messages(instruction, base64_image, SCREENSHOT_FORMAT)
        timing["build_conv_ms"] = (time.perf_counter() - step) * 1000

        step = time.perf_counter()
        response = call_model_inference(
            deps,
            messages,
            api_key=api_key,
            model_name=model_name,
            base_url=base_url,
            timeout_seconds=tuning.request_timeout_seconds,
            max_tokens=tuning.image_max_tokens,
        )
        timing["inference_ms"] = (time.perf_counter() - step) * 1000

        timing["total_ms"] = (time.perf_counter() - total_start) * 1000
        return AutomationResult(
            success=True,
            image_size=(img_width, img_height),
            response=response,
            execution_result=None,
            action_triggered=False,
            action_type=None,
            timing=timing,
            error=None,
        )
    except Exception as exc:  # noqa: BLE001
        timing["total_ms"] = (time.perf_counter() - total_start) * 1000
        return AutomationResult(
            success=False,
            image_size=None,
            response=None,
            execution_result=None,
            action_triggered=False,
            action_type=None,
            timing=timing,
            error=str(exc),
        )


def auto_screen_operation(
    instruction: str,
    api_key: str,
    model_name: str | None = None,
    base_url: str | None = None,
    action_parser: str = "builtin",
    action_parser_callable: str | None = None,
    execute_action: bool = True,
    performance_profile: str = DEFAULT_PERFORMANCE_PROFILE,
    capture_mode: str = DEFAULT_CAPTURE_MODE,
    max_steps: int = 1,
) -> AutomationResult:
    timing: dict[str, float] = {}
    total_start = time.perf_counter()
    try:
        deps = _import_gui_dependencies()
        tuning = resolve_performance_tuning(performance_profile)
        max_steps = max(1, int(max_steps))
        wechat_send_request = _extract_wechat_send_request(instruction) if execute_action else None
        if wechat_send_request is not None:
            return _run_wechat_send_flow(
                deps=deps,
                request=wechat_send_request,
                api_key=api_key,
                model_name=model_name,
                base_url=base_url,
                action_parser=action_parser,
                action_parser_callable=action_parser_callable,
                performance_profile=performance_profile,
                max_steps=max_steps,
            )
        explicit_launch_intent = _looks_like_app_launch_instruction(instruction)
        target_app_name = _extract_target_app_name(instruction)
        recipient_hint = _extract_message_recipient(instruction)
        allow_window_management_hotkeys = _instruction_allows_window_management_hotkeys(instruction)
        app_launch_intent = explicit_launch_intent or target_app_name is not None
        needs_launch_context = app_launch_intent
        pre_execution_steps: list[str] = []
        action_history: list[str] = []
        response_chunks: list[str] = []
        execution_result: list[str] | None = None
        action_triggered = False
        action_type: str | None = None
        img_width = img_height = 0
        capture_region = CaptureRegion(left=0, top=0, width=0, height=0, source="screen")

        def _record_timing(name: str, value_ms: float, step_index: int) -> None:
            timing[name] = timing.get(name, 0.0) + value_ms
            timing[f"step_{step_index}_{name}"] = value_ms

        if execute_action and app_launch_intent:
            step = time.perf_counter()
            direct_ok, direct_steps = _try_direct_app_launch(instruction, app_name=target_app_name)
            timing["direct_launch_ms"] = (time.perf_counter() - step) * 1000
            pre_execution_steps.extend(direct_steps)
            if direct_ok:
                needs_launch_context = False
            if direct_ok and _is_launch_only_instruction(instruction):
                timing["total_ms"] = (time.perf_counter() - total_start) * 1000
                return AutomationResult(
                    success=True,
                    image_size=None,
                    response="finished(content='app launched by direct command')",
                    execution_result=pre_execution_steps,
                    action_triggered=True,
                    action_type="direct_launch",
                    timing=timing,
                    error=None,
                )

        for step_index in range(1, max_steps + 1):
            step = time.perf_counter()
            _, img_width, img_height, base64_image, capture_region = capture_screenshot(
                deps,
                quality=tuning.screenshot_quality,
                max_long_edge=tuning.screenshot_max_long_edge,
                capture_mode=capture_mode,
                app_launch_intent=needs_launch_context,
            )
            _record_timing("screenshot_ms", (time.perf_counter() - step) * 1000, step_index)

            step = time.perf_counter()
            messages = build_conversation(
                instruction,
                base64_image,
                SCREENSHOT_FORMAT,
                action_history=action_history,
                step_index=step_index if max_steps > 1 else None,
                recipient_hint=recipient_hint,
            )
            _record_timing("build_conv_ms", (time.perf_counter() - step) * 1000, step_index)

            step = time.perf_counter()
            response = call_model_inference(
                deps,
                messages,
                api_key=api_key,
                model_name=model_name,
                base_url=base_url,
                timeout_seconds=tuning.request_timeout_seconds,
                max_tokens=tuning.computer_use_max_tokens,
            )
            _record_timing("inference_ms", (time.perf_counter() - step) * 1000, step_index)
            response_chunks.append(response if max_steps == 1 else f"# Step {step_index}\n{response}")
            action_history.append(f"Step {step_index} model response: {response}")

            if not execute_action:
                break

            step = time.perf_counter()
            time.sleep(tuning.action_pre_exec_delay)
            step_triggered, step_result, step_type = parse_and_execute_action(
                deps=deps,
                response=response,
                img_height=img_height,
                img_width=img_width,
                capture_region=capture_region,
                action_parser=action_parser,
                action_parser_callable=action_parser_callable,
                scale_factor=COORDINATE_SCALE,
                tuning=tuning,
                allow_window_management_hotkeys=allow_window_management_hotkeys,
            )
            _record_timing("execution_ms", (time.perf_counter() - step) * 1000, step_index)
            if execution_result is None:
                execution_result = []
                if pre_execution_steps:
                    execution_result.extend(pre_execution_steps)
            execution_result.extend(step_result)
            action_history.append(
                "Step "
                f"{step_index} execution result: "
                + (", ".join(step_result) if step_result else "no_action")
            )

            if step_triggered:
                action_triggered = True
                action_type = step_type or action_type
            if not step_triggered:
                break
            if any(item == "finished" for item in step_result):
                break

            # For app launch intents: if shortcut execution failed, retry once with GUI fallback guidance.
            if app_launch_intent and _has_hotkey_failure(step_result):
                step = time.perf_counter()
                _, fb_img_width, fb_img_height, fb_base64_image, fb_capture_region = capture_screenshot(
                    deps,
                    quality=tuning.screenshot_quality,
                    max_long_edge=tuning.screenshot_max_long_edge,
                    capture_mode=capture_mode,
                    app_launch_intent=needs_launch_context,
                )
                timing["fallback_screenshot_ms"] = timing.get("fallback_screenshot_ms", 0.0) + (
                    (time.perf_counter() - step) * 1000
                )

                step = time.perf_counter()
                fallback_messages = build_conversation(
                    _build_app_launch_fallback_instruction(instruction),
                    fb_base64_image,
                    SCREENSHOT_FORMAT,
                    action_history=action_history,
                    step_index=step_index,
                    recipient_hint=recipient_hint,
                )
                timing["fallback_build_conv_ms"] = timing.get("fallback_build_conv_ms", 0.0) + (
                    (time.perf_counter() - step) * 1000
                )

                step = time.perf_counter()
                fallback_response = call_model_inference(
                    deps,
                    fallback_messages,
                    api_key=api_key,
                    model_name=model_name,
                    base_url=base_url,
                    timeout_seconds=tuning.request_timeout_seconds,
                    max_tokens=tuning.computer_use_max_tokens,
                )
                timing["fallback_inference_ms"] = timing.get("fallback_inference_ms", 0.0) + (
                    (time.perf_counter() - step) * 1000
                )
                response_chunks.append(f"# Step {step_index} fallback\n{fallback_response}")
                action_history.append(f"Step {step_index} fallback response: {fallback_response}")

                step = time.perf_counter()
                time.sleep(tuning.action_pre_exec_delay)
                fb_triggered, fb_result, fb_type = parse_and_execute_action(
                    deps=deps,
                    response=fallback_response,
                    img_height=fb_img_height,
                    img_width=fb_img_width,
                    capture_region=fb_capture_region,
                    action_parser=action_parser,
                    action_parser_callable=action_parser_callable,
                    scale_factor=COORDINATE_SCALE,
                    tuning=tuning,
                    allow_window_management_hotkeys=allow_window_management_hotkeys,
                )
                timing["fallback_execution_ms"] = timing.get("fallback_execution_ms", 0.0) + (
                    (time.perf_counter() - step) * 1000
                )
                execution_result.extend(fb_result)
                action_history.append(
                    "Step "
                    f"{step_index} fallback execution result: "
                    + (", ".join(fb_result) if fb_result else "no_action")
                )

                if fb_triggered:
                    action_triggered = True
                    if action_type in {None, "hotkey"}:
                        action_type = fb_type
                if any(item == "finished" for item in fb_result):
                    break

        timing["total_ms"] = (time.perf_counter() - total_start) * 1000
        return AutomationResult(
            success=True,
            image_size=(img_width, img_height),
            response="\n\n".join(response_chunks) if response_chunks else None,
            execution_result=execution_result,
            action_triggered=action_triggered,
            action_type=action_type,
            timing=timing,
            error=None,
        )
    except Exception as exc:  # noqa: BLE001
        timing["total_ms"] = (time.perf_counter() - total_start) * 1000
        return AutomationResult(
            success=False,
            image_size=None,
            response=None,
            execution_result=None,
            action_triggered=False,
            action_type=None,
            timing=timing,
            error=str(exc),
        )
