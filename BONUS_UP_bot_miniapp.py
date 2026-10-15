import os
import sqlite3
import asyncio
import base64
from io import BytesIO
from urllib.parse import urlparse
from contextlib import closing

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, ReplyKeyboardRemove
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
WELCOME_VIDEO_PATH = os.getenv("WELCOME_VIDEO_PATH", "")

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
        CREATE TABLE IF NOT EXISTS campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT DEFAULT '',
            body TEXT NOT NULL DEFAULT '',
            media_type TEXT DEFAULT '',
            media_file_id TEXT DEFAULT '',
            button_text TEXT DEFAULT '',
            button_url TEXT DEFAULT '',
            enabled INTEGER DEFAULT 1,
            position INTEGER DEFAULT 0,
            sent_count INTEGER DEFAULT 0,
            click_count INTEGER DEFAULT 0,
            last_sent_at TEXT DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)
        campaign_cols = [r[1] for r in con.execute("PRAGMA table_info(campaigns)").fetchall()]
        if "interval_hours" not in campaign_cols:
            con.execute("ALTER TABLE campaigns ADD COLUMN interval_hours INTEGER DEFAULT 8")
        user_cols = [r[1] for r in con.execute("PRAGMA table_info(users)").fetchall()]
        if "last_campaign_id" not in user_cols:
            con.execute("ALTER TABLE users ADD COLUMN last_campaign_id INTEGER DEFAULT 0")
        con.execute("""
        CREATE TABLE IF NOT EXISTS campaign_sends (
            campaign_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            sent_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(campaign_id,user_id)
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS banners (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL DEFAULT '',
            image_url TEXT DEFAULT '',
            button_text TEXT DEFAULT 'СМОТРЕТЬ',
            url TEXT NOT NULL DEFAULT '',
            enabled INTEGER DEFAULT 1,
            position INTEGER DEFAULT 0,
            clicks INTEGER DEFAULT 0
        )
        """)
        banner_cols = [r[1] for r in con.execute("PRAGMA table_info(banners)").fetchall()]
        if "clicks" not in banner_cols:
            con.execute("ALTER TABLE banners ADD COLUMN clicks INTEGER DEFAULT 0")
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


def mini_app_keyboard():
    kb = InlineKeyboardBuilder()
    if WEBAPP_URL:
        kb.button(text="🎁 Ещё больше бонусов 🎁", web_app=WebAppInfo(url=WEBAPP_URL))
    return kb.as_markup() if WEBAPP_URL else None


def main_keyboard():
    # The catalog lives inside the Mini App; do not recreate old category buttons.
    return mini_app_keyboard()


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
    kb.button(text="📢 Рекламные публикации", callback_data="adm:campaigns")
    kb.button(text="👥 Пользователи", callback_data="adm:users")
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
    # Remove the old persistent user category keyboard before showing inline admin controls.
    await message.answer("⌨️ Убираю старую клавиатуру…", reply_markup=ReplyKeyboardRemove())
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

    # Clear any old persistent Telegram reply keyboard still cached by the client.
    text = (
        "🎁 BONUS UP — БУКМЕКЕРЫ И КАЗИНО\n\n"
        "🔥 Открой для себя самые горячие бонусы!\n"
        "🎰 Бонусы казино и бесплатные вращения\n"
        "⚽ Спортивные бонусы и бесплатные ставки\n"
        "🎟️ Эксклюзивные промокоды\n"
        "💰 Найди свой следующий бонус!\n\n"
        "👇 Выбирай категорию и забирай свой бонус!"
    )
    if WELCOME_VIDEO_PATH and os.path.isfile(WELCOME_VIDEO_PATH):
        await message.answer_video(
            FSInputFile(WELCOME_VIDEO_PATH),
            reply_markup=ReplyKeyboardRemove()
        )
        await message.answer(text, reply_markup=mini_app_keyboard())
    else:
        await message.answer(text, reply_markup=ReplyKeyboardRemove())
        if WEBAPP_URL:
            await message.answer("👇 Открой каталог бонусов:", reply_markup=mini_app_keyboard())


@dp.message(Command("admin"))
async def admin(message: Message):
    await send_admin(message)


@dp.message(Command("bonus"))
async def bonus(message: Message):
    await message.answer(
        "🎁 BONUS UP\n\nАктуальные предложения букмекеров и казино — в одном месте.",
        reply_markup=mini_app_keyboard()
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
        "🎁 BONUS UP\n\nАктуальные предложения букмекеров и казино — в одном месте.",
        reply_markup=mini_app_keyboard()
    )
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

    category, name, desc, bonus, url, _geo = row
    text = f"🎁 {name}\n\n"
    if bonus:
        text += f"💰 {bonus}\n"
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



@dp.callback_query(F.data == "adm:users")
async def adm_users(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    with closing(db()) as con:
        total = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        today = con.execute("SELECT COUNT(*) FROM users WHERE date(created_at)=date('now')").fetchone()[0]
    await call.message.edit_text(f"👥 ПОЛЬЗОВАТЕЛИ\n\nВсего уникальных: {total}\nНовых сегодня (UTC): {today}", reply_markup=admin_keyboard())
    await call.answer()


@dp.callback_query(F.data == "adm:campaigns")
async def adm_campaigns(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    with closing(db()) as con:
        rows = con.execute("SELECT id,title,enabled,interval_hours,sent_count,click_count FROM campaigns ORDER BY position,id").fetchall()
    kb = InlineKeyboardBuilder()
    lines = ["📢 РЕКЛАМНЫЕ ПУБЛИКАЦИИ", "", "Текст, фото/видео, CTA-кнопки и расписание."]
    for cid,title,enabled,interval,sent,clicks in rows:
        lines.append(f"{'🟢' if enabled else '🔴'} #{cid} {title or 'Без названия'} — каждые {interval} ч. | отправок {sent} | кликов {clicks}")
        kb.button(text=f"{'🔴 Выключить' if enabled else '🟢 Включить'} #{cid}", callback_data=f"adm:camp_toggle:{cid}")
        kb.button(text=f"🗑 Удалить #{cid}", callback_data=f"adm:camp_delete:{cid}")
    kb.button(text="➕ Создать публикацию", callback_data="adm:camp_add")
    kb.button(text="⬅️ Админ-панель", callback_data="adm:home")
    kb.adjust(1)
    await call.message.edit_text("\n".join(lines), reply_markup=kb.as_markup())
    await call.answer()


@dp.callback_query(F.data == "adm:camp_add")
async def adm_camp_add(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    admin_state[call.from_user.id] = {"action":"campaign","step":"title","data":{}}
    await call.message.edit_text("📢 НОВАЯ ПУБЛИКАЦИЯ\n\n1/6. Название для админки:")
    await call.answer()


@dp.callback_query(F.data.startswith("adm:camp_toggle:"))
async def adm_camp_toggle(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    cid = int(call.data.rsplit(":",1)[1])
    with closing(db()) as con:
        con.execute("UPDATE campaigns SET enabled=CASE enabled WHEN 1 THEN 0 ELSE 1 END WHERE id=?", (cid,))
        con.commit()
    await call.answer("Статус изменён")
    await adm_campaigns(call)


@dp.callback_query(F.data.startswith("adm:camp_delete:"))
async def adm_camp_delete(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    cid = int(call.data.rsplit(":",1)[1])
    with closing(db()) as con:
        con.execute("DELETE FROM campaign_sends WHERE campaign_id=?", (cid,))
        con.execute("DELETE FROM campaigns WHERE id=?", (cid,))
        con.commit()
    await call.answer("Удалено")
    await adm_campaigns(call)


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
    await call.message.edit_text("🏷 Выбери оффер для настройки промокода или логотипа:", reply_markup=admin_offer_keyboard("adm:promoon"))
    await call.answer()


@dp.callback_query(F.data.startswith("adm:promoon:"))
async def adm_promoon(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    oid = int(call.data.split(":")[2])
    with closing(db()) as con:
        row = con.execute("SELECT name,promo_code,logo_url FROM offers WHERE id=?", (oid,)).fetchone()
    if not row:
        await call.answer("Оффер не найден", show_alert=True)
        return
    name, promo_code, logo_url = row
    admin_state.pop(call.from_user.id, None)
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Изменить промокод", callback_data=f"adm:promocode:{oid}")
    kb.button(text="🖼 Загрузить / изменить логотип", callback_data=f"adm:offerlogo:{oid}")
    kb.button(text="🗑 Удалить логотип", callback_data=f"adm:clearlogo:{oid}")
    kb.button(text="⬅️ К списку офферов", callback_data="adm:promo")
    kb.adjust(1)
    await call.message.edit_text(
        f"🏷 {name}\n\nПромокод: {promo_code or 'не задан'}\nЛоготип: {'добавлен' if logo_url else 'не задан'}\n\nВыбери, что нужно изменить. Промокод и логотип сохраняются независимо.",
        reply_markup=kb.as_markup()
    )
    await call.answer()


@dp.callback_query(F.data.startswith("adm:promocode:"))
async def adm_promocode(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    oid = int(call.data.split(":")[2])
    admin_state[call.from_user.id] = {"action":"promo_code_only","step":"promo_code","offer_id":oid,"data":{}}
    await call.message.edit_text("✏️ Отправь новый промокод. Чтобы удалить существующий, отправь -.")
    await call.answer()


@dp.callback_query(F.data.startswith("adm:offerlogo:"))
async def adm_offerlogo(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    oid = int(call.data.split(":")[2])
    admin_state[call.from_user.id] = {"action":"logo_only","step":"logo_url","offer_id":oid,"data":{}}
    await call.message.edit_text("🖼 Отправь логотип изображением (можно как фото или как файл JPG/PNG до 1,5 МБ) либо прямую HTTPS-ссылку. Промокод не изменится. Для отмены напиши «отмена».")
    await call.answer()


@dp.callback_query(F.data.startswith("adm:clearlogo:"))
async def adm_clearlogo(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    oid = int(call.data.split(":")[2])
    with closing(db()) as con:
        con.execute("UPDATE offers SET logo_url='' WHERE id=?", (oid,))
        con.commit()
    await call.answer("Логотип удалён")
    call.data = f"adm:promoon:{oid}"
    await adm_promoon(call)


@dp.callback_query(F.data == "adm:banners")
async def adm_banners(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    with closing(db()) as con:
        rows = con.execute("SELECT id,title,enabled,position,clicks FROM banners ORDER BY position,id").fetchall()
    kb = InlineKeyboardBuilder()
    lines = ["🖼 БАННЕРЫ MINI APP", ""]
    if not rows:
        lines.append("Баннеров пока нет.")
    for bid,title,enabled,pos,clicks in rows:
        lines.append(f"{'🟢' if enabled else '🔴'} #{bid} {title or 'Без заголовка'} | позиция {pos} | переходы {clicks}")
        kb.button(text=f"{'🔴 Выключить' if enabled else '🟢 Включить'} #{bid}", callback_data=f"adm:bannertoggle:{bid}")
    kb.button(text="➕ Добавить баннер в карусель", callback_data="adm:banneradd")
    kb.button(text="⬅️ В админ-панель", callback_data="adm:home")
    kb.adjust(1)
    await call.message.edit_text("\n".join(lines), reply_markup=kb.as_markup())
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
    await call.message.edit_text(
        "➕ НОВЫЙ БАННЕР В КАРУСЕЛЬ MINI APP\n\n"
        "1/4. Название для админки (его видишь только ты):"
    )
    await call.answer()


@dp.message(F.photo | F.video | F.document)
async def admin_campaign_media(message: Message):
    uid = message.from_user.id
    state = admin_state.get(uid)
    if not is_admin(uid) or not state:
        return

    # Offer logo upload accepts Telegram photos and image documents independently
    # from promo-code editing. Existing promo_code is never overwritten here.
    if state.get("action") in {"logo_only", "promo"} and state.get("step") == "logo_url":
        if message.photo:
            file_id = message.photo[-1].file_id
            mime = "image/jpeg"
            size = message.photo[-1].file_size or 0
        elif message.document and (message.document.mime_type or "").startswith("image/"):
            file_id = message.document.file_id
            mime = message.document.mime_type or "image/jpeg"
            size = message.document.file_size or 0
        elif message.video:
            await message.answer("Нужно изображение, не видео. Отправь логотип как фото или файл JPG/PNG.")
            return
        else:
            return
        if size > 1_500_000:
            await message.answer("Изображение слишком большое. Отправь логотип размером до 1,5 МБ.")
            return
        buffer = BytesIO()
        await message.bot.download(file_id, destination=buffer)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        logo = f"data:{mime};base64," + encoded
        with closing(db()) as con:
            con.execute("UPDATE offers SET logo_url=? WHERE id=?", (logo, state["offer_id"]))
            con.commit()
        oid = state["offer_id"]
        admin_state.pop(uid, None)
        await message.answer("✅ Логотип сохранён. Промокод не менялся. Открой Mini App заново, чтобы проверить карточку.", reply_markup=admin_keyboard())
        return

    # Carousel banner upload: accept Telegram photos or image documents directly.
    if state.get("action") == "banner" and state.get("step") == "image_url":
        if message.photo:
            file_id = message.photo[-1].file_id
            mime = "image/jpeg"
            size = message.photo[-1].file_size or 0
        elif message.document and (message.document.mime_type or "").startswith("image/"):
            file_id = message.document.file_id
            mime = message.document.mime_type or "image/jpeg"
            size = message.document.file_size or 0
        elif message.video:
            await message.answer("Для карусели нужно изображение. Отправь баннер как фото или файл JPG/PNG.")
            return
        else:
            await message.answer("Отправь баннер как фото или файл JPG/PNG.")
            return
        if size > 1_800_000:
            await message.answer("Баннер слишком большой. Отправь изображение до 1,8 МБ.")
            return
        buffer = BytesIO()
        await message.bot.download(file_id, destination=buffer)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        state["data"]["image_url"] = f"data:{mime};base64," + encoded
        state["step"] = "url"
        await message.answer("3/4. Партнёрская ссылка (https://...), куда переходить при нажатии на баннер:")
        return

    if state.get("action") != "campaign" or state.get("step") != "media":
        return
    if message.photo:
        state["data"]["media_type"] = "photo"
        state["data"]["media_file_id"] = message.photo[-1].file_id
    elif message.video:
        state["data"]["media_type"] = "video"
        state["data"]["media_file_id"] = message.video.file_id
    else:
        await message.answer("Для рекламной публикации отправь фото или видео, не файл документа.")
        return
    state["step"] = "button_text"
    await message.answer("4/6. Текст кнопки или - без кнопки:")


@dp.message()
async def admin_text(message: Message):
    uid = message.from_user.id
    if not is_admin(uid) or uid not in admin_state:
        return

    state = admin_state[uid]
    action = state["action"]
    step = state["step"]
    text = (message.text or "").strip()

    # Never consume bot commands as field values (e.g. /admin as a promo code).
    if text.startswith("/"):
        if text.split()[0].split("@")[0].lower() == "/admin":
            admin_state.pop(uid, None)
            await send_admin(message)
        elif text.split()[0].split("@")[0].lower() == "/start":
            admin_state.pop(uid, None)
            await start(message)
        return

    if text.lower() in {"отмена", "/cancel"}:
        admin_state.pop(uid, None)
        await message.answer("❌ Отменено.", reply_markup=admin_keyboard())
        return

    if action == "campaign":
        d = state["data"]
        if step == "title":
            d["title"] = text
            state["step"] = "body"
            await message.answer("2/6. Текст публикации:")
            return
        if step == "body":
            d["body"] = "" if text == "-" else text
            state["step"] = "media"
            await message.answer("3/6. Пришли фото или видео отдельным сообщением. Для текста без медиа напиши -.")
            return
        if step == "media":
            if text != "-":
                await message.answer("Пришли фото/видео отдельным сообщением или напиши -.")
                return
            d["media_type"] = ""
            d["media_file_id"] = ""
            state["step"] = "button_text"
            await message.answer("4/6. Текст кнопки или - без кнопки:")
            return
        if step == "button_text":
            d["button_text"] = "" if text == "-" else text[:64]
            if d["button_text"]:
                state["step"] = "button_url"
                await message.answer("5/6. Ссылка кнопки (https://...).")
            else:
                d["button_url"] = ""
                state["step"] = "interval"
                await message.answer("6/6. Интервал в часах: 8 = 3 раза в день, 12 = 2 раза в день.")
            return
        if step == "button_url":
            if not text.startswith(("https://","http://")):
                await message.answer("Нужна ссылка http:// или https://.")
                return
            d["button_url"] = text
            state["step"] = "interval"
            await message.answer("6/6. Интервал в часах: 8 = 3 раза в день, 12 = 2 раза в день.")
            return
        if step == "interval":
            if not d.get("body") and not d.get("media_file_id"):
                await message.answer("Нужен текст или медиа. Отправь текст на шаге 2 либо фото/видео на шаге 3.")
                state["step"] = "body"
                return
            try:
                interval = int(text)
                if interval < 8 or interval > 24:
                    raise ValueError
            except ValueError:
                await message.answer("Введи число от 8 до 24.")
                return
            with closing(db()) as con:
                pos = con.execute("SELECT COALESCE(MAX(position),0)+1 FROM campaigns").fetchone()[0]
                con.execute("INSERT INTO campaigns(title,body,media_type,media_file_id,button_text,button_url,enabled,position,interval_hours) VALUES(?,?,?,?,?,?,0,?,?)",
                    (d["title"],d["body"],d.get("media_type",""),d.get("media_file_id",""),d.get("button_text",""),d.get("button_url",""),pos,interval))
                con.commit()
            admin_state.pop(uid, None)
            await message.answer("✅ Публикация сохранена и выключена. Включи её кнопкой в разделе «📢 Рекламные публикации», когда будешь готов.", reply_markup=admin_keyboard())
            return

    if action == "promo_code_only" and step == "promo_code":
        promo = "" if text == "-" else text
        with closing(db()) as con:
            con.execute("UPDATE offers SET promo_code=? WHERE id=?", (promo, state["offer_id"]))
            con.commit()
        admin_state.pop(uid, None)
        await message.answer("✅ Промокод сохранён. Логотип не менялся.", reply_markup=admin_keyboard())
        return

    if action in {"logo_only", "promo"} and step == "logo_url":
        logo = "" if text == "-" else text
        if logo and not logo.startswith(("https://", "http://")):
            await message.answer("Отправь логотип фото/файлом, прямой HTTPS-ссылкой на изображение или «-» для удаления.")
            return
        with closing(db()) as con:
            con.execute("UPDATE offers SET logo_url=? WHERE id=?", (logo, state["offer_id"]))
            con.commit()
        admin_state.pop(uid, None)
        await message.answer("✅ Логотип сохранён. Промокод не менялся.", reply_markup=admin_keyboard())
        return

    if action == "promo" and step == "promo_code":
        # Compatibility with any in-flight old flow: store the code before logo selection.
        state["data"]["promo_code"] = "" if text == "-" else text
        state["step"] = "logo_url"
        await message.answer("2/2. Теперь отправь логотип картинкой/файлом или прямой HTTPS-ссылкой. Чтобы оставить логотип без изменений, отправь «отмена».")
        return

    if action == "banner":
        if step == "title":
            state["data"]["title"] = text
            state["step"] = "image_url"
            await message.answer("2/4. Отправь баннер фотографией или файлом JPG/PNG. Можно также прислать прямую HTTPS-ссылку на изображение.")
            return
        if step == "image_url":
            if not text.startswith(("https://", "http://")):
                await message.answer("Отправь баннер фотографией/файлом JPG или PNG либо прямой ссылкой на изображение.")
                return
            state["data"]["image_url"] = text
            state["step"] = "url"
            await message.answer("3/4. Партнёрская ссылка (https://...), куда переходить при нажатии на баннер:")
            return
        if step == "url":
            if not text.startswith(("https://", "http://")):
                await message.answer("Нужна партнёрская ссылка http:// или https://")
                return
            state["data"]["url"] = text
            state["step"] = "position"
            await message.answer("4/4. Позиция в карусели (число, например 1):")
            return
        if step == "position":
            try:
                pos = int(text)
            except ValueError:
                await message.answer("Позиция должна быть числом.")
                return
            d = state["data"]
            if not d.get("image_url"):
                state["step"] = "image_url"
                await message.answer("Сначала добавь изображение баннера: отправь его фото/файлом или прямой HTTPS-ссылкой.")
                return
            with closing(db()) as con:
                # Position is 1-based. Shift existing banners down so position 1 really appears first.
                con.execute("UPDATE banners SET position=position+1 WHERE position>=?", (pos,))
                con.execute("INSERT INTO banners(title,text,image_url,button_text,url,enabled,position) VALUES(?,?,?,?,?,1,?)",
                            (d["title"],"",d["image_url"],"",d["url"],pos))
                con.commit()
            admin_state.pop(uid, None)
            await message.answer("✅ Баннер добавлен и включён в карусель Mini App. Нажатие на него ведёт по партнёрской ссылке. Открой Mini App заново для проверки.", reply_markup=admin_keyboard())
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


MINI_APP_HTML = '''<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>BONUS UP</title><script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{--bg:#090909;--card:#191919;--muted:#aaa397;--text:#fff9ec;--gold:#ffd21a;--gold2:#ffe58a;--line:#393021}
*{box-sizing:border-box}html{background:var(--bg)}body{margin:0;background:radial-gradient(ellipse at 50% -10%,#38280b 0,transparent 45%),var(--bg);color:var(--text);font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:760px;margin:auto;padding:calc(10px + env(safe-area-inset-top)) 12px calc(78px + env(safe-area-inset-bottom))}
.header{display:flex;align-items:center;justify-content:space-between;gap:8px;margin:0 0 13px}.brand-head{min-width:0;position:relative;padding:0;flex:1}.brand-head-art{display:block;width:min(100%,270px);height:auto;max-height:65px;overflow:visible;filter:drop-shadow(0 2px 5px #d49a1d45)}.brand-head .sub{display:none}.sub{color:#bdb4a3;font-size:10px;margin-top:5px;letter-spacing:.25px;text-shadow:0 1px 8px #000}.head-actions{display:flex;align-items:center;gap:7px}.profile{display:flex;align-items:center;gap:6px;min-width:0;max-width:145px}.avatar{width:32px;height:32px;border-radius:50%;background:linear-gradient(135deg,#ffe58a,#d7a21c);color:#211700;display:flex;align-items:center;justify-content:center;overflow:hidden;font-weight:950;font-size:16px;flex:none;border:1px solid #6b531e}.avatar img{width:100%;height:100%;object-fit:cover}.profile-name{font-size:11px;font-weight:800;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:85px}.lang-select{width:64px;max-width:64px;min-width:64px;box-sizing:border-box;background:#17140d;color:var(--gold2);border:1px solid #57451d;border-radius:9px;padding:6px 5px;font-weight:850;font-size:11px}.menu-wrap{position:relative}.menu-button{width:32px;height:34px;border:1px solid #49391b;border-radius:12px;background:#17140d;color:var(--text);font-size:23px;font-weight:900}.top-menu{display:none;position:absolute;right:0;top:44px;width:220px;padding:7px;background:#1b1b1b;border:1px solid #4b402b;border-radius:15px;box-shadow:0 12px 40px #000c;z-index:40}.top-menu.open{display:block}.top-menu button{display:block;width:100%;border:0;background:transparent;color:inherit;text-align:left;font:inherit;padding:12px 14px;cursor:pointer}.top-menu button+button,.top-menu button+a{border-top:1px solid #302b20}.top-menu a{display:block;padding:12px 10px;color:var(--text);text-decoration:none;font-size:13px;border-radius:9px}.top-menu a:active{background:#332914}@media(max-width:390px){.profile-name{display:none}.head-actions{gap:5px}.lang-select{width:64px;max-width:64px;min-width:64px}}
.tabs{display:flex;gap:6px;background:#12110fee;padding:5px;border:1px solid #4b3a18;border-radius:15px;position:fixed;bottom:calc(5px + env(safe-area-inset-bottom));left:50%;transform:translateX(-50%);width:calc(100% - 20px);max-width:690px;z-index:20;box-shadow:0 8px 30px #000b;backdrop-filter:blur(18px)}.tab{flex:1;border:0;background:transparent;color:#c8c1b4;padding:10px 6px;border-radius:10px;font-size:12px;font-weight:900;letter-spacing:.2px}.tab.active{background:linear-gradient(115deg,#ffe58a,#d7a21c);color:#181100;box-shadow:0 4px 14px #dca71925}
.banner{position:relative;width:83%;margin:10px auto 13px;overflow:hidden;border-radius:16px;padding:0;background:#080808;border:1px solid #5b4518;box-shadow:0 7px 18px #0007;cursor:pointer}.banner-image-link{display:block;position:relative;width:100%;aspect-ratio:5/3;overflow:hidden;line-height:0;background:#080808}.banner-image-link .banner-backdrop{position:absolute;inset:-24px;display:block;width:calc(100% + 48px);height:calc(100% + 48px);object-fit:cover;object-position:center;filter:blur(28px) brightness(.48) saturate(1.7);transform:scale(1.12)}.banner-image-link .banner-foreground{position:absolute;inset:0;display:block;width:100%;height:100%;object-fit:contain;object-position:center}.banner h1,.banner p,.banner .go{display:none}
.section{font-size:18px;font-weight:950;letter-spacing:-.3px;margin:16px 3px 9px}
.card{position:relative;background:#1c1c1e;border:1px solid #332d20;border-radius:24px;margin:10px 0;overflow:hidden;box-shadow:0 5px 16px #0005;cursor:pointer}
.cardtop{display:flex;gap:14px;padding:16px 14px;align-items:center;min-height:96px;background:#1c1c1e}.icon{width:68px;height:68px;border-radius:15px;background:#111;border:1px solid #303030;display:flex;align-items:center;justify-content:center;font-size:22px;flex:none;overflow:hidden}.icon img,.modal-logo img,.mini-logo img{width:100%;height:100%;object-fit:cover}.icon,.modal-logo,.mini-logo{position:relative}.brand-fallback{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:15px;font-weight:950;letter-spacing:-.5px;color:#ffe58a;background:#111}.icon img,.modal-logo img,.mini-logo img{position:absolute;inset:0;z-index:1;object-fit:cover;padding:0;background:transparent}.icon img.brand-img-1win,.modal-logo img.brand-img-1win,.mini-logo img.brand-img-1win{transform:scale(1.14)}.cardmain{flex:1;min-width:0;display:flex;flex-direction:column}.name{font-size:15px;font-weight:900;letter-spacing:-.2px;overflow-wrap:anywhere;order:2;margin-top:8px}.badges{display:flex;flex-wrap:wrap;gap:6px;margin-top:0;order:1}.bonus{display:inline-block;padding:6px 10px;border-radius:10px;background:#f4cb00;color:#171100;font-weight:950;font-size:12px}.bonus.alt{background:#24532d;color:#d4ffd4}.arrow{width:58px;height:58px;border:0;border-radius:18px;background:#ffd500;color:#111;display:flex;align-items:center;justify-content:center;text-decoration:none;font-size:28px;font-weight:950;flex:none;z-index:2}.cardbottom{border-top:1px solid #303033;padding:13px 18px;display:flex;justify-content:space-between;align-items:center;gap:10px;background:#1c1c1e;min-height:56px}.promo{color:#bdbdc2;font-size:12px;overflow-wrap:anywhere}.promo b{color:#f5f5f7}.copy{border:1px solid #5c4718;background:#211c0d;color:#f4d878;border-radius:12px;padding:10px 12px;font-size:12px;font-weight:900;flex:none}.go{display:block;margin:0 10px 10px;background:linear-gradient(110deg,#ffe58a,#dba51b);color:#191200;text-decoration:none;text-align:center;padding:11px;border-radius:11px;font-weight:950;font-size:12px;letter-spacing:.3px;box-shadow:0 5px 15px #d7a21d20}.empty{color:var(--muted);text-align:center;padding:24px 10px;border:1px dashed #3c3321;border-radius:18px;font-size:13px}
.note{color:#898378;font-size:10px;line-height:1.5;text-align:center;margin-top:14px;padding:12px 4px 5px;border-top:1px solid #2c271c}.brand-footer{position:relative;margin:0 auto 10px;text-align:center;overflow:visible;background:radial-gradient(ellipse at 50% 46%,#6b470c40,transparent 67%);padding:0}.brand-footer-art{display:block;width:min(100%,500px);height:auto;max-height:none;margin:0 auto;overflow:visible;filter:drop-shadow(0 2px 7px #d49a1d45)}.note .footer-age{display:inline-block;border:2px solid #e74b4b;color:#ff7777;border-radius:6px;padding:3px 8px;margin:0 0 10px;font-weight:900;font-size:12px}.note .footer-links{margin-top:12px}.note .footer-links a{color:#d7b64e;margin:0 8px;display:inline-block}
.modal-backdrop{display:none;position:fixed;inset:0;background:#000b;backdrop-filter:blur(8px);z-index:50;align-items:center;justify-content:center;padding:10px;padding-top:calc(16px + env(safe-area-inset-top));padding-bottom:calc(16px + env(safe-area-inset-bottom))}.modal-backdrop.open{display:flex}.modal{width:100%;max-width:650px;max-height:86vh;overflow:auto;background:linear-gradient(145deg,#292929,#191919 72%);border:1px solid #49443a;border-radius:20px;box-shadow:0 20px 80px #000c;padding:12px}.modalhead{display:flex;align-items:center;gap:9px}.modal-logo{width:48px;height:48px;border-radius:13px;overflow:hidden;background:#2d2617;border:1px solid #51421e;display:flex;align-items:center;justify-content:center;font-size:27px;flex:none}.modal-title{font-size:19px;font-weight:950;line-height:1.1;overflow-wrap:anywhere}.modal-close{margin-left:auto;width:34px;height:34px;border:0;border-radius:50%;background:#4a4a4a;color:white;font-size:24px;flex:none}.modal-section{background:#202020;border-radius:13px;padding:11px;margin-top:10px}.muted-label{color:#9c9c9c;font-size:12px;margin-bottom:7px}.description{font-size:13px;line-height:1.45;white-space:pre-wrap;overflow-wrap:anywhere}.promo-large{display:flex;align-items:center;gap:10px}.promo-value{font-size:16px;font-weight:850;overflow-wrap:anywhere;flex:1;min-width:0}.play{display:block;margin-top:10px;background:var(--gold);color:#171100;text-decoration:none;text-align:center;padding:12px;border-radius:11px;font-size:14px;font-weight:950}.other-title{font-size:12px;font-weight:950;color:#bcbcbc;margin:13px 2px 8px}.other-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.mini-game{display:flex;align-items:center;gap:7px;min-width:0;border:0;background:#202020;color:var(--text);padding:7px;border-radius:13px;text-align:left;text-decoration:none}.mini-logo{width:36px;height:36px;border-radius:9px;background:#332a17;display:flex;align-items:center;justify-content:center;overflow:hidden;flex:none}.mini-name{font-size:12px;font-weight:850;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.mini-link{font-size:11px;color:#aaa;margin-top:4px}
@media(min-width:560px){.wrap{padding-left:22px;padding-right:22px}.banner{padding:0}.banner h1{font-size:34px}.cardtop{padding:15px 13px}.icon{width:54px;height:54px}.arrow{width:44px;height:44px}.modal{padding:18px}}
</style></head>
<body>
<div class="wrap">
 <div class="header"><div class="brand-head"><svg class="brand-head-art" viewBox="0 0 330 76" role="img" aria-label="BONUS UP"><defs><linearGradient id="headGold" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#fffbe1"/><stop offset=".3" stop-color="#ffd75b"/><stop offset=".58" stop-color="#c9870a"/><stop offset=".78" stop-color="#fff0a4"/><stop offset="1" stop-color="#8d5700"/></linearGradient><filter id="headGlow" x="-.3" y="-.5" width="1.6" height="2"><feGaussianBlur stdDeviation="2.5"/></filter></defs><path d="M5 60 Q90 75 176 59 T305 46" fill="none" stroke="#e7a51a" stroke-width="4" opacity=".65" filter="url(#headGlow)"/><path d="M5 60 Q90 75 176 59 T305 46" fill="none" stroke="url(#headGold)" stroke-width="2"/><path d="M24 24 L18 6 L34 16 L45 1 L49 21 L66 13 L59 31 Z" fill="url(#headGold)" stroke="#8b5a08" stroke-width="1.2"/><text x="4" y="56" font-family="Arial Black,Arial,sans-serif" font-weight="1000" font-size="40" letter-spacing="-2.6" fill="url(#headGold)" stroke="#704405" stroke-width=".7" paint-order="stroke">BONUS</text><text x="205" y="56" font-family="Arial Black,Arial,sans-serif" font-weight="1000" font-size="40" letter-spacing="-2.5" fill="url(#headGold)" stroke="#704405" stroke-width=".7" paint-order="stroke">P</text><path d="M190 42 L190 2 L179 14 M190 2 L201 14" fill="none" stroke="url(#headGold)" stroke-width="7" stroke-linecap="round" stroke-linejoin="round"/><text x="7" y="73" font-family="Arial,sans-serif" font-size="12" font-weight="700" fill="#ffe7a0">Бонусы и лучшие предложения</text></svg><div class="sub" data-i18n="subtitle">Лучшие бонусы и промокоды в одном месте!</div></div><div class="head-actions"><div class="profile" id="userProfile" aria-label="Профиль Telegram"><div class="avatar" id="userAvatar">?</div><div class="profile-name" id="userName"></div></div><select class="lang-select" id="languageSelect" aria-label="Язык"><option value="ru">🇷🇺 RU</option><option value="en">🇬🇧 EN</option><option value="de">🇩🇪 DE</option><option value="tr">🇹🇷 TR</option><option value="hi">🇮🇳 HI</option></select><div class="menu-wrap"><button class="menu-button" id="menuButton" aria-label="Меню" aria-expanded="false">⋮</button><div class="top-menu" id="topMenu"><button type="button" id="refreshPage" data-i18n="refreshPage">Обновить страницу</button><button type="button" id="createShortcut" data-i18n="createShortcut">Создать ярлык</button><a href="/terms" data-i18n="terms">Условия использования</a></div></div><span style="font-size:12px;font-weight:900">18+</span></div></div>
 <div class="tabs"><button class="tab active" data-cat="casino" data-i18n="games">🎰 ИГРЫ</button><button class="tab" data-cat="bets" data-i18n="sport">⚽ СПОРТ</button></div>
 <div class="banner" id="promoBanner"><h1>🔥 Лучшие бонусы в одном месте</h1><p>Выбирай предложение, смотри бонус и переходи к оператору.</p></div>
 <div id="bannerDots" style="display:flex;justify-content:center;gap:6px;margin:-12px 0 18px"></div>
 <div class="section" id="sectionTitle">🎰 Игры</div><div id="offers"><div class="empty">Загрузка предложений…</div></div>
 <div class="note"><div class="brand-footer"><svg class="brand-footer-art" viewBox="0 0 700 300" role="img" aria-label="BONUS UP — лучшие бонусы и промокоды в одном месте"><defs><linearGradient id="footerGold" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#fffde7"/><stop offset=".24" stop-color="#fff0a0"/><stop offset=".49" stop-color="#e5a51b"/><stop offset=".7" stop-color="#fff5bd"/><stop offset="1" stop-color="#8e5800"/></linearGradient><radialGradient id="coinGold"><stop stop-color="#fff4ae"/><stop offset=".5" stop-color="#e8ad23"/><stop offset="1" stop-color="#8e5600"/></radialGradient><filter id="footerGlow" x="-.3" y="-.5" width="1.6" height="2"><feGaussianBlur stdDeviation="5"/></filter></defs><path d="M8 108 Q155 20 300 78 T690 45 M0 137 Q155 55 310 110 T700 72" fill="none" stroke="#e8ad28" stroke-width="7" opacity=".48" filter="url(#footerGlow)"/><path d="M8 108 Q155 20 300 78 T690 45 M0 137 Q155 55 310 110 T700 72" fill="none" stroke="url(#footerGold)" stroke-width="3"/><g transform="translate(70 95) rotate(-25)"><ellipse rx="38" ry="47" fill="url(#coinGold)" stroke="#fff0a1" stroke-width="3"/><ellipse rx="29" ry="38" fill="none" stroke="#8d5b06" stroke-width="3"/><path d="M-12 -18 Q0 -32 12 -18 L-12 18 Q0 31 12 18" fill="none" stroke="#fff2ac" stroke-width="6"/></g><g transform="translate(628 105) rotate(23)"><ellipse rx="34" ry="44" fill="url(#coinGold)" stroke="#fff0a1" stroke-width="3"/><ellipse rx="25" ry="34" fill="none" stroke="#8d5b06" stroke-width="3"/><path d="M-10 -15 Q0 -27 10 -15 L-10 15 Q0 27 10 15" fill="none" stroke="#fff2ac" stroke-width="5"/></g><g transform="translate(535 83)"><path d="M-35 -6 Q-62 -43 -43 -45 Q-22 -45 -17 -12 Q-20 -53 2 -48 Q21 -43 0 -7" fill="#f2bd37" stroke="#fff0a1" stroke-width="3"/><rect x="-38" y="-12" width="77" height="58" rx="6" fill="url(#footerGold)" stroke="#fff0a1" stroke-width="3"/><path d="M0 -12 V46 M-38 7 H39" stroke="#a36b06" stroke-width="8"/><path d="M-3 -12 V46 M-38 7 H39" stroke="#fff2a4" stroke-width="3"/></g><path d="M174 48 L185 21 L207 39 L231 7 L239 43 L266 30 L258 57 Z" fill="url(#footerGold)" stroke="#8a5907" stroke-width="2"/><text x="88" y="155" font-family="Arial Black,Arial,sans-serif" font-size="82" font-weight="1000" letter-spacing="-5" fill="url(#footerGold)" stroke="#4b2d00" stroke-width="4" paint-order="stroke">BONUS</text><text x="380" y="155" font-family="Arial Black,Arial,sans-serif" font-size="82" font-weight="1000" letter-spacing="-5" fill="url(#footerGold)" stroke="#4b2d00" stroke-width="4" paint-order="stroke">UP</text><path d="M488 126 V25 L465 52 M488 25 L511 52" fill="none" stroke="url(#footerGold)" stroke-width="16" stroke-linecap="round" stroke-linejoin="round"/><path d="M90 174 Q350 201 610 170" fill="none" stroke="url(#footerGold)" stroke-width="6" stroke-linecap="round"/><text x="350" y="226" text-anchor="middle" font-family="Arial,sans-serif" font-size="25" font-weight="700" fill="#fff5dd">Лучшие бонусы и промокоды</text><text x="350" y="263" text-anchor="middle" font-family="Arial Black,Arial,sans-serif" font-size="28" font-weight="900" fill="#ffe58a">в одном месте!</text></svg></div><span class="footer-age">18+</span><br>Азартные игры связаны с риском финансовых потерь. Играйте ответственно и только в пределах своих возможностей. Если азарт выходит из-под контроля, обратитесь за профессиональной помощью.<div class="footer-links"><a href="/terms" data-i18n="terms">Условия использования</a></div></div>
</div>
<div class="modal-backdrop" id="modalBackdrop" role="dialog" aria-modal="true" aria-label="Информация о предложении"><div class="modal" id="modalContent"></div></div>
<script>
const tg=window.Telegram&&window.Telegram.WebApp;if(tg){tg.ready();tg.expand()}
const translations={
ru:{subtitle:"Бонусы и лучшие предложения",games:"🎰 ИГРЫ",sport:"⚽ СПОРТ",terms:"Условия использования",refreshPage:"Обновить страницу",createShortcut:"Создать ярлык",privacy:"Политика конфиденциальности",about:"Об игре",description:"Описание этого предложения скоро появится.",promo:"ПРОМО",partner:"Партнёрское предложение",copy:"▣ Копировать",promoCode:"ПРОМОКОД",play:"ИГРАТЬ ↗",linkSoon:"ПАРТНЁРСКАЯ ССЫЛКА СКОРО ПОЯВИТСЯ",otherGames:"ДРУГИЕ ИГРЫ",otherBets:"ДРУГИЕ БУКМЕКЕРЫ",open:"Открыть ↗",soon:"Ссылка скоро появится",empty:"Пока нет активных предложений.",failed:"Не удалось загрузить предложения.",bannerTitle:"🔥 Лучшие бонусы в одном месте",bannerText:"Выбирай предложение, смотри бонус и переходи к оператору.",show:"СМОТРЕТЬ",close:"Закрыть",details:"Подробнее",copiedTitle:"Промокод",copied:"Промокод скопирован",copyFailed:"Не удалось скопировать. Скопируйте вручную: "},
en:{subtitle:"Bonuses and top offers",games:"🎰 GAMES",sport:"⚽ SPORTS",terms:"Terms of use",refreshPage:"Refresh page",createShortcut:"Create shortcut",privacy:"Privacy policy",about:"About the game",description:"Offer description coming soon.",promo:"PROMO",partner:"Partner offer",copy:"▣ Copy",promoCode:"PROMO CODE",play:"PLAY ↗",linkSoon:"PARTNER LINK COMING SOON",otherGames:"OTHER GAMES",otherBets:"OTHER BOOKMAKERS",open:"Open ↗",soon:"Link coming soon",empty:"No active offers yet.",failed:"Could not load offers.",bannerTitle:"🔥 Top bonuses in one place",bannerText:"Choose an offer, check the bonus and visit the operator.",show:"VIEW OFFERS",close:"Close",details:"Details",copiedTitle:"Promo code",copied:"Promo code copied",copyFailed:"Could not copy. Copy manually: "},
de:{subtitle:"Boni und Top-Angebote",games:"🎰 SPIELE",sport:"⚽ SPORT",terms:"Nutzungsbedingungen",refreshPage:"Seite aktualisieren",createShortcut:"Verknüpfung erstellen",privacy:"Datenschutzerklärung",about:"Über das Spiel",description:"Beschreibung folgt in Kürze.",promo:"PROMO",partner:"Partnerangebot",copy:"▣ Kopieren",promoCode:"PROMOCODE",play:"SPIELEN ↗",linkSoon:"PARTNERLINK FOLGT IN KÜRZE",otherGames:"WEITERE SPIELE",otherBets:"WEITERE SPORTANBIETER",open:"Öffnen ↗",soon:"Link folgt in Kürze",empty:"Noch keine aktiven Angebote.",failed:"Angebote konnten nicht geladen werden.",bannerTitle:"🔥 Top-Boni an einem Ort",bannerText:"Angebot auswählen, Bonus ansehen und zum Anbieter wechseln.",show:"ANGEBOTE ANSEHEN",close:"Schließen",details:"Details",copiedTitle:"Zwischenablage",copied:"Promocode kopiert",copyFailed:"Kopieren fehlgeschlagen. Bitte manuell kopieren: "},
tr:{subtitle:"Bonuslar ve öne çıkan teklifler",games:"🎰 OYUNLAR",sport:"⚽ SPOR",terms:"Kullanım koşulları",refreshPage:"Sayfayı yenile",createShortcut:"Kısayol oluştur",privacy:"Gizlilik politikası",about:"Oyun hakkında",description:"Teklif açıklaması yakında.",promo:"PROMO",partner:"Ortaklık teklifi",copy:"▣ Kopyala",promoCode:"PROMO KODU",play:"OYNA ↗",linkSoon:"ORTAKLIK BAĞLANTISI YAKINDA",otherGames:"DİĞER OYUNLAR",otherBets:"DİĞER BAHİS SİTELERİ",open:"Aç ↗",soon:"Bağlantı yakında",empty:"Henüz aktif teklif yok.",failed:"Teklifler yüklenemedi.",bannerTitle:"🔥 En iyi bonuslar tek yerde",bannerText:"Teklifi seç, bonusu incele ve operatöre git.",show:"TEKLİFLERİ GÖR",close:"Kapat",details:"Detaylar",copiedTitle:"Promosyon kodu",copied:"Promosyon kodu kopyalandı",copyFailed:"Kopyalanamadı. Lütfen manuel kopyalayın: "},
hi:{subtitle:"बोनस और बेहतरीन ऑफ़र",games:"🎰 गेम",sport:"⚽ स्पोर्ट्स",terms:"उपयोग की शर्तें",refreshPage:"पेज रीफ़्रेश करें",createShortcut:"शॉर्टकट बनाएं",privacy:"गोपनीयता नीति",about:"गेम के बारे में",description:"ऑफ़र का विवरण जल्द आएगा।",promo:"प्रोमो",partner:"पार्टनर ऑफ़र",copy:"▣ कॉपी",promoCode:"प्रोमो कोड",play:"खेलें ↗",linkSoon:"पार्टनर लिंक जल्द आएगा",otherGames:"अन्य गेम",otherBets:"अन्य बुकमेकर",open:"खोलें ↗",soon:"लिंक जल्द आएगा",empty:"अभी कोई सक्रिय ऑफ़र नहीं है।",failed:"ऑफ़र लोड नहीं हो सके।",bannerTitle:"🔥 सभी बेहतरीन बोनस एक जगह",bannerText:"ऑफ़र चुनें, बोनस देखें और ऑपरेटर पर जाएँ।",show:"ऑफ़र देखें",close:"बंद करें",details:"विवरण",copiedTitle:"प्रोमो कोड",copied:"प्रोमो कोड कॉपी हो गया",copyFailed:"कॉपी नहीं हो सका। मैन्युअल कॉपी करें: "}
}
let language="ru";
try{const saved=localStorage.getItem("bonusUpLanguage");if(saved&&translations[saved])language=saved;else{const code=((tg&&tg.initDataUnsafe&&tg.initDataUnsafe.user&&tg.initDataUnsafe.user.language_code)||navigator.language||"ru").slice(0,2).toLowerCase();language=translations[code]?code:"ru"}}catch(e){}
function t(key){return (translations[language]&&translations[language][key])||translations.ru[key]||key}
function applyLanguage(){document.documentElement.lang=language;document.querySelectorAll("[data-i18n]").forEach(el=>{el.textContent=t(el.dataset.i18n)});document.getElementById("languageSelect").value=language;render(currentCat);if(selectedId!==null)openModal(selectedId)}
function setTelegramProfile(){const u=tg&&tg.initDataUnsafe&&tg.initDataUnsafe.user;const avatar=document.getElementById("userAvatar"),name=document.getElementById("userName");if(!u){avatar.textContent="B";name.textContent="BONUS UP";return}const display=[u.first_name,u.last_name].filter(Boolean).join(" ")||(u.username?"@"+u.username:"BONUS UP");name.textContent=display;if(u.photo_url){const img=document.createElement("img");img.src=u.photo_url;img.alt="";img.onerror=()=>{avatar.textContent=(display.trim()[0]||"B").toUpperCase()};avatar.textContent="";avatar.appendChild(img)}else avatar.textContent=(display.trim()[0]||"B").toUpperCase()}
let all=[];let currentCat="casino";let selectedId=null;
setTelegramProfile();
document.getElementById("languageSelect").value=language;
document.getElementById("languageSelect").addEventListener("change",e=>{language=translations[e.target.value]?e.target.value:"ru";try{localStorage.setItem("bonusUpLanguage",language)}catch(err){}applyLanguage()});
const menuButton=document.getElementById("menuButton"),topMenu=document.getElementById("topMenu");
menuButton.addEventListener("click",e=>{e.stopPropagation();const opened=topMenu.classList.toggle("open");menuButton.setAttribute("aria-expanded",String(opened))});
document.getElementById("refreshPage").addEventListener("click",()=>window.location.reload());
document.getElementById("createShortcut").addEventListener("click",()=>{if(tg&&typeof tg.addToHomeScreen==="function"){tg.addToHomeScreen()}else if(tg&&typeof tg.showAlert==="function"){tg.showAlert(language==="ru"?"Чтобы создать ярлык, откройте меню Telegram и выберите «Добавить на главный экран», если этот пункт доступен.":"Open the Telegram menu and choose Add to Home Screen if available.")}else{window.alert(language==="ru"?"Откройте меню браузера и выберите «Добавить на главный экран» или «Создать ярлык».":"Open the browser menu and choose Add to Home Screen or Create shortcut.")}});
document.addEventListener("click",e=>{if(!e.target.closest(".menu-wrap")){topMenu.classList.remove("open");menuButton.setAttribute("aria-expanded","false")}});
applyLanguage();
function esc(s){return String(s??"").replace(/[&<>"\']/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;",\'"\':"&quot;","\'":"&#039;"}[m]))}
function logoHtml(x,cls){const name=String(x.name||"?").trim();const initials=name.split(/\s+/).slice(0,2).map(v=>v[0]||"").join("").toUpperCase();const n=name.toLowerCase();const domain=n.includes("1win")?"1win.com":(n.includes("зенит")||n.includes("zenit"))?"zenit.win":"";const url=x.logo_url|| (domain?"https://www.google.com/s2/favicons?domain="+domain+"&sz=128":"");return '<span class="brand-fallback">'+esc(initials)+'</span>'+(url?'<img class="'+(n.includes("1win")?"brand-img-1win":"")+'" src="'+esc(url)+'" alt="'+esc(name)+'" loading="lazy" onerror="this.remove()">':"")}
function bonusBadges(value){if(!value)return "";let parts=String(value).split(/\s*(?:\||\+|,|;)\s*/).filter(Boolean);if(parts.length===1){let match=String(value).match(/(\d+\s*FS|FS\s*\d+|\d+\s*%|Up to\s*[\d$€£]+)/ig);if(match&&match.length>1)parts=match}return parts.map((p,i)=>\'<span class="bonus \'+(/up to/i.test(p)?"alt":"")+\'">\'+esc(p.trim())+\'</span>\').join("")}
function hasPartnerLink(x){try{const u=new URL(String(x.url||""));const h=u.hostname.toLowerCase();return (u.protocol==="https:"||u.protocol==="http:")&&!["example.com","www.example.com","example.org","www.example.org","example.net","www.example.net"].includes(h)&&!h.endsWith(".example.com")&&!h.endsWith(".example.org")&&!h.endsWith(".example.net")}catch(e){return false}}
function render(cat){currentCat=cat;const box=document.getElementById("offers");const items=all.filter(x=>x.category===cat);const catNames={ru:{casino:"Игры",bets:"Спорт"},en:{casino:"Games",bets:"Sports"},de:{casino:"Spiele",bets:"Sport"},tr:{casino:"Oyunlar",bets:"Spor"},hi:{casino:"गेम",bets:"स्पोर्ट्स"}};document.getElementById("sectionTitle").textContent=(cat==="bets"?"⚽ ":"🎰 ")+((catNames[language]||catNames.ru)[cat]);if(!items.length){box.innerHTML='<div class="empty">'+esc(t("empty"))+'</div>';return}
box.innerHTML=items.filter(x=>hasPartnerLink(x)).map(x=>'<article class="card" data-open="'+Number(x.id)+'" role="link" tabindex="0" aria-label="'+esc(t("play"))+': '+esc(x.name)+'"><div class="cardtop"><div class="icon">'+logoHtml(x,"icon")+'</div><div class="cardmain"><div class="name">'+esc(x.name)+'</div><div class="badges">'+bonusBadges(x.bonus)+'</div></div><a class="arrow" href="/go/'+Number(x.id)+'" aria-label="'+esc(t("play"))+'" onclick="event.stopPropagation()"><span>↗</span></a></div><div class="cardbottom"><div class="promo">'+(x.promo?esc(t("promo"))+' <b>'+esc(x.promo)+'</b>':esc(t("partner")))+'</div>'+(x.promo?'<button class="copy" data-copy="'+Number(x.id)+'">'+esc(t("copy"))+'</button>':'')+'</div></article>').join("");
box.querySelectorAll(".card").forEach(card=>{card.addEventListener("click",e=>{if(e.target.closest(".copy"))return;openModal(Number(card.dataset.open))});card.addEventListener("keydown",e=>{if((e.key==="Enter"||e.key===" ")&&e.target===card){e.preventDefault();openModal(Number(card.dataset.open))}})});
box.querySelectorAll("[data-copy]").forEach(btn=>btn.addEventListener("click",e=>{e.stopPropagation();const item=all.find(x=>x.id===Number(btn.dataset.copy));if(item)copyPromo(item.promo)}))
}
function openModal(id){const x=all.find(item=>item.id===id);if(!x)return;selectedId=id;const other=all.filter(item=>item.category===x.category&&item.id!==id&&hasPartnerLink(item));const modal=document.getElementById("modalContent");
modal.innerHTML='<div class="modalhead"><div class="modal-logo">'+logoHtml(x,"modal-logo")+'</div><div style="min-width:0;flex:1"><div class="badges">'+bonusBadges(x.bonus)+'</div><div class="modal-title" style="margin-top:8px">'+esc(x.name)+'</div></div><button class="modal-close" id="closeModal" aria-label="'+esc(t("close"))+'">×</button></div>'+
'<div class="modal-section"><div class="muted-label">'+esc(t("about"))+'</div><div class="description">'+esc(x.description||t("description"))+'</div></div>'+
(x.promo?'<div class="modal-section"><div class="muted-label">'+esc(t("promoCode"))+'</div><div class="promo-large"><div class="promo-value">'+esc(x.promo)+'</div><button class="copy" id="modalCopy">'+esc(t("copy"))+'</button></div></div>':'')+
(hasPartnerLink(x)?'<a class="play" href="/go/'+Number(x.id)+'" target="_blank" rel="noopener">'+esc(t("play"))+'</a>':'<div class="play" style="opacity:.6;cursor:not-allowed;background:#3a362b;color:#d0c8b5">'+esc(t("linkSoon"))+'</div>')+
(other.length?'<div class="other-title">'+esc(t(x.category==="bets"?"otherBets":"otherGames"))+'</div><div class="other-grid">'+other.map(o=>hasPartnerLink(o)?'<a class="mini-game" href="/go/'+Number(o.id)+'" target="_blank" rel="noopener"><span class="mini-logo">'+logoHtml(o,"mini-logo")+'</span><span style="min-width:0"><span class="mini-name" style="display:block">'+esc(o.name)+'</span><span class="mini-link">'+esc(t("open"))+'</span></span></a>':'<div class="mini-game" style="opacity:.55"><span class="mini-logo">'+logoHtml(o,"mini-logo")+'</span><span style="min-width:0"><span class="mini-name" style="display:block">'+esc(o.name)+'</span><span class="mini-link">'+esc(t("soon"))+'</span></span></div>').join("")+'</div>':'');
document.getElementById("modalBackdrop").classList.add("open");document.body.style.overflow="hidden";
document.getElementById("closeModal").onclick=closeModal;
const cp=document.getElementById("modalCopy");if(cp)cp.onclick=()=>copyPromo(x.promo);
}
function closeModal(){document.getElementById("modalBackdrop").classList.remove("open");document.body.style.overflow="";selectedId=null}
document.getElementById("modalBackdrop").addEventListener("click",e=>{if(e.target.id==="modalBackdrop")closeModal()});document.addEventListener("keydown",e=>{if(e.key==="Escape")closeModal()});
async function copyPromo(v){try{if(navigator.clipboard&&navigator.clipboard.writeText)await navigator.clipboard.writeText(v);else{const el=document.createElement("textarea");el.value=v;document.body.appendChild(el);el.select();document.execCommand("copy");el.remove()}if(tg&&tg.showPopup)tg.showPopup({title:t("copiedTitle"),message:t("copied"),buttons:[{type:"ok"}]})}catch(e){if(tg&&tg.showAlert)tg.showAlert(t("copyFailed")+v)}}
async function load(){try{const r=await fetch("/api/offers");all=await r.json();const br=await fetch("/api/banners");const banners=br.ok?await br.json():[];renderBanners(banners);render("casino")}catch(e){document.getElementById("offers").innerHTML=\'<div class="empty">Не удалось загрузить предложения.</div>\'}}
function renderBanners(items){const box=document.getElementById("promoBanner"),dots=document.getElementById("bannerDots");if(!items.length){dots.innerHTML="";box.style.display="none";return}box.style.display="block";let idx=0;function show(){const b=items[idx];box.innerHTML='<a class="banner-image-link" href="/go/banner/'+Number(b.id)+'" aria-label="Открыть предложение"><img class="banner-backdrop" src="'+esc(b.image_url||"") +'" alt="" aria-hidden="true"><img class="banner-foreground" src="'+esc(b.image_url||"") +'" alt="" loading="lazy"></a>';dots.innerHTML=items.map((b,i)=>'<button aria-label="Баннер '+(i+1)+'" data-index="'+i+'" style="width:'+(i===idx?20:6)+'px;height:6px;padding:0;border:0;border-radius:8px;background:'+(i===idx?'#f4c542':'#554a30')+'"></button>').join("");dots.querySelectorAll("button").forEach(btn=>btn.onclick=()=>{idx=Number(btn.dataset.index);show()})}let sx=0;box.ontouchstart=e=>sx=e.touches[0].clientX;box.ontouchend=e=>{const dx=e.changedTouches[0].clientX-sx;if(Math.abs(dx)>45){idx=(idx+(dx<0?1:-1)+items.length)%items.length;show()}};show()}
document.querySelectorAll(".tab").forEach(b=>b.onclick=()=>{document.querySelectorAll(".tab").forEach(x=>x.classList.remove("active"));b.classList.add("active");render(b.dataset.cat)});load();
</script></body></html>'''

async def mini_app(request):
    return web.Response(text=MINI_APP_HTML, content_type="text/html")

async def api_offers(request):
    with closing(db()) as con:
        rows = con.execute("""
            SELECT id,category,name,description,bonus,url,promo_code,logo_url
            FROM offers WHERE enabled=1
            ORDER BY category,
                CASE
                    WHEN lower(name) LIKE '%1win%' THEN 0
                    WHEN lower(name) LIKE '%зенит%' OR lower(name) LIKE '%zenit%' THEN 1
                    ELSE 2
                END,
                position,id
        """).fetchall()
    return web.json_response([
        {"id":r[0],"category":r[1],"name":r[2],"description":r[3],"bonus":r[4],
         "url":r[5],"promo":r[6] or "", "logo_url":r[7] or ""}
        for r in rows
    ])

async def api_banners(request):
    with closing(db()) as con:
        rows = con.execute("""
            SELECT id,title,text,image_url,button_text,url
            FROM banners WHERE enabled=1
            ORDER BY CASE WHEN position<=0 THEN 2147483647 ELSE position END, id
        """).fetchall()
    return web.json_response([{"id":r[0],"title":r[1],"text":r[2],"image_url":r[3],"button_text":r[4],"url":r[5]} for r in rows])

def is_partner_url(value):
    try:
        parsed = urlparse((value or "").strip())
        host = (parsed.hostname or "").lower()
        return (parsed.scheme in {"https", "http"} and bool(host)
                and host not in {"example.com", "www.example.com", "example.org", "www.example.org", "example.net", "www.example.net"}
                and not host.endswith(".example.com")
                and not host.endswith(".example.org")
                and not host.endswith(".example.net"))
    except (TypeError, ValueError):
        return False


async def go_offer(request):
    try:
        offer_id = int(request.match_info["offer_id"])
    except (TypeError, ValueError):
        raise web.HTTPNotFound()
    with closing(db()) as con:
        row = con.execute("SELECT url FROM offers WHERE id=? AND enabled=1", (offer_id,)).fetchone()
        if row and is_partner_url(row[0]):
            con.execute("INSERT INTO clicks(user_id,offer_id) VALUES(NULL,?)", (offer_id,))
            con.commit()
    if not row or not is_partner_url(row[0]):
        raise web.HTTPNotFound(text="Партнёрская ссылка ещё не подключена.")
    raise web.HTTPFound(row[0])

async def go_banner(request):
    try:
        banner_id = int(request.match_info["banner_id"])
    except (TypeError, ValueError):
        raise web.HTTPNotFound()
    with closing(db()) as con:
        row = con.execute("SELECT url FROM banners WHERE id=? AND enabled=1", (banner_id,)).fetchone()
        if row:
            con.execute("UPDATE banners SET clicks=clicks+1 WHERE id=?", (banner_id,))
            con.commit()
    if not row or not row[0].startswith(("https://", "http://")):
        raise web.HTTPNotFound()
    raise web.HTTPFound(row[0])

async def go_campaign(request):
    try:
        campaign_id = int(request.match_info["campaign_id"])
    except (TypeError, ValueError):
        raise web.HTTPNotFound()
    with closing(db()) as con:
        row = con.execute("SELECT button_url FROM campaigns WHERE id=? AND enabled=1", (campaign_id,)).fetchone()
        if row and row[0].startswith(("https://", "http://")):
            con.execute("UPDATE campaigns SET click_count=click_count+1 WHERE id=?", (campaign_id,))
            con.commit()
    if not row or not row[0].startswith(("https://", "http://")):
        raise web.HTTPNotFound()
    raise web.HTTPFound(row[0])


async def terms_page(request):
    return web.Response(text="<html lang='ru'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><body style='background:#090909;color:#fff9ec;font:16px/1.7 Arial;padding:24px'><a style='color:#f4c542' href='/app'>← BONUS UP</a><h1 style='color:#f4c542'>Условия использования</h1><p>BONUS UP — каталог партнёрских предложений. Мы не принимаем ставки, не проводим азартные игры и не управляем сайтами операторов.</p><p>Информация может меняться. Проверяйте действующие условия, возрастные ограничения, доступность услуг в вашей стране и правила оператора.</p><p>Только 18+.</p></body></html>", content_type="text/html")

async def privacy_page(request):
    return web.Response(text="<html lang='ru'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><body style='background:#090909;color:#fff9ec;font:16px/1.7 Arial;padding:24px'><a style='color:#f4c542' href='/app'>← BONUS UP</a><h1 style='color:#f4c542'>Политика конфиденциальности</h1><p>Бот может хранить Telegram ID, имя пользователя, имя профиля, источник запуска и техническую статистику переходов по предложениям для работы сервиса и аналитики.</p><p>После перехода на партнёрский сайт обработка данных регулируется правилами соответствующего оператора.</p><p>Для запроса об удалении данных свяжитесь с администратором BONUS UP через Telegram.</p></body></html>", content_type="text/html")

async def health(request):
    return web.Response(text="ok")

async def campaign_scheduler(bot: Bot):
    from datetime import datetime, timezone
    while True:
        try:
            with closing(db()) as con:
                campaigns = con.execute("""SELECT id,body,media_type,media_file_id,button_text,button_url,interval_hours,last_sent_at
                    FROM campaigns WHERE enabled=1 ORDER BY position,id""").fetchall()
                users = con.execute("SELECT user_id,last_campaign_id FROM users").fetchall()
                global_row = con.execute("SELECT value FROM app_settings WHERE key='last_campaign_broadcast'").fetchone()
            global_last = global_row[0] if global_row else ""
            global_due = True
            if global_last:
                try:
                    stamp = datetime.fromisoformat(global_last.replace("Z","+00:00"))
                    # Global throttle prevents several campaigns from blasting users one after another.
                    global_due = (datetime.now(timezone.utc)-stamp).total_seconds() >= 8*3600
                except Exception:
                    global_due = True
            if global_due:
                last_campaign_by_user = {int(user_id): int(last_id or 0) for user_id,last_id in users}
                for cid,body,media_type,file_id,button_text,button_url,interval,last_sent in campaigns:
                    due = not last_sent
                    if last_sent:
                        try:
                            stamp = datetime.fromisoformat(last_sent.replace("Z","+00:00"))
                            due = (datetime.now(timezone.utc)-stamp).total_seconds() >= int(interval)*3600
                        except Exception:
                            due = True
                    if not due:
                        continue
                    # If every recorded user received this same campaign last time,
                    # skip it without advancing the schedule; try another due campaign.
                    if len(campaigns) > 1 and users and all(
                        last_campaign_by_user.get(int(user_id), int(last_id or 0)) == cid
                        for user_id, last_id in users
                    ):
                        continue
                    markup = None
                    if button_text and button_url:
                        base_url = WEBAPP_URL.rsplit("/app", 1)[0].rstrip("/") if WEBAPP_URL else ""
                        tracked_url = f"{base_url}/go/campaign/{cid}" if base_url else button_url
                        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=button_text, url=tracked_url)]])
                    delivered = 0
                    for user_id,last_campaign_id in users:
                        if len(campaigns) > 1 and last_campaign_by_user.get(int(user_id), int(last_campaign_id or 0)) == cid:
                            continue
                        try:
                            if media_type == "photo" and file_id:
                                await bot.send_photo(user_id, file_id, caption=body[:1024], reply_markup=markup)
                            elif media_type == "video" and file_id:
                                await bot.send_video(user_id, file_id, caption=body[:1024], reply_markup=markup)
                            else:
                                await bot.send_message(user_id, body[:4096], reply_markup=markup)
                            delivered += 1
                            with closing(db()) as con:
                                con.execute("UPDATE users SET last_campaign_id=? WHERE user_id=?", (cid,user_id))
                                con.execute("INSERT OR IGNORE INTO campaign_sends(campaign_id,user_id) VALUES(?,?)", (cid,user_id))
                                con.commit()
                            last_campaign_by_user[int(user_id)] = int(cid)
                        except Exception:
                            # One blocked user or delivery error must not stop the broadcast.
                            continue
                        await asyncio.sleep(0.05)
                    # Do not consume a campaign's schedule if nobody received it.
                    if delivered:
                        now_iso = datetime.now(timezone.utc).isoformat()
                        with closing(db()) as con:
                            con.execute("UPDATE campaigns SET sent_count=sent_count+?,last_sent_at=? WHERE id=?",
                                        (delivered,now_iso,cid))
                            con.execute("INSERT INTO app_settings(key,value) VALUES('last_campaign_broadcast',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                        (now_iso,))
                            con.commit()
                        # One campaign per 8-hour window; avoid a burst when several are enabled.
                        break
        except Exception:
            pass
        await asyncio.sleep(60)


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
    app.router.add_get("/go/campaign/{campaign_id}", go_campaign)
    app.router.add_get("/terms", terms_page)
    app.router.add_get("/privacy", privacy_page)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    scheduler_task = asyncio.create_task(campaign_scheduler(bot))
    try:
        await dp.start_polling(bot)
    finally:
        scheduler_task.cancel()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
