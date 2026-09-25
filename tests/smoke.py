"""ПОЛНЫЙ SMOKE (план 10 блок 11): схема → валидатор → HTTP-авторизация →
ставка→расчёт→void → трансфер→комиссия → парсер CP на фикстуре → OCR-конвейер.

Запуск: venv/bin/python tests/smoke.py   (поднимает свой сервер на порту 9543)
"""
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bot"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

SMOKE_PORT = 9543
SMOKE_DB = "/tmp/smoke_full.db"

if os.path.exists(SMOKE_DB):
    os.remove(SMOKE_DB)
os.environ["DB_PATH"] = SMOKE_DB
# пустой токен теперь блокирует авторизацию — тестовый ставим до импорта config
os.environ.setdefault("BOT_TOKEN", "123456:TEST")

RESULTS: list[tuple[str, bool, str]] = []


def step(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"[{'OK ' if cond else 'FAIL'}] {name} {detail}")


# ===== 1. схема: идемпотентное создание =====
import db  # noqa: E402

db.init_db()
db.init_db()
c = db.db()
if c.backend == "postgres":
    tables = [r["table_name"] for r in c.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='public'").fetchall()]
else:
    tables = [r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
c.close()
step("схема: 34+ таблиц идемпотентно", len(tables) >= 34, f"{len(tables)}")
step("схема: club_players/promo/user_streaks/ties",
     all(t in tables for t in ("club_players", "promo_codes", "user_streaks", "ties", "transfer_lots")))

# ===== 2. валидатор initData (позитив/негатив/auth_date) =====
import config  # noqa: E402
from miniapp.helpers import validate_init_data  # noqa: E402


def sign(pairs: dict) -> str:
    p = {k: v for k, v in pairs.items() if k != "hash"}
    dcs = "\n".join(f"{k}={p[k]}" for k in sorted(p))
    secret = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    return hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()


now = int(time.time())
user_json = json.dumps({"id": 555000, "first_name": "Смок"}, ensure_ascii=False)
good = {"auth_date": str(now), "query_id": "AAsmoke", "user": user_json}
good["hash"] = sign(good)
step("валидатор: свежая подпись ок", validate_init_data(urlencode(good)) is not None)
bad = {**good, "hash": "0" * 64}
step("валидатор: битая подпись → None", validate_init_data(urlencode(bad)) is None)
old = {**good, "auth_date": str(now - 25 * 3600)}
step("валидатор: auth_date 25ч → None", validate_init_data(urlencode({**old, "hash": sign(old)})) is None)
edge = {**good, "auth_date": str(now - 23 * 3600)}
step("валидатор: граница 23ч ок", validate_init_data(urlencode({**edge, "hash": sign(edge)})) is not None)

# ===== 3. HTTP: сервер на 9543, bootstrap 200/401/403 =====
server = subprocess.Popen(
    [str(ROOT / "venv" / "bin" / "python"), "-m", "miniapp.server"],
    cwd=str(ROOT), env={**os.environ, "DB_PATH": SMOKE_DB, "WEBAPP_PORT": str(SMOKE_PORT)},
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
try:
    time.sleep(2.5)

    def http(path, init_data=None, method="GET", body=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{SMOKE_PORT}{path}",
            headers={"X-Telegram-Init-Data": init_data or ""} if init_data else {},
            data=json.dumps(body).encode() if body else None,
            method=method,
        )
        if body:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode() or "{}")
            except json.JSONDecodeError:
                return e.code, {}

    st, boot = http("/api/bootstrap", urlencode(good))
    step("HTTP bootstrap: 200, юзер создан (баланс 422)",
         st == 200 and boot.get("user", {}).get("balance") == 422, str(st))
    st, _ = http("/api/bootstrap")
    step("HTTP bootstrap без initData: 401", st == 401)
    st, _ = http("/api/bootstrap", urlencode(bad))
    step("HTTP bootstrap битая подпись: 401", st == 401)

    d = urlencode(good)
    for ep in ("/api/wallet", "/api/line", "/api/standings", "/api/progression",
               "/api/cabinet/overview", "/api/favorites", "/api/notifications",
               "/api/leaderboard", "/api/tournaments"):
        st, _ = http(ep, d)
        if st != 200:
            step(f"HTTP {ep}", False, str(st))
            break
    else:
        step("HTTP: ядро эндпоинтов 200", True)

    # 403 заморозке
    raw = db.db()
    raw.execute("UPDATE users SET is_frozen=1, freeze_reason='смок-бан' WHERE telegram_id=555000")
    raw.commit()
    raw.close()
    st, body = http("/api/bootstrap", d)
    step("HTTP замороженный: 403 с причиной", st == 403 and "смок-бан" in (body.get("reason") or ""))

    # ставка по HTTP
    raw = db.db()
    raw.execute("UPDATE users SET is_frozen=0, freeze_reason=NULL WHERE telegram_id=555000")
    raw.commit()
    raw.close()
    c = db.db()
    tid = __import__("league").create_league_season("Смок-сезон", 1, "manual", 1, 3)
    div = __import__("league").tournament_divisions(tid)[0]
    for nm in ("Аякс", "Ливерпуль", "Милан", "Порту"):
        c.execute("INSERT INTO clubs (name, division_id, budget) VALUES (?,?,20000000)", (nm, div["id"]))
    c.commit()
    c.close()
    __import__("league").generate_league_calendar(tid, div["id"])
    __import__("markets").refresh_tour(tid, 1)
    c = db.db()
    mid = c.execute("SELECT id FROM matches ORDER BY id LIMIT 1").fetchone()[0]
    c.close()
    st, r = http("/api/predictions", d, "POST", {
        "amount": 50, "selections": [{"match_id": mid, "market_code": "1x2_p1"}],
        "idempotency_key": "smoke-1"})
    step("HTTP ставка принята", st == 200 and r.get("bet_id"), str(r)[:80])
    st, r2 = http("/api/predictions", d, "POST", {
        "amount": 50, "selections": [{"match_id": mid, "market_code": "1x2_p1"}],
        "idempotency_key": "smoke-1"})
    step("HTTP idempotency повтор", r2.get("duplicate") is True)
    st, r3 = http("/api/predictions", d, "POST", {
        "amount": 50, "selections": [{"match_id": mid, "market_code": "1x2_p1", "odds": 99.0}],
        "idempotency_key": "smoke-2"})
    step("HTTP ODDS_CHANGED", st == 400 and r3.get("code") == "ODDS_CHANGED", str(st))
finally:
    server.terminate()

# ===== 4. ставка → расчёт → void (движок) =====
import bets_engine  # noqa: E402
import markets as markets_engine  # noqa: E402
import results as results_engine  # noqa: E402

c = db.db()
m1, m2 = [r["id"] for r in c.execute("SELECT id FROM matches ORDER BY id LIMIT 2").fetchall()]
uu = dict(c.execute("SELECT * FROM users WHERE telegram_id=555000").fetchone())
c.commit()
c.close()
bet = bets_engine.place_bet(uu, 100, [{"match_id": m1, "market_code": "tb25"}])
results_engine.finalize_match(m1, 3, 2, None, None, [], actor="manual")  # ТБ зашёл
c = db.db()
brow = dict(c.execute("SELECT * FROM bets WHERE id=?", (bet["bet_id"],)).fetchone())
c.close()
step("ставка рассчитана автоматически при финализации", brow["status"] == "won", brow["status"])
bet2 = bets_engine.place_bet(uu, 20, [{"match_id": m2, "market_code": "1x2_x"}])
v = bets_engine.void_bet(bet2["bet_id"], "смок-void")
c = db.db()
bal = c.execute("SELECT balance FROM users WHERE telegram_id=555000").fetchone()["balance"]
c.close()
step("void вернул деньги", v["refund"] == 20 and bal > 400, str(bal))

# ===== 5. трансфер → комиссия =====
import transfers as tr  # noqa: E402

c = db.db()
cl1 = c.insert_returning_id("INSERT INTO clubs (name, budget) VALUES ('Аякс-Т', 20000000)")
cl2 = c.insert_returning_id("INSERT INTO clubs (name, budget) VALUES ('Порту-Т', 20000000)")
card_id = c.insert_returning_id("INSERT INTO club_cards (club_id, name, position, rating) VALUES (?, 'Кака', 'ЦП', 80)", (cl2,))
c.commit()
c.close()
lot = tr.create_lot(cl2, card_id, "fix", 1_000_000)
deal = tr.buy_lot(cl1, lot["lot_id"])
step("трансфер: сделка авто, комиссия 5% сгорела",
     deal["status"] == "approved" and deal["commission"] == 50_000, str(deal.get("commission")))

# ===== 6. парсер CP на сохранённой фикстуре =====
import parser_cp  # noqa: E402

fixture = (ROOT / "tests" / "cp_fixture.json").read_text(encoding="utf-8")
state = parser_cp.parse_state(fixture)
step("парсер CP: 31 competitor", len(state["competitors"]) == 31, str(len(state["competitors"])))
step("парсер CP: 3 матча со счётом",
     sum(1 for m in state["matches"] if m["home_score"] is not None) == 3)

# ===== 7. OCR-конвейер на эталонах =====
from references import REFERENCE_SHOTS, REFERENCE_PACKS  # noqa: E402

import ocr  # noqa: E402

n = ocr.normalize_result(dict(REFERENCE_SHOTS["photo_2_2026-09-24_19-49-40.jpg"]))
step("OCR: кейс 120:00+пенальти обязателен",
     (n["score_home"], n["score_away"]) == (3, 3) and n["penalties"] == {"home": 5, "away": 4})
pack = REFERENCE_PACKS[1]
merged = ocr.merge_results([ocr.normalize_result(dict(REFERENCE_SHOTS[f])) for f in pack["files"]])
step("OCR: дедуп ленты голов (5:2, 7 событий)",
     (merged["score_home"], merged["score_away"]) == (5, 2) and len(merged["goal_events"]) == 7)

# ===== итог =====
print()
failed = [name for name, ok, _ in RESULTS if not ok]
print(f"SMOKE: {len(RESULTS) - len(failed)}/{len(RESULTS)} зелёные")
if failed:
    print("ПРОВАЛЫ:", failed)
    sys.exit(1)
print("SMOKE ПОЛНОСТЬЮ ЗЕЛЁНЫЙ")
