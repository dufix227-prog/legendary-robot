"""Инструменты игрока вокруг ставок: кэшаут, черновики купонов, статистика, рейтинги капперов.

Кэшаут — только прематч и в том же окне, что приём ставок (тур открыт, матч pending):
сумма = выплата при выигрыше × P(оставшиеся ноги зайдут) × (1 − маржа кэшаута),
P — честная вероятность по той же модели, что даёт кэфы (Elo + голы турнира).
"""
import json

import db as appdb
import markets as markets_engine
import settings as appsettings
from bets_engine import TIE_CODES, BetError, _limits, _notify, _tour_open

SETTLED = ("won", "lost", "void", "cashout")


def rank_by_level(level: int) -> str:
    # звания от уровня: 1 Окурок → 2-3 Пепел → 4-5 Сигарета → 6-9 Сигара → 10+ Легенда
    if level >= 10:
        return "Легенда"
    if level >= 6:
        return "Сигара"
    if level >= 4:
        return "Сигарета"
    if level >= 2:
        return "Пепел"
    return "Окурок"


# ===== кэшаут =====

def _paused_matches() -> set[int]:
    try:
        raw = json.loads(appsettings.get_setting("bets_paused_matches", "[]") or "[]")
    except json.JSONDecodeError:
        return set()
    return {int(x) for x in (raw if isinstance(raw, list) else [raw]) if str(x).lstrip("-").isdigit()}


def _quote(c, bet: dict) -> dict:
    """→ {available, amount, reason, payout_if_win, win_prob}. Без записи в БД."""
    no = lambda reason: {"available": False, "amount": 0, "reason": reason}  # noqa: E731
    if appsettings.setting_int("cashout_enabled", 1) != 1:
        return no("Кэшаут выключен админом")
    if bet["status"] != "open":
        return no("Купон уже рассчитан")
    if bet["is_frozen"]:
        return no("Купон заморожен: по матчу спор")
    paused, _ = appsettings.bets_paused()
    if paused:
        return no("Приём ставок на паузе")
    legs = [dict(r) for r in c.execute("SELECT * FROM bet_legs WHERE bet_id=?", (bet["id"],)).fetchall()]
    paused_ids = _paused_matches()
    odds_live, prob = 1.0, 1.0
    pending = 0
    for leg in legs:
        if leg["result"] == "lost":
            return no("Одна из ног уже проиграла")
        if leg["result"] == "void":
            continue
        odds_live *= leg["odds"]
        if leg["result"] == "won":
            continue
        if leg["market_code"] in TIE_CODES:
            return no("Кэшаут недоступен для ставок на исход серии")
        m = c.execute("SELECT * FROM matches WHERE id=?", (leg["match_id"],)).fetchone()
        if not m or m["status"] != "pending" or not _tour_open(c, dict(m)):
            return no("Матч уже начался или сыгран — кэшаут закрыт")
        if m["id"] in paused_ids:
            return no("Приём ставок на матч приостановлен")
        p = markets_engine.match_probs(c, dict(m)).get(leg["market_code"])
        if p is None:
            return no("Рынок не поддерживает кэшаут")
        prob *= p
        pending += 1
    if not pending:
        return no("Купон ждёт расчёта")
    max_payout = _limits()["max_payout"]
    has_void = any(l["result"] == "void" for l in legs)
    payout_if_win = min(int(bet["amount"] * odds_live), max_payout) if has_void else int(bet["potential_win"])
    margin = appsettings.setting_float("cashout_margin_pct", 8) / 100.0
    amount = int(payout_if_win * prob * (1 - margin))
    amount = max(1, min(amount, payout_if_win - 1))
    return {"available": True, "amount": amount, "reason": None,
            "payout_if_win": payout_if_win, "win_prob": round(prob, 4),
            "margin_pct": round(margin * 100, 2)}


def _own_bet(c, user_id: int, bet_id: int) -> dict:
    row = c.execute("SELECT * FROM bets WHERE id=? AND user_id=?", (bet_id, user_id)).fetchone()
    if not row:
        raise BetError("NO_BET", "Купон не найден")
    return dict(row)


def cashout_quote(user_row: dict, bet_id: int) -> dict:
    c = appdb.db()
    try:
        return {"bet_id": bet_id, **_quote(c, _own_bet(c, user_row["id"], bet_id))}
    finally:
        c.close()


def cashout(user_row: dict, bet_id: int, expected: int | None = None) -> dict:
    """Выкуп купона. expected — сумма, которую видел игрок: если квота уехала, просим подтвердить."""
    c = appdb.db()
    try:
        fresh = c.execute("SELECT is_frozen FROM users WHERE id=?", (user_row["id"],)).fetchone()
        if fresh and fresh["is_frozen"]:
            raise BetError("FROZEN", "Доступ закрыт")
        bet = _own_bet(c, user_row["id"], bet_id)
        q = _quote(c, bet)
        if not q["available"]:
            raise BetError("CASHOUT_UNAVAILABLE", q["reason"])
        if expected is not None and int(expected) != q["amount"]:
            raise BetError("CASHOUT_CHANGED", f"Сумма кэшаута изменилась: {q['amount']}", {"quote": q})
        cur = c.execute("UPDATE bets SET status='cashout', cashout_amount=? WHERE id=? AND status='open'",
                        (q["amount"], bet_id))
        if cur.rowcount != 1:
            raise BetError("CASHOUT_UNAVAILABLE", "Купон уже рассчитан")
        c.execute("UPDATE users SET balance=balance+? WHERE id=?", (q["amount"], bet["user_id"]))
        c.execute("INSERT INTO balance_history (user_id, delta, reason) VALUES (?,?,?)",
                  (bet["user_id"], q["amount"], f"кэшаут купона #{bet_id}"))
        _notify(c, bet["user_id"], f"💸 Кэшаут купона #{bet_id}: +{q['amount']} дыма "
                                   f"(ставка {bet['amount']})", kind="bet")
        c.commit()
        balance = c.execute("SELECT balance FROM users WHERE id=?", (bet["user_id"],)).fetchone()["balance"]
        return {"bet_id": bet_id, "amount": q["amount"], "balance": balance}
    finally:
        c.close()


def cashout_quotes(user_row: dict) -> dict[int, dict]:
    """Квоты по всем открытым купонам игрока (для истории ставок)."""
    c = appdb.db()
    try:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM bets WHERE user_id=? AND status='open' ORDER BY id DESC LIMIT 50",
            (user_row["id"],)).fetchall()]
        return {b["id"]: _quote(c, b) for b in rows}
    finally:
        c.close()


# ===== черновики купонов =====

def _clean_legs(legs) -> list[dict]:
    if not isinstance(legs, list) or not legs:
        raise BetError("EMPTY", "Черновик пуст")
    max_legs = _limits()["max_legs"]
    if len(legs) > max_legs:
        raise BetError("MAX_LEGS", f"Максимум {max_legs} ног")
    out, seen = [], set()
    for leg in legs:
        try:
            mid, code = int(leg["match_id"]), str(leg["market_code"])[:20]
        except (KeyError, TypeError, ValueError):
            raise BetError("BAD_LEG", "Неверная нога купона")
        if mid in seen:
            raise BetError("DUP_MATCH", "Один матч — одна нога")
        seen.add(mid)
        out.append({"match_id": mid, "market_code": code})
    return out


def _draft_view(c, row: dict) -> dict:
    legs_out = []
    for leg in json.loads(row["legs"] or "[]"):
        m = c.execute("SELECT * FROM matches WHERE id=?", (leg["match_id"],)).fetchone()
        mk = c.execute("SELECT label, odds FROM markets WHERE match_id=? AND code=? AND is_active=1",
                       (leg["match_id"], leg["market_code"])).fetchone()
        names = []
        for cid in ((m["home_club_id"], m["away_club_id"]) if m else ()):
            cl = c.execute("SELECT name FROM clubs WHERE id=?", (cid,)).fetchone()
            names.append(cl["name"] if cl else "—")
        open_ = bool(m and mk and m["status"] == "pending" and _tour_open(c, dict(m)))
        legs_out.append({
            "match_id": leg["match_id"], "market_code": leg["market_code"],
            "label": mk["label"] if mk else markets_engine.MARKET_LABELS.get(leg["market_code"], leg["market_code"]),
            "odds": mk["odds"] if mk else None,
            "match_label": " — ".join(names) if names else f"матч #{leg['match_id']}",
            "available": open_,
        })
    total = 1.0
    for leg in legs_out:
        if leg["available"]:
            total *= leg["odds"]
    return {"id": row["id"], "name": row.get("name") or f"Черновик #{row['id']}", "amount": row["amount"],
            "created_at": row["created_at"], "legs": legs_out,
            "available_legs": sum(l["available"] for l in legs_out), "total_odds": round(total, 3)}


def list_drafts(user_row: dict) -> list[dict]:
    c = appdb.db()
    try:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM saved_coupons WHERE user_id=? ORDER BY id DESC", (user_row["id"],)).fetchall()]
        return [_draft_view(c, r) for r in rows]
    finally:
        c.close()


def save_draft(user_row: dict, legs, amount=None, name: str | None = None) -> dict:
    legs = _clean_legs(legs)
    try:
        amount = max(0, int(amount or 0))
    except (TypeError, ValueError):
        amount = 0
    name = (name or "").strip()[:40] or None
    c = appdb.db()
    try:
        n = c.execute("SELECT COUNT(*) n FROM saved_coupons WHERE user_id=?", (user_row["id"],)).fetchone()["n"]
        cap = appsettings.setting_int("saved_coupons_max", 10)
        if n >= cap:
            raise BetError("DRAFTS_LIMIT", f"Черновиков максимум {cap} — удали старый")
        did = c.insert_returning_id(
            "INSERT INTO saved_coupons (user_id, legs, amount, name) VALUES (?,?,?,?)",
            (user_row["id"], json.dumps(legs), amount, name))
        c.commit()
        row = dict(c.execute("SELECT * FROM saved_coupons WHERE id=?", (did,)).fetchone())
        return _draft_view(c, row)
    finally:
        c.close()


def delete_draft(user_row: dict, draft_id: int) -> None:
    c = appdb.db()
    try:
        cur = c.execute("DELETE FROM saved_coupons WHERE id=? AND user_id=?", (draft_id, user_row["id"]))
        if cur.rowcount != 1:
            raise BetError("NO_DRAFT", "Черновик не найден")
        c.commit()
    finally:
        c.close()


# ===== статистика каппера =====

MARKET_GROUPS = [
    ("Исход", ("1x2_",)), ("Двойной шанс", ("dc_",)), ("Тоталы", ("tb", "tm")),
    ("Обе забьют", ("btts_",)), ("Инд. тоталы", ("itb_",)), ("Фора", ("ah_",)),
    ("Точный счёт", ("cs_",)), ("Серия", ("tie_",)),
]


def market_group(code: str) -> str:
    for title, prefixes in MARKET_GROUPS:
        if code.startswith(prefixes):
            return title
    return "Другие"


def bet_return(b: dict) -> int:
    """Сколько вернулось игроку по рассчитанному купону."""
    if b["status"] == "won":
        return int(b["potential_win"] or 0)
    if b["status"] == "void":
        return int(b["amount"] or 0)
    if b["status"] == "cashout":
        return int(b.get("cashout_amount") or 0)
    return 0


def summarize(bets: list[dict]) -> dict:
    """ROI/проходимость/средний кэф по списку купонов (void в ROI не входит — деньги вернулись)."""
    counted = [b for b in bets if b["status"] in ("won", "lost", "cashout")]
    staked = sum(int(b["amount"] or 0) for b in counted)
    returned = sum(bet_return(b) for b in counted)
    won = sum(1 for b in bets if b["status"] == "won")
    lost = sum(1 for b in bets if b["status"] == "lost")
    return {
        "settled": len([b for b in bets if b["status"] in SETTLED]),
        "won": won, "lost": lost,
        "void": sum(1 for b in bets if b["status"] == "void"),
        "cashout": sum(1 for b in bets if b["status"] == "cashout"),
        "staked": staked, "returned": returned, "profit": returned - staked,
        "roi": round((returned - staked) / staked * 100, 1) if staked else 0.0,
        "hit_rate": round(won / (won + lost) * 100, 1) if won + lost else 0.0,
        "avg_odds": round(sum(float(b["total_odds"] or 0) for b in counted) / len(counted), 2) if counted else 0.0,
    }


def bet_stats(user_row: dict) -> dict:
    c = appdb.db()
    try:
        bets = [dict(r) for r in c.execute(
            "SELECT * FROM bets WHERE user_id=? ORDER BY id", (user_row["id"],)).fetchall()]
        legs = [dict(r) for r in c.execute(
            "SELECT l.market_code, l.result, l.odds, b.bet_type FROM bet_legs l JOIN bets b ON b.id=l.bet_id "
            "WHERE b.user_id=?", (user_row["id"],)).fetchall()]
    finally:
        c.close()
    s = summarize(bets)
    streak = best = 0
    for b in bets:
        if b["status"] == "won":
            streak += 1
            best = max(best, streak)
        elif b["status"] == "lost":
            streak = 0
    wins = [b for b in bets if b["status"] == "won"]
    biggest = max(wins, key=lambda b: int(b["potential_win"] or 0) - int(b["amount"] or 0), default=None)
    groups: dict[str, dict] = {}
    for leg in legs:
        if leg["result"] not in ("won", "lost"):
            continue
        g = groups.setdefault(market_group(leg["market_code"]), {"won": 0, "lost": 0})
        g[leg["result"]] += 1
    by_market = sorted(({"group": k, **v, "hit_rate": round(v["won"] / (v["won"] + v["lost"]) * 100, 1)}
                        for k, v in groups.items()), key=lambda x: -(x["won"] + x["lost"]))
    open_bets = [b for b in bets if b["status"] == "open"]
    return {
        **s,
        "total_bets": len(bets),
        "open": len(open_bets), "open_stake": sum(int(b["amount"] or 0) for b in open_bets),
        "current_streak": streak, "best_streak": best,
        "biggest_win": ({"bet_id": biggest["id"], "profit": int(biggest["potential_win"]) - int(biggest["amount"]),
                         "odds": biggest["total_odds"]} if biggest else None),
        "singles": sum(1 for b in bets if b["bet_type"] == "single"),
        "expresses": sum(1 for b in bets if b["bet_type"] == "express"),
        "by_market": by_market,
    }


# ===== рейтинги капперов: сезон / дивизион =====

def _scope_match_ids(c, scope: str, scope_id: int) -> set[int]:
    if scope == "season":
        rows = c.execute("SELECT id FROM matches WHERE tournament_id=?", (scope_id,)).fetchall()
    elif scope == "division":
        d = c.execute("SELECT tournament_id FROM divisions WHERE id=?", (scope_id,)).fetchone()
        if not d:
            raise BetError("NO_DIVISION", "Дивизион не найден")
        rows = c.execute(
            "SELECT m.id FROM matches m JOIN clubs cl ON cl.id=m.home_club_id "
            "WHERE m.tournament_id=? AND cl.division_id=?", (d["tournament_id"], scope_id)).fetchall()
    else:
        raise BetError("BAD_SCOPE", "Рейтинг: season или division")
    return {r["id"] for r in rows}


def leaderboard(scope: str, scope_id: int, viewer_id: int | None = None, limit: int = 50) -> dict:
    """Капперы по прибыли на матчах сезона/дивизиона: купон входит, если все его ноги — в зачёте."""
    c = appdb.db()
    try:
        mids = _scope_match_ids(c, scope, int(scope_id))
        per_bet: dict[int, list[int]] = {}
        for r in c.execute("SELECT bet_id, match_id FROM bet_legs").fetchall():
            per_bet.setdefault(r["bet_id"], []).append(r["match_id"])
        ids = [bid for bid, ms in per_bet.items() if ms and all(m in mids for m in ms)]
        bets: list[dict] = []
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            bets += [dict(r) for r in c.execute(
                f"SELECT * FROM bets WHERE id IN ({','.join('?' * len(chunk))})", chunk).fetchall()]
        by_user: dict[int, list[dict]] = {}
        for b in bets:
            by_user.setdefault(b["user_id"], []).append(b)
        users = {r["id"]: dict(r) for r in c.execute(
            "SELECT id, telegram_id, username, first_name, is_frozen FROM users").fetchall()}
    finally:
        c.close()
    rows = []
    for uid, ub in by_user.items():
        u = users.get(uid)
        if not u or u["is_frozen"]:
            continue
        s = summarize(ub)
        if not s["staked"]:
            continue
        rows.append({"user_id": u["telegram_id"], "uid": uid,
                     "name": u["username"] or u["first_name"] or f"Игрок {u['telegram_id']}",
                     "bets": s["settled"], "profit": s["profit"], "roi": s["roi"],
                     "hit_rate": s["hit_rate"], "avg_odds": s["avg_odds"]})
    rows.sort(key=lambda r: (-r["profit"], -r["roi"], -r["bets"]))
    for i, r in enumerate(rows, 1):
        r["position"] = i
    me = next((r for r in rows if r["uid"] == viewer_id), None)
    for r in rows:
        r.pop("uid")
    return {"scope": scope, "scope_id": int(scope_id), "leaders": rows[:limit], "me": me}
