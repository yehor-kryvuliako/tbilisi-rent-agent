"""Налаштування, запускається один раз у Терміналі. Усе секретне ви вводите самі, на екран воно не виводиться."""
import asyncio
import getpass
import json
import os
import plistlib
import secrets as pysecrets
import subprocess
import sys
import urllib.request
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession

from . import db, secrets
from .config import DATA_DIR

LABEL = "com.tbilisi-agent"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
AGAIN = ("знову", "заново", "again", "resend")


def ask(prompt: str, hidden=False) -> str:
    v = (getpass.getpass if hidden else input)(prompt).strip()
    if not v:
        sys.exit("Порожнє введення — налаштування перервано.")
    return v


def step_telegram_app():
    if secrets.get("tg_api_id") and secrets.get("tg_api_hash"):
        print("✓ Ключі застосунку Telegram уже збережено")
        return
    print("\n1) Ключі застосунку Telegram (із сайту my.telegram.org → API development tools)")
    api_id = ask("   api_id (число): ")
    if not api_id.isdigit():
        sys.exit("api_id має бути числом.")
    secrets.put("tg_api_id", api_id)
    secrets.put("tg_api_hash", ask("   api_hash (не буде видно під час введення): ", hidden=True))
    print("   ✓ Збережено у зв'язку ключів")


async def ask_password(c):
    """Хмарний пароль: до 5 спроб, із підказкою з Telegram."""
    from telethon import errors
    from telethon.tl.functions.account import GetPasswordRequest
    try:
        hint = (await c(GetPasswordRequest())).hint
    except errors.RPCError:
        hint = None
    print("   В акаунта ввімкнено хмарний пароль (двоетапна перевірка)."
          + (f" Підказка: {hint}" if hint else ""))
    print("   Перевірте розкладку клавіатури й Caps Lock.")
    for _ in range(5):
        try:
            await c.sign_in(password=getpass.getpass("   Хмарний пароль (під час введення не видно): "))
            return
        except errors.PasswordHashInvalidError:
            print("   Пароль неправильний, спробуйте ще раз.")
        except errors.FloodWaitError as ex:
            sys.exit(f"Забагато спроб. Зачекайте {ex.seconds // 60 + 1} хв. і запустіть знову.")
    sys.exit("Пароль не підійшов 5 разів. Перевірте його в Telegram і запустіть налаштування знову.")


async def qr_login(c):
    """Вхід без коду: QR сканується телефоном, на якому відкрито другий акаунт."""
    import segno
    from telethon import errors
    print("\n   На телефоні в ДРУГОМУ акаунті: Налаштування → Пристрої → «Підключити пристрій»")
    print("   і наведіть камеру на QR нижче. QR оновлюється кожні ~30 секунд.\n")
    qr = await c.qr_login()
    while True:
        segno.make(qr.url, error="l").terminal(compact=True)
        try:
            await qr.wait(timeout=30)
            return
        except asyncio.TimeoutError:
            await qr.recreate()
            print("\n   QR оновлено:\n")
        except errors.SessionPasswordNeededError:
            await ask_password(c)
            return


async def step_login():
    if secrets.get("tg_session"):
        print("✓ Вхід у другий акаунт Telegram уже виконано")
        return
    print("\n2) Вхід у ДРУГИЙ акаунт Telegram (не основний!)")
    c = TelegramClient(StringSession(), int(secrets.need("tg_api_id")), secrets.need("tg_api_hash"),
                       device_model="Tbilisi agent")
    def phone():
        while True:
            raw = input("   Номер другого акаунта, лише цифри з кодом країни (наприклад +380…): ").strip()
            first = raw.split()[0] if raw.split() else ""
            digits = "".join(ch for ch in first if ch.isdigit())
            if 9 <= len(digits) <= 15:
                return "+" + digits
            print("   Не схоже на номер, спробуйте ще раз.")

    WHERE = {
        "SentCodeTypeApp": "у застосунок Telegram — чат «Telegram» із синьою галочкою в другому акаунті",
        "SentCodeTypeSms": "SMS на цей номер",
        "SentCodeTypeCall": "дзвінком (код продиктують)",
        "SentCodeTypeFlashCall": "коротким дзвінком (код — останні цифри номера, з якого дзвонять)",
        "SentCodeTypeMissedCall": "пропущеним дзвінком (код — останні цифри номера, з якого дзвонять)",
        "SentCodeTypeEmailCode": "на пошту, прив'язану до акаунта",
        "SentCodeTypeFragmentSms": "через Fragment",
        "SentCodeTypeFirebaseSms": "SMS на цей номер",
        "SentCodeTypeSmsWord": "SMS (кодове слово)",
        "SentCodeTypeSmsPhrase": "SMS (кодова фраза)",
    }

    def explain(sent):
        t = type(sent.type).__name__
        print(f"   Код надіслано: {WHERE.get(t, t)}.")
        nxt = getattr(sent, "next_type", None)
        if nxt:
            print(f"   Якщо не прийде — введіть «знову», і Telegram надішле {WHERE.get(type(nxt).__name__, 'іншим способом')}.")
        else:
            print("   Якщо не прийде за кілька хвилин — введіть «знову».")

    from telethon import errors
    await c.connect()
    way = input("   Як увійти? 1 — за QR-кодом (раджу), 2 — за кодом із Telegram [1]: ").strip() or "1"
    if way != "2":
        await qr_login(c)
        me = await c.get_me()
        secrets.put("tg_session", c.session.save())
        await c.disconnect()
        print(f"   ✓ Увійшов як {me.first_name}. Вхід збережено у зв'язку ключів.")
        return
    num = phone()
    try:
        sent = await c.send_code_request(num)
    except errors.FloodWaitError as ex:
        sys.exit(f"Telegram просить зачекати {ex.seconds // 60 + 1} хв. перед новою спробою.")
    except errors.PhoneNumberInvalidError:
        sys.exit("Telegram каже, що номер неправильний.")
    explain(sent)
    while True:
        raw = input("   Код із Telegram (або «знову»): ").strip().lower()
        if raw in AGAIN:
            try:
                sent = await c.send_code_request(num)  # повторний запит = наступний спосіб доставки
                explain(sent)
            except errors.FloodWaitError as ex:
                print(f"   Telegram просить зачекати {ex.seconds} с.")
            except errors.RPCError as ex:
                print(f"   Не вийшло запросити знову ({type(ex).__name__}).")
            continue
        code = "".join(ch for ch in raw if ch.isdigit())
        if not code:
            print("   Введіть цифри коду.")
            continue
        try:
            await c.sign_in(num, code)
            break
        except errors.SessionPasswordNeededError:
            await ask_password(c)
            break
        except (errors.PhoneCodeInvalidError, errors.PhoneCodeEmptyError):
            print("   Код неправильний, спробуйте ще раз.")
        except errors.PhoneCodeExpiredError:
            print("   Код застарів — введіть «знову».")
    me = await c.get_me()
    secrets.put("tg_session", c.session.save())
    await c.disconnect()
    print(f"   ✓ Увійшов як {me.first_name}. Вхід збережено у зв'язку ключів.")


def bot_call(token, method, **p):
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                 data=json.dumps(p).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=70) as r:
        return json.loads(r.read())["result"]


def step_bot():
    token = secrets.get("bot_token")
    if not token:
        print("\n3) Бот для сповіщень: в основному Telegram напишіть @BotFather → /newbot,")
        print("   придумайте ім'я, і він надішле токен виду 123456:ABC…")
        token = ask("   Токен бота (не буде видно під час введення): ", hidden=True)
        try:
            name = bot_call(token, "getMe")["username"]
        except Exception:
            sys.exit("Токен не підійшов. Перевірте й запустіть налаштування знову.")
        secrets.put("bot_token", token)
    else:
        name = bot_call(token, "getMe")["username"]
    if db.meta_get("owner_chat_id"):
        print(f"✓ Бот @{name} уже прив'язаний до вашого Telegram")
        return
    code = pysecrets.token_urlsafe(9)
    db.meta_set("pair_code", code)
    print(f"\n   Відкрийте в ОСНОВНОМУ Telegram посилання й натисніть «Старт»:\n   https://t.me/{name}?start={code}")
    print("   Чекаю натискання…")
    offset = 0
    while True:
        for u in bot_call(token, "getUpdates", offset=offset, timeout=50):
            offset = u["update_id"] + 1
            m = u.get("message") or {}
            if (m.get("text") or "").strip() == f"/start {code}" and m["chat"]["type"] == "private":
                db.meta_set("owner_chat_id", str(m["chat"]["id"]))
                db.meta_set("pair_code", "")
                db.meta_set("bot_offset", str(offset))
                bot_call(token, "sendMessage", chat_id=m["chat"]["id"],
                         text="✅ Готово! Сюди приходитимуть варіанти квартир.")
                print("   ✓ Бот прив'язано")
                return


def step_autostart():
    exe = Path(sys.executable).with_name("tbilisi-agent")
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    plistlib.dump({
        "Label": LABEL,
        # caffeinate -s: Mac не засинає, поки агент працює й підключено зарядку
        "ProgramArguments": ["/usr/bin/caffeinate", "-s", str(exe)],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 60,
        "StandardOutPath": str(DATA_DIR / "launchd.log"),
        "StandardErrorPath": str(DATA_DIR / "launchd.log"),
        "ProcessType": "Background",
    }, PLIST.open("wb"))
    uid = os.getuid()
    subprocess.run(["/bin/launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
    subprocess.run(["/bin/launchctl", "bootstrap", f"gui/{uid}", str(PLIST)], check=True)
    print("\n✓ Агента запущено, і він стартуватиме сам під час входу в систему.")
    print("  Зупинити:  launchctl bootout gui/$(id -u)/com.tbilisi-agent")


def run():
    if not secrets.get("anthropic"):
        sys.exit("Немає ключа Claude API у зв'язці ключів (tbilisi-agent-anthropic).")
    print("✓ Ключ Claude API знайдено")
    step_telegram_app()
    asyncio.run(step_login())
    step_bot()
    if "--no-start" not in sys.argv:
        step_autostart()
