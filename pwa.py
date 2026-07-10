import asyncio
import hashlib
import json
import re
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
from message_store import MessageStore, hash_file, hash_text, normalize_text
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
        self.backlog_scanned_signatures = set()
        self.backlog_processed_content_keys = set()
        self.backlog_consecutive_delivered = 0
        self.backlog_complete = False
        self.backlog_cycle_active = False
        self.backlog_cycle_number = 0
        self.marker_cycle_phase = "idle"
        self.marker_cycle_pause_until = 0
        self.marker_cycle_marker_y = None
        self.marker_cycle_find_pages = 0
        self.marker_cycle_last_down_signature = None
        self.marker_cycle_sent_keys = set()
        self.marker_cycle_message_cache = {}
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
                if self._backlog_scan_enabled():
                    await self.process_marker_cycle()
                else:
                    await self.process_visible_candidates()
            except Exception as exc:
                log_and_print(f"[AutoBridge] Read loop error: {exc}", "error")
                log_and_print(traceback.format_exc(), "error")
            await asyncio.sleep(self._check_interval())

    async def process_marker_cycle(self):
        now = time.monotonic()
        if self.marker_cycle_phase == "paused":
            remaining = self.marker_cycle_pause_until - now
            if remaining > 0:
                log_and_print(f"[AutoBridge] Cycle pause before next scan: {remaining:.1f}s remaining.")
                return
            self.start_marker_cycle()

        if self.marker_cycle_phase == "idle":
            self.start_marker_cycle()

        if self.marker_cycle_phase == "find_marker_up":
            await self.find_sent_marker_up()
        elif self.marker_cycle_phase == "send_down":
            await self.send_pending_down_page()

    def start_marker_cycle(self):
        self.viber.focus()
        self.viber.scroll_to_bottom(self._setting_int("scroll_to_bottom_count", 3))
        self.baseline_ready = True
        self.backlog_cycle_number += 1
        self.marker_cycle_phase = "find_marker_up"
        self.marker_cycle_marker_y = None
        self.marker_cycle_find_pages = 0
        self.marker_cycle_last_down_signature = None
        self.marker_cycle_sent_keys.clear()
        self.marker_cycle_message_cache.clear()
        self.backlog_scanned_signatures.clear()
        self.backlog_processed_content_keys.clear()
        log_and_print(
            "[AutoBridge] Marker cycle started from bottom without startup wait: "
            f"cycle={self.backlog_cycle_number}"
        )

    async def find_sent_marker_up(self):
        self.viber.focus()
        visible = self.collect_visible_candidates()
        self.save_debug_screenshot("marker_find_up")
        self.marker_cycle_find_pages += 1

        if not visible:
            log_and_print("[AutoBridge] Marker search page has no candidates; scrolling up.")
            self.viber.scroll(amount=self._setting_int("backlog_scroll_count", 1), wheel_dist=5)
            cv2.waitKey(self._setting_int("backlog_scroll_wait_ms", 500))
            return

        for candidate in reversed(visible):
            message = self.get_candidate_message(candidate)
            if not message:
                continue

            content_key = self.message_content_key(message)
            if not content_key:
                continue

            if self.message_is_sent_boundary(message):
                self.marker_cycle_phase = "send_down"
                self.marker_cycle_marker_y = candidate["y"]
                log_and_print(
                    "[AutoBridge] Sent marker found; sending newer messages below it: "
                    f"signature={candidate['signature']}, y={candidate['y']}, key={content_key}"
                )
                await self.send_pending_down_page()
                return

            log_and_print(
                "[AutoBridge] Candidate above current bottom is not delivered yet; "
                f"continue marker search upward: {content_key}"
            )

        max_pages = self._setting_int("marker_search_max_pages", 80)
        if self.marker_cycle_find_pages >= max_pages:
            log_and_print(
                "[AutoBridge] Sent marker was not found before max pages; "
                "pausing and will retry from bottom.",
                "warning",
            )
            self.complete_marker_cycle()
            return

        log_and_print(
            "[AutoBridge] Sent marker not found on this page; scrolling up. "
            f"pages={self.marker_cycle_find_pages}/{max_pages}"
        )
        self.viber.scroll(amount=self._setting_int("backlog_scroll_count", 1), wheel_dist=5)
        cv2.waitKey(self._setting_int("backlog_scroll_wait_ms", 500))

    async def send_pending_down_page(self):
        self.viber.focus()
        visible = self.collect_visible_candidates()
        self.save_debug_screenshot("marker_send_down")
        page_signature = self.visible_page_signature(visible)

        if page_signature and page_signature == self.marker_cycle_last_down_signature:
            log_and_print("[AutoBridge] Bottom reached after downward send scan; cycle complete.")
            self.complete_marker_cycle()
            return

        if not visible:
            log_and_print("[AutoBridge] Send-down page has no candidates; scrolling down.")
            self.marker_cycle_last_down_signature = page_signature
            self.viber.scroll(amount=self._send_down_scroll_count(), wheel_dist=-5)
            cv2.waitKey(self._setting_int("backlog_scroll_wait_ms", 500))
            return

        marker_y = self.marker_cycle_marker_y
        if marker_y is not None:
            candidates = [candidate for candidate in visible if candidate["y"] > marker_y]
            self.marker_cycle_marker_y = None
        else:
            candidates = list(visible)

        log_and_print(
            "[AutoBridge] Send-down page queued: "
            f"candidates={len(candidates)}, page_items={len(visible)}"
        )

        skip_candidates_until_y = -1
        for candidate in candidates:
            if candidate["y"] <= skip_candidates_until_y:
                log_and_print(
                    "[AutoBridge] Candidate skipped inside already copied long message area: "
                    f"{candidate['signature']}, y={candidate['y']}, until={skip_candidates_until_y}"
                )
                continue

            message = self.get_candidate_message(candidate)
            if not message:
                continue

            content_key = self.message_content_key(message)
            if not content_key:
                continue
            skip_px = self.estimated_same_message_skip_px(message)
            if skip_px:
                skip_candidates_until_y = max(skip_candidates_until_y, candidate["y"] + skip_px)
            if content_key in self.marker_cycle_sent_keys:
                log_and_print(f"[AutoBridge] Send-down duplicate skipped: {content_key}")
                continue

            status = await self.deliver_message_with_registry(message)
            self.marker_cycle_sent_keys.add(content_key)
            if status == "partial_failed":
                log_and_print(
                    f"[AutoBridge] Message partially failed; pending targets remain for next cycle: {content_key}",
                    "warning",
                )

        self.marker_cycle_last_down_signature = page_signature
        log_and_print("[AutoBridge] Send-down page processed; scrolling down.")
        self.viber.scroll(amount=self._send_down_scroll_count(), wheel_dist=-5)
        cv2.waitKey(self._setting_int("backlog_scroll_wait_ms", 500))

    def complete_marker_cycle(self):
        delay = self._read_delay()
        self.marker_cycle_phase = "paused"
        self.marker_cycle_pause_until = time.monotonic() + delay
        self.marker_cycle_marker_y = None
        self.marker_cycle_last_down_signature = None
        log_and_print(f"[AutoBridge] Cycle complete. Waiting {delay}s before next full scan.")

    def visible_page_signature(self, visible):
        if not visible:
            return "empty"
        return "|".join(candidate["signature"] for candidate in visible)

    def get_candidate_message(self, candidate):
        signature = candidate["signature"]
        cached = self.marker_cycle_message_cache.get(signature)
        if cached is not None:
            log_and_print(f"[AutoBridge] Candidate message cache hit: {signature}")
            return cached

        message = self.copy_candidate_after_delay(candidate)
        if message:
            self.marker_cycle_message_cache[signature] = message
        return message

    def estimated_same_message_skip_px(self, message):
        if message.get("type") != "text":
            return 0
        text_len = len(self.canonical_text_for_registry(message.get("text", "")))
        if text_len < self._setting_int("long_text_skip_min_chars", 240):
            return 0
        return min(
            self._setting_int("long_text_skip_max_px", 700),
            max(
                self._setting_int("long_text_skip_min_px", 180),
                int(text_len * float(self.settings.get("long_text_skip_px_per_char", 0.35))),
            ),
        )

    async def process_backlog_page(self):
        self.viber.focus()
        if not self.backlog_cycle_active:
            self.start_backlog_scan_cycle()

        visible = self.collect_visible_candidates()
        self.save_debug_screenshot("backlog_visible_scan")
        if not visible:
            log_and_print("[AutoBridge] Backlog page has no candidates; scrolling up.")
            self.viber.scroll(amount=1, wheel_dist=5)
            return

        new_candidates = [
            candidate for candidate in reversed(visible)
            if candidate["signature"] not in self.backlog_scanned_signatures
        ]
        if not new_candidates:
            log_and_print("[AutoBridge] Backlog page already scanned; scrolling up.")
            self.viber.scroll(amount=1, wheel_dist=5)
            cv2.waitKey(self._setting_int("backlog_scroll_wait_ms", 500))
            return

        log_and_print(
            f"[AutoBridge] Backlog page queued for immediate registry check: "
            f"candidates={len(new_candidates)}"
        )

        page_content_keys = set()
        for candidate in new_candidates:
            message = self.copy_candidate_after_delay(candidate)

            if not message:
                self.backlog_consecutive_delivered = 0
                continue

            content_key = self.message_content_key(message)
            if not content_key:
                log_and_print(f"[AutoBridge] Backlog cannot build content key, skipping: {message}", "warning")
                self.backlog_consecutive_delivered = 0
                continue

            if content_key in page_content_keys or content_key in self.backlog_processed_content_keys:
                log_and_print(
                    f"[AutoBridge] Backlog duplicate candidate for already handled content skipped: {content_key}"
                )
                self.backlog_scanned_signatures.add(candidate["signature"])
                continue
            page_content_keys.add(content_key)

            status = self.message_registry_status(message)
            if status == "already_delivered":
                self.backlog_consecutive_delivered += 1
                log_and_print(
                    f"[AutoBridge] Backlog already-delivered streak: "
                    f"{self.backlog_consecutive_delivered}/{self._backlog_stop_after()}"
                )
            else:
                self.backlog_consecutive_delivered = 0
                status = await self.wait_and_deliver_backlog_candidate(candidate)

            if status in ("already_delivered", "delivered_now"):
                self.backlog_processed_content_keys.add(content_key)
                self.backlog_scanned_signatures.add(candidate["signature"])
                self.known_candidate_signatures.add(candidate["signature"])

            if self.backlog_consecutive_delivered >= self._backlog_stop_after():
                self.backlog_complete = True
                self.backlog_cycle_active = False
                self.pending_candidates.clear()
                log_and_print(
                    "[AutoBridge] Backlog scan complete: stop marker reached "
                    f"({self.backlog_consecutive_delivered} consecutive already delivered messages)."
                )
                self.viber.scroll_to_bottom(self._setting_int("scroll_to_bottom_count", 3))
                return

        log_and_print(
            f"[AutoBridge] Backlog page processed; scrolling up. "
            f"already_delivered_streak={self.backlog_consecutive_delivered}"
        )
        self.viber.scroll(amount=self._setting_int("backlog_scroll_count", 1), wheel_dist=5)
        cv2.waitKey(self._setting_int("backlog_scroll_wait_ms", 500))

    def start_backlog_scan_cycle(self):
        self.viber.focus()
        self.viber.scroll_to_bottom(self._setting_int("scroll_to_bottom_count", 3))
        self.baseline_ready = True
        self.backlog_cycle_active = True
        self.backlog_complete = False
        self.backlog_cycle_number += 1
        self.backlog_scanned_signatures.clear()
        self.backlog_processed_content_keys.clear()
        self.backlog_consecutive_delivered = 0
        log_and_print(
            "[AutoBridge] Backlog scan cycle started from bottom: "
            f"cycle={self.backlog_cycle_number}, stop_after={self._backlog_stop_after()} "
            "consecutive already delivered messages."
        )

    async def wait_and_deliver_backlog_candidate(self, candidate):
        delay = self._read_delay()
        log_and_print(
            f"[AutoBridge] Backlog candidate is not delivered yet; "
            f"waiting {delay}s before second scan/send: {candidate['signature']}"
        )
        await self.wait_before_backlog_copy(delay)

        confirmed = self.find_visible_candidate_for_pending(candidate)
        self.save_debug_screenshot("backlog_after_delay_scan")
        if not confirmed:
            log_and_print(
                f"[AutoBridge] Backlog candidate not found after delay; will retry on later scan: "
                f"{candidate['signature']}",
                "warning",
            )
            return "failed"

        message = self.copy_candidate_after_delay(confirmed)
        if not message:
            return "failed"

        return await self.deliver_message_with_registry(message)

    async def wait_before_backlog_copy(self, delay):
        if delay <= 0:
            return

        step = max(1, self._setting_int("backlog_wait_log_interval_seconds", 10))
        remaining = delay
        while remaining > 0:
            sleep_for = min(step, remaining)
            log_and_print(
                f"[AutoBridge] Backlog waiting before second scan/copy: "
                f"{remaining}s remaining."
            )
            await asyncio.sleep(sleep_for)
            remaining -= sleep_for

    async def process_visible_candidates(self):
        self.viber.focus()
        self.viber.scroll_to_bottom(self._setting_int("scroll_to_bottom_count", 2))

        visible = self.collect_visible_candidates()
        self.save_debug_screenshot("visible_scan")
        if not self.baseline_ready:
            self.baseline_ready = True
            log_and_print(
                "[AutoBridge] Startup scan captured; visible messages will be checked "
                f"against delivery registry after delay: {len(visible)}"
            )

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

            if await self.send_message_with_registry(message):
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

    async def send_message_with_registry(self, message):
        return (await self.deliver_message_with_registry(message)) in ("already_delivered", "delivered_now")

    def message_registry_status(self, message):
        content_key = self.message_content_key(message)
        if not content_key:
            log_and_print(f"[AutoBridge] Cannot build registry key for message: {message}", "warning")
            return "failed"

        if self.sent_store.delivered_to_all(content_key, self.channel_names):
            return "already_delivered"

        if self.legacy_message_marked_sent(message):
            log_and_print(
                f"[AutoBridge] Message found in legacy sent registry; marking all targets delivered: {content_key}"
            )
            for channel_name in self.channel_names:
                self.sent_store.mark_delivered(content_key, channel_name)
                self.seen_store.mark_delivered(content_key, channel_name)
            return "already_delivered"

        return "pending"

    async def deliver_message_with_registry(self, message):
        content_key = self.message_content_key(message)
        if not content_key:
            log_and_print(f"[AutoBridge] Cannot build registry key for message: {message}", "warning")
            return "failed"

        registry_status = self.message_registry_status(message)
        if registry_status == "already_delivered":
            log_and_print(f"[AutoBridge] Message already delivered to all targets: {content_key}")
            return "already_delivered"

        pending_targets = [
            channel_name for channel_name in self.channel_names
            if not self.sent_store.has_delivery(content_key, channel_name)
        ]
        log_and_print(
            f"[AutoBridge] Delivery check: key={content_key}, "
            f"pending_targets={pending_targets}"
        )

        for channel_name in pending_targets:
            sent = await self.send_one_message_to_target(message, channel_name)
            if sent:
                self.sent_store.mark_delivered(content_key, channel_name)
                self.seen_store.mark_delivered(content_key, channel_name)
            else:
                log_and_print(
                    f"[AutoBridge] Send failed; will retry later: key={content_key}, target={channel_name}",
                    "warning",
                )

        if self.sent_store.delivered_to_all(content_key, self.channel_names):
            self.mark_message_content_sent(message)
            return "delivered_now"

        return "partial_failed"

    def message_is_sent_boundary(self, message):
        content_key = self.message_content_key(message)
        if not content_key:
            return False
        if self.legacy_message_marked_sent(message):
            return True
        if self.sent_store.delivered_to_all(content_key, self.channel_names):
            return True
        delivered = self.sent_store.deliveries.get(content_key, [])
        return bool(delivered)

    def legacy_message_marked_sent(self, message):
        if message["type"] == "text":
            return any(self.sent_store.has_text(text) for text in self.text_registry_variants(message.get("text", "")))
        if message["type"] == "image":
            return self.sent_store.has_image(message.get("image_hash"))
        if message["type"] in ("file", "video", "voice"):
            return self.sent_store.has_file(message.get("file_hash"))
        return False

    def message_content_key(self, message):
        if message["type"] == "text":
            return f"text:{hash_text(self.canonical_text_for_registry(message.get('text', '')))}"
        if message["type"] == "image":
            return f"image:{message.get('image_hash')}"
        if message["type"] in ("file", "video", "voice"):
            return f"file:{message.get('file_hash')}"
        return None

    def mark_message_content_sent(self, message):
        if message["type"] == "text":
            for text in self.text_registry_variants(message["text"]):
                self.sent_store.mark_text(text)
                self.seen_store.mark_text(text)
        elif message["type"] == "image":
            self.sent_store.mark_image(message["image_hash"])
            self.seen_store.mark_image(message["image_hash"])
        elif message["type"] in ("file", "video", "voice"):
            self.sent_store.mark_file(message["file_hash"])
            self.seen_store.mark_file(message["file_hash"])

    def canonical_text_for_registry(self, text):
        variants = self.text_registry_variants(text)
        return variants[-1] if variants else ""

    def text_registry_variants(self, text):
        normalized = normalize_text(text)
        if not normalized:
            return []

        variants = [normalized]
        without_header = strip_viber_text_header(normalized)
        if without_header and without_header not in variants:
            variants.append(without_header)
        return variants

    async def send_one_message_to_target(self, message, channel_name):
        if message["type"] == "text":
            text = self.telegram_text_for_send(message["text"])
            log_and_print(f"[AutoBridge] Sending text to Telegram target {channel_name}: {text}")
            return await process_one_message(text, self.bot_client, channel_name, self.name_viber, None)

        if message["type"] == "image":
            bio = BytesIO()
            bio.name = f"{message['image_hash']}.png"
            message["image"].save(bio, "PNG")
            bio.seek(0)
            log_and_print(f"[AutoBridge] Sending image to Telegram target {channel_name}: {bio.name}")
            return await process_one_message("", self.bot_client, channel_name, self.name_viber, bio)

        if message["type"] in ("file", "video", "voice"):
            log_and_print(
                f"[AutoBridge] Sending file to Telegram target {channel_name}: "
                f"{message['file_path']}, hash={message['file_hash']}"
            )
            return await process_one_message("", self.bot_client, channel_name, self.name_viber, message["file_path"])

        log_and_print(f"[AutoBridge] Unsupported message type for send: {message}", "warning")
        return False

    def telegram_text_for_send(self, text):
        return strip_viber_text_header(text) or normalize_text(text)

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
            log_and_print(
                f"[AutoBridge] Candidate patch changed before read; continuing with menu recheck. "
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

        if not self.actions_compatible(expected_type, action):
            self.close_context_menu(x, y)
            return None

        if action == "image" and menu_items.get("isImage"):
            menu_key = self.menu_key_for_action(action, menu_items)
            self.click_menu_item(menu_region, menu_items[menu_key])
            cv2.waitKey(self._setting_int("clipboard_wait_ms", 700))
            self.save_debug_screenshot("after_image_copy")
            return self.message_from_clipboard("image")

        if menu_items.get("isSelect"):
            return self.copy_selected_message(candidate, menu_region, menu_items, action)

        if action in ("file", "link"):
            self.close_context_menu(x, y)
            log_and_print(
                f"[AutoBridge] {action} message has no Select item; cannot safely copy via buffer.",
                "warning",
            )
            return None

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

    def copy_selected_message(self, candidate, menu_region, menu_items, expected_type):
        x = candidate["x"]
        y = candidate["y"]
        select_rect = menu_items.get("isSelect")
        if not select_rect:
            log_and_print(f"[AutoBridge] Menu has no Select item; items={menu_items}", "warning")
            self.close_context_menu(x, y)
            return None

        log_and_print(f"[AutoBridge] Selecting message before copy: expected_type={expected_type}")
        self.click_menu_item(menu_region, select_rect)
        cv2.waitKey(self._setting_int("select_wait_ms", self._setting_int("video_select_wait_ms", 500)))
        self.save_debug_screenshot("message_selected")

        pyperclip.copy("")
        self.viber.click_in_messages(x, y, button="right")
        cv2.waitKey(self._setting_int("context_menu_open_delay_ms", 350))
        self.save_debug_screenshot("selected_copy_context_menu")

        copy_menu_region = self.menu_region_around(x, y)
        copy_menu_items = self.read_context_menu(copy_menu_region)
        log_and_print(f"[AutoBridge] Selected copy menu items: {copy_menu_items}")
        copy_rect = copy_menu_items.get("isCopy")
        if not copy_rect:
            self.close_context_menu(x, y)
            log_and_print(
                "[AutoBridge] Selected message copy menu has no Copy item; trying Ctrl+C fallback.",
                "warning",
            )
            pyautogui.hotkey("ctrl", "c")
            cv2.waitKey(self._setting_int("selected_clipboard_wait_ms", self._setting_int("clipboard_wait_ms", 700)))
            self.save_debug_screenshot("after_selected_ctrl_c")
            message = self.message_from_clipboard(expected_type)
            if message:
                return message

            if expected_type in ("file", "video", "voice"):
                paths = self.reveal_file_from_viber(candidate)
                return self.message_from_file_paths(paths)

            return None

        self.click_menu_item(copy_menu_region, copy_rect)
        cv2.waitKey(self._setting_int("selected_clipboard_wait_ms", self._setting_int("clipboard_wait_ms", 700)))
        self.save_debug_screenshot("after_selected_copy")

        message = self.message_from_clipboard(expected_type)
        if message:
            return message

        if expected_type in ("file", "video", "voice"):
            log_and_print(
                "[AutoBridge] Clipboard did not contain file paths after selected Copy; "
                "trying Show in folder fallback.",
                "warning",
            )
            paths = self.reveal_file_from_viber(candidate)
            return self.message_from_file_paths(paths)

        log_and_print(f"[AutoBridge] Clipboard did not contain expected selected data: {expected_type}", "warning")
        return None

    def message_from_clipboard(self, expected_type=None):
        paths = self.read_clipboard_file_paths()
        file_message = self.message_from_file_paths(paths)
        if file_message:
            return file_message

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

        if expected_type in ("file", "video", "voice", "image"):
            log_and_print(
                f"[AutoBridge] Clipboard has no file/image data for expected {expected_type}.",
                "warning",
            )
            return None

        clipboard_text = repair_clipboard_text(pyperclip.paste())
        text = normalize_text(reformat_telegram_text(clipboard_text))
        if text:
            log_and_print(f"[AutoBridge] Clipboard text copied: chars={len(text)}")
            return {"type": "text", "text": text, "key": f"text:{text}"}

        return None

    def message_from_file_paths(self, paths):
        file_path = self.choose_sendable_file(paths)
        if not file_path:
            return None

        file_digest = hash_file(file_path)
        file_size = Path(file_path).stat().st_size
        log_and_print(
            f"[AutoBridge] Clipboard file copied: path={file_path}, size={file_size}, hash={file_digest}"
        )
        return {
            "type": "file",
            "file_path": file_path,
            "file_hash": file_digest,
            "key": f"file:{file_digest}",
        }

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
        return self.reveal_file_from_viber(candidate)

    def reveal_file_from_viber(self, candidate):
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
            recent = self.find_recent_downloaded_file()
            if recent:
                paths = [recent]

        if self.settings.get("video_close_folder_after_path", True):
            pyautogui.hotkey("alt", "f4")
            cv2.waitKey(300)
            self.viber.focus()

        return paths

    def find_recent_downloaded_video(self):
        return self.find_recent_downloaded_file()

    def find_recent_downloaded_file(self):
        root = Path(self.settings.get("path_files_downloads", "") or "")
        if not root.exists():
            return None

        sendable_extensions = self.sendable_file_extensions()
        newest = None
        for path in root.rglob("*"):
            try:
                if not path.is_file() or path.suffix.lower() not in sendable_extensions:
                    continue
                if newest is None or path.stat().st_mtime > newest.stat().st_mtime:
                    newest = path
            except OSError:
                continue

        if newest:
            log_and_print(f"[AutoBridge] Recent downloaded file fallback: {newest}")
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
        return self.choose_sendable_file(paths)

    def choose_sendable_file(self, paths):
        sendable_extensions = self.sendable_file_extensions()
        existing = [Path(path) for path in paths if Path(path).is_file()]
        for path in existing:
            if path.suffix.lower() in sendable_extensions:
                return str(path)
        if existing:
            return str(existing[0])
        return None

    def sendable_file_extensions(self):
        return {
            ".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".3gp", ".wmv",
            ".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wav", ".amr",
            ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip", ".rar", ".7z",
        }

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
        search_phrases.setdefault("isOpenInBrowser", ["Открыть в браузере"])
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
            return "file"
        if menu_items.get("isOpenInBrowser"):
            return "link"
        if menu_items.get("isVideo"):
            return "file"
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
        if expected == "link" and actual in ("link", "text", "copy"):
            return True
        if expected in ("file", "video", "voice") and actual in ("file", "video", "voice"):
            return True
        return False

    def menu_key_for_action(self, action, menu_items):
        preferred_key = {
            "text": "isText",
            "image": "isImage",
            "file": "isShowInFolder",
            "link": "isOpenInBrowser",
            "video": "isShowInFolder",
            "copy": "isCopy",
        }[action]
        if menu_items.get(preferred_key):
            return preferred_key
        if action in ("file", "video", "voice") and menu_items.get("isVideo"):
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

    def _backlog_scan_enabled(self):
        return bool(self.settings.get("backlog_scan_enabled", True))

    def _backlog_stop_after(self):
        return max(1, self._setting_int("backlog_stop_after_sent_count", 3))

    def _send_down_scroll_count(self):
        return max(1, self._setting_int("marker_send_down_scroll_count", 3))

    def _setting_int(self, name, default):
        try:
            return int(self.settings.get(name, default))
        except (TypeError, ValueError):
            return default


def hash_image(image):
    small = image.convert("L").resize((16, 16), Image.Resampling.LANCZOS)
    return hashlib.sha256(small.tobytes()).hexdigest()


def repair_clipboard_text(text):
    if not text:
        return text

    try:
        repaired = text.encode("cp1251").decode("utf-8")
    except UnicodeError:
        return text

    if mojibake_score(text) >= 3 and mojibake_score(repaired) < mojibake_score(text):
        log_and_print("[AutoBridge] Clipboard text encoding repaired from cp1251 mojibake.")
        return repaired
    return text


def strip_viber_text_header(text):
    normalized = normalize_text(text)
    if not normalized.startswith("["):
        return normalized

    # Viber selected-copy text usually starts with:
    # [ weekday, date time ] sender: message body
    match = re.match(r"^\[[^\]]+\]\s*[^\n:]{1,120}:\s*(.*)$", normalized, flags=re.DOTALL)
    if match:
        return normalize_text(match.group(1))
    return normalized


def mojibake_score(text):
    markers = (
        "Р°", "Р±", "РІ", "Рі", "Рґ", "Рµ", "Р¶", "Р·", "Рё", "Р№",
        "Рє", "Р»", "Рј", "РЅ", "Рѕ", "Рї", "СЂ", "СЃ", "С‚", "Сѓ",
        "С„", "С…", "С†", "С‡", "С€", "С‰", "СЊ", "С‹", "СЌ", "СЋ",
        "СЏ", "С–", "С—", "С”", "С‘", "С™", "рџ",
    )
    return sum(text.count(marker) for marker in markers)


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
