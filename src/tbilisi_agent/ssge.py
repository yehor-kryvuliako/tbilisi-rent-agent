"""ss.ge: раз на ~12 хвилин читає першу сторінку нових оголошень оренди в Тбілісі.
Дбайливо: одна сторінка списку і по одному оголошенню з паузою, як звичайний відвідувач."""
import asyncio
import json
import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone

import urllib.parse
import urllib.request

from . import db, judge, pages
from .config import MAX_PHOTOS, MAX_USD, SSGE_DETAIL_PREFIX, SSGE_EVERY_MIN, SSGE_LIST_URL, USER_AGENT

log = logging.getLogger("ssge")
NEXT_RE = re.compile(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

FLAGS = {  # поля оголошення ss.ge → українською
    "heating": "опалення", "hotWater": "гаряча вода", "furniture": "меблі", "fridge": "холодильник",
    "washingMachine": "пральна машина", "airConditioning": "кондиціонер", "internet": "інтернет",
    "wiFi": "Wi-Fi", "naturalGas": "газ", "elevator": "ліфт", "balcony": "балкон", "tv": "телевізор",
    "isPetFriendly": "можна з тваринами",
}


def _get_json(url: str) -> dict | None:
    url = urllib.parse.quote(url, safe=":/?=&%")  # кирилиця в адресі
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "ru"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            m = NEXT_RE.search(r.read().decode("utf-8", "replace"))
        return json.loads(m.group(1))["props"]["pageProps"] if m else None
    except Exception as ex:
        log.warning("ss.ge недоступний: %s", type(ex).__name__)
        return None


def _detail_text(a: dict, item: dict) -> str:
    addr = a.get("address") or {}
    desc = a.get("description") or {}
    if isinstance(desc, dict):
        desc = desc.get("ru") or desc.get("en") or desc.get("ka") or ""
    price = a.get("price") or {}
    has = [v for k, v in FLAGS.items() if a.get(k) is True]
    no = [v for k, v in FLAGS.items() if a.get(k) is False]
    metro = ", ".join(f"{s.get('stationTitle')} ~{s.get('timeDistance')} хв"
                      for s in item.get("nearbySubwayStations") or [])
    return "\n".join([
        a.get("title") or item.get("title") or "",
        f"Ціна: ${price.get('priceUsd')} ({price.get('priceGeo')} ларі) на місяць",
        f"Адреса: {addr.get('subdistrictTitle')}, {addr.get('districtTitle')}, "
        f"{addr.get('streetTitle') or ''} {addr.get('streetNumber') or ''}",
        f"Площа: {item.get('totalArea')} м², спалень: {item.get('numberOfBedrooms')}, "
        f"поверх {item.get('floorNumber')}/{item.get('totalAmountOfFloor')}",
        f"Метро поруч: {metro or 'не вказано'}",
        f"Є (за анкетою сайту): {', '.join(has) or 'не вказано'}",
        f"Позначено як немає: {', '.join(no) or '—'} (анкету часто заповнюють недбало)",
        f"Власник чи агентство: {a.get('agencyName') or a.get('contactPerson') or 'не вказано'}",
        "", str(desc),
    ])


async def process(it: dict, handle, to_backlog=False):
    """Одне оголошення зі списку: перевірити, відкрити, оцінити, передати далі."""
    aid = it.get("applicationId")
    usd = (it.get("price") or {}).get("priceUsd") or 0
    if not aid or db.seen(f"ss:{aid}"):
        return
    if not 0 < usd <= MAX_USD or "посуточ" in (it.get("title") or "").lower():  # сайт пише російською
        return
    await asyncio.sleep(random.uniform(2, 5))  # дбайливо до сайту
    url = f"{SSGE_DETAIL_PREFIX}{aid}"  # коротке робоче посилання на оголошення
    d = await asyncio.to_thread(_get_json, url)
    a = (d or {}).get("applicationData")
    if not a or a.get("isInactiveApplication"):
        return  # оголошення вже немає
    text = _detail_text(a, it)
    if db.seen(db.text_key(str(a.get("description")))):
        return  # те саме оголошення вже було (подали повторно)
    photos = [p["fileName"] for p in (a.get("appImages") or []) if p.get("fileName")][:10]
    ev = await asyncio.to_thread(judge.evaluate, text, "ss.ge", (), photos[:MAX_PHOTOS])
    phones = []
    for p in a.get("applicationPhones") or []:
        n = re.sub(r"\D", "", p.get("phoneNumber") or "")
        # Будь-який мобільний (5xx): навіть якщо позначено лише дзвінок, пишемо у WhatsApp
        if len(n) == 9 and n.startswith("5"):
            phones.append("995" + n)
    ref = {"urls": photos} if photos else None
    extra = {"text": text[:3000], "site": url, "post": None, "note": "",
             "map": pages.map_link(ev or {}, None, a.get("locationLatitude") or 0, a.get("locationLongitude") or 0)}
    await handle(ev, "ss.ge", url, url, None, phones, photos, ref, to_backlog, None, extra)


async def check_once(handle):
    page = await asyncio.to_thread(_get_json, SSGE_LIST_URL)
    db.meta_set("ssge_last", str(time.time()))
    for it in ((page or {}).get("applicationList") or {}).get("realStateItemModel") or []:
        await process(it, handle)


async def backfill(handle, days: int):
    """Тиждень на ss.ge: ідемо від дешевих до дорогих до $500 і беремо подані за останні дні."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    base = SSGE_LIST_URL.replace("&order=1", "") + "&order=4&page="  # order=4 — спершу дешеві
    for page_no in range(1, 400):
        await asyncio.sleep(random.uniform(3, 6))
        page = await asyncio.to_thread(_get_json, base + str(page_no))
        items = ((page or {}).get("applicationList") or {}).get("realStateItemModel") or []
        if not items:
            break
        for it in items:
            if (it.get("createDate") or "")[:10] >= since:
                await process(it, handle, to_backlog=True)
        prices = [(i.get("price") or {}).get("priceUsd") or 0 for i in items]
        if min(prices) > MAX_USD:
            break
    log.info("Тиждень на ss.ge готовий")


async def loop(handle, is_paused):
    while True:
        try:
            if not is_paused():
                await check_once(handle)
        except Exception:
            log.exception("Помилка під час перевірки ss.ge")
        await asyncio.sleep(SSGE_EVERY_MIN * 60 + random.uniform(-90, 90))
