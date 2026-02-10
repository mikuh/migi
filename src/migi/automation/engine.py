from __future__ import annotations

import asyncio
import ast
import base64
import contextlib
import importlib
import io
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable


COORDINATE_SCALE = 1000
SCREENSHOT_FORMAT = "jpeg"
SCREENSHOT_QUALITY = 85

DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_BASE_URL = "https://api.openai.com/v1"

MAX_PIXELS = 16384 * 28 * 28
MIN_PIXELS = 100 * 28 * 28
PIXELS_PER_SCROLL_CLICK = 15


_mss_instance: Any | None = None


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


def _encode_image_from_pil(image: Any, fmt: str = "jpeg", quality: int = 85) -> str:
    buffer = io.BytesIO()
    if fmt.lower() == "jpeg":
        if image.mode == "RGBA":
            image = image.convert("RGB")
        image.save(buffer, format="JPEG", quality=quality, optimize=True)
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
    base64_data = _encode_image_from_pil(screenshot, fmt, quality)
    return screenshot, width, height, base64_data


def build_conversation(instruction: str, base64_image: str, image_format: str = "jpeg") -> list[dict[str, Any]]:
    system_prompt = COMPUTER_USE_PROMPT.format(instruction=instruction)
    image_content = [
        {"type": "text", "text": "[Current Screenshot]"},
        {"type": "image_url", "image_url": {"url": f"data:image/{image_format};base64,{base64_image}"}},
    ]
    return [
        {"role": "user", "content": system_prompt},
        {"role": "user", "content": image_content},
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


async def _call_model_inference_async(
    deps: dict[str, Any],
    messages: list[dict[str, Any]],
    api_key: str,
    model_name: str,
    base_url: str,
) -> str:
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
    async with deps["httpx"].AsyncClient(timeout=120) as client:
        response = await client.post(endpoint, headers=headers, json=payload)
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


def call_model_inference(
    deps: dict[str, Any],
    messages: list[dict[str, Any]],
    api_key: str,
    model_name: str | None = None,
    base_url: str | None = None,
) -> str:
    if not api_key:
        raise ValueError("GUI_VISION_API_KEY is required. Run `migi setup` or set env variable.")
    model_name = model_name or DEFAULT_MODEL
    base_url = base_url or DEFAULT_BASE_URL
    return asyncio.run(
        _call_model_inference_async(
            deps=deps,
            messages=messages,
            api_key=api_key,
            model_name=model_name,
            base_url=base_url,
        )
    )


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
    scale_factor: int,
) -> tuple[int, int]:
    x = float(point[0])
    y = float(point[1])
    if abs(x) <= scale_factor and abs(y) <= scale_factor:
        return (
            round(x * image_width / scale_factor),
            round(y * image_height / scale_factor),
        )
    return (round(x), round(y))


def _box_to_screen_xy(
    box: list[Any] | tuple[Any, ...],
    image_width: int,
    image_height: int,
    scale_factor: int,
) -> tuple[int, int]:
    if len(box) == 4:
        x = float((box[0] + box[2]) / 2)
        y = float((box[1] + box[3]) / 2)
        return _point_to_screen_xy([x, y], image_width, image_height, scale_factor)
    return _point_to_screen_xy(box, image_width, image_height, scale_factor)


def execute_pyautogui_action(
    deps: dict[str, Any],
    responses: dict[str, Any] | list[dict[str, Any]],
    image_height: int,
    image_width: int,
    scale_factor: int = 1000,
) -> list[str]:
    pyautogui = deps["pyautogui"]
    pyperclip = deps["pyperclip"]

    if isinstance(responses, dict):
        responses = [responses]

    result_info: list[str] = []
    for index, response in enumerate(responses):
        if index > 0:
            time.sleep(0.3)
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
                time.sleep(0.2)
                if os.uname().sysname == "Darwin":
                    pyautogui.hotkey("command", "v", interval=0.1)
                else:
                    pyautogui.hotkey("ctrl", "v", interval=0.1)
                time.sleep(0.5)
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
                sx, sy = _box_to_screen_xy(start_box, image_width, image_height, scale_factor)
                ex, ey = _box_to_screen_xy(end_box, image_width, image_height, scale_factor)
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
                x, y = _box_to_screen_xy(start_box, image_width, image_height, scale_factor)
            screen_height = pyautogui.size()[1]
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
                x, y = _box_to_screen_xy(start_box, image_width, image_height, scale_factor)
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
            keys = str(key_combo).lower().strip().split()
            if keys:
                key_map = {
                    "ctrl": "ctrl",
                    "control": "ctrl",
                    "cmd": "command",
                    "command": "command",
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
                pyautogui.hotkey(*mapped, interval=0.1)
                result_info.append(f"hotkey:{'+'.join(mapped)}")
        elif action_type == "finished":
            result_info.append("finished")
            return result_info
        elif action_type == "wait":
            time.sleep(5)
            result_info.append("wait:5s")
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
            time.sleep(0.5)
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

