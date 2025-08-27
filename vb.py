
import signal
from pynput import keyboard  # Для перехвата нажатия клавиш
import threading  # Для запуска слушателя клавиатуры в отдельном потоке
from screen_region import *
from recognize_text import capture_and_recognize
from find_message import *
import time
import asyncio
from browser_utils import sendOneMessagessToFb
from tg import send_message_to_tg_channel, startTgClient
from log import log_and_print

old_text = ""
# Глобальный флаг для предотвращения двойной реакции
processed_messages = set()
# Семафор для последовательной обработки сообщений
processing_semaphore = asyncio.Semaphore(1)

async def process_one_message(message_text, bot_client, service_channel_name, name_viber):

    log_and_print(f"bot_client: {bot_client}", 'info')
    log_and_print(f"service_channel_name: {service_channel_name}", 'info')
    log_and_print(f"name_viber: {name_viber}", 'info')
    # Добавляем ID сообщения в список обработанных
    processed_messages.add(message_text)

    # Обрабатываем сообщение последовательно с использованием семафора
    async with processing_semaphore:
        try:
            log_and_print(f'Обработка сообщения: {message_text}', 'info')

            await send_message_to_tg_channel(bot_client, service_channel_name,
                                             f"Відправляєм повідомлення з Вайбера {name_viber}  - {message_text} - цикл по группам почався.")

            sendOneMessagessToFb(message_text)

            await send_message_to_tg_channel(bot_client, service_channel_name, f"Вайбер {name_viber} - Цикл по групам закінчен.")

        except Exception as e:
            log_and_print(f"Oшибка при обработке одного сообщения: {e}", 'error')
            await asyncio.sleep(10)  # Задержка

async def main():

    global bot_client
    new_text = ""

    log_and_print("Запуск программы")

    # Load initial region from JSON
    left, top, width, height = read_region_from_json()
    region = [left, top, width, height]
    log_and_print(f"Область для захвата: {region}")

    # Draw the initial rectangle on screen
    root = draw_rectangle_on_screen(*region)

    # Prompt user to position Viber under the rectangle
    print(
        "Подгоните окно Viber под красный прямоугольник и нажмите 'Enter' в консоли, или введите 'r' и нажмите 'Enter' для выбора новой области.")
    user_input = input().strip().lower()

    # Handle region selection logic
    if user_input == 'r':
        root.destroy()
        left, top, width, height = select_region()
        region[:] = [left, top, width, height]
        save_region_to_json(left, top, width, height)
        root = draw_rectangle_on_screen(*region)
        print("Подгоните окно Viber под новый красный прямоугольник и нажмите 'Enter' в консоли.")
        input()
    root.destroy()
    log_and_print("Окно с прямоугольником закрыто.")

    # Reset current text after region update
    old_text = load_previous_text()
    log_and_print(f"[main] old_text: {old_text}.")

    running = True
    region_lock = threading.Lock()

    def signal_handler(sig, frame):
        nonlocal running
        log_and_print("Получен сигнал завершения. Остановка программы.")
        running = False

    signal.signal(signal.SIGINT, signal_handler)

    # Define function to handle 'r' key press for region update
    def on_press(key):
        try:
            if key.char == 'r':
                log_and_print("Нажата клавиша 'r' для изменения области экрана.")
                with region_lock:
                    left, top, width, height = select_region()
                    region[:] = [left, top, width, height]
                    save_region_to_json(left, top, width, height)
                    root = draw_rectangle_on_screen(*region)
                    print("Подгоните окно Viber под новый красный прямоугольник и нажмите 'Enter' в консоли.")
                    input()
                    root.destroy()
                    log_and_print("Окно с новым прямоугольником закрыто.")
                    time.sleep(3)
                    # Reset current text after region update
                    old_text = capture_and_recognize(region)
                    log_and_print(f"[main] old_text: {old_text}.")
                    save_current_text(old_text)

        except AttributeError:
            pass  # Ignore non-character key presses

    bot_client, name_viber, service_channel_name = await startTgClient()

    # Start keyboard listener in a separate thread
    listener = keyboard.Listener(on_press=on_press)
    listener.start()

    try:
        while running:
            with region_lock:
                # Capture and recognize text from the selected region
                new_text = capture_and_recognize(region)

            if new_text and new_text != old_text:
                log_and_print("Обнаружено изменение в тексте.")
                added_text = find_addition(old_text, new_text)
                if added_text:
                    log_and_print(f"Отправка нового текста в Facebook: {added_text}")
                    await process_one_message(added_text, bot_client, service_channel_name, name_viber)
                else:
                    log_and_print("Не удалось определить добавленный текст.")

                old_text = new_text
                save_current_text(old_text)

            else:
                log_and_print("Изменений в тексте не обнаружено.")

            # Delay before the next capture
            time.sleep(5)

    except KeyboardInterrupt:
        log_and_print("Прерывание программы пользователем.")
    except Exception as e:
        log_and_print(f"Произошла ошибка: {e}")
    finally:
        save_current_text(new_text)
        listener.stop()
        log_and_print("Программа завершена.")


if __name__ == '__main__':
    main()
