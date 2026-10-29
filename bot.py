import os
import sqlite3
import asyncio
import base64
import hashlib
import hmac
import json
import math
import time
import uuid
from io import BytesIO
from urllib.parse import urlparse, parse_qsl
from contextlib import closing

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, BufferedInputFile, ReplyKeyboardRemove, InputMediaVideo
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


def is_blocked(user_id: int) -> bool:
    if not user_id or is_admin(user_id):
        return False
    with closing(db()) as con:
        return con.execute("SELECT 1 FROM blocked_users WHERE user_id=?", (int(user_id),)).fetchone() is not None


@dp.update.outer_middleware()
async def enforce_user_block(handler, event, data):
    user = None
    if getattr(event, "message", None):
        user = event.message.from_user
    elif getattr(event, "callback_query", None):
        user = event.callback_query.from_user
    if user and is_blocked(user.id):
        if getattr(event, "callback_query", None):
            await event.callback_query.answer("⛔ Доступ к BONUS UP заблокирован.", show_alert=True)
        elif getattr(event, "message", None):
            await event.message.answer("⛔ Доступ к BONUS UP заблокирован.")
        return None
    return await handler(event, data)


def validate_webapp_init_data(init_data: str):
    if not init_data or not TOKEN:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = pairs.pop("hash", "")
        if not received_hash:
            return None
        check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
        secret_key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received_hash):
            return None
        auth_date = int(pairs.get("auth_date", "0"))
        if auth_date <= 0 or abs(int(time.time()) - auth_date) > 86400:
            return None
        user_id = int(json.loads(pairs.get("user", "{}")).get("id", 0))
        return user_id or None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def webapp_access_denied(request):
    init_data = request.headers.get("X-Telegram-Init-Data", "") or request.query.get("tgWebAppData", "")
    user_id = validate_webapp_init_data(init_data)
    if not user_id:
        return web.json_response({"error": "telegram_auth_required"}, status=401)
    if is_blocked(user_id):
        return web.json_response({"error": "access_blocked"}, status=403)
    return None



def make_tracking_token(user_id: int, ttl_seconds: int = 1800) -> str:
    """Create a short-lived signed token to associate a Mini App click with its Telegram user."""
    expires = int(time.time()) + ttl_seconds
    payload = f"{int(user_id)}.{expires}"
    key = hashlib.sha256(TOKEN.encode()).digest()
    signature = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def tracking_user_id(request):
    """Return a Telegram user ID only when the click token signature and expiry are valid."""
    token = request.query.get("track", "")
    try:
        user_text, expires_text, signature = token.split(".", 2)
        user_id, expires = int(user_text), int(expires_text)
        if user_id <= 0 or expires < int(time.time()) or expires > int(time.time()) + 1800:
            return None
        payload = f"{user_id}.{expires}"
        key = hashlib.sha256(TOKEN.encode()).digest()
        expected = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
        return user_id if hmac.compare_digest(expected, signature) else None
    except (ValueError, TypeError):
        return None


async def api_tracking_token(request):
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user_id = validate_webapp_init_data(init_data)
    if not user_id:
        return web.json_response({"error": "telegram_auth_required"}, status=401)
    if is_blocked(user_id):
        return web.json_response({"error": "access_blocked"}, status=403)
    return web.json_response({"token": make_tracking_token(user_id)},
                             headers={"Cache-Control": "no-store"})




def parse_callback_id(data: str, position: int = -1, *, from_right: bool = False) -> int:
    """Return a positive numeric callback ID, or 0 for malformed/stale callback data."""
    try:
        parts = data.rsplit(":", 1) if from_right else data.split(":")
        value = int(parts[position])
        return value if value > 0 else 0
    except (AttributeError, IndexError, TypeError, ValueError):
        return 0


def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    return con


def log_click(con, *, user_id=None, offer_id=None, source="", click_type="offer",
              partner="", banner_id=None, campaign_id=None, target_url="",
              utm_source="", utm_campaign=""):
    """Persist one click while keeping old click totals and records compatible."""
    click_id = uuid.uuid4().hex
    con.execute(
        """INSERT INTO clicks
           (user_id,offer_id,click_id,source,click_type,partner,banner_id,campaign_id,
            target_url,utm_source,utm_campaign)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (user_id, offer_id, click_id, source or "direct", click_type, partner or "",
         banner_id, campaign_id, target_url or "", utm_source or "", utm_campaign or "")
    )
    return click_id


def request_attribution(request, default_source="direct"):
    source = (request.query.get("source") or request.query.get("ref") or
              request.query.get("utm_source") or default_source)[:120]
    utm_source = (request.query.get("utm_source") or "")[:120]
    utm_campaign = (request.query.get("utm_campaign") or "")[:160]
    return source, utm_source, utm_campaign


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
        CREATE TABLE IF NOT EXISTS blocked_users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            reason TEXT DEFAULT '',
            blocked_at DATETIME DEFAULT CURRENT_TIMESTAMP
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
        # Additive click migrations preserve all historical rows and counters.
        click_cols = [r[1] for r in con.execute("PRAGMA table_info(clicks)").fetchall()]
        for col, definition in [
            ("click_id", "TEXT"),
            ("source", "TEXT DEFAULT ''"),
            ("click_type", "TEXT DEFAULT 'offer'"),
            ("partner", "TEXT DEFAULT ''"),
            ("banner_id", "INTEGER"),
            ("campaign_id", "INTEGER"),
            ("target_url", "TEXT DEFAULT ''"),
            ("utm_source", "TEXT DEFAULT ''"),
            ("utm_campaign", "TEXT DEFAULT ''"),
        ]:
            if col not in click_cols:
                con.execute(f"ALTER TABLE clicks ADD COLUMN {col} {definition}")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_clicks_click_id ON clicks(click_id) WHERE click_id IS NOT NULL")
        con.execute("CREATE INDEX IF NOT EXISTS idx_clicks_created_at ON clicks(created_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_clicks_source ON clicks(source)")
        con.execute("""
        CREATE TABLE IF NOT EXISTS conversion_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            click_id TEXT NOT NULL,
            partner TEXT NOT NULL DEFAULT '',
            event_type TEXT NOT NULL DEFAULT 'conversion',
            event_id TEXT DEFAULT '',
            amount REAL DEFAULT 0,
            currency TEXT NOT NULL DEFAULT 'EUR',
            payload TEXT DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(partner, event_id)
        )
        """)
        # Additive migrations preserve existing partner links and offers.
        cols = [r[1] for r in con.execute("PRAGMA table_info(offers)").fetchall()]
        for col, definition in [("geo", "TEXT DEFAULT ''"), ("promo_code", "TEXT DEFAULT ''"), ("logo_url", "TEXT DEFAULT ''"), ("hidden", "INTEGER DEFAULT 0"), ("mini_app_url", "TEXT DEFAULT ''"), ("site_url", "TEXT DEFAULT ''")]:
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
        if "placement" not in campaign_cols:
            con.execute("ALTER TABLE campaigns ADD COLUMN placement TEXT DEFAULT 'broadcast'")
        user_cols = [r[1] for r in con.execute("PRAGMA table_info(users)").fetchall()]
        if "last_campaign_id" not in user_cols:
            con.execute("ALTER TABLE users ADD COLUMN last_campaign_id INTEGER DEFAULT 0")
        con.execute("""
        CREATE TABLE IF NOT EXISTS campaign_sends (
            campaign_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            sent_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            message_id INTEGER DEFAULT NULL,
            PRIMARY KEY(campaign_id,user_id)
        )
        """)
        # Keep every delivered message ID so deleting a campaign can remove all
        # of its published copies, including repeated manual broadcasts.
        con.execute("""
        CREATE TABLE IF NOT EXISTS campaign_delivery_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            sent_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)
        send_cols = [r[1] for r in con.execute("PRAGMA table_info(campaign_sends)").fetchall()]
        if "message_id" not in send_cols:
            con.execute("ALTER TABLE campaign_sends ADD COLUMN message_id INTEGER DEFAULT NULL")
        con.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS revenue_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            partner TEXT NOT NULL,
            amount REAL NOT NULL,
            currency TEXT NOT NULL DEFAULT 'EUR',
            period TEXT DEFAULT '',
            note TEXT DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
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
            clicks INTEGER DEFAULT 0,
            section TEXT NOT NULL DEFAULT 'casino'
        )
        """)
        banner_cols = [r[1] for r in con.execute("PRAGMA table_info(banners)").fetchall()]
        if "clicks" not in banner_cols:
            con.execute("ALTER TABLE banners ADD COLUMN clicks INTEGER DEFAULT 0")
        if "section" not in banner_cols:
            con.execute("ALTER TABLE banners ADD COLUMN section TEXT NOT NULL DEFAULT 'casino'")

        # Banner records are managed only through the admin panel.
        # Do not auto-create or re-enable 1win on startup.
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
        WHERE category=? AND enabled=1 AND COALESCE(hidden,0)=0
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
    kb.button(text="🙈 Скрыть / восстановить", callback_data="adm:visibility")
    kb.button(text="🗑 Удалить оффер", callback_data="adm:delete")
    kb.button(text="🏷 Промокод / логотип", callback_data="adm:promo")
    kb.button(text="🎮 Mini App + сайт (2 ссылки)", callback_data="adm:dual_links")
    kb.button(text="🖼 Баннеры Mini App", callback_data="adm:banners")
    kb.button(text="➕ Добавить баннер", callback_data="adm:banneradd")
    kb.button(text="📊 Клики", callback_data="adm:clicks")
    kb.button(text="📈 Аналитика и доход", callback_data="adm:analytics")
    kb.button(text="🧪 Диагностика базы (временно)", callback_data="adm:dbdiag")
    kb.button(text="📢 Рекламные публикации", callback_data="adm:campaigns")
    kb.button(text="✍️ Разовая текстовая рассылка", callback_data="adm:text_broadcast")
    kb.button(text="👥 Пользователи / блокировка", callback_data="adm:users")
    kb.adjust(1)
    return kb.as_markup()


def admin_offer_keyboard(prefix, include_cancel=True, visible_only=False):
    kb = InlineKeyboardBuilder()
    with closing(db()) as con:
        query = "SELECT id,name,enabled FROM offers"
        if visible_only:
            query += " WHERE COALESCE(hidden,0)=0"
        query += " ORDER BY category,position,id"
        rows = con.execute(query).fetchall()
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


WELCOME_DESCRIPTION = (
    "🏆 Что умеет этот бот?\n\n"
    "Бонусы букмекеров и онлайн-казино в одном месте!\n\n"
    "🏆 Эксклюзивные бонусы и промокоды\n"
    "🎰 Бонусы казино и бесплатные вращения\n"
    "⚽ Спортивные бонусы и бесплатные ставки\n"
    "🎟️ Актуальные акции и предложения\n\n"
    "👇 Нажми «🚀 СТАРТ» и получи доступ ко всем бонусам!"
)


def welcome_start_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 СТАРТ", callback_data="welcome:continue")
    kb.adjust(1)
    return kb.as_markup()


@dp.message(CommandStart())
async def start(message: Message):
    payload = ""
    if message.text and " " in message.text:
        payload = message.text.split(" ", 1)[1][:100]
    add_user(message, payload)

    # The profile preview (description GIF + description text + Telegram Start
    # button) is configured in BotFather. /start must show only the second clip.
    with closing(db()) as con:
        settings = dict(con.execute(
            "SELECT key,value FROM app_settings WHERE key IN "
            "('welcome_video2_animation_file_id','welcome_video2_file_id',"
            "'welcome_media_type','welcome_media_file_id')"
        ).fetchall())
    animation2 = settings.get("welcome_video2_animation_file_id", "")
    video2 = settings.get("welcome_video2_file_id", "")
    text = (
        "🔥 Fresh bonuses & promo codes\n"
        "🎰 Free Spins and casino offers\n"
        "⚽ Sportsbook deals in one place\n\n"
        "👇 Find your offer and claim your bonus!"
    )

    if animation2:
        await message.answer_animation(
            animation2, caption=text, reply_markup=bonus_welcome_keyboard()
        )
    elif video2:
        await message.answer_video(
            video2, caption=text, reply_markup=bonus_welcome_keyboard()
        )
    else:
        # Fallback for installations where the second clip has not been uploaded yet.
        media_type = settings.get("welcome_media_type", "")
        media_file_id = settings.get("welcome_media_file_id", "")
        if media_file_id and media_type == "animation":
            await message.answer_animation(
                media_file_id, caption=text, reply_markup=bonus_welcome_keyboard()
            )
        elif media_file_id and media_type == "video":
            await message.answer_video(
                media_file_id, caption=text, reply_markup=bonus_welcome_keyboard()
            )
        elif media_file_id and media_type == "photo":
            await message.answer_photo(
                media_file_id, caption=text, reply_markup=bonus_welcome_keyboard()
            )
        else:
            await message.answer(
                "Второе видео ещё не настроено. Администратору нужно загрузить его через /setwelcome.",
                reply_markup=mini_app_keyboard()
            )



async def send_start_promotions(message: Message):
    """Send enabled promo blocks configured for placement below the /start welcome CTA."""
    with closing(db()) as con:
        rows = con.execute(
            "SELECT id,body,media_type,media_file_id,button_text,button_url "
            "FROM campaigns WHERE enabled=1 AND COALESCE(placement,'broadcast')='start' "
            "ORDER BY position,id"
        ).fetchall()
    base_url = WEBAPP_URL.rsplit("/app", 1)[0].rstrip("/") if WEBAPP_URL else ""
    for cid, body, media_type, file_id, button_text, button_url in rows:
        markup = None
        if button_text and button_url:
            tracked_url = f"{base_url}/go/campaign/{cid}" if base_url else button_url
            markup = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=button_text, url=tracked_url)]
            ])
        try:
            caption = (body or "")[:1024]
            if media_type == "photo" and file_id:
                await message.answer_photo(file_id, caption=caption or None, reply_markup=markup)
            elif media_type == "video" and file_id:
                await message.answer_video(file_id, caption=caption or None, reply_markup=markup)
            elif body:
                await message.answer(body[:4096], reply_markup=markup)
            with closing(db()) as con:
                con.execute("UPDATE campaigns SET sent_count=sent_count+1 WHERE id=?", (cid,))
                con.commit()
            await asyncio.sleep(0.15)
        except Exception as exc:
            print(f"[start-promotions] campaign_id={cid} error={type(exc).__name__}: {exc}", flush=True)


def bonus_welcome_keyboard():
    kb = InlineKeyboardBuilder()
    if WEBAPP_URL:
        kb.button(text="🎁 ЗАБРАТЬ БОНУСЫ 🎁", web_app=WebAppInfo(url=WEBAPP_URL))
        kb.adjust(1)
        return kb.as_markup()
    return None


@dp.message(Command("setwelcome"))
async def set_welcome_media(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return
    admin_state[message.from_user.id] = {"action": "set_welcome_media"}
    await message.answer(
        "🎞 Отправь один и тот же ролик BONUS UP сюда как GIF/анимацию (не как видео-файл). "
        "Бот сохранит его для отправки после /start. Для профиля до Start загрузи этот же GIF "
        "в BotFather → Edit Bot → Description Picture."
    )


@dp.message(
    (F.animation | F.video | F.document),
    lambda message: admin_state.get(message.from_user.id, {}).get("action") == "set_welcome_media",
)
async def receive_welcome_media(message: Message):
    user_id = message.from_user.id
    if not admin_state.get(user_id) or not is_admin(user_id):
        admin_state.pop(user_id, None)
        return

    animation_id = ""
    video_id = ""
    if message.animation:
        animation_id = message.animation.file_id
    else:
        # Android may send a GIF from Gallery as a Document. Re-upload bytes
        # through Telegram sendAnimation so it is stored as an actual animation.
        media = message.video or message.document
        source_id = media.file_id
        mime = getattr(media, "mime_type", "") or ""
        filename = getattr(media, "file_name", "") or ""
        is_gif = mime == "image/gif" or filename.lower().endswith(".gif")
        extension = ".gif" if is_gif else ".mp4"
        buffer = BytesIO()
        try:
            await message.bot.download(source_id, destination=buffer)
            uploaded = await message.answer_animation(
                animation=BufferedInputFile(buffer.getvalue(), filename="bonus-up-welcome" + extension),
            )
            if uploaded.animation:
                animation_id = uploaded.animation.file_id
                try:
                    await message.bot.delete_message(message.chat.id, uploaded.message_id)
                except Exception:
                    pass
        except Exception:
            animation_id = ""
        if message.video:
            video_id = message.video.file_id
        elif not animation_id:
            video_id = source_id

    with closing(db()) as con:
        if animation_id:
            for key, value in (
                ("welcome_video1_file_id", animation_id),
                ("welcome_video2_animation_file_id", animation_id),
                ("welcome_video2_file_id", video_id),
                ("welcome_media_type", "animation"),
                ("welcome_media_file_id", animation_id),
            ):
                con.execute(
                    "INSERT INTO app_settings(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
        elif video_id:
            for key, value in (
                ("welcome_video1_file_id", video_id),
                ("welcome_video2_file_id", video_id),
                ("welcome_media_type", "video"),
                ("welcome_media_file_id", video_id),
            ):
                con.execute(
                    "INSERT INTO app_settings(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
        con.commit()
    admin_state.pop(user_id, None)
    if animation_id:
        await message.answer(
            "✅ Файл принят и сохранён как анимация для /start. Отправь /start для проверки. "
            "Кнопка «🎁 ЗАБРАТЬ БОНУСЫ 🎁» и ссылка Mini App не изменены."
        )
    else:
        await message.answer(
            "⚠️ Не удалось преобразовать файл в анимацию. Файл не потерян, но для автоматического "
            "движения нужен GIF или MP4, который Telegram принимает как анимацию."
        )


@dp.message(
    lambda message: admin_state.get(message.from_user.id, {}).get("action") == "set_welcome_media",
)
async def welcome_media_wrong_type(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "Я жду GIF, видео или документ с GIF-файлом. Пришли его сюда ещё раз — теперь бот попробует "
        "принять документ и преобразовать его в анимацию."
    )


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
    offer_id = parse_callback_id(call.data, 1)
    if not offer_id:
        await call.answer("Кнопка устарела. Откройте меню заново.", show_alert=True)
        return
    with closing(db()) as con:
        row = con.execute("""
        SELECT category,name,description,bonus,url,geo
        FROM offers
        WHERE id=? AND enabled=1
        """, (offer_id,)).fetchone()
        if row:
            # This records an offer-card opening. Telegram does not tell the bot
            # when the external URL button itself is clicked.
            log_click(con, user_id=call.from_user.id, offer_id=offer_id,
                      source="telegram_bot", click_type="offer_card",
                      partner=row[1], target_url=row[4])
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
    base_url = WEBAPP_URL.rsplit("/app", 1)[0].rstrip("/") if WEBAPP_URL else ""
    tracked_url = f"{base_url}/go/{offer_id}" if base_url else url
    kb.button(text="🎁 ПОЛУЧИТЬ ПРЕДЛОЖЕНИЕ", url=tracked_url)
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
        SELECT id,category,name,enabled,position,geo,COALESCE(hidden,0)
        FROM offers ORDER BY category,position,id
        """).fetchall()
    if not rows:
        text = "📋 Офферов пока нет."
    else:
        text = "📋 ОФФЕРЫ\n\n"
        for oid, cat, name, enabled, pos, geo, hidden in rows:
            mark = "🙈" if hidden else ("🟢" if enabled else "🔴")
            text += f"{mark} #{oid} {name} | {cat} | pos {pos}" + (" | СКРЫТ" if hidden else "")
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
    oid = parse_callback_id(call.data, 2)
    if not oid:
        await call.answer("Кнопка устарела. Откройте меню заново.", show_alert=True)
        return
    with closing(db()) as con:
        row = con.execute("""
        SELECT id,category,name,description,bonus,url,enabled,position,geo
        FROM offers WHERE id=?
        """, (oid,)).fetchone()
        clicks = con.execute(
            "SELECT COUNT(*) FROM clicks WHERE offer_id=? AND click_id IS NOT NULL AND COALESCE(click_type,'offer')!='offer_card'",
            (oid,)
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
        f"Отслеживаемых переходов по ссылке: {clicks}\n\n"
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
    oid = parse_callback_id(call.data, 2)
    if not oid:
        await call.answer("Кнопка устарела. Откройте меню заново.", show_alert=True)
        return
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
    oid = parse_callback_id(call.data, 2)
    if not oid:
        await call.answer("Кнопка устарела. Откройте меню заново.", show_alert=True)
        return
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
    oid = parse_callback_id(call.data, 2)
    if not oid:
        await call.answer("Кнопка устарела. Откройте меню заново.", show_alert=True)
        return
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




@dp.callback_query(F.data == "adm:visibility")
async def adm_visibility(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    with closing(db()) as con:
        rows = con.execute("SELECT id,name,enabled,COALESCE(hidden,0) FROM offers ORDER BY category,position,id").fetchall()
    kb = InlineKeyboardBuilder()
    for oid,name,enabled,hidden in rows:
        mark = "🙈" if hidden else ("🟢" if enabled else "🔴")
        action = "Восстановить" if hidden else "Скрыть"
        kb.button(text=f"{mark} {action}: {name}"[:64], callback_data=f"adm:visibilityone:{oid}")
    kb.button(text="⬅️ Админ-панель", callback_data="adm:home")
    kb.adjust(1)
    await call.message.edit_text("🙈 СКРЫТИЕ / ВОССТАНОВЛЕНИЕ\n\nСкрытый оффер не показывается в Mini App. Данные, ссылка, промокод и логотип сохраняются.", reply_markup=kb.as_markup())
    await call.answer()


@dp.callback_query(F.data.startswith("adm:visibilityone:"))
async def adm_visibilityone(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    oid = parse_callback_id(call.data, 2)
    if not oid:
        await call.answer("Кнопка устарела. Откройте меню заново.", show_alert=True)
        return
    with closing(db()) as con:
        row = con.execute("SELECT name,COALESCE(hidden,0) FROM offers WHERE id=?", (oid,)).fetchone()
        if row:
            new_hidden = 0 if row[1] else 1
            con.execute("UPDATE offers SET hidden=? WHERE id=?", (new_hidden, oid))
            con.commit()
    if not row:
        await call.answer("Оффер не найден", show_alert=True)
        return
    await call.answer(f"{row[0]}: {'скрыт' if new_hidden else 'восстановлен'}", show_alert=True)
    await adm_visibility(call)


@dp.callback_query(F.data == "adm:delete")
async def adm_delete(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    await call.message.edit_text("🗑 Выбери оффер для окончательного удаления:", reply_markup=admin_offer_keyboard("adm:deleteone"))
    await call.answer()


@dp.callback_query(F.data.startswith("adm:deleteone:"))
async def adm_deleteone(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    oid = parse_callback_id(call.data, 2)
    if not oid:
        await call.answer("Кнопка устарела. Откройте меню заново.", show_alert=True)
        return
    with closing(db()) as con:
        row = con.execute("SELECT name FROM offers WHERE id=?", (oid,)).fetchone()
    if not row:
        await call.answer("Оффер не найден", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Да, удалить навсегда", callback_data=f"adm:deleteconfirm:{oid}")
    kb.button(text="↩️ Отмена", callback_data="adm:delete")
    kb.adjust(1)
    await call.message.edit_text(f"⚠️ Удалить оффер «{row[0]}» навсегда?\n\nБудут удалены карточка, партнёрская ссылка, промокод и логотип. Это действие нельзя отменить.", reply_markup=kb.as_markup())
    await call.answer()


@dp.callback_query(F.data.startswith("adm:deleteconfirm:"))
async def adm_deleteconfirm(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    oid = parse_callback_id(call.data, 2)
    if not oid:
        await call.answer("Кнопка устарела. Откройте меню заново.", show_alert=True)
        return
    with closing(db()) as con:
        row = con.execute("SELECT name FROM offers WHERE id=?", (oid,)).fetchone()
        if row:
            con.execute("DELETE FROM clicks WHERE offer_id=?", (oid,))
            con.execute("DELETE FROM offers WHERE id=?", (oid,))
            con.commit()
    if not row:
        await call.answer("Оффер уже удалён или не найден", show_alert=True)
        return
    await call.answer(f"Удалён: {row[0]}", show_alert=True)
    await call.message.edit_text(f"🗑 Оффер «{row[0]}» удалён.", reply_markup=admin_keyboard())


@dp.callback_query(F.data == "adm:users")
async def adm_users(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    with closing(db()) as con:
        total = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        today = con.execute("SELECT COUNT(*) FROM users WHERE date(created_at)=date('now')").fetchone()[0]
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔎 Поиск по @username или ID", callback_data="adm:user_search")],[InlineKeyboardButton(text="📋 Последние пользователи", callback_data="adm:user_recent")],[InlineKeyboardButton(text="🚫 Чёрный список", callback_data="adm:blocked_list")],[InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="adm:home")]])
    await call.message.edit_text(f"👥 ПОЛЬЗОВАТЕЛИ\\n\\nВсего уникальных: {total}\\nНовых сегодня (UTC): {today}\\n\\nПолная блокировка по Telegram ID.", reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data == "adm:user_search")
async def adm_user_search(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    admin_state[call.from_user.id] = {"action": "user_search_id"}
    await call.message.edit_text("🔎 Введи Telegram ID или username, например @Alexandra_iGamePanda. Можно попробовать и имя, если оно есть в базе бота.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ К пользователям", callback_data="adm:users")]]))
    await call.answer()


@dp.callback_query(F.data == "adm:blocked_list")
async def adm_blocked_list(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    with closing(db()) as con:
        rows = con.execute("SELECT user_id,username,first_name FROM blocked_users ORDER BY blocked_at DESC LIMIT 50").fetchall()
    kb = InlineKeyboardBuilder()
    for uid, username, first_name in rows:
        kb.button(text=f"🔓 {first_name or username or uid} · {uid}"[:64], callback_data=f"adm:unblock:{uid}")
    kb.button(text="⬅️ К пользователям", callback_data="adm:users")
    kb.adjust(1)
    await call.message.edit_text("🚫 ЧЁРНЫЙ СПИСОК\\n\\nВыбери пользователя, чтобы восстановить доступ.", reply_markup=kb.as_markup())
    await call.answer()


@dp.callback_query(F.data.startswith("adm:unblock:"))
async def adm_unblock(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    uid = parse_callback_id(call.data, from_right=True)
    if not uid:
        await call.answer("Кнопка устарела. Откройте меню заново.", show_alert=True)
        return
    with closing(db()) as con:
        con.execute("DELETE FROM blocked_users WHERE user_id=?", (uid,))
        con.commit()
    await call.answer(f"Доступ восстановлен: {uid}", show_alert=True)
    await adm_blocked_list(call)


@dp.callback_query(F.data.startswith("adm:blockconfirm:"))
async def adm_block_confirm(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    uid = parse_callback_id(call.data, from_right=True)
    if not uid:
        await call.answer("Кнопка устарела. Откройте меню заново.", show_alert=True)
        return
    if is_admin(uid):
        await call.answer("Нельзя блокировать администратора.", show_alert=True)
        return
    with closing(db()) as con:
        row = con.execute("SELECT username,first_name FROM users WHERE user_id=?", (uid,)).fetchone()
        con.execute("INSERT INTO blocked_users(user_id,username,first_name,reason) VALUES(?,?,?,'admin') ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,first_name=excluded.first_name,reason='admin',blocked_at=CURRENT_TIMESTAMP", (uid, (row[0] or "") if row else "", (row[1] or "") if row else ""))
        con.commit()
    await call.answer("Пользователь заблокирован", show_alert=True)
    await call.message.edit_text(f"⛔ Telegram ID {uid} заблокирован в боте и Mini App.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚫 Чёрный список", callback_data="adm:blocked_list")],[InlineKeyboardButton(text="⬅️ К пользователям", callback_data="adm:users")]]))


@dp.callback_query(F.data.startswith("adm:block:"))
async def adm_block_preview(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    uid = parse_callback_id(call.data, from_right=True)
    if not uid:
        await call.answer("Кнопка устарела. Откройте меню заново.", show_alert=True)
        return
    if is_admin(uid):
        await call.answer("Нельзя блокировать администратора.", show_alert=True)
        return
    with closing(db()) as con:
        row = con.execute("SELECT username,first_name FROM users WHERE user_id=?", (uid,)).fetchone()
        already = con.execute("SELECT 1 FROM blocked_users WHERE user_id=?", (uid,)).fetchone()
    if not row:
        await call.answer("ID не найден. Пользователь должен хотя бы раз запустить бота.", show_alert=True)
        return
    if already:
        await call.answer("Уже заблокирован.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⛔ Да, заблокировать", callback_data=f"adm:blockconfirm:{uid}")],[InlineKeyboardButton(text="↩️ Отмена", callback_data="adm:users")]])
    await call.message.edit_text(f"Заблокировать Telegram ID {uid}?\\n\\nИмя: {row[1] or '—'}\\nUsername: @{row[0] or '—'}\\n\\nДоступ к боту и Mini App будет запрещён.", reply_markup=kb)
    await call.answer()


@dp.message(lambda message: admin_state.get(message.from_user.id, {}).get("action") == "user_search_id")
async def adm_user_search_result(message: Message):
    value = (message.text or "").strip()
    if not value:
        await message.answer("Введи @username или Telegram ID.")
        return
    with closing(db()) as con:
        if value.isdigit():
            row = con.execute("SELECT user_id,username,first_name,created_at,source FROM users WHERE user_id=?", (int(value),)).fetchone()
        else:
            username = value.lstrip("@").strip()
            row = con.execute("SELECT user_id,username,first_name,created_at,source FROM users WHERE LOWER(username)=LOWER(?)", (username,)).fetchone()
            if row is None:
                row = con.execute("SELECT user_id,username,first_name,created_at,source FROM users WHERE LOWER(first_name)=LOWER(?) ORDER BY created_at DESC LIMIT 1", (username,)).fetchone()
        blocked = con.execute("SELECT 1 FROM blocked_users WHERE user_id=?", (row[0],)).fetchone() if row else None
    admin_state.pop(message.from_user.id, None)
    if not row:
        await message.answer("Не нашёл такого пользователя в базе BONUS UP. Поиск работает по ID или @username, если человек хотя бы раз запускал бота. Попробуй список последних пользователей.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📋 Последние пользователи", callback_data="adm:user_recent")],[InlineKeyboardButton(text="🔎 Новый поиск", callback_data="adm:user_search")],[InlineKeyboardButton(text="⬅️ К пользователям", callback_data="adm:users")]]))
        return
    uid, username, first_name, created_at, source = row
    if is_admin(uid):
        await message.answer("Это администратор — блокировка запрещена.", reply_markup=admin_keyboard())
        return
    action = f"adm:unblock:{uid}" if blocked else f"adm:block:{uid}"
    label = "🔓 Разблокировать" if blocked else "⛔ Заблокировать"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, callback_data=action)],[InlineKeyboardButton(text="🔎 Новый поиск", callback_data="adm:user_search")],[InlineKeyboardButton(text="🚫 Чёрный список", callback_data="adm:blocked_list")],[InlineKeyboardButton(text="⬅️ К пользователям", callback_data="adm:users")]])
    await message.answer(f"👤 ПОЛЬЗОВАТЕЛЬ\\n\\nID: {uid}\\nИмя: {first_name or '—'}\\nUsername: @{username or '—'}\\nЗарегистрирован: {created_at}\\nИсточник: {source or '—'}\\nСтатус: {'⛔ ЗАБЛОКИРОВАН' if blocked else '🟢 Активен'}", reply_markup=kb)


@dp.callback_query(F.data == "adm:user_recent")
async def adm_user_recent(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    with closing(db()) as con:
        rows = con.execute("SELECT user_id,username,first_name FROM users ORDER BY created_at DESC LIMIT 40").fetchall()
    kb = InlineKeyboardBuilder()
    for uid, username, first_name in rows:
        if is_admin(uid):
            continue
        label = f"{first_name or 'Без имени'} @{username}" if username else (first_name or f"ID {uid}")
        kb.button(text=label[:64], callback_data=f"adm:useropen:{uid}")
    kb.button(text="🔎 Поиск по @username или ID", callback_data="adm:user_search")
    kb.button(text="⬅️ К пользователям", callback_data="adm:users")
    kb.adjust(1)
    await call.message.edit_text("📋 ПОСЛЕДНИЕ ПОЛЬЗОВАТЕЛИ\\n\\nВыбери человека, чтобы открыть карточку и при необходимости заблокировать.", reply_markup=kb.as_markup())
    await call.answer()


@dp.callback_query(F.data.startswith("adm:useropen:"))
async def adm_user_open(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    uid = parse_callback_id(call.data, from_right=True)
    if not uid:
        await call.answer("Кнопка устарела. Откройте меню заново.", show_alert=True)
        return
    with closing(db()) as con:
        row = con.execute("SELECT user_id,username,first_name,created_at,source FROM users WHERE user_id=?", (uid,)).fetchone()
        blocked = con.execute("SELECT 1 FROM blocked_users WHERE user_id=?", (uid,)).fetchone()
    if not row:
        await call.answer("Пользователь не найден.", show_alert=True)
        return
    uid, username, first_name, created_at, source = row
    action = f"adm:unblock:{uid}" if blocked else f"adm:block:{uid}"
    label = "🔓 Разблокировать" if blocked else "⛔ Заблокировать"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, callback_data=action)],[InlineKeyboardButton(text="🔎 Новый поиск", callback_data="adm:user_search")],[InlineKeyboardButton(text="📋 Последние пользователи", callback_data="adm:user_recent")],[InlineKeyboardButton(text="⬅️ К пользователям", callback_data="adm:users")]])
    await call.message.edit_text(f"👤 ПОЛЬЗОВАТЕЛЬ\\n\\nID: {uid}\\nИмя: {first_name or '—'}\\nUsername: @{username or '—'}\\nЗарегистрирован: {created_at}\\nИсточник: {source or '—'}\\nСтатус: {'⛔ ЗАБЛОКИРОВАН' if blocked else '🟢 Активен'}", reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data == "adm:text_broadcast")
async def adm_text_broadcast(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    admin_state[call.from_user.id] = {"action": "text_broadcast", "step": "body"}
    await call.message.answer(
        "✍️ РАЗОВАЯ ТЕКСТОВАЯ РАССЫЛКА\n\n"
        "Отправь рекламный текст, который нужно прямо сейчас разослать пользователям бота. "
        "Это отдельное сообщение — оно не будет вставляться между баннером и кнопкой.\n\n"
        "Для отмены напиши «отмена»."
    )
    await call.answer()


@dp.callback_query(F.data.startswith("adm:text_broadcast_send:"))
async def adm_text_broadcast_send(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    token = call.data.rsplit(":", 1)[1]
    state = admin_state.get(call.from_user.id, {})
    if state.get("action") != "text_broadcast_confirm" or state.get("token") != token:
        await call.answer("Рассылка уже отменена или устарела.", show_alert=True)
        return
    body = state.get("body", "")
    if not body:
        await call.answer("Текст не найден.", show_alert=True)
        return
    with closing(db()) as con:
        user_rows = con.execute("SELECT user_id FROM users").fetchall()
    await call.answer("Начинаю рассылку…")
    await call.message.edit_text("📤 Рассылаю сообщение подписчикам…")
    delivered = 0
    failed = 0
    for (target_id,) in user_rows:
        try:
            await call.bot.send_message(int(target_id), body[:4096])
            delivered += 1
        except Exception as exc:
            failed += 1
            print(f"[manual-text-broadcast] user_id={target_id} error={type(exc).__name__}: {exc}", flush=True)
        await asyncio.sleep(0.05)
    admin_state.pop(call.from_user.id, None)
    await call.message.answer(
        f"✅ Текстовая рассылка завершена.\n\n"
        f"Доставлено: {delivered}\n"
        f"Не доставлено: {failed}",
        reply_markup=admin_keyboard()
    )


@dp.callback_query(F.data == "adm:text_broadcast_cancel")
async def adm_text_broadcast_cancel(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    admin_state.pop(call.from_user.id, None)
    await call.message.edit_text("❌ Текстовая рассылка отменена.", reply_markup=admin_keyboard())
    await call.answer()


@dp.callback_query(F.data == "adm:campaigns")
async def adm_campaigns(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    with closing(db()) as con:
        rows = con.execute("SELECT id,title,enabled,interval_hours,sent_count,click_count,COALESCE(placement,'broadcast') FROM campaigns ORDER BY position,id").fetchall()
    kb = InlineKeyboardBuilder()
    lines = ["📢 РЕКЛАМНЫЕ ПУБЛИКАЦИИ", "", "Создавай рекламные карточки с фото/GIF, отдельным текстом и кнопкой с партнёрской ссылкой. Можно отправлять вручную всем подписчикам или включить рассылку по расписанию."]
    for cid,title,enabled,interval,sent,clicks,placement in rows:
        if placement == "start":
            where = "старый режим /start"
        elif placement == "manual":
            where = "ручная публикация"
        else:
            where = f"по расписанию: каждые {interval} ч."
        lines.append(f"{'🟢' if enabled else '🔴'} #{cid} {title or 'Без названия'} — {where} | отправок {sent} | кликов {clicks}")
        kb.button(text=f"📣 Отправить сейчас #{cid}", callback_data=f"adm:camp_send:{cid}")
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
    await call.message.edit_text("📢 НОВАЯ ПУБЛИКАЦИЯ\n\n1/7. Название для админки:")
    await call.answer()


@dp.callback_query(F.data.startswith("adm:camp_toggle:"))
async def adm_camp_toggle(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    cid = parse_callback_id(call.data, from_right=True)
    if not cid:
        await call.answer("Кнопка устарела. Откройте меню заново.", show_alert=True)
        return
    with closing(db()) as con:
        row = con.execute("SELECT enabled,COALESCE(placement,'broadcast') FROM campaigns WHERE id=?", (cid,)).fetchone()
        if not row:
            await call.answer("Публикация не найдена", show_alert=True)
            return
        if int(row[0] or 0):
            con.execute("UPDATE campaigns SET enabled=0 WHERE id=?", (cid,))
            notice = "Баннер выключен" if row[1] == "start" else "Публикация выключена"
        else:
            # Reset the schedule only for scheduled broadcasts. Start-placement
            # banners are sent when a user invokes /start and need no timer.
            if row[1] == "start":
                con.execute("UPDATE campaigns SET enabled=1 WHERE id=?", (cid,))
                notice = "Баннер включён: будет показываться после /start"
            else:
                con.execute(
                    "UPDATE campaigns SET enabled=1, created_at=CURRENT_TIMESTAMP, last_sent_at='' WHERE id=?",
                    (cid,)
                )
                notice = "Публикация включена; первая отправка ожидается в ближайшую минуту"
        con.commit()
    await call.answer(notice)
    await adm_campaigns(call)


@dp.callback_query(F.data.startswith("adm:camp_delete:"))
async def adm_camp_delete(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    cid = parse_callback_id(call.data, from_right=True)
    if not cid:
        await call.answer("Кнопка устарела. Откройте меню заново.", show_alert=True)
        return
    with closing(db()) as con:
        sent_rows = con.execute(
            "SELECT user_id,message_id FROM campaign_delivery_messages WHERE campaign_id=?",
            (cid,)
        ).fetchall()
        legacy_rows = con.execute(
            "SELECT user_id,message_id FROM campaign_sends WHERE campaign_id=? AND message_id IS NOT NULL",
            (cid,)
        ).fetchall()
    # Include legacy tracked messages, avoiding duplicate attempts.
    sent_rows = list(dict.fromkeys(sent_rows + legacy_rows))
    deleted_messages = 0
    for target_id, message_id in sent_rows:
        try:
            await call.bot.delete_message(chat_id=int(target_id), message_id=int(message_id))
            deleted_messages += 1
        except Exception as exc:
            print(
                f"[campaign-delete] campaign_id={cid} user_id={target_id} "
                f"message_id={message_id} error={type(exc).__name__}: {exc}",
                flush=True
            )
        await asyncio.sleep(0.03)
    with closing(db()) as con:
        con.execute("DELETE FROM campaign_delivery_messages WHERE campaign_id=?", (cid,))
        con.execute("DELETE FROM campaign_sends WHERE campaign_id=?", (cid,))
        con.execute("DELETE FROM campaigns WHERE id=?", (cid,))
        con.commit()
    await call.answer(f"Публикация удалена. Удалено сообщений: {deleted_messages}", show_alert=True)
    await adm_campaigns(call)


@dp.callback_query(F.data.startswith("adm:camp_send:"))
async def adm_camp_send(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    cid = parse_callback_id(call.data, from_right=True)
    if not cid:
        await call.answer("Кнопка устарела. Откройте меню заново.", show_alert=True)
        return
    with closing(db()) as con:
        campaign = con.execute(
            "SELECT title,body,media_type,media_file_id,button_text,button_url FROM campaigns WHERE id=?",
            (cid,)
        ).fetchone()
        users = con.execute("SELECT user_id FROM users").fetchall()
    if not campaign:
        await call.answer("Публикация не найдена.", show_alert=True)
        return
    title, body, media_type, file_id, button_text, button_url = campaign
    if not body and not file_id:
        await call.answer("В публикации нет ни текста, ни медиа.", show_alert=True)
        return
    markup = None
    if button_text and button_url:
        base_url = WEBAPP_URL.rsplit("/app", 1)[0].rstrip("/") if WEBAPP_URL else ""
        tracked_url = f"{base_url}/go/campaign/{cid}" if base_url else button_url
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=button_text, url=tracked_url)]
        ])
    await call.answer("Начинаю рассылку…")
    await call.message.edit_text(f"📤 Отправляю «{title or 'Без названия'}» всем подписчикам…")
    delivered = 0
    failed = 0
    for (target_id,) in users:
        try:
            if media_type == "photo" and file_id:
                sent = await call.bot.send_photo(int(target_id), file_id, caption=(body or "")[:1024] or None, reply_markup=markup)
            elif media_type == "animation" and file_id:
                sent = await call.bot.send_animation(int(target_id), file_id, caption=(body or "")[:1024] or None, reply_markup=markup)
            elif media_type == "video" and file_id:
                sent = await call.bot.send_video(int(target_id), file_id, caption=(body or "")[:1024] or None, reply_markup=markup)
            else:
                sent = await call.bot.send_message(int(target_id), (body or title or "BONUS UP")[:4096], reply_markup=markup)
            delivered += 1
            with closing(db()) as con:
                con.execute(
                    "INSERT INTO campaign_delivery_messages(campaign_id,user_id,message_id,sent_at) VALUES(?,?,?,CURRENT_TIMESTAMP)",
                    (cid, int(target_id), sent.message_id)
                )
                con.execute(
                    "INSERT OR REPLACE INTO campaign_sends(campaign_id,user_id,message_id,sent_at) VALUES(?,?,?,CURRENT_TIMESTAMP)",
                    (cid, int(target_id), sent.message_id)
                )
                con.commit()
        except Exception as exc:
            failed += 1
            print(f"[manual-campaign-broadcast] campaign_id={cid} user_id={target_id} error={type(exc).__name__}: {exc}", flush=True)
        await asyncio.sleep(0.05)
    with closing(db()) as con:
        con.execute("UPDATE campaigns SET sent_count=sent_count+? WHERE id=?", (delivered, cid))
        con.commit()
    await call.message.answer(
        f"✅ Публикация отправлена.\n\nПолучателей в базе: {len(users)}\nДоставлено: {delivered}\nНе доставлено: {failed}",
        reply_markup=admin_keyboard()
    )


@dp.callback_query(F.data.startswith("adm:analytics"))
async def adm_analytics(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    period = call.data.split(":", 2)[2] if call.data.count(":") >= 2 else "all"
    periods = {"today": ("Сегодня", "-0 days"), "7d": ("7 дней", "-6 days"), "30d": ("30 дней", "-29 days"), "all": ("Всё время", None)}
    if period not in periods:
        period = "all"
    period_label, offset = periods[period]
    date_filter = " AND date(created_at) >= date('now', ?)" if offset else ""
    click_filter = " AND date(c.created_at) >= date('now', ?)" if offset else ""
    params = (offset,) if offset else ()
    with closing(db()) as con:
        total_users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        period_users = con.execute("SELECT COUNT(*) FROM users WHERE 1=1" + date_filter, params).fetchone()[0]
        click_count = con.execute("SELECT COUNT(*) FROM clicks WHERE click_id IS NOT NULL AND TRIM(click_id)!='' AND COALESCE(click_type,'offer')!='offer_card'" + date_filter, params).fetchone()[0]
        unique_users = con.execute("SELECT COUNT(DISTINCT user_id) FROM clicks WHERE user_id IS NOT NULL AND click_id IS NOT NULL AND TRIM(click_id)!='' AND COALESCE(click_type,'offer')!='offer_card'" + date_filter, params).fetchone()[0]
        card_opens = con.execute("SELECT COUNT(*) FROM clicks WHERE click_type='offer_card'" + date_filter, params).fetchone()[0]
        offer_rows = con.execute("""
            SELECT o.name,
                   SUM(CASE WHEN c.click_id IS NOT NULL AND COALESCE(c.click_type,'offer')!='offer_card' THEN 1 ELSE 0 END),
                   COUNT(DISTINCT CASE WHEN c.click_id IS NOT NULL AND COALESCE(c.click_type,'offer')!='offer_card' THEN c.user_id END)
            FROM offers o LEFT JOIN clicks c ON c.offer_id=o.id""" + click_filter + """
            GROUP BY o.id ORDER BY 2 DESC, o.position, o.id
        """, params).fetchall()
        partner_rows = con.execute("""
            SELECT COALESCE(NULLIF(partner,''),'Без партнёра'), currency, COUNT(*), COALESCE(SUM(amount),0)
            FROM conversion_events WHERE 1=1""" + date_filter + """
            GROUP BY partner,currency ORDER BY partner,currency
        """, params).fetchall()
        event_rows = con.execute("""
            SELECT partner,event_type,COUNT(*),COALESCE(SUM(amount),0),currency
            FROM conversion_events WHERE 1=1""" + date_filter + """
            GROUP BY partner,event_type,currency ORDER BY partner,event_type,currency
        """, params).fetchall()
        revenue_rows = con.execute("""
            SELECT partner,currency,COALESCE(SUM(amount),0),COUNT(*)
            FROM revenue_entries WHERE 1=1""" + date_filter + """
            GROUP BY partner,currency ORDER BY partner,currency
        """, params).fetchall()
        campaign_rows = con.execute("SELECT title,sent_count,click_count FROM campaigns ORDER BY position,id").fetchall()
        source_rows = con.execute("""
            SELECT COALESCE(NULLIF(source,''),'direct'),COUNT(*) FROM clicks
            WHERE click_id IS NOT NULL AND TRIM(click_id)!='' AND COALESCE(click_type,'offer')!='offer_card'""" + date_filter + """
            GROUP BY COALESCE(NULLIF(source,''),'direct') ORDER BY COUNT(*) DESC LIMIT 8
        """, params).fetchall()
    lines = [
        f"📈 АНАЛИТИКА ПАРТНЁРОК — {period_label}", "",
        f"👥 Всего пользователей в базе: {total_users}",
        f"🆕 Новых пользователей за период: {period_users}",
        f"🔗 Переходов по партнёрским ссылкам: {click_count}",
        f"👤 Пользователей с отслеживаемыми переходами: {unique_users}",
        f"👁 Открытий карточек офферов: {card_opens}", "",
        "🎁 ПЕРЕХОДЫ ПО ОФФЕРАМ:"
    ]
    lines.extend([f"• {name}: {count or 0} кликов; пользователей: {users or 0}" for name,count,users in offer_rows] or ["Пока нет данных по офферам."])
    lines += ["", "🧭 ИСТОЧНИКИ ПЕРЕХОДОВ:"]
    lines.extend([f"• {source}: {count}" for source,count in source_rows] or ["Источники пока не отмечены."])
    lines += ["", "📥 КОНВЕРСИИ ОТ ПАРТНЁРОВ:"]
    lines.extend([f"• {partner} — {event}: {count}; сумма {amount:.2f} {currency}" for partner,event,count,amount,currency in event_rows] or ["Пока нет подтверждённых postback-конверсий."])
    lines += ["", "💰 ДОХОД, ЗАНЕСЁННЫЙ ВРУЧНУЮ:"]
    lines.extend([f"• {partner}: {amount:.2f} {currency} ({count} запис.)" for partner,currency,amount,count in revenue_rows] or ["Пока доход не добавлен."])
    lines += ["", "📢 РЕКЛАМНЫЕ ПУБЛИКАЦИИ:"]
    lines.extend([f"• {title or 'Без названия'}: отправок {sent}, нажатий {clicks}" for title,sent,clicks in campaign_rows] or ["Публикаций пока нет."])
    kb = InlineKeyboardBuilder()
    kb.button(text="📅 Сегодня", callback_data="adm:analytics:today")
    kb.button(text="📅 7 дней", callback_data="adm:analytics:7d")
    kb.button(text="📅 30 дней", callback_data="adm:analytics:30d")
    kb.button(text="📊 Всё время", callback_data="adm:analytics:all")
    kb.button(text="➕ Добавить доход", callback_data="adm:revenue_add")
    kb.button(text="📒 История доходов", callback_data="adm:revenue_history")
    kb.button(text="⬅️ Админ-панель", callback_data="adm:home")
    kb.adjust(2, 2, 1, 1, 1)
    try:
        await call.message.edit_text("\n".join(lines), reply_markup=kb.as_markup())
    except Exception as exc:
        # Telegram returns this when the same analytics period is tapped twice.
        if "message is not modified" not in str(exc).lower():
            raise
    await call.answer()


@dp.callback_query(F.data == "adm:revenue_add")
async def adm_revenue_add(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    admin_state[call.from_user.id] = {"action":"revenue_add","step":"partner","data":{}}
    await call.message.edit_text("💰 ДОБАВИТЬ ДОХОД\n\n1/5. Название партнёрской программы (например, 1xPartners):\nДля отмены напиши «отмена».")
    await call.answer()


@dp.callback_query(F.data == "adm:revenue_history")
async def adm_revenue_history(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    with closing(db()) as con:
        rows = con.execute("SELECT id,partner,amount,currency,period,note,created_at FROM revenue_entries ORDER BY id DESC LIMIT 20").fetchall()
    lines = ["📒 ПОСЛЕДНИЕ ЗАПИСИ ДОХОДА", ""]
    for rid,partner,amount,currency,period,note,created in rows:
        lines.append(f"#{rid} {partner}: {amount:.2f} {currency}" + (f" | период: {period}" if period else "") + (f" | {note}" if note else "") + f"\n  {created}")
    if not rows:
        lines.append("Записей пока нет.")
    kb = InlineKeyboardBuilder()
    for rid,partner,amount,currency,period,note,created in rows:
        kb.button(text=f"🗑 Удалить #{rid} · {partner[:18]}", callback_data=f"adm:revenue_del_confirm:{rid}")
    kb.button(text="➕ Добавить доход", callback_data="adm:revenue_add")
    kb.button(text="⬅️ К аналитике", callback_data="adm:analytics")
    kb.adjust(1)
    await call.message.edit_text("\n".join(lines), reply_markup=kb.as_markup())
    await call.answer()


@dp.callback_query(F.data.startswith("adm:revenue_del_confirm:"))
async def adm_revenue_del_confirm(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    rid = parse_callback_id(call.data, from_right=True)
    if not rid:
        await call.answer("Кнопка устарела. Откройте меню заново.", show_alert=True)
        return
    with closing(db()) as con:
        row = con.execute("SELECT partner,amount,currency FROM revenue_entries WHERE id=?", (rid,)).fetchone()
    if not row:
        await call.answer("Запись уже удалена или не найдена.", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Да, удалить", callback_data=f"adm:revenue_del_yes:{rid}")
    kb.button(text="↩️ Отмена", callback_data="adm:revenue_history")
    kb.adjust(1)
    await call.message.edit_text(
        f"Удалить запись дохода?\n\n#{rid} {row[0]} — {row[1]:.2f} {row[2]}\n\nЭто действие нельзя отменить.",
        reply_markup=kb.as_markup()
    )
    await call.answer()


@dp.callback_query(F.data.startswith("adm:revenue_del_yes:"))
async def adm_revenue_del_yes(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    rid = parse_callback_id(call.data, from_right=True)
    if not rid:
        await call.answer("Кнопка устарела. Откройте меню заново.", show_alert=True)
        return
    with closing(db()) as con:
        cur = con.execute("DELETE FROM revenue_entries WHERE id=?", (rid,))
        con.commit()
        deleted = cur.rowcount
    await call.answer("Запись удалена." if deleted else "Запись не найдена.", show_alert=True)
    with closing(db()) as con:
        rows = con.execute("SELECT id,partner,amount,currency,period,note,created_at FROM revenue_entries ORDER BY id DESC LIMIT 20").fetchall()
    lines = ["📒 ПОСЛЕДНИЕ ЗАПИСИ ДОХОДА", ""]
    for rid,partner,amount,currency,period,note,created in rows:
        lines.append(f"#{rid} {partner}: {amount:.2f} {currency}" + (f" | период: {period}" if period else "") + (f" | {note}" if note else "") + f"\n  {created}")
    if not rows:
        lines.append("Записей пока нет.")
    kb = InlineKeyboardBuilder()
    for rid,partner,amount,currency,period,note,created in rows:
        kb.button(text=f"🗑 Удалить #{rid} · {partner[:18]}", callback_data=f"adm:revenue_del_confirm:{rid}")
    kb.button(text="➕ Добавить доход", callback_data="adm:revenue_add")
    kb.button(text="⬅️ К аналитике", callback_data="adm:analytics")
    kb.adjust(1)
    await call.message.edit_text("\n".join(lines), reply_markup=kb.as_markup())


@dp.callback_query(F.data == "adm:dbdiag")
async def adm_dbdiag(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    try:
        with closing(db()) as con:
            click_cols = {row[1] for row in con.execute("PRAGMA table_info(clicks)").fetchall()}
            if "created_at" not in click_cols:
                await call.message.answer("Диагностика: в таблице clicks отсутствует поле created_at.")
                await call.answer()
                return
            total, with_id, without_id, earliest, latest = con.execute("""
                SELECT COUNT(*),
                       SUM(CASE WHEN click_id IS NOT NULL AND TRIM(click_id)!='' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN click_id IS NULL OR TRIM(click_id)='' THEN 1 ELSE 0 END),
                       MIN(created_at), MAX(created_at)
                FROM clicks
            """).fetchone()
            today = con.execute("SELECT COUNT(*) FROM clicks WHERE date(created_at)=date('now')").fetchone()[0]
            last7 = con.execute("SELECT COUNT(*) FROM clicks WHERE date(created_at)>=date('now','-6 days')").fetchone()[0]
            last30 = con.execute("SELECT COUNT(*) FROM clicks WHERE date(created_at)>=date('now','-29 days')").fetchone()[0]
            tracked_today = con.execute("SELECT COUNT(*) FROM clicks WHERE click_id IS NOT NULL AND TRIM(click_id)!='' AND date(created_at)=date('now') AND COALESCE(click_type,'offer')!='offer_card'").fetchone()[0]
            tracked7 = con.execute("SELECT COUNT(*) FROM clicks WHERE click_id IS NOT NULL AND TRIM(click_id)!='' AND date(created_at)>=date('now','-6 days') AND COALESCE(click_type,'offer')!='offer_card'").fetchone()[0]
            tracked30 = con.execute("SELECT COUNT(*) FROM clicks WHERE click_id IS NOT NULL AND TRIM(click_id)!='' AND date(created_at)>=date('now','-29 days') AND COALESCE(click_type,'offer')!='offer_card'").fetchone()[0]
            invalid_dates = con.execute("SELECT COUNT(*) FROM clicks WHERE created_at IS NULL OR date(created_at) IS NULL").fetchone()[0]
            by_day = con.execute("""SELECT date(created_at), COUNT(*) FROM clicks WHERE date(created_at) >= date('now', '-6 days') GROUP BY date(created_at) ORDER BY date(created_at)""").fetchall()
        msg = (
            "🧪 ДИАГНОСТИКА БАЗЫ КЛИКОВ\n\n"
            f"Всего строк clicks: {total or 0}\n"
            f"С click_id: {with_id or 0}\n"
            f"Без click_id: {without_id or 0}\n"
            f"Все записи — сегодня: {today}\n"
            f"Все записи — 7 дней: {last7}\n"
            f"Все записи — 30 дней: {last30}\n\n"
            f"Отслеживаемые переходы — сегодня: {tracked_today}\n"
            f"Отслеживаемые переходы — 7 дней: {tracked7}\n"
            f"Отслеживаемые переходы — 30 дней: {tracked30}\n\n"
            f"Первая запись: {earliest or 'нет'}\n"
            f"Последняя запись: {latest or 'нет'}\n"
            f"Записей с некорректной датой: {invalid_dates}\n\n"
            "Клики по дням (последние 7 дней):\n" + ("\n".join(f"{day}: {count}" for day, count in by_day) or "Нет записей за последние 7 дней") + "\n\n"
            "Временная диагностическая функция. Пользовательские данные и секреты не выводятся."
        )
        await call.message.edit_text(msg, reply_markup=admin_keyboard())
    except Exception as exc:
        print(f"[admin-dbdiag] {type(exc).__name__}: {exc}", flush=True)
        await call.message.answer("Диагностика не выполнена. Ошибка записана в серверный лог.")
    await call.answer()


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
            AND c.click_id IS NOT NULL AND COALESCE(c.click_type,'offer')!='offer_card'
        GROUP BY o.id
        ORDER BY COUNT(c.id) DESC, o.position, o.id
        """).fetchall()
    text = "📊 ПЕРЕХОДЫ ПО ПАРТНЁРСКИМ ССЫЛКАМ\n\n"
    text += "\n".join(f"• {name}: {count}" for name, count in rows) or "Пока нет данных."
    text += "\n\nСчитаются переходы через ссылку-редирект BONUS UP. Открытия карточек без перехода здесь не учитываются."
    await call.message.edit_text(text, reply_markup=admin_keyboard())
    await call.answer()


@dp.callback_query(F.data.startswith("adm:clickone:"))
async def adm_clickone(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    oid = parse_callback_id(call.data, 2)
    if not oid:
        await call.answer("Кнопка устарела. Откройте меню заново.", show_alert=True)
        return
    with closing(db()) as con:
        row = con.execute("""
        SELECT o.name, COUNT(c.id)
        FROM offers o LEFT JOIN clicks c ON c.offer_id=o.id
            AND c.click_id IS NOT NULL AND COALESCE(c.click_type,'offer')!='offer_card'
        WHERE o.id=? GROUP BY o.id
        """, (oid,)).fetchone()
    if row:
        await call.answer(f"{row[0]}: {row[1]} переходов", show_alert=True)
    else:
        await call.answer("Оффер не найден", show_alert=True)


@dp.callback_query(F.data == "adm:promo")
async def adm_promo(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    await call.message.edit_text("🏷 Выбери оффер для настройки промокода или логотипа:", reply_markup=admin_offer_keyboard("adm:promoon", visible_only=True))
    await call.answer()


@dp.callback_query(F.data.startswith("adm:promoon:"))
async def adm_promoon(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    oid = parse_callback_id(call.data, 2)
    if not oid:
        await call.answer("Кнопка устарела. Откройте меню заново.", show_alert=True)
        return
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
    oid = parse_callback_id(call.data, 2)
    if not oid:
        await call.answer("Кнопка устарела. Откройте меню заново.", show_alert=True)
        return
    admin_state[call.from_user.id] = {"action":"promo_code_only","step":"promo_code","offer_id":oid,"data":{}}
    await call.message.edit_text("✏️ Отправь новый промокод. Чтобы удалить существующий, отправь -.")
    await call.answer()


@dp.callback_query(F.data.startswith("adm:offerlogo:"))
async def adm_offerlogo(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    oid = parse_callback_id(call.data, 2)
    if not oid:
        await call.answer("Кнопка устарела. Откройте меню заново.", show_alert=True)
        return
    admin_state[call.from_user.id] = {"action":"logo_only","step":"logo_url","offer_id":oid,"data":{}}
    await call.message.edit_text("🖼 Отправь логотип изображением (можно как фото или как файл JPG/PNG до 1,5 МБ) либо прямую HTTPS-ссылку. Промокод не изменится. Для отмены напиши «отмена».")
    await call.answer()


@dp.callback_query(F.data.startswith("adm:clearlogo:"))
async def adm_clearlogo(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    oid = parse_callback_id(call.data, 2)
    if not oid:
        await call.answer("Кнопка устарела. Откройте меню заново.", show_alert=True)
        return
    with closing(db()) as con:
        con.execute("UPDATE offers SET logo_url='' WHERE id=?", (oid,))
        con.commit()
    await call.message.edit_text(
        "🗑 Логотип удалён. Выбери оффер для дальнейшей настройки:",
        reply_markup=admin_offer_keyboard("adm:promoon", visible_only=True)
    )
    await call.answer("Логотип удалён")


@dp.callback_query(F.data == "adm:dual_links")
async def adm_dual_links(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    await call.message.edit_text(
        "🎮 MINI APP + САЙТ\n\nВыбери оффер, которому нужно задать две отдельные ссылки. "
        "Сначала добавь оффер через «Добавить оффер», затем настрой здесь URL Mini App и URL сайта.",
        reply_markup=admin_offer_keyboard("adm:dualoffer", visible_only=True)
    )
    await call.answer()


@dp.callback_query(F.data.startswith("adm:dualoffer:"))
async def adm_dualoffer(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    oid = parse_callback_id(call.data, 2)
    if not oid:
        await call.answer("Кнопка устарела. Откройте меню заново.", show_alert=True)
        return
    with closing(db()) as con:
        row = con.execute("SELECT name,mini_app_url,site_url FROM offers WHERE id=?", (oid,)).fetchone()
    if not row:
        await call.answer("Оффер не найден", show_alert=True)
        return
    name, mini_url, site_url = row
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ URL Mini App", callback_data=f"adm:dualfield:{oid}:mini")
    kb.button(text="✏️ URL сайта", callback_data=f"adm:dualfield:{oid}:site")
    kb.button(text="⬅️ К списку офферов", callback_data="adm:dual_links")
    kb.adjust(1)
    await call.message.edit_text(
        f"🎮 {name}\n\nURL Mini App: {mini_url or 'не задан'}\nURL сайта: {site_url or 'не задан'}\n\n"
        "Обе ссылки должны принадлежать одному промо TonPlay. Для удаления ссылки отправь -.",
        reply_markup=kb.as_markup()
    )
    await call.answer()


@dp.callback_query(F.data.startswith("adm:dualfield:"))
async def adm_dualfield(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    _, _, oid, field = call.data.split(":")
    field_name = "mini_app_url" if field == "mini" else "site_url"
    label = "URL Mini App (telegram.me/tonplay/dapp?...)" if field == "mini" else "URL сайта / URL трафика TonPlay"
    admin_state[call.from_user.id] = {"action":"dual_link","step":field_name,"offer_id":int(oid),"data":{}}
    await call.message.edit_text(f"Отправь {label}. Нужна ссылка http:// или https://. Чтобы очистить поле, отправь -. Для отмены напиши «отмена».")
    await call.answer()


@dp.callback_query(F.data == "adm:banners")
async def adm_banners(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    with closing(db()) as con:
        rows = con.execute("SELECT id,title,enabled,position,clicks,section FROM banners ORDER BY section,position,id").fetchall()
    kb = InlineKeyboardBuilder()
    lines = ["🖼 БАННЕРЫ MINI APP", "", "🎰 Игры · ⚽ Спорт · 🌐 Оба раздела"]
    if not rows:
        lines.append("Баннеров пока нет.")
    for bid,title,enabled,pos,clicks,section in rows:
        banner_title = (title or "Без названия").strip()
        short_title = banner_title[:23] + ("…" if len(banner_title) > 23 else "")
        section_label = {"casino":"🎰 Игры","bets":"⚽ Спорт","both":"🌐 Оба"}.get(section,"🎰 Игры")
        lines.append(f"{'🟢' if enabled else '🔴'} #{bid} {banner_title} | {section_label} | позиция {pos} | переходы {clicks}")
        kb.button(text=f"{'🔴 Выключить' if enabled else '🟢 Включить'}: {short_title}", callback_data=f"adm:bannertoggle:{bid}")
        kb.button(text=f"✏️ Изменить: {short_title}", callback_data=f"adm:banneredit:{bid}")
        kb.button(text=f"🗑 Удалить: {short_title}", callback_data=f"adm:bannerdelete:{bid}")
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
    bid = parse_callback_id(call.data, 2)
    if not bid:
        await call.answer("Кнопка устарела. Откройте меню заново.", show_alert=True)
        return
    with closing(db()) as con:
        con.execute("UPDATE banners SET enabled=CASE enabled WHEN 1 THEN 0 ELSE 1 END WHERE id=?", (bid,))
        con.commit()
    await call.answer("Статус баннера обновлён")
    await adm_banners(call)


@dp.callback_query(F.data.startswith("adm:banneredit:"))
async def adm_banneredit(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    bid = parse_callback_id(call.data, 2)
    if not bid:
        await call.answer("Кнопка устарела. Откройте меню заново.", show_alert=True)
        return
    with closing(db()) as con:
        row = con.execute("SELECT title FROM banners WHERE id=?", (bid,)).fetchone()
    if not row:
        await call.answer("Баннер не найден.", show_alert=True)
        return
    title = row[0] or "Без названия"
    kb = InlineKeyboardBuilder()
    for field,label in [
        ("title","✏️ Название"),
        ("image_url","🖼 Изображение"),
        ("url","🔗 Партнёрская ссылка"),
        ("position","↕️ Позиция в карусели"),
        ("section","🎯 Раздел: Игры / Спорт / Оба"),
    ]:
        kb.button(text=label, callback_data=f"adm:bannereditfield:{bid}:{field}")
    kb.button(text="⬅️ К списку баннеров", callback_data="adm:banners")
    kb.adjust(1)
    await call.message.edit_text(
        f"✏️ ИЗМЕНЕНИЕ БАННЕРА\n\n«{title}»\nВыбери, что изменить:",
        reply_markup=kb.as_markup()
    )
    await call.answer()


@dp.callback_query(F.data.startswith("adm:bannereditfield:"))
async def adm_bannereditfield(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    parts = call.data.split(":")
    bid, field = int(parts[2]), parts[3]
    prompts = {
        "title": "Отправь новое название баннера (только для админки):",
        "image_url": "Отправь новое изображение баннера фото/файлом JPG/PNG или прямую HTTPS-ссылку. Текущее изображение заменится.",
        "url": "Отправь новую партнёрскую ссылку (https://...).",
        "position": "Отправь новую позицию баннера в карусели (целое число, начиная с 1).",
        "section": "Выбери раздел баннера: отправь casino для «Игры», bets для «Спорт» или both для показа в обоих разделах.",
    }
    if field not in prompts:
        await call.answer("Неизвестное поле.", show_alert=True)
        return
    with closing(db()) as con:
        exists = con.execute("SELECT 1 FROM banners WHERE id=?", (bid,)).fetchone()
    if not exists:
        await call.answer("Баннер не найден.", show_alert=True)
        return
    admin_state[call.from_user.id] = {"action":"banner_edit","step":field,"banner_id":bid,"data":{}}
    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ Отмена", callback_data="adm:banners")
    await call.message.edit_text("✏️ ИЗМЕНЕНИЕ БАННЕРА\n\n" + prompts[field] + "\n\nДля отмены напиши «отмена».", reply_markup=kb.as_markup())
    await call.answer()


@dp.callback_query(F.data.startswith("adm:bannerdelete:"))
async def adm_bannerdelete(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    bid = parse_callback_id(call.data, 2)
    if not bid:
        await call.answer("Кнопка устарела. Откройте меню заново.", show_alert=True)
        return
    with closing(db()) as con:
        row = con.execute("SELECT title FROM banners WHERE id=?", (bid,)).fetchone()
    if not row:
        await call.answer("Баннер уже удалён или не найден.", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Да, удалить", callback_data=f"adm:bannerdeleteconfirm:{bid}")
    kb.button(text="↩️ Отмена", callback_data="adm:banners")
    kb.adjust(1)
    await call.message.edit_text(
        f"Удалить баннер #{bid} «{row[0] or 'Без заголовка'}» навсегда?\n\n"
        "Это удалит только баннер, не офферы и не промокоды.",
        reply_markup=kb.as_markup()
    )
    await call.answer()


@dp.callback_query(F.data.startswith("adm:bannerdeleteconfirm:"))
async def adm_bannerdeleteconfirm(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    bid = parse_callback_id(call.data, 2)
    if not bid:
        await call.answer("Кнопка устарела. Откройте меню заново.", show_alert=True)
        return
    with closing(db()) as con:
        row = con.execute("SELECT title FROM banners WHERE id=?", (bid,)).fetchone()
        con.execute("DELETE FROM banners WHERE id=?", (bid,))
        con.commit()
    if not row:
        await call.answer("Баннер уже удалён или не найден.", show_alert=True)
        return
    await call.answer("Баннер удалён")
    await adm_banners(call)


@dp.callback_query(F.data == "adm:banneradd")
async def adm_banneradd(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    admin_state[call.from_user.id] = {"action":"banner","step":"title","data":{}}
    await call.message.edit_text(
        "➕ НОВЫЙ БАННЕР В КАРУСЕЛЬ MINI APP\n\n"
        "1/5. Название для админки (его видишь только ты):"
    )
    await call.answer()


@dp.message(F.photo | F.video | F.animation | F.document)
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

    # Editing an existing carousel banner image.
    if state.get("action") == "banner_edit" and state.get("step") == "image_url":
        if message.photo:
            file_id = message.photo[-1].file_id
            mime = "image/jpeg"
            size = message.photo[-1].file_size or 0
        elif message.document and (message.document.mime_type or "").startswith("image/"):
            file_id = message.document.file_id
            mime = message.document.mime_type or "image/jpeg"
            size = message.document.file_size or 0
        elif message.video:
            await message.answer("Для карусели нужно изображение, не видео. Отправь JPG/PNG.")
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
        image_url = f"data:{mime};base64," + encoded
        bid = state["banner_id"]
        with closing(db()) as con:
            con.execute("UPDATE banners SET image_url=? WHERE id=?", (image_url, bid))
            con.commit()
        admin_state.pop(uid, None)
        await message.answer("✅ Изображение баннера обновлено.", reply_markup=admin_keyboard())
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
    elif message.animation:
        state["data"]["media_type"] = "animation"
        state["data"]["media_file_id"] = message.animation.file_id
    elif message.video:
        state["data"]["media_type"] = "video"
        state["data"]["media_file_id"] = message.video.file_id
    elif message.document:
        # Telegram on Android sometimes sends a GIF/JPG/PNG as a document.
        # Re-upload it using the correct Bot API media method so the published
        # campaign renders as an animation/photo rather than a downloadable file.
        doc = message.document
        mime = (doc.mime_type or "").lower()
        filename = doc.file_name or "campaign-media"
        is_gif = mime == "image/gif" or filename.lower().endswith(".gif")
        is_image = mime.startswith("image/")
        if not (is_gif or is_image):
            await message.answer("Пришли GIF, изображение JPG/PNG или видео. Документ другого типа не подходит.")
            return
        buffer = BytesIO()
        try:
            await message.bot.download(doc.file_id, destination=buffer)
            if is_gif:
                uploaded = await message.answer_animation(
                    BufferedInputFile(buffer.getvalue(), filename=filename if filename.lower().endswith(".gif") else "campaign.gif")
                )
                if not uploaded.animation:
                    raise RuntimeError("Telegram did not return animation metadata")
                state["data"]["media_type"] = "animation"
                state["data"]["media_file_id"] = uploaded.animation.file_id
            else:
                safe_name = filename if filename.lower().endswith((".jpg", ".jpeg", ".png")) else "campaign.jpg"
                uploaded = await message.answer_photo(BufferedInputFile(buffer.getvalue(), filename=safe_name))
                if not uploaded.photo:
                    raise RuntimeError("Telegram did not return photo metadata")
                state["data"]["media_type"] = "photo"
                state["data"]["media_file_id"] = uploaded.photo[-1].file_id
            try:
                await message.bot.delete_message(message.chat.id, uploaded.message_id)
            except Exception:
                pass
        except Exception as exc:
            await message.answer(f"Не удалось обработать файл. Попробуй отправить его как обычное фото или GIF-анимацию. ({type(exc).__name__})")
            return
    else:
        await message.answer("Для рекламной публикации отправь фото, GIF/анимацию или видео.")
        return
    state["step"] = "button_text"
    await message.answer("4/7. Текст кнопки или - без кнопки:")


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

    if action == "dual_link" and step in {"mini_app_url", "site_url"}:
        value = "" if text == "-" else text
        if value and not (value.startswith("https://") or value.startswith("http://")):
            await message.answer("Нужна ссылка, начинающаяся с http:// или https://, либо - для очистки.")
            return
        column = step
        with closing(db()) as con:
            con.execute(f"UPDATE offers SET {column}=? WHERE id=?", (value, state["offer_id"]))
            con.commit()
        admin_state.pop(uid, None)
        await message.answer("✅ Ссылка сохранена.", reply_markup=admin_keyboard())
        return

    if action == "text_broadcast" and step == "body":
        if not text:
            await message.answer("Отправь текст рассылки или напиши «отмена».")
            return
        kb = InlineKeyboardBuilder()
        kb.button(text="📤 Отправить всем сейчас", callback_data="adm:text_broadcast_send:confirm")
        kb.button(text="❌ Отмена", callback_data="adm:text_broadcast_cancel")
        kb.adjust(1)
        state["action"] = "text_broadcast_confirm"
        state["body"] = text
        state["token"] = "confirm"
        with closing(db()) as con:
            total = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        await message.answer(
            f"⚠️ ПРОВЕРЬ ТЕКСТ ПЕРЕД ОТПРАВКОЙ\n\n{text}\n\n"
            f"Получателей в базе: {total}. Отправить сейчас отдельным сообщением всем пользователям бота?",
            reply_markup=kb.as_markup()
        )
        return

    if action == "revenue_add":
        d = state.setdefault("data", {})
        if text.lower() in {"отмена", "cancel"}:
            admin_state.pop(uid, None)
            await message.answer("❌ Добавление дохода отменено.", reply_markup=admin_keyboard())
            return
        if step == "partner":
            d["partner"] = text[:100]
            state["step"] = "amount"
            await message.answer("2/5. Сумма дохода (например, 125.50):")
            return
        if step == "amount":
            try:
                amount = float(text.replace(",", ".").replace(" ", ""))
                if amount < 0 or amount > 1_000_000_000:
                    raise ValueError
            except ValueError:
                await message.answer("Введи корректную неотрицательную сумму, например 125.50.")
                return
            d["amount"] = amount
            state["step"] = "currency"
            await message.answer("3/5. Валюта (например, EUR, USD, RUB):")
            return
        if step == "currency":
            currency = text.strip().upper()
            if not currency.isalpha() or not (3 <= len(currency) <= 5):
                await message.answer("Введи код валюты, например EUR, USD или RUB.")
                return
            d["currency"] = currency
            state["step"] = "period"
            await message.answer("4/5. Период отчёта (например, 01–15.10.2026) или - если не нужен:")
            return
        if step == "period":
            d["period"] = "" if text == "-" else text[:100]
            state["step"] = "note"
            await message.answer("5/5. Примечание (например, доход по кабинету за неделю) или -:")
            return
        if step == "note":
            d["note"] = "" if text == "-" else text[:250]
            with closing(db()) as con:
                con.execute("INSERT INTO revenue_entries(partner,amount,currency,period,note) VALUES(?,?,?,?,?)",
                            (d["partner"],d["amount"],d["currency"],d["period"],d["note"]))
                con.commit()
            partner, amount, currency = d["partner"], d["amount"], d["currency"]
            admin_state.pop(uid, None)
            await message.answer(f"✅ Доход сохранён: {partner} — {amount:.2f} {currency}.\nОткрой «📈 Аналитика и доход», чтобы увидеть сводку.", reply_markup=admin_keyboard())
            return

    if action == "campaign":
        d = state["data"]
        if step == "title":
            d["title"] = text
            state["step"] = "body"
            await message.answer("2/7. Текст публикации:")
            return
        if step == "body":
            d["body"] = "" if text == "-" else text
            state["step"] = "media"
            await message.answer("3/7. Пришли фото или видео отдельным сообщением. Для текста без медиа напиши -.")
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
                await message.answer("5/7. Ссылка кнопки (https://...).")
            else:
                d["button_url"] = ""
                state["step"] = "placement"
                await message.answer("6/7. Как публиковать?\n1 — автоматическая рассылка по расписанию\n2 — отправлять вручную кнопкой «Отправить сейчас».\nВведи 1 или 2.")
            return
        if step == "button_url":
            if not text.startswith(("https://","http://")):
                await message.answer("Нужна ссылка http:// или https://.")
                return
            d["button_url"] = text
            state["step"] = "placement"
            await message.answer("6/7. Как публиковать?\n1 — автоматическая рассылка по расписанию\n2 — отправлять вручную кнопкой «Отправить сейчас».\nВведи 1 или 2.")
            return
        if step == "placement":
            if text not in {"1", "2"}:
                await message.answer("Введи 1 для рассылки по расписанию или 2 для ручной публикации.")
                return
            d["placement"] = "broadcast" if text == "1" else "manual"
            if d["placement"] == "manual":
                with closing(db()) as con:
                    pos = con.execute("SELECT COALESCE(MAX(position),0)+1 FROM campaigns").fetchone()[0]
                    con.execute("INSERT INTO campaigns(title,body,media_type,media_file_id,button_text,button_url,enabled,position,interval_hours,placement) VALUES(?,?,?,?,?,?,0,?,8,?)",
                        (d["title"],d["body"],d.get("media_type",""),d.get("media_file_id",""),d.get("button_text",""),d.get("button_url",""),pos,"manual"))
                    con.commit()
                admin_state.pop(uid, None)
                await message.answer("✅ Рекламная карточка сохранена. Она не отправится сама. Когда будешь готов, открой «📢 Рекламные публикации» и нажми «📣 Отправить сейчас».", reply_markup=admin_keyboard())
                return
            state["step"] = "interval"
            await message.answer("7/7. Интервал рассылки в часах: 8 = примерно 3 раза в день, 12 = 2 раза в день.")
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
                con.execute("INSERT INTO campaigns(title,body,media_type,media_file_id,button_text,button_url,enabled,position,interval_hours,placement) VALUES(?,?,?,?,?,?,0,?,?,?)",
                    (d["title"],d["body"],d.get("media_type",""),d.get("media_file_id",""),d.get("button_text",""),d.get("button_url",""),pos,interval,"broadcast"))
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

    if action == "banner_edit":
        bid = state["banner_id"]
        if step == "title":
            if not text:
                await message.answer("Название не может быть пустым. Отправь новое название.")
                return
            with closing(db()) as con:
                con.execute("UPDATE banners SET title=? WHERE id=?", (text[:120], bid))
                con.commit()
            admin_state.pop(uid, None)
            await message.answer("✅ Название баннера обновлено.", reply_markup=admin_keyboard())
            return
        if step == "image_url":
            if not text.startswith(("https://", "http://")):
                await message.answer("Отправь прямую ссылку http:// или https:// на изображение либо загрузи фото/файл JPG/PNG.")
                return
            with closing(db()) as con:
                con.execute("UPDATE banners SET image_url=? WHERE id=?", (text, bid))
                con.commit()
            admin_state.pop(uid, None)
            await message.answer("✅ Изображение баннера обновлено.", reply_markup=admin_keyboard())
            return
        if step == "url":
            if not text.startswith(("https://", "http://")):
                await message.answer("Нужна партнёрская ссылка http:// или https://.")
                return
            with closing(db()) as con:
                con.execute("UPDATE banners SET url=? WHERE id=?", (text, bid))
                con.commit()
            admin_state.pop(uid, None)
            await message.answer("✅ Партнёрская ссылка баннера обновлена.", reply_markup=admin_keyboard())
            return
        if step == "section":
            section = text.strip().lower()
            if section not in {"casino","bets","both"}:
                await message.answer("Отправь casino для «Игры», bets для «Спорт» или both для обоих разделов.")
                return
            with closing(db()) as con:
                con.execute("UPDATE banners SET section=? WHERE id=?", (section,bid))
                con.commit()
            admin_state.pop(uid, None)
            await message.answer("✅ Раздел баннера обновлён.", reply_markup=admin_keyboard())
            return
        if step == "position":
            try:
                pos = int(text)
                if pos < 1:
                    raise ValueError
            except ValueError:
                await message.answer("Позиция должна быть целым числом от 1.")
                return
            with closing(db()) as con:
                current = con.execute("SELECT position FROM banners WHERE id=?", (bid,)).fetchone()
                if not current:
                    admin_state.pop(uid, None)
                    await message.answer("Баннер не найден.")
                    return
                old_pos = current[0]
                if pos < old_pos:
                    con.execute("UPDATE banners SET position=position+1 WHERE position>=? AND position<? AND id<>?", (pos, old_pos, bid))
                elif pos > old_pos:
                    con.execute("UPDATE banners SET position=position-1 WHERE position>? AND position<=? AND id<>?", (old_pos, pos, bid))
                con.execute("UPDATE banners SET position=? WHERE id=?", (pos, bid))
                con.commit()
            admin_state.pop(uid, None)
            await message.answer("✅ Позиция баннера обновлена.", reply_markup=admin_keyboard())
            return

    if action == "banner":
        if step == "title":
            state["data"]["title"] = text
            state["step"] = "section"
            await message.answer("2/5. Выбери раздел: отправь casino — Игры, bets — Спорт, both — оба раздела.")
            return
        if step == "section":
            section = text.strip().lower()
            if section not in {"casino","bets","both"}:
                await message.answer("Отправь casino, bets или both.")
                return
            state["data"]["section"] = section
            state["step"] = "image_url"
            await message.answer("3/5. Отправь баннер фотографией или файлом JPG/PNG. Можно также прислать прямую HTTPS-ссылку на изображение.")
            return
        if step == "image_url":
            if not text.startswith(("https://", "http://")):
                await message.answer("Отправь баннер фотографией/файлом JPG или PNG либо прямой ссылкой на изображение.")
                return
            state["data"]["image_url"] = text
            state["step"] = "url"
            await message.answer("4/5. Партнёрская ссылка (https://...), куда переходить при нажатии на баннер:")
            return
        if step == "url":
            if not text.startswith(("https://", "http://")):
                await message.answer("Нужна партнёрская ссылка http:// или https://")
                return
            state["data"]["url"] = text
            state["step"] = "position"
            await message.answer("5/5. Позиция в карусели (число, например 1):")
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
                con.execute("INSERT INTO banners(title,text,image_url,button_text,url,enabled,position,section) VALUES(?,?,?,?,?,1,?,?)",
                            (d["title"],"",d["image_url"],"",d["url"],pos,d.get("section","casino")))
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
.banner{position:relative;width:83%;margin:10px auto 13px;overflow:hidden;border-radius:16px;padding:0;background:#080808;border:1px solid #5b4518;box-shadow:0 7px 18px #0007;cursor:pointer}.banner-image-link{display:block;position:relative;width:100%;aspect-ratio:5/3;overflow:hidden;line-height:0;background:#080808}.banner-image-link .banner-backdrop{position:absolute;inset:-24px;display:block;width:calc(100% + 48px);height:calc(100% + 48px);object-fit:cover;object-position:center;filter:blur(28px) brightness(.48) saturate(1.7);transform:scale(1.12)}.banner-image-link .banner-foreground{position:absolute;inset:0;display:block;width:100%;height:100%;object-fit:fill;object-position:center}.banner-dot{position:relative;display:block;height:6px;width:6px;padding:0;border:0;border-radius:8px;background:#554a30;overflow:hidden;flex:none;transition:width .2s ease}.banner-dot-active{width:22px;background:#554a30}.banner-dot-fill{position:absolute;inset:0 auto 0 0;width:0;border-radius:8px;background:linear-gradient(90deg,#ffe58a,#d7a21c)}.banner-dot-active.banner-dot-running .banner-dot-fill{animation:bannerDotProgress var(--banner-duration,5s) linear forwards}@keyframes bannerDotProgress{from{width:0}to{width:100%}}.banner h1,.banner p,.banner .go{display:none}.banner-action-links{display:none;flex-direction:column;gap:8px;padding:10px 12px 12px;background:#111}.banner-action-links.open{display:flex}.banner-action-links .play{display:flex;align-items:center;justify-content:center;min-height:48px;margin:0;border-radius:12px;text-decoration:none;font-weight:800}.banner-action-links .tonplay-telegram{background:#1688e8;color:#fff}.banner-action-links .tonplay-site{background:#ffcf18;color:#17130a}
.section{font-size:18px;font-weight:950;letter-spacing:-.3px;margin:16px 3px 9px}
.card{position:relative;background:#1c1c1e;border:1px solid #332d20;border-radius:24px;margin:10px 0;overflow:hidden;box-shadow:0 5px 16px #0005;cursor:pointer}
.cardtop{display:flex;gap:14px;padding:16px 14px;align-items:center;min-height:96px;background:#1c1c1e}.icon{width:68px;height:68px;border-radius:15px;background:#111;border:1px solid #303030;display:flex;align-items:center;justify-content:center;font-size:22px;flex:none;overflow:hidden}.icon img,.modal-logo img,.mini-logo img{width:100%;height:100%;object-fit:cover}.icon,.modal-logo,.mini-logo{position:relative}.brand-fallback{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:15px;font-weight:950;letter-spacing:-.5px;color:#ffe58a;background:#111}.icon img,.modal-logo img,.mini-logo img{position:absolute;inset:0;z-index:1;object-fit:cover;padding:0;background:transparent}.icon img.brand-img-1win,.modal-logo img.brand-img-1win,.mini-logo img.brand-img-1win{transform:scale(1.14)}.cardmain{flex:1;min-width:0;display:flex;flex-direction:column}.name{font-size:15px;font-weight:900;letter-spacing:-.2px;overflow-wrap:anywhere;order:2;margin-top:8px}.badges{display:flex;flex-wrap:wrap;gap:6px;margin-top:0;order:1}.bonus{display:inline-block;padding:6px 10px;border-radius:10px;background:#f4cb00;color:#171100;font-weight:950;font-size:12px}.bonus.alt{background:#24532d;color:#d4ffd4}.arrow{width:58px;height:58px;border:0;border-radius:18px;background:#ffd500;color:#111;display:flex;align-items:center;justify-content:center;text-decoration:none;font-size:28px;font-weight:950;flex:none;z-index:2}.cardbottom{border-top:1px solid #303033;padding:13px 18px;display:flex;justify-content:space-between;align-items:center;gap:10px;background:#1c1c1e;min-height:56px}.promo{color:#bdbdc2;font-size:12px;overflow-wrap:anywhere}.promo b{color:#f5f5f7}.copy{border:1px solid #5c4718;background:#211c0d;color:#f4d878;border-radius:12px;padding:10px 12px;font-size:12px;font-weight:900;flex:none}.go{display:block;margin:0 10px 10px;background:linear-gradient(110deg,#ffe58a,#dba51b);color:#191200;text-decoration:none;text-align:center;padding:11px;border-radius:11px;font-weight:950;font-size:12px;letter-spacing:.3px;box-shadow:0 5px 15px #d7a21d20}.empty{color:var(--muted);text-align:center;padding:24px 10px;border:1px dashed #3c3321;border-radius:18px;font-size:13px}
.note{color:#898378;font-size:10px;line-height:1.32;text-align:center;margin-top:7px;padding:6px 4px 7px;border-top:1px solid #2c271c}.brand-footer{position:relative;margin:0 auto 2px;text-align:center;overflow:visible;background:radial-gradient(ellipse at 50% 52%,#8b5b1238,transparent 70%);padding:0}.brand-footer-art{display:block;width:min(100%,650px);height:auto;max-height:184px;margin:0 auto;overflow:visible;filter:drop-shadow(0 2px 10px #d49a1d65)}.note .footer-age{display:inline-block;border:2px solid #e74b4b;color:#ff7777;border-radius:6px;padding:2px 9px;margin:0 0 5px;font-weight:900;font-size:12px}.note .footer-links{margin-top:6px}.note .footer-links a{color:#d7b64e;margin:0 8px;display:inline-block}
.modal-backdrop{display:none;position:fixed;inset:0;background:#000b;backdrop-filter:blur(8px);z-index:50;align-items:center;justify-content:center;padding:10px;padding-top:calc(16px + env(safe-area-inset-top));padding-bottom:calc(16px + env(safe-area-inset-bottom))}.modal-backdrop.open{display:flex}.modal{width:100%;max-width:650px;max-height:86vh;overflow:auto;background:linear-gradient(145deg,#292929,#191919 72%);border:1px solid #49443a;border-radius:20px;box-shadow:0 20px 80px #000c;padding:12px}.modalhead{display:flex;align-items:center;gap:9px}.modal-logo{width:48px;height:48px;border-radius:13px;overflow:hidden;background:#2d2617;border:1px solid #51421e;display:flex;align-items:center;justify-content:center;font-size:27px;flex:none}.modal-title{font-size:19px;font-weight:950;line-height:1.1;overflow-wrap:anywhere}.modal-close{margin-left:auto;width:34px;height:34px;border:0;border-radius:50%;background:#4a4a4a;color:white;font-size:24px;flex:none}.modal-section{background:#202020;border-radius:13px;padding:11px;margin-top:10px}.muted-label{color:#9c9c9c;font-size:12px;margin-bottom:7px}.description{font-size:13px;line-height:1.45;white-space:pre-wrap;overflow-wrap:anywhere}.promo-large{display:flex;align-items:center;gap:10px}.promo-value{font-size:16px;font-weight:850;overflow-wrap:anywhere;flex:1;min-width:0}.play{display:block;margin-top:10px;background:var(--gold);color:#171100;text-decoration:none;text-align:center;padding:12px;border-radius:11px;font-size:14px;font-weight:950}.other-title{font-size:12px;font-weight:950;color:#bcbcbc;margin:13px 2px 8px}.other-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.mini-game{display:flex;align-items:center;gap:7px;min-width:0;border:0;background:#202020;color:var(--text);padding:7px;border-radius:13px;text-align:left;text-decoration:none}.mini-logo{width:36px;height:36px;border-radius:9px;background:#332a17;display:flex;align-items:center;justify-content:center;overflow:hidden;flex:none}.mini-name{font-size:12px;font-weight:850;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.mini-link{font-size:11px;color:#aaa;margin-top:4px}
@media(min-width:560px){.wrap{padding-left:22px;padding-right:22px}.banner{padding:0}.banner h1{font-size:34px}.cardtop{padding:15px 13px}.icon{width:54px;height:54px}.arrow{width:44px;height:44px}.modal{padding:18px}}
</style></head>
<body>
<div class="wrap">
 <div class="header"><div class="brand-head"><svg class="brand-head-art" viewBox="0 0 330 76" role="img" aria-label="BONUS UP"><defs><linearGradient id="headGold" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#fffde8"/><stop offset=".28" stop-color="#ffe477"/><stop offset=".55" stop-color="#d99713"/><stop offset=".76" stop-color="#fff0a2"/><stop offset="1" stop-color="#9b5d00"/></linearGradient><linearGradient id="headWhite" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#fff"/><stop offset=".55" stop-color="#ffeeb3"/><stop offset="1" stop-color="#c48a16"/></linearGradient><filter id="headGlow" x="-.4" y="-.7" width="1.8" height="2.4"><feGaussianBlur stdDeviation="3"/></filter></defs><path d="M4 58 Q68 66 118 57 T225 54 Q270 53 319 25" fill="none" stroke="#f3b92d" stroke-width="8" opacity=".7" filter="url(#headGlow)"/><path d="M4 58 Q68 66 118 57 T225 54 Q270 53 319 25" fill="none" stroke="url(#headGold)" stroke-width="2.5"/><path d="M22 23 L15 5 L32 16 L44 1 L49 20 L65 11 L60 29 Z" fill="url(#headGold)" stroke="#8b5a08" stroke-width="1.2"/><text x="4" y="54" font-family="Arial Black,Arial,sans-serif" font-weight="1000" font-size="39" letter-spacing="-2.5" fill="url(#headGold)" stroke="#704405" stroke-width="1" paint-order="stroke">BONUS</text><text x="181" y="54" font-family="Arial Black,Arial,sans-serif" font-weight="1000" font-size="39" letter-spacing="-2" fill="url(#headGold)" stroke="#704405" stroke-width="1" paint-order="stroke">UP</text><path d="M204 39 L204 4 L194 16 M204 4 L214 16" fill="none" stroke="url(#headGold)" stroke-width="7" stroke-linecap="round" stroke-linejoin="round"/><text x="7" y="72" font-family="Arial,sans-serif" font-size="12" font-weight="800" fill="#ffe7a0">Бонусы и лучшие предложения</text><path d="M269 35 l3 -7 l3 7 l7 3 l-7 3 l-3 7 l-3 -7 l-7 -3z" fill="#fff0a1"/><path d="M291 19 l2 -5 l2 5 l5 2 l-5 2 l-2 5 l-2 -5 l-5 -2z" fill="#ffd85e"/></svg><div class="sub" data-i18n="subtitle">Лучшие бонусы и промокоды в одном месте!</div></div><div class="head-actions"><div class="profile" id="userProfile" aria-label="Профиль Telegram"><div class="avatar" id="userAvatar">?</div><div class="profile-name" id="userName"></div></div><select class="lang-select" id="languageSelect" aria-label="Язык"><option value="ru">🇷🇺 RU</option><option value="en">🇬🇧 EN</option><option value="de">🇩🇪 DE</option><option value="tr">🇹🇷 TR</option><option value="hi">🇮🇳 HI</option></select><div class="menu-wrap"><button class="menu-button" id="menuButton" aria-label="Меню" aria-expanded="false">⋮</button><div class="top-menu" id="topMenu"><button type="button" id="refreshPage" data-i18n="refreshPage">Обновить страницу</button><button type="button" id="createShortcut" data-i18n="createShortcut">Создать ярлык</button><a href="https://telegram.org/tos/mini-apps" data-i18n="terms">Условия использования</a></div></div><span style="font-size:12px;font-weight:900">18+</span></div></div>
 <div class="tabs"><button class="tab active" data-cat="casino" data-i18n="games">🎰 ИГРЫ</button><button class="tab" data-cat="bets" data-i18n="sport">⚽ СПОРТ</button></div>
 <div class="banner" id="promoBanner"><h1>🔥 Лучшие бонусы в одном месте</h1><p>Выбирай предложение, смотри бонус и переходи к оператору.</p></div>
 <div id="bannerDots" style="display:flex;justify-content:center;gap:6px;margin:-12px 0 18px"></div>
 <div class="section" id="sectionTitle">🎰 Игры</div><div id="offers"><div class="empty">Загрузка предложений…</div></div>
 <div class="note"><div class="brand-footer"><svg class="brand-footer-art" viewBox="0 0 700 184" role="img" aria-label="BONUS UP — лучшие бонусы и промокоды в одном месте"><defs><linearGradient id="footerGold" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#fffde8"/><stop offset=".2" stop-color="#ffe77c"/><stop offset=".48" stop-color="#d88c08"/><stop offset=".68" stop-color="#fff4bd"/><stop offset="1" stop-color="#8b5100"/></linearGradient><linearGradient id="footerWhiteGold" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#fff"/><stop offset=".5" stop-color="#fff4d5"/><stop offset="1" stop-color="#b9a078"/></linearGradient><radialGradient id="footerCoin"><stop stop-color="#fff8c2"/><stop offset=".48" stop-color="#f3bf36"/><stop offset="1" stop-color="#8b4e00"/></radialGradient><radialGradient id="footerAura"><stop stop-color="#ffc52d" stop-opacity=".5"/><stop offset="1" stop-color="#ffc52d" stop-opacity="0"/></radialGradient><filter id="footerGlow" x="-.35" y="-.7" width="1.7" height="2.4"><feGaussianBlur stdDeviation="5"/></filter><filter id="textGlow" x="-.2" y="-.4" width="1.4" height="1.8"><feGaussianBlur stdDeviation="2.5"/></filter></defs><ellipse cx="350" cy="82" rx="300" ry="82" fill="url(#footerAura)"/><g fill="#ffe58a"><circle cx="65" cy="51" r="2.6"/><circle cx="91" cy="29" r="1.8"/><circle cx="123" cy="69" r="2.2"/><circle cx="154" cy="20" r="2.4"/><circle cx="189" cy="42" r="1.7"/><circle cx="226" cy="17" r="2.1"/><circle cx="267" cy="34" r="1.8"/><circle cx="310" cy="15" r="2.3"/><circle cx="354" cy="34" r="1.7"/><circle cx="399" cy="17" r="2.3"/><circle cx="441" cy="39" r="1.8"/><circle cx="483" cy="18" r="2.4"/><circle cx="526" cy="34" r="1.7"/><circle cx="570" cy="20" r="2.4"/><circle cx="605" cy="45" r="2"/><circle cx="637" cy="25" r="2.4"/><circle cx="668" cy="57" r="1.7"/><circle cx="43" cy="103" r="2"/><circle cx="113" cy="128" r="2.2"/><circle cx="183" cy="111" r="1.6"/><circle cx="527" cy="116" r="2"/><circle cx="603" cy="123" r="2.2"/><circle cx="655" cy="99" r="1.8"/></g><g fill="#fff2a4" filter="url(#footerGlow)"><circle cx="116" cy="72" r="13"/><circle cx="583" cy="63" r="12"/><circle cx="350" cy="44" r="9"/></g><path d="M18 88 Q88 27 174 56 T353 56 T683 38 M18 111 Q110 61 202 87 T390 82 T681 66" fill="none" stroke="#f7c63e" stroke-width="10" opacity=".55" filter="url(#footerGlow)"/><path d="M18 88 Q88 27 174 56 T353 56 T683 38 M18 111 Q110 61 202 87 T390 82 T681 66" fill="none" stroke="url(#footerGold)" stroke-width="2.6"/><g transform="translate(89 84) rotate(-27)"><ellipse rx="30" ry="37" fill="url(#footerCoin)" stroke="#fff2a4" stroke-width="3"/><ellipse rx="23" ry="29" fill="none" stroke="#9b6207" stroke-width="3"/><path d="M-9 -13 Q0 -25 9 -13 L-9 13 Q0 25 9 13" fill="none" stroke="#fff4b7" stroke-width="4.5"/><path d="M-15 -22 L-7 -27 M12 22 L20 17" stroke="#fff8d1" stroke-width="2"/></g><g transform="translate(614 77) rotate(22)"><ellipse rx="27" ry="34" fill="url(#footerCoin)" stroke="#fff2a4" stroke-width="3"/><ellipse rx="20" ry="26" fill="none" stroke="#9b6207" stroke-width="3"/><path d="M-8 -12 Q0 -22 8 -12 L-8 12 Q0 22 8 12" fill="none" stroke="#fff4b7" stroke-width="4"/></g><g transform="translate(533 54)"><path d="M-25 -4 C-52 -33 -36 -43 -23 -31 L-7 -9 C-17 -43 4 -47 11 -32 L4 -4" fill="#e9a922" stroke="#fff1a0" stroke-width="2.5"/><path d="M0 -6 C18 -39 41 -35 30 -17 L13 -4" fill="#f6c74c" stroke="#fff1a0" stroke-width="2.5"/><rect x="-34" y="-5" width="69" height="47" rx="4" fill="url(#footerGold)" stroke="#fff0a1" stroke-width="2.5"/><path d="M0 -5 V42 M-34 10 H35" stroke="#a66a08" stroke-width="7"/><path d="M-2 -5 V42 M-34 10 H35" stroke="#fff3ad" stroke-width="2.5"/></g><g transform="translate(209 28)"><path d="M-30 17 L-35 -4 L-17 8 L0 -20 L15 8 L34 -3 L27 18 Z" fill="url(#footerGold)" stroke="#8d5a06" stroke-width="2"/><path d="M-28 20 H26" stroke="#fff2a4" stroke-width="3"/></g><g fill="#ffd34d"><path d="M130 39 l4 -11 l4 11 l11 4 l-11 4 l-4 11 l-4 -11 l-11 -4z"/><path d="M467 23 l3 -8 l3 8 l8 3 l-8 3 l-3 8 l-3 -8 l-8 -3z"/><path d="M578 112 l3 -8 l3 8 l8 3 l-8 3 l-3 8 l-3 -8 l-8 -3z"/></g><text x="130" y="104" font-family="Arial Black,Arial,sans-serif" font-size="64" font-weight="1000" letter-spacing="-3.5" fill="#ffcf42" opacity=".75" filter="url(#textGlow)">BONUS</text><text x="126" y="100" font-family="Arial Black,Arial,sans-serif" font-size="64" font-weight="1000" letter-spacing="-3.5" fill="url(#footerWhiteGold)" stroke="#573500" stroke-width="3.5" paint-order="stroke">BONUS</text><text x="374" y="100" font-family="Arial Black,Arial,sans-serif" font-size="64" font-weight="1000" letter-spacing="-2.5" fill="url(#footerGold)" stroke="#573500" stroke-width="3.5" paint-order="stroke">UP</text><path d="M389 80 V19 L373 38 M389 19 L405 38" fill="none" stroke="url(#footerGold)" stroke-width="11" stroke-linecap="round" stroke-linejoin="round"/><path d="M115 117 Q350 138 593 113" fill="none" stroke="url(#footerGold)" stroke-width="4.5" stroke-linecap="round"/><text x="350" y="145" text-anchor="middle" font-family="Arial,sans-serif" font-size="18" font-weight="850" fill="#fff6df">Лучшие бонусы и промокоды</text><text x="350" y="166" text-anchor="middle" font-family="Arial Black,Arial,sans-serif" font-size="19" font-weight="900" fill="#ffe58a">в одном месте!</text></svg></div><span class="footer-age">18+</span><br>Азартные игры связаны с риском финансовых потерь. Играйте ответственно и только в пределах своих возможностей. Если азарт выходит из-под контроля, обратитесь за профессиональной помощью.<div class="footer-links"><a href="/terms" data-i18n="terms">Условия использования</a></div></div>
</div>
<div class="modal-backdrop" id="modalBackdrop" role="dialog" aria-modal="true" aria-label="Информация о предложении"><div class="modal" id="modalContent"></div></div>
<script>
const tg=window.Telegram&&window.Telegram.WebApp;if(tg){tg.ready();tg.expand()}
const translations={
ru:{subtitle:"Бонусы и лучшие предложения",games:"🎰 ИГРЫ",sport:"⚽ СПОРТ",terms:"Условия использования",refreshPage:"Обновить страницу",createShortcut:"Создать ярлык",privacy:"Политика конфиденциальности",about:"Об игре",description:"Описание этого предложения скоро появится.",promo:"PROMO",partner:"Партнёрское предложение",copy:"▣ Copy",promoCode:"ПРОМОКОД",play:"ИГРАТЬ ↗",linkSoon:"ПАРТНЁРСКАЯ ССЫЛКА СКОРО ПОЯВИТСЯ",otherGames:"ДРУГИЕ ИГРЫ",otherBets:"ДРУГИЕ БУКМЕКЕРЫ",open:"Открыть ↗",soon:"Ссылка скоро появится",empty:"Пока нет активных предложений.",failed:"Не удалось загрузить предложения.",bannerTitle:"🔥 Лучшие бонусы в одном месте",bannerText:"Выбирай предложение, смотри бонус и переходи к оператору.",show:"СМОТРЕТЬ",close:"Закрыть",details:"Подробнее",copiedTitle:"Промокод",copied:"Промокод скопирован",copyFailed:"Не удалось скопировать. Скопируйте вручную: "},
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
function esc(s){return String(s??"").replace(/[&<>"']/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[m]))}
function logoHtml(x,cls){const name=String(x.name||"?").trim();const initials=name.split(/\s+/).slice(0,2).map(v=>v[0]||"").join("").toUpperCase();const n=name.toLowerCase();const domain=n.includes("1win")?"1win.com":(n.includes("зенит")||n.includes("zenit"))?"zenit.win":"";const url=x.logo_url|| (domain?"https://www.google.com/s2/favicons?domain="+domain+"&sz=128":"");return '<span class="brand-fallback">'+esc(initials)+'</span>'+(url?'<img class="'+(n.includes("1win")?"brand-img-1win":"")+'" src="'+esc(url)+'" alt="'+esc(name)+'" loading="lazy" onerror="this.remove()">':"")}
function bonusBadges(value){if(!value)return "";let parts=String(value).split(/\s*(?:\||\+|,|;)\s*/).filter(Boolean);if(parts.length===1){let match=String(value).match(/(\d+\s*FS|FS\s*\d+|\d+\s*%|Up to\s*[\d$€£]+)/ig);if(match&&match.length>1)parts=match}return parts.map((p,i)=>'<span class="bonus '+(/up to/i.test(p)?"alt":"")+'">'+esc(p.trim())+'</span>').join("")}
function hasPartnerLink(x){try{const u=new URL(String(x.url||""));const h=u.hostname.toLowerCase();return (u.protocol==="https:"||u.protocol==="http:")&&!["example.com","www.example.com","example.org","www.example.org","example.net","www.example.net"].includes(h)&&!h.endsWith(".example.com")&&!h.endsWith(".example.org")&&!h.endsWith(".example.net")}catch(e){return false}}
function render(cat){currentCat=cat;const box=document.getElementById("offers");const items=all.filter(x=>x.category===cat);const catNames={ru:{casino:"Игры",bets:"Спорт"},en:{casino:"Games",bets:"Sports"},de:{casino:"Spiele",bets:"Sport"},tr:{casino:"Oyunlar",bets:"Spor"},hi:{casino:"गेम",bets:"स्पोर्ट्स"}};document.getElementById("sectionTitle").textContent=(cat==="bets"?"⚽ ":"🎰 ")+((catNames[language]||catNames.ru)[cat]);if(!items.length){box.innerHTML='<div class="empty">'+esc(t("empty"))+'</div>';return}
box.innerHTML=items.filter(x=>hasPartnerLink(x)).map(x=>'<article class="card" data-open="'+Number(x.id)+'" role="link" tabindex="0" aria-label="'+esc(t("play"))+': '+esc(x.name)+'"><div class="cardtop"><div class="icon">'+logoHtml(x,"icon")+'</div><div class="cardmain"><div class="name">'+esc(x.name)+'</div><div class="badges">'+bonusBadges(x.bonus)+'</div></div><a class="arrow" href="/go/'+Number(x.id)+'" aria-label="'+esc(t("play"))+'" onclick="event.stopPropagation()"><span>↗</span></a></div><div class="cardbottom"><div class="promo">'+(x.promo?esc(t("promo"))+' <b>'+esc(x.promo)+'</b>':esc(t("promo")))+'</div>'+(x.promo?'<button class="copy" data-copy="'+Number(x.id)+'">'+esc(t("copy"))+'</button>':'')+'</div></article>').join("");
box.querySelectorAll(".card").forEach(card=>{card.addEventListener("click",e=>{if(e.target.closest(".copy"))return;openModal(Number(card.dataset.open))});card.addEventListener("keydown",e=>{if((e.key==="Enter"||e.key===" ")&&e.target===card){e.preventDefault();openModal(Number(card.dataset.open))}})});
box.querySelectorAll("[data-copy]").forEach(btn=>btn.addEventListener("click",e=>{e.stopPropagation();const item=all.find(x=>x.id===Number(btn.dataset.copy));if(item)copyPromo(item.promo)}))
}
function openBannerLinks(id){
 const x=all.find(item=>item.id===id);if(!x)return;
 if(!x.mini_app_url&&!x.site_url){openModal(id);return}
 selectedId=id;
 const modal=document.getElementById("modalContent");
 modal.innerHTML='<div class="modalhead"><div class="modal-logo">'+logoHtml(x,"modal-logo")+'</div><div style="min-width:0;flex:1"><div class="badges">'+bonusBadges(x.bonus)+'</div><div class="modal-title" style="margin-top:8px">'+esc(x.name)+'</div></div><button class="modal-close" id="closeModal" aria-label="'+esc(t("close"))+'">×</button></div>'+
 (x.mini_app_url?'<a class="play tonplay-telegram" href="/go/dual/'+Number(x.id)+'/mini" target="_blank" rel="noopener">✈️ Играть в Telegram</a>':'')+
 (x.site_url?'<a class="play tonplay-site" href="/go/dual/'+Number(x.id)+'/site" target="_blank" rel="noopener">🌐 Играть на сайте</a>':'');
 document.getElementById("modalBackdrop").classList.add("open");document.body.style.overflow="hidden";
 document.getElementById("closeModal").onclick=closeModal;
}
function openModal(id){const x=all.find(item=>item.id===id);if(!x)return;selectedId=id;const other=all.filter(item=>item.category===x.category&&item.id!==id&&hasPartnerLink(item));const modal=document.getElementById("modalContent");
modal.innerHTML='<div class="modalhead"><div class="modal-logo">'+logoHtml(x,"modal-logo")+'</div><div style="min-width:0;flex:1"><div class="badges">'+bonusBadges(x.bonus)+'</div><div class="modal-title" style="margin-top:8px">'+esc(x.name)+'</div></div><button class="modal-close" id="closeModal" aria-label="'+esc(t("close"))+'">×</button></div>'+
'<div class="modal-section"><div class="muted-label">'+esc(t("about"))+'</div><div class="description">'+esc(x.description||t("description"))+'</div></div>'+
(x.promo?'<div class="modal-section"><div class="muted-label">'+esc(t("promoCode"))+'</div><div class="promo-large"><div class="promo-value">'+esc(x.promo)+'</div><button class="copy" id="modalCopy">'+esc(t("copy"))+'</button></div></div>':'')+
((x.mini_app_url||x.site_url)?((x.mini_app_url?'<a class="play tonplay-telegram" href="/go/dual/'+Number(x.id)+'/mini" target="_blank" rel="noopener"><svg class="telegram-brand-icon" viewBox="0 0 24 24" width="22" height="22" aria-hidden="true" style="display:inline-block;vertical-align:-4px;margin-right:7px;fill:currentColor"><path d="M9.78 18.65c-.38 0-.32-.14-.45-.49l-1.1-3.63 8.46-5.3c.37-.23.16-.36-.12-.19l-10.45 4.15-3.58-1.12c-.77-.24-.78-.77.17-1.14L21.02 5.2c.62-.23 1.22.15 1.02 1.14l-2.9 13.68c-.2.97-.79 1.2-1.6.75l-4.45-3.28-2.14 2.07c-.24.24-.44.44-.9.44z"/></svg>Играть в Telegram</a>':'')+(x.site_url?'<a class="play tonplay-site" href="/go/dual/'+Number(x.id)+'/site" target="_blank" rel="noopener">🌐 Играть на сайте</a>':'')):(hasPartnerLink(x)?'<a class="play" href="/go/'+Number(x.id)+'" target="_blank" rel="noopener">'+esc(t("play"))+'</a>':'<div class="play" style="opacity:.6;cursor:not-allowed;background:#3a362b;color:#d0c8b5">'+esc(t("linkSoon"))+'</div>'))+
(other.length?'<div class="other-title">'+esc(t(x.category==="bets"?"otherBets":"otherGames"))+'</div><div class="other-grid">'+other.map(o=>hasPartnerLink(o)?'<a class="mini-game" href="/go/'+Number(o.id)+'" target="_blank" rel="noopener"><span class="mini-logo">'+logoHtml(o,"mini-logo")+'</span><span style="min-width:0"><span class="mini-name" style="display:block">'+esc(o.name)+'</span><span class="mini-link">'+esc(t("open"))+'</span></span></a>':'<div class="mini-game" style="opacity:.55"><span class="mini-logo">'+logoHtml(o,"mini-logo")+'</span><span style="min-width:0"><span class="mini-name" style="display:block">'+esc(o.name)+'</span><span class="mini-link">'+esc(t("soon"))+'</span></span></div>').join("")+'</div>':'');
document.getElementById("modalBackdrop").classList.add("open");document.body.style.overflow="hidden";
document.getElementById("closeModal").onclick=closeModal;
const cp=document.getElementById("modalCopy");if(cp)cp.onclick=()=>copyPromo(x.promo);
}
function closeModal(){document.getElementById("modalBackdrop").classList.remove("open");document.body.style.overflow="";selectedId=null}
document.getElementById("modalBackdrop").addEventListener("click",e=>{if(e.target.id==="modalBackdrop")closeModal()});document.addEventListener("keydown",e=>{if(e.key==="Escape")closeModal()});
async function copyPromo(v){try{if(navigator.clipboard&&navigator.clipboard.writeText)await navigator.clipboard.writeText(v);else{const el=document.createElement("textarea");el.value=v;document.body.appendChild(el);el.select();document.execCommand("copy");el.remove()}if(tg&&tg.showPopup)tg.showPopup({title:t("copiedTitle"),message:t("copied"),buttons:[{type:"ok"}]})}catch(e){if(tg&&tg.showAlert)tg.showAlert(t("copyFailed")+v)}}
let trackingToken="";
document.addEventListener("click",function(e){const a=e.target&&e.target.closest?e.target.closest('a[href]'):null;if(!a)return;try{const u=new URL(a.href,location.href);if(u.origin!==location.origin||!u.pathname.startsWith("/go/"))return;if(trackingToken&&!u.searchParams.has("track"))u.searchParams.set("track",trackingToken);if(tg&&typeof tg.openLink==="function"){e.preventDefault();e.stopPropagation();tg.openLink(u.href)}else if(trackingToken){a.href=u.href}}catch(_e){}},true);
async function load(){try{const tokenResponse=await fetch("/api/tracking-token",{cache:"no-store",headers:{"X-Telegram-Init-Data":tg.initData}});if(tokenResponse.ok){const tokenData=await tokenResponse.json();trackingToken=tokenData.token||""}const r=await fetch("/api/offers",{cache:"no-store",headers:{"X-Telegram-Init-Data":tg.initData}});all=await r.json();renderBanners(await fetchBanners("casino"));render("casino")}catch(e){document.getElementById("offers").innerHTML='<div class="empty">Не удалось загрузить предложения.</div>'}}
async function fetchBanners(section){try{const r=await fetch("/api/banners?section="+encodeURIComponent(section),{cache:"no-store",headers:{"X-Telegram-Init-Data":tg.initData}});return r.ok?await r.json():[]}catch(e){return []}}
let bannerTimer=null;
function renderBanners(items){
 const box=document.getElementById("promoBanner"),dots=document.getElementById("bannerDots");
 if(bannerTimer){clearInterval(bannerTimer);bannerTimer=null}
 if(!Array.isArray(items)||!items.length){dots.innerHTML="";box.style.display="none";return}
 box.style.display="block";
 let idx=0,sx=0;
 const INTERVAL=5000;
 function show(){
  const b=items[idx];
  const isTonPlay=String(b.title||"").toLowerCase().includes("tonplay");
   const tonOffer=isTonPlay?all.find(item=>String(item.name||"").toLowerCase().includes("tonplay")):null;
   const bannerImage='<img class="banner-backdrop" src="'+esc(b.image_url||"")+'" alt="" aria-hidden="true"><img class="banner-foreground" src="'+esc(b.image_url||"")+'" alt="" loading="lazy">';
   if(isTonPlay && tonOffer && (tonOffer.mini_app_url||tonOffer.site_url)){
    box.innerHTML='<div class="banner-image-link tonplay-banner-trigger" role="button" tabindex="0" aria-label="Показать кнопки TonPlay">'+bannerImage+'</div><div class="banner-action-links">'+
      (tonOffer.mini_app_url?'<a class="play tonplay-telegram" href="/go/dual/'+Number(tonOffer.id)+'/mini" target="_blank" rel="noopener"><svg class="telegram-brand-icon" viewBox="0 0 24 24" width="22" height="22" aria-hidden="true" style="display:inline-block;vertical-align:-4px;margin-right:7px;fill:currentColor"><path d="M9.78 18.65c-.38 0-.32-.14-.45-.49l-1.1-3.63 8.46-5.3c.37-.23.16-.36-.12-.19l-10.45 4.15-3.58-1.12c-.77-.24-.78-.77.17-1.14L21.02 5.2c.62-.23 1.22.15 1.02 1.14l-2.9 13.68c-.2.97-.79 1.2-1.6.75l-4.45-3.28-2.14 2.07c-.24.24-.44.44-.9.44z"/></svg>Играть в Telegram</a>':'')+
      (tonOffer.site_url?'<a class="play tonplay-site" href="/go/dual/'+Number(tonOffer.id)+'/site" target="_blank" rel="noopener">🌐 Играть на сайте</a>':'')+'</div>';
    const trigger=box.querySelector(".tonplay-banner-trigger"),actions=box.querySelector(".banner-action-links");
    const toggleActions=()=>actions.classList.toggle("open");
    trigger.addEventListener("click",toggleActions);
    trigger.addEventListener("keydown",e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();toggleActions()}});
   }else{
    box.innerHTML='<a class="banner-image-link" href="/go/banner/'+Number(b.id)+'" aria-label="Открыть предложение">'+bannerImage+'</a>';
   }
   dots.innerHTML=items.map((item,i)=>'<button class="banner-dot '+(i===idx?'banner-dot-active':'')+'" aria-label="Баннер '+(i+1)+'" data-index="'+i+'"><span class="banner-dot-fill"></span></button>').join("");
  dots.querySelectorAll("button").forEach(btn=>btn.onclick=()=>{idx=Number(btn.dataset.index);show();restart()});
 }
 function stop(){
  if(bannerTimer){clearInterval(bannerTimer);bannerTimer=null}
  dots.querySelectorAll(".banner-dot").forEach(dot=>dot.classList.remove("banner-dot-running"));
 }
 function restart(){
  stop();
  if(items.length>1){
   const active=dots.querySelector(".banner-dot-active");
   if(active){active.style.setProperty("--banner-duration",(INTERVAL/1000)+"s");void active.offsetWidth;active.classList.add("banner-dot-running")}
   bannerTimer=setInterval(()=>{
    idx=(idx+1)%items.length;
    show();
    const next=dots.querySelector(".banner-dot-active");
    if(next){next.style.setProperty("--banner-duration",(INTERVAL/1000)+"s");void next.offsetWidth;next.classList.add("banner-dot-running")}
   },INTERVAL);
  }
 }
 box.ontouchstart=e=>{sx=e.touches[0].clientX;stop()};
 box.ontouchend=e=>{const dx=e.changedTouches[0].clientX-sx;if(Math.abs(dx)>45){idx=(idx+(dx<0?1:-1)+items.length)%items.length;show()}restart()};
 box.onmouseenter=stop;
 box.onmouseleave=restart;
 show();
 restart();
}
document.querySelectorAll(".tab").forEach(b=>b.onclick=async()=>{document.querySelectorAll(".tab").forEach(x=>x.classList.remove("active"));b.classList.add("active");const cat=b.dataset.cat;render(cat);renderBanners(await fetchBanners(cat))});load();document.addEventListener("visibilitychange",()=>{if(document.visibilityState==="visible")load()});
</script></body></html>'''

async def mini_app(request):
    return web.Response(text=MINI_APP_HTML, content_type="text/html", headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache", "Expires": "0"})

async def api_offers(request):
    denied = webapp_access_denied(request)
    if denied:
        return denied
    with closing(db()) as con:
        rows = con.execute("""
            SELECT id,category,name,description,bonus,url,promo_code,logo_url,mini_app_url,site_url
            FROM offers WHERE enabled=1 AND COALESCE(hidden,0)=0
            ORDER BY category,
                CASE
                    WHEN lower(category)='casino' AND lower(name) LIKE '%tonplay%' THEN -1
                    WHEN lower(name) LIKE '%1win%' THEN 0
                    WHEN lower(name) LIKE '%зенит%' OR lower(name) LIKE '%zenit%' THEN 1
                    ELSE 2
                END,
                position,id
        """).fetchall()
    return web.json_response([
        {"id":r[0],"category":r[1],"name":r[2],"description":r[3],"bonus":r[4],
         "url":r[5],"promo":r[6] or "", "logo_url":r[7] or "",
         "mini_app_url":r[8] or "", "site_url":r[9] or ""}
        for r in rows
    ], headers={"Cache-Control": "no-store, no-cache, must-revalidate"})

async def api_banners(request):
    denied = webapp_access_denied(request)
    if denied:
        return denied
    section = request.query.get("section", "casino").strip().lower()
    if section not in {"casino", "bets"}:
        section = "casino"
    with closing(db()) as con:
        rows = con.execute("""
            SELECT id,title,text,image_url,button_text,url
            FROM banners
            WHERE enabled=1 AND (section=? OR section='both')
            ORDER BY CASE WHEN lower(title) LIKE '%tonplay%' THEN -1 ELSE CASE WHEN position<=0 THEN 2147483647 ELSE position END END, id
        """, (section,)).fetchall()
    return web.json_response([{"id":r[0],"title":r[1],"text":r[2],"image_url":r[3],"button_text":r[4],"url":r[5]} for r in rows], headers={"Cache-Control": "no-store, no-cache, must-revalidate"})

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


def apply_tracking_macros(target, click_id, user_id=None):
    """Substitute BONUS UP click and user IDs in affiliate tracking URLs."""
    return (target.replace("{click_id}", str(click_id))
                  .replace("{clickid}", str(click_id))
                  .replace("{uid}", str(user_id or "")))


async def go_offer(request):
    try:
        offer_id = int(request.match_info["offer_id"])
    except (TypeError, ValueError):
        raise web.HTTPNotFound()
    source, utm_source, utm_campaign = request_attribution(request, "offer")
    with closing(db()) as con:
        row = con.execute("SELECT url,name FROM offers WHERE id=? AND enabled=1", (offer_id,)).fetchone()
        if row and is_partner_url(row[0]):
            click_id = log_click(con, user_id=tracking_user_id(request), offer_id=offer_id, source=source,
                                 click_type="offer", partner=row[1], target_url=row[0],
                                 utm_source=utm_source, utm_campaign=utm_campaign)
            con.commit()
    if not row or not is_partner_url(row[0]):
        raise web.HTTPNotFound(text="Партнёрская ссылка ещё не подключена.")
    target = row[0]
    target = apply_tracking_macros(target, click_id, tracking_user_id(request))
    raise web.HTTPFound(target)

async def go_dual_offer(request):
    try:
        offer_id = int(request.match_info["offer_id"])
        kind = request.match_info["kind"]
    except (TypeError, ValueError):
        raise web.HTTPNotFound()
    if kind not in {"mini", "site"}:
        raise web.HTTPNotFound()
    source, utm_source, utm_campaign = request_attribution(request, "offer")
    with closing(db()) as con:
        row = con.execute(
            "SELECT mini_app_url,site_url,enabled,name FROM offers WHERE id=?",
            (offer_id,)
        ).fetchone()
        if not row or not row[2]:
            raise web.HTTPNotFound(text="Оффер недоступен.")
        target = row[0] if kind == "mini" else row[1]
        if not is_partner_url(target):
            raise web.HTTPNotFound(text="Ссылка для этого направления не настроена.")
        click_id = log_click(con, user_id=tracking_user_id(request), offer_id=offer_id, source=source,
                             click_type=f"offer_{kind}", partner=row[3],
                             target_url=target, utm_source=utm_source,
                             utm_campaign=utm_campaign)
        con.commit()
    target = apply_tracking_macros(target, click_id, tracking_user_id(request))
    raise web.HTTPFound(target)


async def go_banner(request):
    try:
        banner_id = int(request.match_info["banner_id"])
    except (TypeError, ValueError):
        raise web.HTTPNotFound()
    source, utm_source, utm_campaign = request_attribution(request, "banner")
    with closing(db()) as con:
        row = con.execute("SELECT title,url FROM banners WHERE id=? AND enabled=1", (banner_id,)).fetchone()
        if not row or not is_partner_url(row[1]):
            raise web.HTTPNotFound()
        target = row[1]
        click_id = log_click(con, user_id=tracking_user_id(request), source=source, click_type="banner",
                             partner=row[0], banner_id=banner_id, target_url=target,
                             utm_source=utm_source, utm_campaign=utm_campaign)
        con.execute("UPDATE banners SET clicks=clicks+1 WHERE id=?", (banner_id,))
        con.commit()
    target = apply_tracking_macros(target, click_id, tracking_user_id(request))
    raise web.HTTPFound(target)

async def go_campaign(request):
    try:
        campaign_id = int(request.match_info["campaign_id"])
    except (TypeError, ValueError):
        raise web.HTTPNotFound()
    source, utm_source, utm_campaign = request_attribution(request, "campaign")
    with closing(db()) as con:
        # Existing sent cards should keep their partner link even if the admin
        # disables future sends; deleting the campaign removes this route.
        row = con.execute("SELECT title,button_url FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if not row or not is_partner_url(row[1]):
            raise web.HTTPNotFound()
        target = row[1]
        click_id = log_click(con, user_id=tracking_user_id(request), source=source, click_type="campaign",
                             partner=row[0], campaign_id=campaign_id, target_url=target,
                             utm_source=utm_source, utm_campaign=utm_campaign)
        con.execute("UPDATE campaigns SET click_count=click_count+1 WHERE id=?", (campaign_id,))
        con.commit()
    target = apply_tracking_macros(target, click_id, tracking_user_id(request))
    raise web.HTTPFound(target)


async def partner_postback(request):
    """Optional authenticated affiliate postback endpoint; disabled until secret is configured."""
    expected_secret = os.getenv("POSTBACK_SECRET", "")
    if not expected_secret:
        raise web.HTTPServiceUnavailable(text="Partner postbacks are not configured.")
    supplied_secret = request.headers.get("X-Postback-Secret", "")
    if not supplied_secret or not hmac.compare_digest(supplied_secret, expected_secret):
        raise web.HTTPUnauthorized(text="Invalid postback credentials.")
    # Accept only a JSON object over POST. Do not silently treat query parameters
    # as a postback payload: they are easier to leak into logs and browser history.
    try:
        data = await request.json()
    except (json.JSONDecodeError, ValueError, TypeError):
        raise web.HTTPBadRequest(text="Expected a JSON object.")
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text="Expected a JSON object.")
    click_id = str(data.get("click_id", "")).strip()[:128]
    partner = str(data.get("partner", "")).strip()[:120]
    event_type = str(data.get("event", "conversion")).strip()[:80] or "conversion"
    event_id = str(data.get("event_id", "")).strip()[:160]
    currency = str(data.get("currency", "EUR")).strip().upper()[:8] or "EUR"
    try:
        amount = float(data.get("amount", 0) or 0)
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="Invalid amount.")
    if not click_id or not partner or not event_id:
        raise web.HTTPBadRequest(text="click_id, partner and event_id are required.")
    if not math.isfinite(amount) or not (-1e9 < amount < 1e9):
        raise web.HTTPBadRequest(text="Invalid amount.")
    with closing(db()) as con:
        known_click = con.execute(
            "SELECT partner FROM clicks WHERE click_id=?", (click_id,)
        ).fetchone()
        if not known_click:
            raise web.HTTPNotFound(text="Unknown click_id.")
        # The clicks.partner field is currently a display label (often an
        # offer, banner, or campaign title), not a stable affiliate-network ID.
        # Do not compare it with the partner's postback identifier: that rejects
        # legitimate conversions. The authenticated secret and unique click_id
        # bind this event to a tracked click; preserve the network ID from payload.
        try:
            con.execute(
                """INSERT INTO conversion_events
                   (click_id,partner,event_type,event_id,amount,currency,payload)
                   VALUES(?,?,?,?,?,?,?)""",
                (click_id, partner, event_type, event_id, amount, currency,
                 json.dumps(data, ensure_ascii=False)[:4000])
            )
            con.commit()
        except sqlite3.IntegrityError:
            return web.json_response({"ok": True, "duplicate": True})
    return web.json_response({"ok": True, "recorded": True})


async def tonplay_postback(request):
    """GET-compatible TonPlay callback; configure only after confirming its macro names."""
    expected_secret = os.getenv("TONPLAY_POSTBACK_SECRET", "")
    if not expected_secret:
        raise web.HTTPServiceUnavailable(text="TonPlay postbacks are not configured.")
    supplied_secret = request.query.get("key", "")
    if not supplied_secret or not hmac.compare_digest(supplied_secret, expected_secret):
        raise web.HTTPUnauthorized(text="Invalid postback credentials.")

    # TonPlay documents {click_slug}, {action}, {amount_usd}, {tx_id}.
    # Keep click_id/transaction_id aliases for backward compatibility.
    click_id = str(request.query.get("click_id") or request.query.get("click_slug") or "").strip()[:128]
    event_type = str(request.query.get("event") or request.query.get("action") or request.query.get("status") or "conversion").strip()[:80] or "conversion"
    event_id = str(request.query.get("event_id") or request.query.get("transaction_id") or request.query.get("tx_id") or "").strip()[:160]
    action = event_type.lower()
    # TonPlay registration has no transaction ID; deduplicate its one-time event.
    if not event_id and click_id and action in {"register", "registration"}:
        event_id = f"{click_id}:register"
    # Deposits/withdrawals carry tx_id and must retain distinct transactions.
    currency = str(request.query.get("currency", "USD")).strip().upper()[:8] or "USD"
    amount_raw = request.query.get("amount", request.query.get("payout", request.query.get("amount_usd", request.query.get("rs", request.query.get("ngr", "0")))))
    try:
        amount = float(amount_raw or 0)
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="Invalid amount.")
    if not click_id or not event_id:
        raise web.HTTPBadRequest(text="click_id and event_id/transaction_id are required.")
    if not math.isfinite(amount) or not (-1e9 < amount < 1e9):
        raise web.HTTPBadRequest(text="Invalid amount.")

    payload = {k: v for k, v in request.query.items() if k != "key"}
    with closing(db()) as con:
        known_click = con.execute(
            "SELECT click_id FROM clicks WHERE click_id=?", (click_id,)
        ).fetchone()
        if not known_click:
            raise web.HTTPNotFound(text="Unknown click_id.")
        try:
            con.execute(
                """INSERT INTO conversion_events
                   (click_id,partner,event_type,event_id,amount,currency,payload)
                   VALUES(?,?,?,?,?,?,?)""",
                (click_id, "TonPlay", event_type, event_id, amount, currency,
                 json.dumps(payload, ensure_ascii=False)[:4000])
            )
            con.commit()
        except sqlite3.IntegrityError:
            return web.json_response({"ok": True, "duplicate": True})
    return web.json_response({"ok": True, "recorded": True})


async def vavada_postback(request):
    """VavadaPart GET postback. Accepts the macros shown in the partner dashboard."""
    expected_secret = os.getenv("VAVADA_POSTBACK_SECRET", "")
    if not expected_secret:
        raise web.HTTPServiceUnavailable(text="Vavada postbacks are not configured.")
    supplied_secret = request.query.get("key", "")
    if not supplied_secret or not hmac.compare_digest(supplied_secret, expected_secret):
        raise web.HTTPUnauthorized(text="Invalid postback credentials.")

    # VavadaPart's guide documents click_id, reward, tid and currency.
    # Its URL editor also shows action and player_id; accept both forms.
    click_id = str(request.query.get("subid") or request.query.get("click_id") or "").strip()[:128]
    raw_event = str(request.query.get("status") or request.query.get("action") or
                    request.query.get("event") or "conversion").strip().lower()[:80]
    event_aliases = {
        "reg": "registration", "register": "registration", "registration": "registration",
        "signup": "registration", "sign_up": "registration", "sign-up": "registration",
        "new_registration": "registration", "new registration": "registration",
        "dep": "first_deposit", "deposit": "first_deposit",
        "first deposit": "first_deposit", "first_deposit": "first_deposit",
        "first-deposit": "first_deposit", "firstdeposit": "first_deposit",
        "first_dep": "first_deposit", "firstdep": "first_deposit", "ftd": "first_deposit",
        "reward": "reward", "sale": "reward", "commission": "reward",
    }
    event_type = event_aliases.get(raw_event, raw_event or "conversion")

    # tid is preferred for distinct RevShare/reward transactions. player_id
    # is accepted as a fallback because it is the macro shown in the URL editor.
    event_id = str(request.query.get("tid") or request.query.get("event_id") or
                   request.query.get("transaction_id") or request.query.get("player_id") or "").strip()[:160]
    if not event_id and click_id and event_type == "registration":
        event_id = f"{click_id}:registration"

    currency = str(request.query.get("currency", "EUR")).strip().upper()[:8] or "EUR"
    amount_raw = request.query.get("payout", request.query.get("reward", request.query.get("amount", "0")))
    try:
        amount = float(amount_raw or 0)
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="Invalid payout amount.")
    if not click_id or not event_id:
        raise web.HTTPBadRequest(text="subid/click_id and tid/event_id/player_id are required.")
    if not math.isfinite(amount) or not (-1e9 < amount < 1e9):
        raise web.HTTPBadRequest(text="Invalid payout amount.")

    payload = {k: v for k, v in request.query.items() if k != "key"}
    with closing(db()) as con:
        known_click = con.execute("SELECT click_id FROM clicks WHERE click_id=?", (click_id,)).fetchone()
        if not known_click:
            raise web.HTTPNotFound(text="Unknown click_id.")
        try:
            con.execute(
                """INSERT INTO conversion_events
                   (click_id,partner,event_type,event_id,amount,currency,payload)
                   VALUES(?,?,?,?,?,?,?)""",
                (click_id, "Vavada", event_type, event_id, amount, currency,
                 json.dumps(payload, ensure_ascii=False)[:4000])
            )
            con.commit()
        except sqlite3.IntegrityError:
            return web.json_response({"ok": True, "duplicate": True})
    return web.json_response({"ok": True, "recorded": True})


async def vodka_postback(request):
    """Vodka Money HTTP GET postback; validates the documented X-Signature."""
    expected_secret = os.getenv("VODKA_POSTBACK_SECRET", "")
    if not expected_secret:
        raise web.HTTPServiceUnavailable(text="Vodka Money postback signature is not configured.")

    signature = str(request.headers.get("X-Signature", "")).strip().lower()
    if signature.startswith("md5="):
        signature = signature[4:]
    # Vodka Money's guide specifies MD5(full_url + secret) in X-Signature.
    full_url = f"{request.scheme}://{request.host}{request.raw_path}"
    expected_signature = hashlib.md5((full_url + expected_secret).encode("utf-8")).hexdigest()
    if not signature or not hmac.compare_digest(signature, expected_signature):
        raise web.HTTPUnauthorized(text="Invalid Vodka Money signature.")

    q = request.query
    click_id = str(q.get("click_id") or q.get("subid") or q.get("sub_id") or
                   q.get("aff_sub") or q.get("subid1") or q.get("clickid") or "").strip()[:128]
    raw_event = str(q.get("type") or q.get("event") or q.get("status") or "conversion").strip().lower()[:80]
    aliases = {
        "click": "click",
        "reg": "registration", "register": "registration", "registration": "registration",
        "signup": "registration", "sign_up": "registration",
        "fd": "first_deposit", "ftd": "first_deposit", "first deposit": "first_deposit",
        "first_deposit": "first_deposit", "first-deposit": "first_deposit",
        "any_second_deposit": "any_second_deposit", "second_deposit": "any_second_deposit",
        "deposit": "deposit", "pd": "deposit",
    }
    event_type = aliases.get(raw_event, raw_event or "conversion")
    # Clicks are already counted when the user leaves BONUS UP; acknowledge but
    # do not create a conversion record for a duplicate network-side click event.
    if event_type == "click":
        return web.json_response({"ok": True, "ignored": "click event tracked on outbound redirect"})

    event_id = str(q.get("transaction_id") or q.get("transaction") or q.get("tx_id") or
                   q.get("event_id") or q.get("id") or "").strip()[:160]
    if not event_id and click_id and event_type in {"registration", "first_deposit"}:
        event_id = f"{click_id}:{event_type}"
    amount_raw = q.get("usd_amount", q.get("amount", "0"))
    try:
        amount = float(amount_raw or 0)
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="Invalid usd_amount.")
    if not click_id or not event_id:
        raise web.HTTPBadRequest(text="A tracked click ID and event ID are required.")
    if not math.isfinite(amount) or not (-1e9 < amount < 1e9):
        raise web.HTTPBadRequest(text="Invalid usd_amount.")

    payload = {k: v for k, v in q.items()}
    with closing(db()) as con:
        known_click = con.execute("SELECT click_id FROM clicks WHERE click_id=?", (click_id,)).fetchone()
        if not known_click:
            raise web.HTTPNotFound(text="Unknown click_id.")
        try:
            con.execute(
                """INSERT INTO conversion_events
                   (click_id,partner,event_type,event_id,amount,currency,payload)
                   VALUES(?,?,?,?,?,?,?)""",
                (click_id, "Vodka Money", event_type, event_id, amount, "USD",
                 json.dumps(payload, ensure_ascii=False)[:4000])
            )
            con.commit()
        except sqlite3.IntegrityError:
            return web.json_response({"ok": True, "duplicate": True})
    return web.json_response({"ok": True, "recorded": True})


async def sykaaa_postback(request):
    """SYKAAA / Snow Dunes Partners S2S postback for registration and first deposit."""
    q = request.query
    expected_secret = os.getenv("SYKAAA_POSTBACK_SECRET", "")
    supplied_secret = q.get("key", "")
    if not expected_secret:
        raise web.HTTPServiceUnavailable(text="SYKAAA postbacks are not configured.")
    if not supplied_secret or not hmac.compare_digest(supplied_secret, expected_secret):
        raise web.HTTPUnauthorized(text="Invalid postback credentials.")

    event_type = str(request.match_info.get("event_type", "")).strip().lower()
    if event_type not in {"registration", "deposit"}:
        raise web.HTTPBadRequest(text="Unsupported event type.")

    click_id = str(q.get("hit") or q.get("clickid") or q.get("click_id") or
                   q.get("subid") or "").strip()[:128]
    # The partner's test tool may send literal macros. Acknowledge tests without
    # ever recording them as real conversions.
    if click_id.startswith("{") and click_id.endswith("}"):
        return web.json_response({"ok": True, "test": True, "recorded": False})
    if not click_id:
        raise web.HTTPBadRequest(text="Missing SYKAAA hit/click ID.")

    event_id = str(q.get("event_id") or q.get("transaction_id") or
                   q.get("deposit_id") or "").strip()[:160]
    if not event_id and event_type == "registration":
        event_id = f"{click_id}:registration"
    if not event_id:
        raise web.HTTPBadRequest(text="Missing unique event_id for deposit.")

    amount_raw = q.get("amount", q.get("deposit_amount", q.get("sum", "0")))
    try:
        amount = float(amount_raw or 0) if event_type == "deposit" else 0.0
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="Invalid amount.")
    if not math.isfinite(amount) or not (0 <= amount < 1e9):
        raise web.HTTPBadRequest(text="Invalid amount.")

    currency = str(q.get("currency") or "USD").strip().upper()[:8] or "USD"
    payload = {k: v for k, v in q.items() if k != "key"}
    with closing(db()) as con:
        known_click = con.execute(
            "SELECT click_id FROM clicks WHERE click_id=?", (click_id,)
        ).fetchone()
        if not known_click:
            raise web.HTTPNotFound(text="Unknown SYKAAA click ID.")
        try:
            con.execute(
                """INSERT INTO conversion_events
                   (click_id,partner,event_type,event_id,amount,currency,payload)
                   VALUES(?,?,?,?,?,?,?)""",
                (click_id, "SYKAAA", event_type, event_id, amount, currency,
                 json.dumps(payload, ensure_ascii=False)[:4000])
            )
            con.commit()
        except sqlite3.IntegrityError:
            return web.json_response({"ok": True, "duplicate": True})
    return web.json_response({"ok": True, "recorded": True})


async def norm_lacasino_postback(request):
    """NormAffiliates callback for La Casino registration/deposit events."""
    # NormAffiliates' built-in tester may send literal macros rather than
    # real event data. Allow only that synthetic probe through without the
    # shared secret; it is acknowledged below and never recorded as a conversion.
    q = request.query
    synthetic_test = (
        q.get("source_id1", "{source_id1}") in ("", "{source_id1}")
        and q.get("event_id", "{event_id}") in ("", "{event_id}")
        and q.get("deposit_amount", "{deposit_amount}") in ("", "{deposit_amount}")
    )
    if synthetic_test:
        # A tester may omit macro values entirely. Acknowledge this probe
        # before auth/validation and never persist it as a real conversion.
        return web.json_response({"ok": True, "test": True, "recorded": False})
    expected_secret = os.getenv("NORM_LACASINO_POSTBACK_SECRET", "")
    supplied_secret = q.get("key", "")
    if not synthetic_test:
        if not expected_secret:
            raise web.HTTPServiceUnavailable(text="NormAffiliates La Casino postbacks are not configured.")
        if not supplied_secret or not hmac.compare_digest(supplied_secret, expected_secret):
            raise web.HTTPUnauthorized(text="Invalid postback credentials.")

    # The route fixes the event type; event_id identifies the event, not its type.
    event_type = str(request.match_info.get("event_type", "")).strip().lower()
    if event_type not in {"registration", "deposit"}:
        raise web.HTTPBadRequest(text="Unsupported event type.")
    # Use a dedicated source_id slot for BONUS UP's click ID. Do not use
    # source_id itself as it may be the publisher's static UTM/source code.
    click_id = str(q.get("source_id1") or q.get("source_id2") or
                   q.get("source_id3") or q.get("source_id4") or
                   q.get("source_id5") or q.get("click_id") or
                   q.get("subid") or "").strip()[:128]
    event_id = str(q.get("event_id") or "").strip()[:160]
    if not click_id:
        raise web.HTTPBadRequest(text="Missing BONUS UP click ID (source_id1).")
    if not event_id and event_type == "registration":
        event_id = f"{click_id}:registration"
    if not event_id:
        raise web.HTTPBadRequest(text="Missing unique event_id for deposit.")

    # NormAffiliates' built-in test can send macros literally instead of
    # real values. Acknowledge this synthetic test before parsing numeric
    # deposit fields; never record it as a real conversion.
    if click_id.startswith("{") and click_id.endswith("}"):
        return web.json_response({"ok": True, "test": True, "recorded": False})

    currency = str(q.get("player_currency") or q.get("currency") or "EUR").strip().upper()[:8] or "EUR"
    # NormAffiliates defines deposit_amount as the player's cumulative deposits
    # up to the callback, not necessarily the amount of this individual deposit.
    amount_raw = q.get("deposit_amount", "0") if event_type == "deposit" else "0"
    try:
        amount = float(amount_raw or 0)
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="Invalid deposit_amount.")
    if not math.isfinite(amount) or not (0 <= amount < 1e9):
        raise web.HTTPBadRequest(text="Invalid deposit_amount.")

    payload = {k: v for k, v in q.items() if k != "key"}
    with closing(db()) as con:
        known_click = con.execute(
            "SELECT click_id FROM clicks WHERE click_id=?", (click_id,)
        ).fetchone()
        if not known_click:
            raise web.HTTPNotFound(text="Unknown click_id.")
        try:
            con.execute(
                """INSERT INTO conversion_events
                   (click_id,partner,event_type,event_id,amount,currency,payload)
                   VALUES(?,?,?,?,?,?,?)""",
                (click_id, "NormAffiliates / La Casino", event_type, event_id,
                 amount, currency, json.dumps(payload, ensure_ascii=False)[:4000])
            )
            con.commit()
        except sqlite3.IntegrityError:
            return web.json_response({"ok": True, "duplicate": True})
    return web.json_response({"ok": True, "recorded": True})


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
                    FROM campaigns WHERE enabled=1 AND COALESCE(placement,'broadcast')='broadcast' ORDER BY position,id""").fetchall()
                users = con.execute("SELECT user_id,last_campaign_id FROM users").fetchall()
                global_row = con.execute("SELECT value FROM app_settings WHERE key='last_campaign_broadcast'").fetchone()
            global_last = global_row[0] if global_row else ""
            global_due = True
            if global_last:
                try:
                    stamp = datetime.fromisoformat(global_last.replace("Z","+00:00"))
                    # Global throttle spaces out broadcasts, but a campaign created
                    # after the previous broadcast is allowed its first delivery.
                    global_due = (datetime.now(timezone.utc)-stamp).total_seconds() >= 8*3600
                    if not global_due:
                        with closing(db()) as con:
                            fresh = con.execute(
                                "SELECT 1 FROM campaigns WHERE enabled=1 AND last_sent_at='' "
                                "AND datetime(created_at)>datetime(?) LIMIT 1",
                                (global_last,)
                            ).fetchone()
                        global_due = bool(fresh)
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
                                sent_message = await bot.send_photo(user_id, file_id, caption=body[:1024] or None, reply_markup=markup)
                            elif media_type == "animation" and file_id:
                                sent_message = await bot.send_animation(user_id, file_id, caption=body[:1024] or None, reply_markup=markup)
                            elif media_type == "video" and file_id:
                                sent_message = await bot.send_video(user_id, file_id, caption=body[:1024] or None, reply_markup=markup)
                            else:
                                sent_message = await bot.send_message(user_id, body[:4096], reply_markup=markup)
                            delivered += 1
                            with closing(db()) as con:
                                con.execute("UPDATE users SET last_campaign_id=? WHERE user_id=?", (cid,user_id))
                                con.execute(
                                    "INSERT OR REPLACE INTO campaign_sends(campaign_id,user_id,message_id,sent_at) "
                                    "VALUES(?,?,?,CURRENT_TIMESTAMP)",
                                    (cid,user_id,sent_message.message_id)
                                )
                                con.execute(
                                    "INSERT INTO campaign_delivery_messages(campaign_id,user_id,message_id,sent_at) "
                                    "VALUES(?,?,?,CURRENT_TIMESTAMP)",
                                    (cid,user_id,sent_message.message_id)
                                )
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
        except Exception as exc:
            print(f"[campaign-scheduler] Error: {type(exc).__name__}: {exc}", flush=True)
        await asyncio.sleep(60)


async def main():
    init_db()
    seed_demo()
    bot = Bot(TOKEN)
    app = web.Application()
    app.router.add_get("/app", mini_app)
    app.router.add_get("/api/offers", api_offers)
    app.router.add_get("/api/banners", api_banners)
    app.router.add_get("/api/tracking-token", api_tracking_token)
    app.router.add_get("/go/{offer_id}", go_offer)
    app.router.add_get("/go/dual/{offer_id}/{kind}", go_dual_offer)
    app.router.add_get("/go/banner/{banner_id}", go_banner)
    app.router.add_get("/go/campaign/{campaign_id}", go_campaign)
    app.router.add_post("/postback", partner_postback)
    app.router.add_get("/postback/tonplay", tonplay_postback)
    app.router.add_get("/postback/vavada", vavada_postback)
    app.router.add_get("/postback/vodka", vodka_postback)
    app.router.add_get("/postback/sykaaa/{event_type}", sykaaa_postback)
    app.router.add_get("/postback/norm/lacasino/{event_type}", norm_lacasino_postback)
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
