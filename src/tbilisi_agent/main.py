"""Головний цикл: Telegram-чати + сайти → оцінка Claude → повідомлення орендодавцю або картка вам."""
import asyncio
import logging
import logging.handlers
import re
import time
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient, errors, events
from telethon.sessions import StringSession
from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl, User

from . import bot as botmod
from . import compose, db, geo, judge, pages, secrets, sites, ssge
from .config import (AUTO_SEND_PER_DAY, AUTO_SEND_PER_HOUR, DATA_DIR, GEL_PER_USD, MAX_PHOTOS, MAX_USD,
                     RENT_CHAT_WORDS)

log = logging.getLogger("agent")

# Слова з постів у чатах оренди (російська, англійська, грузинська) — це текст чужих оголошень, не перекладаємо
RENT_WORDS = re.compile(r"аренд|сда[мюёе]|сдаётся|сдается|rent|ქირ|\$|usd|gel|лари|ლარ|₾|долл", re.I)
DEMAND_WORDS = re.compile(r"\bищу\b|\bсниму\b|\blooking for\b|ищем квартиру", re.I)
PHONE_RE = re.compile(r"(?:\+?995[\s\-()]*)?(5\d{2})[\s\-]?(\d{2,3})[\s\-]?(\d{2})[\s\-]?(\d{2,3})")
WEEK_DAYS = 7

HELP = """<b>Як працює агент</b>

Він читає чати про оренду (через другий акаунт) і сайти ss.ge, korter.ge, livo.ge, place.ge, rentor.ge, а кожне оголошення оцінює Claude за вашим завданням.

🔥 <b>Сильний варіант</b> — якщо контакт у Telegram, агент сам пише орендодавцю (не більше 6 на годину і 25 на добу) і надсилає вам картку «Написав сам».
🤔 <b>Є сумніви</b> — картка з кнопками, вирішуєте ви.

<b>Кнопки в картці</b>
✉️ Telegram — агент напише автору з другого акаунта
📲 WhatsApp — відкриється WhatsApp із готовим текстом, вам лишається натиснути «Надіслати»
Фото гортаються свайпом просто в картці, «Параметри й зручності» розгортаються натисканням
👎 Не підходить — прибрати варіант і підказати причину, щоб агент навчився

Перше повідомлення людині — з привітанням і «дякую», повторне — без привітання й коротко.

<b>Удвох</b>
👥 Запросити — одноразове посилання для другої людини. Картки приходять усім, а коли хтось написав або приховав варіант, в інших це видно на кнопках.
👤 Учасники — хто підключений, там же можна відключити."""


def norm_phone(p: str) -> str | None:
    d = re.sub(r"\D", "", p)
    if d.startswith("995"):
        d = d[3:]
    return "995" + d if len(d) == 9 and d.startswith("5") else None


def too_expensive(text: str) -> bool:
    """Грубий попередній фільтр: явна ціна значно вища за бюджет. Сумнівне пропускаємо далі."""
    usd = []
    for a, b in re.findall(r"\$\s?(\d{3,5})|(\d{3,5})\s?(?:\$|usd|долл)", text, re.I):
        usd.append(int(a or b))
    gel = [int(x) for x in re.findall(r"(\d{3,5})\s?(?:gel|лари|ლარ|₾)", text, re.I)]
    prices = usd + [round(g / GEL_PER_USD) for g in gel]
    return bool(prices) and min(prices) > MAX_USD * 1.15


def rich_post(m) -> tuple[str, list[str], list]:
    """Пост нового формату Telegram (rich message): текст, посилання і фото з блоків."""
    rm = getattr(m, "rich_message", None)
    if not rm:
        return "", [], []
    out, urls = [], []

    def walk(o):
        if isinstance(o, list):
            for x in o:
                walk(x)
            return
        if not isinstance(o, dict):
            return
        kind = o.get("_", "")
        if kind == "TextPlain":
            out.append(o.get("text") or "")
        elif kind == "TextStrike":  # закреслена стара ціна
            out.append(" (було ")
            walk(o.get("text"))
            out.append(")")
        elif kind == "TextUrl":
            walk(o.get("text"))
            if o.get("url"):
                urls.append(o["url"])
        elif kind in ("PageBlockPhoto", "PageBlockSlideshow"):
            return  # фото беремо окремо
        else:
            if isinstance(o.get("url"), str) and o["url"].startswith("http"):
                urls.append(o["url"])  # кнопки всередині поста: «Зв'язатися», «На мапі»
            for v in o.values():
                if isinstance(v, (dict, list)):
                    walk(v)
            if kind.startswith("PageBlock") or kind in ("PageTableRow",):
                out.append("\n")
            elif kind == "PageTableCell":
                out.append(" | ")

    walk(rm.to_dict().get("blocks") or [])
    text = re.sub(r"[ \t]+", " ", "".join(out))
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip(" ⠀"))
    return text, list(dict.fromkeys(urls)), list(getattr(rm, "photos", None) or [])


WHY = {"repair": "ремонт або меблі", "area": "район", "price": "ціна", "size": "маленька або незручна",
       "other": "інше"}
WHY_ROWS = [[("🏚 Ремонт/меблі", "repair"), ("📍 Район", "area")],
            [("💵 Ціна", "price"), ("📐 Маленька", "size"), ("🤷 Інше", "other")]]


class Agent:
    def __init__(self):
        self.tg: TelegramClient | None = None
        self.bot = botmod.Bot()
        self.me_id = None
        self.backfill_running = False
        self.stats = {"checked": 0, "found": 0}
        self.backfill_where = ""

    @property
    def paused(self) -> bool:
        return db.meta_get("paused") == "1"

    # ---------- спільний розбір знайденого варіанта ----------

    async def handle(self, ev: dict | None, source: str, card_link: str | None, public_link: str | None,
                     tg_target, wa_phones: list[str], photos=(), photo_ref=None, to_backlog=False, src=None,
                     extra: dict | None = None):
        self.stats["checked"] += 1
        db.meta_set("last_checked", str(time.time()))
        if not ev:
            return
        if ev.get("listing_active") is False:
            log.info("Знято із сайту (%s): %s", source, ev.get("short"))
            return
        if (ev.get("condition_score") or 10) <= 5 and ev.get("verdict") != "reject":
            ev["verdict"], ev["reject_reason"] = "reject", f"Стан за фото {ev['condition_score']}/10 — старий ремонт"
        if ev.get("verdict") == "strong" and (ev.get("condition_score") or 0) < 7:
            ev["verdict"] = "maybe"
        if not ev.get("is_offer") or ev.get("verdict") == "reject":
            log.info("Відсіяно (%s): %s", source, ev.get("reject_reason") or "не пропозиція")
            return
        keys = [f"pp:{p}:{ev.get('price_usd')}" for p in wa_phones if ev.get("price_usd")]
        if keys and db.seen(*keys):
            log.info("Дублікат за телефоном і ціною: %s", ev.get("short"))
            hit = db.similar_lead(ev, None, wa_phones)
            if hit and extra and extra.get("site"):
                await self.add_twin(hit, source, extra["site"])
            return
        self.stats["found"] += 1
        lead = {"ev": ev, "source": source, "card_link": card_link, "public_link": public_link,
                "tg": tg_target, "phones": wa_phones, "photo_ref": photo_ref, "src": src, **(extra or {})}
        # Знайдене (і наживо, і під час перебору тижня) надсилаємо одразу, по одному
        await self.deliver(lead, photos, allow_auto=True)

    async def add_twin(self, hit, source: str, url: str):
        """Відповідь під старою карткою: ця сама квартира є ще на одному сайті."""
        lid, old = hit
        twins = old.setdefault("twins", [])
        if url in twins:
            return
        twins.append(url)
        db.update_lead(lid, old)
        text = f'🔁 Ця сама квартира є і на {botmod.e(source)}: <a href="{botmod.e(url)}">відкрити</a>'
        for chat, mid in (old.get("msgs") or {}).items():
            await self.bot.call("sendMessage", chat_id=chat, text=text, parse_mode="HTML",
                                link_preview_options={"is_disabled": True},
                                reply_parameters={"message_id": mid, "allow_sending_without_reply": True})

    @staticmethod
    def links(lead: dict) -> list[tuple[str, str]]:
        """Підписані посилання: де пост, де сайт, де мапа."""
        site = lead.get("site")
        post = lead.get("post") or (lead.get("card_link") if not site else None)
        out = []
        if post:
            out.append(("📢 Пост у Telegram", post))
        if site:
            out.append((f"🌐 Оголошення на {pages.host_of(site)}", site))
        if lead.get("map"):
            out.append(("🗺 На мапі", lead["map"]))
        return out

    def link_row(self, lead: dict) -> list[list[dict]]:
        btn = {"📢 Пост у Telegram": "📢 Пост", "🗺 На мапі": "🗺 На мапі"}
        row = [{"text": btn.get(label, "🌐 Сайт"), "url": url} for label, url in self.links(lead)]
        return [row] if row else []

    async def deliver(self, lead: dict, photos, allow_auto: bool):
        """Картка варіанта всім учасникам: новий формат (слайдшоу), за збою — звичайна з одним фото."""
        ev = lead["ev"]
        photos = [p for p in (photos or []) if p][:10]
        # Де квартира — потрібно і для картки, і щоб упізнати ту саму квартиру з іншого сайту
        p = pages.coords(lead.get("map")) or await asyncio.to_thread(geo.geocode, ev.get("map_query"))
        lead["pt"] = list(p) if p else None
        hit = db.similar_lead(ev, p, lead["phones"]) if lead.get("site") else None
        if hit and pages.host_of(hit[1].get("site") or hit[1].get("card_link") or "") != pages.host_of(lead["site"]):
            log.info("Повтор із %s: %s", lead["source"], ev.get("short"))
            await self.add_twin(hit, lead["source"], lead["site"])
            return
        strong = ev.get("verdict") == "strong"
        extra, actions = [], None
        if lead.get("note"):
            extra.append(("ℹ️ Важливо", botmod.e(lead["note"])))
        header = "🔥 <b>Сильний варіант</b>" if strong else "🤔 <b>Можна подивитися</b>"
        if allow_auto and strong and lead["tg"] and self.can_autosend() and (ev.get("condition_score") or 0) >= 8:
            # Друга, уважніша перевірка фото, перш ніж писати людині від вашого імені
            urls = [p for p in photos if isinstance(p, str)]
            raw = [p for p in photos if isinstance(p, bytes)]
            chk = await asyncio.to_thread(judge.verify, lead.get("text", ""), raw, urls)
            lead["check"] = chk
            log.info("Друга перевірка: %s (%s/10) — %s", chk["approve"], chk["score"], chk["reason"])
            if not chk["approve"]:
                extra.append(("🔍 Чому не написав сам", botmod.e(chk["reason"])))
                allow_auto = False
        elif allow_auto and strong and lead["tg"] and (ev.get("condition_score") or 0) < 8:
            allow_auto = False
            extra.append(("🔍 Чому не написав сам",
                          f"Стан за фото {ev.get('condition_score')}/10 — пишу сам лише від 8/10."))
        if allow_auto and strong and lead["tg"] and self.can_autosend():
            ok, info = await self.send_tg(lead)
            if ok:
                header = "✉️ <b>Написав сам у Telegram</b>"
                extra.append(("✉️ Що я написав", botmod.e(info[:900])))
                actions = []
                lead["_sent"] = True
            else:
                log.warning("Автонадсилання не вдалося: %s", info)
        lead_id = db.add_lead(lead)
        if lead.pop("_sent", False):
            db.set_lead_status(lead_id, "sent")
        no_row = [{"text": "👎 Не підходить", "callback_data": f"no:{lead_id}"}]
        if actions is None:
            row = []
            if lead["tg"]:
                row.append({"text": "✉️ Написати в Telegram", "callback_data": f"tg:{lead_id}"})
            if lead["phones"]:
                row.append({"text": "📲 WhatsApp", "callback_data": f"wa:{lead_id}"})
            if not row:
                extra.append(("📞 Контакт", "Контакту не знайшов ні в пості, ні на сайті — подивіться за посиланням."))
            elif lead["phones"] and not lead["tg"]:
                extra.append(("📞 Якщо немає WhatsApp", "Якщо в номера немає WhatsApp, застосунок так і скаже — тоді телефонуйте."))
            actions = ([row] if row else []) + [no_row]
        else:
            actions = [no_row]
        lead["actions"] = actions
        lead["rich"] = True
        # Де квартира: координати з посилання на мапу, інакше шукаємо адресу; далі — метро, зупинка, центр
        if p and not lead.get("map"):
            lead["map"] = f"https://www.google.com/maps?q={p[0]:.6f},{p[1]:.6f}"
        t = await asyncio.to_thread(geo.transport, p) if p else None
        lead["transport"] = geo.summary(t) if t else ""
        html_text = botmod.rich_card(ev, self.links(lead), lead["source"], header, len(photos), extra,
                                     lead["transport"])
        self.bot.last_fids = []
        lead["msgs"] = await self.bot.send_rich(html_text, self.markup(lead_id, lead, None), photos)
        lead["fids"] = self.bot.last_fids
        if len(lead["msgs"]) < len(self.bot.members):  # новий формат не пройшов — по-старому
            lead["rich"] = False
            lead["has_photo"] = bool(photos)
            lead["caption"] = botmod.card(ev, lead.get("site") or lead["card_link"], lead["source"], header)
            rest = [c for c in self.bot.members if c not in lead["msgs"]]
            lead["msgs"].update(await self.bot.send_card(lead["caption"], self.markup(lead_id, lead, None),
                                                         photos[0] if photos else None, rest))
        db.update_lead(lead_id, lead)

    # ---------- кнопки картки й гортання фото ----------

    @staticmethod
    def photo_count(lead: dict) -> int:
        ref = lead.get("photo_ref") or {}
        return len(ref.get("urls") or ref.get("ids") or [])

    def markup(self, lead_id: int, lead: dict, chat, actions=None) -> list[list[dict]]:
        """Кнопки дій (спільні для всіх) + кнопка фото (у кожного своя: показати чи згорнути)."""
        rows = list(actions if actions is not None else lead.get("actions") or []) + self.link_row(lead)
        n = 0 if lead.get("rich") else self.photo_count(lead)
        if n:
            if chat and (lead.get("album") or {}).get(str(chat)):
                rows.append([{"text": "✖️ Згорнути фото", "callback_data": f"hide:{lead_id}"}])
            else:
                rows.append([{"text": f"📷 Усі фото ({n})", "callback_data": f"ph:{lead_id}"}])
        return rows

    def fill_old(self, lead_id: int, lead: dict, status: str):
        """Картки з минулих версій: відновити текст і кнопки, щоб гортання їх не стерло."""
        if "caption" not in lead:
            lead["caption"] = botmod.card(lead["ev"], lead.get("card_link"), lead.get("source", ""),
                                          "🤔 <b>Варіант</b>")
        if "actions" not in lead:
            row = []
            if status == "new":
                if lead.get("tg"):
                    row.append({"text": "✉️ Telegram", "callback_data": f"tg:{lead_id}"})
                if lead.get("phones"):
                    row.append({"text": "📲 WhatsApp", "callback_data": f"wa:{lead_id}"})
                row.append({"text": "🙈 Приховати", "callback_data": f"skip:{lead_id}"})
            lead["actions"] = [row] if row else []
        lead.setdefault("has_photo", bool(lead.get("photo_ref")))

    async def refresh(self, lead_id: int, lead: dict, msgs: dict, actions=None):
        for c, mid in msgs.items():
            if c in self.bot.members:
                await self.bot.set_markup(c, mid, self.markup(lead_id, lead, c, actions))

    async def all_photos(self, lead: dict) -> list:
        """Усі фото квартири: спершу з кешу Telegram (миттєво), інакше посилання або завантажені з чату."""
        fids = lead.get("fids") or []
        n = min(self.photo_count(lead), 10)
        if fids and len(fids) >= n:
            return fids[:n]
        ref = lead.get("photo_ref") or {}
        if ref.get("urls"):
            return ref["urls"][:10]
        try:
            msgs = await self.tg.get_messages(ref["chat"], ids=ref["ids"][:10])
            return await asyncio.gather(*[self.tg.download_media(m, file=bytes) for m in msgs if m and m.photo])
        except Exception as ex:
            log.warning("Не завантажив фото: %s", type(ex).__name__)
            return []

    async def show_album(self, lead_id: int, lead: dict, chat: str, card_msg) -> str:
        if (lead.get("album") or {}).get(chat):
            return "Фото вже відкриті нижче 👇"
        photos = await self.all_photos(lead)
        if not photos:
            return "Фото вже недоступні"
        sent = await self.bot.send_album(photos, chat, card_msg)
        if not sent:
            return "Не вийшло надіслати фото"
        ids = [m for m, _ in sent]
        closer = await self.bot.send("👆 Фото квартири. Натисніть на перше й гортайте свайпом.",
                                     {"inline_keyboard": [[{"text": "✖️ Згорнути фото", "callback_data": f"hide:{lead_id}"}]]},
                                     chat)
        ids += list(closer.values())
        lead.setdefault("album", {})[chat] = ids
        if all(f for _, f in sent):
            lead["fids"] = [f for _, f in sent]  # наступного разу — миттєво
        db.update_lead(lead_id, lead)
        await self.bot.set_markup(chat, card_msg, self.markup(lead_id, lead, chat))
        return "Фото нижче 👇"

    async def hide_album(self, lead_id: int, lead: dict, chat: str, card_msg) -> str:
        ids = (lead.get("album") or {}).pop(chat, None)
        db.update_lead(lead_id, lead)
        if ids:
            await self.bot.delete(chat, ids)
        if card_msg:
            await self.bot.set_markup(chat, card_msg, self.markup(lead_id, lead, chat))
        return "Згорнув"

    def can_autosend(self) -> bool:
        h, d = db.sent_counts()
        return h < AUTO_SEND_PER_HOUR and d < AUTO_SEND_PER_DAY

    # ---------- надсилання в Telegram з урахуванням «перший раз / повторно» ----------

    @staticmethod
    def post_has_price(body: str, usd) -> bool:
        """Чи є в тексті поста ціна варіанта: у доларах (±10%) або в ларі за грубим курсом (±15%)."""
        if not usd:
            return True
        nums = [int(n.replace(",", "").replace(" ", "")) for n in re.findall(r"\d[\d, ]{1,6}\d", body)]
        return any(abs(n - usd) <= usd * 0.1 or abs(n - usd * GEL_PER_USD) <= usd * GEL_PER_USD * 0.15
                   for n in nums)

    async def send_tg(self, lead: dict) -> tuple[bool, str]:
        t = lead["tg"]
        try:
            ent = await self.tg.get_entity(t["username"] if t.get("username") else t["user_id"])
        except (ValueError, errors.RPCError) as ex:
            return False, f"не знайшов отримувача ({type(ex).__name__})"
        if not isinstance(ent, User) or ent.bot or ent.id == self.me_id:
            return False, "отримувач — не людина"
        key = f"tg:{ent.id}"
        # Віримо самому листуванню, а не пам'яті: якщо ви видалили повідомлення й чат порожній — пишемо як уперше
        last_mine = None
        async for m in self.tg.iter_messages(ent, limit=1, from_user="me"):
            last_mine = m.date.timestamp()
        db.contact_sync(key, last_mine)
        # Спершу пересилаємо саме оголошення (з фото), щоб було ясно, про яку квартиру мова
        forwarded = False
        src = lead.get("src")
        if src:
            try:
                # Спершу читаємо сам пост: це точно ця квартира і цій людині його ще не надсилали?
                # (одного разу менеджерові двічі пішов той самий пост замість двох різних квартир)
                posts = [m for m in await self.tg.get_messages(src["chat"], ids=src["ids"]) if m]
                body = "\n".join(m.message or "" for m in posts)
                if not posts:
                    log.warning("Пост для пересилання не знайдено — даю посилання")
                elif body.strip() and db.seen(f"fw:{ent.id}:{db.text_key(body)}"):
                    log.warning("Цей пост цій людині вже пересилали — даю посилання")
                elif body.strip() and not self.post_has_price(body, lead["ev"].get("price_usd")):
                    log.warning("У пості немає ціни варіанта — схоже, не той пост; даю посилання")
                else:
                    await self.tg.forward_messages(ent, src["ids"], from_peer=src["chat"])
                    forwarded = True
            except (errors.RPCError, ValueError) as ex:  # у чаті заборонено пересилання — тоді дамо посилання
                log.info("Пересилання не вдалося (%s), даю посилання", type(ex).__name__)
        text, kind = compose.message(key, lead["ev"], lead.get("public_link") or lead.get("card_link"), forwarded)
        try:
            await self.tg.send_message(ent, text, link_preview=not forwarded)
        except errors.FloodWaitError as ex:
            return False, f"Telegram просить зачекати {ex.seconds} с"
        except errors.RPCError as ex:
            return False, f"Telegram не дав надіслати ({type(ex).__name__})"
        db.contact_written(key)
        db.mark_sent()
        log.info("Надіслано в Telegram (%s)", kind)
        return True, text

    # ---------- кнопки в картці ----------

    async def on_button(self, data: str, chat: str, who: str, msg_id: int | None) -> str:
        """Натискання кнопки в картці. Дії змінюють кнопки в усіх учасників, гортання фото — лише у вас."""
        parts = data.split(":")
        if len(parts) < 2 or not parts[1].isdigit():
            return "Готово"
        action, lead_id = parts[0], int(parts[1])
        if action == "learn":
            await self.after_feedback(force=True)
            await self.send_learned(chat)
            return "Готово"
        lead, status = db.get_lead(lead_id)
        if not lead:
            return "Варіант не знайдено"
        self.fill_old(lead_id, lead, status)
        msgs = lead.get("msgs") or ({chat: msg_id} if msg_id else {})  # старі картки — лише ця
        my_msg = msgs.get(chat, msg_id)
        if action in ("ph", "pg"):  # pg — стрілки з карток минулої версії
            return await self.show_album(lead_id, lead, chat, my_msg)
        if action == "hide":
            return await self.hide_album(lead_id, lead, chat, msgs.get(chat))
        short = botmod.e(botmod._cut(lead["ev"].get("short") or "квартира", 60))
        others = [c for c in self.bot.members if c != chat]
        if action == "no":
            if status == "rejected":
                return "Уже позначено"
            db.set_lead_status(lead_id, "rejected")
            lead["actions"] = []
            db.update_lead(lead_id, lead)
            db.add_feedback(lead_id, False, judge.summary(lead["ev"]))
            judge.refresh_taste()
            why = [[{"text": t, "callback_data": f"why:{lead_id}:{code}"} for t, code in row] for row in WHY_ROWS]
            for c, mid in msgs.items():
                if c not in self.bot.members:
                    continue
                done = None
                if lead.get("rich"):
                    fids = (lead.get("fids") or [])[:10]  # усі фото — карусель лишається
                    done = await self.bot.edit_rich(c, mid, botmod.rejected_card(lead["ev"], who, self.links(lead), len(fids)),
                                                    fids, (why if c == chat else []) + self.link_row(lead))
                if not done:  # стара картка — хоча б кнопки
                    await self.bot.set_markup(c, mid, [[{"text": f"❌ Не підходить ({who[:20]})", "callback_data": "noop:0"}]]
                                              + (why if c == chat else []) + self.link_row(lead))
            return "Позначено: не підходить. Підкажіть чому — так я навчуся"
        if action == "why" and len(parts) == 3 and parts[2] in WHY:
            rows = self.link_row(lead)
            if not lead.get("rich"):
                rows = [[{"text": f"❌ Не підходить: {WHY[parts[2]]}", "callback_data": "noop:0"}]] + rows
            await self.bot.set_markup(chat, my_msg, rows)
            if parts[2] == "other":  # своїми словами: чекаємо відповідь текстом
                db.meta_set(f"await_reason:{chat}", str(lead_id))
                short = botmod.e(botmod._cut(lead["ev"].get("short") or "квартира", 60))
                await self.bot.send(
                    f"✍️ <b>Що не так із варіантом?</b>\n{short}\n\nНапишіть у відповідь, своїми словами — я запам'ятаю й урахую.",
                    {"force_reply": True, "input_field_placeholder": "Наприклад: перший поверх, вікна у двір"}, chat)
                return "Напишіть причину нижче 👇"
            db.set_feedback_reason(lead_id, WHY[parts[2]])
            judge.refresh_taste()
            await self.after_feedback()
            return f"Запам'ятав: {WHY[parts[2]]} 👍"
        if status in ("skipped", "rejected"):
            return "Цей варіант приховано"
        if action == "skip":
            db.set_lead_status(lead_id, "skipped")
            lead["actions"] = [[{"text": f"🙈 Приховав(-ла) {who[:20]}", "callback_data": "noop:0"}]]
            db.update_lead(lead_id, lead)
            await self.refresh(lead_id, lead, msgs)
            return "Приховано в усіх"
        if action == "tg":
            if status == "sent":
                return "Щодо цієї квартири вже написали"
            ok, info = await self.send_tg(lead)
            if not ok:
                return f"Не вийшло: {info}"
            db.set_lead_status(lead_id, "sent")
            db.add_feedback(lead_id, True, judge.summary(lead["ev"]))
            judge.refresh_taste()
            lead["actions"] = [[{"text": f"✅ Написав(-ла) {who[:20]} у Telegram", "callback_data": "noop:0"}]]
            db.update_lead(lead_id, lead)
            await self.refresh(lead_id, lead, msgs)
            await self.bot.send(f"✉️ <b>Надіслано в Telegram</b> ({botmod.e(who)}) — {short}\n"
                                f"<blockquote expandable>{botmod.e(info[:900])}</blockquote>")
            return "Надіслано ✅"
        if action == "wa":
            phone = lead["phones"][0]
            key = f"wa:{phone}"
            text, _ = compose.message(key, lead["ev"], lead.get("public_link"))
            db.add_feedback(lead_id, True, judge.summary(lead["ev"]))
            judge.refresh_taste()
            if status != "sent":
                db.contact_written(key)  # WhatsApp не скаже, чи ви надіслали — рахуємо з цього моменту
                db.set_lead_status(lead_id, "sent")
            lead["actions"] = [[{"text": f"📲 {who[:20]} пише у WhatsApp", "callback_data": "noop:0"}]]
            db.update_lead(lead_id, lead)
            await self.refresh(lead_id, lead, {c: m for c, m in msgs.items() if c != chat})
            await self.refresh(lead_id, lead, {chat: my_msg},
                               [[{"text": "📲 Відкрити WhatsApp ↗", "url": botmod.wa_link(phone, text)}]])
            if others:
                await self.bot.send(f"📲 {botmod.e(who)} пише у WhatsApp щодо: {short}", to=others)
            return "Натисніть «Відкрити WhatsApp»"
        return "Готово"

    # ---------- меню ----------

    async def on_text(self, txt: str, chat: str, who: str):
        t = txt.lower()
        B = botmod
        menu = {B.MENU_STATUS, B.MENU_WEEK, B.MENU_PAUSE, B.MENU_RESUME, B.MENU_HELP, B.MENU_INVITE,
                B.MENU_MEMBERS, B.MENU_LEARN}
        pending = db.meta_get(f"await_reason:{chat}")
        if pending and txt not in menu and not txt.startswith("/"):
            db.meta_set(f"await_reason:{chat}", "")
            reason = " ".join(txt.split())[:300]
            db.set_feedback_reason(int(pending), f"своїми словами: {reason}")
            judge.refresh_taste()
            await self.bot.send(f"🧠 Запам'ятав: «{B.e(reason)}». Враховуватиму в наступних варіантах.", to=chat)
            await self.after_feedback()
            return
        if txt == B.MENU_LEARN or t.startswith("/learn"):
            await self.send_learned(chat)
        elif txt == B.MENU_STATUS or t.startswith("/status"):
            await self.send_status(chat)
        elif txt == B.MENU_WEEK or t.startswith("/week"):
            if self.backfill_running:
                await self.bot.send("⏳ Перебір тижня вже йде — надішлю добірку, коли закінчу.", to=chat)
            else:
                asyncio.create_task(self._safe(self.backfill))
        elif txt == B.MENU_PAUSE or t.startswith("/pause"):
            db.meta_set("paused", "1")
            await self.bot.send(f"⏸ <b>Пауза</b> ({B.e(who)}). Нові оголошення не дивлюся й нікому не пишу.",
                                B.menu_keyboard(True))
        elif txt == B.MENU_RESUME or t.startswith("/resume"):
            db.meta_set("paused", "0")
            await self.bot.send(f"▶️ <b>Продовжую</b> ({B.e(who)}) стежити за чатами й сайтами.", B.menu_keyboard(False))
        elif txt == B.MENU_INVITE or t.startswith("/invite"):
            link = self.bot.new_invite()
            await self.bot.send(
                "<b>👥 Запрошення</b>\n\nПерешліть це посилання людині. Вона відкриє його в Telegram і натисне «Старт».\n\n"
                f"{B.e(link)}\n\n<i>Посилання одноразове й працює 24 години. Нове посилання скасовує старе.</i>", to=chat)
        elif txt == B.MENU_MEMBERS or t.startswith("/members"):
            await self.bot.members_message(chat)
        else:
            await self.bot.send(HELP, B.menu_keyboard(self.paused), chat)

    async def after_feedback(self, force: bool = False):
        """Кожні 3 нові відгуки з причиною — Claude заново робить висновки про ваш смак."""
        n = db.conn().execute("SELECT COUNT(*) FROM feedback WHERE reason != '' OR liked = 1").fetchone()[0]
        if not force and n - int(db.meta_get("learned_at") or 0) < 3:
            return
        items = db.all_feedback(80)  # читаємо базу тут, в основному потоці
        rules = await asyncio.to_thread(judge.learn_rules, items)
        if rules:
            db.meta_set("taste_rules", rules)
            db.meta_set("learned_at", str(n))
            judge.refresh_taste()
            log.info("Оновив висновки про смак (%d відгуків)", n)

    async def send_learned(self, chat):
        B = botmod
        rules = db.meta_get("taste_rules") or ""
        rows = db.all_feedback(12)
        n = db.conn().execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
        parts = [f"<b>🧠 Чого я навчився</b> (відгуків: {n})", ""]
        parts.append("<b>Висновки про ваш смак:</b>\n" + B.e(rules) if rules else
                     "<i>Висновків поки немає — потрібно хоча б 3 відгуки з причиною. Натискайте «👎 Не підходить» "
                     "і обирайте причину.</i>")
        if rows:
            parts += ["", "<b>Останні відгуки:</b>"]
            parts += [("👍 " if liked else "👎 ") + B.e(B._cut(s, 70)) + (f" — <i>{B.e(B._cut(r, 80))}</i>" if r else "")
                      for liked, s, r in rows]
        parts += ["", "<i>Усе це я враховую під час кожної оцінки й перед тим, як написати сам.</i>"]
        await self.bot.send("\n".join(parts), {"inline_keyboard": [[{"text": "🔄 Перезібрати висновки",
                                                                       "callback_data": "learn:0"}]]}, chat)

    async def send_status(self, chat):
        h, d = db.sent_counts()
        c = db.conn()
        found = c.execute("SELECT COUNT(*) FROM leads WHERE ts>?", (time.time() - 86400,)).fetchone()[0]

        def at(key):
            v = db.meta_get(key)
            return datetime.fromtimestamp(float(v)).strftime("%H:%M") if v else "ще не було"

        state = "⏸ На паузі" if self.paused else "🟢 Працюю"
        lines = [f"<b>{state}</b>", "",
                 f"🕐 Останнє оголошення розібрано: <b>{at('last_checked')}</b>",
                 f"🌐 ss.ge перевіряв: {at('ssge_last')} (раз на ~12 хв)"]
        lines += [f"🌐 {n}: {at('site_last:' + n)}" for n in sites.SITES]
        if self.backfill_running:
            lines.append(f"⏳ Перебираю тиждень: {botmod.e(self.backfill_where)}, "
                         f"перевірено {self.stats['checked']}, відповідних {self.stats['found']}")
        lines += ["", "<b>За добу</b>",
                  f"• відповідних варіантів: <b>{found}</b>",
                  f"• написав сам: <b>{d}</b> із {AUTO_SEND_PER_DAY} (за годину {h} із {AUTO_SEND_PER_HOUR})"]
        await self.bot.send("\n".join(lines), botmod.menu_keyboard(self.paused), chat)

    # ---------- Telegram-чати ----------

    @staticmethod
    def is_rent_chat(chat) -> bool:
        title = (getattr(chat, "title", "") or "").lower()
        return any(w in title for w in RENT_CHAT_WORDS)

    @staticmethod
    def post_urls(msgs) -> list[str]:
        """Усі посилання поста: у тексті, сховані під словами і в кнопках під постом."""
        urls = []
        for m in msgs:
            for ent, txt in m.get_entities_text() or []:
                if isinstance(ent, MessageEntityTextUrl):
                    urls.append(ent.url)
                elif isinstance(ent, MessageEntityUrl):
                    urls.append(txt if txt.startswith("http") else "https://" + txt)
            for row in getattr(getattr(m, "reply_markup", None), "rows", None) or []:
                for btn in row.buttons:
                    if getattr(btn, "url", None):
                        urls.append(btn.url)
        return list(dict.fromkeys(urls))

    async def open_site(self, url: str) -> dict:
        """Сторінка оголошення: ss.ge — через його дані (точніше), решта — як звичайний сайт."""
        if pages.host_of(url).endswith("ss.ge"):
            m = re.search(r"(\d{6,})", url)
            d = await asyncio.to_thread(ssge._get_json, url) if m else None
            a = (d or {}).get("applicationData")
            if not a:
                return {"status": "gone" if d else "error", "text": "", "phones": [], "tg": [], "lat": 0, "lng": 0}
            phones = ["995" + re.sub(r"\D", "", p.get("phoneNumber") or "")[-9:] for p in a.get("applicationPhones") or []]
            return {"status": "gone" if a.get("isInactiveApplication") else "ok",
                    "text": ssge._detail_text(a, {}), "phones": phones, "tg": [],
                    "lat": a.get("locationLatitude") or 0, "lng": a.get("locationLongitude") or 0}
        return await asyncio.to_thread(pages.fetch, url)

    async def on_post(self, msgs, to_backlog=False):
        if self.paused:
            return
        m0 = msgs[0]
        if m0.out or not (m0.is_group or m0.is_channel):
            return
        chat = await m0.get_chat()
        if not self.is_rent_chat(chat):
            return  # читаємо лише чати про оренду
        text = "\n".join(m.message for m in msgs if m.message).strip()
        rich_text, rich_urls, rich_photos = rich_post(m0)
        if rich_text:
            text = (text + "\n" + rich_text).strip()
        if len(text) < 30 or not RENT_WORDS.search(text) or DEMAND_WORDS.search(text):
            return
        if too_expensive(text):
            return
        if db.seen(f"tgm:{chat.id}:{m0.id}", db.text_key(text)):
            return
        sender = await m0.get_sender()
        if isinstance(sender, User) and sender.bot:
            return

        photos, photo_ids = [], [m.id for m in msgs if m.photo]
        for item in [m for m in msgs if m.photo] + rich_photos:
            if len(photos) >= 10:
                break
            try:
                photos.append(await self.tg.download_media(item, file=bytes))
            except Exception:
                pass
        title = getattr(chat, "title", "чат")

        # Посилання з поста на сайт оголошення: беремо звідти подробиці, контакти й місце на мапі
        links = pages.classify(self.post_urls(msgs) + rich_urls)
        page = {"status": None, "text": "", "phones": [], "tg": [], "lat": 0, "lng": 0}
        note = ""
        if links["site"]:
            page = await self.open_site(links["site"])
            if page["status"] == "gone":
                log.info("Знято із сайту (Telegram: %s): %s", title, links["site"])
                return
            if page["status"] == "blocked":
                note = f"Сайт {pages.host_of(links['site'])} не пускає роботів — оцінив лише за постом."
            elif page["status"] == "error":
                note = "Сайт оголошення не відкрився — оцінив лише за постом."
        ev = await asyncio.to_thread(judge.evaluate, text, f"Telegram: {title}", photos[:MAX_PHOTOS], (),
                                     page["text"])
        if not ev:
            return

        uname = getattr(chat, "username", None)
        public = f"https://t.me/{uname}/{m0.id}" if uname else None
        card_link = public or f"https://t.me/c/{chat.id}/{m0.id}"
        usernames = [u.lstrip("@") for u in ev.get("tg_usernames", [])] + links["tg"] + page["tg"]
        usernames = [u for u in dict.fromkeys(usernames) if u.lower() != (uname or "").lower()]
        tg_target = None
        if usernames:
            tg_target = {"username": usernames[0]}
        elif isinstance(sender, User) and not sender.bot:
            tg_target = {"user_id": sender.id}
        raw_phones = (ev.get("phones", []) + links["wa"] + page["phones"]
                      + ["".join(g) for g in PHONE_RE.findall(text)])
        phones = list(dict.fromkeys(filter(None, map(norm_phone, raw_phones))))
        ref = {"chat": m0.chat_id, "ids": photo_ids} if photo_ids else None
        extra = {"text": text[:3000], "site": links["site"], "post": card_link,
                 "map": pages.map_link(ev, links["map"], page["lat"], page["lng"]), "note": note}
        await self.handle(ev, f"Telegram: {title}", card_link, public, tg_target, phones, photos, ref, to_backlog,
                          {"chat": m0.chat_id, "ids": [m.id for m in msgs]}, extra)

    # ---------- перебір оголошень за тиждень ----------

    async def backfill(self):
        """Перебір оголошень за тиждень. Відповідні варіанти приходять одразу, у міру знаходження."""
        self.backfill_running = True
        self.stats = {"checked": 0, "found": 0}
        await self.bot.send(
            "🔎 <b>Перебираю оголошення за останні 7 днів</b>\n"
            "Спершу Telegram-чати, потім сайти. Відповідні варіанти надсилаю одразу, щойно знаходжу. "
            "Займе від пів години до кількох годин. Хід — у «📊 Статус».")
        since = datetime.now(timezone.utc) - timedelta(days=WEEK_DAYS)
        try:
            async for d in self.tg.iter_dialogs():
                if not (d.is_group or d.is_channel) or not self.is_rent_chat(d.entity):
                    continue
                self.backfill_where = f"чат «{d.name}»"
                groups: dict = {}
                async for m in self.tg.iter_messages(d.entity, limit=3000):
                    if m.date < since:
                        break
                    if m.grouped_id:
                        groups.setdefault(m.grouped_id, []).append(m)
                    else:
                        await self._safe(self.on_post, [m])
                for msgs in groups.values():
                    await self._safe(self.on_post, sorted(msgs, key=lambda x: x.id))
                await self.mark_read(d.entity)
                log.info("Тиждень: чат «%s» готовий, знайдено поки %d", d.name, self.stats["found"])
            self.backfill_where = "сайт ss.ge"
            await ssge.backfill(self.handle, WEEK_DAYS)
            self.backfill_where = "korter.ge, livo.ge, place.ge, rentor.ge"
            await sites.backfill(self.handle)
        finally:
            self.backfill_running = False
            db.meta_set("backfill_done", str(time.time()))
        await self.bot.send(
            f"✅ <b>Перебір тижня закінчено</b>\n\n"
            f"Перевірено оголошень: {self.stats['checked']}\n"
            f"Відповідних: <b>{self.stats['found']}</b> — усі вже надіслано вище.",
            botmod.menu_keyboard(self.paused))

    # ---------- запуск ----------

    async def main(self):
        api_id = int(secrets.need("tg_api_id"))
        self.tg = TelegramClient(StringSession(secrets.need("tg_session")), api_id,
                                 secrets.need("tg_api_hash"), device_model="Tbilisi agent")
        await self.tg.connect()
        if not await self.tg.is_user_authorized():
            raise SystemExit("Вхід у Telegram втрачено. Запустіть налаштування заново.")
        self.me_id = (await self.tg.get_me()).id
        await self.tg.get_dialogs()  # підвантажує список чатів у пам'ять

        async def rent_chat(event) -> bool:
            return (event.is_group or event.is_channel) and self.is_rent_chat(await event.get_chat())

        @self.tg.on(events.NewMessage(incoming=True))
        async def _new(event):
            if event.message.grouped_id or not await rent_chat(event):
                return  # альбом прийде цілим у події Album; чужі чати не чіпаємо
            await self._safe(self.on_post, [event.message])
            await self.mark_read(event.chat_id, event.message.id)

        @self.tg.on(events.Album())
        async def _album(event):
            if not await rent_chat(event):
                return
            await self._safe(self.on_post, list(event.messages))
            await self.mark_read(event.chat_id, max(m.id for m in event.messages))

        judge.refresh_taste()  # досвід із ваших відгуків
        await self.bot.setup_menu()
        await self.bot.send("🟢 <b>Агента запущено</b>\nСтежу за чатами й сайтами. Меню — внизу 👇",
                            botmod.menu_keyboard(self.paused))
        log.info("Запущено")
        tasks = [self.tg.run_until_disconnected(), self.bot.poll(self.on_button, self.on_text),
                 ssge.loop(self.handle, lambda: self.paused),
                 sites.loop(self.handle, lambda: self.paused)]
        if not db.meta_get("backfill_done"):
            tasks.append(self._safe(self.backfill))
        else:
            tasks.append(self._safe(self.mark_all_read))
        await asyncio.gather(*tasks)

    async def mark_read(self, chat, max_id=None):
        """Позначити повідомлення прочитаними: лічильник у Telegram показує, що агент їх розібрав."""
        try:
            await self.tg.send_read_acknowledge(chat, max_id=max_id)
        except Exception as ex:
            log.debug("Не позначив прочитаним: %s", type(ex).__name__)

    async def mark_all_read(self):
        """Під час запуску: усе, що агент уже розібрав (перебір тижня), позначаємо прочитаним."""
        async for d in self.tg.iter_dialogs():
            if (d.is_group or d.is_channel) and d.unread_count and self.is_rent_chat(d.entity):
                await self.mark_read(d.entity)
                await asyncio.sleep(1)

    async def _safe(self, fn, *a):
        try:
            await fn(*a)
        except Exception:
            log.exception("Помилка")


def setup_logging():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(DATA_DIR / "agent.log", maxBytes=2_000_000,
                                             backupCount=3, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[h, logging.StreamHandler()],
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    for noisy in ("telethon", "httpx", "httpx2", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def run():
    setup_logging()
    asyncio.run(Agent().main())
