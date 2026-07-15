from dataclasses import dataclass
from pathlib import Path
import subprocess
import time

import cv2
import numpy as np
from PIL import ImageGrab
from pywinauto import Application, Desktop, mouse
from pywinauto.findwindows import ElementNotFoundError
import win32api
import win32con
import win32gui
import win32process

from log import log_and_print


@dataclass
class ScreenRegion:
    left: int
    top: int
    width: int
    height: int

    @property
    def right(self):
        return self.left + self.width

    @property
    def bottom(self):
        return self.top + self.height

    def as_tuple(self):
        return self.left, self.top, self.width, self.height


class ViberWindow:
    def __init__(self, settings):
        self.settings = settings
        self.app = None
        self.window = None
        self.handle = None

    def connect(self):
        self._ensure_window_available()
        handle = self._find_main_window_handle()
        self.handle = handle
        self.app = Application(backend="uia").connect(handle=handle)
        self.window = self.app.window(handle=handle)
        self.arrange_window()
        self.focus()
        return self

    def focus(self):
        self.ensure_connected()
        try:
            if self.window is not None:
                self.window.restore()
        except Exception:
            pass
        try:
            win32gui.ShowWindow(self.handle, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(self.handle)
        except Exception:
            pass
        try:
            if self.window is not None:
                self.window.set_focus()
        except Exception:
            pass
        time.sleep(0.2)

    def arrange_window(self):
        self.ensure_connected()
        placement = self.settings.get("viber_window_placement", "left_half")
        if placement != "left_half":
            return

        screen_width = win32api.GetSystemMetrics(0)
        screen_height = win32api.GetSystemMetrics(1)
        width = screen_width // 2
        height = screen_height - int(self.settings.get("taskbar_reserved_height", 48))
        try:
            win32gui.ShowWindow(self.handle, win32con.SW_RESTORE)
            win32gui.SetWindowPos(self.handle, None, 0, 0, width, height, 0)
            log_and_print(f"[ViberWindow] arranged left half: 0,0,{width},{height}")
        except Exception as exc:
            log_and_print(f"[ViberWindow] failed to arrange window: {exc}", "warning")

    def rect(self):
        for _ in range(2):
            try:
                self.ensure_connected()
                left, top, right, bottom = win32gui.GetWindowRect(self.handle)
                return ScreenRegion(left, top, max(1, right - left), max(1, bottom - top))
            except Exception as exc:
                log_and_print(f"[ViberWindow] failed to read rect; reconnecting: {exc}", "warning")
                self.reconnect()
        raise RuntimeError("Cannot read Viber window rectangle after reconnect.")

    def messages_region(self):
        rect = self.rect()
        capture = self.settings.get("auto_capture", {})

        left_sidebar_width = int(capture.get("left_sidebar_width", 320))
        header_height = int(capture.get("header_height", 92))
        input_height = int(capture.get("input_height", 110))
        right_padding = int(capture.get("right_padding", 18))
        top_padding = int(capture.get("top_padding", 0))
        bottom_padding = int(capture.get("bottom_padding", 0))

        left = rect.left + left_sidebar_width
        top = rect.top + header_height + top_padding
        right = rect.right - right_padding
        bottom = rect.bottom - input_height - bottom_padding

        region = ScreenRegion(left, top, max(1, right - left), max(1, bottom - top))
        log_and_print(f"[ViberWindow] messages_region={region.as_tuple()}")
        return region

    def ensure_channel(self):
        channel = self.settings.get("viber_channel", {})
        title = channel.get("title")
        logo_template = channel.get("logo_template")

        if title and self._activate_channel_by_title(title):
            log_and_print(f"[ViberWindow] Канал найден по названию: {title}")
            return True

        if logo_template and self._match_logo(logo_template):
            log_and_print(f"[ViberWindow] Канал подтвержден по логотипу: {logo_template}")
            return True

        if title or logo_template:
            log_and_print("[ViberWindow] Не удалось подтвердить нужный канал.", "warning")
            return False

        log_and_print("[ViberWindow] Канал не задан в настройках, используется текущий открытый чат.")
        return True

    def click_in_messages(self, x, y, button="right"):
        self.focus()
        mouse.click(button=button, coords=(int(x), int(y)))

    def scroll_to_bottom(self, amount=5):
        if self.click_scroll_to_bottom():
            return True
        anchor = self.settings.get("auto_capture", {}).get("scroll_bottom_anchor", {})
        if bool(anchor.get("wheel_fallback_enabled", False)):
            self.scroll(amount=amount, wheel_dist=-5)
        else:
            log_and_print("[ViberWindow] Scroll-bottom button is absent; list is assumed to be at bottom.")
        return False

    def click_scroll_to_bottom(self):
        anchor = self.settings.get("auto_capture", {}).get("scroll_bottom_anchor", {})
        if not bool(anchor.get("enabled", True)):
            return False

        template_path = Path(str(anchor.get("template_path", "images/viber_scroll_bottom_anchor.png")))
        if not template_path.is_file():
            log_and_print(f"[ViberWindow] Scroll-bottom anchor template not found: {template_path}", "warning")
            return False

        self.focus()
        window_region = self.rect()
        search_width = max(60, int(anchor.get("search_width_px", 120)))
        search_height = max(80, int(anchor.get("search_height_px", 150)))
        search_left = max(window_region.left, window_region.right - search_width)
        search_top = max(window_region.top, window_region.bottom - search_height)
        screenshot = ImageGrab.grab((search_left, search_top, window_region.right, window_region.bottom))
        haystack = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2GRAY)
        template = cv2.imread(str(template_path), cv2.IMREAD_GRAYSCALE)
        if template is None or haystack.shape[0] < template.shape[0] or haystack.shape[1] < template.shape[1]:
            log_and_print("[ViberWindow] Scroll-bottom anchor image is not usable.", "warning")
            return False

        result = cv2.matchTemplate(haystack, template, cv2.TM_CCOEFF_NORMED)
        _, score, _, location = cv2.minMaxLoc(result)
        threshold = float(anchor.get("threshold", 0.86))
        if score < threshold:
            log_and_print(
                "[ViberWindow] Scroll-bottom button not found; using wheel fallback: "
                f"score={score:.3f}, threshold={threshold:.3f}"
            )
            return False

        click_x = search_left + location[0] + template.shape[1] // 2
        click_y = search_top + location[1] + template.shape[0] // 2
        mouse.click(button="left", coords=(click_x, click_y))
        time.sleep(max(0, int(anchor.get("click_wait_ms", 800))) / 1000)
        log_and_print(
            "[ViberWindow] Scroll-bottom button clicked: "
            f"coords=({click_x},{click_y}), score={score:.3f}"
        )
        return True

    def scroll(self, amount=5, wheel_dist=-5):
        region = self.messages_region()
        x = region.left + region.width // 2
        y = region.top + region.height // 2
        for _ in range(max(1, amount)):
            mouse.scroll(coords=(x, y), wheel_dist=wheel_dist)

    def ensure_connected(self):
        if self.handle and win32gui.IsWindow(self.handle):
            return
        self.reconnect()

    def reconnect(self):
        self._ensure_window_available()
        handle = self._find_main_window_handle()
        self.handle = handle
        try:
            self.app = Application(backend="uia").connect(handle=handle)
            self.window = self.app.window(handle=handle)
        except Exception as exc:
            log_and_print(f"[ViberWindow] UIA reconnect warning: {exc}", "warning")
            self.app = None
            self.window = None
        self.arrange_window()
        return self

    def _activate_channel_by_title(self, title):
        try:
            for item in self.window.descendants():
                item_text = ""
                try:
                    item_text = item.window_text()
                except Exception:
                    pass

                if item_text and title.lower() in item_text.lower():
                    item.click_input()
                    self.focus()
                    return True
        except Exception as exc:
            log_and_print(f"[ViberWindow] Ошибка поиска канала по названию: {exc}", "warning")
        return False

    def _ensure_window_available(self):
        try:
            self._find_main_window_handle()
            return
        except (ElementNotFoundError, RuntimeError):
            pass

        hidden_handle = self._find_hidden_main_window_handle()
        if hidden_handle:
            log_and_print(f"[ViberWindow] Восстанавливаю окно Viber из трея: handle={hidden_handle}")
            self._show_window(hidden_handle)
            if self._wait_for_visible_window(3):
                return

        exe_path = self.settings.get("viber_exe_path") or str(
            Path.home() / "AppData" / "Local" / "Viber" / "Viber.exe"
        )
        if Path(exe_path).exists():
            log_and_print(f"[ViberWindow] Открываю Viber: {exe_path}")
            subprocess.Popen([exe_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        if self._wait_for_visible_window(int(self.settings.get("viber_open_timeout_seconds", 20))):
            return

        raise RuntimeError("Окно Viber не найдено и не удалось восстановить из трея.")

    def _wait_for_visible_window(self, timeout_seconds):
        timeout = time.monotonic() + max(1, int(timeout_seconds))
        while time.monotonic() < timeout:
            try:
                self._find_main_window_handle()
                return True
            except RuntimeError:
                hidden_handle = self._find_hidden_main_window_handle()
                if hidden_handle:
                    self._show_window(hidden_handle)
            time.sleep(1)
        return False

    def _find_hidden_main_window_handle(self):
        candidates = self._collect_main_window_handles(include_hidden=True)
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    def _show_window(self, handle):
        try:
            win32gui.ShowWindow(handle, win32con.SW_SHOW)
            win32gui.ShowWindow(handle, win32con.SW_RESTORE)
            return True
        except Exception as exc:
            log_and_print(f"[ViberWindow] Не удалось восстановить скрытое окно: {exc}", "warning")
            return False

    def _find_main_window_handle(self):
        candidates = self._collect_main_window_handles(include_hidden=False)
        if not candidates:
            raise RuntimeError("Основное окно Viber не найдено.")

        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    def _collect_main_window_handles(self, include_hidden=False):
        candidates = []

        def collect(handle, _):
            if not self._is_viber_handle(handle, include_hidden=include_hidden):
                return
            try:
                left, top, right, bottom = win32gui.GetWindowRect(handle)
                area = max(0, right - left) * max(0, bottom - top)
            except Exception:
                return
            if area > 100000:
                candidates.append((area, handle))

        win32gui.EnumWindows(collect, None)
        return candidates

    def _is_viber_window(self, window):
        return self._is_viber_handle(window.handle)

    def _is_viber_handle(self, handle, include_hidden=False):
        if not include_hidden and not win32gui.IsWindowVisible(handle):
            return False
        try:
            _, pid = win32process.GetWindowThreadProcessId(handle)
            process = win32api.OpenProcess(
                win32con.PROCESS_QUERY_LIMITED_INFORMATION | win32con.PROCESS_VM_READ,
                False,
                pid,
            )
            try:
                path = win32process.GetModuleFileNameEx(process, 0)
            finally:
                win32api.CloseHandle(process)
        except Exception:
            return False

        if Path(path).name.lower() != "viber.exe":
            return False

        title = win32gui.GetWindowText(handle).lower()
        class_name = win32gui.GetClassName(handle).lower()
        return "rakuten viber" in title or "mainwindow_qml" in class_name

    def _match_logo(self, template_path):
        path = Path(template_path)
        if not path.exists():
            log_and_print(f"[ViberWindow] Файл логотипа не найден: {template_path}", "warning")
            return False

        threshold = float(self.settings.get("viber_channel", {}).get("logo_match_threshold", 0.82))
        rect = self.rect()
        screenshot = ImageGrab.grab((rect.left, rect.top, rect.right, rect.bottom))
        haystack = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2GRAY)
        template = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)

        if template is None:
            return False

        result = cv2.matchTemplate(haystack, template, cv2.TM_CCOEFF_NORMED)
        _, max_value, _, max_location = cv2.minMaxLoc(result)
        log_and_print(f"[ViberWindow] logo match={max_value:.3f}, location={max_location}")
        return max_value >= threshold
