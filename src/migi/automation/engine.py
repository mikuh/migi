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
SCREENSHOT_QUALITY = 80

DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_BASE_URL = "https://api.openai.com/v1"

MAX_PIXELS = 16384 * 28 * 28
MIN_PIXELS = 100 * 28 * 28
PIXELS_PER_SCROLL_CLICK = 15

SCREENSHOT_MAX_LONG_EDGE = 1920
ACTION_INTER_STEP_DELAY = 0.08
ACTION_CLIPBOARD_SYNC_DELAY = 0.05
ACTION_PASTE_SETTLE_DELAY = 0.12
ACTION_PRE_EXEC_DELAY = 0.08
ACTION_HOTKEY_INTERVAL = 0.05
ACTION_WAIT_DURATION = 2.0

_mss_instance: Any | None = None
_httpx_client: Any | None = None


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
- Runtime may have already attempted command-based launch (e.g., macOS `open`, Windows `Start-Process`) before this step.
- If user asks to open/launch an app and its icon is not visible, use app search instead of random clicking.
- Never rely on shell checks like `which`, `where`, or `Get-Command` for GUI app availability.
- Current OS: {os_name}; search entry: {search_entry}; preferred app-search hotkey: {search_hotkey}.
- Shortcut-first policy for app launch (macOS/Windows): try app-search hotkey first; if hotkey fails/unavailable, fallback to GUI-visible search controls.
- App-search sequence (STRICT):
  1) <action>hotkey(key='{search_hotkey}')</action>
  2) <action>type(content='AppName')</action>
  3) Confirm an application result is visible (e.g. in "Applications/应用程序") and target exactly matches app name.
  4) Only then open it (click that app result or navigate to it, then Enter).
  5) If top result is not an application (e.g. message/contact/document), do NOT press Enter on it.
- If the target is still uncertain, use wait() or finished(content='target not visible') instead of clicking.

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


def _downscale_if_needed(image: Any, max_long_edge: int = SCREENSHOT_MAX_LONG_EDGE) -> Any:
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


def capture_screenshot(
    deps: dict[str, Any],
    quality: int = SCREENSHOT_QUALITY,
    fmt: str = SCREENSHOT_FORMAT,
) -> tuple[Any, int, int, str]:
    sct = _get_mss_instance(deps["mss"])
    monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
    sct_img = sct.grab(monitor)
    screenshot = deps["Image"].frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
    width, height = screenshot.size
    encoded = _downscale_if_needed(screenshot)
    base64_data = _encode_image_from_pil(encoded, fmt, quality)
    return screenshot, width, height, base64_data


def load_image_file(
    deps: dict[str, Any],
    image_path: str | Path,
    quality: int = SCREENSHOT_QUALITY,
    fmt: str = SCREENSHOT_FORMAT,
) -> tuple[Any, int, int, str]:
    path = Path(image_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Image file not found: {path}")
    if not path.is_file():
        raise ValueError(f"Image path is not a file: {path}")

    with deps["Image"].open(path) as image:
        loaded = image.copy()
    width, height = loaded.size
    base64_data = _encode_image_from_pil(loaded, fmt, quality)
    return loaded, width, height, base64_data


def _platform_search_hint() -> tuple[str, str, str]:
    system = platform.system().lower()
    if system == "darwin":
        return "macOS", "Spotlight", "command space"
    if system == "windows":
        return "Windows", "Start menu search", "win s"
    return "Linux", "desktop app search", "ctrl space"


def build_conversation(instruction: str, base64_image: str, image_format: str = "jpeg") -> list[dict[str, Any]]:
    os_name, search_entry, search_hotkey = _platform_search_hint()
    system_prompt = COMPUTER_USE_PROMPT.format(
        instruction=instruction,
        os_name=os_name,
        search_entry=search_entry,
        search_hotkey=search_hotkey,
    )
    image_content = [
        {"type": "text", "text": "[Current Screenshot]"},
        {"type": "image_url", "image_url": {"url": f"data:image/{image_format};base64,{base64_image}"}},
    ]
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


def _get_httpx_client(deps: dict[str, Any]) -> Any:
    global _httpx_client
    if _httpx_client is None:
        _httpx_client = deps["httpx"].Client(timeout=120)
    return _httpx_client


def call_model_inference(
    deps: dict[str, Any],
    messages: list[dict[str, Any]],
    api_key: str,
    model_name: str | None = None,
    base_url: str | None = None,
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
    client = _get_httpx_client(deps)
    response = client.post(endpoint, headers=headers, json=payload)
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
        return _clamp((x * screen_width, y * screen_height))

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
                x * screen_width / scale_factor,
                y * screen_height / scale_factor,
            )
        )

    # Treat as absolute screenshot pixels and remap to pyautogui coordinate space.
    mapped_x = x * screen_width / image_width if image_width > 0 else x
    mapped_y = y * screen_height / image_height if image_height > 0 else y
    return _clamp((mapped_x, mapped_y))


def _box_to_screen_xy(
    box: list[Any] | tuple[Any, ...],
    image_width: int,
    image_height: int,
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
            screen_width,
            screen_height,
            scale_factor,
        )
    return _point_to_screen_xy(
        box,
        image_width,
        image_height,
        screen_width,
        screen_height,
        scale_factor,
    )


def execute_pyautogui_action(
    deps: dict[str, Any],
    responses: dict[str, Any] | list[dict[str, Any]],
    image_height: int,
    image_width: int,
    scale_factor: int = 1000,
) -> list[str]:
    pyautogui = deps["pyautogui"]
    pyperclip = deps["pyperclip"]
    screen_width, screen_height = pyautogui.size()

    if isinstance(responses, dict):
        responses = [responses]

    result_info: list[str] = []
    for index, response in enumerate(responses):
        if index > 0:
            time.sleep(ACTION_INTER_STEP_DELAY)
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
                time.sleep(ACTION_CLIPBOARD_SYNC_DELAY)
                if platform.system() == "Darwin":
                    pyautogui.hotkey("command", "v", interval=ACTION_HOTKEY_INTERVAL)
                else:
                    pyautogui.hotkey("ctrl", "v", interval=ACTION_HOTKEY_INTERVAL)
                time.sleep(ACTION_PASTE_SETTLE_DELAY)
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
                    screen_width,
                    screen_height,
                    scale_factor,
                )
                ex, ey = _box_to_screen_xy(
                    end_box,
                    image_width,
                    image_height,
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
                    if len(mapped) == 1:
                        pyautogui.press(mapped[0])
                    else:
                        pyautogui.hotkey(*mapped, interval=ACTION_HOTKEY_INTERVAL)
                    result_info.append(f"hotkey:{'+'.join(mapped)}")
                except Exception as exc:  # noqa: BLE001
                    result_info.append(f"hotkey_failed:{type(exc).__name__}")
        elif action_type == "finished":
            result_info.append("finished")
            return result_info
        elif action_type == "wait":
            time.sleep(ACTION_WAIT_DURATION)
            result_info.append(f"wait:{ACTION_WAIT_DURATION}s")
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
    return cleaned or None


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
    aliases: dict[str, list[str]] = {
        "wechat": ["WeChat", "微信"],
        "微信": ["WeChat", "微信"],
    }
    candidates = [base]
    candidates.extend(aliases.get(key, []))
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


def _try_direct_app_launch(instruction: str) -> tuple[bool, list[str]]:
    app_name = _extract_app_name_from_instruction(instruction)
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
    action_parser: str = "builtin",
    action_parser_callable: str | None = None,
    scale_factor: int = 1000,
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
    result = execute_pyautogui_action(deps, parsed_all, img_height, img_width, scale_factor)
    return True, result, first_type


def auto_image_understanding(
    instruction: str,
    image_path: str | Path,
    api_key: str,
    model_name: str | None = None,
    base_url: str | None = None,
) -> AutomationResult:
    timing: dict[str, float] = {}
    total_start = time.perf_counter()
    try:
        deps = _import_vision_dependencies()

        step = time.perf_counter()
        _, img_width, img_height, base64_image = load_image_file(deps, image_path)
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
) -> AutomationResult:
    timing: dict[str, float] = {}
    total_start = time.perf_counter()
    try:
        deps = _import_gui_dependencies()
        app_launch_intent = _looks_like_app_launch_instruction(instruction)
        pre_execution_steps: list[str] = []

        if execute_action and app_launch_intent:
            step = time.perf_counter()
            direct_ok, direct_steps = _try_direct_app_launch(instruction)
            timing["direct_launch_ms"] = (time.perf_counter() - step) * 1000
            pre_execution_steps.extend(direct_steps)
            if direct_ok:
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

        step = time.perf_counter()
        _, img_width, img_height, base64_image = capture_screenshot(deps)
        timing["screenshot_ms"] = (time.perf_counter() - step) * 1000

        step = time.perf_counter()
        messages = build_conversation(instruction, base64_image, SCREENSHOT_FORMAT)
        timing["build_conv_ms"] = (time.perf_counter() - step) * 1000

        step = time.perf_counter()
        response = call_model_inference(deps, messages, api_key=api_key, model_name=model_name, base_url=base_url)
        timing["inference_ms"] = (time.perf_counter() - step) * 1000

        execution_result: list[str] | None = None
        action_triggered = False
        action_type: str | None = None

        if execute_action:
            step = time.perf_counter()
            time.sleep(ACTION_PRE_EXEC_DELAY)
            action_triggered, execution_result, action_type = parse_and_execute_action(
                deps=deps,
                response=response,
                img_height=img_height,
                img_width=img_width,
                action_parser=action_parser,
                action_parser_callable=action_parser_callable,
                scale_factor=COORDINATE_SCALE,
            )
            timing["execution_ms"] = (time.perf_counter() - step) * 1000
            if pre_execution_steps:
                execution_result = [*pre_execution_steps, *(execution_result or [])]

            # For app launch intents: if shortcut execution failed, retry once with GUI fallback guidance.
            if app_launch_intent and _has_hotkey_failure(execution_result):
                step = time.perf_counter()
                _, fb_img_width, fb_img_height, fb_base64_image = capture_screenshot(deps)
                timing["fallback_screenshot_ms"] = (time.perf_counter() - step) * 1000

                step = time.perf_counter()
                fallback_messages = build_conversation(
                    _build_app_launch_fallback_instruction(instruction),
                    fb_base64_image,
                    SCREENSHOT_FORMAT,
                )
                timing["fallback_build_conv_ms"] = (time.perf_counter() - step) * 1000

                step = time.perf_counter()
                fallback_response = call_model_inference(
                    deps,
                    fallback_messages,
                    api_key=api_key,
                    model_name=model_name,
                    base_url=base_url,
                )
                timing["fallback_inference_ms"] = (time.perf_counter() - step) * 1000

                step = time.perf_counter()
                time.sleep(ACTION_PRE_EXEC_DELAY)
                fb_triggered, fb_result, fb_type = parse_and_execute_action(
                    deps=deps,
                    response=fallback_response,
                    img_height=fb_img_height,
                    img_width=fb_img_width,
                    action_parser=action_parser,
                    action_parser_callable=action_parser_callable,
                    scale_factor=COORDINATE_SCALE,
                )
                timing["fallback_execution_ms"] = (time.perf_counter() - step) * 1000

                if fb_triggered:
                    action_triggered = True
                    if execution_result is None:
                        execution_result = []
                    execution_result.extend(fb_result)
                    if action_type in {None, "hotkey"}:
                        action_type = fb_type
                response = f"{response}\n\n# Fallback pass\n{fallback_response}"

        timing["total_ms"] = (time.perf_counter() - total_start) * 1000
        return AutomationResult(
            success=True,
            image_size=(img_width, img_height),
            response=response,
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
