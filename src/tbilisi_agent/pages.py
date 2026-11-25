"""Сторінка оголошення за посиланням із поста: текст для Claude, контакти, координати, чи вона жива.

Безпека: відкриваємо лише http(s) на публічних адресах (не локальна мережа і не сам Mac),
перевіряємо й кожну переадресацію, читаємо не більше 2 МБ. Текст сторінки — лише дані для Claude."""
import html
import ipaddress

import logging
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

from .config import USER_AGENT
from .geo import UNKNOWN

log = logging.getLogger("pages")
MAX_BYTES = 2_000_000
SKIP_HOSTS = ("t.me", "telegram.me", "instagram.com", "facebook.com", "youtube.com", "youtu.be", "tiktok.com")
BLOCKED_HOSTS = ("myhome.ge",)  # закритий захистом від роботів — не обходимо
MAP_RE = re.compile(r"(google\.[a-z.]+/maps|maps\.app\.goo\.gl|goo\.gl/maps|maps\.google\.|yandex\.[a-z.]+/maps|2gis)", re.I)


def host_of(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower().removeprefix("www.")


def _public(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    return all(ipaddress.ip_address(i[4][0]).is_global for i in infos)


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        u = urllib.parse.urlsplit(newurl)
        if u.scheme not in ("http", "https") or not _public(u.hostname or ""):
            raise urllib.error.URLError("переадресація на непублічну адресу")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SafeRedirect())


def classify(urls: list[str]) -> dict:
    """Розкласти посилання з поста: мапа, сайт оголошення, контакти."""
    out = {"map": None, "site": None, "tg": [], "wa": []}
    for u in urls:
        h = host_of(u)
        if MAP_RE.search(u):
            out["map"] = out["map"] or u
        elif h in ("wa.me", "api.whatsapp.com"):
            digits = re.sub(r"\D", "", urllib.parse.urlsplit(u).path + urllib.parse.urlsplit(u).query)
            if digits:
                out["wa"].append(digits)
        elif h in ("t.me", "telegram.me"):
            parts = urllib.parse.urlsplit(u)
            name = parts.path.strip("/").split("/")[0]
            is_app = "start" in parts.query or name.lower().endswith(("bot", "_app", "app"))  # міні-застосунки й боти
            is_post = len(parts.path.strip("/").split("/")) > 1  # посилання на пост каналу, а не на людину
            if (name and not is_app and not is_post and not name.startswith(("+", "joinchat", "share"))
                    and name != "c" and re.fullmatch(r"\w{4,32}", name)):
                out["tg"].append(name)
        elif not any(h == s or h.endswith("." + s) for s in SKIP_HOSTS) and not out["site"]:
            out["site"] = u
    return out


def fetch(url: str) -> dict:
    """{'status': 'ok'|'gone'|'blocked'|'error', 'text': ..., 'phones': [...], 'tg': [...], 'lat':..., 'lng':...}"""
    res = {"status": "error", "text": "", "phones": [], "tg": [], "lat": 0.0, "lng": 0.0}
    u = urllib.parse.urlsplit(url)
    host = host_of(url)
    if u.scheme not in ("http", "https") or not host:
        return res
    if any(host == b or host.endswith("." + b) for b in BLOCKED_HOSTS):
        res["status"] = "blocked"
        return res
    if not _public(host):
        return res
    req = urllib.request.Request(urllib.parse.quote(url, safe=":/?=&%#+,;@~!$*'()"),
                                 headers={"User-Agent": USER_AGENT, "Accept-Language": "ru,en"})
    try:
        with _opener.open(req, timeout=25) as r:
            if "html" not in (r.headers.get("Content-Type") or "html"):
                return res
            raw = r.read(MAX_BYTES).decode("utf-8", "replace")
    except urllib.error.HTTPError as ex:
        res["status"] = "gone" if ex.code in (404, 410) else ("blocked" if ex.code in (401, 403, 429) else "error")
        return res
    except Exception as ex:
        log.info("Сторінка %s не відкрилася: %s", host, type(ex).__name__)
        return res

    res["status"] = "ok"
    # Контакти й координати прямо з розмітки
    res["phones"] = list(dict.fromkeys(re.sub(r"\D", "", m) for m in re.findall(r'href="tel:([^"]+)"', raw)))
    res["phones"] += [re.sub(r"\D", "", m) for m in re.findall(r'wa\.me/(\+?\d{9,15})', raw)]
    res["tg"] = list(dict.fromkeys(m for m in re.findall(r't\.me/([A-Za-z]\w{3,31})(?=["/?])', raw)
                                   if m.lower() not in ("share", "joinchat", "s", "iv")))
    m = re.search(r'"?(?:lat|latitude|locationLatitude)"?\s*[:=]\s*"?(4[01]\.\d{3,})"?.{0,80}?'
                  r'"?(?:lng|lon|longitude|locationLongitude)"?\s*[:=]\s*"?(4[45]\.\d{3,})', raw, re.S)
    m = m or re.search(r'[?&@](?:q=|ll=)?(4[01]\.\d{4,}),\s*(4[45]\.\d{4,})', raw)
    if m:
        res["lat"], res["lng"] = float(m.group(1)), float(m.group(2))
    # Видимий текст: без скриптів, стилів і тегів
    meta = " ".join(html.unescape(x) for x in re.findall(
        r'<meta[^>]+(?:name|property)="(?:description|og:description|og:title)"[^>]+content="([^"]*)"', raw))
    title = html.unescape((re.search(r"<title[^>]*>(.*?)</title>", raw, re.S) or [None, ""])[1])
    body = re.sub(r"<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"\s+", " ", html.unescape(body)).strip()
    res["text"] = f"Адреса сторінки: {url}\nЗаголовок: {title.strip()}\n{meta}\n\n{body}"[:9000]
    return res


def coords(link: str | None):
    """Координати з посилання на мапу виду ?q=41.7,44.8 → (41.7, 44.8)."""
    m = re.search(r"q=(4[01]\.\d+),(4[45]\.\d+)", link or "")
    return (float(m.group(1)), float(m.group(2))) if m else None


def map_link(ev: dict, post_map: str | None, lat: float = 0, lng: float = 0) -> str | None:
    """Посилання на Google Maps: із поста, за координатами або за адресою."""
    if post_map:
        # Яндекс-мапи (pt=довгота,широта) переводимо в Google Maps
        m = re.search(r"(?:pt|ll)=(4[45]\.\d+)(?:,|%2C)(4[01]\.\d+)", post_map)
        if m:
            return f"https://www.google.com/maps?q={float(m.group(2)):.6f},{float(m.group(1)):.6f}"
        # Google-посилання, де довгота й широта переплутані (44.x,41.x) — міняємо місцями
        m = re.search(r"maps/(?:search/|place/|@)?(4[45]\.\d+),\s*(4[01]\.\d+)", post_map)
        if m:
            return f"https://www.google.com/maps?q={float(m.group(2)):.6f},{float(m.group(1)):.6f}"
        return post_map
    lat, lng = lat or ev.get("lat") or 0, lng or ev.get("lng") or 0
    if 41 < lat < 42 and 44 < lng < 46:
        return f"https://www.google.com/maps?q={lat:.6f},{lng:.6f}"
    q = (ev.get("map_query") or "").strip()
    if q and q.lower() not in UNKNOWN:
        return "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(q)
    return None
