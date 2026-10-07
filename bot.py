import os
import sqlite3
from contextlib import closing

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
DB_PATH = os.getenv("DB_PATH", "/data/bonus_up.db" if os.path.isdir("/data") else "bonus_up.db")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

dp = Dispatcher()
admin_state = {}


def db():
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA foreign_keys = ON")
    return con


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
    kb.button(text="⚽ БУКМЕКЕРЫ")
    kb.button(text="🎰 КАЗИНО")
    kb.button(text="🔥 ТОП БОНУСЫ")
    kb.button(text="⭐ ЛУЧШИЕ")
    kb.adjust(2, 2)
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
        "🔥 ТОП БОНУСЫ\n\nВыберите категорию:",
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


@dp.message(F.text.in_({"🔥 ТОП БОНУСЫ", "⭐ ЛУЧШИЕ"}))
async def top(message: Message):
    with closing(db()) as con:
        rows = con.execute("""
        SELECT id,name,bonus FROM offers
        WHERE enabled=1
        ORDER BY position,id
        LIMIT 5
        """).fetchall()
    if not rows:
        await message.answer("Пока нет активных предложений.", reply_markup=main_keyboard())
        return
    kb = InlineKeyboardBuilder()
    text = "🔥 ТОП БОНУСЫ\n\n"
    for oid, name, bonus in rows:
        text += f"• {name}"
        if bonus:
            text += f" — {bonus}"
        text += "\n"
        kb.button(text=f"🎁 {name}", callback_data=f"offer:{oid}")
    kb.adjust(1)
    await message.answer(text, reply_markup=kb.as_markup())


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


async def main():
    init_db()
    seed_demo()
    bot = Bot(TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
