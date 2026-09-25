"""Хендлеры результатов (блок 5): репорт скринами → OCR → финализация сразу,
оспаривание, судья, ручной ввод (план 05 раздел 2 + решение 09)."""
import asyncio
import hashlib
import html
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

import core
import db as appdb
import league
import ocr
import report_match
import results
import settings as appsettings

log = logging.getLogger("bot.results")


# ===== helpers =====

def _club_name(c, club_id: int | None) -> str:
    if not club_id:
        return "—"
    row = c.execute("SELECT name FROM clubs WHERE id=?", (club_id,)).fetchone()
    return row["name"] if row else f"#{club_id}"


def _goal_events_from_text(text: str) -> list[dict]:
    """«Antony x2, Wirtz», «Antony 2, Wirtz» или «Antony, Antony, Wirtz» → события (без минут)."""
    events = []
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(.*?)(?:\s*[x×]\s*|\s+)(\d{1,2})$", part)
        if m:
            events += [{"side": "home", "name": m.group(1).strip(), "minute": None}
                       for _ in range(int(m.group(2)))]
        else:
            events.append({"side": "home", "name": part, "minute": None})
    return events


def _parse_extras(args: list[str]) -> tuple[int | None, int | None, list[dict]]:
    """Хвост команды «pens=5:4 goals=Имя x2, Имя Фамилия» (пен=/голы= тоже можно) → (пен1, пен2, голы).
    context.args режет по пробелам, поэтому голы собираем из всего хвоста и делим по запятым."""
    text = " ".join(args)
    p1 = p2 = None
    pm = re.search(r"(?:пен|pens)=\s*(\d{1,2}\s*[:\-–]\s*\d{1,2})", text)
    if pm:
        p1, p2 = _parse_score(pm.group(1))
    gm = re.search(r"(?:голы|goals)=(.*?)(?=\s+(?:пен|pens)=|$)", text)
    goals = _goal_events_from_text(gm.group(1)) if gm else []
    return p1, p2, goals


async def _notify_player(bot, telegram_id: int | None, text: str, kb=None) -> None:
    if not telegram_id:
        return
    try:
        await bot.send_message(telegram_id, text, reply_markup=kb)
    except Exception:
        log.warning("не доставил ЛС %s", telegram_id)


async def _notify_judges(bot, tournament_id: int, text: str) -> None:
    import config
    ids = set(config.ADMIN_IDS)
    c = appdb.db()
    for r in c.execute("SELECT telegram_id FROM tournament_admins WHERE tournament_id=?",
                       (tournament_id,)).fetchall():
        ids.add(r["telegram_id"])
    c.close()
    for tid in ids:
        try:
            await bot.send_message(tid, text)
        except Exception:
            pass


# ===== 📨 Репорт =====
# Поток: игрок кидает 1–3 скрина (альбомом или подряд) → копим пачку REPORT_WAIT_SEC →
# один прогон OCR → матч и сторона по никам FC27 (report_match) → финализация сразу
# (решение 09) или уточнение кнопками, если ники не узнаны.

REPORT_MAX_SHOTS = 3
REPORT_WAIT_SEC = 4

REPORT_HOWTO = (
    "📨 Как прислать результат:\n"
    "1. После матча открой экран «Статистика матча» — шапка с никами и счётом должна быть видна целиком.\n"
    "2. Если нужны бомбардиры — добавь экран с лентой голов (до 3 скринов).\n"
    "3. Кинь скрины сюда одним альбомом. Обрезать и выбирать матч не нужно: "
    "бот сам найдёт матч по никам FC27 и поймёт, кто хозяин.\n\n"
    "Важно: ник в боте (/nick) должен совпадать с ником в игре."
)

_STAGE_RU = {"group": "группа", "r16": "1/8", "qf": "1/4", "sf": "1/2", "final": "финал"}


def _my_pending_matches(telegram_id: int) -> list[dict]:
    player = core.get_player(telegram_id)
    club = core.club_of_player(player["id"]) if player else None
    if not club:
        return []
    c = appdb.db()
    rows = report_match.reportable_matches(c, club["id"])
    c.close()
    return rows


def _match_button_text(c, m: dict) -> str:
    where = f"тур {m['tour_number']}" if m.get("tour_number") is not None else _STAGE_RU.get(m.get("stage"), "кубок")
    return f"{_club_name(c, m['home_club_id'])} — {_club_name(c, m['away_club_id'])} · {where}"


async def menu_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    matches = _my_pending_matches(update.effective_user.id)
    player = core.get_player(update.effective_user.id)
    nick = player["game_nickname"] if player else None
    text = REPORT_HOWTO + f"\n\nТвой ник: {nick or '❗ не задан — /nick ТвойНик'}"
    if not matches:
        await update.message.reply_text(text + "\n\nНесыгранных матчей у тебя сейчас нет.")
        return
    c = appdb.db()
    lines = [f"• {_match_button_text(c, m)}" for m in matches[:8]]
    c.close()
    await update.message.reply_text(text + "\n\nТвои несыгранные матчи:\n" + "\n".join(lines))


async def cb_pick_report_match(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Выбор матча после скринов, если по никам не определился."""
    q = update.callback_query
    await q.answer()
    mid = int(q.data.split(":")[1])
    pending = context.user_data.get("report_parsed")
    if not pending:
        await q.edit_message_text("Скрины устарели — пришли их ещё раз.")
        return
    side = report_match.side_for_match(pending, mid)
    if side is None:
        await _ask_side(q.message.chat_id, context, mid, edit=q)
        return
    await q.edit_message_text("Матч выбран, финализирую…")
    await _finalize_report(context, q.message.chat_id, update.effective_user.id, mid, side)


async def cb_pick_side(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    _, mid, sw = q.data.split(":")
    if not context.user_data.get("report_parsed"):
        await q.edit_message_text("Скрины устарели — пришли их ещё раз.")
        return
    await q.edit_message_text("Принято, финализирую…")
    await _finalize_report(context, q.message.chat_id, update.effective_user.id, int(mid), sw == "1")


async def _ask_side(chat_id: int, context, mid: int, edit=None) -> None:
    parsed = context.user_data["report_parsed"]
    c = appdb.db()
    m = dict(c.execute("SELECT * FROM matches WHERE id=?", (mid,)).fetchone())
    home, away = _club_name(c, m["home_club_id"]), _club_name(c, m["away_club_id"])
    c.close()
    left = parsed.get("player_home") or "слева"
    right = parsed.get("player_away") or "справа"
    score = f"{parsed['score_home']}:{parsed['score_away']}"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{left} = {home}", callback_data=f"repside:{mid}:0")],
        [InlineKeyboardButton(f"{left} = {away}", callback_data=f"repside:{mid}:1")],
    ])
    text = (f"На скрине: {left} {score} {right}.\n"
            f"Матч: {home} — {away}. Кто слева на скрине?\n"
            "(Чтобы не спрашивал — поставь ник как в игре: /nick)")
    if edit:
        await edit.edit_message_text(text, reply_markup=kb)
    else:
        await context.bot.send_message(chat_id, text, reply_markup=kb)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Копим пачку скринов; обработка — через REPORT_WAIT_SEC после последнего."""
    msg = update.message
    if msg.photo:
        file_id = msg.photo[-1].file_id
    elif msg.document and (msg.document.mime_type or "").startswith("image/"):
        file_id = msg.document.file_id
    else:
        return
    batch = context.user_data.setdefault("report_batch", [])
    if len(batch) >= REPORT_MAX_SHOTS:
        if not context.user_data.get("report_overflow_warned"):
            context.user_data["report_overflow_warned"] = True
            await msg.reply_text(f"Беру первые {REPORT_MAX_SHOTS} скрина, остальные пропускаю.")
        return
    batch.append(file_id)
    context.user_data["report_chat"] = msg.chat_id

    ack = context.user_data.get("report_ack")
    if ack is None:
        sent = await msg.reply_text("📥 Скрин принят, жду остальные…")
        context.user_data["report_ack"] = sent.message_id
    else:
        try:
            await context.bot.edit_message_text(f"📥 Принято скринов: {len(batch)}",
                                                chat_id=msg.chat_id, message_id=ack)
        except Exception:
            pass

    jq = context.job_queue
    if jq is None:  # без job-queue (тесты) — сразу
        await _process_batch(context, msg.chat_id, update.effective_user.id)
        return
    name = f"report:{update.effective_user.id}"
    for job in jq.get_jobs_by_name(name):
        job.schedule_removal()
    jq.run_once(_job_process_batch, REPORT_WAIT_SEC, name=name,
                chat_id=msg.chat_id, user_id=update.effective_user.id)


async def _job_process_batch(context: ContextTypes.DEFAULT_TYPE) -> None:
    await _process_batch(context, context.job.chat_id, context.job.user_id)


async def _process_batch(context, chat_id: int, user_id: int, reuse_images: bool = False) -> None:
    ud = context.user_data
    if reuse_images:
        images = ud.get("report_images") or []
    else:
        file_ids = ud.pop("report_batch", [])
        ud.pop("report_overflow_warned", None)
        ud["ocr_skip"] = 0
        images = []
        for fid in file_ids:
            f = await context.bot.get_file(fid)
            images.append(bytes(await f.download_as_bytearray()))
    ack = ud.pop("report_ack", None)
    if not images:
        return

    # дедуп: уже засчитанные скрины (любого матча) повторно не принимаем
    shas = [hashlib.sha256(b).hexdigest() for b in images]
    c = appdb.db()
    seen = {r["sha256"] for r in c.execute(
        f"SELECT sha256 FROM processed_screenshots WHERE sha256 IN ({','.join('?' * len(shas))})",
        shas).fetchall()}
    c.close()
    fresh = [(s, b) for s, b in zip(shas, images) if s not in seen]
    if not fresh:
        await context.bot.send_message(chat_id, "Эти скрины уже засчитаны раньше. Если это новый матч — пришли свежие.")
        return
    ud["report_images"] = [b for _, b in fresh]
    ud["report_shas"] = [s for s, _ in fresh]

    status_text = f"⏳ Распознаю {len(fresh)} скрин(а)…"
    if ack:
        try:
            await context.bot.edit_message_text(status_text, chat_id=chat_id, message_id=ack)
        except Exception:
            await context.bot.send_message(chat_id, status_text)
    else:
        await context.bot.send_message(chat_id, status_text)

    skip = ud.get("ocr_skip", 0)
    cascade = ocr.build_cascade()
    total = len(cascade)
    cascade = cascade[skip:] if skip < total else []
    parsed = await asyncio.to_thread(ocr.parse_screenshots, ud["report_images"], cascade) if cascade else None
    if not parsed:
        if skip + 1 < total:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Другой моделью", callback_data="ocralt")]])
            await context.bot.send_message(chat_id, "⚠️ Не распознал. Попробовать следующей моделью?", reply_markup=kb)
        else:
            await context.bot.send_message(
                chat_id,
                "⚠️ Распознать не вышло (или не настроены OCR-ключи). Введи вручную:\n"
                "/manual <id матча> <счёт> [goals=Имя x2, Имя]\nНапример: /manual 41 2:1 goals=Антони x2, Виртц\n"
                "id матча — в /calendar.")
        return
    ud["report_parsed"] = parsed

    res = report_match.resolve(parsed, user_id)
    if res["status"] == "error":
        await context.bot.send_message(chat_id, "⛔ " + res["message"])
        return
    if res["status"] == "auto":
        await _finalize_report(context, chat_id, user_id, res["match_id"], res["swapped"])
        return
    if res["status"] == "ask_side":
        await context.bot.send_message(chat_id, "🤔 " + res["message"])
        await _ask_side(chat_id, context, res["match_id"])
        return
    c = appdb.db()
    rows = [dict(c.execute("SELECT * FROM matches WHERE id=?", (mid,)).fetchone()) for mid in res["candidates"]]
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(_match_button_text(c, m), callback_data=f"report:{m['id']}")]
                               for m in rows])
    c.close()
    await context.bot.send_message(
        chat_id,
        f"🤔 {res['message']}\nСчёт на скрине: {parsed.get('player_home') or '?'} "
        f"{parsed['score_home']}:{parsed['score_away']} {parsed.get('player_away') or '?'}.\nКакой это матч?",
        reply_markup=kb)


async def _finalize_report(context, chat_id: int, reporter_tg: int, match_id: int, swapped: bool) -> None:
    ud = context.user_data
    parsed = ud.get("report_parsed")
    if not parsed:
        await context.bot.send_message(chat_id, "Скрины устарели — пришли их ещё раз.")
        return
    c = appdb.db()
    m = c.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
    c.close()
    if not m or m["status"] not in ("pending", "reported"):
        await context.bot.send_message(chat_id, "Матч уже финализирован. Не согласен — «⚔️ Оспорить» у сообщения о результате.")
        return
    m = dict(m)
    player = core.get_player(reporter_tg)
    club = core.club_of_player(player["id"]) if player else None
    if not (club and club["id"] in (m["home_club_id"], m["away_club_id"])) \
            and not core.is_tournament_admin(m["tournament_id"], reporter_tg):
        await context.bot.send_message(chat_id, "⛔ Это не твой матч. Репорт шлёт участник или судья.")
        return

    o = report_match.orient(parsed, swapped)
    pens = o.get("penalties") or {}
    summary = results.finalize_match(
        match_id, o["score_home"], o["score_away"], pens.get("home"), pens.get("away"),
        o.get("goal_events") or [], actor="ocr", players=o.get("players") or [],
    )
    try:
        import league_stats
        imgs = ud.get("report_images") or []
        if imgs:
            league_stats.save_screenshot(match_id, imgs[0])
    except Exception:
        log.exception("скрин матча %s не сохранён", match_id)
    c = appdb.db()
    for sha in ud.get("report_shas") or []:
        c.execute(
            "INSERT INTO processed_screenshots (sha256, tournament_id, match_id, reporter_id) VALUES (?,?,?,?) "
            "ON CONFLICT DO NOTHING", (sha, m["tournament_id"], match_id, reporter_tg))
    c.commit()
    home = _club_name(c, summary["home_club"])
    away = _club_name(c, summary["away_club"])
    where = report_match.match_context(c, m)
    opp_club = summary["away_club"] if club and club["id"] == summary["home_club"] else summary["home_club"]
    row = c.execute(
        "SELECT p.telegram_id FROM club_players cp JOIN players p ON p.id=cp.player_id WHERE cp.club_id=?",
        (opp_club,)).fetchone()
    opp_tg = row["telegram_id"] if row else None
    judges = [r["telegram_id"] for r in c.execute(
        "SELECT telegram_id FROM tournament_admins WHERE tournament_id=?", (summary["tournament_id"],)).fetchall()]
    c.close()
    for k in ("report_parsed", "report_images", "report_shas", "ocr_skip"):
        ud.pop(k, None)

    providers = ", ".join(w.split("провайдеры: ")[-1] for w in o.get("warnings", []) if "провайдеры" in w) or "—"
    nicks = f"{o.get('player_home') or '?'} vs {o.get('player_away') or '?'}"
    text = (f"✅ Матч #{match_id} засчитан\n{where}\n"
            f"⚽ {home} {summary['score']}{summary['pens']} {away}\n"
            f"👤 {nicks}\n"
            f"Голов распознано: {len(o.get('goal_events') or [])} · OCR: {providers}")
    extra = [w for w in o.get("warnings", []) if "провайдеры" not in w]
    if extra:
        text += "\n⚠️ " + "; ".join(extra)
    await context.bot.send_message(chat_id, text)

    window = appsettings.setting_int("dispute_window_hours", 24)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⚔️ Оспорить", callback_data=f"disp:{match_id}")]])
    if opp_tg and opp_tg != reporter_tg:
        await _notify_player(context.bot, opp_tg,
                             f"Соперник прислал результат:\n{where}\n⚽ {home} {summary['score']}{summary['pens']} {away}\n"
                             f"Не согласен? Оспорь в течение {window} ч:", kb)
    for j in judges:
        await _notify_player(context.bot, j, f"Матч #{match_id}: {home} {summary['score']}{summary['pens']} {away} (OCR).")


# ===== оспаривание =====

async def cb_dispute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    _, mid = update.callback_query.data.split(":")
    mid = int(mid)
    msg = results.dispute_match(mid, update.effective_user.id)
    if msg != "ok":
        await update.callback_query.edit_message_text(msg)
        return
    c = appdb.db()
    m = dict(c.execute("SELECT * FROM matches WHERE id=?", (mid,)).fetchone())
    home, away = _club_name(c, m["home_club_id"]), _club_name(c, m["away_club_id"])
    c.close()
    await update.callback_query.edit_message_text(
        f"⚔️ Матч #{mid} ({home} — {away}) помечен «спорный». Ставки на него заморожены. "
        f"Судья решит: /resolve {mid} <счёт>")
    await _notify_judges(
        context.bot, m["tournament_id"],
        f"⚔️ Спор по матчу #{mid}: {home} {m['score1']}:{m['score2']} {away}. "
        f"Реши: /resolve {mid} <счёт> [pens=h:a] [goals=...]")

async def cmd_disputes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c = appdb.db()
    rows = c.execute("SELECT * FROM matches WHERE status='disputed' ORDER BY id").fetchall()
    if not rows:
        c.close()
        await update.message.reply_text("Спорных матчей нет.")
        return
    lines = []
    for m in rows:
        lines.append(f"#{m['id']} {_club_name(c, m['home_club_id'])} — {_club_name(c, m['away_club_id'])} "
                     f"({m['score1']}:{m['score2']})")
    c.close()
    await update.message.reply_text("\n".join(lines))


def _parse_score(s: str) -> tuple[int, int]:
    m = re.match(r"^(\d{1,2})\s*[:\-–]\s*(\d{1,2})$", s.strip())
    if not m:
        raise ValueError("счёт вида 2:1")
    return int(m.group(1)), int(m.group(2))


def _is_judge_of(update: Update, tournament_id: int) -> bool:
    return core.is_tournament_admin(tournament_id, update.effective_user.id)


async def cmd_resolve_dispute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/resolve <match_id> <счёт> [pens=h:a] [goals=...]"""
    if len(context.args or []) < 2:
        await update.message.reply_text("Формат: /resolve <матч> <счёт> [pens=5:4] [goals=Имя x2, Имя]")
        return
    if not context.args[0].isdigit():
        await update.message.reply_text("id матча — число.")
        return
    mid = int(context.args[0])
    c = appdb.db()
    m = c.execute("SELECT * FROM matches WHERE id=?", (mid,)).fetchone()
    c.close()
    if not m:
        await update.message.reply_text("Матч не найден.")
        return
    if not _is_judge_of(update, m["tournament_id"]):
        await update.message.reply_text("⛔ Только судья турнира.")
        return
    try:
        s1, s2 = _parse_score(context.args[1])
        p1, p2, goals = _parse_extras(context.args[2:])
    except ValueError as e:
        await update.message.reply_text(f"Не разобрал: {e}")
        return
    summary = results.resolve_dispute(mid, s1, s2, p1, p2, goals, update.effective_user.id)
    c = appdb.db()
    await update.message.reply_text(
        f"✅ Спор решён: {_club_name(c, summary['home_club'])} {summary['score']}{summary['pens']} "
        f"{_club_name(c, summary['away_club'])}. Купоны разморожены, ставки пересчитаны.")
    c.close()


# ===== ручной ввод =====

async def cmd_manual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/manual <match_id> <счёт> [pens=h:a] [goals=Имя x2, Имя]"""
    if len(context.args or []) < 2:
        await update.message.reply_text(
            "Формат: /manual <матч> <счёт> [pens=5:4] [goals=Имя x2, Имя]\n"
            "Голы — авторы из состава твоего клуба и соперника, через запятую.")
        return
    if not context.args[0].isdigit():
        await update.message.reply_text("id матча — число.")
        return
    mid = int(context.args[0])
    c = appdb.db()
    m = c.execute("SELECT * FROM matches WHERE id=?", (mid,)).fetchone()
    c.close()
    if not m:
        await update.message.reply_text("Матч не найден.")
        return
    m = dict(m)
    player = core.get_player(update.effective_user.id)
    club = core.club_of_player(player["id"]) if player else None
    is_owner = club and club["id"] in (m["home_club_id"], m["away_club_id"])
    is_judge = _is_judge_of(update, m["tournament_id"])
    if not (is_owner or is_judge):
        await update.message.reply_text("⛔ Ты не участник матча и не судья.")
        return
    if m["status"] == "cancelled":
        await update.message.reply_text("Матч отменён — вносить нечего.")
        return
    # владелец вносит только несыгранный матч; правка итога — судья/root (с пересчётом ставок)
    if m["status"] not in ("pending", "reported") and not is_judge:
        await update.message.reply_text(
            "⛔ Матч уже финализирован. Не согласен — «⚔️ Оспорить», исправит судья.")
        return
    try:
        s1, s2 = _parse_score(context.args[1])
        p1, p2, goals = _parse_extras(context.args[2:])
    except ValueError as e:
        await update.message.reply_text(f"Не разобрал: {e}")
        return
    # имена без стороны: раскладываем по составам клубов
    c = appdb.db()

    def roster_names(club_id):
        if not club_id:
            return set()
        return {r["name"].lower() for r in c.execute(
            "SELECT name FROM club_cards WHERE club_id=?", (club_id,)).fetchall()}

    home_names, away_names = roster_names(m["home_club_id"]), roster_names(m["away_club_id"])
    c.close()
    for g in goals:
        n = g["name"].lower()
        if n in home_names:
            g["side"] = "home"
        elif n in away_names:
            g["side"] = "away"
    summary = results.finalize_match(mid, s1, s2, p1, p2, goals, actor="manual")
    c = appdb.db()
    await update.message.reply_text(
        f"✅ Внесено вручную: {_club_name(c, summary['home_club'])} {summary['score']}{summary['pens']} "
        f"{_club_name(c, summary['away_club'])}")
    c.close()


async def cb_ocr_alternate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«🔄 Другой моделью» (план 06): те же скрины — со следующего провайдера, пересылать не нужно."""
    q = update.callback_query
    await q.answer()
    if not context.user_data.get("report_images"):
        await q.edit_message_text("Скрины устарели — пришли их ещё раз.")
        return
    context.user_data["ocr_skip"] = context.user_data.get("ocr_skip", 0) + 1
    await q.edit_message_text("🔄 Пробую следующей моделью…")
    await _process_batch(context, q.message.chat_id, update.effective_user.id, reuse_images=True)


HANDLERS = [
    ("text", "📨 Репорт", menu_report),
    ("callback", r"^ocralt$", cb_ocr_alternate),
    ("callback", r"^report:\d+$", cb_pick_report_match),
    ("callback", r"^repside:\d+:[01]$", cb_pick_side),
    ("photo", None, on_photo),
    ("callback", r"^disp:\d+$", cb_dispute),
    ("command", "disputes", cmd_disputes),
    ("command", "resolve", cmd_resolve_dispute),
    ("command", "manual", cmd_manual),
]
