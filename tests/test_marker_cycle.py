import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
import win32con

from pwa import AutoBridge, main
from message_store import MessageStore
from viber_window import ScreenRegion, ViberWindow


class DummyViber:
    def __init__(self):
        self.scroll_calls = 0

    def focus(self):
        pass

    def scroll(self, **_kwargs):
        self.scroll_calls += 1

    def click_in_messages(self, *_args, **_kwargs):
        pass


def marker_bridge(candidates):
    bridge = AutoBridge.__new__(AutoBridge)
    bridge.viber = DummyViber()
    bridge.marker_cycle_find_pages = 0
    bridge.marker_cycle_boundary_streak = 0
    bridge.marker_cycle_boundary_keys = []
    bridge.marker_cycle_boundary_y = None
    bridge.marker_cycle_sent_keys = set()
    bridge.marker_cycle_phase = "find_marker_up"
    bridge.marker_cycle_marker_y = None
    bridge.collect_visible_candidates = lambda: candidates
    bridge.save_debug_screenshot = lambda *_args: None
    bridge.get_candidate_message = lambda candidate: candidate["message"]
    bridge.message_content_key = lambda message: message["key"]
    bridge.message_is_sent_boundary = lambda message: message["sent"]
    bridge._marker_stop_after_sent_count = lambda: 3
    bridge._setting_int = lambda _name, default: default
    bridge.sent_store = Mock()
    bridge.send_pending_down_page = AsyncMock()
    return bridge


class MarkerBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_fortieth_page_starts_ordered_fallback_send_down(self):
        candidates = [
            {"y": 300, "signature": "pending", "message": {"key": "file:new", "sent": False}},
        ]
        bridge = marker_bridge(candidates)
        bridge.marker_cycle_find_pages = 39

        await bridge.find_sent_marker_up()

        self.assertEqual(bridge.marker_cycle_find_pages, 40)
        self.assertEqual(bridge.marker_cycle_phase, "send_down")
        self.assertIsNone(bridge.marker_cycle_marker_y)
        bridge.send_pending_down_page.assert_awaited_once()


class MessageStoreProtectionTests(unittest.IsolatedAsyncioTestCase):
    def test_confirmed_feed_boundary_survives_hourly_registry_expiry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = str(Path(temp_dir) / "sent.json")
            store = MessageStore(
                path=path,
                reset_interval_seconds=0,
                preserve_latest_count=3,
            )
            targets = ["one", "two"]
            boundary_keys = ["image:old", "text:middle", "text:new"]
            for key in boundary_keys + ["file:recent-send"]:
                for target in targets:
                    store.mark_delivered(key, target)
                store.mark_completed(key)

            store.set_protected_keys(boundary_keys)
            store.reset_interval_seconds = 1
            store.last_reset_at = 0

            self.assertTrue(store.clear_if_expired(now=10))
            self.assertEqual(set(store.deliveries), set(boundary_keys))
            self.assertTrue(store.delivered_to_all("image:old", targets))
            self.assertNotIn("file:recent-send", store.deliveries)

            reloaded = MessageStore(
                path=path,
                reset_interval_seconds=1,
                preserve_latest_count=3,
            )
            self.assertEqual(list(reloaded.protected_keys), boundary_keys)
            self.assertTrue(reloaded.delivered_to_all("image:old", targets))

    async def test_duplicate_content_key_counts_only_once(self):
        candidates = [
            {"y": 300, "signature": "one", "message": {"key": "file:same", "sent": True}},
            {"y": 200, "signature": "two", "message": {"key": "file:same", "sent": True}},
            {"y": 100, "signature": "three", "message": {"key": "file:same", "sent": True}},
        ]
        bridge = marker_bridge(candidates)

        await bridge.find_sent_marker_up()

        self.assertEqual(bridge.marker_cycle_boundary_streak, 1)
        self.assertEqual(bridge.marker_cycle_boundary_keys, ["file:same"])
        bridge.send_pending_down_page.assert_not_awaited()

    async def test_three_distinct_sent_messages_confirm_boundary(self):
        candidates = [
            {"y": 300, "signature": "one", "message": {"key": "file:a", "sent": True}},
            {"y": 200, "signature": "two", "message": {"key": "file:b", "sent": True}},
            {"y": 100, "signature": "three", "message": {"key": "text:c", "sent": True}},
        ]
        bridge = marker_bridge(candidates)

        await bridge.find_sent_marker_up()

        self.assertEqual(bridge.marker_cycle_phase, "send_down")
        self.assertEqual(bridge.marker_cycle_boundary_streak, 3)
        self.assertEqual(bridge.marker_cycle_sent_keys, {"file:a", "file:b", "text:c"})
        bridge.send_pending_down_page.assert_awaited_once()


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_main_stops_without_propagating_traceback(self):
        bridge = Mock()
        bridge.run = AsyncMock(side_effect=asyncio.CancelledError)

        with (
            patch("pwa.startTgClient", AsyncMock(return_value=(object(), "Viber1", None, []))),
            patch("pwa.load_config", return_value=(None, None, {})),
            patch("pwa.AutoBridge", return_value=bridge),
            patch("pwa.log_and_print") as log,
        ):
            await main()

        log.assert_called_once_with("[AutoBridge] Stopped by user.")

    def test_repeated_content_page_detects_bottom_despite_visual_changes(self):
        bridge = AutoBridge.__new__(AutoBridge)
        bridge.marker_cycle_last_down_content_signature = None
        bridge.marker_cycle_same_content_pages = 0
        bridge._bottom_same_content_pages = lambda: 2

        self.assertFalse(bridge.down_content_page_repeated({"file:a", "file:b"}))
        self.assertTrue(bridge.down_content_page_repeated({"file:b", "file:a"}))

    async def test_pending_messages_between_retained_markers_do_not_reset_boundary(self):
        candidates = [
            {"y": 500, "signature": "sent-new", "message": {"key": "file:a", "sent": True}},
            {"y": 400, "signature": "missed-new", "message": {"key": "file:x", "sent": False}},
            {"y": 300, "signature": "sent-middle", "message": {"key": "file:b", "sent": True}},
            {"y": 200, "signature": "missed-old", "message": {"key": "file:y", "sent": False}},
            {"y": 100, "signature": "sent-old", "message": {"key": "text:c", "sent": True}},
        ]
        bridge = marker_bridge(candidates)

        await bridge.find_sent_marker_up()

        self.assertEqual(bridge.marker_cycle_phase, "send_down")
        self.assertEqual(bridge.marker_cycle_boundary_keys, ["file:a", "file:b", "text:c"])
        self.assertEqual(bridge.marker_cycle_sent_keys, {"file:a", "file:b", "text:c"})
        self.assertEqual(bridge.marker_cycle_marker_y, 100)
        bridge.send_pending_down_page.assert_awaited_once()


class MediaResolutionTests(unittest.TestCase):
    def test_viber_video_placeholder_refines_generic_copy_type(self):
        bridge = AutoBridge.__new__(AutoBridge)

        placeholder_type = bridge.media_placeholder_type(
            "[ Wednesday, 15 July 2026 14:08 ] Anna HR: Видео"
        )

        self.assertEqual(placeholder_type, "video")

    def test_text_uses_direct_copy_even_when_select_is_present(self):
        bridge = AutoBridge.__new__(AutoBridge)
        bridge.settings = {}
        bridge.viber = DummyViber()
        bridge.save_debug_screenshot = lambda *_args: None
        bridge.screen_patch_hash = lambda *_args: "hash"
        bridge.menu_region_around = lambda *_args: (0, 0, 100, 100)
        bridge.read_context_menu = lambda *_args: {
            "isText": (1, 2, 3, 4),
            "isCopy": None,
            "isSelect": (5, 6, 7, 8),
        }
        bridge.click_menu_item = Mock()
        bridge.copy_selected_message = Mock(
            side_effect=AssertionError("text must not use Select")
        )

        candidate = {
            "x": 10,
            "y": 20,
            "type": "copy",
            "visual_hash": "hash",
            "key": "copy:test",
        }
        with (
            patch("pwa.cv2.waitKey", return_value=0),
            patch("pwa.pyperclip.copy"),
            patch("pwa.pyperclip.paste", return_value="direct text"),
        ):
            message = bridge.copy_candidate_after_delay(candidate)

        self.assertEqual(message["type"], "text")
        self.assertEqual(message["text"], "direct text")
        bridge.copy_selected_message.assert_not_called()
        bridge.click_menu_item.assert_called_once_with((0, 0, 100, 100), (1, 2, 3, 4))

    def test_generic_candidate_does_not_use_menu_position_guess(self):
        bridge = AutoBridge.__new__(AutoBridge)
        bridge.viber = DummyViber()
        bridge.settings = {"show_in_folder_fallback_rect": [1, 2, 3, 4]}
        bridge.save_debug_screenshot = lambda *_args: None
        bridge.menu_region_around = lambda *_args: (0, 0, 10, 10)
        bridge.read_context_menu = lambda *_args: {}
        bridge.close_context_menu = lambda *_args: None

        with patch("pwa.cv2.waitKey", return_value=0):
            paths = bridge.reveal_file_from_viber(
                {"x": 10, "y": 20, "type": "copy"},
                allow_fallback_rect=False,
            )

        self.assertEqual(paths, [])


class AnchorCandidateTests(unittest.TestCase):
    def test_enabled_anchor_mode_does_not_fall_back_to_grid_clicks(self):
        bridge = AutoBridge.__new__(AutoBridge)
        bridge.settings = {"auto_capture": {"reaction_anchor": {"enabled": True}}}
        bridge.viber = Mock()
        bridge.viber.messages_region.return_value = Mock()
        bridge.collect_anchor_candidates = Mock(return_value=[])
        bridge.inspect_candidate_at = Mock(
            side_effect=AssertionError("grid scan must not run in anchor mode")
        )
        bridge.write_debug_json = lambda *_args: None

        candidates = bridge.collect_visible_candidates()

        self.assertEqual(candidates, [])
        bridge.inspect_candidate_at.assert_not_called()


class ScrollBottomTests(unittest.TestCase):
    def test_scroll_bottom_retries_after_closing_a_context_menu(self):
        window = ViberWindow.__new__(ViberWindow)
        window.settings = {
            "auto_capture": {
                "scroll_bottom_anchor": {
                    "retry_count": 3,
                    "retry_wait_ms": 0,
                    "wheel_fallback_enabled": False,
                }
            }
        }
        window.click_scroll_to_bottom = Mock(side_effect=[False, True])
        window.scroll = Mock()

        with (
            patch("viber_window.win32api.keybd_event") as keybd_event,
            patch("viber_window.time.sleep"),
        ):
            clicked = window.scroll_to_bottom(amount=3)

        self.assertTrue(clicked)
        self.assertEqual(window.click_scroll_to_bottom.call_count, 2)
        self.assertEqual(keybd_event.call_count, 2)
        window.scroll.assert_not_called()

    def test_stuck_selection_mode_is_closed_with_escape(self):
        window = ViberWindow.__new__(ViberWindow)
        window.settings = {
            "auto_capture": {
                "selection_mode_anchor": {
                    "enabled": True,
                    "template_path": "images/viber_selection_mode_anchor.png",
                    "threshold": 0.88,
                    "left_offset_px": 300,
                    "search_width_px": 220,
                    "search_height_px": 70,
                    "escape_wait_ms": 0,
                }
            }
        }
        window.focus = Mock()
        window.rect = lambda: ScreenRegion(0, 0, 960, 1032)

        with (
            patch("viber_window.ImageGrab.grab", return_value=Mock()),
            patch("viber_window.np.array", return_value=Mock()),
            patch("viber_window.cv2.cvtColor", return_value=Mock(shape=(70, 220))),
            patch("viber_window.cv2.imread", return_value=Mock(shape=(24, 64))),
            patch("viber_window.cv2.matchTemplate", return_value=Mock()),
            patch("viber_window.cv2.minMaxLoc", return_value=(0, 0.99, (0, 0), (10, 10))),
            patch("viber_window.win32api.keybd_event") as keybd_event,
            patch("viber_window.time.sleep"),
        ):
            recovered = window.exit_selection_mode_if_open()

        self.assertTrue(recovered)
        self.assertEqual(keybd_event.call_count, 2)
        self.assertEqual(keybd_event.call_args_list[0].args, (win32con.VK_ESCAPE, 0, 0, 0))

    def test_media_viewer_close_link_is_clicked_in_top_right_search_area(self):
        window = ViberWindow.__new__(ViberWindow)
        window.settings = {
            "auto_capture": {
                "media_viewer_close_anchor": {
                    "enabled": True,
                    "template_path": "images/viber_media_viewer_close.png",
                    "threshold": 0.88,
                    "search_width_px": 140,
                    "search_height_px": 90,
                    "click_wait_ms": 0,
                }
            }
        }
        window.rect = lambda: ScreenRegion(0, 0, 960, 1032)

        with (
            patch("viber_window.ImageGrab.grab", return_value=Mock()),
            patch("viber_window.np.array", return_value=Mock()),
            patch("viber_window.cv2.cvtColor", return_value=Mock(shape=(90, 140))),
            patch("viber_window.cv2.imread", return_value=Mock(shape=(24, 62))),
            patch("viber_window.cv2.matchTemplate", return_value=Mock()),
            patch("viber_window.cv2.minMaxLoc", return_value=(0, 0.99, (0, 0), (50, 52))),
            patch("viber_window.mouse.click") as click,
            patch("viber_window.time.sleep"),
        ):
            closed = window.close_media_viewer_if_open()

        self.assertTrue(closed)
        click.assert_called_once_with(button="left", coords=(901, 64))

    def test_absent_button_does_not_move_wheel_when_fallback_disabled(self):
        window = ViberWindow.__new__(ViberWindow)
        window.settings = {
            "auto_capture": {
                "scroll_bottom_anchor": {"wheel_fallback_enabled": False}
            }
        }
        window.click_scroll_to_bottom = Mock(return_value=False)
        window.scroll = Mock()

        clicked = window.scroll_to_bottom(amount=3)

        self.assertFalse(clicked)
        window.scroll.assert_not_called()

    def test_scroll_bottom_clicks_detected_button_without_wheel(self):
        window = ViberWindow.__new__(ViberWindow)
        window.settings = {
            "auto_capture": {
                "scroll_bottom_anchor": {
                    "enabled": True,
                    "template_path": "images/viber_scroll_bottom_anchor.png",
                    "threshold": 0.86,
                    "search_width_px": 120,
                    "search_height_px": 150,
                    "click_wait_ms": 0,
                }
            }
        }
        window.focus = Mock()
        window.rect = lambda: ScreenRegion(0, 0, 960, 1032)
        window.scroll = Mock()

        with (
            patch("viber_window.ImageGrab.grab", return_value=Mock()),
            patch("viber_window.np.array", return_value=Mock()),
            patch("viber_window.cv2.cvtColor", return_value=Mock(shape=(150, 120))),
            patch("viber_window.cv2.imread", return_value=Mock(shape=(52, 52))),
            patch("viber_window.cv2.matchTemplate", return_value=Mock()),
            patch("viber_window.cv2.minMaxLoc", return_value=(0, 0.95, (0, 0), (49, 78))),
            patch("viber_window.mouse.click") as click,
            patch("viber_window.time.sleep"),
        ):
            clicked = window.scroll_to_bottom(amount=3)

        self.assertTrue(clicked)
        click.assert_called_once_with(button="left", coords=(915, 986))
        window.scroll.assert_not_called()


class MediaGroupTests(unittest.TestCase):
    def test_media_group_reads_each_configured_tile_once(self):
        bridge = AutoBridge.__new__(AutoBridge)
        bridge.settings = {
            "auto_capture": {
                "media_group_anchor": {
                    "minimum_items": 2,
                    "tile_offsets_px": [[-20, -10], [20, -10], [-20, 10], [20, 10]],
                }
            }
        }
        bridge.screen_patch_hash = lambda x, y: f"hash:{x}:{y}"
        bridge._setting_int = lambda _name, default: default
        bridge.message_content_key = AutoBridge.message_content_key.__get__(bridge)
        bridge.clear_clipboard = Mock()
        points = []

        def read_tile(candidate):
            points.append((candidate["x"], candidate["y"]))
            index = len(points)
            if index % 2:
                return {"type": "image", "image_hash": f"image-{index}"}
            return {"type": "file", "file_hash": f"file-{index}"}

        bridge.copy_candidate_after_delay = read_tile
        with patch("pwa.pyautogui.press"), patch("pwa.cv2.waitKey", return_value=0):
            message = bridge.copy_media_group({"x": 100, "y": 200})

        self.assertEqual(points, [(80, 190), (120, 190), (80, 210), (120, 210)])
        self.assertEqual(message["type"], "media_group")
        self.assertEqual(len(message["items"]), 4)
        self.assertTrue(bridge.message_content_key(message).startswith("media_group:"))
        self.assertEqual(bridge.clear_clipboard.call_count, 4)

    def test_media_group_with_duplicate_clipboard_content_is_not_returned(self):
        bridge = AutoBridge.__new__(AutoBridge)
        bridge.settings = {
            "auto_capture": {
                "media_group_anchor": {
                    "minimum_items": 2,
                    "tile_offsets_px": [[-20, 0], [20, 0]],
                }
            }
        }
        bridge.screen_patch_hash = lambda x, y: f"hash:{x}:{y}"
        bridge._setting_int = lambda _name, default: default
        bridge.message_content_key = AutoBridge.message_content_key.__get__(bridge)
        bridge.clear_clipboard = Mock()
        bridge.copy_candidate_after_delay = Mock(
            return_value={"type": "file", "file_hash": "same-video"}
        )

        with patch("pwa.pyautogui.press"), patch("pwa.cv2.waitKey", return_value=0):
            message = bridge.copy_media_group({"x": 100, "y": 200})

        self.assertIsNone(message)
        self.assertEqual(bridge.copy_candidate_after_delay.call_count, 2)


class MediaGroupDeliveryBridge(AutoBridge):
    def __init__(self):
        pass

    async def send_one_message_to_target(self, message, channel_name):
        if message["type"] == "media_group":
            return await super().send_one_message_to_target(message, channel_name)
        self.sent_items.append((message, channel_name))
        return True


class MediaGroupDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_group_items_are_sent_only_once(self):
        bridge = MediaGroupDeliveryBridge()
        bridge.sent_store = Mock()
        bridge.seen_store = Mock()
        bridge.sent_items = []
        bridge.message_content_key = AutoBridge.message_content_key.__get__(bridge)
        bridge.sent_store.has_delivery.return_value = False
        duplicate = {"type": "file", "file_hash": "same-video"}
        message = {"type": "media_group", "items": [duplicate, duplicate, duplicate]}

        sent = await bridge.send_one_message_to_target(message, "target")

        self.assertTrue(sent)
        self.assertEqual(bridge.sent_items, [(duplicate, "target")])
        bridge.sent_store.mark_delivered.assert_called_once()

    async def test_retry_skips_only_item_already_sent_from_same_group(self):
        bridge = MediaGroupDeliveryBridge()
        bridge.sent_store = Mock()
        bridge.seen_store = Mock()
        bridge.sent_items = []
        bridge.message_content_key = AutoBridge.message_content_key.__get__(bridge)
        bridge.sent_store.has_delivery.side_effect = (
            lambda key, _target: ":item:1:" in key
        )
        message = {
            "type": "media_group",
            "items": [
                {"type": "image", "image_hash": "same-as-an-old-standalone-image"},
                {"type": "file", "file_hash": "new-video"},
            ],
        }

        sent = await bridge.send_one_message_to_target(message, "target")

        self.assertTrue(sent)
        self.assertEqual(bridge.sent_items, [(message["items"][1], "target")])
        marked_key = bridge.sent_store.mark_delivered.call_args.args[0]
        self.assertIn(":item:2:file:new-video", marked_key)


if __name__ == "__main__":
    unittest.main()
