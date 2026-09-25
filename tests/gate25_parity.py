"""ГЕЙТ 25: функции оригинала — бомбардиры (таблица Г/А со скрина и /manual goals=), события
и скрин матча, публичный профиль, «Мои матчи», горячие матчи, движение кэфов, награды
капперам за сезон (один раз), предупреждения о рисках (новое → уведомление админу, «принято»)."""
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
os.environ["ADMIN_IDS"] = "925001"
os.environ["DB_PATH"] = "/tmp/gate25.db"
os.environ["MEDIA_DIR"] = "/tmp/gate25_media"
if os.path.exists("/tmp/gate25.db"):
    os.remove("/tmp/gate25.db")

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import bets_engine  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import league  # noqa: E402
import league_stats as ls  # noqa: E402
import results  # noqa: E402
import settings as appsettings  # noqa: E402

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


def ex(sql, args=()):
    c = db.db()
    c.execute(sql, args)
    c.commit()
    c.close()


def user(tg, balance=20000, admin=0):
    ex("INSERT OR IGNORE INTO users (telegram_id, username, balance, is_admin) VALUES (?,?,?,?)",
       (tg, f"u{tg}", balance, admin))
    return q1("SELECT * FROM users WHERE telegram_id=?", (tg,))


db.init_db()
tid = league.create_league_season("Гейт-25", ["Высший"], "manual", 1, 3)
div = league.tournament_divisions(tid)[0]
OWNER = 925101
c = db.db()
pid = c.insert_returning_id("INSERT INTO players (username, telegram_id) VALUES ('own', ?)", (OWNER,))
c.commit()
c.close()
A = league.create_club("Аякс", None, div["id"], 20_000_000)
B = league.create_club("Милан", None, div["id"], 20_000_000)
C = league.create_club("Порту", None, div["id"], 20_000_000)
D = league.create_club("Бенфика", None, div["id"], 20_000_000)
league.assign_club_owner(A, pid)
league.generate_league_calendar(tid, div["id"])
admin = user(925001, admin=1)
owner = user(OWNER)
b1, b2, b3 = user(925201), user(925202), user(925203)


def match_of(x, y):
    return q1("SELECT * FROM matches WHERE tournament_id=? AND ((home_club_id=? AND away_club_id=?) OR "
              "(home_club_id=? AND away_club_id=?))", (tid, x, y, y, x))


# ===== «Мои матчи» до игр =====
mm = ls.my_matches(OWNER)
check("мои матчи: 3 матча клуба, тур 1 открыт с дедлайном",
      mm["registered"] and len(mm["matches"]) == 3 and mm["matches"][0]["state"] == "open"
      and mm["matches"][0]["deadline"] and all(x["state"] == "upcoming" for x in mm["matches"][1:]), str(mm)[:300])
check("без клуба — registered=false", ls.my_matches(925201)["registered"] is False)

# ===== горячие матчи и риски =====
t1 = league.tour_matches(tid, 1)
m_ab = next(m for m in t1 if A in (m["home_club_id"], m["away_club_id"]))
m_cd = next(m for m in t1 if m["id"] != m_ab["id"])
B = m_ab["away_club_id"] if m_ab["home_club_id"] == A else m_ab["home_club_id"]
mk = lambda mid, code: {"match_id": mid, "market_code": code}  # noqa: E731
bets_engine.place_bet(b1, 3000, [mk(m_cd["id"], "1x2_p1")])
bets_engine.place_bet(b2, 200, [mk(m_cd["id"], "1x2_p1")])
bets_engine.place_bet(b3, 100, [mk(m_cd["id"], "tb25")])
bets_engine.place_bet(b3, 100, [mk(m_ab["id"], "1x2_x")])
hot = ls.hot_matches()
check("горячие: C–D первым, 3 каппера, популярный П1",
      hot[0]["match_id"] == m_cd["id"] and hot[0]["bettors"] == 3 and hot[0]["popular"]["code"] == "1x2_p1", str(hot[:1]))
al = ls.risk_alerts()
kinds = {a["kind"] for a in al if a["match_id"] == m_cd["id"]}
check("риски после ставок: крупная выплата, перекос, кит", {"liability", "one_sided", "whale"} <= kinds, str(al))
n_notif = q1("SELECT COUNT(*) n FROM notifications WHERE user_id=? AND text LIKE '⚠️ Риск%'", (admin["id"],))["n"]
check("админу пришли уведомления", n_notif >= 3, str(n_notif))
ls.risk_scan()
check("повторный скан не дублирует", len(ls.risk_alerts()) == len(al)
      and q1("SELECT COUNT(*) n FROM notifications WHERE user_id=? AND text LIKE '⚠️ Риск%'", (admin["id"],))["n"] == n_notif)
aid = next(a["id"] for a in al if a["kind"] == "whale")
check("принять предупреждение", ls.ack_alert(aid, 925001) and not any(a["id"] == aid for a in ls.risk_alerts()))
ls.risk_scan()
check("принятое не возвращается при том же риске", not any(a["kind"] == "whale" and a["match_id"] == m_cd["id"]
                                                          for a in ls.risk_alerts()))

# ===== результаты: таблица Г/А со скрина, лента голов, /manual =====
players = [{"side": "home", "name": "Mbappé", "goals": 2, "assists": 0},
           {"side": "home", "name": "Vinícius", "goals": 0, "assists": 2},
           {"side": "away", "name": "Leão", "goals": 1, "assists": 0},
           {"side": "away", "name": "Pulisic", "goals": 0, "assists": 1}]
goals = [{"side": "home", "name": "Mbappé", "minute": 12}, {"side": "away", "name": "Leão", "minute": 30},
         {"side": "home", "name": "Mbappé", "minute": 77}]
swap = m_ab["home_club_id"] != A
if swap:  # таблица ориентирована на хозяев календаря
    players = [{**p, "side": "away" if p["side"] == "home" else "home"} for p in players]
    goals = [{**g, "side": "away" if g["side"] == "home" else "home"} for g in goals]
results.finalize_match(m_ab["id"], *((1, 2) if swap else (2, 1)), None, None, goals, actor="ocr", players=players)
ev = ls.match_events(m_ab["id"])
check("события: 3 гола с минутами + 4 игрока с Г/А", len(ev["goals"]) == 3 and len(ev["players"]) == 4
      and ev["goals"][0]["minute"] == 12, str(ev)[:300])
results.finalize_match(m_cd["id"], 3, 0, None, None,
                       [{"side": "home", "name": "Pepê"}, {"side": "home", "name": "Pepê"},
                        {"side": "home", "name": "Mbappé"}], actor="manual")
ts = ls.top_scorers(tid)
top = ts["scorers"][0]
check("бомбардиры: Mbappé (Аякс) 2 гола первым", top["name"] == "Mbappé" and top["club"] == "Аякс" and top["goals"] == 2,
      str(ts["scorers"][:3]))
check("тёзка из другого клуба — отдельная строка", sum(1 for s in ts["scorers"] if s["name"] == "Mbappé") == 2)
check("ассистенты: Vinícius 2", ts["assists"][0]["name"] == "Vinícius" and ts["assists"][0]["assists"] == 2)
results.finalize_match(m_ab["id"], *((1, 2) if swap else (2, 1)), None, None, None, actor="dispute")
check("правка счёта без новой таблицы сохраняет бомбардиров", len(ls.match_events(m_ab["id"])["players"]) == 4)
check("рассчитанные ставки закрывают риски матча", not any(a["match_id"] == m_cd["id"] for a in ls.risk_alerts()))

# ===== скрин =====
png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
check("скрин сохранён", ls.save_screenshot(m_ab["id"], png) == f"{m_ab['id']}.png"
      and ls.match_events(m_ab["id"])["has_photo"])
ls.save_screenshot(m_ab["id"], b"\xff\xd8\xff" + b"1" * 50)
check("новый репорт заменяет скрин", q1("SELECT screenshot_path p FROM matches WHERE id=?", (m_ab["id"],))["p"]
      == f"{m_ab['id']}.jpg" and not os.path.exists(f"/tmp/gate25_media/matches/{m_ab['id']}.png"))
check("имя файла из БД не выходит за папку", ls.screenshot_file({"screenshot_path": "../../etc/passwd"}) is None)

# ===== движение кэфов: результат тура сдвигает линию тура 2 =====
league.open_tour(tid, 2)
for m in league.tour_matches(tid, 2):
    pass
ex("UPDATE odds_history SET odds=odds*1.3 WHERE id IN (SELECT MIN(id) FROM odds_history "
   "WHERE match_id IN (SELECT id FROM matches WHERE tournament_id=? AND tour_number=2) GROUP BY match_id, market_code)",
   (tid,))
mv = ls.odds_movers()
check("движение кэфов: изменения открытой линии, по модулю", mv and all(abs(x["change_pct"]) >= 3 for x in mv)
      and abs(mv[0]["change_pct"]) >= abs(mv[-1]["change_pct"]), str(mv[:2]))

# ===== профиль и награды =====
for m in league.tour_matches(tid, 2) + league.tour_matches(tid, 3):
    if m["status"] == "pending":
        for u, code in ((b1, "1x2_p1"), (b2, "1x2_x"), (b3, "tb15")):
            league.open_tour(tid, m["tour_number"]) if league.ensure_tour(tid, m["tour_number"])["status"] != "open" else None
            try:
                bets_engine.place_bet(q1("SELECT * FROM users WHERE id=?", (u["id"],)), 100, [mk(m["id"], code)])
            except bets_engine.BetError:
                pass
for tour in (2, 3):
    if league.ensure_tour(tid, tour)["status"] != "open":
        league.open_tour(tid, tour)
    for m in league.tour_matches(tid, tour):
        results.finalize_match(m["id"], 2, 0, None, None, [], actor="manual")
pp = ls.public_profile(925201)
check("публичный профиль: статистика, достижения, без чужих секретов",
      pp["name"] == "u925201" and pp["stats"]["settled"] >= 3 and "achievements" in pp and "telegram_id" not in pp, str(pp)[:300])
appsettings.set_setting("award_min_bets", "2")
fin = league.finalize_season(tid, 925001)
aw = fin.get("bettor_awards") or []
check("награды капперам в финале сезона", aw and aw[0]["place"] == 1 and aw[0]["award"] == "champion", str(aw))
champ = q1("SELECT * FROM bettor_awards WHERE place=1")
bal_hist = q1("SELECT delta FROM balance_history WHERE user_id=? AND reason LIKE 'награда сезона%'", (champ["user_id"],))
check("дым зачислен и записан в журнал", bal_hist and bal_hist["delta"] == 3000)
check("повторно не выдаётся", ls.award_bettors(tid) == [])
check("награда видна в профиле", any(a["award"] == "champion" for a in ls.public_profile(
    q1("SELECT telegram_id FROM users WHERE id=?", (champ["user_id"],))["telegram_id"])["awards"]))


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

    async def call(method, path, tg, raw=False):
        r = await client.request(method, path, headers={"X-Telegram-Init-Data": sign(tg)})
        return r.status, (await r.read() if raw else await r.json())

    try:
        st, d = await call("GET", f"/api/tournaments/{tid}/top-scorers?division_id={div['id']}", 925201)
        check("API бомбардиры", st == 200 and d["scorers"][0]["name"] == "Mbappé")
        st, d = await call("GET", f"/api/matches/{m_ab['id']}/events", 925201)
        check("API события", st == 200 and len(d["players"]) == 4 and d["has_photo"])
        st, d = await call("GET", f"/api/matches/{m_ab['id']}", 925201)
        check("карточка матча: has_photo", st == 200 and d["has_photo"] is True)
        st, body = await call("GET", f"/api/matches/{m_ab['id']}/photo", 925201, raw=True)
        check("API скрин", st == 200 and body.startswith(b"\xff\xd8\xff"))
        r = await client.get(f"/api/matches/{m_ab['id']}/photo")
        check("скрин без авторизации — нет", r.status == 401)
        st, d = await call("GET", "/api/player/925202/public", 925201)
        check("API публичный профиль", st == 200 and d["player"]["name"] == "u925202")
        st, _ = await call("GET", "/api/player/999999/public", 925201)
        check("API профиль: нет игрока → 404", st == 404)
        st, d = await call("GET", "/api/cabinet/matches", OWNER)
        check("API мои матчи", st == 200 and d["registered"] and d["recent"])
        st, d = await call("GET", "/api/matches-hot", 925201)
        check("API горячие", st == 200 and "matches" in d)
        st, d = await call("GET", "/api/odds/movers", 925201)
        check("API движение кэфов", st == 200 and "movers" in d)
        st, d = await call("GET", f"/api/season/awards?tournament_id={tid}", 925201)
        check("API награды сезона", st == 200 and d["awards"][0]["title"].startswith("Чемпион"))
        st, _ = await call("GET", "/api/admin/risk/alerts", 925201)
        check("API риски: не админу 403", st == 403)
        st, d = await call("GET", "/api/admin/risk/alerts?status=ack", 925001)
        check("API риски: админ видит принятые", st == 200 and any(a["kind"] == "whale" for a in d["alerts"]))
    finally:
        await client.close()


asyncio.run(run_api())

print()
if fails:
    print(f"GATE 25: {len(fails)} FAIL:", fails)
    sys.exit(1)
print("GATE 25: OK")
