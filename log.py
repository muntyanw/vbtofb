import logging
from datetime import datetime

# Настройка логирования
logging.basicConfig(
    filename='log.log',
    filemode='w',
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    encoding='utf-8'
)

def log_and_print(message, level='info'):
    current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    message = str(message)

    try:
        print(f"[{current_time}] {message}")
    except OSError:
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
