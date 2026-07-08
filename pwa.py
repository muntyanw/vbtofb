from pywinauto import Application

# Подключаемся к Viber
app = Application(backend="uia").connect(title_re=".*Viber.*")
window = app.window(title_re=".*Viber.*")

# Выводим иерархию элементов
print(window.print_control_identifiers())
