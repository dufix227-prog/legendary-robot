"""Хендлеры турнирного ядра (блок 4): сезоны, клубы, календарь, туры, таблицы.

Root-команды: /season /cup /cupnext /club /clubs /calendar /tour /tournaments /pairs /editpair /judge.
Всем: /bracket — сетка кубка.
Кнопки меню flesh: 🏆 Турниры, ⚽ Мой Клуб.
"""
import logging
import re
from datetime import datetime

from telegram import Update
from telegram.ext import ContextTypes

import config
import core
import db as appdb
import league
from clubs_catalog import FORMAT_NAMES, FORMAT_BY_INPUT, find_club, logo_path_for

log = logging.getLogger("bot.tournament")


# ключи параметров: английские в подсказках, русские (старые) тоже принимаются
_OPT_ALIASES = {"divisions": "дивизионы", "mode": "режим", "rounds": "круги", "days": "дней",
                "format": "формат", "clubs": "клубы", "division": "дивизион", "div": "дивизион"}


def _parse_args(text: str) -> tuple[str, dict]:
    """/season Season-1 divisions=2 rounds=2 → ('Season-1', {'дивизионы': '2', ...})"""
    parts = text.split()
    name_parts, opts = [], {}
    for p in parts:
        if "=" in p:
            k, v = p.split("=", 1)
            k = _OPT_ALIASES.get(k.lower(), k.lower())
            opts[k] = "все" if (k == "клубы" and v.lower() == "all") else v
        else:
            name_parts.append(p)
    return " ".join(name_parts), opts


def _resolve_player(query: str) -> dict | None:
    """@username | telegram_id → players-строка."""
    q = query.strip().lstrip("@")
    c = appdb.db()
    if q.isdigit():
        row = c.execute("SELECT * FROM players WHERE telegram_id=?", (int(q),)).fetchone()
    else:
        row = c.execute("SELECT * FROM players WHERE username=?", (query.strip().lstrip("@"),)).fetchone()
    c.close()
    return dict(row) if row else None


def _root_only(update: Update) -> bool:
    if not core.is_root(update.effective_user.id):
        return False
    return True


async def deny(update: Update):
    await update.message.reply_text("⛔ Только для root.")


# ===== /season =====

_DIVS_RE = re.compile(r"(?:divs|divisions|дивизионы)=(.+?)(?=\s+[\wа-яё]+=|$)", re.IGNORECASE)


async def cmd_season(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    import league_admin as la
    if not _root_only(update):
        return await deny(update)
    text = context.args and " ".join(context.args) or ""
    # дивизионы с пробелами в названиях («Ла Лига, Серия А») вырезаем до разбора key=value
    m = _DIVS_RE.search(text)
    divs_raw = m.group(1).strip() if m else ""
    name, opts = _parse_args(_DIVS_RE.sub(" ", text))
    if not name:
        await update.message.reply_text(
            "Формат: /season <название> [divisions=N | divisions=Имя, Имя…] [rounds=1|2] [days=N] [promote=N]\n"
            "Примеры:\n/season Сезон-1 divisions=2\n"
            "/season Сезон-1 divisions=Ла Лига, Серия А, Лига 1, Лига 2, Серия С promote=3\n"
            "promote — сколько клубов меняются между дивизионами (0 — независимые лиги).\n"
            "Удобнее — в мини-аппе: Кабинет → 👮 → 🏆 Лиги и участники."
        )
        return
    names = ([f"Дивизион {i}" for i in range(1, int(divs_raw) + 1)] if divs_raw.isdigit()
             else la.parse_division_names(divs_raw)) or ["Дивизион 1"]
    rounds = int(opts.get("круги", 2)) if str(opts.get("круги", 2)).isdigit() else 2
    days = int(opts["дней"]) if str(opts.get("дней", "")).isdigit() else config.DEFAULT_TOUR_DAYS
    promote = opts.get("promote", opts.get("вылет", "3"))
    try:
        tid = la.create_league(name, names, rounds, days, int(promote) if str(promote).isdigit() else 3)
    except la.LeagueError as e:
        await update.message.reply_text(f"⛔ {e}")
        return
    core.audit(tid, update.effective_user.id, "create_season", f"{name} div={names} rounds={rounds} days={days}")
    div_lines = "\n".join(f"{i}. {n}" for i, n in enumerate(names, 1))
    await update.message.reply_text(
        f"✅ Сезон «{name}» (#{tid}) создан, кругов {rounds}, тур {days} дн.\n{div_lines}\n\n"
        f"Клубы: /club <игрок> <клуб> division=N (N — номер из списка), "
        f"затем календарь: /calendar {tid}"
    )


# ===== /cup =====

async def cmd_cup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _root_only(update):
        return await deny(update)
    name, opts = _parse_args(context.args and " ".join(context.args) or "")
    if not name:
        await update.message.reply_text(
            "Формат: /cup <название> format=ucl|uel|uecl [clubs=all|1,2,3] [division=N] [days=N]\n"
            "(ucl = ЛЧ, uel = ЛЕ, uecl = ЛК)"
        )
        return
    fmt = FORMAT_BY_INPUT.get(opts.get("формат", "лч").lower())
    if not fmt:
        await update.message.reply_text("format=ucl|uel|uecl (ЛЧ/ЛЕ/ЛК)")
        return
    days = int(opts.get("дней", config.DEFAULT_TOUR_DAYS))
    tid = league.create_cup(name, fmt, "manual", days)

    c = appdb.db()
    if opts.get("клубы", "все") == "все" and "дивизион" not in opts:
        rows = c.execute("SELECT id FROM clubs WHERE owner_player_id IS NOT NULL").fetchall()
    elif "дивизион" in opts:
        rows = c.execute(
            "SELECT id FROM clubs WHERE division_id=? AND owner_player_id IS NOT NULL",
            (int(opts["дивизион"]),),
        ).fetchall()
    else:
        ids = [int(x) for x in re.split("[,; ]+", opts.get("клубы", "")) if x.isdigit()]
        rows = [{"id": i} for i in ids]
    c.close()
    club_ids = [r["id"] for r in rows]
    if len(club_ids) < 2:
        await update.message.reply_text(f"Кубок «{name}» создан (#{tid}), но участников <2 — сетка не сгенерирована.")
        return
    ties = league.create_cup_bracket(tid, club_ids)
    core.audit(tid, update.effective_user.id, "create_cup", f"{name} {fmt} participants={len(club_ids)} ties={len(ties)}")
    await update.message.reply_text(
        f"✅ Кубок «{name}» ({FORMAT_NAMES[fmt]}): участников {len(club_ids)}, "
        f"стадия {ties[0]['stage']}, серий {len(ties)}. Серии до 2 побед."
    )


# ===== /cupnext /bracket =====

def _cups(active_only: bool = True) -> list[dict]:
    c = appdb.db()
    sql = "SELECT * FROM tournaments WHERE format!='league'"
    if active_only:
        sql += " AND stage!='finished'"
    rows = [dict(r) for r in c.execute(sql + " ORDER BY id").fetchall()]
    c.close()
    return rows


async def cmd_cup_next(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cupnext <турнир_id> — принудительно провести следующую стадию (обычно это
    делается само после решающей игры серии). Идемпотентно."""
    if not _root_only(update):
        return await deny(update)
    args = context.args or []
    if not args or not args[0].isdigit():
        cups = _cups()
        hint = "\n".join(f"#{t['id']} «{t['name']}»" for t in cups) or "Активных кубков нет."
        await update.message.reply_text(f"Формат: /cupnext <турнир_id>\n{hint}")
        return
    tid = int(args[0])
    res = league.sync_cup(tid, actor_tg=update.effective_user.id)
    if not res.get("ok"):
        await update.message.reply_text(f"⚠️ {res.get('error', 'не кубок')}")
        return
    parts = []
    if res["created"]:
        parts.append(f"создано серий: {len(res['created'])}")
    if res["rebuilt"]:
        parts.append(f"пересобрано пар: {len(res['rebuilt'])}")
    if res["removed"]:
        parts.append(f"снято серий: {len(res['removed'])}")
    if res["finished"]:
        club = league.get_club(res["finished"])
        parts.append(f"кубок завершён, победитель — {club['name'] if club else res['finished']}")
    if not parts:
        c = appdb.db()
        open_ties = c.execute(
            "SELECT COUNT(*) n FROM ties WHERE tournament_id=? AND winner_club_id IS NULL", (tid,)).fetchone()["n"]
        c.close()
        parts.append(f"без изменений: не решено серий — {open_ties}" if open_ties else "без изменений")
    core.audit(tid, update.effective_user.id, "cup_next", "; ".join(parts))
    text = "🏆 " + "; ".join(parts)
    if res["warnings"]:
        text += "\n⚠️ " + "\n⚠️ ".join(res["warnings"])
    await update.message.reply_text(text + "\n\n" + league.format_bracket(tid))


async def cmd_bracket(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/bracket [турнир_id] — сетка кубка текстом (без id — все активные кубки)."""
    args = context.args or []
    if args and args[0].isdigit():
        ids = [int(args[0])]
    else:
        ids = [t["id"] for t in _cups()]
    if not ids:
        await update.message.reply_text("Активных кубков нет. Формат: /bracket <турнир_id>")
        return
    for tid in ids[:5]:
        text = league.format_bracket(tid)
        # лимит Telegram — 4096 символов
        for i in range(0, len(text), 4000):
            await update.message.reply_text(text[i:i + 4000])


# ===== /club =====

async def cmd_club(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _root_only(update):
        return await deny(update)
    args = list(context.args or [])
    if len(args) < 2:
        await update.message.reply_text("Формат: /club <@user|id> <клуб> [division=N]\nПример: /club @vasya Реал Мадрид division=1")
        return
    player = _resolve_player(args[0])
    if not player:
        await update.message.reply_text("Игрок не найден. Пусть сначала нажмёт /start в боте.")
        return
    club_query, opts = _parse_args(" ".join(args[1:]))
    found = find_club(club_query)
    if not found:
        await update.message.reply_text("Клуб не найден в каталоге FC (80 команд). Смотри /catalog.")
        return
    canon, logo_file = found

    c = appdb.db()
    row = c.execute("SELECT id FROM clubs WHERE name=?", (canon,)).fetchone()
    c.close()
    if row:
        club_id = row["id"]
    else:
        div_id = None
        if "дивизион" in opts:
            c = appdb.db()
            d = c.execute(
                "SELECT id FROM divisions WHERE tournament_id=(SELECT id FROM tournaments WHERE stage!='finished' AND format='league' ORDER BY id DESC LIMIT 1) "
                "AND sort_order=?", (int(opts["дивизион"]),),
            ).fetchone()
            c.close()
            if d:
                div_id = d["id"]
        club_id = league.create_club(canon, logo_path_for(logo_file), div_id, config.CLUB_START_BUDGET)

    league.assign_club_owner(club_id, player["id"])
    # перезаписать игрока в матчей, если календарь уже генерился
    c = appdb.db()
    c.execute("UPDATE matches SET home_player_id=? WHERE home_club_id=? AND home_player_id IS NULL", (player["id"], club_id))
    c.execute("UPDATE matches SET away_player_id=? WHERE away_club_id=? AND away_player_id IS NULL", (player["id"], club_id))
    c.commit()
    c.close()
    core.audit(None, update.effective_user.id, "issue_club", f"club={canon} player={player['telegram_id']}")
    await update.message.reply_text(
        f"✅ {canon} отдана игроку {player['username'] or player['telegram_id']} "
        f"(бюджет {config.CLUB_START_BUDGET:,} ₼).".replace(",", " ")
    )


async def cmd_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from clubs_catalog import TEAM_LOGO_MAP
    names = " • ".join(sorted(TEAM_LOGO_MAP))
    for chunk_start in range(0, len(names), 3800):
        await update.message.reply_text(names[chunk_start:chunk_start + 3800])


async def cmd_clubs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _root_only(update):
        return await deny(update)
    c = appdb.db()
    rows = c.execute(
        "SELECT cl.id, cl.name, cl.division_id, cl.budget, p.username "
        "FROM clubs cl LEFT JOIN club_players cp ON cp.club_id=cl.id "
        "LEFT JOIN players p ON p.id=cp.player_id ORDER BY cl.division_id, cl.name"
    ).fetchall()
    c.close()
    lines = [f"#{r['id']} {r['name']} — {r['username'] or 'без владельца'} "
             f"(д{r['division_id'] or '—'}, {r['budget']:,})".replace(",", " ") for r in rows]
    await update.message.reply_text("\n".join(lines) if lines else "Клубов нет.")


# ===== /calendar =====

async def cmd_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _root_only(update):
        return await deny(update)
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Формат: /calendar <турнир_id> [division=N]\n(без division=N — все дивизионы)")
        return
    tid = int(context.args[0])
    t = league.get_tournament(tid)
    if not t:
        await update.message.reply_text("Турнир не найден.")
        return
    divs = league.tournament_divisions(tid)
    _, opts = _parse_args(" ".join(context.args[1:]))
    if opts.get("дивизион", "").isdigit():
        divs = [d for d in divs if d["sort_order"] == int(opts["дивизион"])]
    total = 0
    for d in divs:
        total += league.generate_league_calendar(tid, d["id"])
    if total == 0:
        await update.message.reply_text("Календарь не сгенерирован: в дивизионах меньше 2 клубов.")
        return
    core.audit(tid, update.effective_user.id, "generate_calendar", f"matches={total}")
    t = league.get_tournament(tid)  # total_tours появился только что
    await update.message.reply_text(
        f"✅ Календарь «{t['name']}»: туров {t['total_tours']}, матчей {total}. "
        f"Тур 1 открыт (дедлайн {t['tour_days']} дн.). Правка пар — до открытия тура: /editpair <матч> <дом> <гости>"
    )


# ===== /tour =====

async def cmd_open_tour(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _root_only(update):
        return await deny(update)
    if len(context.args or []) < 2 or not context.args[0].isdigit() or not context.args[1].isdigit():
        await update.message.reply_text("Формат: /tour <турнир_id> <номер> [days=N]")
        return
    tid, no = int(context.args[0]), int(context.args[1])
    _, opts = _parse_args(" ".join(context.args[2:]))
    days = int(opts["дней"]) if opts.get("дней", "").isdigit() else None
    ok = league.open_tour(tid, no, days)
    await update.message.reply_text("✅ Тур открыт." if ok else "Турнир не найден.")


# ===== /tournaments, /pairs, /editpair, /judge =====

async def cmd_tournaments(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = []
    for t in league.all_active_tournaments():
        fmt = FORMAT_NAMES.get(t["format"], t["format"])
        lines.append(f"#{t['id']} «{t['name']}» [{fmt}] тур {t['current_tour']}/{t['total_tours']} ({t['tour_mode']})")
    await update.message.reply_text("\n".join(lines) or "Турниров нет.")


async def cmd_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _root_only(update):
        return await deny(update)
    if len(context.args or []) < 2:
        await update.message.reply_text("Формат: /pairs <турнир_id> <тур>")
        return
    text = league.format_matches_for_tour(int(context.args[0]), int(context.args[1]))
    await update.message.reply_text(text)


async def cmd_edit_pair(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _root_only(update):
        return await deny(update)
    if len(context.args or []) < 3 or not all(x.isdigit() for x in context.args[:3]):
        await update.message.reply_text("Формат: /editpair <матч_id> <дом_club_id> <гости_club_id>")
        return
    mid, home, away = int(context.args[0]), int(context.args[1]), int(context.args[2])
    c = appdb.db()
    m = c.execute("SELECT * FROM matches WHERE id=?", (mid,)).fetchone()
    if not m:
        await update.message.reply_text("Матч не найден.")
        c.close()
        return
    tour = league.ensure_tour(m["tournament_id"], m["tour_number"] or 0)
    if tour and tour.get("status") == "open":
        await update.message.reply_text("Тур уже открыт — правка пар запрещена (решение 09).")
        c.close()
        return
    c.execute(
        "UPDATE matches SET home_club_id=?, away_club_id=? WHERE id=? AND status='pending'",
        (home, away, mid),
    )
    c.commit()
    c.close()
    core.audit(m["tournament_id"], update.effective_user.id, "edit_pair", f"match={mid} → {home} vs {away}")
    await update.message.reply_text("✅ Пара обновлена.")


async def cmd_judge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Root: /judge <турнир_id> <@user|id> — выдать/снять (toggle)."""
    if not _root_only(update):
        return await deny(update)
    if len(context.args or []) < 2:
        await update.message.reply_text("Формат: /judge <турнир_id> <@user|id>")
        return
    tid = int(context.args[0])
    player = _resolve_player(context.args[1])
    if not player:
        await update.message.reply_text("Игрок не найден.")
        return
    c = appdb.db()
    row = c.execute(
        "SELECT 1 FROM tournament_admins WHERE tournament_id=? AND telegram_id=?",
        (tid, player["telegram_id"]),
    ).fetchone()
    if row:
        c.execute("DELETE FROM tournament_admins WHERE tournament_id=? AND telegram_id=?",
                  (tid, player["telegram_id"]))
        msg = "снят"
    else:
        c.execute("INSERT OR IGNORE INTO tournament_admins (tournament_id, telegram_id) VALUES (?,?)",
                  (tid, player["telegram_id"]))
        msg = "выдан"
    c.commit()
    c.close()
    core.audit(tid, update.effective_user.id, f"judge_{msg}", str(player["telegram_id"]))
    await update.message.reply_text(f"✅ Судья {msg}.")


# ===== кнопки меню =====

async def menu_tournaments(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = []
    for t in league.all_active_tournaments():
        fmt = FORMAT_NAMES.get(t["format"], t["format"])
        c = appdb.db()
        if t["format"] == "league":
            divs = league.tournament_divisions(t["id"])
            extra = ", ".join(d["name"] for d in divs)
            lines.append(f"🏆 «{t['name']}» [{fmt}] — {extra}; тур {t['current_tour']}/{t['total_tours']}")
        else:
            lines.append(f"🏆 «{t['name']}» [{fmt}] — стадия {t['stage']}")
        c.close()
    player = core.get_player(update.effective_user.id)
    club = core.club_of_player(player["id"]) if player else None
    if club:
        table = league.division_standings(club["division_id"]) if club["division_id"] else []
        if table:
            lines.append("\nТаблица твоего дивизиона:")
            for row in table[:10]:
                name = league.get_club(row["club_id"])["name"]
                lines.append(f"{row['position']}. {name} — {row['points']} очк. ({row['games']} игр, {row['gf']}:{row['ga']})")
    await update.message.reply_text("\n".join(lines) or "Турниров нет — root создаёт командой /season.")


async def menu_my_club(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    player = core.get_player(update.effective_user.id)
    if not player:
        await update.message.reply_text("Сначала /start.")
        return
    club = core.club_of_player(player["id"])
    if not club:
        await update.message.reply_text(
            "Клуба нет. Попроси root выдать клуб командой /club — или отправь заявку в мини-аппе "
            "(«Мой клуб» → «Запросить клуб»)."
        )
        return
    c = appdb.db()
    cards = c.execute("SELECT COUNT(*) AS n FROM club_cards WHERE club_id=?", (club["id"],)).fetchone()["n"]
    c.close()
    await update.message.reply_text(
        f"⚽ {club['name']}\nБюджет: {club['budget']:,} ₼\nСостав: {cards} карт\n"
        f"Форма: {club['form'] or '—'}\nElo: {club['elo']}".replace(",", " ")
    )


HANDLERS = [
    ("command", "season", cmd_season),
    ("command", "cup", cmd_cup),
    ("command", "cupnext", cmd_cup_next),
    ("command", "bracket", cmd_bracket),
    ("command", "club", cmd_club),
    ("command", "catalog", cmd_catalog),
    ("command", "clubs", cmd_clubs),
    ("command", "calendar", cmd_calendar),
    ("command", "tour", cmd_open_tour),
    ("command", "tournaments", cmd_tournaments),
    ("command", "pairs", cmd_pairs),
    ("command", "editpair", cmd_edit_pair),
    ("command", "judge", cmd_judge),
    ("text", "🏆 Турниры", menu_tournaments),
    ("text", "⚽ Мой Клуб", menu_my_club),
]
