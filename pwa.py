import asyncio
import hashlib
import json
import time
import traceback
from datetime import datetime
from io import BytesIO
from pathlib import Path

import cv2
import pyautogui
import pyperclip
from PIL import Image, ImageGrab

from init import init as load_config
from log import log_and_print
from message_store import MessageStore, normalize_text
from recognize_text import capture_and_find_multiple_text_coordinates
from tg import startTgClient
from vb_utils import process_one_message, reformat_telegram_text
from viber_window import ViberWindow


class AutoBridge:
    def __init__(self, bot_client, name_viber, channel_names, settings):
        self.bot_client = bot_client
        self.name_viber = name_viber
        self.channel_names = channel_names
        self.settings = settings
        self.sent_store = MessageStore(
            path=settings.get("sent_messages_file", "sent_messages.json"),
            max_items=int(settings.get("sent_messages_limit", 1000)),
        )
        self.seen_store = MessageStore(
            path=settings.get("seen_messages_file", "seen_messages.json"),
            max_items=int(settings.get("seen_messages_limit", settings.get("sent_messages_limit", 1000))),
        )
        self.viber = ViberWindow(settings)
        self.pending_candidates = {}
        self.known_candidate_signatures = set()
        self.baseline_ready = False
        self.debug_dir = Path(settings.get("debug_screenshot_dir", "runtime_debug"))
        self.debug_screenshots = bool(settings.get("debug_screenshots_enabled", True))
        self.shot_index = 0

    async def run(self):
        self.viber.connect()
        if not self.viber.ensure_channel():
            raise RuntimeError("Cannot find or confirm the configured Viber channel.")

        self.viber.scroll_to_bottom(self._setting_int("scroll_to_bottom_count", 3))
        log_and_print(
            "[AutoBridge] Started. "
            f"check_interval={self._check_interval()}s, read_delay={self._read_delay()}s"
        )

        while True:
            try:
                await self.process_visible_candidates()
            except Exception as exc:
                log_and_print(f"[AutoBridge] Read loop error: {exc}", "error")
                log_and_print(traceback.format_exc(), "error")
            await asyncio.sleep(self._check_interval())

    async def process_visible_candidates(self):
        self.viber.focus()
        self.viber.scroll_to_bottom(self._setting_int("scroll_to_bottom_count", 2))

        visible = self.collect_visible_candidates()
        self.save_debug_screenshot("visible_scan")
        if not self.baseline_ready:
            self.known_candidate_signatures = {candidate["signature"] for candidate in visible}
            self.baseline_ready = True
            log_and_print(
                "[AutoBridge] Baseline captured; existing visible messages will not be sent: "
                f"{len(self.known_candidate_signatures)}"
            )
            return

        if not visible:
            self.pending_candidates.clear()
            log_and_print("[AutoBridge] No visible message candidates.")
            return

        now = time.monotonic()
        visible_signatures = {candidate["signature"] for candidate in visible}

        for signature in list(self.pending_candidates):
            if signature not in visible_signatures:
                pending = self.pending_candidates[signature]
                if self.find_near_candidate(pending, visible):
                    pending["misses"] = 0
                    continue

                pending["misses"] = pending.get("misses", 0) + 1
                if pending["misses"] > self._candidate_missing_tolerance():
                    log_and_print(f"[AutoBridge] Candidate disappeared before read, skipping: {signature}")
                    self.pending_candidates.pop(signature, None)
                else:
                    log_and_print(
                        f"[AutoBridge] Candidate temporarily missing before read: "
                        f"{signature}, misses={pending['misses']}/{self._candidate_missing_tolerance()}"
                    )

        ready = []
        for candidate in visible:
            signature = candidate["signature"]
            if signature in self.known_candidate_signatures:
                continue

            pending_key = signature
            pending = self.pending_candidates.get(signature)
            if pending is None:
                near_pending_key = self.find_pending_key_for_candidate(candidate)
                if near_pending_key:
                    pending_key = near_pending_key
                    pending = self.pending_candidates[near_pending_key]

            if pending is None:
                candidate["first_seen"] = now
                candidate["misses"] = 0
                candidate["_pending_key"] = signature
                self.pending_candidates[signature] = candidate
                log_and_print(
                    f"[AutoBridge] New candidate queued for delayed read: signature={signature}, "
                    f"type={candidate['type']}, delay={self._read_delay()}s"
                )
                continue

            pending["misses"] = 0
            first_seen = pending["first_seen"]
            pending.update(candidate)
            pending["first_seen"] = first_seen
            pending["_pending_key"] = pending_key
            if now - pending["first_seen"] >= self._read_delay():
                ready.append(pending)

        for candidate in ready:
            signature = candidate["signature"]
            pending_key = candidate.get("_pending_key", signature)
            confirmed = self.find_visible_candidate_for_pending(candidate)
            if not confirmed:
                log_and_print(
                    f"[AutoBridge] Delayed candidate was not found on second scan, skipping: "
                    f"{signature}",
                    "warning",
                )
                self.pending_candidates.pop(pending_key, None)
                continue

            log_and_print(
                f"[AutoBridge] Delayed candidate confirmed on second scan: {confirmed['signature']}"
            )
            message = self.copy_candidate_after_delay(confirmed)
            self.pending_candidates.pop(pending_key, None)

            if not message:
                continue

            if message["type"] == "text" and self.sent_store.has_text(message["text"]):
                log_and_print("[AutoBridge] Text already sent before, skipping duplicate.")
                self.known_candidate_signatures.add(signature)
            elif message["type"] == "text":
                if await self.send_text(message["text"]):
                    self.sent_store.mark_text(message["text"])
                    self.seen_store.mark_text(message["text"])
                    self.known_candidate_signatures.add(signature)
            elif message["type"] == "image" and self.sent_store.has_image(message["image_hash"]):
                log_and_print("[AutoBridge] Image already sent before, skipping duplicate.")
                self.known_candidate_signatures.add(signature)
            elif message["type"] == "image":
                if await self.send_image(message["image"], message["image_hash"]):
                    self.sent_store.mark_image(message["image_hash"])
                    self.seen_store.mark_image(message["image_hash"])
                    self.known_candidate_signatures.add(signature)
            elif message["type"] == "video":
                log_and_print(
                    "[AutoBridge] Video was detected by OCR, but automatic safe video extraction "
                    "is disabled. Enable video_save_enabled only after a controlled test.",
                    "warning",
                )
                self.known_candidate_signatures.add(signature)

    def collect_visible_candidates(self):
        region = self.viber.messages_region()
        capture = self.settings.get("auto_capture", {})
        step = int(capture.get("scan_step_px", 130))
        x_ratios = capture.get("message_click_x_ratios", [capture.get("message_click_x_ratio", 0.22)])
        y_min = region.top + int(capture.get("scan_top_skip_px", 18))
        y_max = region.bottom - int(capture.get("scan_bottom_skip_px", 12))

        candidates = []
        seen = set()

        for y in range(y_max, y_min, -step):
            for x_ratio in x_ratios:
                x = region.left + int(region.width * float(x_ratio))
                candidate = self.inspect_candidate_at(x, y)
                if not candidate:
                    continue
                if candidate["key"] in seen:
                    continue
                seen.add(candidate["key"])
                candidates.append(candidate)
                break

        candidates.reverse()
        log_and_print(f"[AutoBridge] Visible candidates found: {len(candidates)}")
        self.write_debug_json("visible_candidates", candidates)
        return candidates

    def find_visible_candidate_for_pending(self, pending):
        self.viber.focus()
        self.viber.scroll_to_bottom(self._setting_int("scroll_to_bottom_count", 2))
        candidates = self.collect_visible_candidates()
        self.save_debug_screenshot("second_scan")
        for candidate in candidates:
            if candidate["signature"] == pending["signature"]:
                return candidate

        near = self.find_near_candidate(pending, candidates)
        if near:
            log_and_print(
                f"[AutoBridge] Exact signature changed; using nearby same-type candidate. "
                f"old={pending['signature']}, new={near['signature']}"
            )
        return near

    def find_pending_key_for_candidate(self, candidate):
        for pending_key, pending in self.pending_candidates.items():
            if self.candidates_are_near(pending, candidate):
                return pending_key
        return None

    def find_near_candidate(self, candidate, candidates):
        same_type = [
            item for item in candidates
            if self.candidates_are_near(candidate, item)
        ]
        if not same_type:
            return None
        return min(
            same_type,
            key=lambda item: abs(item["x"] - candidate["x"]) + abs(item["y"] - candidate["y"]),
        )

    def candidates_are_near(self, first, second):
        if first["type"] != second["type"]:
            return False
        tolerance = self._candidate_match_tolerance()
        return (
            abs(int(first["x"]) - int(second["x"])) <= tolerance
            and abs(int(first["y"]) - int(second["y"])) <= tolerance
        )

    def inspect_candidate_at(self, x, y):
        visual_hash = self.screen_patch_hash(x, y)

        self.viber.click_in_messages(x, y, button="right")
        cv2.waitKey(self._setting_int("context_menu_open_delay_ms", 350))

        menu_region = self.menu_region_around(x, y)
        menu_items = self.read_context_menu(menu_region)
        action = self.choose_menu_action(menu_items)
        self.close_context_menu(x, y)

        if not action:
            log_and_print(f"[AutoBridge] No actionable OCR menu at x={x}, y={y}; items={menu_items}")
            return None

        candidate = {
            "type": action,
            "x": int(x),
            "y": int(y),
            "visual_hash": visual_hash,
            "signature": f"{action}:{visual_hash}",
            "key": f"{action}:{int(x)}:{int(y)}:{visual_hash}",
        }
        log_and_print(f"[AutoBridge] Candidate detected: {candidate}")
        return candidate

    def copy_candidate_after_delay(self, candidate):
        x = candidate["x"]
        y = candidate["y"]
        expected_type = candidate["type"]

        self.save_debug_screenshot("before_delayed_copy")
        current_hash = self.screen_patch_hash(x, y)
        if current_hash != candidate["visual_hash"]:
            log_and_print(
                f"[AutoBridge] Candidate patch changed before read; continuing to OCR menu recheck. "
                f"key={candidate['key']}, old_hash={candidate['visual_hash']}, new_hash={current_hash}",
                "warning",
            )

        pyperclip.copy("")
        self.viber.click_in_messages(x, y, button="right")
        cv2.waitKey(self._setting_int("context_menu_open_delay_ms", 350))
        self.save_debug_screenshot("delayed_context_menu")

        menu_region = self.menu_region_around(x, y)
        menu_items = self.read_context_menu(menu_region)
        action = self.choose_menu_action(menu_items)
        log_and_print(
            "[AutoBridge] Menu rechecked before clipboard read: "
            f"expected={expected_type}, actual={action}, items={menu_items}"
        )

        if action != expected_type:
            self.close_context_menu(x, y)
            return None

        if action == "video" and not self.settings.get("video_save_enabled", False):
            self.close_context_menu(x, y)
            return {"type": "video", "key": candidate["key"]}

        menu_key = self.menu_key_for_action(action)
        self.click_menu_item(menu_region, menu_items[menu_key])
        cv2.waitKey(self._setting_int("clipboard_wait_ms", 700))
        self.save_debug_screenshot("after_delayed_copy")

        if action == "text":
            text = normalize_text(reformat_telegram_text(pyperclip.paste()))
            if text:
                log_and_print(f"[AutoBridge] Clipboard text copied: chars={len(text)}")
                return {"type": "text", "text": text, "key": f"text:{text}"}

        if action == "image":
            image = ImageGrab.grabclipboard()
            if isinstance(image, Image.Image):
                image_hash = hash_image(image)
                log_and_print(f"[AutoBridge] Clipboard image copied: size={image.size}, hash={image_hash}")
                return {
                    "type": "image",
                    "image": image,
                    "image_hash": image_hash,
                    "key": f"image:{image_hash}",
                }

        log_and_print(f"[AutoBridge] Clipboard did not contain expected data for action={action}.", "warning")
        return None

    def save_debug_screenshot(self, label):
        if not self.debug_screenshots:
            return None
        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            self.shot_index += 1
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = self.debug_dir / f"{self.shot_index:04d}_{stamp}_{label}.png"
            ImageGrab.grab().save(path)
            log_and_print(f"[AutoBridge] Debug screenshot saved: {path}")
            return str(path)
        except Exception as exc:
            log_and_print(f"[AutoBridge] Failed to save debug screenshot {label}: {exc}", "warning")
            return None

    def write_debug_json(self, label, data):
        if not self.debug_screenshots:
            return
        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            path = self.debug_dir / f"{label}.json"
            with path.open("w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=2)
        except Exception as exc:
            log_and_print(f"[AutoBridge] Failed to write debug json {label}: {exc}", "warning")

    def menu_region_around(self, x, y):
        menu = self.settings.get("context_menu", {})
        return [
            int(x + int(menu.get("left_offset", -40))),
            int(y + int(menu.get("top_offset", -180))),
            int(menu.get("width", 260)),
            int(menu.get("height", 260)),
        ]

    def read_context_menu(self, menu_region):
        menu_items = capture_and_find_multiple_text_coordinates(
            menu_region,
            self.settings.get("search_phrases", {}),
        )
        log_and_print(f"[AutoBridge] OCR context menu: region={menu_region}, items={menu_items}")
        return menu_items

    def choose_menu_action(self, menu_items):
        if menu_items.get("isText"):
            return "text"
        if menu_items.get("isImage"):
            return "image"
        if menu_items.get("isVideo"):
            return "video"
        return None

    def menu_key_for_action(self, action):
        return {
            "text": "isText",
            "image": "isImage",
            "video": "isVideo",
        }[action]

    def click_menu_item(self, menu_region, item_rect):
        x, y, w, h = item_rect
        click_x = menu_region[0] + x + w // 2
        click_y = menu_region[1] + y + h // 2
        log_and_print(
            f"[AutoBridge] Clicking OCR menu item: x={click_x}, y={click_y}, "
            f"item_rect={item_rect}, menu_region={menu_region}"
        )
        self.viber.click_in_messages(click_x, click_y, button="left")

    def close_context_menu(self, x, y):
        pyautogui.press("esc")
        cv2.waitKey(120)

    def screen_patch_hash(self, x, y):
        patch = int(self.settings.get("candidate_patch_size", 80))
        bbox = (
            max(0, int(x - patch // 2)),
            max(0, int(y - patch // 2)),
            max(1, int(x + patch // 2)),
            max(1, int(y + patch // 2)),
        )
        image = ImageGrab.grab(bbox)
        small = image.convert("L").resize((16, 16), Image.Resampling.LANCZOS)
        return hashlib.sha256(small.tobytes()).hexdigest()

    async def send_text(self, text):
        log_and_print(f"[AutoBridge] Sending text to Telegram: {text}")
        results = []
        for channel_name in self.channel_names:
            results.append(await process_one_message(text, self.bot_client, channel_name, self.name_viber, None))
        return any(results)

    async def send_image(self, image, image_hash):
        bio = BytesIO()
        bio.name = f"{image_hash}.png"
        image.save(bio, "PNG")
        bio.seek(0)

        log_and_print(f"[AutoBridge] Sending image to Telegram: {bio.name}")
        results = []
        for channel_name in self.channel_names:
            bio.seek(0)
            results.append(await process_one_message("", self.bot_client, channel_name, self.name_viber, bio))
        return any(results)

    def _check_interval(self):
        return self._setting_int("check_interval_seconds", 5)

    def _read_delay(self):
        return self._setting_int("read_delay_seconds", self._setting_int("pause_read_messages_second", 60))

    def _candidate_missing_tolerance(self):
        return self._setting_int("candidate_missing_tolerance", 4)

    def _candidate_match_tolerance(self):
        return self._setting_int("candidate_match_tolerance_px", 180)

    def _setting_int(self, name, default):
        try:
            return int(self.settings.get(name, default))
        except (TypeError, ValueError):
            return default


def hash_image(image):
    small = image.convert("L").resize((16, 16), Image.Resampling.LANCZOS)
    return hashlib.sha256(small.tobytes()).hexdigest()


async def main():
    try:
        bot_client, name_viber, _, channel_names = await startTgClient()
        _, _, settings = load_config()
        bridge = AutoBridge(bot_client, name_viber, channel_names, settings)
        await bridge.run()
    except KeyboardInterrupt:
        log_and_print("[AutoBridge] Stopped by user.")
    except Exception as exc:
        log_and_print(f"[AutoBridge] Fatal error: {exc}", "error")


if __name__ == "__main__":
    asyncio.run(main())
