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
import win32clipboard
import win32con
from PIL import Image, ImageGrab

from init import init as load_config
from log import log_and_print
from message_store import MessageStore, hash_file, normalize_text
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

            pending = self.pending_candidates.get(signature)

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
            pending["_pending_key"] = signature
            elapsed = now - pending["first_seen"]
            if elapsed >= self._read_delay():
                log_and_print(
                    f"[AutoBridge] Candidate delay elapsed: signature={signature}, "
                    f"elapsed={elapsed:.1f}/{self._read_delay()}s"
                )
                ready.append(pending)
            else:
                log_and_print(
                    f"[AutoBridge] Candidate waiting before read: signature={signature}, "
                    f"elapsed={elapsed:.1f}/{self._read_delay()}s"
                )

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
                self.known_candidate_signatures.add(confirmed["signature"])
            elif message["type"] == "text":
                if await self.send_text(message["text"]):
                    self.sent_store.mark_text(message["text"])
                    self.seen_store.mark_text(message["text"])
                    self.known_candidate_signatures.add(signature)
                    self.known_candidate_signatures.add(confirmed["signature"])
            elif message["type"] == "image" and self.sent_store.has_image(message["image_hash"]):
                log_and_print("[AutoBridge] Image already sent before, skipping duplicate.")
                self.known_candidate_signatures.add(signature)
                self.known_candidate_signatures.add(confirmed["signature"])
            elif message["type"] == "image":
                if await self.send_image(message["image"], message["image_hash"]):
                    self.sent_store.mark_image(message["image_hash"])
                    self.seen_store.mark_image(message["image_hash"])
                    self.known_candidate_signatures.add(signature)
                    self.known_candidate_signatures.add(confirmed["signature"])
            elif message["type"] == "video":
                if self.sent_store.has_file(message["file_hash"]):
                    log_and_print("[AutoBridge] Video already sent before, skipping duplicate.")
                    self.known_candidate_signatures.add(signature)
                    self.known_candidate_signatures.add(confirmed["signature"])
                elif await self.send_video(message["file_path"], message["file_hash"]):
                    self.sent_store.mark_file(message["file_hash"])
                    self.seen_store.mark_file(message["file_hash"])
                    self.known_candidate_signatures.add(signature)
                    self.known_candidate_signatures.add(confirmed["signature"])

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
        candidates = self.collect_visible_candidates()
        self.save_debug_screenshot("second_scan")
        for candidate in candidates:
            if candidate["signature"] == pending["signature"]:
                return candidate

        log_and_print(
            f"[AutoBridge] Exact delayed candidate not found on second scan; "
            f"it will be queued again if still visible: {pending['signature']}",
            "warning",
        )
        return None

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
            if expected_type == "video":
                log_and_print(
                    f"[AutoBridge] Video candidate patch changed before read; continuing with menu recheck. "
                    f"key={candidate['key']}, old_hash={candidate['visual_hash']}, new_hash={current_hash}",
                    "warning",
                )
            else:
                log_and_print(
                    f"[AutoBridge] Candidate patch changed before read; aborting this copy attempt. "
                    f"key={candidate['key']}, old_hash={candidate['visual_hash']}, new_hash={current_hash}",
                    "warning",
                )
                return None

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

        if not self.actions_compatible(expected_type, action):
            self.close_context_menu(x, y)
            return None

        if action == "video":
            return self.copy_video_after_select(candidate, menu_region, menu_items)

        menu_key = self.menu_key_for_action(action, menu_items)
        self.click_menu_item(menu_region, menu_items[menu_key])
        cv2.waitKey(self._setting_int("clipboard_wait_ms", 700))
        self.save_debug_screenshot("after_delayed_copy")

        if action in ("text", "copy"):
            text = normalize_text(reformat_telegram_text(pyperclip.paste()))
            if text:
                log_and_print(f"[AutoBridge] Clipboard text copied: chars={len(text)}")
                return {"type": "text", "text": text, "key": f"text:{text}"}

        if action in ("image", "copy"):
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

    def copy_video_after_select(self, candidate, menu_region, menu_items):
        x = candidate["x"]
        y = candidate["y"]
        select_rect = menu_items.get("isSelect")
        if not select_rect:
            log_and_print(f"[AutoBridge] Video menu has no Select item; items={menu_items}", "warning")
            self.close_context_menu(x, y)
            return None

        log_and_print("[AutoBridge] Video detected; selecting video before copy.")
        self.click_menu_item(menu_region, select_rect)
        cv2.waitKey(self._setting_int("video_select_wait_ms", 500))
        self.save_debug_screenshot("video_selected")

        pyperclip.copy("")
        self.viber.click_in_messages(x, y, button="right")
        cv2.waitKey(self._setting_int("context_menu_open_delay_ms", 350))
        self.save_debug_screenshot("video_copy_context_menu")

        copy_menu_region = self.menu_region_around(x, y)
        copy_menu_items = self.read_context_menu(copy_menu_region)
        log_and_print(f"[AutoBridge] Video copy menu items: {copy_menu_items}")
        copy_rect = copy_menu_items.get("isCopy")
        if not copy_rect:
            self.close_context_menu(x, y)
            log_and_print("[AutoBridge] Video copy menu has no Copy item.", "warning")
            return None

        self.click_menu_item(copy_menu_region, copy_rect)
        cv2.waitKey(self._setting_int("video_clipboard_wait_ms", self._setting_int("clipboard_wait_ms", 700)))
        self.save_debug_screenshot("after_video_copy")

        paths = self.read_clipboard_file_paths()
        if not paths:
            log_and_print(
                "[AutoBridge] Clipboard did not contain video file paths after Copy; "
                "trying Show in folder fallback.",
                "warning",
            )
            paths = self.reveal_video_file_from_viber(candidate)
        if not paths:
            log_and_print("[AutoBridge] Could not resolve video file path.", "warning")
            return None

        file_path = self.choose_video_file(paths)
        if not file_path:
            log_and_print(f"[AutoBridge] Clipboard file paths are not usable video files: {paths}", "warning")
            return None

        file_digest = hash_file(file_path)
        file_size = Path(file_path).stat().st_size
        log_and_print(
            f"[AutoBridge] Clipboard video copied: path={file_path}, size={file_size}, hash={file_digest}"
        )
        return {
            "type": "video",
            "file_path": file_path,
            "file_hash": file_digest,
            "key": f"video:{file_digest}",
        }

    def reveal_video_file_from_viber(self, candidate):
        x = candidate["x"]
        y = candidate["y"]

        self.viber.focus()
        self.viber.click_in_messages(x, y, button="right")
        cv2.waitKey(self._setting_int("context_menu_open_delay_ms", 350))
        self.save_debug_screenshot("video_show_in_folder_menu")

        menu_region = self.menu_region_around(x, y)
        menu_items = self.read_context_menu(menu_region)
        show_rect = menu_items.get("isShowInFolder")
        if not show_rect:
            self.close_context_menu(x, y)
            log_and_print(f"[AutoBridge] Show in folder item not found for video: {menu_items}", "warning")
            return []

        self.click_menu_item(menu_region, show_rect)
        cv2.waitKey(self._setting_int("video_show_in_folder_wait_ms", 5000))
        self.save_debug_screenshot("video_folder_opened")

        pyautogui.hotkey("ctrl", "c")
        cv2.waitKey(self._setting_int("video_folder_clipboard_wait_ms", 500))
        paths = self.read_clipboard_file_paths()
        if not paths:
            recent = self.find_recent_downloaded_video()
            if recent:
                paths = [recent]

        if self.settings.get("video_close_folder_after_path", True):
            pyautogui.hotkey("alt", "f4")
            cv2.waitKey(300)
            self.viber.focus()

        return paths

    def find_recent_downloaded_video(self):
        root = Path(self.settings.get("path_files_downloads", "") or "")
        if not root.exists():
            return None

        video_extensions = {
            ".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".3gp", ".wmv",
        }
        newest = None
        for path in root.rglob("*"):
            try:
                if not path.is_file() or path.suffix.lower() not in video_extensions:
                    continue
                if newest is None or path.stat().st_mtime > newest.stat().st_mtime:
                    newest = path
            except OSError:
                continue

        if newest:
            log_and_print(f"[AutoBridge] Recent downloaded video fallback: {newest}")
            return str(newest)
        return None

    def read_clipboard_file_paths(self):
        paths = []
        try:
            win32clipboard.OpenClipboard()
            if win32clipboard.IsClipboardFormatAvailable(win32con.CF_HDROP):
                paths = list(win32clipboard.GetClipboardData(win32con.CF_HDROP))
        except Exception as exc:
            log_and_print(f"[AutoBridge] Failed to read file paths from clipboard: {exc}", "warning")
        finally:
            try:
                win32clipboard.CloseClipboard()
            except Exception:
                pass

        if not paths:
            try:
                clipboard_data = ImageGrab.grabclipboard()
                if isinstance(clipboard_data, list):
                    paths = [str(item) for item in clipboard_data]
            except Exception as exc:
                log_and_print(f"[AutoBridge] PIL clipboard file fallback failed: {exc}", "warning")

        log_and_print(f"[AutoBridge] Clipboard file paths: {paths}")
        return paths

    def choose_video_file(self, paths):
        video_extensions = {
            ".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".3gp", ".wmv",
        }
        existing = [Path(path) for path in paths if Path(path).is_file()]
        for path in existing:
            if path.suffix.lower() in video_extensions:
                return str(path)
        if existing:
            return str(existing[0])
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
        search_phrases = dict(self.settings.get("search_phrases", {}))
        search_phrases.setdefault("isCopy", ["Копировать", "Скопировать"])
        search_phrases.setdefault("isPhotoWord", ["фото", "фот"])
        search_phrases.setdefault("isShowInFolder", ["Показать в папке"])
        search_phrases.setdefault("isSelect", ["Выбрать"])
        menu_items = capture_and_find_multiple_text_coordinates(
            menu_region,
            search_phrases,
        )
        log_and_print(f"[AutoBridge] OCR context menu: region={menu_region}, items={menu_items}")
        return menu_items

    def choose_menu_action(self, menu_items):
        if menu_items.get("isImage"):
            return "image"
        if menu_items.get("isCopy") and menu_items.get("isPhotoWord"):
            return "image"
        if menu_items.get("isShowInFolder"):
            return "video"
        if menu_items.get("isVideo"):
            return "video"
        if menu_items.get("isText"):
            return "text"
        if menu_items.get("isCopy"):
            return "copy"
        return None

    def actions_compatible(self, expected, actual):
        if expected == actual:
            return True
        if expected == "copy" and actual in ("text", "image", "copy"):
            return True
        if actual == "copy" and expected in ("text", "image", "copy"):
            return True
        return False

    def menu_key_for_action(self, action, menu_items):
        preferred_key = {
            "text": "isText",
            "image": "isImage",
            "video": "isShowInFolder",
            "copy": "isCopy",
        }[action]
        if menu_items.get(preferred_key):
            return preferred_key
        if action == "video" and menu_items.get("isVideo"):
            return "isVideo"
        if action in ("text", "image", "copy") and menu_items.get("isCopy"):
            return "isCopy"
        return preferred_key

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

    async def send_video(self, file_path, file_hash):
        log_and_print(f"[AutoBridge] Sending video to Telegram: {file_path}, hash={file_hash}")
        results = []
        for channel_name in self.channel_names:
            results.append(await process_one_message("", self.bot_client, channel_name, self.name_viber, file_path))
        return any(results)

    def _check_interval(self):
        return self._setting_int("check_interval_seconds", 5)

    def _read_delay(self):
        return self._setting_int("read_delay_seconds", self._setting_int("pause_read_messages_second", 60))

    def _candidate_missing_tolerance(self):
        return self._setting_int("candidate_missing_tolerance", 4)

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
        tg_start = await startTgClient()
        if not tg_start:
            raise RuntimeError("Telegram client failed to start. See previous log lines for details.")
        bot_client, name_viber, _, channel_names = tg_start
        _, _, settings = load_config()
        bridge = AutoBridge(bot_client, name_viber, channel_names, settings)
        await bridge.run()
    except KeyboardInterrupt:
        log_and_print("[AutoBridge] Stopped by user.")
    except Exception as exc:
        log_and_print(f"[AutoBridge] Fatal error: {exc}", "error")


if __name__ == "__main__":
    asyncio.run(main())
