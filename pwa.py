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
import numpy as np
import pyautogui
import pyperclip
import win32clipboard
import win32com.client
import win32con
import win32gui
from PIL import Image, ImageGrab

from init import init as load_config
from log import log_and_print
from message_store import MessageStore, hash_file, hash_text, normalize_text
from recognize_text import capture_and_find_multiple_text_coordinates
from runtime_cleanup import cleanup_runtime_artifacts
from tg import startTgClient
from vb_utils import process_one_message, reformat_telegram_text
from viber_window import ViberWindow


class AutoBridge:
    def __init__(self, bot_client, name_viber, channel_names, settings):
        self.bot_client = bot_client
        self.name_viber = name_viber
        self.channel_names = channel_names
        self.settings = settings
        registry_reset_interval = int(settings.get("message_registry_reset_interval_seconds", 3600))
        registry_preserve_latest = int(settings.get("message_registry_preserve_latest_count", 3))
        self.sent_store = MessageStore(
            path=settings.get("sent_messages_file", "sent_messages.json"),
            max_items=int(settings.get("sent_messages_limit", 1000)),
            reset_interval_seconds=registry_reset_interval,
            preserve_latest_count=registry_preserve_latest,
        )
        self.seen_store = MessageStore(
            path=settings.get("seen_messages_file", "seen_messages.json"),
            max_items=int(settings.get("seen_messages_limit", settings.get("sent_messages_limit", 1000))),
            reset_interval_seconds=registry_reset_interval,
            preserve_latest_count=registry_preserve_latest,
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
        self.marker_cycle_last_down_content_signature = None
        self.marker_cycle_same_content_pages = 0
        self.marker_cycle_sent_keys = set()
        self.marker_cycle_message_cache = {}
        self.marker_cycle_boundary_streak = 0
        self.marker_cycle_boundary_keys = []
        self.marker_cycle_boundary_y = None
        self.marker_cycle_control_remaining = 0
        self.debug_dir = Path(settings.get("debug_screenshot_dir", "runtime_debug"))
        self.debug_screenshots = bool(settings.get("debug_screenshots_enabled", False))
        self.shot_index = 0

    async def run(self):
        self.viber.connect()
        if not self.viber.ensure_channel():
            raise RuntimeError("Cannot find or confirm the configured Viber channel.")

        self.recover_stuck_selection_mode()
        self.viber.scroll_to_bottom(self._setting_int("scroll_to_bottom_count", 3))
        log_and_print(
            "[AutoBridge] Started. "
            f"check_interval={self._check_interval()}s, read_delay={self._read_delay()}s"
        )

        while True:
            try:
                self.recover_stuck_selection_mode()
                self.reset_expired_message_registries()
                if self._backlog_scan_enabled():
                    await self.process_marker_cycle()
                else:
                    await self.process_visible_candidates()
            except Exception as exc:
                log_and_print(f"[AutoBridge] Read loop error: {exc}", "error")
                log_and_print(traceback.format_exc(), "error")
            await asyncio.sleep(self._check_interval())

    def recover_stuck_selection_mode(self):
        if not self.viber.exit_selection_mode_if_open():
            return False

        self.marker_cycle_phase = "idle"
        self.pending_candidates.clear()
        self.known_candidate_signatures.clear()
        self.marker_cycle_message_cache.clear()
        self.backlog_scanned_signatures.clear()
        self.backlog_processed_content_keys.clear()
        log_and_print(
            "[AutoBridge] Viber selection mode recovery completed; "
            "the scan will restart from the bottom."
        )
        return True

    def reset_expired_message_registries(self):
        sent_cleared = self.sent_store.clear_if_expired()
        seen_cleared = self.seen_store.clear_if_expired()
        if not (sent_cleared or seen_cleared):
            return

        self.marker_cycle_phase = "idle"
        self.marker_cycle_sent_keys.clear()
        self.marker_cycle_message_cache.clear()
        self.backlog_processed_content_keys.clear()
        self.backlog_scanned_signatures.clear()
        self.known_candidate_signatures.clear()
        self.pending_candidates.clear()
        log_and_print(
            "[AutoBridge] Message registries expired; old entries were cleared "
            "and protected recent entries were retained; "
            "starting a fresh scan cycle."
        )

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
        elif self.marker_cycle_phase in ("send_down", "send_down_control"):
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
        self.marker_cycle_last_down_content_signature = None
        self.marker_cycle_same_content_pages = 0
        self.marker_cycle_sent_keys.clear()
        self.marker_cycle_message_cache.clear()
        self.marker_cycle_boundary_streak = 0
        self.marker_cycle_boundary_keys.clear()
        self.marker_cycle_boundary_y = None
        self.marker_cycle_control_remaining = 0
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
            if await self.start_marker_fallback_send_down_if_due():
                return
            log_and_print("[AutoBridge] Marker search page has no candidates; scrolling up.")
            self.viber.scroll(amount=self._setting_int("backlog_scroll_count", 1), wheel_dist=5)
            cv2.waitKey(self._setting_int("backlog_scroll_wait_ms", 500))
            return

        # Search from newest to oldest. This phase only establishes a stable
        # boundary; sending before the boundary is known breaks message order.
        # Pending messages do not reset protected sent markers. After registry
        # expiry the three retained markers can have missed messages between
        # them, and those gaps are exactly what the downward pass must recover.
        for candidate in sorted(visible, key=lambda item: item["y"], reverse=True):
            message = self.get_candidate_message(candidate)
            if not message:
                continue

            content_key = self.message_content_key(message)
            if not content_key:
                continue

            if self.message_is_sent_boundary(message):
                if content_key in self.marker_cycle_boundary_keys:
                    log_and_print(
                        "[AutoBridge] Duplicate sent boundary candidate ignored: "
                        f"streak={self.marker_cycle_boundary_streak}/{self._marker_stop_after_sent_count()}, "
                        f"signature={candidate['signature']}, y={candidate['y']}, key={content_key}"
                    )
                    continue

                self.marker_cycle_boundary_keys.append(content_key)
                self.marker_cycle_boundary_streak = len(self.marker_cycle_boundary_keys)
                log_and_print(
                    "[AutoBridge] Sent boundary candidate found: "
                    f"streak={self.marker_cycle_boundary_streak}/{self._marker_stop_after_sent_count()}, "
                    f"signature={candidate['signature']}, y={candidate['y']}, key={content_key}"
                )
                if self.marker_cycle_boundary_streak >= self._marker_stop_after_sent_count():
                    self.marker_cycle_phase = "send_down"
                    self.sent_store.set_protected_keys(self.marker_cycle_boundary_keys)
                    # Registry expiry may happen during a long video backlog.
                    # Keep boundary messages protected for this whole cycle so
                    # reaching them again on the downward pass cannot resend them.
                    self.marker_cycle_sent_keys.update(self.marker_cycle_boundary_keys)
                    # Use the third marker that is visible now. A streak can span
                    # two scrolled pages, so an older saved Y coordinate is unsafe.
                    self.marker_cycle_marker_y = candidate["y"]
                    log_and_print(
                        "[AutoBridge] Sent boundary confirmed; sending newer messages below it: "
                        f"marker_y={self.marker_cycle_marker_y}"
                    )
                    await self.send_pending_down_page()
                    return
                continue

            log_and_print(
                "[AutoBridge] Pending candidate found during boundary search; "
                "retaining previously found sent markers and not sending until "
                f"the three-message boundary is confirmed: {content_key}"
            )

        if await self.start_marker_fallback_send_down_if_due():
            return

        fallback_pages = self._marker_fallback_after_pages()
        log_and_print(
            "[AutoBridge] Sent marker not found on this page; scrolling up. "
            f"pages={self.marker_cycle_find_pages}/{fallback_pages}"
        )
        self.viber.scroll(amount=self._setting_int("backlog_scroll_count", 1), wheel_dist=5)
        cv2.waitKey(self._setting_int("backlog_scroll_wait_ms", 500))

    async def start_marker_fallback_send_down_if_due(self):
        fallback_pages = self._marker_fallback_after_pages()
        if self.marker_cycle_find_pages < fallback_pages:
            return False

        self.marker_cycle_phase = "send_down"
        self.marker_cycle_marker_y = None
        self.marker_cycle_sent_keys.update(self.marker_cycle_boundary_keys)
        log_and_print(
            "[AutoBridge] Three-message sent boundary was not found within "
            f"{fallback_pages} pages; sending downward from the oldest reached page. "
            f"protected_sent_keys={len(self.marker_cycle_boundary_keys)}",
            "warning",
        )
        await self.send_pending_down_page()
        return True

    async def send_pending_down_page(self):
        self.viber.focus()
        visible = self.collect_visible_candidates()
        self.save_debug_screenshot("marker_send_down")
        page_signature = self.visible_page_signature(visible)

        if visible and page_signature == self.marker_cycle_last_down_signature:
            log_and_print("[AutoBridge] Bottom reached after downward send scan.")
            if self.start_bottom_control_scan_if_needed():
                return
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
            candidates = sorted(
                (candidate for candidate in visible if candidate["y"] > marker_y),
                key=lambda item: item["y"],
            )
            self.marker_cycle_marker_y = None
        else:
            candidates = sorted(visible, key=lambda item: item["y"])

        log_and_print(
            "[AutoBridge] Send-down page queued: "
            f"candidates={len(candidates)}, page_items={len(visible)}"
        )

        skip_candidates_until_y = -1
        page_content_keys = set()
        page_duplicate_keys = 0
        stop_after_duplicate_keys = self._page_duplicate_content_stop_after()
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
            if content_key in page_content_keys:
                page_duplicate_keys += 1
                log_and_print(f"[AutoBridge] Send-down duplicate content skipped on page: {content_key}")
                if page_duplicate_keys >= stop_after_duplicate_keys:
                    log_and_print(
                        "[AutoBridge] Send-down stops this page after repeated duplicate content: "
                        f"duplicates={page_duplicate_keys}/{stop_after_duplicate_keys}"
                    )
                    break
                continue
            page_content_keys.add(content_key)
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

        if self.down_content_page_repeated(page_content_keys):
            log_and_print(
                "[AutoBridge] Bottom reached after repeated stable content page: "
                f"repeats={self.marker_cycle_same_content_pages}/"
                f"{self._bottom_same_content_pages()}"
            )
            if self.start_bottom_control_scan_if_needed():
                return
            self.complete_marker_cycle()
            return

        self.marker_cycle_last_down_signature = page_signature
        log_and_print("[AutoBridge] Send-down page processed; scrolling down.")
        self.viber.scroll(amount=self._send_down_scroll_count(), wheel_dist=-5)
        cv2.waitKey(self._setting_int("backlog_scroll_wait_ms", 500))

    def start_bottom_control_scan_if_needed(self):
        if self.marker_cycle_phase == "send_down_control":
            self.marker_cycle_control_remaining -= 1
            if self.marker_cycle_control_remaining > 0:
                log_and_print(
                    "[AutoBridge] Bottom control scan pass complete; repeating: "
                    f"remaining={self.marker_cycle_control_remaining}"
                )
                self.viber.scroll_to_bottom(self._setting_int("scroll_to_bottom_count", 2))
                self.marker_cycle_last_down_signature = None
                self.marker_cycle_last_down_content_signature = None
                self.marker_cycle_same_content_pages = 0
                return True
            log_and_print("[AutoBridge] Bottom control scan complete.")
            return False

        passes = self._bottom_control_passes()
        if passes <= 0:
            return False
        self.marker_cycle_phase = "send_down_control"
        self.marker_cycle_control_remaining = passes
        self.marker_cycle_last_down_signature = None
        self.marker_cycle_last_down_content_signature = None
        self.marker_cycle_same_content_pages = 0
        self.marker_cycle_marker_y = None
        self.viber.scroll_to_bottom(self._setting_int("scroll_to_bottom_count", 2))
        log_and_print(f"[AutoBridge] Starting bottom control scan: passes={passes}")
        return True

    def complete_marker_cycle(self):
        delay = self._cycle_pause_seconds()
        self.marker_cycle_phase = "paused"
        self.marker_cycle_pause_until = time.monotonic() + delay
        self.marker_cycle_marker_y = None
        self.marker_cycle_last_down_signature = None
        self.marker_cycle_last_down_content_signature = None
        self.marker_cycle_same_content_pages = 0
        self.marker_cycle_boundary_streak = 0
        self.marker_cycle_boundary_keys.clear()
        self.marker_cycle_boundary_y = None
        self.marker_cycle_control_remaining = 0
        log_and_print(f"[AutoBridge] Cycle complete. Waiting {delay}s before next full scan.")

    def visible_page_signature(self, visible):
        if not visible:
            return "empty"
        return "|".join(candidate["signature"] for candidate in visible)

    def down_content_page_repeated(self, content_keys):
        signature = "|".join(sorted(content_keys))
        if not signature:
            self.marker_cycle_last_down_content_signature = None
            self.marker_cycle_same_content_pages = 0
            return False

        if signature == self.marker_cycle_last_down_content_signature:
            self.marker_cycle_same_content_pages += 1
        else:
            self.marker_cycle_last_down_content_signature = signature
            self.marker_cycle_same_content_pages = 1
        return self.marker_cycle_same_content_pages >= self._bottom_same_content_pages()

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
        if not bool(self.settings.get("long_text_skip_enabled", False)):
            return 0
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
        if delay > 0:
            log_and_print(
                f"[AutoBridge] Backlog candidate is not delivered yet; "
                f"waiting {delay}s before second scan/send: {candidate['signature']}"
            )
        else:
            log_and_print(
                f"[AutoBridge] Backlog candidate is not delivered yet; "
                f"second scan/send starts immediately: {candidate['signature']}"
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
        read_delay = self._read_delay()

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
                if read_delay <= 0:
                    log_and_print(
                        f"[AutoBridge] New candidate queued for immediate read: signature={signature}, "
                        f"type={candidate['type']}"
                    )
                    ready.append(candidate)
                    continue
                log_and_print(
                    f"[AutoBridge] New candidate queued for delayed read: signature={signature}, "
                    f"type={candidate['type']}, delay={read_delay}s"
                )
                continue

            pending["misses"] = 0
            first_seen = pending["first_seen"]
            pending.update(candidate)
            pending["first_seen"] = first_seen
            pending["_pending_key"] = signature
            elapsed = now - pending["first_seen"]
            if elapsed >= read_delay:
                log_and_print(
                    f"[AutoBridge] Candidate delay elapsed: signature={signature}, "
                    f"elapsed={elapsed:.1f}/{read_delay}s"
                )
                ready.append(pending)
            else:
                log_and_print(
                    f"[AutoBridge] Candidate waiting before read: signature={signature}, "
                    f"elapsed={elapsed:.1f}/{read_delay}s"
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
        anchor_enabled = bool(capture.get("reaction_anchor", {}).get("enabled", False))
        if anchor_enabled:
            anchor_candidates = self.collect_anchor_candidates(region, capture)
            media_group_candidates = self.collect_media_group_candidates(region, capture)
            candidates = self.dedupe_candidates_by_y(
                sorted(anchor_candidates + media_group_candidates, key=lambda item: item["y"]),
                self._setting_int("candidate_y_dedupe_px", 90),
            )
            log_and_print(
                "[AutoBridge] Visible candidates found by anchors: "
                f"reactions={len(anchor_candidates)}, media_groups={len(media_group_candidates)}, "
                f"total={len(candidates)}"
            )
            self.write_debug_json("visible_candidates", candidates)
            return candidates

        step = max(20, int(capture.get("scan_step_px", 80)))
        offsets = capture.get("scan_step_offsets_px", [0, step // 2])
        bottom_offsets = capture.get("scan_bottom_probe_offsets_px", [0, 20, 40, 60])
        x_ratios = capture.get("message_click_x_ratios", [capture.get("message_click_x_ratio", 0.22)])
        y_min = region.top + int(capture.get("scan_top_skip_px", 18))
        y_max = region.bottom - int(capture.get("scan_bottom_skip_px", 12))

        candidates = []
        seen = set()
        accepted_candidate_y = []
        candidate_y_gap = self._setting_int("candidate_y_dedupe_px", 90)
        y_values = []
        y_seen = set()

        def add_scan_y(scan_y):
            scan_y = int(scan_y)
            if scan_y < y_min or scan_y > y_max or scan_y in y_seen:
                return
            y_seen.add(scan_y)
            y_values.append(scan_y)

        for offset in bottom_offsets:
            add_scan_y(y_max - int(offset))

        for y in range(y_max, y_min, -step):
            for offset in offsets:
                add_scan_y(y - int(offset))

        for y in sorted(y_values, reverse=True):
            if any(abs(y - accepted_y) <= candidate_y_gap for accepted_y in accepted_candidate_y):
                log_and_print(
                    "[AutoBridge] Scan Y skipped near already detected candidate: "
                    f"y={y}, gap={candidate_y_gap}"
                )
                continue
            for x_ratio in x_ratios:
                x = region.left + int(region.width * float(x_ratio))
                candidate = self.inspect_candidate_at(x, y)
                if not candidate:
                    continue
                if candidate["key"] in seen:
                    continue
                seen.add(candidate["key"])
                candidates.append(candidate)
                accepted_candidate_y.append(candidate["y"])
                break

        candidates.reverse()
        candidates = self.dedupe_candidates_by_y(
            candidates,
            self._setting_int("candidate_y_dedupe_px", 90),
        )
        log_and_print(f"[AutoBridge] Visible candidates found: {len(candidates)}")
        self.write_debug_json("visible_candidates", candidates)
        return candidates

    def dedupe_candidates_by_y(self, candidates, tolerance_px):
        if tolerance_px <= 0:
            return candidates

        deduped = []
        for candidate in candidates:
            if any(abs(candidate["y"] - kept["y"]) <= tolerance_px for kept in deduped):
                log_and_print(
                    "[AutoBridge] Candidate skipped as same message row: "
                    f"{candidate['signature']}, y={candidate['y']}, tolerance={tolerance_px}"
                )
                continue
            deduped.append(candidate)
        return deduped

    def collect_anchor_candidates(self, region, capture):
        anchor = capture.get("reaction_anchor", {})
        if not bool(anchor.get("enabled", False)):
            return []

        template_paths = self.reaction_anchor_template_paths(anchor)
        if not template_paths:
            log_and_print("[AutoBridge] Reaction anchor is enabled but no templates are configured.", "warning")
            return []

        screenshot = ImageGrab.grab((region.left, region.top, region.right, region.bottom))
        haystack = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2GRAY)
        threshold = float(anchor.get("threshold", 0.82))
        matches = []

        for template_path in template_paths:
            path = Path(template_path)
            if not path.exists():
                log_and_print(f"[AutoBridge] Reaction anchor template not found: {template_path}", "warning")
                continue

            template = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if template is None:
                log_and_print(f"[AutoBridge] Reaction anchor template cannot be loaded: {template_path}", "warning")
                continue

            result = cv2.matchTemplate(haystack, template, cv2.TM_CCOEFF_NORMED)
            ys, xs = np.where(result >= threshold)
            if len(xs) == 0:
                log_and_print(f"[AutoBridge] Reaction anchor template found no matches: {template_path}")
                continue

            h, w = template.shape[:2]
            for x, y in zip(xs, ys):
                matches.append({
                    "x": int(region.left + x + w // 2),
                    "y": int(region.top + y + h // 2),
                    "score": float(result[y, x]),
                    "template": str(path),
                })

        if not matches:
            log_and_print("[AutoBridge] Reaction anchor templates found no matches.")
            return []

        y_tolerance = max(4, int(anchor.get("dedupe_y_tolerance_px", 28)))
        grouped = []
        for match in sorted(matches, key=lambda item: (-item["score"], item["y"])):
            if any(abs(match["y"] - kept["y"]) <= y_tolerance for kept in grouped):
                continue
            grouped.append(match)

        click_offset_x = int(anchor.get("click_offset_x_px", -140))
        click_offset_y = int(anchor.get("click_offset_y_px", 0))
        min_x = region.left + int(anchor.get("min_click_left_padding_px", 35))
        max_x = region.right - int(anchor.get("max_click_right_padding_px", 35))
        candidates = []
        seen_rows = set()

        for match in sorted(grouped, key=lambda item: item["y"], reverse=True):
            row = int(match["y"] // y_tolerance)
            if row in seen_rows:
                continue
            seen_rows.add(row)

            click_x = min(max(int(match["x"] + click_offset_x), min_x), max_x)
            click_y = int(match["y"] + click_offset_y)
            visual_hash = self.screen_patch_hash(click_x, click_y)
            candidate = {
                "type": "copy",
                "x": click_x,
                "y": click_y,
                "visual_hash": visual_hash,
                "signature": f"copy:{visual_hash}",
                "key": f"copy:{click_x}:{click_y}:{visual_hash}",
            }

            candidate["anchor"] = {
                "type": "reaction",
                "x": match["x"],
                "y": match["y"],
                "score": round(match["score"], 4),
                "template": match.get("template"),
            }
            log_and_print(
                "[AutoBridge] Candidate created directly from reaction anchor without menu probe: "
                f"anchor=({match['x']},{match['y']}), click=({click_x},{click_y}), "
                f"score={match['score']:.3f}"
            )
            candidates.append(candidate)

        return self.dedupe_candidates_by_y(
            list(reversed(candidates)),
            self._setting_int("candidate_y_dedupe_px", 90),
        )

    def reaction_anchor_template_paths(self, anchor):
        paths = []
        raw_paths = anchor.get("template_paths", [])
        if isinstance(raw_paths, str):
            raw_paths = [raw_paths]
        elif not isinstance(raw_paths, list):
            raw_paths = []

        legacy_path = anchor.get("template_path", "")
        for value in [legacy_path, *raw_paths]:
            if not value:
                continue
            value = str(value)
            if value not in paths:
                paths.append(value)
        return paths

    def collect_media_group_candidates(self, region, capture):
        anchor = capture.get("media_group_anchor", {})
        if not bool(anchor.get("enabled", False)):
            return []

        template_path = Path(str(anchor.get("template_path", "")))
        if not template_path.is_file():
            log_and_print(f"[AutoBridge] Media group anchor template not found: {template_path}", "warning")
            return []

        screenshot = ImageGrab.grab((region.left, region.top, region.right, region.bottom))
        haystack = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2GRAY)
        template = cv2.imread(str(template_path), cv2.IMREAD_GRAYSCALE)
        if template is None:
            log_and_print(f"[AutoBridge] Media group anchor template cannot be loaded: {template_path}", "warning")
            return []

        result = cv2.matchTemplate(haystack, template, cv2.TM_CCOEFF_NORMED)
        threshold = float(anchor.get("threshold", 0.86))
        ys, xs = np.where(result >= threshold)
        if len(xs) == 0:
            log_and_print("[AutoBridge] Media group anchor found no matches.")
            return []

        h, w = template.shape[:2]
        tolerance = max(4, int(anchor.get("dedupe_y_tolerance_px", 32)))
        matches = []
        for x, y in zip(xs, ys):
            match = {
                "x": int(region.left + x + w // 2),
                "y": int(region.top + y + h // 2),
                "score": float(result[y, x]),
            }
            if any(abs(match["y"] - kept["y"]) <= tolerance for kept in matches):
                continue
            matches.append(match)

        candidates = []
        for match in sorted(matches, key=lambda item: item["y"]):
            visual_hash = self.screen_patch_hash(match["x"], match["y"])
            candidate = {
                "type": "media_group",
                "x": match["x"],
                "y": match["y"],
                "visual_hash": visual_hash,
                "signature": f"media_group:{visual_hash}",
                "key": f"media_group:{match['x']}:{match['y']}:{visual_hash}",
                "anchor": {
                    "type": "media_group",
                    "x": match["x"],
                    "y": match["y"],
                    "score": round(match["score"], 4),
                    "template": str(template_path),
                },
            }
            log_and_print(
                "[AutoBridge] Media group candidate created from arrow anchor: "
                f"anchor=({match['x']},{match['y']}), score={match['score']:.3f}"
            )
            candidates.append(candidate)
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
            self.sent_store.mark_completed(content_key)
            self.seen_store.mark_completed(content_key)
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
            self.sent_store.mark_completed(content_key)
            self.seen_store.mark_completed(content_key)
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
        if self.sent_store.deliveries.get(content_key, []):
            log_and_print(
                "[AutoBridge] Message has partial deliveries only; not using it as sent boundary: "
                f"{content_key}"
            )
        return False

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
        if message["type"] == "media_group":
            item_keys = [self.message_content_key(item) for item in message.get("items", [])]
            if not item_keys or any(not key for key in item_keys):
                return None
            return f"media_group:{hash_text('|'.join(item_keys))}"
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
        elif message["type"] == "media_group":
            for item in message.get("items", []):
                self.mark_message_content_sent(item)

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
        if message["type"] == "media_group":
            items = message.get("items", [])
            group_key = self.message_content_key(message)
            unique_item_keys = set()
            log_and_print(
                f"[AutoBridge] Sending media group to Telegram target {channel_name}: "
                f"items={len(items)}"
            )
            for index, item in enumerate(items, start=1):
                item_key = self.message_content_key(item)
                if item_key in unique_item_keys:
                    log_and_print(
                        "[AutoBridge] Duplicate media group item blocked before Telegram send: "
                        f"item={index}/{len(items)}, target={channel_name}, key={item_key}",
                        "warning",
                    )
                    continue
                unique_item_keys.add(item_key)
                item_delivery_key = f"{group_key}:item:{index}:{item_key}"
                if self.sent_store.has_delivery(item_delivery_key, channel_name):
                    log_and_print(
                        "[AutoBridge] Media group item already delivered to target; skipping: "
                        f"item={index}/{len(items)}, target={channel_name}, key={item_delivery_key}"
                    )
                    continue
                if not await self.send_one_message_to_target(item, channel_name):
                    log_and_print(
                        "[AutoBridge] Media group item delivery failed: "
                        f"item={index}/{len(items)}, target={channel_name}, key={item_delivery_key}",
                        "warning",
                    )
                    return False
                self.sent_store.mark_delivered(item_delivery_key, channel_name)
                self.seen_store.mark_delivered(item_delivery_key, channel_name)
            return True

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
        probes = self.settings.get("inspect_probe_offsets", [[0, 0], [0, -18], [0, 18], [45, 0]])
        last_items = None
        for probe in probes:
            try:
                dx, dy = probe
            except (TypeError, ValueError):
                dx, dy = 0, 0

            probe_x = int(x + int(dx))
            probe_y = int(y + int(dy))
            visual_hash = self.screen_patch_hash(probe_x, probe_y)

            self.viber.click_in_messages(probe_x, probe_y, button="right")
            cv2.waitKey(self._setting_int("context_menu_open_delay_ms", 350))

            menu_region = self.menu_region_around(probe_x, probe_y)
            menu_items = self.read_context_menu(menu_region)
            action = self.choose_menu_action(menu_items)
            self.close_context_menu(probe_x, probe_y)
            last_items = menu_items

            if not action:
                log_and_print(
                    "[AutoBridge] No actionable OCR menu at probe: "
                    f"x={probe_x}, y={probe_y}, base=({int(x)},{int(y)}), items={menu_items}"
                )
                continue

            candidate = {
                "type": action,
                "x": probe_x,
                "y": probe_y,
                "visual_hash": visual_hash,
                "signature": f"{action}:{visual_hash}",
                "key": f"{action}:{probe_x}:{probe_y}:{visual_hash}",
            }
            log_and_print(f"[AutoBridge] Candidate detected: {candidate}")
            return candidate

        log_and_print(f"[AutoBridge] No actionable OCR menu after probes at x={x}, y={y}; last_items={last_items}")
        return None

    def copy_candidate_after_delay(self, candidate):
        x = candidate["x"]
        y = candidate["y"]
        expected_type = candidate["type"]

        if expected_type == "media_group":
            return self.copy_media_group(candidate)

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

        has_direct_copy = action in ("text", "copy") and bool(
            menu_items.get("isText") or menu_items.get("isCopy")
        )
        if menu_items.get("isSelect") and not has_direct_copy:
            return self.copy_selected_message(candidate, menu_region, menu_items, action)

        if has_direct_copy:
            log_and_print(
                "[AutoBridge] Using direct text copy from the first context menu; "
                "Select is not required."
            )

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
                if self.is_media_placeholder_text(text):
                    log_and_print(
                        f"[AutoBridge] Direct clipboard text looks like media placeholder; not sending as text: {text!r}",
                        "warning",
                    )
                    return None
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

    def copy_media_group(self, candidate):
        anchor = self.settings.get("auto_capture", {}).get("media_group_anchor", {})
        offsets = anchor.get("tile_offsets_px", [])
        minimum_items = max(2, int(anchor.get("minimum_items", 2)))
        if not isinstance(offsets, list) or not offsets:
            log_and_print("[AutoBridge] Media group has no configured tile offsets.", "warning")
            return None

        items = []
        item_keys = set()
        log_and_print(
            "[AutoBridge] Reading media group tiles: "
            f"anchor=({candidate['x']},{candidate['y']}), configured_tiles={len(offsets)}"
        )
        for index, offset in enumerate(offsets, start=1):
            try:
                offset_x, offset_y = (int(value) for value in offset)
            except (TypeError, ValueError):
                log_and_print(f"[AutoBridge] Invalid media group tile offset: {offset}", "warning")
                return None

            tile_x = int(candidate["x"] + offset_x)
            tile_y = int(candidate["y"] + offset_y)
            tile_hash = self.screen_patch_hash(tile_x, tile_y)
            tile_candidate = {
                "type": "copy",
                "x": tile_x,
                "y": tile_y,
                "visual_hash": tile_hash,
                "signature": f"media_group_tile:{tile_hash}",
                "key": f"media_group_tile:{index}:{tile_x}:{tile_y}:{tile_hash}",
            }
            log_and_print(
                "[AutoBridge] Reading media group tile: "
                f"item={index}/{len(offsets)}, point=({tile_x},{tile_y})"
            )
            self.clear_clipboard()
            item = self.copy_candidate_after_delay(tile_candidate)
            pyautogui.press("esc")
            cv2.waitKey(self._setting_int("media_group_tile_wait_ms", 300))
            if not item:
                log_and_print(
                    "[AutoBridge] Media group tile could not be read; group will be retried: "
                    f"item={index}/{len(offsets)}, point=({tile_x},{tile_y})",
                    "warning",
                )
                return None
            item_key = self.message_content_key(item)
            if item_key in item_keys:
                log_and_print(
                    "[AutoBridge] Media group tile copied the same content as an earlier tile; "
                    "aborting the group without sending: "
                    f"item={index}/{len(offsets)}, key={item_key}",
                    "warning",
                )
                return None
            item_keys.add(item_key)
            items.append(item)

        if len(items) < minimum_items:
            log_and_print(
                f"[AutoBridge] Media group is incomplete: items={len(items)}, minimum={minimum_items}",
                "warning",
            )
            return None

        ordered_item_keys = [self.message_content_key(item) for item in items]
        log_and_print(f"[AutoBridge] Media group read successfully: item_keys={ordered_item_keys}")
        return {
            "type": "media_group",
            "items": items,
            "key": f"media_group:{hash_text('|'.join(ordered_item_keys))}",
        }

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
                paths = self.reveal_file_from_viber(candidate, allow_fallback_rect=True)
                return self.message_from_file_paths(paths)

            return None

        self.click_menu_item(copy_menu_region, copy_rect)
        cv2.waitKey(self._setting_int("selected_clipboard_wait_ms", self._setting_int("clipboard_wait_ms", 700)))
        self.save_debug_screenshot("after_selected_copy")

        message = self.message_from_clipboard(expected_type)
        if message:
            return message

        placeholder_type = self.media_placeholder_type(pyperclip.paste())
        if expected_type == "copy" and placeholder_type in ("video", "voice"):
            expected_type = placeholder_type
            log_and_print(
                "[AutoBridge] Selected clipboard marker refined the candidate type: "
                f"type={expected_type}"
            )

        if expected_type in ("file", "video", "voice", "copy"):
            log_and_print(
                "[AutoBridge] Clipboard did not contain file paths after selected Copy; "
                "trying Show in folder fallback.",
                "warning",
            )
            paths = self.reveal_file_from_viber(
                candidate,
                allow_fallback_rect=expected_type in ("file", "video", "voice"),
            )
            file_message = self.message_from_file_paths(paths)
            if file_message:
                return file_message
            if expected_type == "copy":
                log_and_print(
                    "[AutoBridge] Generic selected copy fallback did not resolve a file; "
                    "will retry this candidate later.",
                    "warning",
                )
                return None

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
            if self.is_media_placeholder_text(text):
                log_and_print(
                    f"[AutoBridge] Clipboard text looks like media placeholder; not sending as text: {text!r}",
                    "warning",
                )
                return None
            log_and_print(f"[AutoBridge] Clipboard text copied: chars={len(text)}")
            return {"type": "text", "text": text, "key": f"text:{text}"}

        return None

    def is_media_placeholder_text(self, text):
        return self.media_placeholder_type(text) is not None

    def media_placeholder_type(self, text):
        value = strip_viber_text_header(text) or normalize_text(text)
        value = value.strip().lower()
        placeholders = {
            "видео": "video",
            "відео": "video",
            "video": "video",
            "фото": "image",
            "photo": "image",
            "изображение": "image",
            "зображення": "image",
            "картинка": "image",
            "голосовое сообщение": "voice",
            "голосове повідомлення": "voice",
            "voice message": "voice",
        }
        return placeholders.get(value)

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
        return self.reveal_file_from_viber(candidate, allow_fallback_rect=True)

    def reveal_file_from_viber(self, candidate, allow_fallback_rect=False):
        x = candidate["x"]
        y = candidate["y"]

        self.viber.focus()
        self.viber.click_in_messages(x, y, button="right")
        cv2.waitKey(self._setting_int("context_menu_open_delay_ms", 350))
        self.save_debug_screenshot("video_show_in_folder_menu")

        menu_region = self.menu_region_around(x, y)
        menu_items = self.read_context_menu(menu_region)
        show_rect = menu_items.get("isShowInFolder")
        if not show_rect and allow_fallback_rect:
            show_rect = self.show_in_folder_fallback_rect()
            if show_rect:
                log_and_print(
                    f"[AutoBridge] OCR missed Show in folder; using fallback rect: {show_rect}; "
                    f"items={menu_items}",
                    "warning",
                )
        if not show_rect:
            self.close_context_menu(x, y)
            log_and_print(
                "[AutoBridge] Show in folder was not confirmed; refusing to guess a file "
                f"for candidate type={candidate.get('type')}: {menu_items}",
                "warning",
            )
            return []

        self.click_menu_item(menu_region, show_rect)
        cv2.waitKey(self._setting_int("video_show_in_folder_wait_ms", 5000))
        self.save_debug_screenshot("video_folder_opened")

        paths = self.read_selected_explorer_paths()
        if not paths:
            self.clear_clipboard()
            pyautogui.hotkey("ctrl", "c")
            cv2.waitKey(self._setting_int("video_folder_clipboard_wait_ms", 500))
            paths = self.read_clipboard_file_paths()
        if not paths:
            log_and_print(
                "[AutoBridge] Explorer did not copy the selected file; "
                "the candidate will be retried instead of using an unrelated recent file.",
                "warning",
            )

        if self.settings.get("video_close_folder_after_path", True):
            pyautogui.hotkey("alt", "f4")
            cv2.waitKey(300)
            self.viber.focus()

        return paths

    def read_selected_explorer_paths(self):
        try:
            foreground_handle = int(win32gui.GetForegroundWindow())
            shell = win32com.client.Dispatch("Shell.Application")
            for window in shell.Windows():
                try:
                    if int(window.HWND) != foreground_handle:
                        continue
                    selected = window.Document.SelectedItems()
                    paths = [
                        str(selected.Item(index).Path)
                        for index in range(selected.Count)
                        if selected.Item(index).Path
                    ]
                    paths = [path for path in paths if Path(path).is_file()]
                    log_and_print(f"[AutoBridge] Selected Explorer file paths: {paths}")
                    return paths
                except Exception:
                    continue
        except Exception as exc:
            log_and_print(f"[AutoBridge] Failed to read selected Explorer files: {exc}", "warning")
        return []

    def show_in_folder_fallback_rect(self):
        rect = self.settings.get("show_in_folder_fallback_rect")
        if rect is None:
            rect = self.settings.get("context_menu", {}).get("show_in_folder_fallback_rect")
        if not rect or len(rect) != 4:
            return None
        try:
            return tuple(int(value) for value in rect)
        except (TypeError, ValueError):
            log_and_print(f"[AutoBridge] Invalid show_in_folder_fallback_rect setting: {rect}", "warning")
            return None

    def clear_clipboard(self):
        try:
            win32clipboard.OpenClipboard()
            win32clipboard.EmptyClipboard()
        except Exception as exc:
            log_and_print(f"[AutoBridge] Failed to clear clipboard: {exc}", "warning")
        finally:
            try:
                win32clipboard.CloseClipboard()
            except Exception:
                pass

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
            self.prune_debug_screenshots()
            log_and_print(f"[AutoBridge] Debug screenshot saved: {path}")
            return str(path)
        except Exception as exc:
            log_and_print(f"[AutoBridge] Failed to save debug screenshot {label}: {exc}", "warning")
            return None

    def prune_debug_screenshots(self):
        max_files = max(1, self._setting_int("debug_screenshot_max_files", 100))
        screenshots = sorted(
            self.debug_dir.glob("*.png"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for path in screenshots[max_files:]:
            try:
                path.unlink()
            except OSError as exc:
                log_and_print(f"[AutoBridge] Failed to remove old debug screenshot {path}: {exc}", "warning")

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
        if menu_items.get("isSelect"):
            return "copy"
        return None

    def actions_compatible(self, expected, actual):
        if expected == actual:
            return True
        if expected == "copy" and actual in ("text", "image", "copy", "file", "video", "voice", "link"):
            return True
        if actual == "copy" and expected in ("text", "image", "copy", "file", "video", "voice", "link"):
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

    def _marker_stop_after_sent_count(self):
        return max(1, self._setting_int("marker_stop_after_sent_count", 3))

    def _cycle_pause_seconds(self):
        default_delay = max(1, self._read_delay() // 2)
        return max(1, self._setting_int("cycle_pause_seconds", default_delay))

    def _bottom_control_passes(self):
        return max(0, self._setting_int("bottom_control_passes", 1))

    def _marker_fallback_after_pages(self):
        return max(1, self._setting_int("marker_fallback_after_pages", 40))

    def _bottom_same_content_pages(self):
        return max(2, self._setting_int("bottom_same_content_pages", 2))

    def _page_duplicate_content_stop_after(self):
        return max(1, self._setting_int("page_duplicate_content_stop_after", 1))

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
        if settings.get("runtime_artifact_cleanup_on_start", True):
            cleanup = cleanup_runtime_artifacts()
            log_and_print(
                "[AutoBridge] Runtime artifacts cleaned: "
                f"directories={cleanup['directories']}, files={cleanup['files']}, "
                f"bytes={cleanup['bytes']}"
            )
        bridge = AutoBridge(bot_client, name_viber, channel_names, settings)
        await bridge.run()
    except asyncio.CancelledError:
        log_and_print("[AutoBridge] Stopped by user.")
    except KeyboardInterrupt:
        log_and_print("[AutoBridge] Stopped by user.")
    except Exception as exc:
        log_and_print(f"[AutoBridge] Fatal error: {exc}", "error")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log_and_print("[AutoBridge] Stopped by user.")
