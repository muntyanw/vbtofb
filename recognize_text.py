import cv2
import numpy as np
import pyautogui
import pytesseract
from difflib import SequenceMatcher
from PIL import Image

from log import log_and_print
from utils import read_setting


pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"


def preprocess_image(pil_image):
    image = np.array(pil_image)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return Image.fromarray(gray)


def filter_recognized_text(text):
    lines = text.split("\n")
    filtered_lines = []
    for line in lines:
        stripped = line.strip()
        if len(stripped) < 6 and ":" in stripped:
            continue
        filtered_lines.append(line)
    return "\n".join(filtered_lines)


def showImage(processed_image, region):
    processed_array = np.array(processed_image)
    window_name = "Processed Image"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    cv2.moveWindow(window_name, region[0], region[1])
    cv2.imshow(window_name, processed_array)
    cv2.waitKey(1)


def capture_and_recognize(region):
    log_and_print(f"[capture_and_recognize] region: {region}")
    try:
        cv2.destroyAllWindows()
        screenshot = pyautogui.screenshot(region=region)
        processed_image = preprocess_image(screenshot)
        custom_config = read_setting("capture_and_recognize.custom_config")
        lang = read_setting("capture_and_recognize.lang")
        text = pytesseract.image_to_string(processed_image, lang=lang, config=custom_config)
        log_and_print(f"Recognized text (before filtering):\n{text}")
        filtered_text = filter_recognized_text(text)
        log_and_print(f"Recognized text (after filtering):\n{filtered_text}")
        return filtered_text
    except Exception as exc:
        log_and_print(f"Error during capture and recognition: {exc}")
        return None


def capture_and_find_multiple_text_coordinates(region, search_phrases):
    log_and_print(f"[capture_and_find_multiple_text_coordinates] region: {region}, search_phrases: {search_phrases}")
    try:
        cv2.destroyAllWindows()
        screenshot = pyautogui.screenshot(region=region)
        processed_image = preprocess_image(screenshot)
        custom_config = read_setting("capture_and_recognize.custom_config")
        lang = read_setting("capture_and_recognize.lang")
        data = pytesseract.image_to_data(
            processed_image,
            lang=lang,
            config=custom_config,
            output_type=pytesseract.Output.DICT,
        )
        log_and_print(f"OCR menu data = {data}")

        result = {}
        for key, phrase in search_phrases.items():
            result[key] = find_phrase_coordinates(data, phrase)

        for key, coords in result.items():
            phrase = search_phrases[key]
            if coords is None:
                log_and_print(f"Phrase '{phrase}' (key: {key}) not found.")
            else:
                log_and_print(f"Coordinates for phrase '{phrase}' (key: {key}): {coords}")

        return result
    except Exception as exc:
        log_and_print(f"Error during capture and coordinate recognition: {exc}")
        return {key: None for key in search_phrases}


def find_phrase_coordinates(data, phrase):
    phrase_words = [word for word in normalize_ocr_text(phrase).split() if word]
    if not phrase_words:
        return None

    tokens = []
    for index, text in enumerate(data["text"]):
        normalized = normalize_ocr_text(text)
        if normalized:
            tokens.append((index, normalized))

    if not tokens:
        return None

    if len(phrase_words) == 1:
        needle = phrase_words[0]
        for index, word in tokens:
            if words_match(needle, word):
                return (
                    data["left"][index],
                    data["top"][index],
                    data["width"][index],
                    data["height"][index],
                )
        return None

    for start in range(0, len(tokens) - len(phrase_words) + 1):
        window = tokens[start:start + len(phrase_words)]
        words = [word for _, word in window]
        if all(words_match(needle, word) for needle, word in zip(phrase_words, words)):
            indexes = [index for index, _ in window]
            left = min(data["left"][index] for index in indexes)
            top = min(data["top"][index] for index in indexes)
            right = max(data["left"][index] + data["width"][index] for index in indexes)
            bottom = max(data["top"][index] + data["height"][index] for index in indexes)
            return (left, top, right - left, bottom - top)

    return None


def normalize_ocr_text(text):
    text = (text or "").strip().lower().replace("ё", "е")
    return "".join(char for char in text if char.isalnum() or char.isspace())


def words_match(expected, actual):
    if expected == actual:
        return True
    if len(expected) < 4 or len(actual) < 4:
        return False
    return SequenceMatcher(None, expected, actual).ratio() >= 0.78
