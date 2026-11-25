"""Бот-сповіщувач: картки квартир, кнопки й меню у вашому основному Telegram."""
import asyncio
import html
import json
import secrets as pysecrets
import time
import logging
import urllib.parse
import urllib.request
import uuid

from . import db, secrets

log = logging.getLogger("bot")
e = html.escape  # усе зовнішнє екрануємо: текст оголошень чужий

# Постійне меню внизу чату
MENU_STATUS = "📊 Статус"
MENU_WEEK = "🔎 Переглянути тиждень"
MENU_PAUSE = "⏸ Пауза"
MENU_RESUME = "▶️ Продовжити"
MENU_HELP = "❓ Як це працює"
MENU_INVITE = "👥 Запросити"
MENU_MEMBERS = "👤 Учасники"
MENU_LEARN = "🧠 Чого навчився"
INVITE_TTL = 24 * 3600
OWNER = "власник"  # тимчасове ім'я першого учасника, доки не прийде ім'я з Telegram

# Підказки-«тости», після яких не треба окремого повідомлення (їх повертає main.py)
QUIET_TOASTS = ("Надіслано", "Натисніть", "Фото", "Згорнув", "Приховано")


def menu_keyboard(paused: bool) -> dict:
    return {"keyboard": [[{"text": MENU_STATUS}, {"text": MENU_WEEK}],
                         [{"text": MENU_RESUME if paused else MENU_PAUSE}, {"text": MENU_HELP}],
                         [{"text": MENU_INVITE}, {"text": MENU_MEMBERS}],
                         [{"text": MENU_LEARN}]],
            "resize_keyboard": True, "is_persistent": True}


def _name(u: dict) -> str:
    n = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x) or u.get("username") or "учасник"
    return n[:40]


class Bot:
    def __init__(self):
        self.token = secrets.need("bot_token")
        raw = db.meta_get("members")
        self.members: dict[str, str] = json.loads(raw) if raw else {}
        owner = db.meta_get("owner_chat_id")
        if owner and owner not in self.members:  # перехід зі старої версії: один власник
            self.members[owner] = OWNER
            self._save_members()
        self.username = None
        self.last_fids: list[str] = []

    def _save_members(self):
        db.meta_set("members", json.dumps(self.members, ensure_ascii=False))

    def is_member(self, chat_id) -> bool:
        return str(chat_id) in self.members

    # ---------- низький рівень ----------

    def _call(self, method: str, **params):
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/{method}", data=json.dumps(params).encode(),
            headers={"Content-Type": "application/json"})
        return self._open(req, method)

    def _upload(self, method: str, fields: dict, files: dict):
        """multipart/form-data — для фото з Telegram-чатів (байтами)."""
        b = uuid.uuid4().hex
        body = bytearray()
        for k, v in fields.items():
            body += f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
        for k, data in files.items():
            body += (f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{k}.jpg\"\r\n"
                     f"Content-Type: image/jpeg\r\n\r\n").encode() + data + b"\r\n"
        body += f"--{b}--\r\n".encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/{method}", data=bytes(body),
                                     headers={"Content-Type": f"multipart/form-data; boundary={b}"})
        return self._open(req, method)

    def _open(self, req, method):
        for attempt in range(3):  # мережа інколи блимає — пробуємо ще кілька разів
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return json.loads(r.read()).get("result")
            except urllib.error.HTTPError as ex:
                try:
                    why = json.loads(ex.read()).get("description", "")
                except Exception:
                    why = ""
                log.warning("Bot API %s: %s %s", method, ex.code, why[:200])
                return None
            except Exception as ex:  # мережа — не валимо агента
                log.warning("Bot API %s: %s %s (спроба %d)", method, type(ex).__name__,
                            str(getattr(ex, "reason", ""))[:100], attempt + 1)
                if method == "getUpdates":
                    return None
                time.sleep(3 * (attempt + 1))
        return None

    async def call(self, method: str, **params):
        return await asyncio.to_thread(self._call, method, **params)

    def _targets(self, to) -> list[str]:
        if to is None:
            return list(self.members)
        return [str(t) for t in (to if isinstance(to, (list, tuple, set)) else [to])]

    # ---------- надсилання ----------

    async def send(self, text: str, markup: dict | None = None, to=None):
        """Повідомлення учасникам (за замовчуванням — усім). Повертає {chat_id: message_id}."""
        out = {}
        for chat in self._targets(to):
            p = {"chat_id": chat, "text": text[:4000], "parse_mode": "HTML",
                 "link_preview_options": {"is_disabled": True}}
            if markup:
                p["reply_markup"] = markup
            r = await self.call("sendMessage", **p)
            if r:
                out[chat] = r["message_id"]
        return out

    async def send_card(self, caption: str, buttons: list[list[dict]], photo=None, to=None):
        """Одна картка = одне повідомлення: головне фото + текст + кнопки. Повертає {chat_id: message_id}."""
        markup = {"inline_keyboard": buttons}
        if photo is None:
            return await self.send(caption, markup, to)
        caption = caption[:1024]
        out = {}
        for chat in self._targets(to):
            if isinstance(photo, str):
                r = await self.call("sendPhoto", chat_id=chat, photo=photo, caption=caption,
                                    parse_mode="HTML", reply_markup=markup)
            else:
                r = await asyncio.to_thread(self._upload, "sendPhoto", {
                    "chat_id": chat, "caption": caption, "parse_mode": "HTML",
                    "reply_markup": json.dumps(markup), "photo": "attach://p0"}, {"p0": photo})
            if r:
                out[chat] = r["message_id"]
            else:  # фото не пішло — надсилаємо хоча б текст
                out.update(await self.send(caption, markup, chat))
        return out

    async def set_buttons(self, msgs: dict, buttons: list[list[dict]]):
        """Змінити кнопки в картці в усіх чатах, куди вона пішла."""
        for chat, mid in (msgs or {}).items():
            if chat in self.members:
                await self.call("editMessageReplyMarkup", chat_id=chat, message_id=mid,
                                reply_markup={"inline_keyboard": buttons})

    async def send_rich(self, html_text: str, buttons, photos=(), to=None) -> dict:
        """Картка нового формату Telegram: слайдшоу фото + розгортувані розділи + кнопки.
        Фото завантажуються один раз, іншим учасникам ідуть за file_id. Повертає {chat_id: message_id}."""
        photos = [p for p in photos if p][:10]
        out = {}
        markup = {"inline_keyboard": buttons}
        for chat in self._targets(to):
            media, files = [], {}
            for i, ph in enumerate(photos):
                if isinstance(ph, bytes):
                    files[f"f{i}"] = ph
                    src = f"attach://f{i}"
                else:
                    src = ph
                media.append({"id": f"p{i}", "media": {"type": "photo", "media": src}})
            rm = {"html": html_text}
            if media:
                rm["media"] = media
            if files:
                r = await asyncio.to_thread(self._upload, "sendRichMessage", {
                    "chat_id": chat, "rich_message": json.dumps(rm, ensure_ascii=False),
                    "reply_markup": json.dumps(markup)}, files)
            else:
                r = await self.call("sendRichMessage", chat_id=chat, rich_message=rm, reply_markup=markup)
            if not r:
                return out  # не вийшло — нехай той, хто викликав, надішле по-старому
            out[chat] = r["message_id"]
            fids = _rich_file_ids(r)
            if len(fids) == len(photos):
                photos = fids  # іншим — миттєво, без повторного завантаження
                self.last_fids = fids
        return out

    async def edit_rich(self, chat, msg_id, html_text: str, photos, buttons):
        """Замінити вміст картки (наприклад, згорнути до «Не підходить»). Фото — file_id."""
        rm = {"html": html_text}
        if photos:
            rm["media"] = [{"id": f"p{i}", "media": {"type": "photo", "media": p}} for i, p in enumerate(photos)]
        return await self.call("editMessageText", chat_id=chat, message_id=msg_id, rich_message=rm,
                               reply_markup={"inline_keyboard": buttons})

    async def set_markup(self, chat, msg_id, buttons):
        await self.call("editMessageReplyMarkup", chat_id=chat, message_id=msg_id,
                        reply_markup={"inline_keyboard": buttons})

    async def send_album(self, photos, chat, reply_to: int | None = None) -> list[tuple[int, str]]:
        """Усі фото альбомом у відповідь на картку. Фото — посилання, file_id або байти.
        Повертає [(message_id, file_id)] — щоб потім згорнути й показати повторно миттєво."""
        photos = [p for p in photos if p][:10]
        if not photos:
            return []
        rp = {"message_id": reply_to, "allow_sending_without_reply": True} if reply_to else None
        files, media = {}, []
        for i, ph in enumerate(photos):
            if isinstance(ph, bytes):
                files[f"p{i}"] = ph
                media.append({"type": "photo", "media": f"attach://p{i}"})
            else:
                media.append({"type": "photo", "media": ph})
        single = len(media) == 1
        if files:
            fields = {"chat_id": chat}
            if rp:
                fields["reply_parameters"] = json.dumps(rp)
            if single:
                fields["photo"] = media[0]["media"]
            else:
                fields["media"] = json.dumps(media)
            r = await asyncio.to_thread(self._upload, "sendPhoto" if single else "sendMediaGroup", fields, files)
        else:
            extra = {"reply_parameters": rp} if rp else {}
            r = await (self.call("sendPhoto", chat_id=chat, photo=media[0]["media"], **extra) if single else
                       self.call("sendMediaGroup", chat_id=chat, media=media, **extra))
        msgs = [r] if isinstance(r, dict) else (r or [])
        return [(m["message_id"], (m.get("photo") or [{}])[-1].get("file_id", "")) for m in msgs]

    async def delete(self, chat, ids: list[int]):
        if ids:
            await self.call("deleteMessages", chat_id=chat, message_ids=ids[:100])

    async def setup_menu(self):
        me = await self.call("getMe")
        self.username = (me or {}).get("username")
        await self.call("setMyCommands", commands=[
            {"command": "status", "description": "Статус агента"},
            {"command": "week", "description": "Переглянути оголошення за тиждень"},
            {"command": "pause", "description": "Пауза"},
            {"command": "resume", "description": "Продовжити"},
            {"command": "invite", "description": "Запросити людину"},
            {"command": "members", "description": "Учасники"},
            {"command": "learn", "description": "Чого навчився агент"},
            {"command": "help", "description": "Як це працює"},
        ])

    # ---------- учасники ----------

    def new_invite(self) -> str:
        code = pysecrets.token_urlsafe(12)
        db.meta_set("invite", json.dumps({"code": code, "exp": time.time() + INVITE_TTL}))
        return f"https://t.me/{self.username}?start={code}"

    def _take_invite(self, code: str) -> bool:
        raw = db.meta_get("invite")
        inv = json.loads(raw) if raw else None
        if not inv or not code or time.time() > inv["exp"] or not pysecrets.compare_digest(code, inv["code"]):
            return False
        db.meta_set("invite", "")  # одноразове
        return True

    async def members_message(self, chat):
        rows, lines = [], []
        for cid, name in self.members.items():
            me = " (ви)" if cid == str(chat) else ""
            lines.append(f"• {e(name)}{me}")
            if cid != str(chat):
                rows.append([{"text": f"❌ Відключити {name[:25]}", "callback_data": f"rm:{cid}"}])
        await self.send("<b>👤 Учасники</b>\n\n" + "\n".join(lines) +
                        "\n\nУсім приходять однакові картки. Запросити ще — «👥 Запросити».",
                        {"inline_keyboard": rows} if rows else None, chat)

    async def remove_member(self, cid: str, by: str) -> str:
        if cid not in self.members or len(self.members) <= 1:
            return "Не можна відключити"
        name = self.members.pop(cid)
        self._save_members()
        await self.send("Вас відключили від бота пошуку квартир.", {"remove_keyboard": True}, cid)
        await self.send(f"👤 {e(by)} відключив(-ла): {e(name)}")
        return f"{name} відключено"

    # ---------- приймання натискань ----------

    async def poll(self, on_button, on_text):
        """Безкінечно отримує натискання кнопок і команди. Чужих користувачів ігнорує."""
        offset = int(db.meta_get("bot_offset") or 0)
        while True:
            ups = await self.call("getUpdates", offset=offset, timeout=60,
                                  allowed_updates=["message", "callback_query"])
            for u in ups or []:
                offset = u["update_id"] + 1
                db.meta_set("bot_offset", str(offset))
                # кожне натискання — окремим завданням, щоб довгі справи не затримували відповіді на кнопки
                asyncio.create_task(self._handle_safe(u, on_button, on_text))
            if ups is None:
                await asyncio.sleep(10)

    async def _handle_safe(self, u, on_button, on_text):
        try:
            await self._handle(u, on_button, on_text)
        except Exception:
            log.exception("Помилка обробки кнопки")

    async def _handle(self, u, on_button, on_text):
        if "message" in u:
            m = u["message"]
            chat, txt = str(m["chat"]["id"]), (m.get("text") or "").strip()
            if m["chat"]["type"] != "private" or not txt:
                return
            if self.is_member(chat):
                if self.members[chat] in (OWNER, "владелец"):  # ім'я першого учасника беремо з Telegram
                    self.members[chat] = _name(m.get("from") or {})
                    self._save_members()
                await on_text(txt, chat, self.members[chat])
            elif txt.startswith("/start ") and self._take_invite(txt[7:].strip()):
                name = _name(m.get("from") or {})
                self.members[chat] = name
                self._save_members()
                await self.send(f"👥 <b>{e(name)}</b> приєднався(-лася) до пошуку квартир.",
                                to=[c for c in self.members if c != chat])
                await on_text("/help", chat, name)
            return
        cq = u.get("callback_query")
        if not cq:
            return
        chat = str(cq["from"]["id"])
        if not self.is_member(chat):
            await self.call("answerCallbackQuery", callback_query_id=cq["id"])
            return
        msg = cq.get("message") or {}
        data = cq.get("data", "")
        if data.startswith("rm:"):
            toast = await self.remove_member(data[3:], self.members[chat])
            await self.call("answerCallbackQuery", callback_query_id=cq["id"], text=toast[:190])
            return
        # Telegram чекає відповіді на натискання ~15 с. Довгі дії (надсилання орендодавцю) не встигають —
        # тоді одразу відповідаємо «⏳», а підсумок видно на кнопці картки.
        task = asyncio.create_task(on_button(data, chat, self.members[chat], msg.get("message_id")))
        try:
            toast = await asyncio.wait_for(asyncio.shield(task), timeout=8)
        except asyncio.TimeoutError:
            await self.call("answerCallbackQuery", callback_query_id=cq["id"], text="⏳ Роблю, це займе трохи часу…")
            toast = await task
            if toast and not toast.startswith(QUIET_TOASTS):
                await self.send(f"ℹ️ {e(toast)}", to=chat)  # наприклад, «Не вийшло: …»
            return
        await self.call("answerCallbackQuery", callback_query_id=cq["id"], text=toast[:190])


# ---------- оформлення картки ----------

def _cut(s, n=70) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def _known(s) -> bool:
    s = str(s or "").strip().lower()
    return bool(s) and not s.startswith(("не вказ", "не указ", "невідом", "неизвест", "немає даних", "нет данных", "—", "-"))


def card(ev: dict, link: str | None, source: str, header: str, footer: str = "") -> str:
    """Коротка картка: головне зверху, подробиці — під «розгорнути». До 1024 символів."""
    price = f"${ev['price_usd']}" if ev.get("price_usd") else "ціна ?"
    title = ("⭐ " if ev.get("highlight") else "") + _cut(ev.get("short") or "Квартира", 60)
    top = [header, "", f"<b>{e(title)}</b>",
           f"💵 <b>{e(price)}</b>/міс  ·  🏠 {e(_cut(ev.get('rooms_area'), 40))}"]
    place = ", ".join(_cut(x, 40) for x in (ev.get("district"), ev.get("address")) if _known(x))
    if place:
        top.append(f"📍 {e(place)}")
    if _known(ev.get("transport")):
        top.append(f"🚇 {e(_cut(ev['transport'], 60))}")

    lists = []
    pros = [p for p in ev.get("pros") or [] if p.strip()][:3]
    cons = [c for c in ev.get("cons") or [] if c.strip()][:3]
    if pros:
        lists.append("\n".join(f"✅ {e(_cut(p, 55))}" for p in pros))
    if cons:
        lists.append("\n".join(f"⚠️ {e(_cut(c, 55))}" for c in cons))

    details = []
    for icon, key in (("🔥 Опалення", "heating"), ("🛋 Меблі", "furniture"), ("🧱 Ремонт", "renovation"),
                      ("❄️ Кондиц./інтернет", "ac_internet"), ("🌳 Поруч", "nearby"), ("📄 Умови", "terms")):
        val = ev.get(key)
        details.append(f"{icon}: {e(_cut(val, 80)) if _known(val) else '<i>не вказано</i>'}")
    qs = [q for q in ev.get("questions") or [] if q.strip()][:3]
    if qs:
        details.append("❓ Уточнити: " + e(" ".join(_cut(q, 70) for q in qs)))

    src = f"<a href=\"{e(link, quote=True)}\">Відкрити оголошення</a>" if link else ""
    tail = " · ".join(x for x in (src, e(_cut(source, 40))) if x)

    def build(det):
        parts = ["\n".join(top)] + lists
        if det:
            parts.append("<blockquote expandable>" + "\n".join(det) + "</blockquote>")
        if footer:
            parts.append(footer)
        parts.append(tail)
        return "\n\n".join(parts)

    out = build(details)
    while len(out) > 1024 and details:  # не влазить у підпис до фото — прибираємо подробиці з кінця
        details.pop()
        out = build(details)
    return out[:1024]


def _rich_file_ids(msg: dict) -> list[str]:
    out = []
    for block in (msg.get("rich_message") or {}).get("blocks") or []:
        if block.get("type") == "slideshow":
            for b in block.get("blocks") or []:
                if b.get("photo"):
                    out.append(b["photo"][-1]["file_id"])
    return out


def links_html(links: list[tuple[str, str]]) -> str:
    return " · ".join(f'<a href="{e(u, quote=True)}">{label}</a>' for label, u in links if u)


def rejected_card(ev: dict, who: str, links: list[tuple[str, str]], n_photos: int) -> str:
    """Згорнута картка «Не підходить»: карусель фото, що це було; посилання — кнопками."""
    price = f"${ev['price_usd']}" if ev.get("price_usd") else ""
    what = " · ".join(x for x in (price, _cut(ev.get("short"), 60)) if x)
    slides = "".join(f'<img src="tg://photo?id=p{i}">' for i in range(n_photos))
    return ((f"<tg-slideshow>{slides}</tg-slideshow>" if n_photos > 1 else slides)
            + f"<h3>❌ Не підходить</h3><p>{e(what)}</p><p><i>Позначив(-ла) {e(who)}. Посилання — у кнопках нижче.</i></p>")


def rich_card(ev: dict, links: list[tuple[str, str]], source: str, header: str, n_photos: int,
              extra: list[tuple[str, str]] = (), transport: str = "") -> str:
    """Картка в стилі оголошень-постів: фото гортаються свайпом, подробиці розгортаються."""
    parts = []
    if n_photos:
        parts.append("<tg-slideshow>" + "".join(f'<img src="tg://photo?id=p{i}">' for i in range(n_photos))
                     + "</tg-slideshow>")
    price = f"${ev['price_usd']}" if ev.get("price_usd") else "ціна ?"
    title = " · ".join(x for x in (price, _cut(ev.get("short"), 50)) if x)
    parts.append(f"<p>{header}</p>")
    parts.append(f"<h3>{'⭐ ' if ev.get('highlight') else ''}{e(title)}</h3>")
    info = []
    if _known(ev.get("rooms_area")):
        info.append(f"🏠 {e(_cut(ev['rooms_area'], 60))}")
    place = ", ".join(_cut(x, 50) for x in (ev.get("district"), ev.get("address")) if _known(x))
    if place:
        info.append(f"📍 {e(place)}")
    if transport:  # пораховано за мапою: метро, зупинка, час до центру
        info.append(e(transport))
    elif _known(ev.get("transport")):
        info.append(f"🚇 {e(_cut(ev['transport'], 60))}")
    parts += [f"<p>{x}</p>" for x in info]
    good = [e(_cut(p, 70)) for p in (ev.get("pros") or [])[:4] if p.strip()]
    bad = [e(_cut(c, 70)) for c in (ev.get("cons") or [])[:4] if c.strip()]
    if good:
        parts.append(f"<details><summary>✅ Плюси ({len(good)})</summary><ul>"
                     + "".join(f"<li>{x}</li>" for x in good) + "</ul></details>")
    if bad:
        parts.append(f"<details><summary>⚠️ Мінуси ({len(bad)})</summary><ul>"
                     + "".join(f"<li>{x}</li>" for x in bad) + "</ul></details>")

    rows = []
    for icon, key in (("🔥 Опалення", "heating"), ("🛋 Меблі й техніка", "furniture"), ("🧱 Ремонт", "renovation"),
                      ("❄️ Кондиціонер, інтернет", "ac_internet"), ("🌳 Поруч", "nearby"), ("📄 Умови, депозит", "terms")):
        val = ev.get(key)
        rows.append(f"<li><b>{icon}:</b> {e(_cut(val, 120)) if _known(val) else '<i>не вказано</i>'}</li>")
    parts.append(f"<details><summary>Параметри й зручності</summary><ul>{''.join(rows)}</ul></details>")
    qs = [q for q in ev.get("questions") or [] if q.strip()][:3]
    if qs:
        parts.append("<details><summary>❓ Що уточнити в орендодавця</summary><ul>"
                     + "".join(f"<li>{e(_cut(q, 150))}</li>" for q in qs) + "</ul></details>")
    for summary, body in extra:
        parts.append(f"<details><summary>{summary}</summary><p>{body}</p></details>")
    parts.append(f"<p><i>{e(_cut(source, 50))}</i></p>")  # посилання — кнопками під карткою
    return "".join(parts)


def wa_link(phone: str, text: str) -> str:
    return f"https://wa.me/{phone}?text={urllib.parse.quote(text)}"
