# BONUS UP

Telegram-бот и Mini App с каталогом букмекерских и casino-офферов, управляемыми баннерами и рекламными кампаниями. Данные и настройки хранятся в SQLite.

## Запуск локально

Требуется Python 3.11 или новее.

```bash
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
python bot.py
```

Перед запуском задайте переменные окружения:

- `BOT_TOKEN` — токен Telegram-бота (обязательно).
- `ADMIN_IDS` — Telegram ID администраторов через запятую.
- `DB_PATH` — путь к SQLite-файлу; по умолчанию `bonus_up.db`.
- `WEBAPP_URL` — публичный адрес Mini App, если он задаётся отдельно.
- `POSTBACK_SECRET` — необязательный секрет для входящих партнёрских postback-запросов. Пока он не задан, `POST /postback` отключён.
- `WELCOME_VIDEO_PATH` — необязательный путь к локальному видео приветствия.

Не публикуйте токен бота, postback-секрет или другие секреты в GitHub и не отправляйте их в чат.

## Основные возможности

- Telegram-бот и Mini App с категориями и карточками офферов.
- Администрирование офферов, ссылок, GEO, баннеров и рекламных кампаний.
- SQLite-хранилище; при использовании Railway Volume задайте `DB_PATH` на постоянном диске (например, `/data/bonus_up.db`).
- Tracking-переходы через серверные маршруты для офферов, dual-ссылок, баннеров и рекламных кампаний.
- Атрибуция кликов: уникальный `click_id`, источник и UTM-поля.
- Административная аналитика кликов и событий конверсии.
- Защищённый необязательный endpoint `POST /postback`.
- Служебные маршруты: `/health`, `/terms`, `/privacy`.

## Как работает click tracking

Переходы через tracking-маршруты записываются в таблицу `clicks`. Если партнёрская ссылка содержит буквальный плейсхолдер `{click_id}`, бот заменяет его сгенерированным ID перед перенаправлением.

Важно: партнёрская сеть должна поддерживать передачу sub-ID/click ID и возвращать тот же ID в postback. Не добавляйте произвольные параметры в партнёрские ссылки без проверки документации конкретной сети. Реальная передача click ID и получение конверсий не считаются проверенными, пока это не протестировано с кабинетом соответствующего партнёра.

### TonPlay GET postback (prepared endpoint)

The optional GET endpoint is `/postback/tonplay`. It is disabled until Railway has a `TONPLAY_POSTBACK_SECRET` variable. It expects `key`, `click_id`, and a unique `event_id` or `transaction_id`; optional fields are `event` or `status`, `amount` or `payout`, and `currency`. Example template:

```text
https://bonus-up-bot-production.up.railway.app/postback/tonplay?key=REPLACE_WITH_SECRET&click_id={click_id}&transaction_id={transaction_id}&event=registration
```

**Do not paste this template into TonPlay until its manager/cabinet confirms the exact supported macros and whether it sends GET callbacks.** The secret is carried in the query string because GET-only callback settings often cannot set headers; query secrets may appear in provider request logs. Use a dedicated long random secret, not the bot token or `POSTBACK_SECRET`. If TonPlay supports custom headers or POST JSON, prefer the authenticated `POST /postback` endpoint instead.

### Формат postback

Запрос отправляется методом POST на `/postback`, с заголовком `X-Postback-Secret: <ваш секрет>` и JSON-телом:

```json
{
  "click_id": "ID_клика_из_ссылки",
  "partner": "идентификатор_партнёра",
  "event_id": "уникальный_ID_события_у_партнёра",
  "event": "conversion",
  "amount": 12.5,
  "currency": "EUR"
}
```

Обязательны `click_id`, `partner` и уникальный `event_id`. Повторная отправка того же события от того же партнёра считается дубликатом. Секрет должен быть случайным и храниться только в переменных окружения. Подключайте postback лишь после сверки формата с документацией партнёрской программы.

## Ограничения аналитики

- Старые строки кликов сохраняются после миграции, но у исторических записей нет нового click ID и UTM-атрибуции.
- Клик подтверждает только переход через tracking-ссылку; он сам по себе не доказывает регистрацию, депозит или доход.
- Подпись в таблице кликов может быть названием оффера/баннера/кампании, а не техническим ID партнёрской сети. Поэтому postback не сравнивает поле `partner` с этой подписью: такое сравнение ошибочно отклоняло бы реальные конверсии. Для приёма postback обязательны секрет и известный `click_id`; значение `partner` из postback сохраняется для отчётности. Используйте отдельный секрет на каждую партнёрскую сеть, если сети должны быть изолированы друг от друга.
- Ручные записи дохода и автоматические события конверсии — разные источники данных; сравнивайте их по валютам и не суммируйте разные валюты напрямую.

## Проверки перед production

Перед применением изменений на рабочем боте проверьте запуск на копии базы, миграции SQLite, переходы из Mini App, корректность ссылок и postback с тестовыми данными. Этот README не является подтверждением прохождения автоматических или end-to-end тестов.
