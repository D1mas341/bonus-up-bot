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
- `TONPLAY_POSTBACK_SECRET` — отдельный секрет для GET callback TonPlay. Пока он не задан, `GET /postback/tonplay` отключён.
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
- Защищённый необязательный endpoint `GET /postback/tonplay`.
- Служебные маршруты: `/health`, `/terms`, `/privacy`.

## Как работает click tracking

Переходы через tracking-маршруты записываются в таблицу `clicks`. Если партнёрская ссылка содержит буквальный плейсхолдер `{click_id}`, бот заменяет его сгенерированным ID перед перенаправлением.

Важно: партнёрская сеть должна поддерживать передачу sub-ID/click ID и возвращать тот же ID в postback. Не добавляйте произвольные параметры в партнёрские ссылки без проверки документации конкретной сети. Реальная передача click ID и получение конверсий не считаются проверенными, пока это не протестировано с кабинетом соответствующего партнёра.

### NormAffiliates / La Casino GET postback

The La Casino handler uses `GET /postback/norm/lacasino/registration` and `GET /postback/norm/lacasino/deposit`. Configure the Railway variable `NORM_LACASINO_POSTBACK_SECRET` before enabling either callback. The secret is supplied as the `key` query parameter.

Use a dedicated BONUS UP click ID in NormAffiliates `source_id1` (the outgoing tracked link must pass `source_id1={click_id}`, where BONUS UP replaces `{click_id}` with the generated click ID). Do not use the static `source_id` as the click ID. The postback must return `source_id1` and the unique `event_id` for deposit events. The endpoint also accepts `player_currency` or `currency`.

Create separate callback URLs for registration and deposit:

```text
https://bonus-up-bot-production.up.railway.app/postback/norm/lacasino/registration?key=YOUR_NORM_LACASINO_SECRET&source_id1={source_id1}&event_id={event_id}&player_currency={player_currency}
https://bonus-up-bot-production.up.railway.app/postback/norm/lacasino/deposit?key=YOUR_NORM_LACASINO_SECRET&source_id1={source_id1}&event_id={event_id}&deposit_amount={deposit_amount}&player_currency={player_currency}
```

In NormAffiliates, select the corresponding macros in its key selector; exact availability and substitution should be verified with a test event before treating the integration as live. `deposit_amount` is documented in the partner cabinet as the player's cumulative deposits up to callback time, not necessarily the amount of the single deposit. The handler stores it as the amount field, so interpret it as cumulative unless NormAffiliates confirms a per-transaction amount macro. Deposit callbacks without a unique `event_id` are rejected to avoid collapsing repeat deposits. Duplicate partner events are acknowledged without being stored twice.

### TonPlay GET postback

Endpoint `GET /postback/tonplay` принимает `key`, `click_id`, `transaction_id`, `event`, `amount` (или `amount_usd`) и `currency`. Он отключён, пока в Railway не задана отдельная переменная `TONPLAY_POSTBACK_SECRET`. Для события регистрации без ID транзакции используется уникальный ключ из click ID и типа события; для депозитов необходим ID транзакции, чтобы не объединять повторные депозиты.

Согласно документации TonPlay, события передаются через `{action}` (значения `register`, `first_deposit`, `deposit` и другие), уникальный ID транзакции — через `{tx_id}`, сумма в USD — через `{amount_usd}`. Если партнёрская ссылка BONUS UP передаёт собственный ID как параметр `click_id={click_id}`, callback должен вернуть это значение через макрос `{click_id}`.

Шаблон URL для поля Postback в TonPlay (использовать только после развёртывания endpoint и настройки секрета в Railway):

```text
https://bonus-up-bot-production.up.railway.app/postback/tonplay?key=YOUR_TONPLAY_SECRET&click_id={click_id}&transaction_id={tx_id}&event={action}&amount={amount_usd}&currency=USD
```

В партнёрской ссылке TonPlay должен сохраняться плейсхолдер BONUS UP `{click_id}`, который сервер заменяет на ID клика перед перенаправлением. Не меняйте имя этого параметра без согласованного изменения callback. Перед включением убедитесь, что в форме TonPlay выбран нужный тип событий и кабинет допускает GET callback. Секрет передаётся в query string и может попадать в журналы запросов; используйте отдельный случайный секрет, не токен бота и не `POSTBACK_SECRET`.

### Формат стандартного postback

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
