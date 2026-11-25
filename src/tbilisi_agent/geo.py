"""Транспорт біля квартири: найближче метро, автобусна зупинка і скільки приблизно їхати до центру.

Дані — OpenStreetMap (безкоштовно). Станції метро зашиті нижче, зупинки шукаємо на сервері Overpass,
адресу без координат — через Nominatim (не частіше 1 запиту на секунду, як просять їхні правила).
Надсилаємо туди лише адресу або координати квартири з оголошення — нічого про користувача."""
import json
import logging
import math
import threading
import time
import urllib.parse
import urllib.request

log = logging.getLogger("geo")
UA = "tbilisi-rent-agent/0.1 (personal apartment search)"
CENTER = (41.69452, 44.80056)  # площа Свободи
TBILISI_BOX = (41.60, 44.60, 41.85, 45.05)
UNKNOWN = ("не вказано", "не указано", "tbilisi")  # «невідомо» у відповіді Claude (укр. і рос.)

METRO = {  # OpenStreetMap, 2026-11
    "Ахметелі": (41.79106, 44.815), "Сараджишвілі": (41.78406, 44.79992), "Гурамішвілі": (41.77576, 44.79558),
    "Грмагеле": (41.76497, 44.78996), "Дідубе": (41.74945, 44.77999), "Гоцирідзе": (41.74294, 44.78405),
    "Надзаладеві": (41.7334, 44.79643), "Вокзальна площа": (41.72256, 44.7971), "Марджанішвілі": (41.70959, 44.79687),
    "Руставелі": (41.7035, 44.78965), "Площа Свободи": (41.69452, 44.80056), "Авлабарі": (41.69226, 44.81584),
    "300 Арагвелі": (41.68799, 44.82623), "Ісані": (41.68663, 44.83996), "Самгорі": (41.68552, 44.85448),
    "Варкетілі": (41.6919, 44.87088), "Церетелі": (41.72641, 44.78766), "Технічний університет": (41.72035, 44.77664),
    "Медичний університет": (41.72728, 44.76384), "Делісі": (41.72549, 44.74534), "Важа-Пшавела": (41.72403, 44.73085),
    "Державний університет": (41.72283, 44.71851),
}
OVERPASS = ("https://overpass.kumi.systems/api/interpreter", "https://overpass-api.de/api/interpreter")
_lock = threading.Lock()
_last = [0.0]
_cache: dict = {}


def dist_m(a, b) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371000 * 2 * math.asin(math.sqrt(h))


def in_tbilisi(p) -> bool:
    return bool(p) and TBILISI_BOX[0] < p[0] < TBILISI_BOX[2] and TBILISI_BOX[1] < p[1] < TBILISI_BOX[3]


def _get(url: str, data: bytes | None = None, timeout: float = 10):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, "Accept-Language": "uk,ru"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def geocode(query: str):
    """Адреса → (широта, довгота) у Тбілісі або None."""
    q = (query or "").strip()
    if not q or q.lower() in UNKNOWN:
        return None
    if q in _cache:
        return _cache[q]
    with _lock:  # правило Nominatim: не частіше разу на секунду
        time.sleep(max(0, 1.1 - (time.time() - _last[0])))
        _last[0] = time.time()
        try:
            r = _get("https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
                {"format": "json", "limit": 1, "countrycodes": "ge", "q": q}))
            p = (float(r[0]["lat"]), float(r[0]["lon"])) if r else None
        except Exception as ex:
            log.info("Адресу не знайдено: %s", type(ex).__name__)
            p = None
    p = p if in_tbilisi(p) else None
    _cache[q] = p
    return p


def bus_stop(p):
    """Найближча зупинка в радіусі 600 м: (назва, метри) або None."""
    key = (round(p[0], 3), round(p[1], 3))
    if key in _cache:
        return _cache[key]
    q = f'[out:json][timeout:8];node["highway"="bus_stop"](around:600,{p[0]},{p[1]});out;'
    deadline = time.time() + 8  # картку не затримуємо: не встигли — без зупинки
    for server in OVERPASS:
        if time.time() > deadline:
            break
        try:
            els = _get(server, urllib.parse.urlencode({"data": q}).encode(),
                       timeout=max(1, deadline - time.time()))["elements"]
            if not els:
                _cache[key] = None
                return None
            best = min(els, key=lambda e: dist_m(p, (e["lat"], e["lon"])))
            t = best.get("tags", {})
            _cache[key] = (t.get("name:uk") or t.get("name:ru") or t.get("name:en") or t.get("name") or "зупинка",
                           dist_m(p, (best["lat"], best["lon"])))
            return _cache[key]
        except Exception:
            continue
    return None


def transport(p) -> dict | None:
    """Метро, зупинка і приблизний час до центру для точки p=(широта, довгота)."""
    if not in_tbilisi(p):
        return None
    name, m_dist = min(((n, dist_m(p, xy)) for n, xy in METRO.items()), key=lambda x: x[1])
    km = dist_m(p, CENTER) / 1000
    stop = bus_stop(p) if m_dist > 700 else None
    # Варіанти шляху (хвилини), беремо найшвидший. Швидкості: пішки 80 м/хв, метро ~35 км/год,
    # автобус/маршрутка ~18 км/год дорогами (у 1.3 раза довше за пряму) + очікування.
    walk_stop = (stop[1] if stop else 400) / 80
    metro_km = dist_m(METRO[name], CENTER) / 1000
    options = {"пішки": km * 1300 / 80}
    to_metro = m_dist / 80 if m_dist <= 1200 else walk_stop + m_dist * 1.3 / 300 + 6
    options["метро" if m_dist <= 1200 else "автобус + метро"] = to_metro + metro_km / 0.58 + 4
    options["автобусом або маршруткою"] = walk_stop + km * 1.3 / 0.3 + 7
    how, minutes = min(options.items(), key=lambda x: x[1])
    minutes = max(5, int(round(minutes / 5.0)) * 5)
    return {"metro": name, "metro_m": int(m_dist), "stop": stop[0] if stop else None,
            "stop_m": int(stop[1]) if stop else None, "center_km": round(km, 1), "minutes": minutes, "how": how}


def walk_txt(m: int) -> str:
    return f"{max(1, round(m / 80))} хв пішки" if m <= 1500 else f"{m / 1000:.1f} км"


def summary(t: dict) -> str:
    """Короткий рядок для картки."""
    parts = [f"🚇 {t['metro']} — {walk_txt(t['metro_m'])}"]
    if t.get("stop"):
        parts.append(f"🚌 {t['stop']} — {walk_txt(t['stop_m'])}")
    parts.append(f"🏛 центр ≈ {t['minutes']} хв, {t['how']} ({t['center_km']} км)")
    return " · ".join(parts)
