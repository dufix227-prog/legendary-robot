# KURILKA SIGARKI — турнир FC27 + ставки на дым (клон «Логово»)

> Новая сессия? Сначала открой `CONTEXT.md` — там всё состояние проекта.

## Структура
- `bot/` — Telegram-бот (PTB) + сервер мини-аппа в одном процессе (`bot/main.py`)
- `miniapp/` — API мини-аппа (`routes.py`) и фронт (`static/`)
- `tests/` — smoke + гейты (`gate*.py`), тест-сид `seed_data.py`, снос `wipe.py`
- `samples/fc27-screens/` — эталонные скрины FC27 для OCR
- `docs/plans/`, `docs/sessions/` — планы и решения владельца
- `logovo-copy/` — разбор оригинала (фронт, скрины, API-образцы, `OVERVIEW.md`)

## Запуск
Сервер/домашний ПК, туннель, HTTPS, автозапуск, бэкапы — пошагово в **[docs/DEPLOY.md](docs/DEPLOY.md)**
(`deploy/`: systemd-юнит, `install.sh`, `tunnel.sh` для cloudflared, `Caddyfile`, `backup.sh`).

Локально:
```
cd bot && cp .env.example .env     # BOT_TOKEN, ADMIN_IDS
cd .. && python3 -m venv venv && venv/bin/pip install -r bot/requirements.txt
venv/bin/python bot/main.py        # бот + http://127.0.0.1:9542/app
```
Только мини-апп: `venv/bin/python -m miniapp.server`.
`/scan` по ссылке Challenge Place (необязательно): `venv/bin/pip install -r bot/requirements-optional.txt
&& venv/bin/playwright install --with-deps chromium`.
Тесты: `tests/run_all.sh` (SQLite) или `PG_URL=postgresql://user@host:5432 tests/run_all.sh` (Postgres).
Справочник реальных игроков: `venv/bin/python bot/players_directory.py` (или кнопка в админке).

## Админы
- **root** — Telegram ID в `ADMIN_IDS` (`bot/.env`). Свой ID покажет `/myid`. Root сразу админ
  мини-аппа и видит полное меню команд.
- **админ мини-аппа** — root выдаёт `/admin @user` (ставки, void, игроки в Кабинет → 👮).
- **судья турнира** — root выдаёт `/judge <турнир_id> @user` (споры, ручной ввод).

## Команды (root)
`/season` (можно `divisions=Ла Лига, Серия А, Лига 1`) `/setnick` `/cup` `/club` `/clubs` `/catalog` `/calendar` `/tour` `/pairs` `/editpair` `/judge`
`/disputes` `/resolve` `/final` `/promo` `/paid` `/scan` `/admin`.
Игрокам: `/start` `/nick` `/tournaments` `/manual` `/myid`. Результат матча — просто скрины в чат.

## Правила
- Секреты только в `bot/.env` (в git не попадает).
- Тексты для игроков — на русском; код, файлы и команды — латиницей.
