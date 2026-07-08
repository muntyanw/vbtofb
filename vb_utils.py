from pywinauto import Application, mouse
from log import log_and_print

def scroll_with_mouse(count_scroll):
    # Подключаемся к Viber
    app = Application(backend="uia").connect(title_re=".*Viber.*")
    window = app.window(title_re=".*Viber.*")

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

def isText(menuList):
    return "Копіювати повідомлення" in menuList

def isImage(menuList):
    return "Копіювати зображення" in menuList
