import ctypes
import logging
import sys
from datetime import datetime

UTF8_CODE_PAGE = 65001


def _configure_text_output():
    try:
        ctypes.windll.kernel32.SetConsoleCP(UTF8_CODE_PAGE)
        ctypes.windll.kernel32.SetConsoleOutputCP(UTF8_CODE_PAGE)
    except Exception:
        pass

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_configure_text_output()

logging.basicConfig(
    filename='log.log',
    filemode='w',
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    encoding='utf-8-sig',
    force=True,
)

def log_and_print(message, level='info'):
    current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    message = str(message)

    try:
        print(f"[{current_time}] {message}")
    except (OSError, UnicodeEncodeError):
        pass

    try:
        if level == 'info':
            logging.info(message)
        elif level == 'warning':
            logging.warning(message)
        elif level == 'error':
            logging.error(message)
        else:
            logging.info(message)
    except Exception:
        pass
