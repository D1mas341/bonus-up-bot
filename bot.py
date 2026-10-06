import os
import sqlite3
import asyncio
from contextlib import closing

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from dotenv import load_dotenv
from aiohttp import web

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
DB_PATH = os.getenv("DB_PATH", "bonus_up.db")
WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

dp = Dispatcher()
admin_state = {}


def db():
    return sqlite3.connect(DB_PATH)


def init_db():
    with closing(db()) as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            bonus TEXT DEFAULT '',
            url TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            position INTEGER DEFAULT 0,
            geo TEXT DEFAULT ''
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            source TEXT DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS clicks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            offer_id INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)
        # Migration for an existing database created by the first version.
        cols = [r[1] for r in con.execute("PRAGMA table_info(offers)").fetchall()]
        if "geo" not in cols:
            con.execute("ALTER TABLE offers ADD COLUMN geo TEXT DEFAULT ''")
        con.commit()


def seed_demo():
    with closing(db()) as con:
        if con.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 0:
            demo = [
                ("bets", "1xBet", "Партнёрский оффер — ссылка добавляется позже.",
                 "", "https://example.com/1xbet", 1, 1, "CIS"),
                ("bets", "MelBet", "Партнёрский оффер — ссылка добавляется позже.",
                 "", "https://example.com/melbet", 1, 2, "CIS"),
                ("bets", "Mellstroy", "Партнёрский оффер — ссылка добавляется позже.",
                 "", "https://example.com/mellstroy", 1, 3, "CIS"),
                ("casino", "PIN-UP Casino", "Партнёрский оффер — ссылка добавляется позже.",
                 "", "https://example.com/pinup", 1, 1, "CIS"),
                ("casino", "Crypto Casino", "Тестовая карточка. Заменить после проверки партнёрки.",
                 "", "https://example.com/crypto", 1, 2, "GLOBAL"),
            ]
            con.executemany("""
                INSERT INTO offers
                (category,name,description,bonus,url,enabled,position,geo)
                VALUES(?,?,?,?,?,?,?,?)
            """, demo)
            con.commit()


def add_user(message: Message, source=""):
    u = message.from_user
    with closing(db()) as con:
        con.execute("""
        INSERT INTO users(user_id,username,first_name,source)
        VALUES(?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name
        """, (u.id, u.username or "", u.first_name or "", source))
        con.commit()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def main_keyboard():
    kb = ReplyKeyboardBuilder()
    if WEBAPP_URL:
        kb.button(text="🎁 ОТКРЫТЬ BONUS UP", web_app=WebAppInfo(url=WEBAPP_URL))
        kb.adjust(1)
    else:
        kb.button(text="⚽ БУКМЕКЕРЫ")
        kb.button(text="🎰 КАЗИНО")
        kb.adjust(2)
    return kb.as_markup(resize_keyboard=True)


def offers_keyboard(category):
    kb = InlineKeyboardBuilder()
    with closing(db()) as con:
        rows = con.execute("""
        SELECT id,name FROM offers
        WHERE category=? AND enabled=1
        ORDER BY position,id
        """, (category,)).fetchall()
    for oid, name in rows:
        kb.button(text=f"🎁 {name}", callback_data=f"offer:{oid}")
    kb.adjust(1)
    kb.button(text="⬅️ Главное меню", callback_data="home")
    return kb.as_markup()


def admin_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить оффер", callback_data="adm:add")
    kb.button(text="📋 Список офферов", callback_data="adm:list")
    kb.button(text="✏️ Изменить оффер", callback_data="adm:edit")
    kb.button(text="🔗 Изменить ссылку", callback_data="adm:link")
    kb.button(text="🟢/🔴 Вкл. / выкл.", callback_data="adm:toggle")
    kb.button(text="📊 Клики", callback_data="adm:clicks")
    kb.adjust(1)
    return kb.as_markup()


def admin_offer_keyboard(prefix, include_cancel=True):
    kb = InlineKeyboardBuilder()
    with closing(db()) as con:
        rows = con.execute(
            "SELECT id,name,enabled FROM offers ORDER BY category,position,id"
        ).fetchall()
    for oid, name, enabled in rows:
        mark = "🟢" if enabled else "🔴"
        kb.button(text=f"{mark} {name}", callback_data=f"{prefix}:{oid}")
    kb.adjust(1)
    if include_cancel:
        kb.button(text="⬅️ Админ-панель", callback_data="adm:home")
    return kb.as_markup()


async def send_admin(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return
    await message.answer(
        "🛠 АДМИН-ПАНЕЛЬ\n\n"
        "Здесь можно управлять офферами без GitHub и без изменения кода.",
        reply_markup=admin_keyboard()
    )


@dp.message(CommandStart())
async def start(message: Message):
    payload = ""
    if message.text and " " in message.text:
        payload = message.text.split(" ", 1)[1][:100]
    add_user(message, payload)
    await message.answer(
        "🎁 BONUS UP\n\n"
        "Актуальные предложения БК и онлайн-казино в одном месте.\n\n"
        "Выберите раздел:",
        reply_markup=main_keyboard()
    )


@dp.message(Command("admin"))
async def admin(message: Message):
    await send_admin(message)


@dp.message(Command("bonus"))
async def bonus(message: Message):
    await message.answer(
        "🎁 BONUS UP\n\nОткройте витрину с актуальными предложениями:",
        reply_markup=main_keyboard()
    )


@dp.message(F.text == "⚽ БУКМЕКЕРЫ")
async def bets(message: Message):
    await message.answer(
        "⚽ БУКМЕКЕРЫ\n\nВыберите предложение:",
        reply_markup=offers_keyboard("bets")
    )


@dp.message(F.text == "🎰 КАЗИНО")
async def casino(message: Message):
    await message.answer(
        "🎰 ОНЛАЙН-КАЗИНО\n\nВыберите предложение:",
        reply_markup=offers_keyboard("casino")
    )


@dp.callback_query(F.data == "home")
async def home(call: CallbackQuery):
    await call.message.edit_text(
        "🎁 BONUS UP\n\nАктуальные предложения БК и онлайн-казино в одном месте."
    )
    await call.message.answer("Выберите раздел:", reply_markup=main_keyboard())
    await call.answer()


@dp.callback_query(F.data.startswith("offer:"))
async def offer(call: CallbackQuery):
    offer_id = int(call.data.split(":")[1])
    with closing(db()) as con:
        row = con.execute("""
        SELECT category,name,description,bonus,url,geo
        FROM offers
        WHERE id=? AND enabled=1
        """, (offer_id,)).fetchone()
        if row:
            # This records an offer-card opening. Telegram does not tell the bot
            # when the external URL button itself is clicked.
            con.execute(
                "INSERT INTO clicks(user_id,offer_id) VALUES(?,?)",
                (call.from_user.id, offer_id)
            )
            con.commit()
    if not row:
        await call.answer("Предложение больше недоступно.", show_alert=True)
        return

    category, name, desc, bonus, url, geo = row
    text = f"🎁 {name}\n\n"
    if bonus:
        text += f"💰 {bonus}\n"
    if geo:
        text += f"🌍 GEO: {geo}\n"
    if desc:
        text += f"{desc}\n"
    text += "\n18+. Условия и доступность зависят от страны и правил оператора."

    kb = InlineKeyboardBuilder()
    kb.button(text="🎁 ПОЛУЧИТЬ ПРЕДЛОЖЕНИЕ", url=url)
    kb.button(text="⬅️ Назад", callback_data=f"back:{category}")
    kb.adjust(1)
    await call.message.edit_text(text, reply_markup=kb.as_markup())
    await call.answer()


@dp.callback_query(F.data.startswith("back:"))
async def back(call: CallbackQuery):
    category = call.data.split(":", 1)[1]
    title = "⚽ БУКМЕКЕРЫ" if category == "bets" else "🎰 ОНЛАЙН-КАЗИНО"
    await call.message.edit_text(
        title + "\n\nВыберите предложение:",
        reply_markup=offers_keyboard(category)
    )
    await call.answer()


# -------------------- ADMIN --------------------

@dp.callback_query(F.data == "adm:home")
async def adm_home(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    admin_state.pop(call.from_user.id, None)
    await call.message.edit_text("🛠 АДМИН-ПАНЕЛЬ", reply_markup=admin_keyboard())
    await call.answer()


@dp.callback_query(F.data == "adm:add")
async def adm_add(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    admin_state[call.from_user.id] = {"action": "add", "step": "category", "data": {}}
    await call.message.edit_text(
        "➕ ДОБАВЛЕНИЕ ОФФЕРА\n\n"
        "Шаг 1/8. Напиши категорию:\n"
        "bets — букмекер\n"
        "casino — казино"
    )
    await call.answer()


@dp.callback_query(F.data == "adm:list")
async def adm_list(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    with closing(db()) as con:
        rows = con.execute("""
        SELECT id,category,name,enabled,position,geo
        FROM offers ORDER BY category,position,id
        """).fetchall()
    if not rows:
        text = "📋 Офферов пока нет."
    else:
        text = "📋 ОФФЕРЫ\n\n"
        for oid, cat, name, enabled, pos, geo in rows:
            mark = "🟢" if enabled else "🔴"
            text += f"{mark} #{oid} {name} | {cat} | pos {pos}"
            if geo:
                text += f" | {geo}"
            text += "\n"
    await call.message.edit_text(text, reply_markup=admin_offer_keyboard("adm:view"))
    await call.answer()


@dp.callback_query(F.data.startswith("adm:view:"))
async def adm_view(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    oid = int(call.data.split(":")[2])
    with closing(db()) as con:
        row = con.execute("""
        SELECT id,category,name,description,bonus,url,enabled,position,geo
        FROM offers WHERE id=?
        """, (oid,)).fetchone()
        clicks = con.execute(
            "SELECT COUNT(*) FROM clicks WHERE offer_id=?", (oid,)
        ).fetchone()[0]
    if not row:
        await call.answer("Оффер не найден", show_alert=True)
        return
    _, cat, name, desc, bonus, url, enabled, pos, geo = row
    text = (
        f"🎁 {name}\n\n"
        f"Категория: {cat}\n"
        f"Описание: {desc or '—'}\n"
        f"Бонус: {bonus or '—'}\n"
        f"GEO: {geo or '—'}\n"
        f"Позиция: {pos}\n"
        f"Статус: {'🟢 включён' if enabled else '🔴 выключен'}\n"
        f"Открытий карточки: {clicks}\n\n"
        f"Ссылка:\n{url}"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Изменить", callback_data=f"adm:editone:{oid}")
    kb.button(text="🔗 Ссылка", callback_data=f"adm:linkone:{oid}")
    kb.button(text="🟢/🔴 Вкл./выкл.", callback_data=f"adm:toggleone:{oid}")
    kb.button(text="📊 Клики", callback_data=f"adm:clickone:{oid}")
    kb.button(text="⬅️ К списку", callback_data="adm:list")
    kb.adjust(1)
    await call.message.edit_text(text, reply_markup=kb.as_markup())
    await call.answer()


@dp.callback_query(F.data == "adm:edit")
async def adm_edit(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    await call.message.edit_text(
        "✏️ Выбери оффер для изменения:",
        reply_markup=admin_offer_keyboard("adm:editone")
    )
    await call.answer()


@dp.callback_query(F.data.startswith("adm:editone:"))
async def adm_editone(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    oid = int(call.data.split(":")[2])
    admin_state[call.from_user.id] = {
        "action": "edit", "step": "name", "offer_id": oid, "data": {}
    }
    await call.message.edit_text(
        "✏️ ИЗМЕНЕНИЕ ОФФЕРА\n\n"
        "Напиши новое название.\n"
        "Если название менять не нужно — напиши: -"
    )
    await call.answer()


@dp.callback_query(F.data == "adm:link")
async def adm_link(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    await call.message.edit_text(
        "🔗 Выбери оффер:",
        reply_markup=admin_offer_keyboard("adm:linkone")
    )
    await call.answer()


@dp.callback_query(F.data.startswith("adm:linkone:"))
async def adm_linkone(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    oid = int(call.data.split(":")[2])
    admin_state[call.from_user.id] = {
        "action": "link", "step": "url", "offer_id": oid
    }
    await call.message.edit_text(
        "🔗 ПРИВЯЗКА ССЫЛКИ\n\n"
        "Отправь новую партнёрскую ссылку одним сообщением."
    )
    await call.answer()


@dp.callback_query(F.data == "adm:toggle")
async def adm_toggle(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    await call.message.edit_text(
        "🟢/🔴 Выбери оффер:",
        reply_markup=admin_offer_keyboard("adm:toggleone")
    )
    await call.answer()


@dp.callback_query(F.data.startswith("adm:toggleone:"))
async def adm_toggleone(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    oid = int(call.data.split(":")[2])
    with closing(db()) as con:
        con.execute(
            "UPDATE offers SET enabled=CASE enabled WHEN 1 THEN 0 ELSE 1 END WHERE id=?",
            (oid,)
        )
        row = con.execute(
            "SELECT name,enabled FROM offers WHERE id=?", (oid,)
        ).fetchone()
        con.commit()
    if row:
        name, enabled = row
        await call.answer(f"{name}: {'включён' if enabled else 'выключен'}")
        await call.message.edit_text(
            f"{'🟢' if enabled else '🔴'} {name} — "
            f"{'включён' if enabled else 'выключен'}",
            reply_markup=admin_keyboard()
        )
    else:
        await call.answer("Оффер не найден", show_alert=True)


@dp.callback_query(F.data == "adm:clicks")
async def adm_clicks(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    with closing(db()) as con:
        rows = con.execute("""
        SELECT o.name, COUNT(c.id)
        FROM offers o
        LEFT JOIN clicks c ON c.offer_id=o.id
        GROUP BY o.id
        ORDER BY COUNT(c.id) DESC, o.position, o.id
        """).fetchall()
    text = "📊 СТАТИСТИКА\n\n"
    text += "\n".join(f"• {name}: {count}" for name, count in rows) or "Пока нет данных."
    text += "\n\n⚠️ Считаются открытия карточки оффера. Telegram не передаёт боту факт нажатия внешней ссылки."
    await call.message.edit_text(text, reply_markup=admin_keyboard())
    await call.answer()


@dp.callback_query(F.data.startswith("adm:clickone:"))
async def adm_clickone(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    oid = int(call.data.split(":")[2])
    with closing(db()) as con:
        row = con.execute("""
        SELECT o.name, COUNT(c.id)
        FROM offers o LEFT JOIN clicks c ON c.offer_id=o.id
        WHERE o.id=? GROUP BY o.id
        """, (oid,)).fetchone()
    if row:
        await call.answer(f"{row[0]}: {row[1]} открытий", show_alert=True)
    else:
        await call.answer("Оффер не найден", show_alert=True)


@dp.message()
async def admin_text(message: Message):
    uid = message.from_user.id
    if not is_admin(uid) or uid not in admin_state:
        return

    state = admin_state[uid]
    action = state["action"]
    step = state["step"]
    text = (message.text or "").strip()

    if text.lower() in {"отмена", "/cancel"}:
        admin_state.pop(uid, None)
        await message.answer("❌ Отменено.", reply_markup=admin_keyboard())
        return

    if action == "link":
        if step == "url":
            if not (text.startswith("http://") or text.startswith("https://")):
                await message.answer("Нужна ссылка, начинающаяся с http:// или https://")
                return
            with closing(db()) as con:
                con.execute(
                    "UPDATE offers SET url=? WHERE id=?",
                    (text, state["offer_id"])
                )
                con.commit()
            admin_state.pop(uid, None)
            await message.answer("✅ Партнёрская ссылка обновлена.", reply_markup=admin_keyboard())
            return

    if action == "add":
        if step == "category":
            if text not in {"bets", "casino"}:
                await message.answer("Напиши только: bets или casino")
                return
            state["data"]["category"] = text
            state["step"] = "name"
            await message.answer("Шаг 2/8. Название оффера:")
            return

        if step == "name":
            state["data"]["name"] = text
            state["step"] = "description"
            await message.answer("Шаг 3/8. Описание:")
            return

        if step == "description":
            state["data"]["description"] = text
            state["step"] = "bonus"
            await message.answer("Шаг 4/8. Бонус. Если нет — напиши -")
            return

        if step == "bonus":
            state["data"]["bonus"] = "" if text == "-" else text
            state["step"] = "url"
            await message.answer("Шаг 5/8. Партнёрская ссылка:")
            return

        if step == "url":
            if not (text.startswith("http://") or text.startswith("https://")):
                await message.answer("Нужна ссылка, начинающаяся с http:// или https://")
                return
            state["data"]["url"] = text
            state["step"] = "geo"
            await message.answer("Шаг 6/8. GEO. Например: CIS, RU, KZ, GLOBAL. Если не нужно — -")
            return

        if step == "geo":
            state["data"]["geo"] = "" if text == "-" else text
            state["step"] = "position"
            await message.answer("Шаг 7/8. Позиция (число). Например: 1")
            return

        if step == "position":
            try:
                state["data"]["position"] = int(text)
            except ValueError:
                await message.answer("Позиция должна быть числом, например 1")
                return
            state["step"] = "confirm"
            d = state["data"]
            await message.answer(
                "Шаг 8/8. Проверь:\n\n"
                f"Название: {d['name']}\n"
                f"Категория: {d['category']}\n"
                f"Описание: {d['description']}\n"
                f"Бонус: {d['bonus'] or '—'}\n"
                f"GEO: {d['geo'] or '—'}\n"
                f"Позиция: {d['position']}\n"
                f"Ссылка: {d['url']}\n\n"
                "Напиши ДА для сохранения или НЕТ для отмены."
            )
            return

        if step == "confirm":
            if text.lower() not in {"да", "yes", "y"}:
                admin_state.pop(uid, None)
                await message.answer("❌ Добавление отменено.", reply_markup=admin_keyboard())
                return
            d = state["data"]
            with closing(db()) as con:
                con.execute("""
                INSERT INTO offers
                (category,name,description,bonus,url,enabled,position,geo)
                VALUES(?,?,?,?,?,?,?,?)
                """, (
                    d["category"], d["name"], d["description"], d["bonus"],
                    d["url"], 1, d["position"], d["geo"]
                ))
                con.commit()
            admin_state.pop(uid, None)
            await message.answer("✅ Оффер добавлен и включён.", reply_markup=admin_keyboard())
            return

    if action == "edit":
        oid = state["offer_id"]

        if step == "name":
            state["data"]["name"] = "" if text == "-" else text
            state["step"] = "description"
            await message.answer("Новое описание. Или - чтобы оставить старое:")
            return

        if step == "description":
            state["data"]["description"] = "" if text == "-" else text
            state["step"] = "bonus"
            await message.answer("Новый бонус. Или - чтобы оставить старый:")
            return

        if step == "bonus":
            state["data"]["bonus"] = "" if text == "-" else text
            state["step"] = "geo"
            await message.answer("Новый GEO. Или - чтобы оставить старый:")
            return

        if step == "geo":
            state["data"]["geo"] = "" if text == "-" else text
            state["step"] = "position"
            await message.answer("Новая позиция. Или - чтобы оставить старую:")
            return

        if step == "position":
            if text == "-":
                state["data"]["position"] = None
            else:
                try:
                    state["data"]["position"] = int(text)
                except ValueError:
                    await message.answer("Позиция должна быть числом или -")
                    return

            with closing(db()) as con:
                old = con.execute("""
                SELECT name,description,bonus,geo,position
                FROM offers WHERE id=?
                """, (oid,)).fetchone()
                if not old:
                    admin_state.pop(uid, None)
                    await message.answer("Оффер не найден.", reply_markup=admin_keyboard())
                    return

                name = state["data"]["name"] or old[0]
                desc = state["data"]["description"] if state["data"]["description"] != "" else old[1]
                bonus = state["data"]["bonus"] if state["data"]["bonus"] != "" else old[2]
                geo = state["data"]["geo"] if state["data"]["geo"] != "" else old[3]
                pos = state["data"]["position"] if state["data"]["position"] is not None else old[4]

                con.execute("""
                UPDATE offers
                SET name=?,description=?,bonus=?,geo=?,position=?
                WHERE id=?
                """, (name, desc, bonus, geo, pos, oid))
                con.commit()

            admin_state.pop(uid, None)
            await message.answer("✅ Оффер изменён.", reply_markup=admin_keyboard())
            return


MINI_APP_HTML = '<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>BONUS UP</title><script src="https://telegram.org/js/telegram-web-app.js"></script><style>:root{--bg:#0b0d10;--card:#171a1f;--muted:#a8adb7;--text:#fff;--accent:#f4c84b}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Arial,sans-serif}.wrap{max-width:760px;margin:auto;padding:18px 14px 90px}.header{display:flex;align-items:center;justify-content:space-between;margin:4px 0 18px}.logo{font-size:26px;font-weight:800}.sub{color:var(--muted);font-size:13px;margin-top:3px}.tabs{display:flex;gap:8px;background:#111419;padding:6px;border-radius:18px;position:sticky;top:8px;z-index:5}.tab{flex:1;border:0;background:transparent;color:#ddd;padding:13px 8px;border-radius:14px;font-size:16px;font-weight:700}.tab.active{background:var(--accent);color:#111}.banner{margin:18px 0;border-radius:22px;padding:24px 20px;background:linear-gradient(135deg,#282015,#14171c);border:1px solid #403728}.banner h1{margin:0 0 8px;font-size:25px}.banner p{margin:0;color:#c7cad0;line-height:1.4}.section{font-size:22px;font-weight:800;margin:24px 4px 12px}.card{background:var(--card);border:1px solid #252a31;border-radius:22px;margin:12px 0;overflow:hidden}.cardtop{display:flex;gap:12px;padding:16px;align-items:center}.icon{width:62px;height:62px;border-radius:16px;background:#090b0e;display:flex;align-items:center;justify-content:center;font-size:29px;flex:none}.name{font-size:20px;font-weight:800}.bonus{display:inline-block;margin-top:7px;padding:5px 9px;border-radius:8px;background:#9bdb6a;color:#10150f;font-weight:800;font-size:13px}.geo{color:var(--muted);font-size:12px;margin-top:6px}.cardbottom{border-top:1px solid #2a2e35;padding:12px 16px;display:flex;justify-content:space-between;align-items:center}.promo{color:#ddd}.copy{border:0;background:none;color:#aaa;font-size:14px}.go{display:block;margin:0 16px 16px;background:var(--accent);color:#111;text-decoration:none;text-align:center;padding:14px;border-radius:14px;font-weight:900}.empty{color:var(--muted);text-align:center;padding:35px 10px}.note{color:#777;font-size:11px;line-height:1.4;text-align:center;margin-top:20px}</style></head><body><div class="wrap"><div class="header"><div><div class="logo">🎁 BONUS UP</div><div class="sub">Бонусы и лучшие предложения</div></div><div>18+</div></div><div class="tabs"><button class="tab active" data-cat="casino">🎰 ИГРЫ</button><button class="tab" data-cat="bets">⚽ СПОРТ</button></div><div class="banner"><h1>🔥 Лучшие бонусы в одном месте</h1><p>Выбирай предложение, смотри бонус и переходи к оператору.</p></div><div class="section" id="sectionTitle">🎰 Игры</div><div id="offers"><div class="empty">Загрузка предложений…</div></div><div class="note">18+. Только для совершеннолетних. Условия, доступность и правила зависят от страны и оператора.</div></div><script>const tg=window.Telegram&&window.Telegram.WebApp;if(tg){tg.ready();tg.expand()}let all=[];function esc(s){return String(s??\'\').replace(/[&<>"\']/g,m=>({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',\'"\':\'&quot;\',"\'":\'&#039;\'}[m]))}function render(cat){const box=document.getElementById(\'offers\');const items=all.filter(x=>x.category===cat);document.getElementById(\'sectionTitle\').textContent=cat===\'bets\'?\'⚽ Спорт\':\'🎰 Игры\';if(!items.length){box.innerHTML=\'<div class="empty">Пока нет активных предложений.</div>\';return}box.innerHTML=items.map(x=>`<article class="card"><div class="cardtop"><div class="icon">${cat===\'bets\'?\'⚽\':\'🎰\'}</div><div><div class="name">${esc(x.name)}</div>${x.bonus?`<span class="bonus">${esc(x.bonus)}</span>`:\'\'}${x.geo?`<div class="geo">GEO: ${esc(x.geo)}</div>`:\'\'}</div></div><div class="cardbottom"><div class="promo">${x.promo?`ПРОМО <b>${esc(x.promo)}</b>`:\'Партнёрское предложение\'}</div>${x.promo?`<button class="copy" onclick="copyPromo(\'${esc(x.promo)}\')">▣ Скопировать</button>`:\'\'}</div><a class="go" href="${esc(x.url)}" target="_blank" rel="noopener">🎁 ПОЛУЧИТЬ БОНУС</a></article>`).join(\'\')}async function load(){try{const r=await fetch(\'/api/offers\');all=await r.json();render(\'casino\')}catch(e){document.getElementById(\'offers\').innerHTML=\'<div class="empty">Не удалось загрузить предложения.</div>\'}}function copyPromo(v){navigator.clipboard?.writeText(v);if(tg)tg.showPopup({title:\'Промокод\',message:\'Промокод скопирован\',buttons:[{type:\'ok\'}]})}document.querySelectorAll(\'.tab\').forEach(b=>b.onclick=()=>{document.querySelectorAll(\'.tab\').forEach(x=>x.classList.remove(\'active\'));b.classList.add(\'active\');render(b.dataset.cat)});load();</script></body></html>'

async def mini_app(request):
    return web.Response(text=MINI_APP_HTML, content_type="text/html")

async def api_offers(request):
    with closing(db()) as con:
        rows = con.execute("SELECT id,category,name,description,bonus,url,geo FROM offers WHERE enabled=1 ORDER BY category,position,id").fetchall()
    return web.json_response([{"id":r[0],"category":r[1],"name":r[2],"description":r[3],"bonus":r[4],"url":r[5],"geo":r[6],"promo":""} for r in rows])

async def health(request):
    return web.Response(text="ok")

async def main():
    init_db()
    seed_demo()
    bot = Bot(TOKEN)
    app = web.Application()
    app.router.add_get("/app", mini_app)
    app.router.add_get("/api/offers", api_offers)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
