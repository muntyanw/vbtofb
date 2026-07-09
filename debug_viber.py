import json
import sys
import time
from pathlib import Path

import cv2
import pyautogui
from PIL import ImageGrab

from init import load_json
from log import log_and_print
from pwa import AutoBridge


class DebugBridge(AutoBridge):
    def __init__(self, settings, out_dir):
        super().__init__(None, settings.get("name_viber", "Viber"), [], settings)
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.step = 0

    def shot(self, label, bbox=None):
        self.step += 1
        path = self.out_dir / f"{self.step:02d}_{label}.png"
        image = ImageGrab.grab(bbox=bbox)
        image.save(path)
        log_and_print(f"[Debug] screenshot {label}: {path}")
        return str(path)


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "debug_run"
    scroll_up = parse_int_arg("--scroll-up", 0)
    settings = load_json("settings.json")
    bridge = DebugBridge(settings, out_dir)

    result = {
        "out_dir": out_dir,
        "steps": [],
    }

    bridge.viber.connect()
    pyautogui.press("esc")
    cv2.waitKey(150)
    pyautogui.press("esc")
    cv2.waitKey(150)
    bridge.viber.ensure_channel()

    if scroll_up:
        log_and_print(f"[Debug] scrolling up: {scroll_up}")
        bridge.viber.scroll(amount=scroll_up, wheel_dist=5)
        cv2.waitKey(300)

    bridge.viber.focus()
    result["steps"].append({"name": "window_open", "screenshot": bridge.shot("window_open")})

    region = bridge.viber.messages_region()
    result["messages_region"] = region.as_tuple()
    result["steps"].append({
        "name": "messages_region",
        "region": region.as_tuple(),
        "screenshot": bridge.shot("messages_region", (region.left, region.top, region.right, region.bottom)),
    })

    capture = settings.get("auto_capture", {})
    x_ratios = capture.get("message_click_x_ratios", [capture.get("message_click_x_ratio", 0.22)])
    y = region.bottom - int(capture.get("scan_bottom_skip_px", 12))

    for index, x_ratio in enumerate(x_ratios):
        x = region.left + int(region.width * float(x_ratio))
        log_and_print(f"[Debug] right click probe index={index}, x={x}, y={y}, ratio={x_ratio}")

        bridge.viber.click_in_messages(x, y, button="right")
        cv2.waitKey(int(settings.get("context_menu_open_delay_ms", 350)))
        result["steps"].append({
            "name": f"context_menu_{index}",
            "x": x,
            "y": y,
            "screenshot": bridge.shot(f"context_menu_{index}"),
        })

        menu_region = bridge.menu_region_around(x, y)
        result["steps"].append({
            "name": f"menu_region_{index}",
            "region": menu_region,
            "screenshot": bridge.shot(
                f"menu_region_{index}",
                (
                    menu_region[0],
                    menu_region[1],
                    menu_region[0] + menu_region[2],
                    menu_region[1] + menu_region[3],
                ),
            ),
        })

        menu_items = bridge.read_context_menu(menu_region)
        action = bridge.choose_menu_action(menu_items)
        bridge.close_context_menu(x, y)

        result["steps"].append({
            "name": f"ocr_result_{index}",
            "menu_items": menu_items,
            "action": action,
        })

        if action:
            candidate = bridge.inspect_candidate_at(x, y)
            result["candidate"] = candidate
            if candidate:
                result["steps"].append({
                    "name": "before_copy",
                    "screenshot": bridge.shot("before_copy"),
                })
                copied = bridge.copy_candidate_after_delay(candidate)
                result["copied"] = summarize_copied(copied)
                result["steps"].append({
                    "name": "after_copy",
                    "screenshot": bridge.shot("after_copy"),
                })
            break

        time.sleep(0.3)

    result_path = Path(out_dir) / "result.json"
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log_and_print(f"[Debug] result saved: {result_path}")


def parse_int_arg(name, default):
    if name not in sys.argv:
        return default
    index = sys.argv.index(name)
    if index + 1 >= len(sys.argv):
        return default
    try:
        return int(sys.argv[index + 1])
    except ValueError:
        return default


def summarize_copied(copied):
    if not copied:
        return None
    if copied["type"] == "text":
        return {
            "type": "text",
            "chars": len(copied.get("text", "")),
            "preview": copied.get("text", "")[:200],
        }
    if copied["type"] == "image":
        image = copied["image"]
        return {
            "type": "image",
            "size": image.size,
            "image_hash": copied.get("image_hash"),
        }
    if copied["type"] == "video":
        return {
            "type": "video",
            "file_path": copied.get("file_path"),
            "file_hash": copied.get("file_hash"),
        }
    return {"type": copied.get("type")}


if __name__ == "__main__":
    main()
