"""ГЕЙТ 22: клубы — оформление (цвета/эмблема/стадион, права владельца), доход со стадиона
и от спонсора при подтверждении матча, пересчёт при правке счёта без задвоения, апгрейд
стадиона, ввод времени матча участниками + уведомление сопернику; API и поля в линии."""
import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "bot"))
os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ["ADMIN_IDS"] = "922001"
os.environ["DB_PATH"] = "/tmp/gate22.db"
if os.path.exists("/tmp/gate22.db"):
    os.remove("/tmp/gate22.db")

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import club_economy as ce  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import league  # noqa: E402
import markets  # noqa: E402
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


def budget(cid):
    return q1("SELECT budget FROM clubs WHERE id=?", (cid,))["budget"]


def expect_err(name, code, fn, *a):
    try:
        fn(*a)
        check(name, False, "нет ошибки")
    except ce.ClubError as e:
        check(name, e.code == code, e.code)


db.init_db()
OWNER_A, OWNER_B, STRANGER, ADMIN = 922101, 922102, 922103, 922001
tid = league.create_league_season("Гейт-22", 1, "manual", 1, 3)
div = league.tournament_divisions(tid)[0]
c = db.db()
pa = c.insert_returning_id("INSERT INTO players (username, telegram_id) VALUES ('oa', ?)", (OWNER_A,))
pb = c.insert_returning_id("INSERT INTO players (username, telegram_id) VALUES ('ob', ?)", (OWNER_B,))
ps = c.insert_returning_id("INSERT INTO players (username, telegram_id) VALUES ('st', ?)", (STRANGER,))
for tg in (OWNER_A, OWNER_B, STRANGER, ADMIN):
    c.execute("INSERT OR IGNORE INTO users (telegram_id, username) VALUES (?,?)", (tg, f"u{tg}"))
c.commit()
c.close()
A = league.create_club("Аякс", None, div["id"], 20_000_000)
B = league.create_club("Милан", None, div["id"], 20_000_000)
C = league.create_club("Порту", None, div["id"], 20_000_000)
D = league.create_club("Бенфика", None, div["id"], 20_000_000)
league.assign_club_owner(A, pa)
league.assign_club_owner(B, pb)
league.generate_league_calendar(tid, div["id"])
markets.refresh_tour(tid, 1)

# ===== оформление =====
ov = ce.update_profile(OWNER_A, A, {"color1": "#FF0000", "color2": "#ffffff", "emblem": "🦁",
                                    "motto": "  Вперёд,   Аякс ", "stadium_name": "Йохан Кройф Арена"})
check("владелец меняет оформление", ov["profile"] == {"color1": "#ff0000", "color2": "#ffffff", "emblem": "🦁",
                                                       "motto": "Вперёд, Аякс", "stadium_name": "Йохан Кройф Арена"},
      str(ov["profile"]))
expect_err("чужой не меняет", "FORBIDDEN", ce.update_profile, STRANGER, A, {"motto": "x"})
expect_err("владелец другого клуба не меняет", "FORBIDDEN", ce.update_profile, OWNER_B, A, {"motto": "x"})
check("админ меняет", ce.update_profile(ADMIN, A, {"motto": "Админ"})["profile"]["motto"] == "Админ")
expect_err("цвет не #RRGGBB", "BAD_COLOR", ce.update_profile, OWNER_A, A, {"color1": "red"})
expect_err("эмблема — не текст", "BAD_EMBLEM", ce.update_profile, OWNER_A, A, {"emblem": "AJAX"})
expect_err("без HTML", "BAD_TEXT", ce.update_profile, OWNER_A, A, {"stadium_name": "<b>x</b>"})
check("сброс цвета пустой строкой", ce.update_profile(OWNER_A, A, {"color2": ""})["profile"]["color2"] is None)

# ===== спонсор =====
b0 = budget(A)
ov = ce.sign_sponsor(OWNER_A, A, "balanced")
check("спонсор подписан + бонус за подпись", ov["sponsor"]["code"] == "balanced" and budget(A) == b0 + 1_000_000)
expect_err("второй спонсор за сезон — нельзя", "ALREADY", ce.sign_sponsor, OWNER_A, A, "risky")
expect_err("неизвестный спонсор", "BAD_SPONSOR", ce.sign_sponsor, OWNER_B, B, "nope")
ce.sign_sponsor(OWNER_B, B, "risky")

# ===== доход с матча =====
m = next(x for x in league.tour_matches(tid, 1) if {x["home_club_id"], x["away_club_id"]} == {A, B}) \
    if any({x["home_club_id"], x["away_club_id"]} == {A, B} for x in league.tour_matches(tid, 1)) else None
if m is None:  # пара A–B может быть в другом туре — берём её из календаря
    m = q1("SELECT * FROM matches WHERE tournament_id=? AND ((home_club_id=? AND away_club_id=?) OR "
           "(home_club_id=? AND away_club_id=?))", (tid, A, B, B, A))
home, away = m["home_club_id"], m["away_club_id"]
bh, ba = budget(home), budget(away)
results.finalize_match(m["id"], 2, 1, None, None, [], actor="manual")
S = ce.SPONSORS
spon = {A: S["balanced"], B: S["risky"]}
exp_home = ce.stadium_income(1, "W") + spon[home]["match"] + spon[home]["win"]
exp_away = spon[away]["match"]
check("хозяева: стадион (победа) + спонсор", budget(home) - bh == exp_home, f"{budget(home) - bh} vs {exp_home}")
check("гости: спонсор за поражение (без стадиона)", budget(away) - ba == exp_away, f"{budget(away) - ba} vs {exp_away}")
results.finalize_match(m["id"], 2, 1, None, None, [], actor="manual")
check("повторная финализация — без задвоения", budget(home) - bh == exp_home)
results.finalize_match(m["id"], 0, 0, None, None, [], actor="manual")
exp_home0 = ce.stadium_income(1, "D") + spon[home]["match"] + spon[home]["draw"]
exp_away0 = spon[away]["match"] + spon[away]["draw"]
check("правка счёта пересчитывает доходы", budget(home) - bh == exp_home0 and budget(away) - ba == exp_away0,
      f"{budget(home) - bh}/{budget(away) - ba}")
check("журнал доходов: по записи на клуб и вид",
      q1("SELECT COUNT(*) n FROM club_ledger WHERE match_id=?", (m["id"],))["n"] == 3)
appsettings.set_setting("club_income_enabled", "0")
results.finalize_match(m["id"], 0, 0, None, None, [], actor="manual")
check("доходы выключаются настройкой (и откатываются)", budget(home) == bh and budget(away) == ba)
appsettings.set_setting("club_income_enabled", "1")
results.finalize_match(m["id"], 0, 0, None, None, [], actor="manual")

# ===== стадион =====
c = db.db()
c.execute("UPDATE clubs SET budget=? WHERE id=?", (8_500_000, C))
c.commit()
c.close()
expect_err("апгрейд: только владелец/админ", "FORBIDDEN", ce.upgrade_stadium, STRANGER, C)
ov = ce.upgrade_stadium(ADMIN, C)
check("апгрейд до ур. 2 за 8 млн", ov["stadium"]["level"] == 2 and budget(C) == 500_000)
expect_err("апгрейд без денег", "NO_FUNDS", ce.upgrade_stadium, ADMIN, C)
check("доход растёт с уровнем", ce.stadium_income(2, "D") > ce.stadium_income(1, "D"))
check("журнал: апгрейд с минусом",
      q1("SELECT amount FROM club_ledger WHERE club_id=? AND kind='stadium_upgrade'", (C,))["amount"] == -8_000_000)

# ===== время матча =====
pend = q1("SELECT * FROM matches WHERE tournament_id=? AND status='pending' AND (home_club_id=? OR away_club_id=?) "
          "ORDER BY id LIMIT 1", (tid, A, A))
when = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:00.000Z")
r = ce.set_match_time(OWNER_A, pend["id"], when)
check("участник ставит время (ISO из браузера)", r["scheduled_at"] == when[:16].replace("T", " ") + ":00", str(r))
expect_err("чужой не ставит", "FORBIDDEN", ce.set_match_time, STRANGER, pend["id"], when)
expect_err("кривая дата", "BAD_TIME", ce.set_match_time, OWNER_A, pend["id"], "завтра")
expect_err("не дальше 60 дней", "BAD_TIME", ce.set_match_time, OWNER_A, pend["id"],
           (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=90)).strftime("%Y-%m-%d %H:%M"))
expect_err("сыгранный матч — нельзя", "PLAYED", ce.set_match_time, OWNER_A, m["id"], when)
pend_b = q1("SELECT * FROM matches WHERE tournament_id=? AND status='pending' AND (home_club_id=? OR away_club_id=?) "
            "ORDER BY id LIMIT 1", (tid, B, B))
ce.set_match_time(ADMIN, pend_b["id"], when)
n = q1("SELECT COUNT(*) n FROM notifications n JOIN users u ON u.id=n.user_id WHERE u.telegram_id=? "
       "AND n.text LIKE ?", (OWNER_B, "%Время матча%"))["n"]
check("время поставил админ → владельцу клуба уведомление", n == 1, str(n))
n_self = q1("SELECT COUNT(*) n FROM notifications n JOIN users u ON u.id=n.user_id WHERE u.telegram_id=? "
            "AND n.text LIKE ?", (OWNER_A, "%Время матча%"))["n"]
check("себе уведомление не шлём", n_self == 0)
check("снять время", ce.set_match_time(OWNER_A, pend["id"], "")["scheduled_at"] is None)
ce.set_match_time(OWNER_A, pend["id"], when)


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

    async def call(method, path, tg, body=None):
        r = await client.request(method, path, headers={"X-Telegram-Init-Data": sign(tg)},
                                 data=json.dumps(body) if body is not None else None)
        return r.status, await r.json()

    try:
        st, ov = await call("GET", f"/api/clubs/{A}/economy", OWNER_A)
        check("API экономика клуба", st == 200 and ov["can_manage"] and ov["sponsor"]["code"] == "balanced"
              and ov["ledger"] and len(ov["sponsors"]) == 3, str(st))
        st, ov2 = await call("GET", f"/api/clubs/{A}/economy", STRANGER)
        check("API: чужой видит, но не управляет", st == 200 and not ov2["can_manage"])
        st, _ = await call("POST", f"/api/clubs/{A}/profile", STRANGER, {"motto": "x"})
        check("API оформление: чужому 403", st == 403)
        st, pr = await call("POST", f"/api/clubs/{A}/profile", OWNER_A, {"color2": "#00ff00"})
        check("API оформление", st == 200 and pr["profile"]["color2"] == "#00ff00")
        st, _ = await call("GET", "/api/clubs/99999/economy", OWNER_A)
        check("API: нет клуба → 404", st == 404)
        st, sp = await call("POST", f"/api/clubs/{D}/sponsor", ADMIN, {"code": "reliable"})
        check("API спонсор (админ за клуб без владельца)", st == 200 and sp["sponsor"]["code"] == "reliable")
        st, up = await call("POST", f"/api/clubs/{D}/stadium/upgrade", ADMIN)
        check("API апгрейд стадиона", st == 200 and up["stadium"]["level"] == 2)
        st, sc = await call("GET", f"/api/matches/{pend['id']}/schedule", OWNER_A)
        check("API время: можно редактировать", st == 200 and sc["can_edit"] and sc["scheduled_at"])
        st, sc2 = await call("GET", f"/api/matches/{pend['id']}/schedule", STRANGER)
        check("API время: чужому только чтение", st == 200 and not sc2["can_edit"])
        st, _ = await call("POST", f"/api/matches/{pend['id']}/schedule", STRANGER, {"scheduled_at": when})
        check("API время: чужому 403", st == 403)
        st, line = await call("GET", "/api/line", STRANGER)
        lm = next((x for x in line["matches"] if x["id"] == pend["id"]), None)
        a_side = (lm or {}).get("home") if pend["home_club_id"] == A else (lm or {}).get("away")
        check("линия: время матча и цвета/эмблема клуба",
              lm and lm["scheduled_at"] and a_side["emblem"] == "🦁" and a_side["color1"] == "#ff0000", str(lm)[:300])
    finally:
        await client.close()


asyncio.run(run_api())

print()
if fails:
    print(f"GATE 22: {len(fails)} FAIL:", fails)
    sys.exit(1)
print("GATE 22: OK")
