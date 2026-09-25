"""Финализация матчей: Elo, таблица, голы, серии, штрафы, уведомления.

Чистая логика (без telegram) — вызывается ботом (репорт/спор/вручную) и
парсером Challenge Place (блок 9). Ставки рассчитываются хуком
bets_engine.settle_match (появляется в блоке 7; тут вызов под try/except).

Повторная финализация (спор решён судьёй иначе / правка) — безопасна: снапшот
elo_before откатывает рейтинги/форму/статы, серия кубка пересчитывается с нуля,
расчёт купонов с изменившимся исходом откатывается и делается заново.
"""
import json
import logging
from datetime import datetime, timedelta

import db as appdb
import elo
import settings as appsettings

log = logging.getLogger("bot.results")


def _player_for_elo(c, match: dict, side: str) -> int | None:
    """players.telegram_id: сначала home/away_player_id, фолбэк — владелец клуба."""
    pid = match.get(f"{side}_player_id")
    club_id = match.get(f"{side}_club_id")
    if pid:
        row = c.execute("SELECT telegram_id FROM players WHERE id=?", (pid,)).fetchone()
        if row:
            return row["telegram_id"]
    if club_id:
        row = c.execute(
            "SELECT p.telegram_id FROM club_players cp JOIN players p ON p.id=cp.player_id "
            "WHERE cp.club_id=?", (club_id,),
        ).fetchone()
        if row:
            return row["telegram_id"]
    return None


def _get_tournament_elo(c, tournament_id: int, tg_id: int) -> tuple[int, int]:
    row = c.execute(
        "SELECT elo, games FROM tournament_elo WHERE tournament_id=? AND player_id=?",
        (tournament_id, tg_id),
    ).fetchone()
    return (row["elo"], row["games"]) if row else (1000, 0)


def _confirmed_games(c, tg_id: int) -> int:
    """Сыгранные матчи игрока (players.id; в кубках player_id пуст — по клубу)."""
    p = c.execute("SELECT id FROM players WHERE telegram_id=?", (tg_id,)).fetchone()
    if not p:
        return 0
    cl = c.execute("SELECT club_id FROM club_players WHERE player_id=?", (p["id"],)).fetchone()
    club_id = cl["club_id"] if cl else -1
    row = c.execute(
        "SELECT COUNT(*) n FROM matches WHERE status='confirmed' AND ("
        " home_player_id=? OR away_player_id=?"
        " OR (home_player_id IS NULL AND home_club_id=?)"
        " OR (away_player_id IS NULL AND away_club_id=?))",
        (p["id"], p["id"], club_id, club_id),
    ).fetchone()
    return row["n"]


def _snapshot(c, match: dict) -> dict:
    """Снапшот рейтингов/форм/статов до финализации (для отката при споре)."""
    snap = {"club_elo": {}, "club_form": {}, "telo": {}, "player_stats": {}}
    for side in ("home", "away"):
        club_id = match.get(f"{side}_club_id")
        if club_id:
            row = c.execute("SELECT elo, form FROM clubs WHERE id=?", (club_id,)).fetchone()
            if row:
                snap["club_elo"][str(club_id)] = row["elo"]
                snap["club_form"][str(club_id)] = row["form"] or ""
        tg = _player_for_elo(c, match, side)
        if tg:
            snap["telo"][str(tg)] = _get_tournament_elo(c, match["tournament_id"], tg)
            row = c.execute(
                "SELECT wins, draws, losses, goals_scored, goals_conceded, assists "
                "FROM players WHERE telegram_id=?", (tg,),
            ).fetchone()
            if row:
                snap["player_stats"][str(tg)] = dict(row)
    return snap


def _revert(c, match: dict) -> None:
    """Откат эффектов предыдущей финализации (спор решён иначе)."""
    if not match.get("elo_before"):
        return
    snap = json.loads(match["elo_before"])
    for cid, v in snap.get("club_elo", {}).items():
        c.execute("UPDATE clubs SET elo=? WHERE id=?", (v, int(cid)))
    for cid, v in snap.get("club_form", {}).items():
        c.execute("UPDATE clubs SET form=? WHERE id=?", (v, int(cid)))
    for tg, v in snap.get("telo", {}).items():
        c.execute(
            "INSERT INTO tournament_elo (tournament_id, player_id, elo, games) VALUES (?,?,?,?) "
            "ON CONFLICT(tournament_id, player_id) DO UPDATE SET elo=excluded.elo, games=excluded.games",
            (match["tournament_id"], int(tg), v[0], v[1]),
        )
    for tg, v in snap.get("player_stats", {}).items():
        c.execute(
            "UPDATE players SET wins=?, draws=?, losses=?, goals_scored=?, goals_conceded=?, assists=? "
            "WHERE telegram_id=?",
            (v["wins"], v["draws"], v["losses"], v["goals_scored"], v["goals_conceded"], v["assists"], int(tg)),
        )


def _result_letter(sf: int, sa: int) -> str:
    return "W" if sf > sa else ("D" if sf == sa else "L")


def finalize_match(match_id: int, score1: int, score2: int,
                   pens1: int | None = None, pens2: int | None = None,
                   goals: list[dict] | None = None, actor: str = "ocr") -> dict:
    """Финализация СРАЗУ без подтверждения (решение 09). goals: [{side,name,minute,is_penalty?}].
    Пересчёт при споре — просто вызвать ещё раз с новым счётом."""
    c = appdb.db()
    row = c.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
    if not row:
        c.close()
        raise ValueError("матч не найден")
    m = dict(row)
    tournament_id = m["tournament_id"]
    home, away = m["home_club_id"], m["away_club_id"]

    # 0) повторная финализация: откатить прошлые эффекты
    refinalize = m["status"] in ("confirmed", "disputed")
    if refinalize:
        _revert(c, m)
    snap = _snapshot(c, m)

    # 1) матч
    c.execute(
        "UPDATE matches SET score1=?, score2=?, pens1=?, pens2=?, status='confirmed', "
        "elo_before=?, played_at=COALESCE(played_at, datetime('now')) WHERE id=?",
        (score1, score2, pens1, pens2, json.dumps(snap, ensure_ascii=False), match_id),
    )

    # 2) голы
    c.execute("DELETE FROM match_goals WHERE match_id=?", (match_id,))
    for i, g in enumerate(goals or []):
        side = "home" if g.get("side") == "home" else "away"
        c.execute(
            "INSERT INTO match_goals (match_id, raw_name, minute, side, ord, is_penalty) VALUES (?,?,?,?,?,?)",
            (match_id, g.get("name"), g.get("minute"), side, i, 1 if g.get("is_penalty") else 0),
        )

    # 3) Elo клубов (честная пара) + tournament_elo игроков
    r1row = c.execute("SELECT elo FROM clubs WHERE id=?", (home,)).fetchone()
    r2row = c.execute("SELECT elo FROM clubs WHERE id=?", (away,)).fetchone()
    r1 = r1row["elo"] if r1row else 1000
    r2 = r2row["elo"] if r2row else 1000
    new1, new2 = elo.compute_elo_change(r1, r2, score1, score2)
    c.execute("UPDATE clubs SET elo=? WHERE id=?", (round(new1), home))
    c.execute("UPDATE clubs SET elo=? WHERE id=?", (round(new2), away))

    tg_home = _player_for_elo(c, m, "home")
    tg_away = _player_for_elo(c, m, "away")
    if tg_home and tg_away and tg_home != tg_away:
        er1, g1 = _get_tournament_elo(c, tournament_id, tg_home)
        er2, g2 = _get_tournament_elo(c, tournament_id, tg_away)
        nt1, nt2 = elo.compute_elo_change(er1, er2, score1, score2,
                                          _confirmed_games(c, tg_home), _confirmed_games(c, tg_away))
        for tg, nr, ng in ((tg_home, nt1, g1 + 1), (tg_away, nt2, g2 + 1)):
            c.execute(
                "INSERT INTO tournament_elo (tournament_id, player_id, elo, games) VALUES (?,?,?,?) "
                "ON CONFLICT(tournament_id, player_id) DO UPDATE SET elo=excluded.elo, games=excluded.games",
                (tournament_id, tg, round(nr), ng),
            )

    # 4) форма клубов + статы игроков
    for club_id, res in ((home, _result_letter(score1, score2)), (away, _result_letter(score2, score1))):
        row2 = c.execute("SELECT form FROM clubs WHERE id=?", (club_id,)).fetchone()
        if row2:
            new_form = ((row2["form"] or "") + res)[-5:]
            c.execute("UPDATE clubs SET form=? WHERE id=?", (new_form, club_id))
    home_goals = [g for g in (goals or []) if g.get("side") == "home"]
    away_goals = [g for g in (goals or []) if g.get("side") == "away"]
    for tg, sf, sa, gs in ((tg_home, score1, score2, len(home_goals)),
                           (tg_away, score2, score1, len(away_goals))):
        if not tg:
            continue
        w = 1 if sf > sa else 0
        d = 1 if sf == sa else 0
        l = 1 if sf < sa else 0
        c.execute(
            "UPDATE players SET wins=wins+?, draws=draws+?, losses=losses+?, "
            "goals_scored=goals_scored+?, goals_conceded=goals_conceded+? WHERE telegram_id=?",
            (w, d, l, gs, sa, tg),
        )

    # 5) кубковая серия (ties): пересчёт с нуля, повторная финализация не удваивает победу
    cancelled_games: list[int] = []
    if m.get("tie_id"):
        import league
        cancelled_games = league.advance_tie(c, m, score1, score2, pens1, pens2) or []

    # 6) ставки: при правке результата — откат расчёта купонов с изменившимся
    # исходом и разморозка (в той же транзакции), затем расчёт заново
    try:
        import bets_engine
    except ImportError:
        bets_engine = None
    if bets_engine and refinalize:
        bets_engine.unsettle_for_match(c, match_id)
    if bets_engine:
        _unfreeze_bets(c, match_id)

    # 7) доходы клубов: стадион + спонсор (пересчёт при правке счёта без задвоения)
    import club_economy
    club_economy.apply_match_income(c, match_id)
    c.commit()
    c.close()

    if bets_engine:
        for mid in [match_id, *cancelled_games]:
            try:
                bets_engine.settle_match(mid)
            except Exception:
                log.exception("settle_match упал на матче %s", mid)

    # 8) кубок: серия решилась/переигралась → следующая стадия, пересборка пар, финал
    if m.get("tie_id"):
        try:
            import league
            league.sync_cup(tournament_id)
        except Exception:
            log.exception("sync_cup упал на турнире %s", tournament_id)

    return {
        "match_id": match_id,
        "score": f"{score1}:{score2}",
        "pens": f" ({pens1}:{pens2} пен.)" if pens1 is not None else "",
        "home_club": home, "away_club": away,
        "tournament_id": tournament_id,
    }


def _unfreeze_bets(c, match_id: int) -> None:
    """Снять заморозку с купонов матча, если у них нет ног на других спорных матчах."""
    c.execute(
        "UPDATE bets SET is_frozen=0 WHERE is_frozen=1 AND id IN "
        "(SELECT bet_id FROM bet_legs WHERE match_id=?) AND id NOT IN "
        "(SELECT l.bet_id FROM bet_legs l JOIN matches m ON m.id=l.match_id "
        " WHERE m.status='disputed' AND m.id!=?)",
        (match_id, match_id),
    )


def _is_participant(c, match: dict, tg_id: int) -> bool:
    p = c.execute("SELECT id FROM players WHERE telegram_id=?", (tg_id,)).fetchone()
    if not p:
        return False
    if p["id"] in (match.get("home_player_id"), match.get("away_player_id")):
        return True
    cl = c.execute("SELECT club_id FROM club_players WHERE player_id=?", (p["id"],)).fetchone()
    return bool(cl and cl["club_id"] in (match["home_club_id"], match["away_club_id"]))


def dispute_match(match_id: int, by_telegram_id: int) -> str:
    """Оспаривание в окне dispute_window_hours → «спорный», ставки заморожены (решение 09).
    Оспорить может только участник матча."""
    c = appdb.db()
    m = c.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
    if not m:
        c.close()
        return "Матч не найден."
    m = dict(m)
    if m["status"] != "confirmed":
        c.close()
        return "Оспорить можно только подтверждённый матч."
    if not _is_participant(c, m, by_telegram_id):
        c.close()
        return "⛔ Оспорить может только участник матча."
    window = appsettings.setting_int("dispute_window_hours", 24)
    try:
        played = datetime.fromisoformat(str(m["played_at"])) if m["played_at"] else None
    except ValueError:
        played = None
    if played and played.tzinfo:
        played = played.replace(tzinfo=None)
    # played_at пишется datetime('now') — это UTC
    if played and datetime.utcnow() - played > timedelta(hours=window):
        c.close()
        return f"⌛ Окно оспаривания ({window} ч) закрыто. Обратись к судье."
    c.execute("UPDATE matches SET status='disputed' WHERE id=?", (match_id,))
    # заморозка купонов на этот матч
    c.execute(
        "UPDATE bets SET is_frozen=1 WHERE id IN "
        "(SELECT DISTINCT bet_id FROM bet_legs WHERE match_id=?) AND status='open'",
        (match_id,),
    )
    c.commit()
    c.close()
    return "ok"


def resolve_dispute(match_id: int, score1: int, score2: int,
                    pens1: int | None = None, pens2: int | None = None,
                    goals: list[dict] | None = None, decided_by: int | None = None) -> dict:
    """Судья решает спор: откат + новая финализация (там же пересчёт и разморозка купонов)."""
    summary = finalize_match(match_id, score1, score2, pens1, pens2, goals, actor="dispute")
    if decided_by:
        c = appdb.db()
        c.execute(
            "INSERT INTO tournament_audit_log (tournament_id, actor_telegram_id, action, details) "
            "VALUES ((SELECT tournament_id FROM matches WHERE id=?), ?, 'resolve_dispute', ?)",
            (match_id, decided_by, f"{score1}:{score2}"),
        )
        c.commit()
        c.close()
    return summary


def fine_unplayed(tournament_id: int, tour_number: int) -> list[tuple[str, int]]:
    """Тур закрылся, матч не сыгран → штраф-долг обоим клубам (per план 09: нарушителю
    штраф, размер регулируемый; виновного не определяем — судья может списать долг
    в админке). Матч остаётся доигрываться (решение 09), статус не трогаем."""
    fine = appsettings.setting_int("unplayed_fine", 1_000_000)
    c = appdb.db()
    rows = c.execute(
        "SELECT * FROM matches WHERE tournament_id=? AND tour_number=? AND status='pending'",
        (tournament_id, tour_number),
    ).fetchall()
    fined = []
    seen_clubs = set()
    for m in rows:
        for cid in (m["home_club_id"], m["away_club_id"]):
            if cid is None or cid in seen_clubs:
                continue
            seen_clubs.add(cid)
            club = c.execute("SELECT name FROM clubs WHERE id=?", (cid,)).fetchone()
            c.execute(
                "INSERT INTO debts (club_id, amount, reason, source, status) VALUES (?,?,?,'manual','open')",
                (cid, fine, f"не сыгран матч тура {tour_number}"),
            )
            fined.append((club["name"] if club else f"#{cid}", fine))
    c.commit()
    c.close()
    return fined
