from telethon import TelegramClient, events
from browser_utils import sendOneMessagessToFb
from log import log_and_print  # Импорт функции логирования
import asyncio
from telethon.errors import RPCError
from telethon import TelegramClient
from utils import read_setting, load_json

# Глобальный флаг для предотвращения двойной реакции
processed_messages = set()
# Семафор для последовательной обработки сообщений
processing_semaphore = asyncio.Semaphore(1)

telegram_channel_name = None
telegram_channel_id = None

async def send_message_to_tg_channel(bot_client, channel_name, message_text, image_path=None):
    try:

        # Получаем объект канала
        channel_entity = await bot_client.get_entity(channel_name)

        # Отправка сообщения с изображением или только текста
        if image_path:
            await bot_client.send_file(
                channel_entity,
                image_path,
                caption=message_text[:1024]  # Обрезаем подпись до допустимой длины
            )
        else:
            await bot_client.send_message(
                channel_entity,
                message_text
            )

        log_and_print(f"Сообщение успешно отправлено в канал: {channel_name}", 'info')

    except RPCError as e:
        log_and_print(f"Ошибка при отправке сообщения в канал: {e}", 'error')
    except Exception as e:
        log_and_print(f"Непредвиденная ошибка при отправке сообщения в канал: {e}", 'error')

async def check_connection(bot_client):
    while True:
        if not bot_client.is_connected():
            try:
                log_and_print("Потеряно подключение. Переподключение...", 'warning')
                await bot_client.connect()
                if bot_client.is_user_authorized():
                    log_and_print("Подключение восстановлено.", 'info')
                else:
                    log_and_print("Авторизация потеряна. Необходимо перезайти.", 'error')
            except Exception as e:
                log_and_print(f"Ошибка при попытке переподключения: {e}", 'error')
        await asyncio.sleep(10)  # Проверяем каждые 10 секунд

async def start_listening(bot_client):
    while True:
        try:
            log_and_print('Начинаем прослушивание канала Telegram...', 'info')
            await bot_client.run_until_disconnected()
        except Exception as e:
            log_and_print(f"Ошибка при прослушивании Telegram: {e}. Переподключение через 5 секунд...", 'error')
            await asyncio.sleep(5)  # Задержка перед повторной попыткой

async def startTgClient():
    try:
        name_viber = read_setting('name_viber')
        log_and_print(f"name_viber:{name_viber}")
        service_channels = read_setting('service_tg_channels')
        if not service_channels:
            log_and_print("Список сервісних каналів пуст.", 'warning')
            return
        service_channel_data = service_channels[0]
        service_channel_name = service_channel_data.get('service_channel_name')
        log_and_print(f"service_channel_name:{service_channel_name}")

        creds = load_json('creds.json')
        tg_creds = creds.get('tg_creds', {})
        log_and_print(f"tg_creds {tg_creds}.", 'info')

        bot_token = tg_creds.get('bot_token')
        api_id = tg_creds.get('api_id')
        api_hash = tg_creds.get('api_hash')

        #sendOneMessagessToFb("", fb_creds, fb_groups, image_path="E:\\ttofb\\downloads\\31.jpg")

        if not (service_channel_name and bot_token and api_id and api_hash):
            log_and_print("Недостаточно данных для подключения к Telegram.", 'error')
            return None

        # Создаем клиент Telethon
        bot_client = TelegramClient('bot_session', api_id, api_hash)

        while True:
            try:
                log_and_print(f"Запуск клиента с токеном: {bot_token}", 'info')
                await bot_client.start(bot_token=bot_token)
                log_and_print("Клиент успешно запущен.", 'info')

                # Получаем информацию о боте
                bot_info = await bot_client.get_me()

                log_and_print(f"Имя бота: {bot_info.first_name}", 'info')
                log_and_print(f"Юзернейм бота: {bot_info.username}", 'info')

                break  # Выход из цикла, если подключение успешно
            except RPCError as e:
                log_and_print(f"Ошибка подключения к Telegram: {e}. Повторная попытка через 10 секунд...", 'error')
                await asyncio.sleep(10)  # Задержка перед повторной попыткой
            except Exception as e:
                log_and_print(f"Непредвиденная ошибка: {e}. Повторная попытка через 10 секунд...", 'error')
                await asyncio.sleep(10)  # Задержка

    except Exception as e:
        log_and_print(f"Ошибка при запуске клиента: {e}", 'error')

    return  bot_client, name_viber, service_channel_name