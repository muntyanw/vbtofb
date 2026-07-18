import ctypes
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
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

LOG_FILE = "log.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"


class SingleFileRotatingHandler(RotatingFileHandler):
    def doRollover(self):
        if self.stream:
            self.stream.close()
            self.stream = None

        with open(
            self.baseFilename,
            "w",
            encoding=self.encoding,
            errors=self.errors,
        ):
            pass

        if not self.delay:
            self.stream = self._open()


def _delete_log_archives():
    log_path = Path(LOG_FILE)
    for archive_path in log_path.parent.glob(f"{log_path.name}.*"):
        try:
            archive_path.unlink()
        except OSError:
            pass


def _configure_logging():
    _delete_log_archives()
    handler = SingleFileRotatingHandler(
        LOG_FILE,
        maxBytes=LOG_MAX_BYTES,
        backupCount=0,
        encoding="utf-8-sig",
    )
    handler.setFormatter(logging.Formatter(LOG_FORMAT))

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


_configure_logging()

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
