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
        # Additive migrations preserve existing partner links and offers.
        cols = [r[1] for r in con.execute("PRAGMA table_info(offers)").fetchall()]
        for col, definition in [("geo", "TEXT DEFAULT ''"), ("promo_code", "TEXT DEFAULT ''"), ("logo_url", "TEXT DEFAULT ''")]:
            if col not in cols:
                con.execute(f"ALTER TABLE offers ADD COLUMN {col} {definition}")
        con.execute("""
        CREATE TABLE IF NOT EXISTS banners (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL DEFAULT '',
            image_url TEXT DEFAULT '',
            button_text TEXT DEFAULT 'СМОТРЕТЬ',
            url TEXT NOT NULL DEFAULT '',
            enabled INTEGER DEFAULT 1,
            position INTEGER DEFAULT 0
        )
        """)
        con.commit()


def seed_demo():
    # Never seed fake example.com offers into production.
    return


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
    kb.button(text="⚽ БУКМЕКЕРЫ")
    kb.button(text="🎰 КАЗИНО")
    if WEBAPP_URL:
        kb.button(text="🎰 ЕЩЁ БОЛЬШЕ БОНУСОВ 🎰", web_app=WebAppInfo(url=WEBAPP_URL))
        kb.adjust(2, 1)
    else:
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
    kb.button(text="🏷 Промокод / логотип", callback_data="adm:promo")
    kb.button(text="🖼 Баннеры Mini App", callback_data="adm:banners")
    kb.button(text="➕ Добавить баннер", callback_data="adm:banneradd")
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
    kb.button(text="🏷 Промокод / логотип", callback_data=f"adm:promoon:{oid}")
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


@dp.callback_query(F.data == "adm:promo")
async def adm_promo(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    await call.message.edit_text("🏷 Выбери оффер, чтобы изменить промокод и логотип:", reply_markup=admin_offer_keyboard("adm:promoon"))
    await call.answer()


@dp.callback_query(F.data.startswith("adm:promoon:"))
async def adm_promoon(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    oid = int(call.data.split(":")[2])
    admin_state[call.from_user.id] = {"action":"promo","step":"promo_code","offer_id":oid,"data":{}}
    await call.message.edit_text("🏷 Отправь промокод. Если убрать промокод — напиши -.")
    await call.answer()


@dp.callback_query(F.data == "adm:banners")
async def adm_banners(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    with closing(db()) as con:
        rows = con.execute("SELECT id,title,enabled,position FROM banners ORDER BY position,id").fetchall()
    kb = InlineKeyboardBuilder()
    lines = ["🖼 БАННЕРЫ MINI APP", ""]
    if not rows:
        lines.append("Баннеров пока нет.")
    for bid,title,enabled,pos in rows:
        lines.append(f"{'🟢' if enabled else '🔴'} #{bid} {title or 'Без заголовка'} | позиция {pos}")
        kb.button(text=f"{'🔴 Выключить' if enabled else '🟢 Включить'} #{bid}", callback_data=f"adm:bannertoggle:{bid}")
    kb.button(text="⬅️ В админ-панель", callback_data="adm:home")
    kb.adjust(1)
    await call.message.edit_text("\\n".join(lines), reply_markup=kb.as_markup())
    await call.answer()


@dp.callback_query(F.data.startswith("adm:bannertoggle:"))
async def adm_bannertoggle(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    bid = int(call.data.split(":")[2])
    with closing(db()) as con:
        con.execute("UPDATE banners SET enabled=CASE enabled WHEN 1 THEN 0 ELSE 1 END WHERE id=?", (bid,))
        con.commit()
    await call.answer("Статус баннера обновлён")
    await adm_banners(call)


@dp.callback_query(F.data == "adm:banneradd")
async def adm_banneradd(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    admin_state[call.from_user.id] = {"action":"banner","step":"title","data":{}}
    await call.message.edit_text("➕ НОВЫЙ БАННЕР\n\n1/6. Заголовок баннера:")
    await call.answer()


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

    if action == "promo":
        if step == "promo_code":
            state["data"]["promo_code"] = "" if text == "-" else text
            state["step"] = "logo_url"
            await message.answer("Отправь HTTPS-ссылку на логотип. Если не нужен — напиши -.")
            return
        if step == "logo_url":
            logo = "" if text == "-" else text
            if logo and not logo.startswith(("https://", "http://")):
                await message.answer("Нужна ссылка на изображение http:// или https://, либо -")
                return
            with closing(db()) as con:
                con.execute("UPDATE offers SET promo_code=?,logo_url=? WHERE id=?",
                            (state["data"]["promo_code"], logo, state["offer_id"]))
                con.commit()
            admin_state.pop(uid, None)
            await message.answer("✅ Промокод и логотип сохранены.", reply_markup=admin_keyboard())
            return

    if action == "banner":
        fields = [
            ("title", "2/6. Текст под заголовком:"),
            ("text", "3/6. HTTPS-ссылка на изображение или -:"),
            ("image_url", "4/6. Текст кнопки:"),
            ("button_text", "5/6. Ссылка перехода баннера (https://...):"),
            ("url", "6/6. Позиция баннера (число, например 1):"),
        ]
        if step == "title":
            state["data"]["title"] = text
            state["step"] = "text"
            await message.answer(fields[0][1])
            return
        if step == "text":
            state["data"]["text"] = text
            state["step"] = "image_url"
            await message.answer(fields[1][1])
            return
        if step == "image_url":
            if text != "-" and not text.startswith(("https://", "http://")):
                await message.answer("Нужна ссылка http:// или https://, либо -")
                return
            state["data"]["image_url"] = "" if text == "-" else text
            state["step"] = "button_text"
            await message.answer(fields[2][1])
            return
        if step == "button_text":
            state["data"]["button_text"] = text
            state["step"] = "url"
            await message.answer(fields[3][1])
            return
        if step == "url":
            if not text.startswith(("https://", "http://")):
                await message.answer("Нужна партнёрская ссылка http:// или https://")
                return
            state["data"]["url"] = text
            state["step"] = "position"
            await message.answer(fields[4][1])
            return
        if step == "position":
            try:
                pos = int(text)
            except ValueError:
                await message.answer("Позиция должна быть числом.")
                return
            d = state["data"]
            with closing(db()) as con:
                con.execute("INSERT INTO banners(title,text,image_url,button_text,url,enabled,position) VALUES(?,?,?,?,?,1,?)",
                            (d["title"],d["text"],d["image_url"],d["button_text"],d["url"],pos))
                con.commit()
            admin_state.pop(uid, None)
            await message.answer("✅ Баннер добавлен и включён.", reply_markup=admin_keyboard())
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


MINI_APP_HTML = '''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>BONUS UP</title><script src="https://telegram.org/js/telegram-web-app.js"></script><style>
:root{--bg:#090909;--card:#171613;--muted:#aaa397;--text:#fff9ec;--gold:#f4c542;--gold2:#ffe58a;--line:#3a301a}
*{box-sizing:border-box}html{background:var(--bg)}body{margin:0;background:radial-gradient(ellipse at 50% -10%,#38280b 0,transparent 45%),var(--bg);color:var(--text);font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:760px;margin:auto;padding:calc(18px + env(safe-area-inset-top)) 15px calc(105px + env(safe-area-inset-bottom))}
.header{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:3px 0 22px}.logo{font-size:25px;font-weight:950;letter-spacing:1px}.sub{color:var(--muted);font-size:11px;margin-top:5px;letter-spacing:1px}.tabs{display:flex;gap:8px;background:#12110fee;padding:7px;border:1px solid #4b3a18;border-radius:18px;position:fixed;top:auto;bottom:calc(8px + env(safe-area-inset-bottom));left:50%;transform:translateX(-50%);width:calc(100% - 30px);max-width:690px;z-index:20;box-shadow:0 8px 30px #000b;backdrop-filter:blur(18px)}.tab{flex:1;border:0;background:transparent;color:#c8c1b4;padding:13px 8px;border-radius:12px;font-size:13px;font-weight:900;letter-spacing:.4px}.tab.active{background:linear-gradient(115deg,#ffe58a,#d7a21c);color:#181100;box-shadow:0 4px 14px #dca71925}
.banner{position:relative;overflow:hidden;margin:18px 0 22px;border-radius:23px;padding:25px 21px;background:linear-gradient(120deg,#3a2a0c,#211909 54%,#121212);border:1px solid #5b4518;box-shadow:inset 0 1px #ffffff10,0 12px 30px #0008}.banner:after{content:"🎁";position:absolute;right:-5px;bottom:-26px;font-size:110px;opacity:.14;transform:rotate(-12deg);pointer-events:none}.banner h1{margin:0 0 10px;font-size:27px;line-height:1.08;letter-spacing:-.6px;font-weight:950;max-width:86%}.banner p{margin:0;color:#d2c6a8;line-height:1.5;font-size:13px;max-width:85%}
.section{font-size:22px;font-weight:950;letter-spacing:-.4px;margin:24px 4px 13px}.card{position:relative;background:linear-gradient(135deg,#1c1b17,#121212 75%);border:1px solid #30291a;border-radius:21px;margin:12px 0;overflow:hidden;box-shadow:0 8px 20px #0003}.card:before{content:"";position:absolute;left:0;top:17px;bottom:17px;width:3px;background:linear-gradient(var(--gold2),#a9750b);border-radius:0 3px 3px 0}
.cardtop{display:flex;gap:13px;padding:17px 15px;align-items:center}.icon{width:60px;height:60px;border-radius:17px;background:linear-gradient(145deg,#35270c,#17130a);border:1px solid #55421b;display:flex;align-items:center;justify-content:center;font-size:27px;flex:none}.name{font-size:18px;font-weight:900;letter-spacing:-.2px}.bonus{display:inline-block;margin-top:8px;padding:6px 9px;border-radius:8px;background:linear-gradient(110deg,#ffe58a,#e1ad28);color:#181100;font-weight:950;font-size:12px}.geo{display:inline-block;color:#e1c66e;background:#292213;border:1px solid #51411c;border-radius:7px;font-size:10px;padding:4px 7px;margin-top:7px}
.cardbottom{border-top:1px solid #2c271c;padding:12px 15px;display:flex;justify-content:space-between;align-items:center;gap:8px}.promo{color:#f4e6bd;font-size:12px}.copy{border:1px solid #59471d;background:#241d0d;color:var(--gold2);border-radius:9px;padding:8px 10px;font-size:11px;font-weight:850}.go{display:block;margin:0 14px 14px;background:linear-gradient(110deg,#ffe58a,#dba51b);color:#191200;text-decoration:none;text-align:center;padding:14px;border-radius:13px;font-weight:950;font-size:13px;letter-spacing:.3px;box-shadow:0 5px 15px #d7a21d20}.empty{color:var(--muted);text-align:center;padding:35px 12px;border:1px dashed #3c3321;border-radius:18px;font-size:13px}.note{color:#898378;font-size:10px;line-height:1.55;text-align:center;margin-top:28px;padding:22px 4px 8px;border-top:1px solid #2c271c}.note:before{content:"BONUS UP";display:block;color:var(--gold2);font-size:17px;font-weight:950;letter-spacing:1px;margin-bottom:9px}.note:after{content:"18+";display:block;color:#ff7777;border:2px solid #e74b4b;border-radius:7px;width:38px;padding:4px 0;margin:12px auto;font-size:14px;font-weight:950}
@media(min-width:560px){.wrap{padding-left:22px;padding-right:22px}.banner{padding:31px 28px}.banner h1{font-size:34px}.cardtop{padding:20px}.icon{width:66px;height:66px}}
</style></head><body><div class="wrap"><div class="header"><div><div class="logo">🎁 BONUS UP</div><div class="sub">Бонусы и лучшие предложения</div></div><div>18+</div></div><div class="tabs"><button class="tab active" data-cat="casino">🎰 ИГРЫ</button><button class="tab" data-cat="bets">⚽ СПОРТ</button></div><div class="banner"><h1>🔥 Лучшие бонусы в одном месте</h1><p>Выбирай предложение, смотри бонус и переходи к оператору.</p></div><div class="section" id="sectionTitle">🎰 Игры</div><div id="offers"><div class="empty">Загрузка предложений…</div></div><div class="note">BONUS UP — каталог партнёрских предложений.<br><span style="display:inline-block;border:2px solid #e74b4b;color:#ff7777;border-radius:6px;padding:3px 8px;margin:9px 0;font-weight:900">18+</span><br>Азартные игры связаны с риском финансовых потерь. Играйте ответственно и только в пределах своих возможностей. Если азарт выходит из-под контроля, обратитесь за профессиональной помощью.<br><br><a href="/terms" style="color:#d7b64e;margin:0 8px">Условия использования</a><a href="/privacy" style="color:#d7b64e;margin:0 8px">Политика конфиденциальности</a></div></div><script>const tg=window.Telegram&&window.Telegram.WebApp;if(tg){tg.ready();tg.expand()}let all=[];function esc(s){return String(s??\'\').replace(/[&<>"\']/g,m=>({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',\'"\':\'&quot;\',"\'":\'&#039;\'}[m]))}function render(cat){const box=document.getElementById(\'offers\');const items=all.filter(x=>x.category===cat);document.getElementById(\'sectionTitle\').textContent=cat===\'bets\'?\'⚽ Спорт\':\'🎰 Игры\';if(!items.length){box.innerHTML=\'<div class="empty">Пока нет активных предложений.</div>\';return}box.innerHTML=items.map(x=>`<article class="card"><div class="cardtop"><div class="icon">${cat===\'bets\'?\'⚽\':\'🎰\'}</div><div><div class="name">${esc(x.name)}</div>${x.bonus?`<span class="bonus">${esc(x.bonus)}</span>`:\'\'}${x.geo?`<div class="geo">GEO: ${esc(x.geo)}</div>`:\'\'}</div></div><div class="cardbottom"><div class="promo">${x.promo?`ПРОМО <b>${esc(x.promo)}</b>`:\'Партнёрское предложение\'}</div>${x.promo?`<button class="copy" onclick="copyPromo(\'${esc(x.promo)}\')">▣ Скопировать</button>`:\'\'}</div><a class="go" href="/go/${x.id}" target="_blank" rel="noopener">🎁 ПОЛУЧИТЬ БОНУС</a></article>`).join(\'\')}async function load(){try{const r=await fetch(\'/api/offers\');all=await r.json();render(\'casino\')}catch(e){document.getElementById(\'offers\').innerHTML=\'<div class="empty">Не удалось загрузить предложения.</div>\'}}function copyPromo(v){navigator.clipboard?.writeText(v);if(tg)tg.showPopup({title:\'Промокод\',message:\'Промокод скопирован\',buttons:[{type:\'ok\'}]})}document.querySelectorAll(\'.tab\').forEach(b=>b.onclick=()=>{document.querySelectorAll(\'.tab\').forEach(x=>x.classList.remove(\'active\'));b.classList.add(\'active\');render(b.dataset.cat)});load();</script></body></html>'''

async def mini_app(request):
    return web.Response(text=MINI_APP_HTML, content_type="text/html")

async def api_offers(request):
    with closing(db()) as con:
        rows = con.execute("""
            SELECT id,category,name,description,bonus,url,geo,promo_code,logo_url
            FROM offers WHERE enabled=1 ORDER BY category,position,id
        """).fetchall()
    return web.json_response([
        {"id":r[0],"category":r[1],"name":r[2],"description":r[3],"bonus":r[4],
         "url":r[5],"geo":r[6],"promo":r[7] or "", "logo_url":r[8] or ""}
        for r in rows
    ])

async def api_banners(request):
    with closing(db()) as con:
        rows = con.execute("SELECT id,title,text,image_url,button_text,url FROM banners WHERE enabled=1 ORDER BY position,id").fetchall()
    return web.json_response([{"id":r[0],"title":r[1],"text":r[2],"image_url":r[3],"button_text":r[4],"url":r[5]} for r in rows])

async def go_offer(request):
    try:
        offer_id = int(request.match_info["offer_id"])
    except (TypeError, ValueError):
        raise web.HTTPNotFound()
    with closing(db()) as con:
        row = con.execute("SELECT url FROM offers WHERE id=? AND enabled=1", (offer_id,)).fetchone()
        if row:
            con.execute("INSERT INTO clicks(user_id,offer_id) VALUES(NULL,?)", (offer_id,))
            con.commit()
    if not row or not row[0].startswith(("https://", "http://")):
        raise web.HTTPNotFound()
    raise web.HTTPFound(row[0])

async def go_banner(request):
    try:
        banner_id = int(request.match_info["banner_id"])
    except (TypeError, ValueError):
        raise web.HTTPNotFound()
    with closing(db()) as con:
        row = con.execute("SELECT url FROM banners WHERE id=? AND enabled=1", (banner_id,)).fetchone()
    if not row or not row[0].startswith(("https://", "http://")):
        raise web.HTTPNotFound()
    raise web.HTTPFound(row[0])

async def terms_page(request):
    return web.Response(text="<html lang='ru'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><body style='background:#090909;color:#fff9ec;font:16px/1.7 Arial;padding:24px'><a style='color:#f4c542' href='/app'>← BONUS UP</a><h1 style='color:#f4c542'>Условия использования</h1><p>BONUS UP — каталог партнёрских предложений. Мы не принимаем ставки, не проводим азартные игры и не управляем сайтами операторов.</p><p>Информация может меняться. Проверяйте действующие условия, возрастные ограничения, доступность услуг в вашей стране и правила оператора.</p><p>Переход по партнёрским ссылкам может приносить проекту вознаграждение. Только 18+.</p></body></html>", content_type="text/html")

async def privacy_page(request):
    return web.Response(text="<html lang='ru'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><body style='background:#090909;color:#fff9ec;font:16px/1.7 Arial;padding:24px'><a style='color:#f4c542' href='/app'>← BONUS UP</a><h1 style='color:#f4c542'>Политика конфиденциальности</h1><p>Бот может хранить Telegram ID, имя пользователя, имя профиля, источник запуска и техническую статистику переходов по предложениям для работы сервиса и аналитики.</p><p>После перехода на партнёрский сайт обработка данных регулируется правилами соответствующего оператора.</p><p>Для запроса об удалении данных свяжитесь с администратором BONUS UP через Telegram.</p></body></html>", content_type="text/html")

async def health(request):
    return web.Response(text="ok")

async def main():
    init_db()
    seed_demo()
    bot = Bot(TOKEN)
    app = web.Application()
    app.router.add_get("/app", mini_app)
    app.router.add_get("/api/offers", api_offers)
    app.router.add_get("/api/banners", api_banners)
    app.router.add_get("/go/{offer_id}", go_offer)
    app.router.add_get("/go/banner/{banner_id}", go_banner)
    app.router.add_get("/terms", terms_page)
    app.router.add_get("/privacy", privacy_page)
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
