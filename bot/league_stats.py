"""Экраны оригинала «Логово», которых не было: бомбардиры, события матча, скрин результата,
публичный профиль каппера, «Мои матчи», горячие матчи, движение кэфов, награды капперам
за сезон, предупреждения о рисках для админа.
"""
import json
import logging
import re
from pathlib import Path

import config
import db as appdb
import settings as appsettings

log = logging.getLogger("bot.league_stats")

SCREENS_DIR = Path(config.MEDIA_DIR) / "matches"


def _club_name(c, cid):
    r = c.execute("SELECT name FROM clubs WHERE id=?", (cid,)).fetchone() if cid else None
    return r["name"] if r else "—"


def _norm(name: str) -> str:
    return " ".join(str(name or "").casefold().split())


# ===== статистика игроков матча =====

def save_player_stats(c, match: dict, goals: list[dict] | None, players: list[dict] | None) -> None:
    """В транзакции finalize_match. players — таблица со скрина (Г/А); без неё — из списка голов."""
    c.execute("DELETE FROM match_player_stats WHERE match_id=?", (match["id"],))
    rows: dict[tuple[str, str], dict] = {}
    for p in players or []:
        name = " ".join(str(p.get("name") or "").split())[:40]
        if not name or not (int(p.get("goals") or 0) or int(p.get("assists") or 0)):
            continue
        side = "home" if p.get("side") == "home" else "away"
        r = rows.setdefault((side, _norm(name)), {"side": side, "name": name, "goals": 0, "assists": 0})
        r["goals"] += int(p.get("goals") or 0)
        r["assists"] += int(p.get("assists") or 0)
    if not any(r["goals"] for r in rows.values()):
        # таблицы нет или в ней не видно голов — считаем по ленте голов
        for r in rows.values():
            r["goals"] = 0
        for g in goals or []:
            name = " ".join(str(g.get("name") or "").split())[:40]
            if not name:
                continue
            side = "home" if g.get("side") == "home" else "away"
            rows.setdefault((side, _norm(name)), {"side": side, "name": name, "goals": 0, "assists": 0})["goals"] += 1
    for r in rows.values():
        club = match["home_club_id"] if r["side"] == "home" else match["away_club_id"]
        c.execute("INSERT INTO match_player_stats (match_id, side, club_id, name, goals, assists) VALUES (?,?,?,?,?,?)",
                  (match["id"], r["side"], club, r["name"], r["goals"], r["assists"]))


def top_scorers(tournament_id: int, division_id: int | None = None, limit: int = 30) -> dict:
    c = appdb.db()
    try:
        sql = ("SELECT s.name, s.club_id, s.goals, s.assists, s.match_id FROM match_player_stats s "
               "JOIN matches m ON m.id=s.match_id WHERE m.tournament_id=? AND m.status='confirmed'")
        args: list = [tournament_id]
        if division_id:
            sql += " AND s.club_id IN (SELECT id FROM clubs WHERE division_id=?)"
            args.append(int(division_id))
        agg: dict[tuple, dict] = {}
        for r in c.execute(sql, args).fetchall():
            k = (r["club_id"], _norm(r["name"]))
            a = agg.setdefault(k, {"name": r["name"], "club_id": r["club_id"], "goals": 0, "assists": 0,
                                   "matches": set()})
            a["goals"] += r["goals"] or 0
            a["assists"] += r["assists"] or 0
            a["matches"].add(r["match_id"])
        rows = sorted(agg.values(), key=lambda a: (-a["goals"], -a["assists"], len(a["matches"]), a["name"]))
        out = []
        for i, a in enumerate([a for a in rows if a["goals"] or a["assists"]][:limit], 1):
            out.append({"position": i, "name": a["name"], "club": _club_name(c, a["club_id"]), "club_id": a["club_id"],
                        "goals": a["goals"], "assists": a["assists"], "matches": len(a["matches"])})
        assists = sorted([a for a in rows if a["assists"]], key=lambda a: (-a["assists"], -a["goals"], a["name"]))[:10]
        return {"tournament_id": tournament_id, "division_id": division_id, "scorers": out,
                "assists": [{"name": a["name"], "club": _club_name(c, a["club_id"]), "assists": a["assists"],
                             "goals": a["goals"]} for a in assists]}
    finally:
        c.close()


def match_events(match_id: int) -> dict:
    c = appdb.db()
    try:
        m = c.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
        if not m:
            raise LookupError("Матч не найден")
        goals = [dict(r) for r in c.execute(
            "SELECT raw_name AS name, minute, side, is_penalty FROM match_goals WHERE match_id=? "
            "ORDER BY CASE WHEN minute IS NULL THEN 1 ELSE 0 END, minute, ord",
            (match_id,)).fetchall()]
        stats = [dict(r) for r in c.execute(
            "SELECT side, name, goals, assists FROM match_player_stats WHERE match_id=? "
            "ORDER BY goals DESC, assists DESC, name", (match_id,)).fetchall()]
        return {"match_id": match_id, "status": m["status"], "score_home": m["score1"], "score_away": m["score2"],
                "goals": goals, "players": stats, "has_photo": bool(screenshot_file(dict(m)))}
    finally:
        c.close()


# ===== скрин результата =====

def save_screenshot(match_id: int, image: bytes) -> str | None:
    """Первый скрин результата → data/media/matches/<id>.jpg (перезаписывается при новом репорте)."""
    if not image:
        return None
    SCREENS_DIR.mkdir(parents=True, exist_ok=True)
    ext = ".png" if image[:8] == b"\x89PNG\r\n\x1a\n" else ".jpg"
    for old in SCREENS_DIR.glob(f"{int(match_id)}.*"):
        old.unlink(missing_ok=True)
    path = SCREENS_DIR / f"{int(match_id)}{ext}"
    path.write_bytes(image)
    c = appdb.db()
    c.execute("UPDATE matches SET screenshot_path=? WHERE id=?", (path.name, match_id))
    c.commit()
    c.close()
    return path.name


def screenshot_file(m: dict) -> Path | None:
    name = m.get("screenshot_path")
    if not name or not re.fullmatch(r"\d+\.(jpg|png)", name):
        return None
    f = SCREENS_DIR / name
    return f if f.is_file() else None


# ===== публичный профиль =====

def public_profile(target_tg: int) -> dict:
    import achievements
    import bet_tools
    c = appdb.db()
    try:
        u = c.execute("SELECT * FROM users WHERE telegram_id=?", (int(target_tg),)).fetchone()
        if not u or u["is_frozen"]:
            raise LookupError("Игрок не найден")
        u = dict(u)
        club = c.execute(
            "SELECT cl.id, cl.name, cl.emblem, cl.color1 FROM clubs cl JOIN club_players cp ON cp.club_id=cl.id "
            "JOIN players p ON p.id=cp.player_id WHERE p.telegram_id=?", (u["telegram_id"],)).fetchone()
        awards = [dict(r) for r in c.execute(
            "SELECT a.award, a.place, a.coins, a.created_at, t.name AS season, d.name AS division FROM bettor_awards a "
            "LEFT JOIN tournaments t ON t.id=a.tournament_id LEFT JOIN divisions d ON d.id=a.division_id "
            "WHERE a.user_id=? ORDER BY a.id DESC", (u["id"],)).fetchall()]
        rank = c.execute("SELECT COUNT(*) n FROM users WHERE is_frozen=0 AND balance>?", (u["balance"],)).fetchone()["n"] + 1
    finally:
        c.close()
    from bet_tools import rank_by_level
    st = bet_tools.bet_stats(u)
    ach = [a for a in achievements.evaluate(u["id"]) if a["is_unlocked"]]
    ach.sort(key=lambda a: a["unlocked_at"] or "", reverse=True)
    return {
        "user_id": u["telegram_id"], "name": u["username"] or u["first_name"] or f"Игрок {u['telegram_id']}",
        "level": u["level"], "rank_title": rank_by_level(u["level"]), "balance": u["balance"], "balance_rank": rank,
        "club": dict(club) if club else None,
        "stats": {k: st[k] for k in ("settled", "won", "lost", "void", "cashout", "roi", "hit_rate", "avg_odds",
                                      "profit", "best_streak", "current_streak", "biggest_win", "by_market",
                                      "total_bets", "open")},
        "achievements": {"count": len(ach), "recent": [{"name": a["name"], "icon": a["badge_icon"],
                                                         "rarity": a["rarity"]} for a in ach[:8]]},
        "awards": awards,
    }


# ===== «Мои матчи» =====

def my_matches(tg: int) -> dict:
    c = appdb.db()
    try:
        club = c.execute(
            "SELECT cl.* FROM clubs cl JOIN club_players cp ON cp.club_id=cl.id JOIN players p ON p.id=cp.player_id "
            "WHERE p.telegram_id=?", (tg,)).fetchone()
        if not club:
            return {"registered": False, "matches": [], "recent": []}
        cid = club["id"]
        rows = [dict(r) for r in c.execute(
            "SELECT m.*, t.status AS tour_status, t.deadline AS tour_deadline, tr.name AS tournament_name, "
            "tr.current_tour FROM matches m "
            "LEFT JOIN tours t ON t.tournament_id=m.tournament_id AND t.tour_number=m.tour_number "
            "LEFT JOIN tournaments tr ON tr.id=m.tournament_id "
            "WHERE (m.home_club_id=? OR m.away_club_id=?) AND m.status IN ('pending','reported','disputed') "
            "AND COALESCE(tr.stage,'') != 'finished' ORDER BY COALESCE(m.tour_number, 999), m.id LIMIT 60",
            (cid, cid)).fetchall()]
        out = []
        for m in rows:
            home = m["home_club_id"] == cid
            opp = m["away_club_id"] if home else m["home_club_id"]
            # locked бывает и у ещё не открытых туров: просрочен только тур не позже текущего
            state = ("disputed" if m["status"] == "disputed"
                     else "open" if (m["tour_number"] is None or m["tour_status"] == "open")
                     else "overdue" if m["tour_number"] <= (m["current_tour"] or 0) else "upcoming")
            out.append({"match_id": m["id"], "tournament": m["tournament_name"], "tour_number": m["tour_number"],
                        "home": home, "opponent": _club_name(c, opp), "opponent_id": opp,
                        "deadline": m["tour_deadline"] if state in ("open", "overdue") else None,
                        "scheduled_at": m.get("scheduled_at"), "state": state, "status": m["status"]})
        # просроченные и открытые — сверху, будущие туры ниже
        order = {"overdue": 0, "disputed": 1, "open": 2, "upcoming": 3}
        out.sort(key=lambda x: (order[x["state"]], x["tour_number"] or 999, x["match_id"]))
        recent = []
        for m in c.execute(
                "SELECT * FROM matches WHERE (home_club_id=? OR away_club_id=?) AND status='confirmed' "
                "ORDER BY COALESCE(played_at, created_at) DESC, id DESC LIMIT 5", (cid, cid)).fetchall():
            home = m["home_club_id"] == cid
            gf, ga = (m["score1"], m["score2"]) if home else (m["score2"], m["score1"])
            recent.append({"match_id": m["id"], "opponent": _club_name(c, m["away_club_id"] if home else m["home_club_id"]),
                           "home": home, "score": f"{gf}:{ga}", "result": "W" if gf > ga else "D" if gf == ga else "L"})
        # будущие туры — только ближайшие 3, остальное счётчиком
        now_ = [x for x in out if x["state"] != "upcoming"]
        later = [x for x in out if x["state"] == "upcoming"]
        return {"registered": True, "club": {"id": cid, "name": club["name"]}, "matches": now_ + later[:3],
                "more_upcoming": max(0, len(later) - 3), "recent": recent}
    finally:
        c.close()


# ===== горячие матчи и движение кэфов =====

def _open_line(c) -> list[dict]:
    return [dict(r) for r in c.execute(
        "SELECT m.* FROM matches m LEFT JOIN tours t ON t.tournament_id=m.tournament_id "
        "AND t.tour_number=m.tour_number WHERE m.status='pending' AND (m.tour_number IS NULL OR t.status='open')"
    ).fetchall()]


def hot_matches(limit: int = 6) -> list[dict]:
    """Открытые матчи по сумме ставок (все купоны, не только открытые — интерес за тур)."""
    c = appdb.db()
    try:
        out = []
        for m in _open_line(c):
            legs = [dict(r) for r in c.execute(
                "SELECT l.market_code, b.amount, b.user_id FROM bet_legs l JOIN bets b ON b.id=l.bet_id "
                "WHERE l.match_id=? AND b.status != 'void'", (m["id"],)).fetchall()]
            if not legs:
                continue
            by_code: dict[str, int] = {}
            for leg in legs:
                by_code[leg["market_code"]] = by_code.get(leg["market_code"], 0) + 1
            top = max(by_code, key=by_code.get)
            lbl = c.execute("SELECT label, odds FROM markets WHERE match_id=? AND code=?", (m["id"], top)).fetchone()
            out.append({"match_id": m["id"], "home": _club_name(c, m["home_club_id"]),
                        "away": _club_name(c, m["away_club_id"]), "tour_number": m["tour_number"],
                        "bets": len(legs), "bettors": len({x["user_id"] for x in legs}),
                        "stake": sum(int(x["amount"] or 0) for x in legs),
                        "popular": {"code": top, "label": lbl["label"] if lbl else top,
                                    "odds": lbl["odds"] if lbl else None,
                                    "share": round(by_code[top] / len(legs) * 100)}})
        out.sort(key=lambda x: (-x["bettors"], -x["stake"]))
        return out[:limit]
    finally:
        c.close()


def odds_movers(limit: int = 10, min_change_pct: float = 3.0) -> list[dict]:
    """Открытая линия: кэф при открытии → сейчас, по модулю изменения."""
    c = appdb.db()
    try:
        out = []
        for m in _open_line(c):
            first: dict[str, float] = {}
            for r in c.execute("SELECT market_code, odds FROM odds_history WHERE match_id=? ORDER BY id",
                               (m["id"],)).fetchall():
                first.setdefault(r["market_code"], r["odds"])
            for k in c.execute("SELECT code, label, odds FROM markets WHERE match_id=? AND is_active=1",
                               (m["id"],)).fetchall():
                o0 = first.get(k["code"])
                # точный счёт и кэфы-«лотерея» (>10) дёргаются сильнее всего и забивают сводку
                if not o0 or not k["odds"] or k["code"].startswith("cs_") or max(o0, k["odds"]) > 10:
                    continue
                ch = (k["odds"] - o0) / o0 * 100
                if abs(ch) >= min_change_pct:
                    out.append({"match_id": m["id"], "home": _club_name(c, m["home_club_id"]),
                                "away": _club_name(c, m["away_club_id"]), "code": k["code"], "label": k["label"],
                                "from": o0, "to": k["odds"], "change_pct": round(ch, 1)})
        out.sort(key=lambda x: -abs(x["change_pct"]))
        return out[:limit]
    finally:
        c.close()


# ===== награды капперам за сезон =====

AWARDS = [  # (место до, код, название, настройка суммы, дефолт)
    (1, "champion", "Чемпион дивизиона 👑", "award_coins_champion", 3000),
    (3, "top3", "Призёр сезона 🥈", "award_coins_top3", 1500),
    (10, "top10", "Элита сезона 🎖", "award_coins_top10", 500),
]


def award_bettors(tournament_id: int) -> list[dict]:
    """После финала сезона: лучшие капперы каждого дивизиона (по прибыли на его матчах).
    Нужен минимум award_min_bets рассчитанных купонов и прибыль > 0. Идемпотентно."""
    import bet_tools
    min_bets = appsettings.setting_int("award_min_bets", 5)
    c = appdb.db()
    divs = [dict(r) for r in c.execute("SELECT id, name FROM divisions WHERE tournament_id=? ORDER BY sort_order, id",
                                       (tournament_id,)).fetchall()]
    c.close()
    given = []
    for d in divs:
        lb = bet_tools.leaderboard("division", d["id"], limit=1000)
        eligible = [r for r in lb["leaders"] if r["bets"] >= min_bets and r["profit"] > 0]
        for place, r in enumerate(eligible[:AWARDS[-1][0]], 1):
            _, code, title, key, dflt = next(a for a in AWARDS if place <= a[0])
            coins = appsettings.setting_int(key, dflt)
            c = appdb.db()
            try:
                u = c.execute("SELECT id FROM users WHERE telegram_id=?", (r["user_id"],)).fetchone()
                if not u:
                    continue
                cur = c.execute(
                    "INSERT OR IGNORE INTO bettor_awards (tournament_id, division_id, user_id, place, award, coins, profit, roi) "
                    "VALUES (?,?,?,?,?,?,?,?)", (tournament_id, d["id"], u["id"], place, code, coins, r["profit"], r["roi"]))
                if cur.rowcount != 1:
                    continue
                if coins:
                    c.execute("UPDATE users SET balance=balance+? WHERE id=?", (coins, u["id"]))
                    c.execute("INSERT INTO balance_history (user_id, delta, reason) VALUES (?,?,?)",
                              (u["id"], coins, f"награда сезона: {title} ({d['name']})"))
                c.execute("INSERT INTO notifications (user_id, text, kind) VALUES (?,?, 'bet')",
                          (u["id"], f"🏆 {title} — {d['name']}, {place} место среди капперов. +{coins} дыма"))
                c.commit()
                given.append({"division": d["name"], "place": place, "award": code, "name": r["name"], "coins": coins})
            finally:
                c.close()
    return given


AWARD_TITLES = {code: title for _, code, title, _, _ in AWARDS}


# ===== предупреждения о рисках =====

def _upsert_alert(c, match_id, kind, level, message, value) -> bool:
    """→ True, если алерт новый (тогда уведомляем админов)."""
    row = c.execute("SELECT id, level FROM risk_alerts WHERE match_id=? AND kind=? AND status='open'",
                    (match_id, kind)).fetchone()
    if row:
        c.execute("UPDATE risk_alerts SET level=?, message=?, value=?, updated_at=datetime('now') WHERE id=?",
                  (level, message, value, row["id"]))
        return row["level"] != level and level == "high"
    acked = c.execute("SELECT value FROM risk_alerts WHERE match_id=? AND kind=? AND status='ack' "
                      "ORDER BY id DESC LIMIT 1", (match_id, kind)).fetchone()
    # после «принято» не дёргаем, пока риск не вырастет ещё в полтора раза
    if acked and value < (acked["value"] or 0) * 1.5:
        return False
    c.execute("INSERT INTO risk_alerts (match_id, kind, level, message, value) VALUES (?,?,?,?,?)",
              (match_id, kind, level, message, value))
    return True


def risk_scan(match_ids: list[int] | None = None) -> list[dict]:
    """Проверка открытых ставок по матчам. Пороги — bot_settings (risk_*)."""
    liab_lim = appsettings.setting_int("risk_liability", 5000)
    one_share = appsettings.setting_float("risk_one_sided_pct", 80)
    one_min = appsettings.setting_int("risk_min_stake", 1000)
    whale_share = appsettings.setting_float("risk_whale_pct", 60)
    c = appdb.db()
    fresh = []
    try:
        mids = match_ids or [r["match_id"] for r in c.execute(
            "SELECT DISTINCT l.match_id FROM bet_legs l JOIN bets b ON b.id=l.bet_id WHERE b.status='open'").fetchall()]
        for mid in mids:
            m = c.execute("SELECT * FROM matches WHERE id=?", (mid,)).fetchone()
            if not m or m["status"] != "pending":
                c.execute("UPDATE risk_alerts SET status='ack', acked_at=datetime('now') WHERE match_id=? AND status='open'",
                          (mid,))
                continue
            legs = [dict(r) for r in c.execute(
                "SELECT l.market_code, b.amount, b.potential_win, b.user_id FROM bet_legs l JOIN bets b ON b.id=l.bet_id "
                "WHERE l.match_id=? AND b.status='open'", (mid,)).fetchall()]
            if not legs:
                continue
            name = f"{_club_name(c, m['home_club_id'])} — {_club_name(c, m['away_club_id'])}"
            by_code: dict[str, dict] = {}
            by_user: dict[int, int] = {}
            for leg in legs:
                x = by_code.setdefault(leg["market_code"], {"stake": 0, "liab": 0})
                x["stake"] += int(leg["amount"] or 0)
                x["liab"] += int(leg["potential_win"] or 0)
                by_user[leg["user_id"]] = by_user.get(leg["user_id"], 0) + int(leg["amount"] or 0)
            stake = sum(x["stake"] for x in by_code.values())
            code, top = max(by_code.items(), key=lambda kv: kv[1]["liab"])
            lbl = c.execute("SELECT label FROM markets WHERE match_id=? AND code=?", (mid, code)).fetchone()
            lbl = lbl["label"] if lbl else code
            checks = []
            if top["liab"] >= liab_lim:
                checks.append(("liability", "high" if top["liab"] >= liab_lim * 2 else "warn",
                               f"{name}: при «{lbl}» выплата до {top['liab']} дыма", top["liab"]))
            share = top["stake"] / stake * 100 if stake else 0
            if stake >= one_min and share >= one_share and len(by_code) >= 1:
                checks.append(("one_sided", "warn", f"{name}: {share:.0f}% ставок ({top['stake']} из {stake}) на «{lbl}»",
                               round(share, 1)))
            uid, ustake = max(by_user.items(), key=lambda kv: kv[1])
            if stake >= one_min and len(by_user) >= 1 and ustake / stake * 100 >= whale_share:
                un = c.execute("SELECT username, first_name, telegram_id FROM users WHERE id=?", (uid,)).fetchone()
                who = (un["username"] or un["first_name"] or un["telegram_id"]) if un else uid
                checks.append(("whale", "warn", f"{name}: {who} — {ustake / stake * 100:.0f}% всех денег на матч",
                               round(ustake / stake * 100, 1)))
            active = {k for k, *_ in checks}
            for kind, level, msg, val in checks:
                if _upsert_alert(c, mid, kind, level, msg, val):
                    fresh.append({"match_id": mid, "kind": kind, "level": level, "message": msg})
            # риск ушёл (ставки рассчитали/аннулировали) — закрываем без шума
            for r in c.execute("SELECT id, kind FROM risk_alerts WHERE match_id=? AND status='open'", (mid,)).fetchall():
                if r["kind"] not in active:
                    c.execute("UPDATE risk_alerts SET status='ack', acked_at=datetime('now') WHERE id=?", (r["id"],))
        if fresh:
            admins = [r["id"] for r in c.execute("SELECT id FROM users WHERE is_admin=1").fetchall()]
            admins += [r["id"] for r in c.execute(
                f"SELECT id FROM users WHERE telegram_id IN ({','.join('?' * len(config.ADMIN_IDS)) or 'NULL'})",
                list(config.ADMIN_IDS)).fetchall()]
            for a in set(admins):
                for f in fresh:
                    c.execute("INSERT INTO notifications (user_id, text, kind) VALUES (?,?, 'app')",
                              (a, f"⚠️ Риск: {f['message']}"))
        c.commit()
        return fresh
    finally:
        c.close()


def risk_alerts(status: str = "open", limit: int = 50) -> list[dict]:
    c = appdb.db()
    try:
        return [dict(r) for r in c.execute(
            "SELECT * FROM risk_alerts WHERE status=? ORDER BY CASE level WHEN 'high' THEN 0 ELSE 1 END, "
            "updated_at DESC LIMIT ?", (status, limit)).fetchall()]
    finally:
        c.close()


def ack_alert(alert_id: int, actor_tg: int) -> bool:
    c = appdb.db()
    try:
        cur = c.execute("UPDATE risk_alerts SET status='ack', acked_by=?, acked_at=datetime('now') "
                        "WHERE id=? AND status='open'", (actor_tg, alert_id))
        c.execute("INSERT INTO tournament_audit_log (tournament_id, actor_telegram_id, action, details) "
                  "VALUES (NULL, ?, 'risk_ack', ?)", (actor_tg, json.dumps({"alert": alert_id})))
        c.commit()
        return cur.rowcount == 1
    finally:
        c.close()
