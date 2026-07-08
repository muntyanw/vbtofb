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

    def connect(self):
        self._ensure_window_available()
        handle = self._find_main_window_handle()
        self.app = Application(backend="uia").connect(handle=handle)
        self.window = self.app.window(handle=handle)
        self.arrange_window()
        self.focus()
        return self

    def focus(self):
        try:
            self.window.restore()
        except Exception:
            pass
        try:
            win32gui.ShowWindow(self.window.handle, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(self.window.handle)
        except Exception:
            pass
        try:
            self.window.set_focus()
        except Exception:
            pass
        time.sleep(0.2)

    def arrange_window(self):
        placement = self.settings.get("viber_window_placement", "left_half")
        if placement != "left_half":
            return

        screen_width = win32api.GetSystemMetrics(0)
        screen_height = win32api.GetSystemMetrics(1)
        width = screen_width // 2
        height = screen_height - int(self.settings.get("taskbar_reserved_height", 48))
        try:
            win32gui.ShowWindow(self.window.handle, win32con.SW_RESTORE)
            win32gui.SetWindowPos(self.window.handle, None, 0, 0, width, height, 0)
            log_and_print(f"[ViberWindow] arranged left half: 0,0,{width},{height}")
        except Exception as exc:
            log_and_print(f"[ViberWindow] failed to arrange window: {exc}", "warning")

    def rect(self):
        rect = self.window.rectangle()
        return ScreenRegion(rect.left, rect.top, rect.width(), rect.height())

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
        self.scroll(amount=amount, wheel_dist=-5)

    def scroll(self, amount=5, wheel_dist=-5):
        region = self.messages_region()
        x = region.left + region.width // 2
        y = region.top + region.height // 2
        for _ in range(max(1, amount)):
            mouse.scroll(coords=(x, y), wheel_dist=wheel_dist)

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

        exe_path = self.settings.get("viber_exe_path") or str(
            Path.home() / "AppData" / "Local" / "Viber" / "Viber.exe"
        )
        if Path(exe_path).exists():
            log_and_print(f"[ViberWindow] Открываю Viber: {exe_path}")
            subprocess.Popen([exe_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        timeout = time.monotonic() + int(self.settings.get("viber_open_timeout_seconds", 20))
        while time.monotonic() < timeout:
            try:
                self._find_main_window_handle()
                return
            except RuntimeError:
                pass
            time.sleep(1)

        raise RuntimeError("Окно Viber не найдено. Проверьте, что Viber установлен и запускается.")

    def _find_main_window_handle(self):
        candidates = []
        for window in Desktop(backend="uia").windows():
            if not self._is_viber_window(window):
                continue
            rect = window.rectangle()
            area = rect.width() * rect.height()
            if area > 100000:
                candidates.append((area, window))

        if not candidates:
            raise RuntimeError("Основное окно Viber не найдено.")

        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1].handle

    def _is_viber_window(self, window):
        try:
            _, pid = win32process.GetWindowThreadProcessId(window.handle)
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

        title = window.window_text().lower()
        class_name = (window.element_info.class_name or "").lower()
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
