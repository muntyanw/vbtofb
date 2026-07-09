from log import log_and_print  # Импорт функции логирования
import asyncio
from datetime import datetime
from pathlib import Path
import traceback
from telethon.errors import RPCError
from telethon import TelegramClient
from init import init

# Глобальный флаг для предотвращения двойной реакции
processed_messages = set()
# Семафор для последовательной обработки сообщений
processing_semaphore = asyncio.Semaphore(1)

telegram_channel_name = None
telegram_channel_id = None

async def send_message_to_tg_channel(bot_client, channel_name, message_text, image_path=None):
    try:
        channel_name = normalize_channel_target(channel_name)
        if is_invite_link_target(channel_name):
            log_and_print(
                "Telegram target is an invite link/hash, not a sendable chat entity. "
                "Add the bot to that group and use its numeric chat id (-100...), "
                "or use a public @username.",
                "error",
            )
            return False

        message_text = str(message_text or "")
        if message_text:
            log_and_print(
                f"Telegram payload preview: {message_text[:120]!r}; "
                f"unicode={message_text[:120].encode('unicode_escape').decode('ascii')}",
                "info",
            )
            if looks_like_broken_encoding(message_text):
                log_and_print(
                    "Telegram send blocked: text looks encoding-corrupted before send.",
                    "error",
                )
                return False

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
        return True

    except RPCError as e:
        log_and_print(f"Ошибка при отправке сообщения в канал: {e}", 'error')
        return False
    except Exception as e:
        log_and_print(f"Непредвиденная ошибка при отправке сообщения в канал: {e}", 'error')
        return False

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
    bot_client = None
    name_viber = None
    service_channel_name = None
    channel_names = []
    try:
        tg_creds, tg_channels, settings = init()

        name_viber = settings.get('name_viber')
        log_and_print(f"name_viber:{name_viber}")

        channels = tg_channels.get('channels', [])
        if not channels:
            log_and_print("Список каналів пуст.", 'warning')
            return

        channel_names = [channel_target_from_config(channel) for channel in channels]
        for channel_name in channel_names:
            if is_invite_link_target(channel_name):
                log_and_print(
                    f"Telegram channel target {channel_name!r} is an invite link/hash. "
                    "It cannot be used for bot sending; use numeric chat id (-100...) "
                    "or a public username.",
                    "warning",
                )

        service_channels = tg_channels.get('service_channels', [])
        service_channel_data = service_channels[0]
        service_channel_name = service_channel_data.get('service_channel_name')

        bot_token = tg_creds.get('bot_token')
        api_id = tg_creds.get('api_id')
        api_hash = tg_creds.get('api_hash')


        if not (service_channel_name and bot_token and api_id and api_hash):
            log_and_print("Недостаточно данных для подключения к Telegram.", 'error')
            return None

        # Создаем клиент Telethon
        bot_client = create_telegram_client('bot_session', api_id, api_hash)

        while True:
            try:
                log_and_print(f"Запуск клиента с токеном: {mask_secret(bot_token)}", 'info')
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
        log_and_print(traceback.format_exc(), "error")

    if bot_client is None:
        return None

    return bot_client, name_viber, service_channel_name, channel_names


def mask_secret(value):
    value = str(value or "")
    if len(value) <= 10:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def create_telegram_client(session_name, api_id, api_hash):
    try:
        return TelegramClient(session_name, api_id, api_hash)
    except ValueError as exc:
        if "too many values to unpack" not in str(exc):
            raise

        recovered_session = quarantine_session_files(session_name)
        log_and_print(
            f"Telegram session {session_name!r} was incompatible/corrupt; "
            f"using {recovered_session!r}.",
            "warning",
        )
        return TelegramClient(recovered_session, api_id, api_hash)


def quarantine_session_files(session_name):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    recovered_session = session_name
    for suffix in (".session", ".session-journal"):
        path = Path(f"{session_name}{suffix}")
        if not path.exists():
            continue
        target = path.with_name(f"{path.name}.bad_{stamp}")
        try:
            path.replace(target)
            log_and_print(f"Moved old Telegram session file to {target}", "warning")
        except OSError as exc:
            recovered_session = f"{session_name}_recovered_{stamp}"
            log_and_print(
                f"Could not move old Telegram session file {path}: {exc}. "
                f"Will use new session file {recovered_session}.session",
                "warning",
            )
    return recovered_session


def normalize_channel_target(value):
    if isinstance(value, int):
        return value

    text = str(value or "").strip()
    if not text:
        return text

    lowered = text.lower()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if lowered.startswith(prefix):
            text = text[len(prefix):]
            break

    text = text.split("?", 1)[0].strip("/")
    if text.startswith("@"):
        text = text[1:]

    if text.lstrip("-").isdigit():
        try:
            return int(text)
        except ValueError:
            return text

    return text


def channel_target_from_config(channel):
    for key in ("telegram_chat_id", "telegram_channel_id", "chat_id", "id"):
        if key in channel and channel[key] not in (None, ""):
            return normalize_channel_target(channel[key])

    return normalize_channel_target(channel.get("telegram_channel_name", ""))


def is_invite_link_target(value):
    if isinstance(value, int):
        return False

    text = str(value or "").strip()
    lowered = text.lower()
    if lowered.startswith(("https://t.me/+", "http://t.me/+", "t.me/+")):
        return True
    if lowered.startswith(("https://t.me/joinchat/", "http://t.me/joinchat/", "t.me/joinchat/")):
        return True
    normalized = normalize_channel_target(text)
    return isinstance(normalized, str) and (
        normalized.startswith("+") or normalized.lower().startswith("joinchat/")
    )


def looks_like_broken_encoding(text):
    if not text:
        return False

    question_count = text.count("?")
    if question_count < 3:
        return False

    cyrillic_count = sum(1 for char in text if "\u0400" <= char <= "\u04ff")
    latin_count = sum(1 for char in text if "A" <= char <= "z")
    visible_count = sum(1 for char in text if not char.isspace())
    if visible_count == 0:
        return False

    return question_count / visible_count > 0.3 and cyrillic_count == 0 and latin_count == 0
