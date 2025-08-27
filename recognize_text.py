from log import log_and_print
import pyautogui
import pytesseract
import re
from utils import read_setting
import cv2
import numpy as np
from PIL import Image

pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

def preprocess_image(pil_image):
    """
    Preprocess the image to make messages visible while hiding timestamps and checkmarks.
    """
    # Convert PIL Image to OpenCV format
    image = np.array(pil_image)

    # Convert to grayscale for easier processing
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    # Detect areas with timestamps and checkmarks (using brightness threshold)
    _, binary = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY)

    # Define a mask to remove timestamps and checkmarks
    mask = np.zeros_like(binary)

    # Apply the mask to hide timestamps and checkmarks
    processed = cv2.bitwise_and(gray, gray, mask=~mask)

    # Convert back to PIL Image format
    return Image.fromarray(processed)

def filter_recognized_text(text):
    """
    Filters out lines that are shorter than 6 characters and contain colons.
    """
    lines = text.split('\n')  # Разбиваем текст на строки
    filtered_lines = []
    for line in lines:
        stripped_line = line.strip()  # Убираем пробелы в начале и конце строки
        if len(stripped_line) < 6 and ':' in stripped_line:
            continue  # Пропускаем строки, если их длина < 6 и они содержат двоеточие
        filtered_lines.append(line)  # Добавляем остальные строки
    return '\n'.join(filtered_lines)  # Собираем текст обратно

def showImage(processed_image, region):
    # Отображение обработанного изображения
    processed_array = np.array(processed_image)
    window_name = "Processed Image"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    cv2.moveWindow(window_name, region[0], region[1])
    cv2.imshow(window_name, processed_array)
    cv2.waitKey(3000)
    cv2.destroyAllWindows()

def capture_and_recognize(region):
    log_and_print(f"[capture_and_recognize] region: {region}")
    """Capture and recognize text only when the image changes."""
    try:
        # Take a screenshot
        screenshot = pyautogui.screenshot(region=region)
        showImage(screenshot, region)
        # Preprocess the image (if needed)
        processed_image = preprocess_image(screenshot) #
        showImage(processed_image, region)
        # Perform OCR
        custom_config = read_setting("capture_and_recognize.custom_config")
        lang = read_setting("capture_and_recognize.lang")
        text = pytesseract.image_to_string(processed_image, lang=lang, config=custom_config)

        log_and_print(f"Recognized text (before filtering):\n{text}")

        # Filter the recognized text
        filtered_text = filter_recognized_text(text)

        log_and_print(f"Recognized text (after filtering):\n{filtered_text}")

        return filtered_text

    except Exception as e:
        log_and_print(f"Error during capture and recognition: {e}")
        return None
