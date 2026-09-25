"""ГЕЙТ 21: расширенная линия (35 рынков, расчёт = модель), кэшаут (квота, смена суммы,
закрытие по локу тура, без двойной выплаты), черновики купонов, статистика каппера
(ROI/проходимость/ср. кэф), рейтинги по сезону и дивизиону, H2H/аналитика, «как получен кэф», value."""
import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
from urllib.parse import urlencode

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "bot"))
os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ["ADMIN_IDS"] = "921001"
os.environ["DB_PATH"] = "/tmp/gate21.db"
if os.path.exists("/tmp/gate21.db"):
    os.remove("/tmp/gate21.db")

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import bet_tools  # noqa: E402
import bets_engine  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import league  # noqa: E402
import markets  # noqa: E402
import match_insights  # noqa: E402
import results  # noqa: E402
import settings as appsettings  # noqa: E402
from bets_engine import BetError  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(f"[{'OK ' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        fails.append(name)


def q1(sql, args=()):
    c = db.db()
    r = c.execute(sql, args).fetchone()
    c.close()
    return dict(r) if r else None


def user(tg, balance=5000):
    c = db.db()
    c.execute("INSERT OR IGNORE INTO users (telegram_id, username, balance) VALUES (?,?,?)", (tg, f"u{tg}", balance))
    c.commit()
    row = dict(c.execute("SELECT * FROM users WHERE telegram_id=?", (tg,)).fetchone())
    c.close()
    return row


def bal(u):
    return q1("SELECT balance FROM users WHERE id=?", (u["id"],))["balance"]


def odds_of(mid, code):
    return q1("SELECT odds FROM markets WHERE match_id=? AND code=?", (mid, code))["odds"]


mk = lambda mid, code: {"match_id": mid, "market_code": code}  # noqa: E731

# ===== модель: вероятности и расчёт из одной таблицы =====
db.init_db()
probs = markets.market_probs(1100, 950)
check("35 рынков в модели", len(markets.MARKET_LABELS) == 35 and set(probs) == set(markets.MARKET_LABELS),
      str(len(markets.MARKET_LABELS)))
check("1X2 в сумме = 1", abs(probs["1x2_p1"] + probs["1x2_x"] + probs["1x2_p2"] - 1) < 1e-9)
check("двойной шанс = сумма исходов", abs(probs["dc_1x"] - probs["1x2_p1"] - probs["1x2_x"]) < 1e-9)
check("ТБ/ТМ 1.5 и 3.5 дополняют друг друга",
      abs(probs["tb15"] + probs["tm15"] - 1) < 1e-9 and abs(probs["tb35"] + probs["tm35"] - 1) < 1e-9)
check("точные счета < 1 в сумме", 0.5 < sum(v for k, v in probs.items() if k.startswith("cs_")) < 1)
odds = markets.compute_odds(1600, 600)
check("кэф не ниже 1.01 даже для суперфаворита", min(odds.values()) >= 1.01, str(min(odds.values())))
cases = [("dc_x2", 1, 1, "won"), ("dc_x2", 2, 1, "lost"), ("dc_12", 0, 0, "lost"), ("tb35", 2, 2, "won"),
         ("tm15", 1, 0, "won"), ("itb_a05", 3, 0, "lost"), ("cs_2_1", 2, 1, "won"), ("cs_2_1", 1, 2, "lost"),
         ("ah_a15", 2, 1, "won"), ("unknown", 1, 1, "void")]
check("resolve_leg новых рынков", all(markets.resolve_leg(0, c, h, a) == r for c, h, a, r in cases),
      str([(c, markets.resolve_leg(0, c, h, a)) for c, h, a, r in cases if markets.resolve_leg(0, c, h, a) != r]))
ex = markets.explain(1100, 950)
check("explain: λ, маржа и кэф рынка совпадают с линией",
      ex["lambda_home"] > ex["lambda_away"] and ex["margin_pct"] == 5.0
      and ex["markets"]["1x2_p1"]["odds"] == markets.compute_odds(1100, 950)["1x2_p1"])

# ===== сезон: 2 дивизиона по 4 клуба =====
tid = league.create_league_season("Гейт-21", ["Высший", "Первый"], "manual", 1, 3)
divs = league.tournament_divisions(tid)
clubs = {}
for d, names in zip(divs, (["Аякс", "Милан", "Порту", "Бенфика"], ["Лион", "Севилья", "Брюгге", "Селтик"])):
    for n in names:
        clubs[n] = league.create_club(n, None, d["id"], 20_000_000)
    league.generate_league_calendar(tid, d["id"])
markets.refresh_tour(tid, 1)
t1 = league.tour_matches(tid, 1)
check("линия тура: у матча 35 рынков",
      q1("SELECT COUNT(*) n FROM markets WHERE match_id=?", (t1[0]["id"],))["n"] == 35)
div_of = lambda m: divs[0]["id"] if q1("SELECT division_id FROM clubs WHERE id=?", (m["home_club_id"],))["division_id"] == divs[0]["id"] else divs[1]["id"]  # noqa: E731
top = [m for m in t1 if div_of(m) == divs[0]["id"]]
low = [m for m in t1 if div_of(m) == divs[1]["id"]]
mA, mB = top[0]["id"], top[1]["id"]
mC = low[0]["id"]

ua, ub, uc = user(921101), user(921102), user(921103)

# ===== кэшаут =====
b1 = bets_engine.place_bet(ua, 100, [mk(mA, "1x2_p1"), mk(mB, "tb25")])["bet_id"]
qt = bet_tools.cashout_quote(ua, b1)
pw = q1("SELECT potential_win FROM bets WHERE id=?", (b1,))["potential_win"]
check("квота кэшаута: доступна, 0 < сумма < выплаты", qt["available"] and 0 < qt["amount"] < pw, str(qt))
p_exp = markets.market_probs(1000, 1000)
expect = int(pw * p_exp["1x2_p1"] * p_exp["tb25"] * 0.92)
check("квота = выплата × P(ноги) × (1 − 8%)", abs(qt["amount"] - expect) <= 1, f"{qt['amount']} vs {expect}")
try:
    bet_tools.cashout(ua, b1, qt["amount"] + 5)
    check("CASHOUT_CHANGED при устаревшей сумме", False)
except BetError as e:
    check("CASHOUT_CHANGED при устаревшей сумме", e.code == "CASHOUT_CHANGED" and e.extra["quote"]["amount"] == qt["amount"])
try:
    bet_tools.cashout_quote(ub, b1)
    check("чужой купон не виден", False)
except BetError as e:
    check("чужой купон не виден", e.code == "NO_BET")
before = bal(ua)
r = bet_tools.cashout(ua, b1, qt["amount"])
check("кэшаут зачислен", bal(ua) == before + qt["amount"] and r["balance"] == bal(ua))
row = q1("SELECT status, cashout_amount FROM bets WHERE id=?", (b1,))
check("купон в статусе cashout", row["status"] == "cashout" and row["cashout_amount"] == qt["amount"])
try:
    bet_tools.cashout(ua, b1)
    check("повторный кэшаут запрещён", False)
except BetError as e:
    check("повторный кэшаут запрещён", e.code == "CASHOUT_UNAVAILABLE")
b_cs = bets_engine.place_bet(ua, 50, [mk(mA, "cs_1_0")])["bet_id"]
b2 = bets_engine.place_bet(ub, 100, [mk(mA, "1x2_p1")])["bet_id"]
b3 = bets_engine.place_bet(ub, 100, [mk(mB, "dc_x2")])["bet_id"]
b4 = bets_engine.place_bet(uc, 200, [mk(mC, "1x2_p1")])["bet_id"]
b5 = bets_engine.place_bet(uc, 100, [mk(mA, "1x2_p2")])["bet_id"]
appsettings.set_setting("cashout_enabled", "0")
check("кэшаут выключается настройкой", not bet_tools.cashout_quote(ub, b2)["available"])
appsettings.set_setting("cashout_enabled", "1")

# матч A: 1:0 → b1 (кэшаут) не пересчитывается, cs_1_0 и b2 выигрывают, b5 проиграл
ua_before = bal(ua)
results.finalize_match(mA, 1, 0, None, None, [], actor="manual")
check("кэшаутнутый купон не рассчитывается повторно",
      q1("SELECT status FROM bets WHERE id=?", (b1,))["status"] == "cashout")
check("точный счёт 1:0 сыграл", q1("SELECT status FROM bets WHERE id=?", (b_cs,))["status"] == "won")
check("выплата по cs_1_0 = 50 × кэф", bal(ua) == ua_before + int(50 * odds_of(mA, "cs_1_0")),
      f"{bal(ua) - ua_before}")
results.finalize_match(mA, 0, 2, None, None, [], actor="manual")  # правка счёта
check("правка счёта не трогает кэшаут", q1("SELECT status FROM bets WHERE id=?", (b1,))["status"] == "cashout")
results.finalize_match(mA, 1, 0, None, None, [], actor="manual")
c = db.db()
c.execute("UPDATE tours SET status='locked' WHERE tournament_id=? AND tour_number=1", (tid,))
c.commit()
c.close()
qt3 = bet_tools.cashout_quote(ub, b3)
check("тур залочен → кэшаут закрыт", not qt3["available"] and "начался" in qt3["reason"], str(qt3))
results.finalize_match(mB, 1, 1, None, None, [], actor="manual")
results.finalize_match(mC, 2, 0, None, None, [], actor="manual")

# ===== черновики =====
d = bet_tools.save_draft(ua, [mk(mB, "tb25"), mk(t1[-1]["id"], "1x2_x")], 70, "Вечерний")
check("черновик сохранён с именем", d["name"] == "Вечерний" and d["amount"] == 70 and len(d["legs"]) == 2)
check("закрытые матчи помечены недоступными", d["available_legs"] == 0, str(d["legs"]))
try:
    bet_tools.save_draft(ua, [mk(mB, "tb25"), mk(mB, "tm25")])
    check("один матч — одна нога в черновике", False)
except BetError as e:
    check("один матч — одна нога в черновике", e.code == "DUP_MATCH")
appsettings.set_setting("saved_coupons_max", "1")
try:
    bet_tools.save_draft(ua, [mk(mB, "tb25")])
    check("лимит черновиков", False)
except BetError as e:
    check("лимит черновиков", e.code == "DRAFTS_LIMIT")
appsettings.set_setting("saved_coupons_max", "10")

# ===== статистика =====
st = bet_tools.bet_stats(ub)
pay2 = q1("SELECT potential_win FROM bets WHERE id=?", (b2,))["potential_win"]
pay3 = q1("SELECT potential_win FROM bets WHERE id=?", (b3,))["potential_win"]
exp_roi = round((pay2 + pay3 - 200) / 200 * 100, 1)
check("статистика: 2 из 2, ROI и ср. кэф",
      st["won"] == 2 and st["hit_rate"] == 100.0 and st["roi"] == exp_roi
      and st["avg_odds"] == round((odds_of(mA, "1x2_p1") + odds_of(mB, "dc_x2")) / 2, 2), str(st))
sa = bet_tools.bet_stats(ua)
check("кэшаут в статистике и ROI", sa["cashout"] == 1 and sa["staked"] == 150
      and sa["returned"] == qt["amount"] + int(50 * odds_of(mA, "cs_1_0")), str(sa))
check("разбивка по рынкам", any(g["group"] == "Точный счёт" and g["won"] == 1 for g in sa["by_market"]))

# ===== рейтинги =====
lb = bet_tools.leaderboard("season", tid, viewer_id=ub["id"])
names = [r["name"] for r in lb["leaders"]]
check("рейтинг сезона: все трое, по прибыли", set(names) == {"u921101", "u921102", "u921103"}
      and all(lb["leaders"][i]["profit"] >= lb["leaders"][i + 1]["profit"] for i in range(len(names) - 1)), str(lb))
check("рейтинг: своя строка", lb["me"] and lb["me"]["name"] == "u921102")
lbd = bet_tools.leaderboard("division", divs[1]["id"])
check("рейтинг дивизиона: только ставки на его матчи",
      [r["name"] for r in lbd["leaders"]] == ["u921103"] and lbd["leaders"][0]["bets"] == 1, str(lbd))

# ===== аналитика: H2H, форма, explain, value =====
ins = match_insights.insights(mA)
check("H2H: 1 встреча, форма и статы", ins["h2h"]["total"] == 1 and ins["home"]["recent"][0]["result"] == "W"
      and ins["home"]["stats"]["games"] == 1, str(ins["h2h"]))
exp = match_insights.explain(mB)
check("explain по матчу: Elo клубов после тура и все рынки", len(exp["markets"]) == 35 and exp["elo_home"] > 0)
# value: сыграем тур 2 и 3 так, чтобы у «Аякса» был огромный тотал — модель по голам уйдёт выше Elo
for tour in (2, 3):
    markets.refresh_tour(tid, tour)
    for m in league.tour_matches(tid, tour):
        results.finalize_match(m["id"], 5, 4, None, None, [], actor="manual")
c = db.db()
c.execute("UPDATE matches SET status='pending', score1=NULL, score2=NULL, elo_before=NULL WHERE id=?", (mB,))
c.execute("UPDATE tours SET status='open' WHERE tournament_id=? AND tour_number=1", (tid,))
c.commit()
c.close()
markets.generate_markets(mB)
appsettings.set_setting("value_min_games", "2")
radar = match_insights.value_radar()
check("value: ТБ на результативных клубах", any(p["match_id"] == mB and p["market_code"] in ("tb25", "tb35", "btts_yes")
                                                for p in radar), str(radar[:3]))
check("value: перевес ≥ порога", all(p["edge_pp"] >= 5 for p in radar))
appsettings.set_setting("value_min_games", "50")
check("value: мало матчей → без подсветки", match_insights.value_radar() == [])
appsettings.set_setting("value_min_games", "2")


# ===== API =====
def sign(user_id: int) -> str:
    pairs = {"auth_date": str(int(time.time())), "user": json.dumps({"id": user_id, "first_name": "T"})}
    dcs = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


async def run_api():
    from miniapp.server import build_app
    client = TestClient(TestServer(build_app()))
    await client.start_server()
    H = lambda tg: {"X-Telegram-Init-Data": sign(tg)}  # noqa: E731

    async def call(method, path, tg, body=None):
        r = await client.request(method, path, headers=H(tg), data=json.dumps(body) if body is not None else None)
        return r.status, await r.json()

    try:
        bid = bets_engine.place_bet(uc, 100, [mk(mB, "1x2_x")])["bet_id"]
        st, qd = await call("GET", f"/api/predictions/{bid}/cashout-quote", 921103)
        check("API квота", st == 200 and qd["available"], str(qd))
        st, qs = await call("GET", "/api/cashout/quotes", 921103)
        check("API квоты по открытым", st == 200 and str(bid) in qs["quotes"])
        st, co = await call("POST", f"/api/predictions/{bid}/cashout", 921103, {"amount": qd["amount"] - 1})
        check("API кэшаут: смена суммы → 400 с новой квотой", st == 400 and co["code"] == "CASHOUT_CHANGED" and co["quote"])
        st, co = await call("POST", f"/api/predictions/{bid}/cashout", 921103, {"amount": qd["amount"]})
        check("API кэшаут", st == 200 and co["amount"] == qd["amount"])
        st, pr = await call("GET", "/api/predictions?limit=5", 921103)
        check("история: статус и сумма кэшаута", any(p["id"] == bid and p["status"] == "cashout"
                                                     and p["cashout_amount"] == qd["amount"] for p in pr["predictions"]))
        st, sv = await call("POST", "/api/saved-coupons", 921103, {"legs": [mk(mB, "tb25")], "amount": 40})
        check("API черновик: создан, нога доступна", st == 200 and sv["coupon"]["available_legs"] == 1, str(sv))
        st, ls = await call("GET", "/api/saved-coupons", 921103)
        check("API черновики: список", st == 200 and len(ls["saved_coupons"]) == 1)
        st, _ = await call("DELETE", f"/api/saved-coupons/{sv['coupon']['id']}", 921102)
        check("API черновик: чужой не удалить", st == 404)
        st, _ = await call("DELETE", f"/api/saved-coupons/{sv['coupon']['id']}", 921103)
        check("API черновик: удалён", st == 200)
        st, bs = await call("GET", "/api/profile/bet-stats", 921102)
        check("API статистика", st == 200 and bs["won"] == 2)
        st, sc = await call("GET", "/api/leaderboard/scopes", 921102)
        check("API рейтинги: сезоны и дивизионы", st == 200 and sc["seasons"][0]["id"] == tid and len(sc["divisions"]) == 2)
        st, lbs = await call("GET", f"/api/leaderboard/season?tournament_id={tid}", 921102)
        check("API рейтинг сезона", st == 200 and lbs["me"]["name"] == "u921102")
        st, lbd2 = await call("GET", f"/api/leaderboard/division?division_id={divs[1]['id']}", 921102)
        check("API рейтинг дивизиона", st == 200 and len(lbd2["leaders"]) == 1)
        st, _ = await call("GET", "/api/leaderboard/division?division_id=99999", 921102)
        check("API рейтинг: нет дивизиона → 404", st == 404)
        st, ins2 = await call("GET", f"/api/matches/{mA}/insights", 921102)
        check("API H2H/аналитика", st == 200 and ins2["h2h"]["total"] >= 1)
        st, exj = await call("GET", f"/api/matches/{mB}/odds-explain", 921102)
        check("API как получен кэф", st == 200 and exj["lambda_home"] and any(r["value"] for r in exj["markets"]))
        st, vr = await call("GET", "/api/intelligence/value", 921102)
        check("API value", st == 200 and vr["count"] >= 1)
        st, _ = await call("GET", "/api/matches/999999/insights", 921102)
        check("API аналитика: нет матча → 404", st == 404)
    finally:
        await client.close()


asyncio.run(run_api())

print()
if fails:
    print(f"GATE 21: {len(fails)} FAIL:", fails)
    sys.exit(1)
print("GATE 21: OK")
