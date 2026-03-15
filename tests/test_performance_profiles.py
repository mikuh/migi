from __future__ import annotations

import base64
import importlib
import io
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from migi.automation.engine import (
    CaptureRegion,
    WeChatSendRequest,
    _extract_target_app_name,
    _extract_message_content,
    _extract_message_recipient,
    _extract_wechat_send_request,
    _instruction_allows_window_management_hotkeys,
    _is_launch_only_instruction,
    _point_to_screen_xy,
    _run_wechat_send_flow,
    build_conversation,
    execute_pyautogui_action,
    load_image_file,
    resolve_capture_mode,
    resolve_performance_tuning,
)


class PerformanceProfileTests(unittest.TestCase):
    def test_default_profile_is_balanced(self) -> None:
        tuning = resolve_performance_tuning()
        accurate = resolve_performance_tuning("accurate")

        self.assertEqual(tuning.name, "balanced")
        self.assertLess(tuning.screenshot_max_long_edge, accurate.screenshot_max_long_edge)
        self.assertLess(tuning.screenshot_quality, accurate.screenshot_quality)

    def test_fast_profile_is_more_aggressive(self) -> None:
        fast = resolve_performance_tuning("fast")
        balanced = resolve_performance_tuning("balanced")

        self.assertLess(fast.screenshot_max_long_edge, balanced.screenshot_max_long_edge)
        self.assertLess(fast.request_timeout_seconds, balanced.request_timeout_seconds)
        self.assertLess(fast.action_wait_duration, balanced.action_wait_duration)

    def test_invalid_profile_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported performance profile"):
            resolve_performance_tuning("turbo")


class ImageDownscaleTests(unittest.TestCase):
    def test_large_images_are_downscaled_before_encoding(self) -> None:
        try:
            image_module = importlib.import_module("PIL.Image")
        except ModuleNotFoundError as exc:
            self.skipTest(f"Pillow is not installed: {exc}")

        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "large.jpg"
            image = image_module.new("RGB", (4000, 3000), color="white")
            image.save(image_path, format="JPEG", quality=95)

            _, width, height, encoded = load_image_file(
                deps={"Image": image_module},
                image_path=image_path,
                quality=70,
                max_long_edge=1600,
            )

            self.assertEqual((width, height), (4000, 3000))
            decoded = base64.b64decode(encoded)
            with image_module.open(io.BytesIO(decoded)) as uploaded:
                self.assertEqual(max(uploaded.size), 1600)


class ConversationTests(unittest.TestCase):
    def test_conversation_includes_step_and_history(self) -> None:
        messages = build_conversation(
            instruction="测试任务",
            base64_image="ZmFrZQ==",
            action_history=["Step 1 execution result: click:10,10"],
            step_index=2,
        )

        self.assertEqual(len(messages), 2)
        content = messages[1]["content"]
        texts = [item["text"] for item in content if item["type"] == "text"]
        self.assertIn("[Step 2]", texts)
        self.assertTrue(any("Action History" in text for text in texts))

    def test_conversation_includes_recipient_hint(self) -> None:
        messages = build_conversation(
            instruction="给 geb 发送微信消息",
            base64_image="ZmFrZQ==",
            recipient_hint="geb",
        )

        texts = [item["text"] for item in messages[1]["content"] if item["type"] == "text"]
        self.assertIn("[Recipient]\ngeb", texts)


class CaptureModeTests(unittest.TestCase):
    def test_auto_capture_mode_prefers_window_for_non_launch_tasks(self) -> None:
        self.assertEqual(resolve_capture_mode("auto", app_launch_intent=False), "window")

    def test_auto_capture_mode_uses_screen_for_launch_tasks(self) -> None:
        self.assertEqual(resolve_capture_mode("auto", app_launch_intent=True), "screen")

    def test_window_capture_maps_to_absolute_screen_coordinates(self) -> None:
        region = CaptureRegion(left=100, top=200, width=400, height=300, source="window")
        x, y = _point_to_screen_xy(
            [500, 500],
            image_width=400,
            image_height=300,
            capture_region=region,
            screen_width=1600,
            screen_height=1200,
            scale_factor=1000,
        )

        self.assertEqual((x, y), (300, 350))


class AppInferenceTests(unittest.TestCase):
    def test_extract_target_app_name_from_wechat_send_instruction(self) -> None:
        self.assertEqual(_extract_target_app_name("给 geb 发送微信消息"), "WeChat")

    def test_extract_message_recipient(self) -> None:
        self.assertEqual(_extract_message_recipient("给 geb 发送微信消息，说 hi"), "geb")

    def test_extract_message_content(self) -> None:
        self.assertEqual(_extract_message_content("给 geb 发送微信消息，说 hi there"), "hi there")

    def test_extract_wechat_send_request(self) -> None:
        request = _extract_wechat_send_request("给 geb 发送微信消息，说 gui测试")
        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual(request.recipient, "geb")
        self.assertEqual(request.content, "gui测试")

    def test_launch_only_instruction_detection(self) -> None:
        self.assertTrue(_is_launch_only_instruction("打开微信"))
        self.assertFalse(_is_launch_only_instruction("打开微信后给 geb 发消息"))

    def test_instruction_allows_window_management_hotkeys(self) -> None:
        self.assertTrue(_instruction_allows_window_management_hotkeys("关闭当前窗口"))
        self.assertFalse(_instruction_allows_window_management_hotkeys("点击左侧公众号"))


class HotkeyGuardTests(unittest.TestCase):
    def test_close_window_hotkey_is_blocked_by_default(self) -> None:
        class FakePyAutoGUI:
            KEYBOARD_KEYS = {"command", "w"}

            def __init__(self) -> None:
                self.hotkey_calls: list[tuple[str, ...]] = []
                self.press_calls: list[str] = []

            def size(self) -> tuple[int, int]:
                return (1600, 1200)

            def hotkey(self, *keys: str, interval: float = 0.0) -> None:
                self.hotkey_calls.append(keys)

            def press(self, key: str) -> None:
                self.press_calls.append(key)

        class FakePyperclip:
            def copy(self, content: str) -> None:
                return None

        fake_pyautogui = FakePyAutoGUI()
        result = execute_pyautogui_action(
            deps={"pyautogui": fake_pyautogui, "pyperclip": FakePyperclip()},
            responses={"action_type": "hotkey", "action_inputs": {"key": "command w"}},
            image_height=600,
            image_width=800,
            capture_region=CaptureRegion(left=0, top=0, width=800, height=600, source="window"),
            tuning=resolve_performance_tuning("fast"),
        )

        self.assertEqual(result, ["hotkey_skipped:blocked=command+w"])
        self.assertEqual(fake_pyautogui.hotkey_calls, [])


class WeChatFlowTests(unittest.TestCase):
    def test_wechat_flow_skips_deterministic_stage_screenshots(self) -> None:
        execution_calls: list[list[dict[str, object]]] = []

        def fake_capture_screenshot(*args: object, **kwargs: object) -> tuple[object, int, int, str, CaptureRegion]:
            return (
                object(),
                1280,
                720,
                "ZmFrZQ==",
                CaptureRegion(left=0, top=0, width=1280, height=720, source="window"),
            )

        def fake_execute_pyautogui_action(**kwargs: object) -> list[str]:
            responses = kwargs["responses"]
            assert isinstance(responses, list)
            execution_calls.append(responses)
            return ["ok"]

        with (
            mock.patch(
                "migi.automation.engine._try_direct_app_launch",
                return_value=(True, ["direct_launch_ok:mac-open-a:WeChat"]),
            ),
            mock.patch(
                "migi.automation.engine.capture_screenshot",
                side_effect=fake_capture_screenshot,
            ) as capture_mock,
            mock.patch(
                "migi.automation.engine.call_model_inference",
                return_value="<action>finished(content='recipient_selected')</action>",
            ),
            mock.patch(
                "migi.automation.engine.parse_and_execute_action",
                return_value=(True, ["finished"], "finished"),
            ),
            mock.patch(
                "migi.automation.engine.execute_pyautogui_action",
                side_effect=fake_execute_pyautogui_action,
            ),
        ):
            result = _run_wechat_send_flow(
                deps={},
                request=WeChatSendRequest(recipient="geb", content="早上好"),
                api_key="test-api-key",
                model_name=None,
                base_url=None,
                action_parser="builtin",
                action_parser_callable=None,
                performance_profile="fast",
                max_steps=3,
            )

        self.assertTrue(result.success)
        self.assertEqual(capture_mock.call_count, 1)
        self.assertEqual(result.timing.get("wechat_search_screenshot_ms"), 0.0)
        self.assertEqual(result.timing.get("wechat_send_screenshot_ms"), 0.0)
        self.assertEqual(len(execution_calls), 2)
        self.assertEqual(
            [item.get("action_type") for item in execution_calls[0]],
            ["hotkey", "hotkey", "type", "hotkey"],
        )
        self.assertEqual(
            [item.get("action_type") for item in execution_calls[1]],
            ["type", "hotkey"],
        )


if __name__ == "__main__":
    unittest.main()
