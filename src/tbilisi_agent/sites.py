"""Інші сайти оренди, крім ss.ge: korter.ge, livo.ge (база myhome.ge), place.ge, rentor.ge.
Як і ss.ge: раз на ~12 хвилин перша сторінка нових оголошень і по одному оголошенню з паузою.
Лише читання відкритих сторінок; приховане за кнопками (телефон на livo.ge) не дістаємо.
Слова російською в регулярних виразах — це текст самих сайтів, їх не перекладаємо."""
import asyncio
import html as htmllib
import json
import logging
import random
import re
import time
import urllib.parse
import urllib.request

from . import db, judge, pages
from .config import MAX_PHOTOS, MAX_USD, SSGE_EVERY_MIN, USER_AGENT

log = logging.getLogger("sites")
AGENCY_NOTE = "🏢 Агентство — можлива комісія"


def _get(url: str, accept="text/html") -> str | None:
    url = urllib.parse.quote(url, safe=":/?=&%,")  # кирилиця в адресі
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "ru,en",
                                               "Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read(6_000_000).decode("utf-8", "replace")
    except Exception as ex:
        log.warning("%s недоступний: %s", pages.host_of(url), type(ex).__name__)
        return None


def _text(h: str) -> str:
    h = re.sub(r"(?s)<(script|style|svg|noscript)[^>]*>.*?</\1>", " ", h)
    h = re.sub(r"<br\s*/?>|</p>|</div>", "\n", h)
    return re.sub(r"[ \t]+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", h))).strip()


def _mobile(raw: str) -> str | None:
    """Грузинський мобільний (5xx xx xx xx) → 995…, інакше None."""
    n = re.sub(r"\D", "", raw or "")
    n = n[3:] if n.startswith("995") else n
    return "995" + n if len(n) == 9 and n.startswith("5") else None


# ---------------- korter.ge ----------------

def _korter_state(h: str) -> dict:
    i = h.find("window.INITIAL_STATE = ")
    if i < 0:
        return {}
    try:
        return json.JSONDecoder().raw_decode(h[i + 23:])[0]
    except ValueError:
        return {}


def korter_list() -> list[dict]:
    h = _get("https://korter.ge/ru/аренда-квартир-тбилиси?max_price=500&currency=USD")
    items = ((_korter_state(h or "").get("apartmentListingStore") or {}).get("apartments")) or []
    return [{"id": str(a["objectId"]), "usd": a.get("price") if a.get("currency") == "USD" else None,
             "url": "https://korter.ge" + a["link"]} for a in items if a.get("objectId") and a.get("link")]


def korter_detail(it: dict) -> dict | None:
    h = _get(it["url"])
    L = _korter_state(h or "").get("layoutLandingStore") or {}
    a, s = L.get("layout") or {}, L.get("seller") or {}
    if not a or a.get("publicationStatus") != "published" or a.get("section") != "rent":
        return None
    usd = a.get("price") if a.get("currency") == "USD" else None
    photos = []
    for im in a.get("images") or []:
        src = ((im.get("mediaSrc") or {}).get("default") or {})
        if src.get("x1"):
            photos.append(src["x1"])
    phones = [p for p in (_mobile(x.get("displayNumber")) for x in s.get("phones") or []) if p]
    agency = s.get("sellerType") not in (None, "owner", "private")
    floors = (a.get("floorsByHouse") or [{}])[0].get("floorCount")
    text = "\n".join([
        f"{(a.get('propertyType') or {}).get('propertyTypeRoomCountLabel') or 'Квартира'} в оренду",
        f"Ціна: ${usd} на місяць" if usd else f"Ціна: {a.get('price')} {a.get('currency')} на місяць",
        f"Адреса: Тбілісі, {a.get('address') or (L.get('building') or {}).get('address') or 'не вказано'}",
        f"Площа: {a.get('area')} м², кімнат: {a.get('roomCount')}, спалень: {a.get('bedroomCount')}, "
        f"поверхів у будинку: {floors or 'не вказано'}",
        f"Балкон: {'є' if a.get('hasBalcony') else 'не вказано'}, тераса: {'є' if a.get('hasTerrace') else 'не вказано'}",
        f"Продавець: {s.get('name') or 'не вказано'} ({'агентство/рієлтор' if agency else 'власник'})",
        "", a.get("description") or "",
    ])
    return {"usd": usd, "text": text, "photos": photos, "phones": phones, "agency": agency,
            "lat": 0, "lng": 0, "posted": a.get("createTime")}


# ---------------- livo.ge (база myhome.ge) ----------------

def _livo_queries(h: str) -> list:
    s = "".join(json.loads('"' + c + '"')
                for c in re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', h or "", re.S))
    out, dec, pos = [], json.JSONDecoder(), 0
    while (i := s.find('"queries":[', pos)) >= 0:
        try:
            q, end = dec.raw_decode(s, i + 10)
            out += q
            pos = end
        except ValueError:
            pos = i + 10
    return out


def livo_list() -> list[dict]:
    h = _get("https://livo.ge/ru/s?deal_types=2&real_estate_types=1&cities=1&currency_id=2&price_to=500"
             "&page=1&order_by=date&sequence=desc&per_page=20")
    for q in _livo_queries(h):
        data = (((q.get("state") or {}).get("data") or {}).get("data") or {})
        arr = data.get("data") if isinstance(data, dict) else None
        if isinstance(arr, list) and arr and isinstance(arr[0], dict) and "deal_type_id" in arr[0]:
            return [{"id": str(a["id"]), "usd": (a.get("price") or {}).get("2", {}).get("price_total"),
                     "url": f"https://livo.ge/ru/udzravi-qoneba/{a['middle_slug']}/{a['dynamic_slug']}-{a['id']}",
                     "daily": a.get("price_type_id") not in (None, 3)}  # 3 — помісячно
                    for a in arr if a.get("deal_type_id") == 2 and a.get("city_id") == 1
                    and a.get("middle_slug") and a.get("dynamic_slug")]
    return []


def livo_detail(it: dict) -> dict | None:
    h = _get(it["url"])
    a = None
    for q in _livo_queries(h):
        if "STATEMENT_BY_ID" in json.dumps(q.get("queryKey")):
            d = ((q.get("state") or {}).get("data") or {})
            a = (d.get("data") or {}).get("statement") or d.get("data") or d
            break
    if not isinstance(a, dict) or not a.get("id"):
        return None
    if a.get("hidden"):
        return None
    usd = (a.get("price") or {}).get("2", {}).get("price_total")
    photos = [im["large"] for im in a.get("images") or [] if im.get("large")]
    params = ", ".join(p.get("display_name") or p.get("title") or "" for p in a.get("parameters") or []
                       if isinstance(p, dict))
    ut = (a.get("user_type") or {}).get("type")
    agency = ut not in (None, "physical")
    text = "\n".join([
        a.get("dynamic_title") or "Квартира в оренду",
        f"Ціна: ${usd} на місяць",
        f"Адреса: Тбілісі, {a.get('district_name') or ''}, {a.get('urban_name') or ''}, {a.get('address') or ''}",
        f"Площа: {a.get('area')} м², кімнат: {a.get('room')}, спалень: {a.get('bedroom')}, "
        f"поверх {a.get('floor')}/{a.get('total_floors')}",
        f"Зручності (за анкетою сайту): {params or 'не вказано'}",
        f"Власник чи агентство: {'агентство' if agency else 'приватна особа'} ({a.get('owner_name') or a.get('user_title') or ''})",
        "Телефон на сайті приховано — дивіться за посиланням.",
        "", a.get("comment") or "",
    ])
    return {"usd": usd, "text": text, "photos": photos, "phones": [], "agency": agency,
            "lat": a.get("lat") or 0, "lng": a.get("lng") or 0, "posted": a.get("last_updated")}


# ---------------- place.ge ----------------

def place_list() -> list[dict]:
    h = _get("https://place.ge/ru/ads?object_type=flat&type=for_rent&price_to=500&currency_id=2") or ""
    out = []
    for block in re.findall(r'<div class="infoFilter">(.*?)<div class="editFilter">(.*?)</div>', h, re.S):
        info = _text(block[0] + block[1])
        m = re.search(r"/ru/ads/view/(\d+)", block[0] + block[1])
        usd = re.search(r'data-price_usd="\$([\d,]+)\s*/\s*([^"]+)"', block[0])
        if not m or "Тбилиси" not in info or "СДАЕТСЯ" not in info.upper():  # текст сайту російською
            continue
        out.append({"id": m.group(1), "url": f"https://place.ge/ru/ads/view/{m.group(1)}",
                    "usd": int(usd.group(1).replace(",", "")) if usd else None,
                    "daily": bool(usd and "сут" in usd.group(2)) or "посуточ" in info.lower()})
    return out


def place_detail(it: dict) -> dict | None:
    h = _get(it["url"])
    if not h:
        return None
    t = _text(h)
    i, j = t.find("Главная / Сдается"), t.find("Местонахождение объекта")  # межі опису на сторінці сайту
    body = t[i:j if j > i else i + 3000] if i >= 0 else ""
    if not body:
        return None
    body = re.sub(r"\s*\n\s*", "\n", body)
    usd = it.get("usd")  # ціна в доларах зі списку (data-price_usd) — надійніше, ніж на сторінці
    photos = list(dict.fromkeys(re.findall(r'https://photos\.place\.ge/photos/\d+/800x600\.webp', h)))
    phones = [p for p in (_mobile(x) for x in re.findall(r"\b5-?\d{2}-?\d{6}\b", body)) if p]
    agency = bool(re.search(r"\bагент", body, re.I))
    return {"usd": usd, "text": f"Ціна: ${usd} на місяць\n" + body,
            "photos": photos, "phones": list(dict.fromkeys(phones)), "agency": agency,
            "lat": 0, "lng": 0, "posted": None}


# ---------------- rentor.ge (відкритий API сайту) ----------------

def rentor_list() -> list[dict]:
    raw = _get("https://rentor.ge/wp-json/wp/v2/listings?per_page=30&orderby=date&order=desc",
               "application/json")
    try:
        arr = json.loads(raw or "[]")
    except ValueError:
        return []
    out = []
    for a in arr if isinstance(arr, list) else []:
        if "Apartment" not in str(a.get("myhome_14")):
            continue
        price = " ".join(a.get("myhome_130") or [])
        m = re.search(r"\$\s*([\d,]+)\s*/month", price)
        out.append({"id": str(a.get("id")), "url": a.get("link"), "raw": a,
                    "usd": int(m.group(1).replace(",", "")) if m else None})
    return out


def rentor_detail(it: dict) -> dict | None:
    a = it["raw"]
    loc = (a.get("myhome_153") or {}).get("location") or {}
    title = htmllib.unescape((a.get("title") or {}).get("rendered") or "")
    desc = _text((a.get("content") or {}).get("rendered") or "")
    text = "\n".join([
        title,
        f"Ціна: ${it['usd']} на місяць",
        f"Район: {', '.join(a.get('myhome_9508') or []) or 'не вказано'}, "
        f"адреса: {(a.get('myhome_153') or {}).get('address') or 'не вказано'}",
        f"Площа: {', '.join(a.get('myhome_340') or [])}, спалень: {', '.join(a.get('myhome_5462') or []) or '?'}",
        f"Будинок: {', '.join(a.get('myhome_15717') or []) or 'не вказано'}",
        "Оголошення агентства Rentor (дзвінок через їхній спільний номер).",
        "", desc,
    ])
    return {"usd": it["usd"], "text": text, "photos": [p for p in a.get("myhome_145") or [] if isinstance(p, str)],
            "phones": [], "agency": True, "lat": loc.get("lat") or 0, "lng": loc.get("lng") or 0,
            "posted": a.get("date")}


SITES = {  # назва → (ключ у пам'яті, список, подробиці)
    "korter.ge": ("ko", korter_list, korter_detail),
    "livo.ge": ("lv", livo_list, livo_detail),
    "place.ge": ("pl", place_list, place_detail),
    "rentor.ge": ("rt", rentor_list, rentor_detail),
}


async def process(name: str, it: dict, handle, to_backlog=False):
    prefix, _, detail = SITES[name]
    if db.seen(f"{prefix}:{it['id']}"):
        return
    if it.get("daily") or (it.get("usd") is not None and not 0 < it["usd"] <= MAX_USD):
        return
    await asyncio.sleep(random.uniform(2, 5))  # дбайливо до сайту
    d = await asyncio.to_thread(detail, it)
    if not d or not d.get("text"):
        return  # оголошення вже немає або сторінка не розібралася
    if d.get("usd") and not 0 < d["usd"] <= MAX_USD:
        return
    tkey = db.text_key(d["text"].split("\n\n", 1)[-1] or d["text"])
    if db.seen(tkey):
        return  # той самий текст уже був
    photos = d["photos"][:10]
    ev = await asyncio.to_thread(judge.evaluate, d["text"], name, (), photos[:MAX_PHOTOS])
    if ev is None:  # Claude не відповів — не губимо оголошення, перевіримо на наступному колі (до 3 разів)
        tries = int(db.meta_get(f"retry:{prefix}:{it['id']}") or 0) + 1
        db.meta_set(f"retry:{prefix}:{it['id']}", str(tries))
        if tries < 3:
            db.forget(f"{prefix}:{it['id']}", tkey)
        return
    note = AGENCY_NOTE if d.get("agency") else ""
    extra = {"text": d["text"][:3000], "site": it["url"], "post": None, "note": note,
             "map": pages.map_link(ev or {}, None, d.get("lat") or 0, d.get("lng") or 0)}
    await handle(ev, name, it["url"], it["url"], None, d["phones"], photos,
                 {"urls": photos} if photos else None, to_backlog, None, extra)


async def check_once(name: str, handle):
    items = await asyncio.to_thread(SITES[name][1])
    db.meta_set(f"site_last:{name}", str(time.time()))
    if not items:
        log.warning("%s: список порожній — сайт міг змінити верстку", name)
    for it in items:
        try:
            await process(name, it, handle)
        except Exception:
            log.exception("Помилка в оголошенні %s %s", name, it.get("url"))


async def backfill(handle):
    """Перебір тижня: на цих сайтах — свіжа перша сторінка кожного (там і так спершу нові)."""
    for name in SITES:
        items = await asyncio.to_thread(SITES[name][1])
        for it in items:
            try:
                await process(name, it, handle, to_backlog=True)
            except Exception:
                log.exception("Помилка в оголошенні %s %s", name, it.get("url"))
    log.info("Тиждень на інших сайтах готовий")


async def loop(handle, is_paused):
    await asyncio.sleep(random.uniform(30, 120))  # не одночасно з ss.ge
    while True:
        for name in SITES:
            try:
                if not is_paused():
                    await check_once(name, handle)
            except Exception:
                log.exception("Помилка під час перевірки %s", name)
            await asyncio.sleep(random.uniform(20, 60))
        await asyncio.sleep(SSGE_EVERY_MIN * 60 + random.uniform(-90, 90))
