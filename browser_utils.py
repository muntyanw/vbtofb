import sys

from selenium import webdriver
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from log import log_and_print
import time
import os
from selenium.common.exceptions import TimeoutException
import markdown
from selenium.webdriver.common.action_chains import ActionChains
import random
from utils import read_setting, load_json

CHROME_PORT = 9222  # Порт для отладки
CHROME_DATA_DIR = os.path.abspath("./chrome-data")  # Каталог для данных пользователя
drivers = {}

def get_random_value(array):
    if not array:  # Проверка на пустой массив
        return None
    return random.choice(array)

def convert_markdown_to_html(markdown_text):
    # Конвертация Markdown в HTML
    html_text = markdown.markdown(markdown_text)
    return html_text

def start_chrome_new(driver_path, chromedriver_port):
    chrome_options = webdriver.ChromeOptions()
    chrome_options.add_argument(f"--remote-debugging-port={chromedriver_port}")
    chrome_options.add_argument(f"--user-data-dir={CHROME_DATA_DIR}")  # Сохранение данных пользователя
    chrome_options.add_argument("--disable-usb")
    chrome_options.add_argument("--disable-usb-keyboard-detect")
    #chrome_options.add_argument("--no-sandbox")
    #chrome_options.add_argument("--disable-gpu")
    #chrome_options.add_argument("--disable-dev-shm-usage")

    try:
        log_and_print("Попитка запустить новая сессия Chrome.", 'info')
        driver = webdriver.Chrome(service=Service(driver_path), options=chrome_options)
        log_and_print("Запущена новая сессия Chrome.", 'info')
        return driver
    except Exception as e:
        log_and_print(f"Ошибка при запуске новой сессии Chrome: {e}", 'error')
        return None

def start_chrome_with_retries(driver_path, chromedriver_port, retries=3):
    """Функция для запуска Chrome с заданным количеством попыток."""
    for attempt in range(retries):
        try:
            driver = start_chrome_new(driver_path, chromedriver_port)
            if driver:
                return driver
        except Exception as e:
            log_and_print(f"Ошибка при запуске Chrome (попытка {attempt + 1}/{retries}): {e}", 'error')
        time.sleep(5)  # Задержка перед повторной попыткой
    log_and_print("Не удалось запустить Chrome после нескольких попыток.", 'error')
    return None

def autorizeFb(driver, login_fb, pass_fb):
    log_and_print("Открытие страницы Facebook...", 'info')
    driver.get('https://www.facebook.com/')
    time.sleep(2)

    try:
        log_and_print("Спроба авторізаціі.", 'info')
        username = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, 'email'))
        )
        password = driver.find_element(By.ID, 'pass')

        log_and_print("Ввод логина и пароля...", 'info')
        username.send_keys(login_fb)
        password.send_keys(pass_fb)
        password.send_keys(Keys.RETURN)

        WebDriverWait(driver, 15).until(
            EC.url_contains("https://www.facebook.com/")
        )
        log_and_print("Авторизация прошла успешно.", 'info')
    except TimeoutException:
        log_and_print("Ошибка авторизации.", 'error')

def remove_non_bmp_characters(text):
    return ''.join(char for char in text if ord(char) <= 0xFFFF)

def publichOneMessage(driver, group_id, message, image_path=None, fone_colors=None):
    group_url = f'https://www.facebook.com/groups/{group_id}'
    log_and_print(f"Переход на страницу группы: {group_url}", 'info')
    driver.get(group_url)
    time.sleep(5)

    log_and_print("Поиск элемента для начала публикации...", 'info')
    post_box_trigger = WebDriverWait(driver, 15).until(
        EC.element_to_be_clickable((By.XPATH, "//*[text()='Напишите что-нибудь...']"))
    )
    post_box_trigger.click()
    log_and_print("Элемент 'Напишите что-нибудь...' найден и активирован.", 'info')
    time.sleep(2)

    post_input = None

    try:
        log_and_print("Пошук Создайте общедоступную публикацию…", 'info')
        post_input = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable(
                (By.XPATH, "//div[@aria-label='Создайте общедоступную публикацию…' and @contenteditable='true']")
            )
        )
        post_input.click()
        log_and_print("Создайте общедоступную публикацию… знайдений та активований", 'info')
        time.sleep(3)

    except TimeoutException:
        log_and_print("Создайте общедоступную публикацию… не знайдений", 'info')
        log_and_print("Пошук 'Напишите что-нибудь...'", 'info')
        post_input = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located(
                (By.XPATH, "//div[@contenteditable='true' and @aria-label='Напишите что-нибудь...']")
            )
        )
        log_and_print("Поле в попапе 'Напишите что-нибудь...' найдено.", 'info')

    try:
        cleaned_message = remove_non_bmp_characters(message)
        log_and_print(f"Ввод текста сообщения: {cleaned_message}", 'info')
        post_input.send_keys(cleaned_message)
        time.sleep(2)

        element = driver.find_element(By.XPATH, "//img[contains(@src, '/images/composer/SATP_Aa_square-2x.png')]")
        element.click()

        time.sleep(3)

        element = driver.find_element(By.XPATH, "//div[@aria-label='Настройки фона' and @role='button']")
        element.click()

        time.sleep(3)

        element = driver.find_element(By.XPATH, f"//div[@aria-label='{get_random_value(fone_colors)}' and @role='button']")
        element.click()

        time.sleep(3)

        # Если указан путь к изображению, загружаем его
        if image_path and os.path.exists(image_path):
            log_and_print(f"Загрузка изображения: {image_path}", 'info')

            try:
                # Клик по кнопке "Дополните публикацию"
                additional_options_button = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, "//*[text()='Дополните публикацию']"))
                )
                additional_options_button.click()
                log_and_print("Клик по кнопке 'Дополните публикацию' выполнен.", 'info')
                time.sleep(2)

                # Клик по кнопке "Фото/видео"
                photo_video_button = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable(
                        (By.XPATH, "//span[contains(@class, 'x193iq5w') and text()='Фото/видео']"))
                )
                photo_video_button.click()
                log_and_print("Клик по кнопке 'Фото/видео' выполнен.", 'info')
                time.sleep(2)

                sel = "//div[contains(@class, 'x9f619') and contains(@class, 'x1n2onr6') and contains(@class, 'x1ja2u2z') and contains(@class, 'x78zum5') and contains(@class, 'xdt5ytf') and contains(@class, 'x2lah0s') and contains(@class, 'x193iq5w') and contains(@class, 'xurb0ha') and contains(@class, 'x1sxyh0') and contains(@class, 'x1gslohp') and contains(@class, 'x12nagc') and contains(@class, 'xzboxd6') and contains(@class, 'x14l7nz5')]//input[@type='file']"
                file_inputs = driver.find_elements(By.XPATH, sel)

                if file_inputs:
                    # Выбираем первый из найденных
                    image_upload_input = file_inputs[0]
                    image_upload_input.send_keys(image_path)
                    log_and_print("Изображение успешно загружено через скрытый input.", 'info')
                else:
                    print("Не удалось найти элементы для загрузки файлов.")

                time.sleep(5)  # Задержка для завершения загрузки

            except Exception as e:
                log_and_print(f"Ошибка при загрузке изображения: {e}", 'error')

        log_and_print("Поиск кнопки 'Опубликовать'...", 'info')
        post_button = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.XPATH, "//div[@aria-label='Отправить']"))
        )
        post_button.click()
        log_and_print("Сообщение успешно опубликовано!", 'info')

    except Exception as e:
        log_and_print(f"Не удалось отправить сообщение, ошибка: {e}", 'error')

def sendOneMessagessToFb(message, image_path=None):
    global drivers

    fone_colors = read_setting('fone_colors')
    chromedriver_port =  read_setting('chromedriver_port')

    creds = load_json('creds.json')
    fb_creds = creds.get('fb_creds', {})
    log_and_print(f"fb_creds {fb_creds}.", 'info')
    fb_groups = load_json('fb_groups.json')
    log_and_print(f"fb_groups {fb_groups}.", 'info')

    current_dir = os.path.dirname(os.path.abspath(__file__))
    log_and_print(f"current_dir: {current_dir}", 'info')
    driver_path = os.path.join(current_dir, 'chromedriver.exe')
    log_and_print(f"driver_path: {driver_path}", 'info')

    for login_fb, user_data in fb_groups.items():
        log_and_print(f"Пользователь: {login_fb}", 'info')
        pass_fb = fb_creds.get(login_fb, {}).get('password')

        if drivers.get(login_fb) is None:
            drivers[login_fb] = start_chrome_with_retries(driver_path, chromedriver_port)

            if drivers[login_fb] is None:
                log_and_print("Не удалось запустить или подключиться к браузеру.", 'error')
                continue  # Переходим к следующему пользователю, если запуск не удался

            autorizeFb(drivers[login_fb], login_fb, pass_fb)

        for group_id, group_data in user_data.get('groups', {}).items():
            pause_seconds = group_data.get('pause_seconds', 5)
            try:
                publichOneMessage(drivers[login_fb], group_id, message, image_path, fone_colors)
            except Exception as e:
                log_and_print(f"Ошибка при публикации: {e}", 'error')
                # Перезапускаем драйвер при возникновении ошибки
                drivers[login_fb] = start_chrome_with_retries(driver_path, chromedriver_port)
                if drivers[login_fb] is None:
                    log_and_print("Не удалось перезапустить драйвер. Переход к следующему пользователю.", 'error')
                    break  # Переходим к следующему пользователю, если перезапуск не удался
            finally:
                log_and_print(f"Пауза: {pause_seconds} секунд", 'info')
                time.sleep(pause_seconds)

    log_and_print("Оставляем браузер открытым.", 'info')
