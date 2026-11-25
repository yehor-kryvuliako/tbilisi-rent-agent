"""Тексти повідомлень орендодавцю.
Перше — з привітанням і подякою, повторні — без них. Щонайбільше одне запитання: не засипаємо людей питаннями.
Самі повідомлення — російською: орендодавці в Тбілісі зазвичай спілкуються нею."""
import random
import time

from . import db
from .config import CLOSE_FOLLOWUP_HOURS


def _question(ev: dict) -> str:
    qs = [q.strip()[:150].rstrip("?") + "?" for q in ev.get("questions", []) if q.strip()]
    return qs[0] if qs else ""


def message(contact_key: str, ev: dict, link: str | None, forwarded: bool = False) -> tuple[str, str]:
    """Повертає (текст, вид: first | repeat | close).
    forwarded — оголошення вже переслано співрозмовнику, тоді пишемо «по этому объявлению 👆»."""
    info = db.contact_info(contact_key)
    # Довжину обмежено: захист від дивного тексту в оголошенні
    what = (ev.get("where") or ev.get("short") or "квартира")[:90]
    if forwarded:
        ref = f"по этому объявлению 👆 ({what})"
    else:
        ref = f"по объявлению: {what}" + (f"\n{link}" if link else "")
    q = _question(ev)

    if not info:
        hello = random.choice(["Здравствуйте!", "Добрый день!"])
        text = f"{hello} Пишу {ref}\nКвартира ещё свободна? Можно посмотреть в ближайшие дни? {q} Спасибо!"
        kind = "first"
    elif time.time() - info[1] < CLOSE_FOLLOWUP_HOURS * 3600:
        text = f"И ещё {ref.replace('по этому объявлению', 'вот этот вариант').replace('по объявлению:', 'вот этот вариант:')}\nСвободен? {q}"
        kind = "close"
    else:
        text = f"Ещё {ref}\nОна свободна? Можно посмотреть? {q}"
        kind = "repeat"
    lines = [" ".join(line.split()) for line in text.split("\n")]
    return "\n".join(line for line in lines if line), kind
