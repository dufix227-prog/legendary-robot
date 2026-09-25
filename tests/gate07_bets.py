"""ГЕЙТ БЛОКА 7: ординар и экспресс ставятся и рассчитываются, ODDS_CHANGED,
запреты владельца/судьи, авто-обрез выплаты, idempotency, void, XP/уровень,
бонус-серия и промокод."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot"))
os.environ["DB_PATH"] = "/tmp/gate7.db"
if os.path.exists("/tmp/gate7.db"):
    os.remove("/tmp/gate7.db")

import db
import league
import markets
import results
import bets_engine
from bets_engine import BetError

db.init_db()
fails = []


def check(name, cond, detail=""):
    print(f"[{'OK ' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        fails.append(name)


def user(tg_id, is_admin=0, balance=422):
    c = db.db()
    c.execute("INSERT OR IGNORE INTO users (telegram_id, username, balance, is_admin) VALUES (?,?,?,?)",
              (tg_id, f"u{tg_id}", balance, is_admin))
    c.commit()
    row = dict(c.execute("SELECT * FROM users WHERE telegram_id=?", (tg_id,)).fetchone())
    c.close()
    return row


# сезон: 4 клуба, 2 без владельцев (чужие), тур открыт с кэфами
tid = league.create_league_season("Гейт-ставки", 1, "manual", 1, 3)
div = league.tournament_divisions(tid)[0]
c = db.db()
pl1 = c.insert_returning_id("INSERT INTO players (username, telegram_id) VALUES ('own1', 2001)")
pl2 = c.insert_returning_id("INSERT INTO players (username, telegram_id) VALUES ('own2', 2002)")
judge_tg = 2099
c.commit()
c.close()
cl1 = league.create_club("Аякс", None, div["id"], 20000000)
cl2 = league.create_club("Ливерпуль", None, div["id"], 20000000)
cl3 = league.create_club("Милан", None, div["id"], 20000000)
cl4 = league.create_club("Порту", None, div["id"], 20000000)
league.assign_club_owner(cl1, pl1)
league.assign_club_owner(cl2, pl2)
league.generate_league_calendar(tid, div["id"])
markets.refresh_tour(tid, 1)
ms = league.tour_matches(tid, 1)
m1, m2 = ms[0]["id"], ms[1]["id"]

u_plain = user(2100)              # обычный юзер, баланс 422
u_owner = user(2001)              # владелец Аякса
u_judge = user(judge_tg, is_admin=1)
c = db.db()
c.execute("INSERT OR IGNORE INTO tournament_admins (tournament_id, telegram_id) VALUES (?,?)", (tid, judge_tg))
c.commit(); c.close()

mk = lambda mid, code: {"match_id": mid, "market_code": code}
c = db.db()
odds1 = dict(c.execute("SELECT code, odds FROM markets WHERE match_id=?", (m1,)).fetchall())
odds2 = dict(c.execute("SELECT code, odds FROM markets WHERE match_id=?", (m2,)).fetchall())
c.close()
print("кэфы m1:", odds1)

# 1) ординар
r = bets_engine.place_bet(u_plain, 50, [mk(m1, "1x2_p1")], "idem-1")
check("ординар принят", r["bet_id"] and not r.get("duplicate"))
c = db.db()
bal = c.execute("SELECT balance FROM users WHERE id=?", (u_plain["id"],)).fetchone()["balance"]
c.close()
check("списание 50", bal == 422 - 50, str(bal))

# 2) idempotency: тот же ключ → дубликат без списания
r2 = bets_engine.place_bet(u_plain, 50, [mk(m1, "1x2_p1")], "idem-1")
check("idempotency повтор", r2.get("duplicate") is True)

# 3) ODDS_CHANGED: клиентский кэф врёт
try:
    bets_engine.place_bet(u_plain, 10, [{**mk(m1, "1x2_x"), "odds": 99.0}])
    check("ODDS_CHANGED", False)
except BetError as e:
    check("ODDS_CHANGED", e.code == "ODDS_CHANGED", e.code)

# 4) владелец не ставит на свой клуб
try:
    bets_engine.place_bet(u_owner, 10, [mk(m1, "1x2_x")])
    check("запрет владельцу", False)
except BetError as e:
    check("запрет владельцу", e.code == "OWN_CLUB", e.code)

# 5) судья не ставит на судимый матч
try:
    bets_engine.place_bet(u_judge, 10, [mk(m1, "1x2_x")])
    check("запрет судье", False)
except BetError as e:
    check("запрет судье", e.code == "JUDGE_MATCH", e.code)

# 6) экспресс с авто-обрезом выплаты: кэф ~ (p1*x) монстр-стакан → payout обрезан до 10000
legs = [mk(m1, "1x2_p1"), mk(m2, "1x2_p2")]
r3 = bets_engine.place_bet(user(2101, balance=100000), 5000, legs)
check("экспресс принят, выплата обрезана до 10000", r3["potential_win"] == 10000, str(r3))

# 7) залоченный тур не принимает
league.open_tour(tid, 1)  # остаётся open; залочим вручную:
c = db.db()
c.execute("UPDATE tours SET status='locked' WHERE tournament_id=? AND tour_number=1", (tid,))
c.commit(); c.close()
try:
    bets_engine.place_bet(u_plain, 10, [mk(m1, "1x2_x")])
    check("TOUR_LOCKED", False)
except BetError as e:
    check("TOUR_LOCKED", e.code == "TOUR_LOCKED", e.code)
c = db.db()
c.execute("UPDATE tours SET status='open' WHERE tournament_id=? AND tour_number=1", (tid,))
c.commit(); c.close()

# 8) расчёт: m1 — П1 зашёл; m2 — П2 зашёл → оба купона выиграли
c = db.db()
c.execute("UPDATE matches SET score1=2, score2=0, status='confirmed', played_at=datetime('now') WHERE id=?", (m1,))
c.execute("UPDATE matches SET score1=1, score2=3, status='confirmed', played_at=datetime('now') WHERE id=?", (m2,))
c.commit(); c.close()
s1 = bets_engine.settle_match(m1)
s2 = bets_engine.settle_match(m2)
check("рассчитано 2 купона", s1["settled"] + s2["settled"] == 2, f"{s1}{s2}")
c = db.db()
b1 = dict(c.execute("SELECT * FROM bets WHERE id=?", (r["bet_id"],)).fetchone())
b3 = dict(c.execute("SELECT * FROM bets WHERE id=?", (r3["bet_id"],)).fetchone())
u_after = dict(c.execute("SELECT * FROM users WHERE id=?", (u_plain["id"],)).fetchone())
u3_after = dict(c.execute("SELECT * FROM users WHERE id=?", (user(2101)["id"],)).fetchone())
check("ординар выиграл: выплата на баланс", b1["status"] == "won" and u_after["balance"] == 422 - 50 + b1["potential_win"],
      f"{b1['status']} {u_after['balance']}")
check("XP +100 и уровень растёт при переходе порога", u3_after["xp"] == 100 and u3_after["level"] == 1,
      f"xp={u3_after['xp']} lvl={u3_after['level']}")
check("экспресс выиграл: выплата = 10000 (обрез)", b3["status"] == "won" and b3["potential_win"] == 10000)
c.close()

# 9) проигрыш: возвращаем m1 в pending, ставим против хозяев, подтверждаем 2:0
c = db.db()
c.execute("UPDATE matches SET status='pending', score1=NULL, score2=NULL WHERE id=?", (m1,))
c.commit(); c.close()
r4 = bets_engine.place_bet(u_plain, 30, [mk(m1, "1x2_p2")])
c = db.db()
c.execute("UPDATE matches SET score1=2, score2=0, status='confirmed' WHERE id=?", (m1,))
c.commit(); c.close()
bets_engine.settle_match(m1)
c = db.db()
b4 = dict(c.execute("SELECT * FROM bets WHERE id=?", (r4["bet_id"],)).fetchone())
check("проигрыш рассчитан", b4["status"] == "lost")
c.close()

# 10) void = возврат + причина (возвращаем m2 в pending для ставки)
c = db.db()
c.execute("UPDATE matches SET status='pending', score1=NULL, score2=NULL WHERE id=?", (m2,))
c.commit(); c.close()
r5 = bets_engine.place_bet(u_plain, 20, [mk(m2, "btts_yes")])
v = bets_engine.void_bet(r5["bet_id"], " suspicious ")
c = db.db()
bal5 = c.execute("SELECT balance FROM users WHERE id=?", (u_plain["id"],)).fetchone()["balance"]
notif = c.execute("SELECT text FROM notifications WHERE user_id=? AND text LIKE '%возвращено%'", (u_plain["id"],)).fetchone()
c.close()
check("void возврат", v["refund"] == 20 and bal5 >= 422, str(bal5))
check("void уведомление с причиной", bool(notif))

# 11) бонус-серия
st = bets_engine.claim_streak(u_plain)
check("бонус 50 в первый день", st["amount"] == 50 and st["streak_days"] == 1, str(st))
try:
    bets_engine.claim_streak(u_plain)
    check("повторный бонус сегодня отклонён", False)
except BetError as e:
    check("повторный бонус сегодня отклонён", e.code == "ALREADY")
import bets_engine as be
from datetime import date, timedelta
c = db.db()
c.execute("UPDATE user_streaks SET last_bonus_date=? WHERE user_id=?",
          ((date.today() - timedelta(days=1)).isoformat(), u_plain["id"]))
c.commit(); c.close()
st2 = bets_engine.claim_streak(u_plain)
check("серия 2 дня → 60", st2["amount"] == 60 and st2["streak_days"] == 2, str(st2))

# 12) промокод
c = db.db()
c.execute("INSERT INTO promo_codes (code, amount, max_activations, deadline) VALUES ('SIGAR50', 50, 2, NULL)")
c.commit(); c.close()
pr = bets_engine.redeem_promo(u_plain, "sigar50")
check("промокод +50 (регистронезависимо)", pr["amount"] == 50, str(pr))
try:
    bets_engine.redeem_promo(u_plain, "SIGAR50")
    check("повторный промокод отклонён", False)
except BetError as e:
    check("повторный промокод отклонён", e.code == "DUP")

# 13) лимит открытых купонов (6)
u6 = user(2102)
made = 0
for i in range(7):
    try:
        bets_engine.place_bet(u6, 10, [mk(m2, "1x2_x")])
        made += 1
    except BetError as e:
        check(f"лимит 6 купонов на попытке {i+1}", made == 6 and e.code == "MAX_OPEN_BETS")
        break

print()
print("GATE 7:", "OK" if not fails else f"ПРОВАЛЫ: {fails}")
if os.path.exists("/tmp/gate7.db"):  # на Postgres файла нет
    os.remove("/tmp/gate7.db")
sys.exit(1 if fails else 0)
