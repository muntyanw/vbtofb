from pywinauto import Application, mouse
from log import log_and_print
import asyncio
from tg import send_message_to_tg_channel
import re

def scroll_with_mouse(window, count_scroll):
    window.set_focus()

    # Находим панель с сообщениями
    chat_pane = window.child_window(control_type="Pane", found_index=0)

    # Получаем координаты панели
    rect = chat_pane.rectangle()
    center_x = (rect.left + rect.right) // 2
    center_y = (rect.top + rect.bottom) // 2

    # Прокручиваем вниз
    for _ in range(count_scroll):  # Повторяем несколько раз
        mouse.scroll(coords=(center_x, center_y), wheel_dist=-1)  # Отрицательное значение для скроллинга вниз
        print("Scrolled down with mouse wheel")

def right_click_on_panel(x_offset=0, y_offset=0):
    """
    Кликает правой кнопкой мыши на панели с сообщениями Viber.

    :param x_offset: Смещение по X относительно центра панели.
    :param y_offset: Смещение по Y относительно центра панели.
    """
    # Подключаемся к Viber
    app = Application(backend="uia").connect(title_re=".*Viber.*")
    window = app.window(title_re=".*Viber.*")
    window.set_focus()

    # Находим панель с сообщениями
    chat_pane = window.child_window(control_type="Pane", found_index=0)

    # Получаем координаты панели
    rect = chat_pane.rectangle()
    center_x = (rect.left + rect.right) // 2 + x_offset
    center_y = (rect.top + rect.bottom) // 2 + y_offset

    # Выполняем клик правой кнопкой мыши
    mouse.click(button="right", coords=(center_x, center_y))
    log_and_print(f"Right-clicked at ({center_x}, {center_y}) on the chat panel")
    return center_x, center_y


def right_click(app, window_title, x=0, y=0):
    """
    Устанавливает фокус на окно, а затем кликает правой кнопкой мыши по указанным координатам.

    Args:
        app: экземпляр pywinauto.Application
        window_title: название окна
        x: координата X для клика
        y: координата Y для клика
    """
    try:
        # Подключаемся к окну приложения
        window = app.window(title=window_title)

        # Устанавливаем фокус на окно
        window.set_focus()

        # Выполняем клик правой кнопкой мыши
        mouse.click(button="right", coords=(x, y))

        print(f"Right-clicked at ({x}, {y}) on the window '{window_title}'")
    except pywinauto.findwindows.ElementNotFoundError:
        print(f"Window with title '{window_title}' not found!")
    except Exception as e:
        print(f"Error during right-click: {e}")

def right_click(x=0, y=0):
    """
    Кликает правой кнопкой мыши
    """

    # Выполняем клик правой кнопкой мыши
    mouse.click(button="right", coords=(x, y))
    log_and_print(f"Right-clicked at ({x}, {y}) on the chat panel")

def left_click(x=0, y=0):
    """
    Кликает левой кнопкой мыши
    """

    # Выполняем клик левой кнопкой мыши
    mouse.click(button="left", coords=(x, y))
    log_and_print(f"Left-clicked at ({x}, {y}) on the chat panel")

# Глобальный флаг для предотвращения двойной реакции
processed_messages = set()
# Семафор для последовательной обработки сообщений
processing_semaphore = asyncio.Semaphore(1)

async def process_one_message(message_text, bot_client, channel_name, name_viber, image_path):

    log_and_print(f"bot_client: {bot_client}", 'info')
    log_and_print(f"service_channel_name: {channel_name}", 'info')
    log_and_print(f"name_viber: {name_viber}", 'info')
    # Добавляем ID сообщения в список обработанных
    processed_messages.add(message_text)

    # Обрабатываем сообщение последовательно с использованием семафора
    async with processing_semaphore:
        try:
            log_and_print(f'Обработка сообщения: {message_text}', 'info')

            await send_message_to_tg_channel(bot_client, channel_name, message_text, image_path)

        except Exception as e:
            log_and_print(f"Oшибка при обработке одного сообщения: {e}", 'error')
            await asyncio.sleep(10)  # Задержка


def reformat_telegram_text(input_text):
    """
    Takes a text, finds all text enclosed in single asterisks (*),
    and replaces them with double asterisks (**) for Telegram formatting.

    Args:
        input_text (str): The input text.

    Returns:
        str: The modified text with updated formatting.
    """
    # Regular expression to find text enclosed in single asterisks
    pattern = r'\*(.*?)\*'

    # Replace single asterisks with double asterisks
    formatted_text = re.sub(pattern, r'**\1**', input_text)

    return formatted_text
