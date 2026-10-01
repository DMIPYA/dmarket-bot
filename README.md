# DMarket Auto-Sell Bot

Асинхронный Python-бот: следит за статусом предметов в инвентаре DMarket (по умолчанию —
Dreams & Nightmares Case), при снятии trade-lock мгновенно уведомляет в Telegram и
(опционально) сразу выставляет предмет на продажу через `POST /marketplace-api/v2/offers:batchCreate`.
Деплой — на Koyeb как worker.

## Структура

- `bot.py` — точка входа: asyncio-петля мониторинга (5 c) + Telegram-команды
- `dmarket.py` — клиент DMarket API: подпись HMAC-SHA256, запросы httpx, backoff на 429
- `config.py` — переменные окружения через pydantic-settings
- `requirements.txt`, `Procfile`, `.env.example`

## Команды Telegram (только из чата TELEGRAM_CHAT_ID)

- `/status` — сколько кейсов доступно, лучший ask, состояние автопродажи
- `/sell` — вручную выставить все доступные кейсы на продажу

## Локальный запуск

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows; Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # заполните ключи
python bot.py
```

Самопроверка модуля подписи (без сети): `python dmarket.py`

## Загрузка на GitHub

```bash
git init
git add .
git commit -m "DMarket auto-sell bot"
git branch -M main
git remote add origin https://github.com/<ваш-логин>/dmarket-bot.git
git push -u origin main
```

`.env` в git не попадает (`.gitignore`).

## Запуск на Koyeb

1. Koyeb → **Create App** → источник: GitHub-репозиторий.
2. Builder: **Buildpack** — процесс `worker` подхватится из `Procfile`.
   Если воркер не определился — задайте Run Command вручную: `python bot.py`.
3. Вкладка **Environment**: добавьте переменные из `.env.example`
   (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DMARKET_PUBLIC_KEY`, `DMARKET_SECRET_KEY`, при желании `AUTO_SELL`).
4. **Deploy**. Логи — во вкладке Logs; о запуске бот пришлёт «🚀 Бот запущен» в Telegram.

## Заметки

- Подпись запросов — HMAC-SHA256 (базовые ключи DMarket; `hmac` из stdlib, Ed25519 не требуется).
- Цена автопродажи: лучший ask стакана − $0.01 (фронт книги → продажа за секунды).
- 429 обрабатывается экспоненциальным backoff (1/2/4/…60 c) внутри `dmarket.py`.
- `AUTO_SELL=false` по умолчанию: включайте, только если готовы к реальной продаже за деньги.
