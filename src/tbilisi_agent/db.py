"""Пам'ять агента: що вже бачив, кому й коли писав, чернетки для кнопок."""
import hashlib
import json
import re
import sqlite3
import time

from .config import DATA_DIR

_db: sqlite3.Connection | None = None


def conn() -> sqlite3.Connection:
    global _db
    if _db is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        DATA_DIR.chmod(0o700)
        # Оцінка оголошень іде в окремих потоках і читає відгуки — дозволяємо доступ із них
        _db = sqlite3.connect(DATA_DIR / "agent.db", check_same_thread=False)
        _db.executescript("""
        CREATE TABLE IF NOT EXISTS seen(key TEXT PRIMARY KEY, ts REAL);
        CREATE TABLE IF NOT EXISTS contacts(
            key TEXT PRIMARY KEY, first_ts REAL, last_ts REAL, count INTEGER);
        CREATE TABLE IF NOT EXISTS leads(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, data TEXT, status TEXT);
        CREATE TABLE IF NOT EXISTS sent(ts REAL);
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS feedback(
            lead_id INTEGER PRIMARY KEY, ts REAL, liked INTEGER, reason TEXT, summary TEXT);
        """)
        _db.commit()
    return _db


# ---------- повтори оголошень ----------

def text_key(text: str) -> str:
    norm = re.sub(r"[^\w]+", "", (text or "").lower())[:400]
    return "t:" + hashlib.sha1(norm.encode()).hexdigest()


def seen(*keys: str) -> bool:
    """True, якщо хоч один ключ уже траплявся. Усі ключі запам'ятовуються."""
    c = conn()
    keys = [k for k in keys if k]
    hit = any(c.execute("SELECT 1 FROM seen WHERE key=?", (k,)).fetchone() for k in keys)
    now = time.time()
    c.executemany("INSERT OR IGNORE INTO seen VALUES(?,?)", [(k, now) for k in keys])
    c.commit()
    return hit


def forget(*keys: str) -> None:
    """Забути ключі: оголошення не вдалося оцінити (збій Claude) — переглянемо його ще раз."""
    conn().executemany("DELETE FROM seen WHERE key=?", [(k,) for k in keys if k])
    conn().commit()


# ---------- контакти: перший раз чи повторно ----------

def contact_info(key: str):
    """(скільки разів писали, коли востаннє) або None, якщо ще не писали."""
    r = conn().execute("SELECT count, last_ts FROM contacts WHERE key=?", (key,)).fetchone()
    return r


def contact_written(key: str) -> None:
    c, now = conn(), time.time()
    c.execute("""INSERT INTO contacts VALUES(?,?,?,1)
                 ON CONFLICT(key) DO UPDATE SET last_ts=excluded.last_ts, count=count+1""",
              (key, now, now))
    c.commit()


def contact_sync(key: str, last_ts: float | None) -> None:
    """Узгодити пам'ять із листуванням: немає наших повідомлень — забути контакт,
    є — час останнього реального повідомлення."""
    c = conn()
    if last_ts is None:
        c.execute("DELETE FROM contacts WHERE key=?", (key,))
    else:
        c.execute("""INSERT INTO contacts VALUES(?,?,?,1)
                     ON CONFLICT(key) DO UPDATE SET last_ts=excluded.last_ts""", (key, last_ts, last_ts))
    c.commit()


def contact_seed(key: str, last_ts: float) -> None:
    """Запам'ятати, що листування вже було (знайдено в історії Telegram), з датою останнього повідомлення."""
    conn().execute("INSERT OR IGNORE INTO contacts VALUES(?,?,?,1)", (key, last_ts, last_ts))
    conn().commit()


# ---------- обмеження частоти автонадсилання ----------

def sent_counts() -> tuple[int, int]:
    now = time.time()
    c = conn()
    h = c.execute("SELECT COUNT(*) FROM sent WHERE ts>?", (now - 3600,)).fetchone()[0]
    d = c.execute("SELECT COUNT(*) FROM sent WHERE ts>?", (now - 86400,)).fetchone()[0]
    return h, d


def mark_sent() -> None:
    conn().execute("INSERT INTO sent VALUES(?)", (time.time(),))
    conn().commit()


# ---------- чернетки (для кнопок у боті) ----------

def add_lead(data: dict) -> int:
    c = conn()
    cur = c.execute("INSERT INTO leads(ts,data,status) VALUES(?,?,?)",
                    (time.time(), json.dumps(data, ensure_ascii=False), "new"))
    c.commit()
    return cur.lastrowid


def get_lead(lead_id: int):
    r = conn().execute("SELECT data,status FROM leads WHERE id=?", (lead_id,)).fetchone()
    return (json.loads(r[0]), r[1]) if r else (None, None)


def update_lead(lead_id: int, data: dict) -> None:
    conn().execute("UPDATE leads SET data=? WHERE id=?", (json.dumps(data, ensure_ascii=False), lead_id))
    conn().commit()


def set_lead_status(lead_id: int, status: str) -> None:
    conn().execute("UPDATE leads SET status=? WHERE id=?", (status, lead_id))
    conn().commit()


def _area(ev: dict):
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*м", str(ev.get("rooms_area") or ""))
    return float(m.group(1).replace(",", ".")) if m else None


def similar_lead(ev: dict, pt, phones, days=21):
    """Та сама квартира, уже надіслана з іншого сайту: той самий мобільний і ціна ±$15,
    або в межах ~150 м, ціна ±$15 і площа ±3 м². Повертає (id, дані) або None."""
    price, area = ev.get("price_usd") or 0, _area(ev)
    if not price:
        return None
    rows = conn().execute("SELECT id, data FROM leads WHERE ts>? ORDER BY id DESC",
                          (time.time() - days * 86400,)).fetchall()
    for lid, raw in rows:
        d = json.loads(raw)
        p2 = (d.get("ev") or {}).get("price_usd") or 0
        if not p2 or abs(p2 - price) > 15 or not d.get("msgs"):
            continue
        if set(phones or ()) & set(d.get("phones") or ()):
            return lid, d
        q, a2 = d.get("pt"), _area(d.get("ev") or {})
        if pt and q and area and a2 and abs(area - a2) <= 3 \
                and abs(pt[0] - q[0]) < 0.0014 and abs(pt[1] - q[1]) < 0.0018:
            return lid, d
    return None


# ---------- відгуки: на чому вчиться агент ----------

def add_feedback(lead_id: int, liked: bool, summary: str, reason: str = "") -> None:
    conn().execute("INSERT OR REPLACE INTO feedback VALUES(?,?,?,?,?)",
                   (lead_id, time.time(), int(liked), reason, summary))
    conn().commit()


def set_feedback_reason(lead_id: int, reason: str) -> None:
    conn().execute("UPDATE feedback SET reason=? WHERE lead_id=?", (reason, lead_id))
    conn().commit()


def all_feedback(limit: int = 80) -> list[tuple[int, str, str]]:
    """(чи сподобалося, опис, причина) — для висновків про смак."""
    return conn().execute("SELECT liked, summary, reason FROM feedback ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()


def recent_feedback(liked: bool, limit: int) -> list[tuple[str, str]]:
    return conn().execute("SELECT summary, reason FROM feedback WHERE liked=? ORDER BY ts DESC LIMIT ?",
                          (int(liked), limit)).fetchall()


def meta_get(k: str):
    r = conn().execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else None


def meta_set(k: str, v: str) -> None:
    conn().execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, v))
    conn().commit()
