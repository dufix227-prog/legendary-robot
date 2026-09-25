"""ГЕЙТ БЛОКА 8: сделка проходит, комиссия 5% сгорает, порог 2М уводит крупную
сделку к судье, свободный агент по рейтинг²×K, обмен (игрок+деньги), откат судьи."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot"))
os.environ["DB_PATH"] = "/tmp/gate8.db"
if os.path.exists("/tmp/gate8.db"):
    os.remove("/tmp/gate8.db")

import db
import transfers as tr

db.init_db()
fails = []


def check(name, cond, detail=""):
    print(f"[{'OK ' if bool(cond) else 'FAIL'}] {name} {detail}")
    if not cond:
        fails.append(name)


def club(name, budget):
    c = db.db()
    cid = c.insert_returning_id("INSERT INTO clubs (name, budget) VALUES (?,?)", (name, budget))
    pid = c.insert_returning_id("INSERT INTO players (username, telegram_id) VALUES (?,?)", (f"o{cid}", 3000 + cid))
    c.execute("INSERT INTO club_players (player_id, club_id, role) VALUES (?,?,'owner')", (pid, cid))
    c.execute("UPDATE clubs SET owner_player_id=? WHERE id=?", (pid, cid))
    c.commit()
    c.close()
    return cid


def card(club_id, name, pos, rating):
    c = db.db()
    cid = c.insert_returning_id("INSERT INTO club_cards (club_id, name, position, rating) VALUES (?,?,?,?)",
                                (club_id, name, pos, rating))
    c.commit()
    c.close()
    return cid


a = club("Аякс", 20_000_000)
b = club("Бенфика", 20_000_000)
ca1 = card(a, "Бергвейн", "ПВ", 84)
cb1 = card(b, "Ди Мария", "ПВ", 86)

# 1) свободный агент: цена = рейтинг² × K (K=1000 → 84² = 7 056 000)
fa = card(None, "Тотти", "ЦП", 80)
check("цена агента = рейтинг²×K", tr.free_agent_price(80) == 6_400_000, str(tr.free_agent_price(80)))
r = tr.sign_free_agent(a, fa)
check("агент подписан, бюджет списан", r["price"] == 6_400_000 and r["budget"] == 20_000_000 - 6_400_000, str(r))
try:
    tr.sign_free_agent(b, fa)
    check("повторная покупка агента отклонена", False)
except tr.TransferError as e:
    check("повторная покупка агента отклонена", e.code == "NOT_FREE")

# 2) лот фикс 1.5М → авто-сделка, комиссия 5% сгорает
lot = tr.create_lot(b, cb1, "fix", 1_500_000)
r2 = tr.buy_lot(a, lot["lot_id"])
check("сделка авто (1.5М ≤ 2М)", r2["status"] == "approved")
check("комиссия 5% = 75 000 сгорела", r2["commission"] == 75_000, str(r2["commission"]))
check("продавец получил 1 425 000", r2["budgets"][b] == 20_000_000 + 1_500_000 - 75_000)
c = db.db()
card_owner = c.execute("SELECT club_id FROM club_cards WHERE id=?", (cb1,)).fetchone()["club_id"]
lot_status = c.execute("SELECT status FROM transfer_lots WHERE id=?", (lot["lot_id"],)).fetchone()["status"]
c.close()
check("карточка у покупателя, лот sold", card_owner == a and lot_status == "sold")

# 3) лот 3.5М > порога 2М → к судье
ca2 = card(a, "Бергвейн-2", "ПВ", 85)
lot2 = tr.create_lot(a, ca2, "fix", 3_500_000)
r3 = tr.buy_lot(b, lot2["lot_id"])
check("крупная сделка ушла судье", r3["status"] == "needs_judge")
c = db.db()
budget_b_mid = c.execute("SELECT budget FROM clubs WHERE id=?", (b,)).fetchone()["budget"]
c.close()
check("деньги не списаны до одобрения", budget_b_mid == r2["budgets"][b], str(budget_b_mid))
r3_ok = tr.judge_decide(r3["transfer_id"], approve=True, judge_tg=999)
check("судья одобрил: сделка исполнена", r3_ok["status"] == "approved")

# 4) судья отклоняет
ca3 = card(a, "Йоргенсен", "ВРТ", 78)
lot3 = tr.create_lot(a, ca3, "fix", 5_000_000)
buy3 = tr.buy_lot(b, lot3["lot_id"])
r4 = tr.judge_decide(buy3["transfer_id"], approve=False, judge_tg=999)
check("судья отклонил, лот вернулся на рынок", r4["status"] == "rejected")
c = db.db()
lot3_status = c.execute("SELECT status FROM transfer_lots WHERE id=?", (lot3["lot_id"],)).fetchone()["status"]
c.close()
check("лот снова open после отклонения", lot3_status == "open")

# 5) обмен: игрок+деньги, вторая сторона подтверждает
ca4 = card(a, "Клэссен", "ЦП", 82)
cb2 = card(b, "Гракса", "ЦОП", 79)
r5 = tr.propose_exchange(a, b, ca4, cb2, 500_000)
r6 = tr.accept_exchange(r5["transfer_id"], 3000 + b)   # владелец Бенфики подтверждает
check("обмен игрок+деньги состоялся", r6["status"] == "approved", str(r6))
c = db.db()
where_ca4 = c.execute("SELECT club_id FROM club_cards WHERE id=?", (ca4,)).fetchone()["club_id"]
where_cb2 = c.execute("SELECT club_id FROM club_cards WHERE id=?", (cb2,)).fetchone()["club_id"]
budget_b = c.execute("SELECT budget FROM clubs WHERE id=?", (b,)).fetchone()["budget"]
c.close()
check("карточки переехали", where_ca4 == b and where_cb2 == a)
check("деньги ушли продавцу карточки", budget_b > r3_ok["budgets"][b])

# 6) запреты
try:
    tr.propose_exchange(a, b, cb1, cb2, 0)   # cb1 уже у a? cb1 куплен a в шаге 2
    # cb1 у a — ок как give; want cb2 — тоже у a теперь → NOT_THEIRS
    check("обмен с самим собой/чужой картой отклонён", False)
except tr.TransferError as e:
    check("обмен с чужой картой отклонён", e.code == "NOT_THEIRS", e.code)
try:
    tr.buy_lot(a, lot["lot_id"])   # лот уже sold
    check("повторная покупка лота отклонена", False)
except tr.TransferError as e:
    check("повторная покупка лота отклонена", e.code == "NOT_OPEN")

# 7) маркет-список с фильтром
m = tr.market_list(position="ВРТ")
check("фильтр по позиции", all(x["position"] == "ВРТ" for x in m["lots"] + m["free_agents"]))

print()
print("GATE 8:", "OK" if not fails else f"ПРОВАЛЫ: {fails}")
if os.path.exists("/tmp/gate8.db"):  # на Postgres файла нет
    os.remove("/tmp/gate8.db")
sys.exit(1 if fails else 0)
