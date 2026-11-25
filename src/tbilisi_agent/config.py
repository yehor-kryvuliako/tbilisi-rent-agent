"""Налаштування агента. Усе, що можна змінювати без програмування, — тут."""
from pathlib import Path

# Де агент зберігає базу, журнал тощо (не на Робочому столі: iCloud і заборони macOS)
DATA_DIR = Path.home() / "Library" / "Application Support" / "TbilisiAgent"

# Назви записів у зв'язці ключів macOS (самі секрети лежать лише там)
KEYCHAIN = {
    "anthropic": "tbilisi-agent-anthropic",   # ключ Claude API
    "tg_api_id": "tbilisi-agent-tg-api-id",   # з my.telegram.org
    "tg_api_hash": "tbilisi-agent-tg-api-hash",
    "tg_session": "tbilisi-agent-tg-session", # вхід другого акаунта
    "bot_token": "tbilisi-agent-bot-token",   # бот для сповіщень (@BotFather)
}

# Модель Claude. Haiku 5.5 — найдешевша ($0.10 за 1 млн вхідних токенів), уміє читати фото.
MODEL = "claude-haiku-5-5"
MAX_PHOTOS = 10  # скільки фото з оголошення дивиться Claude (щоб не питати про те, що видно на фото)

# Бюджет (для простого попереднього фільтра; остаточно вирішує Claude за завданням)
MAX_USD = 500
GEL_PER_USD = 2.7  # грубий курс для попереднього фільтра

# Скільки повідомлень агент може надіслати сам (захист від бану за спам)
AUTO_SEND_PER_HOUR = 6
AUTO_SEND_PER_DAY = 25

# Повторне повідомлення тій самій людині: якщо минуло менше за це — зовсім коротко
CLOSE_FOLLOWUP_HOURS = 3

# ss.ge: як часто дивитися нові оголошення (хвилини) і сторінка списку оренди в Тбілісі
SSGE_EVERY_MIN = 12
SSGE_LIST_URL = (
    "https://home.ss.ge/ru/%D0%BD%D0%B5%D0%B4%D0%B2%D0%B8%D0%B6%D0%B8%D0%BC%D0%BE%D1%81%D1%82%D1%8C"
    "/l/%D0%9A%D0%B2%D0%B0%D1%80%D1%82%D0%B8%D1%80%D0%B0/%D0%90%D1%80%D0%B5%D0%BD%D0%B4%D0%B0"
    "?cityIdList=95&order=1"
)
SSGE_DETAIL_PREFIX = "https://home.ss.ge/ru/недвижимость/"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

# Які Telegram-чати читати: у назві має бути одне з цих слів (без урахування регістру).
# Чати оренди в Тбілісі здебільшого російськомовні й англомовні.
RENT_CHAT_WORDS = ("аренд", "квартир", "недвиж", "жиль", "rent", "home", "estate", "residence", "flat", "apartment")
