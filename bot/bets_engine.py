"""Движок ставок (план 09: модель оригинала, проверенная стресс-тестом).

- серверная сверка кэфа → ODDS_CHANGED (клиентский кэф не доверяем);
- idempotency_key — повторный POST не списывает деньги дважды;
- лимиты: 10/50 000/выплата 10 000/6 купонов/exposure 20 000 (bot_settings);
- авто-обрез выплаты под max_payout (клиент видит ДО подтверждения);
- запреты: владелец клуба не ставит на свой клуб; судья — на судимые матчи;
- окно приёма: тур открыт → принимаем; тур залочен → TOUR_LOCKED;
- расчёт: авто при финализации (results.finalize_match → settle_match),
  повторный вызов безопасен (только open-купоны).
"""
import json
import logging
from datetime import date, timedelta

import db as appdb
import bet_notify
import markets as markets_engine
import settings as appsettings

log = logging.getLogger("bets.engine")

TIE_CODES = {"tie_2_0", "tie_2_1", "tie_1_2", "tie_0_2"}


class BetError(Exception):
    def __init__(self, code: str, message: str, extra: dict | None = None):
        super().__init__(message)
        self.code = code
        self.extra = extra or {}


def _limits() -> dict:
    return {
        "min_bet": appsettings.setting_int("min_bet", 10),
        "max_bet": appsettings.setting_int("max_bet", 50000),
        "max_payout": appsettings.setting_int("max_payout", 10000),
        "max_open_bets": appsettings.setting_int("max_open_bets", 6),
        "max_open_exposure": appsettings.setting_int("max_open_exposure", 20000),
        "max_legs": appsettings.setting_int("max_legs", 5),
    }


def _notify(c, user_id: int, text: str, kind: str = "app") -> None:
    """kind='bet' — расчёт купона: дублируется в ЛС бота (job_push_bet_results)."""
    c.execute("INSERT INTO notifications (user_id, text, kind) VALUES (?,?,?)", (user_id, text, kind))


def _check_restrictions(c, user_row: dict, match: dict) -> None:
    """Владелец не ставит на свой клуб; судья — на судимые матчи (решение 09)."""
    tg = user_row["telegram_id"]
    owner = c.execute(
        "SELECT club_id FROM club_players WHERE player_id="
        "(SELECT id FROM players WHERE telegram_id=?)", (tg,),
    ).fetchone()
    if owner and owner["club_id"] in (match["home_club_id"], match["away_club_id"]):
        raise BetError("OWN_CLUB", "Нельзя ставить на матчи своего клуба")
    judge = c.execute(
        "SELECT 1 FROM tournament_admins WHERE tournament_id=? AND telegram_id=?",
        (match["tournament_id"], tg),
    ).fetchone()
    if judge:
        raise BetError("JUDGE_MATCH", "Судья не ставит на матчи, которые судит")


def _tour_open(c, match: dict) -> bool:
    """Лига: тур должен быть open. Кубки: pending-матч принимается."""
    if match["tour_number"] is None:
        return True
    row = c.execute(
        "SELECT status FROM tours WHERE tournament_id=? AND tour_number=?",
        (match["tournament_id"], match["tour_number"]),
    ).fetchone()
    return bool(row and row["status"] == "open")


def place_bet(user_row: dict, amount: int, selections: list[dict],
              idempotency_key: str | None = None) -> dict:
    lim = _limits()
    # заморозку перечитываем из БД: бан мог случиться после загрузки снапшота
    c0 = appdb.db()
    fresh = c0.execute("SELECT is_frozen FROM users WHERE id=?", (user_row["id"],)).fetchone()
    c0.close()
    if fresh and fresh["is_frozen"]:
        raise BetError("FROZEN", "Доступ закрыт")

    paused, reason = appsettings.bets_paused()
    if paused:
        raise BetError("BETS_PAUSED", f"Приём ставок на паузе: {reason or 'админ'}")

    # idempotency: тот же ключ → вернуть тот же купон без списания
    if idempotency_key:
        c = appdb.db()
        row = c.execute("SELECT id FROM bets WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        c.close()
        if row:
            return {"bet_id": row["id"], "duplicate": True}

    amount = int(amount)
    if amount < lim["min_bet"]:
        raise BetError("MIN_BET", f"Минимум {lim['min_bet']} дыма")
    if amount > lim["max_bet"]:
        raise BetError("MAX_BET", f"Максимум {lim['max_bet']} дыма")
    if not selections:
        raise BetError("EMPTY", "Купон пуст")
    if len(selections) > lim["max_legs"]:
        raise BetError("MAX_LEGS", f"Экспресс — максимум {lim['max_legs']} ног")

    c = appdb.db()
    try:
        uid = user_row["id"]
        balance = c.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()["balance"]
        if balance < amount:
            raise BetError("NO_FUNDS", "Недостаточно дыма")

        open_count = c.execute(
            "SELECT COUNT(*) n FROM bets WHERE user_id=? AND status='open'", (uid,)).fetchone()["n"]
        if open_count >= lim["max_open_bets"]:
            raise BetError("MAX_OPEN_BETS", f"Максимум {lim['max_open_bets']} открытых купонов")
        exposure = c.execute(
            "SELECT COALESCE(SUM(potential_win),0) s FROM bets WHERE user_id=? AND status='open'",
            (uid,)).fetchone()["s"]

        # серверная сверка кэфов (модель оригинала)
        legs, server_odds = [], []
        match_ids_seen = set()
        for sel in selections:
            mid, code = int(sel["match_id"]), sel["market_code"]
            if mid in match_ids_seen:
                raise BetError("DUP_MATCH", "Один матч — одна нога в экспрессе")
            match_ids_seen.add(mid)
            m = c.execute("SELECT * FROM matches WHERE id=?", (mid,)).fetchone()
            if not m or m["status"] != "pending":
                raise BetError("MATCH_CLOSED", f"Матч {mid} уже не принимает ставки")
            if not _tour_open(c, dict(m)):
                raise BetError("TOUR_LOCKED", "Тур залочен — приём ставок закрыт")
            paused_raw = appsettings.get_setting("bets_paused_matches", "[]") or "[]"
            try:
                paused_matches = json.loads(paused_raw)
                if not isinstance(paused_matches, list):
                    paused_matches = [paused_matches]
            except json.JSONDecodeError:
                paused_matches = []
            if mid in paused_matches:
                raise BetError("MATCH_PAUSED", "Приём ставок на этот матч приостановлен")
            _check_restrictions(c, user_row, dict(m))
            mk = c.execute(
                "SELECT * FROM markets WHERE match_id=? AND code=? AND is_active=1",
                (mid, code),
            ).fetchone()
            if not mk:
                raise BetError("NO_MARKET", f"Рынка {code} нет на матче {mid}")
            client_odds = sel.get("odds")
            if client_odds is not None and abs(float(client_odds) - mk["odds"]) > 0.01:
                fresh = [dict(r) for r in c.execute(
                    "SELECT code, odds FROM markets WHERE match_id=? AND code=?", (mid, code)).fetchall()]
                _notify(c, uid, f"Кэф изменился: {mk['label']} → {mk['odds']}. Обнови купон.")
                c.commit()
                raise BetError("ODDS_CHANGED", "Кэф изменился", {"markets": fresh})
            legs.append({"match_id": mid, "market_code": code, "odds": mk["odds"]})
            server_odds.append(mk["odds"])

        total_odds = 1.0
        for o in server_odds:
            total_odds *= o
        potential = min(int(amount * total_odds), lim["max_payout"])  # авто-обрез
        exposure += potential
        if exposure > lim["max_open_exposure"]:
            raise BetError("MAX_EXPOSURE", f"Лимит экспозиции {lim['max_open_exposure']} дыма")

        # транзакция: списание + купон
        bet_id = c.insert_returning_id(
            "INSERT INTO bets (user_id, bet_type, amount, total_odds, potential_win, status, idempotency_key) "
            "VALUES (?,?,?,?,?, 'open', ?)",
            (uid, "express" if len(legs) > 1 else "single", amount, round(total_odds, 3), potential, idempotency_key),
        )
        for leg in legs:
            c.execute(
                "INSERT INTO bet_legs (bet_id, match_id, market_code, odds, result) VALUES (?,?,?,?, 'pending')",
                (bet_id, leg["match_id"], leg["market_code"], leg["odds"]),
            )
        c.execute("UPDATE users SET balance=balance-?, total_wagered=total_wagered+?, bets_count=bets_count+1 WHERE id=?",
                  (amount, amount, uid))
        c.execute("INSERT INTO balance_history (user_id, delta, reason) VALUES (?,?,?)",
                  (uid, -amount, f"ставка #{bet_id}"))
        _notify(c, uid, f"Ставка принята: {amount} дыма × {round(total_odds, 2)} → до {potential}")
        c.commit()
        balance -= amount
        return {"bet_id": bet_id, "balance": balance, "potential_win": potential,
                "total_odds": round(total_odds, 3), "duplicate": False}
    finally:
        c.close()


def _leg_result_now(c, leg: dict) -> str:
    """Исход ноги по текущему состоянию БД: tie_* — по серии, остальное — по счёту."""
    m = c.execute("SELECT * FROM matches WHERE id=?", (leg["match_id"],)).fetchone()
    if leg["market_code"] in TIE_CODES:
        # игра серии отменена до старта (пара пересобрана/снята) — рынок серии аннулирован
        if m and m["status"] == "cancelled":
            return "void"
        return _resolve_tie_leg(c, leg, dict(m)) if m else "void"
    if not m or m["status"] == "cancelled":
        return "void"
    if m["status"] == "confirmed" and m["score1"] is not None:
        return markets_engine.resolve_leg(leg["match_id"], leg["market_code"], m["score1"], m["score2"])
    return "pending"


def _scope_leg_filter(c, match_id: int) -> tuple[str, list]:
    """Ноги, зависящие от матча: сам матч + tie_* на любой игре той же серии."""
    m = c.execute("SELECT tie_id FROM matches WHERE id=?", (match_id,)).fetchone()
    if m and m["tie_id"]:
        codes = ",".join("?" * len(TIE_CODES))
        return (f"(l.match_id=? OR (l.market_code IN ({codes}) AND l.match_id IN "
                f"(SELECT id FROM matches WHERE tie_id=?)))",
                [match_id, *sorted(TIE_CODES), m["tie_id"]])
    return "l.match_id=?", [match_id]


def _paid_for_bet(c, b: dict) -> int:
    """Сколько по купону реально выплачено (выплаты минус прошлые откаты, в т.ч. неполные)."""
    row = c.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(delta),0) s FROM balance_history WHERE user_id=? "
        "AND (reason IN (?,?) OR reason LIKE ?)",
        (b["user_id"], f"выплата по купону #{b['id']}", f"пересчёт купона #{b['id']}",
         f"пересчёт купона #{b['id']} (%")).fetchone()
    if row["n"]:
        return int(row["s"])
    return int(b["potential_win"] or 0) if b["status"] in ("won", "void") else 0


def _outstanding_for_bet(c, b: dict) -> int:
    """Недосписанное при прошлом пересчёте (баланса не хватило) — удерживается из новой выплаты."""
    row = c.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(delta),0) s FROM balance_history WHERE user_id=? "
        "AND (reason IN (?,?) OR reason LIKE ?)",
        (b["user_id"], f"выплата по купону #{b['id']}", f"пересчёт купона #{b['id']}",
         f"пересчёт купона #{b['id']} (%")).fetchone()
    return max(0, int(row["s"])) if row["n"] else 0


def unsettle_for_match(c, match_id: int) -> list[int]:
    """Результат матча изменён: откатить расчёт купонов, чей исход по этому матчу
    (или по его серии) поменялся. Работает в транзакции вызывающего, без commit.
    Идемпотентно: при том же счёте ничего не трогает. → id переоткрытых купонов.

    Баланс (bot_settings resettle_allow_negative, деф. 0): списываем не больше, чем есть
    на балансе; недостачу пишем в причину, аудит и уведомление. 1 — старое поведение (минус)."""
    where, args = _scope_leg_filter(c, match_id)
    legs = [dict(r) for r in c.execute(
        f"SELECT l.*, b.status AS bet_status FROM bet_legs l JOIN bets b ON b.id=l.bet_id WHERE {where}",
        args).fetchall()]
    lim = _limits()
    xp_win = appsettings.setting_int("xp_per_win", 100)
    allow_negative = appsettings.setting_int("resettle_allow_negative", 0) == 1
    reopened: list[int] = []
    for leg in legs:
        new = _leg_result_now(c, leg)
        old = leg["result"]
        if new == old:
            continue
        if leg["bet_status"] == "open" or leg["bet_id"] in reopened:
            c.execute("UPDATE bet_legs SET result='pending', settled_at=NULL WHERE id=?", (leg["id"],))
            continue
        # void (админом/все ноги) — окончательный; pending-нога в проигранном
        # купоне на исход не влияла (его решила другая нога)
        if leg["bet_status"] not in ("won", "lost") or old == "pending":
            continue
        b = dict(c.execute("SELECT * FROM bets WHERE id=?", (leg["bet_id"],)).fetchone())
        # проигранный купон ничего не держит (недостача прошлого clamp удержится при выплате)
        paid = _paid_for_bet(c, b) if b["status"] == "won" else 0
        take, short = paid, 0
        if paid:
            if not allow_negative:
                bal = c.execute("SELECT balance FROM users WHERE id=?", (b["user_id"],)).fetchone()["balance"]
                take = max(0, min(paid, int(bal or 0)))
                short = paid - take
            c.execute("UPDATE users SET balance=balance-?, total_won=total_won-? WHERE id=?",
                      (take, paid if b["status"] == "won" else 0, b["user_id"]))
            reason = f"пересчёт купона #{b['id']}" + (f" (не хватило {short})" if short else "")
            c.execute("INSERT INTO balance_history (user_id, delta, reason) VALUES (?,?,?)",
                      (b["user_id"], -take, reason))
            if short:
                c.execute(
                    "INSERT INTO tournament_audit_log (tournament_id, actor_telegram_id, action, details) "
                    "VALUES ((SELECT tournament_id FROM matches WHERE id=?), NULL, 'resettle_shortfall', ?)",
                    (match_id, f"купон #{b['id']} user={b['user_id']}: к списанию {paid}, списано {take}, "
                               f"не хватило {short}"))
        if b["status"] == "won":
            c.execute("UPDATE users SET bets_won=CASE WHEN bets_won>0 THEN bets_won-1 ELSE 0 END, "
                      "xp=CASE WHEN xp>? THEN xp-? ELSE 0 END WHERE id=?",
                      (xp_win, xp_win, b["user_id"]))
        odds = 1.0
        for r in c.execute("SELECT odds FROM bet_legs WHERE bet_id=? ORDER BY id", (b["id"],)).fetchall():
            odds *= r["odds"]
        potential = min(int(b["amount"] * odds), lim["max_payout"])
        c.execute("UPDATE bets SET status='open', potential_win=? WHERE id=?", (potential, b["id"]))
        c.execute("UPDATE bet_legs SET result='pending', settled_at=NULL WHERE id=?", (leg["id"],))
        note = ""
        if paid and short:
            note = (f" (списано {take} из {paid} дыма прошлой выплаты — на балансе не хватило {short}, "
                    f"в минус не уходим; недостача удержится, если купон снова сыграет)")
        elif paid:
            note = f" (списано {paid} дыма прошлой выплаты)"
        _notify(c, b["user_id"], f"♻️ Результат матча исправлен — купон #{b['id']} пересчитывается{note}")
        reopened.append(b["id"])
    return reopened


def settle_match(match_id: int) -> dict:
    """Расчёт всех open-купонов с ногами на матч (и tie_* на его серии).
    Спорные матчи пропускаются."""
    c = appdb.db()
    m = c.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
    if not m or (m["score1"] is None and m["status"] != "cancelled"):
        c.close()
        return {"settled": 0}
    if m["status"] == "disputed":
        c.close()
        return {"settled": 0, "frozen": True}
    where, args = _scope_leg_filter(c, match_id)
    bet_rows = [dict(r) for r in c.execute(
        f"SELECT DISTINCT b.* FROM bets b JOIN bet_legs l ON l.bet_id=b.id "
        f"WHERE {where} AND b.status='open' AND b.is_frozen=0", args).fetchall()]
    settled = 0
    lim = _limits()
    xp_win = appsettings.setting_int("xp_per_win", 100)
    for b in bet_rows:
        legs = [dict(r) for r in c.execute(
            "SELECT * FROM bet_legs WHERE bet_id=?", (b["id"],)).fetchall()]
        all_resolved, any_lost, any_void = True, False, False
        eff_odds = 1.0
        for leg in legs:
            if leg["result"] == "pending":
                # tie_* и ноги на других матчах экспресса — через общий резолвер
                res = _leg_result_now(c, leg)
                if res == "pending":
                    all_resolved = False
                    continue
                c.execute("UPDATE bet_legs SET result=?, settled_at=datetime('now') WHERE id=?",
                          (res, leg["id"]))
                leg["result"] = res
            if leg["result"] == "lost":
                any_lost = True
            elif leg["result"] == "void":
                any_void = True
            elif leg["result"] == "won":
                eff_odds *= leg["odds"]
            else:
                all_resolved = False
        # проигранная нога решает экспресс сразу, не дожидаясь остальных матчей
        if not all_resolved and not any_lost:
            continue
        if any_lost:
            status, payout = "lost", 0
        elif any_void and eff_odds <= 1.0:
            status, payout = "void", b["amount"]  # все ноги void → возврат
        else:
            status = "won"
            payout = min(int(b["amount"] * eff_odds), lim["max_payout"]) if any_void else b["potential_win"]
        c.execute("UPDATE bets SET status=?, potential_win=? WHERE id=?", (status, payout, b["id"]))
        if payout > 0:
            # недостача прошлого пересчёта (clamp) удерживается из повторной выплаты
            held = min(payout, _outstanding_for_bet(c, b))
            credit = payout - held
            c.execute("UPDATE users SET balance=balance+?, total_won=total_won+? WHERE id=?",
                      (credit, payout if status == "won" else 0, b["user_id"]))
            c.execute("INSERT INTO balance_history (user_id, delta, reason) VALUES (?,?,?)",
                      (b["user_id"], credit, f"выплата по купону #{b['id']}"))
            if held:
                _notify(c, b["user_id"], f"Из выплаты по купону #{b['id']} удержано {held} дыма — "
                                         f"недостача прошлого пересчёта")
        if status == "won":
            c.execute("UPDATE users SET bets_won=bets_won+1 WHERE id=?", (b["user_id"],))
            _apply_xp(c, b["user_id"], xp_win)
        _notify(c, b["user_id"], bet_notify.settlement_text(c, b, legs, status, payout), kind="bet")
        settled += 1
    c.commit()
    c.close()
    return {"settled": settled}


def _resolve_tie_leg(c, leg: dict, game: dict) -> str:
    """Рынок «исход противостояния» (2:0/2:1/1:2/0:2, серии до 2 побед — план 09)."""
    tie = c.execute("SELECT * FROM ties WHERE id=?", (game.get("tie_id"),)).fetchone()
    if not tie or not tie["winner_club_id"]:
        return "pending"  # серия не завершена — купон ждёт
    # точный счёт серии относительно клуба А (как в generate_tie_markets)
    return "won" if leg["market_code"] == f"tie_{tie['wins_a']}_{tie['wins_b']}" else "lost"


def _apply_xp(c, user_id: int, xp_gain: int) -> None:
    row = c.execute("SELECT xp, level FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        return
    xp = row["xp"] + xp_gain
    level = row["level"]
    step = appsettings.setting_int("level_xp_step", 500)
    while xp >= level * step:
        level += 1
    c.execute("UPDATE users SET xp=?, level=? WHERE id=?", (xp, level, user_id))


def void_bet(bet_id: int, reason: str, actor_id: int | None = None) -> dict:
    """Void = возврат денег + уведомление с причиной (решение 09)."""
    c = appdb.db()
    b = c.execute("SELECT * FROM bets WHERE id=?", (bet_id,)).fetchone()
    if not b:
        c.close()
        raise BetError("NO_BET", "Купон не найден")
    if b["status"] != "open":
        c.close()
        raise BetError("NOT_OPEN", "Купон уже рассчитан")
    c.execute("UPDATE bets SET status='void' WHERE id=?", (bet_id,))
    c.execute("UPDATE bet_legs SET result='void' WHERE bet_id=? AND result='pending'", (bet_id,))
    c.execute("UPDATE users SET balance=balance+? WHERE id=?", (b["amount"], b["user_id"]))
    c.execute("INSERT INTO balance_history (user_id, delta, reason) VALUES (?,?,?)",
              (b["user_id"], b["amount"], f"возврат по купону #{bet_id}"))
    _notify(c, b["user_id"], f"Купон #{bet_id} аннулирован, {b['amount']} дыма возвращено. Причина: {reason}")
    if actor_id:
        c.execute(
            "INSERT INTO tournament_audit_log (tournament_id, actor_telegram_id, action, details) "
            "VALUES (NULL, ?, 'void_bet', ?)", (actor_id, f"bet={bet_id} reason={reason}"))
    c.commit()
    c.close()
    return {"bet_id": bet_id, "refund": b["amount"]}


def claim_streak(user_row: dict) -> dict:
    """Бонус за серию входов: 50 → +10/день, кап 150, пропуск дня сбрасывает (решение 09)."""
    base = appsettings.setting_int("streak_bonus_base", 50)
    step = appsettings.setting_int("streak_bonus_step", 10)
    cap = appsettings.setting_int("streak_bonus_cap", 150)
    today = date.today()
    c = appdb.db()
    uid = user_row["id"]
    row = c.execute("SELECT * FROM user_streaks WHERE user_id=?", (uid,)).fetchone()
    if row and row["last_bonus_date"]:
        last = date.fromisoformat(row["last_bonus_date"])
        if last == today:
            c.close()
            raise BetError("ALREADY", "Бонус уже забран сегодня — приходи завтра")
        streak = row["streak_days"] + 1 if (today - last).days == 1 else 1
    else:
        streak = 1
    amount = min(base + (streak - 1) * step, cap)
    c.execute(
        "INSERT INTO user_streaks (user_id, streak_days, last_bonus_date) VALUES (?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET streak_days=excluded.streak_days, last_bonus_date=excluded.last_bonus_date",
        (uid, streak, today.isoformat()),
    )
    c.execute("UPDATE users SET balance=balance+? WHERE id=?", (amount, uid))
    c.execute("INSERT INTO balance_history (user_id, delta, reason) VALUES (?,?,?)",
              (uid, amount, f"бонус за серию ({streak} дн.)"))
    _notify(c, uid, f"🎁 Бонус за {streak} дн. серии: +{amount} дыма")
    c.commit()
    balance = c.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()["balance"]
    c.close()
    return {"amount": amount, "streak_days": streak, "balance": balance}


def redeem_promo(user_row: dict, code: str) -> dict:
    c = appdb.db()
    uid = user_row["id"]
    row = c.execute("SELECT * FROM promo_codes WHERE code=? AND is_active=1", (code.strip().upper(),)).fetchone()
    if not row:
        c.close()
        raise BetError("BAD_CODE", "Промокод не найден или погас")
    if row["deadline"]:
        try:
            if date.fromisoformat(row["deadline"]) < date.today():
                c.close()
                raise BetError("EXPIRED", "Срок действия промокода истёк")
        except ValueError:
            pass
    used = c.execute("SELECT COUNT(*) n FROM promo_activations WHERE code_id=?",
                     (row["id"],)).fetchone()["n"]
    if row["max_activations"] and used >= row["max_activations"]:
        c.close()
        raise BetError("EXHAUSTED", "Активации промокода закончились")
    dup = c.execute("SELECT 1 FROM promo_activations WHERE code_id=? AND user_id=?",
                    (row["id"], uid)).fetchone()
    if dup:
        c.close()
        raise BetError("DUP", "Ты уже активировал этот промокод")
    c.execute("INSERT INTO promo_activations (code_id, user_id) VALUES (?,?)", (row["id"], uid))
    c.execute("UPDATE users SET balance=balance+? WHERE id=?", (row["amount"], uid))
    c.execute("INSERT INTO balance_history (user_id, delta, reason) VALUES (?,?,?)",
              (uid, row["amount"], f"промокод {row['code']}"))
    _notify(c, uid, f"🎟 Промокод {row['code']}: +{row['amount']} дыма")
    c.commit()
    balance = c.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()["balance"]
    c.close()
    return {"amount": row["amount"], "balance": balance}


def user_predictions(user_row: dict, status: str | None = None, limit: int = 50) -> list[dict]:
    c = appdb.db()
    sql = "SELECT * FROM bets WHERE user_id=?"
    args: list = [user_row["id"]]
    if status and status != "all":
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(min(limit, 100))
    rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    out = []
    for b in rows:
        legs = [dict(r) for r in c.execute(
            "SELECT l.*, m.home_club_id, m.away_club_id FROM bet_legs l "
            "LEFT JOIN matches m ON m.id=l.match_id WHERE l.bet_id=?", (b["id"],)).fetchall()]
        labels = []
        for l in legs:
            mk = c.execute("SELECT label FROM markets WHERE match_id=? AND code=?",
                           (l["match_id"], l["market_code"])).fetchone()
            names = []
            for cid in (l["home_club_id"], l["away_club_id"]):
                cl = c.execute("SELECT name FROM clubs WHERE id=?", (cid,)).fetchone()
                names.append(cl["name"] if cl else "—")
            labels.append(f"{names[0]} — {names[1]}: {mk['label'] if mk else l['market_code']}")
        out.append({
            "id": b["id"], "bet_type": b["bet_type"], "amount": b["amount"],
            "total_odds": b["total_odds"], "potential_win": b["potential_win"],
            "status": b["status"], "legs": len(legs), "legs_label": " + ".join(labels[:5]),
            "cashout_amount": b.get("cashout_amount"),
            "created_at": b["created_at"],
        })
    c.close()
    return out
