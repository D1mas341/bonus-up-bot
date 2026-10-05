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
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DB_PATH = os.getenv("DB_PATH", "bonus_up.db")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set. Put your BotFather token into .env")

dp = Dispatcher()

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
            position INTEGER DEFAULT 0
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
        con.commit()

def add_user(message: Message, source: str = ""):
    u = message.from_user
    with closing(db()) as con:
        con.execute("""
        INSERT INTO users(user_id, username, first_name, source)
        VALUES(?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
          username=excluded.username,
          first_name=excluded.first_name
        """, (u.id, u.username or "", u.first_name or "", source))
        con.commit()

def seed_demo():
    with closing(db()) as con:
        count = con.execute("SELECT COUNT(*) FROM offers").fetchone()[0]
        if count == 0:
            demo = [
                ("bets", "1xBet", "Партнёрский оффер — ссылка добавляется позже.", "", "https://example.com/1xbet", 1, 1),
                ("bets", "MelBet", "Партнёрский оффер — ссылка добавляется позже.", "", "https://example.com/melbet", 1, 2),
                ("bets", "Mellstroy", "Партнёрский оффер — ссылка добавляется позже.", "", "https://example.com/mellstroy", 1, 3),
                ("casino", "PIN-UP Casino", "Партнёрский оффер — ссылка добавляется позже.", "", "https://example.com/pinup", 1, 1),
                ("casino", "Crypto Casino", "Тестовая карточка. Заменить после проверки партнёрки.", "", "https://example.com/crypto", 1, 2),
            ]
            con.executemany("""
                INSERT INTO offers(category,name,description,bonus,url,enabled,position)
                VALUES(?,?,?,?,?,?,?)
            """, demo)
            con.commit()

def main_keyboard():
    kb = ReplyKeyboardBuilder()
    kb.button(text="⚽ БУКМЕКЕРЫ")
    kb.button(text="🎰 КАЗИНО")
    kb.button(text="🔥 ТОП БОНУСЫ")
    kb.button(text="⭐ ЛУЧШИЕ")
    kb.adjust(2, 2)
    return kb.as_markup(resize_keyboard=True)

def offers_keyboard(category: str):
    kb = InlineKeyboardBuilder()
    with closing(db()) as con:
        rows = con.execute("""
            SELECT id, name FROM offers
            WHERE category=? AND enabled=1
            ORDER BY position, id
        """, (category,)).fetchall()
    for offer_id, name in rows:
        kb.button(text=f"🎁 {name}", callback_data=f"offer:{offer_id}")
    kb.adjust(1)
    kb.button(text="⬅️ Главное меню", callback_data="home")
    return kb.as_markup()

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

@dp.message(Command("bonus"))
async def bonus(message: Message):
    await message.answer("🔥 ТОП БОНУСЫ\n\nВыберите категорию:", reply_markup=main_keyboard())

@dp.message(F.text == "⚽ БУКМЕКЕРЫ")
async def bets(message: Message):
    await message.answer("⚽ БУКМЕКЕРЫ\n\nВыберите предложение:", reply_markup=offers_keyboard("bets"))

@dp.message(F.text == "🎰 КАЗИНО")
async def casino(message: Message):
    await message.answer("🎰 ОНЛАЙН-КАЗИНО\n\nВыберите предложение:", reply_markup=offers_keyboard("casino"))

@dp.message(F.text.in_({"🔥 ТОП БОНУСЫ", "⭐ ЛУЧШИЕ"}))
async def top(message: Message):
    with closing(db()) as con:
        rows = con.execute("""
            SELECT id, category, name, bonus, description
            FROM offers WHERE enabled=1 ORDER BY position, id LIMIT 5
        """).fetchall()
    if not rows:
        await message.answer("Пока нет активных предложений.", reply_markup=main_keyboard())
        return
    kb = InlineKeyboardBuilder()
    text = "🔥 ТОП БОНУСЫ\n\n"
    for oid, cat, name, bonus, desc in rows:
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
        "🎁 BONUS UP\n\nАктуальные предложения БК и онлайн-казино в одном месте.",
    )
    await call.message.answer("Выберите раздел:", reply_markup=main_keyboard())
    await call.answer()

@dp.callback_query(F.data.startswith("offer:"))
async def offer(call: CallbackQuery):
    offer_id = int(call.data.split(":")[1])
    with closing(db()) as con:
        row = con.execute("""
            SELECT category,name,description,bonus,url FROM offers
            WHERE id=? AND enabled=1
        """, (offer_id,)).fetchone()
        if row:
            con.execute("INSERT INTO clicks(user_id,offer_id) VALUES(?,?)",
                        (call.from_user.id, offer_id))
            con.commit()
    if not row:
        await call.answer("Предложение больше недоступно.", show_alert=True)
        return
    category, name, desc, bonus, url = row
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
    await call.message.edit_text(title + "\n\nВыберите предложение:",
                                 reply_markup=offers_keyboard(category))
    await call.answer()

async def main():
    init_db()
    seed_demo()
    bot = Bot(TOKEN)
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
