"""ГЕЙТ 16: аукционы и обмены (план 08 B2–B3).
Ставки, шаг, блокировка/возврат сумм, анти-снайпинг, запрет ставки на свой лот,
нехватка бюджета, выкуп, закрытие + комиссия + переезд карточки, порог → судья,
идемпотентное закрытие, обмен/предложение через API, права судьи на decide."""
import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "bot"))
os.environ.setdefault("BOT_TOKEN", "123456:TESTTOKEN")
os.environ["ADMIN_IDS"] = "990001"
os.environ["DB_PATH"] = "/tmp/gate16.db"
if os.path.exists("/tmp/gate16.db"):
    os.remove("/tmp/gate16.db")

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import config  # noqa: E402
import db  # noqa: E402
import transfers as tr  # noqa: E402

db.init_db()
fails = []


def check(name, cond, detail=""):
    print(f"[{'OK ' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        fails.append(name)


def q1(sql, args=()):
    c = db.db()
    r = c.execute(sql, args).fetchone()
    c.close()
    return r


def budget(cid):
    return q1("SELECT budget FROM clubs WHERE id=?", (cid,))["budget"]


def owner_of(cid):
    return q1("SELECT club_id FROM club_cards WHERE id=?", (cid,))["club_id"]


def expect_err(name, fn, code):
    try:
        fn()
        check(name, False, "ошибки не было")
    except tr.TransferError as e:
        check(name, e.code == code, f"{e.code}: {e}")


c = db.db()
t1 = c.insert_returning_id("INSERT INTO tournaments (name) VALUES ('Сезон-А')")
t2 = c.insert_returning_id("INSERT INTO tournaments (name) VALUES ('Сезон-Б')")
d1 = c.insert_returning_id("INSERT INTO divisions (tournament_id, name) VALUES (?, 'Д1')", (t1,))
c.execute("INSERT INTO tournament_admins (tournament_id, telegram_id) VALUES (?, 5001)", (t1,))  # судья турнира А
c.execute("INSERT INTO tournament_admins (tournament_id, telegram_id) VALUES (?, 5002)", (t2,))  # судья чужого
c.commit()
c.close()


def club(name, bud, tg):
    c = db.db()
    cid = c.insert_returning_id("INSERT INTO clubs (name, budget, division_id) VALUES (?,?,?)", (name, bud, d1))
    pid = c.insert_returning_id("INSERT INTO players (username, telegram_id) VALUES (?,?)", (f"o{cid}", tg))
    c.execute("INSERT INTO club_players (player_id, club_id, role) VALUES (?,?,'owner')", (pid, cid))
    c.execute("INSERT INTO users (telegram_id, username, balance) VALUES (?,?,422)", (tg, f"o{cid}"))
    c.commit()
    c.close()
    return cid


def card(club_id, name, rating=80):
    c = db.db()
    cid = c.insert_returning_id("INSERT INTO club_cards (club_id, name, position, rating) VALUES (?,?, 'ЦП', ?)",
                                (club_id, name, rating))
    c.commit()
    c.close()
    return cid


START = 20_000_000
A, B, C_ = club("Аякс", START, 4001), club("Бенфика", START, 4002), club("Челси", START, 4003)
POOR = club("Бедняки", 500_000, 4004)
now0 = datetime(2026, 9, 25, 12, 0, 0)

# ===== 1) ставки, шаг, блокировки =====
ca = card(A, "Бергвейн")
lot = tr.create_lot(A, ca, "auction", 1_000_000, now=now0)["lot_id"]
ends0 = q1("SELECT ends_at FROM transfer_lots WHERE id=?", (lot,))["ends_at"]
check("таймер лота = auction_hours (24 ч)", ends0 == "2026-09-26 12:00:00", ends0)
expect_err("ставка на свой лот запрещена", lambda: tr.place_bid(A, lot, 1_000_000, now=now0), "SELF")
expect_err("ставка ниже старта отклонена", lambda: tr.place_bid(B, lot, 900_000, now=now0), "LOW_BID")
expect_err("нехватка бюджета", lambda: tr.place_bid(POOR, lot, 1_000_000, now=now0), "NO_FUNDS")
r = tr.place_bid(B, lot, 1_000_000, 4002, now=now0 + timedelta(hours=1))
check("первая ставка = старт", r["status"] == "bid" and r["min_next"] == 1_050_000, str(r))
check("ставка заблокировала сумму у B", budget(B) == START - 1_000_000)
expect_err("шаг 5%: 1 040 000 мало", lambda: tr.place_bid(C_, lot, 1_040_000, now=now0 + timedelta(hours=2)), "LOW_BID")
tr.place_bid(C_, lot, 1_050_000, 4003, now=now0 + timedelta(hours=2))
check("перебили: блок B снят, у C заблокировано", budget(B) == START and budget(C_) == START - 1_050_000)
st = [r["status"] for r in db.db().execute("SELECT status FROM transfer_bids WHERE lot_id=? ORDER BY id", (lot,)).fetchall()]
check("статусы ставок outbid/held", st == ["outbid", "held"], str(st))
tr.place_bid(B, lot, 1_200_000, 4002, now=now0 + timedelta(hours=3))
tr.place_bid(B, lot, 1_300_000, 4002, now=now0 + timedelta(hours=4))  # лидер поднимает свою ставку
check("лидер поднял ставку: блок = новая сумма, C свободен",
      budget(B) == START - 1_300_000 and budget(C_) == START, f"{budget(B)} {budget(C_)}")
check("held-ставка одна на лот", q1("SELECT COUNT(*) AS n FROM transfer_bids WHERE lot_id=? AND status='held'", (lot,))["n"] == 1)

# ===== 2) анти-снайпинг =====
r = tr.place_bid(C_, lot, 1_400_000, 4003, now=now0 + timedelta(hours=23, minutes=55))
ends1 = q1("SELECT ends_at FROM transfer_lots WHERE id=?", (lot,))["ends_at"]
check("ставка за 5 мин до конца продлила на 10 мин", r["extended"] and ends1 == "2026-09-26 12:10:00", ends1)
early = tr.close_expired_auctions(now=now0 + timedelta(hours=24, minutes=5))
check("до продления закрытие не происходит", q1("SELECT status FROM transfer_lots WHERE id=?", (lot,))["status"] == "open")
check("очередь ЛС отдала «перебили»", any(tg == 4002 and "перебили" in t for tg, t in early), str(early))
expect_err("ставка после конца отклонена",
           lambda: tr.place_bid(B, lot, 2_000_000, now=now0 + timedelta(hours=24, minutes=11)), "ENDED")

# ===== 3) закрытие: комиссия, карточка, ЛС, идемпотентность =====
msgs = early + tr.close_expired_auctions(now=now0 + timedelta(hours=24, minutes=11))
row = q1("SELECT status FROM transfer_lots WHERE id=?", (lot,))
check("лот продан", row["status"] == "sold")
check("карточка переехала к победителю", owner_of(ca) == C_)
check("продавец получил 1.4М − 5%", budget(A) == START + 1_400_000 - 70_000, str(budget(A)))
check("победитель заплатил ровно ставку (эскроу)", budget(C_) == START - 1_400_000, str(budget(C_)))
check("проигравший получил всё назад", budget(B) == START)
tgs = {tg for tg, _ in msgs}
check("ЛС: победитель, продавец и перебитый", {4001, 4002, 4003} <= tgs, str(msgs))
check("уведомления в центре (kind app)",
      q1("SELECT COUNT(*) AS n FROM notifications n JOIN users u ON u.id=n.user_id "
         "WHERE u.telegram_id=4003 AND n.kind='app'")["n"] >= 2)
snap = (budget(A), budget(B), budget(C_))
again = tr.close_expired_auctions(now=now0 + timedelta(hours=30))
check("повторное закрытие идемпотентно", again == [] and (budget(A), budget(B), budget(C_)) == snap
      and q1("SELECT COUNT(*) AS n FROM transfers WHERE card_id=?", (ca,))["n"] == 1)
check("прямой повторный _finish_auction = skip", tr._finish_auction(lot, now0 + timedelta(hours=31))["status"] == "skip")

# ===== 4) выкуп, снятие лота, без ставок =====
cb = card(A, "Тимбер")
lot_b = tr.create_lot(A, cb, "auction", 800_000, buyout_price=1_500_000, now=now0)["lot_id"]
tr.place_bid(C_, lot_b, 800_000, now=now0 + timedelta(hours=1))
expect_err("аукцион со ставками не снять", lambda: tr.cancel_lot(A, lot_b), "HAS_BIDS")
c_before, b_before, a_before = budget(C_), budget(B), budget(A)
r = tr.place_bid(B, lot_b, 5_000_000, now=now0 + timedelta(hours=2))
check("ставка ≥ выкупа = выкуп по цене выкупа", r["buyout"] and r["amount"] == 1_500_000 and r["status"] == "approved", str(r))
check("выкуп: карточка у B сразу, C вернули ставку",
      owner_of(cb) == B and budget(C_) == c_before + 800_000 and budget(B) == b_before - 1_500_000)
check("выкуп: продавцу −5%", budget(A) == a_before + 1_500_000 - 75_000)
cn = card(A, "Никто")
lot_n = tr.create_lot(A, cn, "auction", 300_000, now=now0)["lot_id"]
expect_err("buy_lot на аукцион без выкупа", lambda: tr.buy_lot(B, lot_n), "AUCTION")
tr.close_expired_auctions(now=now0 + timedelta(days=2))
check("без ставок → expired, карточка у продавца",
      q1("SELECT status FROM transfer_lots WHERE id=?", (lot_n,))["status"] == "expired" and owner_of(cn) == A)

# ===== 5) порог → судья (эскроу держится до решения) =====
cj = card(A, "Де Йонг", 87)
lot_j = tr.create_lot(A, cj, "auction", 2_500_000, now=now0)["lot_id"]
b0 = budget(B)
tr.place_bid(B, lot_j, 2_500_000, now=now0 + timedelta(hours=1))
tr.close_expired_auctions(now=now0 + timedelta(days=2))
tj = q1("SELECT * FROM transfers WHERE lot_id=? AND status='needs_judge'", (lot_j,))
check("аукцион > порога → судье", tj is not None and q1("SELECT status FROM transfer_lots WHERE id=?", (lot_j,))["status"] == "needs_judge")
check("сделке присвоен турнир клубов", tj["tournament_id"] == t1)
check("сумма заблокирована до решения", budget(B) == b0 - 2_500_000)
a_before = budget(A)
tr.judge_decide(tj["id"], True, 5001)
check("судья одобрил: карточка у B, деньги не списаны дважды",
      owner_of(cj) == B and budget(B) == b0 - 2_500_000 and budget(A) == a_before + 2_500_000 - 125_000)
check("та же строка сделки → approved, без дублей",
      q1("SELECT status FROM transfers WHERE id=?", (tj["id"],))["status"] == "approved"
      and q1("SELECT COUNT(*) AS n FROM transfers WHERE card_id=?", (cj,))["n"] == 1
      and q1("SELECT status FROM transfer_lots WHERE id=?", (lot_j,))["status"] == "sold")
cr = card(A, "Пасвер", 81)
lot_r = tr.create_lot(A, cr, "auction", 3_000_000, now=now0)["lot_id"]
b0 = budget(B)
tr.place_bid(B, lot_r, 3_000_000, now=now0 + timedelta(hours=1))
tr.close_expired_auctions(now=now0 + timedelta(days=2))
tr_ = q1("SELECT id FROM transfers WHERE lot_id=?", (lot_r,))
tr.judge_decide(tr_["id"], False, 5001)
check("судья отклонил: ставка вернулась, лот cancelled, карточка у продавца",
      budget(B) == b0 and owner_of(cr) == A and q1("SELECT status FROM transfer_lots WHERE id=?", (lot_r,))["status"] == "cancelled")
# фикс-лот > порога: одобрение закрывает лот (раньше висел needs_judge)
cf = card(A, "Йоргенсен", 78)
lot_f = tr.create_lot(A, cf, "fix", 4_000_000)["lot_id"]
bf = tr.buy_lot(B, lot_f)
tr.judge_decide(bf["transfer_id"], True, 5001)
check("фикс > порога: лот sold, одна сделка",
      q1("SELECT status FROM transfer_lots WHERE id=?", (lot_f,))["status"] == "sold"
      and q1("SELECT COUNT(*) AS n FROM transfers WHERE card_id=?", (cf,))["n"] == 1)

# ===== 6) обмен: карточка на рынке не меняется; денежное предложение =====
cm = card(A, "Клэссен")
tr.create_lot(A, cm, "fix", 1_000_000)
cw = card(B, "Гракса")
expect_err("карточку с рынка в обмен не отдать", lambda: tr.propose_exchange(A, B, cm, cw, 0), "ON_MARKET")
expect_err("пустое предложение отклонено", lambda: tr.propose_exchange(A, B, None, cw, 0), "BAD_PRICE")


# ===== 7) API =====
def sign(user_id: int) -> str:
    pairs = {"auth_date": str(int(time.time())), "user": json.dumps({"id": user_id, "first_name": "T"})}
    dcs = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return {"X-Telegram-Init-Data": urlencode(pairs)}


async def run_api():
    from miniapp.server import build_app
    client = TestClient(TestServer(build_app()))
    await client.start_server()
    hA, hB, hC = sign(4001), sign(4002), sign(4003)
    judge, alien_judge, root = sign(5001), sign(5002), sign(990001)

    async def post(path, h, body=None):
        r = await client.post(path, headers=h, data=json.dumps(body or {}))
        return r.status, await r.json()

    async def get(path, h):
        r = await client.get(path, headers=h)
        return r.status, await r.json()

    ck = card(A, "Кака", 88)
    s, d = await post("/api/transfers/lots", hA, {"card_id": ck, "kind": "auction", "price": 1_000_000,
                                                  "buyout_price": 1_800_000, "hours": 12})
    check("API: аукцион выставлен", s == 200 and d.get("ends_at"), str(d))
    lid = d["lot_id"]
    s, d = await get(f"/api/transfers/lots/{lid}", hB)
    check("API: карточка лота — время и мин. ставка",
          s == 200 and 12 * 3600 - 60 < d["seconds_left"] <= 12 * 3600 and d["min_bid"] == 1_000_000, str(d.get("seconds_left")))
    s, d = await post(f"/api/transfers/lots/{lid}/bid", hA, {"amount": 1_000_000})
    check("API: ставка на свой лот → 400 SELF", s == 400 and d["code"] == "SELF")
    s, d = await post(f"/api/transfers/lots/{lid}/bid", hB, {"amount": 10})
    check("API: мало → 400 LOW_BID", s == 400 and d["code"] == "LOW_BID")
    s, d = await post(f"/api/transfers/lots/{lid}/bid", hB, {"amount": "abc"})
    check("API: мусор → 400", s == 400)
    s, d = await post(f"/api/transfers/lots/{lid}/bid", hB, {"amount": 1_000_000})
    check("API: ставка принята", s == 200 and d["result"] == "bid" and d["min_next"] == 1_050_000, str(d))
    s, d = await get(f"/api/transfers/lots/{lid}", hB)
    check("API: история ставок + лидер", len(d["bids"]) == 1 and d["is_top"] and d["top_club_name"] == "Бенфика")
    s, d = await get("/api/transfers/view", hB)
    check("API: view — заблокировано и мои ставки", d["locked"] == 1_000_000 and d["bids"][0]["id"] == lid, str(d.get("locked")))
    s, d = await get("/api/transfers/market", hC)
    al = [x for x in d["lots"] if x["id"] == lid]
    check("API: маркет отдаёт поля аукциона", al and al[0]["current_price"] == 1_000_000 and al[0]["seconds_left"] > 0)

    # клубы и составы
    s, d = await get("/api/transfers/clubs", hB)
    check("API: клубы без своего", s == 200 and B not in [x["id"] for x in d["clubs"]] and A in [x["id"] for x in d["clubs"]])
    s, d = await get(f"/api/transfers/clubs/{C_}/squad", hB)
    check("API: состав клуба", s == 200 and ca in [x["id"] for x in d["squad"]])

    # обмен карточка+деньги через API
    gb = card(B, "Бро́бби", 79)
    s, d = await post("/api/transfers/exchange", hB, {"to_club_id": C_, "give_card_id": gb, "want_card_id": ca, "money": 300_000})
    check("API: обмен предложен", s == 200 and d.get("transfer_id"), str(d))
    xid = d["transfer_id"]
    s, d = await get("/api/transfers/view", hC)
    check("API: входящее у получателя", xid in [o["id"] for o in d["offers_in"]])
    s, d = await post(f"/api/transfers/exchange/{xid}/accept", hA)
    check("API: чужой не принимает", s == 400 and d["code"] == "NOT_YOURS")
    b0, c0 = budget(B), budget(C_)
    s, d = await post(f"/api/transfers/exchange/{xid}/accept", hC)
    check("API: обмен принят", s == 200 and d["status"] == "approved", str(d))
    check("API: карточки и доплата", owner_of(gb) == C_ and owner_of(ca) == B
          and budget(B) == b0 - 300_000 and budget(C_) == c0 + 300_000)
    s, d = await post("/api/transfers/exchange", hB, {"to_club_id": C_, "want_card_id": 999999})
    check("API: кривой обмен → 400", s == 400)

    # денежное предложение и отклонение/отзыв
    s, d = await post("/api/transfers/exchange", hC, {"to_club_id": B, "want_card_id": gb, "money": 1})
    check("API: чужая карточка → 400 NOT_THEIRS", s == 400 and d["code"] == "NOT_THEIRS")
    s, d = await post("/api/transfers/exchange", hC, {"to_club_id": B, "want_card_id": ca, "money": 1_000_000})
    oid = d["transfer_id"]
    s, d = await post(f"/api/transfers/exchange/{oid}/decline", hB)
    check("API: получатель отклонил", s == 200 and d["status"] == "rejected")
    s, d = await post("/api/transfers/exchange", hC, {"to_club_id": B, "want_card_id": ca, "money": 1_000_000})
    oid = d["transfer_id"]
    b0, c0 = budget(B), budget(C_)
    s, d = await post(f"/api/transfers/exchange/{oid}/accept", hB)
    check("API: денежное предложение принято, комиссия 5%", s == 200 and owner_of(ca) == C_
          and budget(C_) == c0 - 1_000_000 and budget(B) == b0 + 950_000, str(d))

    # судья: крупная фикс-сделка
    cbig = card(A, "Тотти", 85)
    s, d = await post("/api/transfers/lots", hA, {"card_id": cbig, "kind": "fix", "price": 3_000_000})
    s, d = await post(f"/api/transfers/lots/{d['lot_id']}/buy", hB)
    check("API: крупная покупка → судье", d.get("status") == "needs_judge", str(d))
    did = d["transfer_id"]
    s, d = await get("/api/transfers/judge", alien_judge)
    check("API: судья чужого турнира очередь не видит", s == 200 and did not in [x["id"] for x in d["deals"]])
    s, d = await get("/api/transfers/judge", judge)
    check("API: судья турнира видит сделку", did in [x["id"] for x in d["deals"]])
    s, d = await get("/api/transfers/judge", root)
    check("API: root видит сделку", did in [x["id"] for x in d["deals"]])
    s, d = await post(f"/api/transfers/deals/{did}/decide", alien_judge, {"approve": True})
    check("API: судья чужого турнира → 403", s == 403)
    s, d = await post(f"/api/transfers/deals/{did}/decide", hB, {"approve": True})
    check("API: участник сделки → 403", s == 403)
    s, d = await post(f"/api/transfers/deals/{did}/decide", hC, {"approve": True})
    check("API: владелец клуба без прав → 403", s == 403)
    s, d = await post(f"/api/transfers/deals/{did}/decide", judge, {"approve": True})
    check("API: судья турнира одобрил", s == 200 and d["status"] == "approved" and owner_of(cbig) == B, str(d))
    s, d = await post(f"/api/transfers/deals/{did}/decide", judge, {"approve": True})
    check("API: повторно не решить", s == 400)

    await client.close()


asyncio.run(run_api())
print("\nGATE 16:", "OK" if not fails else f"FAIL {fails}")
if os.path.exists("/tmp/gate16.db"):  # на Postgres файла нет
    os.remove("/tmp/gate16.db")
sys.exit(1 if fails else 0)
