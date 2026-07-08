import asyncio
import re

from pywinauto import Application, mouse

from log import log_and_print
from tg import send_message_to_tg_channel


def scroll_with_mouse(window, count_scroll):
    window.set_focus()
    chat_pane = window.child_window(control_type="Pane", found_index=0)
    rect = chat_pane.rectangle()
    center_x = (rect.left + rect.right) // 2
    center_y = (rect.top + rect.bottom) // 2

    for _ in range(count_scroll):
        mouse.scroll(coords=(center_x, center_y), wheel_dist=-1)
        log_and_print("Scrolled down with mouse wheel")


def right_click_on_panel(x_offset=0, y_offset=0):
    app = Application(backend="uia").connect(title_re=".*Viber.*")
    window = app.window(title_re=".*Viber.*")
    window.set_focus()
    chat_pane = window.child_window(control_type="Pane", found_index=0)
    rect = chat_pane.rectangle()
    center_x = (rect.left + rect.right) // 2 + x_offset
    center_y = (rect.top + rect.bottom) // 2 + y_offset
    mouse.click(button="right", coords=(center_x, center_y))
    log_and_print(f"Right-clicked at ({center_x}, {center_y}) on the chat panel")
    return center_x, center_y


def right_click(x=0, y=0):
    mouse.click(button="right", coords=(x, y))
    log_and_print(f"Right-clicked at ({x}, {y}) on the chat panel")


def left_click(x=0, y=0):
    mouse.click(button="left", coords=(x, y))
    log_and_print(f"Left-clicked at ({x}, {y}) on the chat panel")


processed_messages = set()
processing_semaphore = asyncio.Semaphore(1)


async def process_one_message(message_text, bot_client, channel_name, name_viber, image_path):
    log_and_print(f"bot_client: {bot_client}", "info")
    log_and_print(f"service_channel_name: {channel_name}", "info")
    log_and_print(f"name_viber: {name_viber}", "info")

    async with processing_semaphore:
        try:
            log_and_print(f"Processing message: {message_text}", "info")
            sent = await send_message_to_tg_channel(
                bot_client,
                channel_name,
                message_text,
                image_path,
            )
            if sent:
                processed_messages.add(message_text)
            return sent
        except Exception as exc:
            log_and_print(f"Error processing one message: {exc}", "error")
            await asyncio.sleep(10)
            return False


def reformat_telegram_text(input_text):
    pattern = r"\*(.*?)\*"
    return re.sub(pattern, r"**\1**", input_text)
