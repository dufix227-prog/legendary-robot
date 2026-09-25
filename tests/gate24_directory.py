"""ГЕЙТ 24: справочник реальных игроков (открытый датасет Transfermarkt, CC0): импорт снимка,
фильтр неактивных, позиции → коды FC, поиск без диакритики и по-русски, API поиска/статуса,
импорт только для root. Сеть не нужна — фикстура tests/fixtures/tm_players_sample.csv."""
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
os.environ["ADMIN_IDS"] = "924001"
os.environ["DB_PATH"] = "/tmp/gate24.db"
if os.path.exists("/tmp/gate24.db"):
    os.remove("/tmp/gate24.db")

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import config  # noqa: E402
import db  # noqa: E402
import players_directory as pd  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "tm_players_sample.csv")
fails = []


def check(name, cond, detail=""):
    print(f"[{'OK ' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        fails.append(name)


db.init_db()
check("до импорта справочник пуст", pd.status()["count"] == 0 and pd.search("mbappe") == [])
r = pd.import_players(FIXTURE, "2026-07-06")
check("импорт: 7 активных, Клозе (2015) отброшен", r["count"] == 7 and r["snapshot"] == "2026-07-06", str(r))
m = pd.search("mbappe")
check("поиск без диакритики, дороже — выше", [p["name"] for p in m] == ["Kylian Mbappé", "Ethan Mbappé"], str(m))
k = m[0]
check("данные игрока: позиция FC, клуб, лига, нога, рост",
      k["position"] == "ST" and k["real_club"] == "Real Madrid" and k["league"] == "LaLiga"
      and k["foot"] == "R" and k["height_cm"] == 180 and k["nation"] == "France", str(k))
check("Ø ищется как o", pd.search("odegaard")[0]["name"] == "Martin Ødegaard")
check("по-русски (транслит)", pd.search("Ямал")[0]["name"] == "Lamine Yamal")
check("несколько слов", [p["name"] for p in pd.search("de bruyne")] == ["Kevin De Bruyne"])
check("позиция CAM из Attacking Midfield", pd.search("bruyne")[0]["position"] == "CAM")
check("короткий запрос — пусто", pd.search("m") == [])
check("SQL-символы безопасны", pd.search("%' OR 1=1 --") == [])
r2 = pd.import_players(FIXTURE, "2026-08-01")
check("повторный импорт заменяет снимок", r2["count"] == 7 and pd.status()["snapshot"] == "2026-08-01"
      and len(pd.search("haaland")) == 1)
try:
    pd.import_players(os.path.join(ROOT, "tests", "cp_fixture.json"))
    check("чужой файл отклонён", False)
except pd.DirectoryError:
    check("чужой файл отклонён", True)


def sign(user_id: int) -> str:
    pairs = {"auth_date": str(int(time.time())), "user": json.dumps({"id": user_id, "first_name": "T"})}
    dcs = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


async def run_api():
    from miniapp.server import build_app
    pd.DATASET_URL = FIXTURE  # импорт из админки без сети
    client = TestClient(TestServer(build_app()))
    await client.start_server()

    async def call(method, path, tg, body=None):
        r = await client.request(method, path, headers={"X-Telegram-Init-Data": sign(tg)},
                                 data=json.dumps(body) if body is not None else None)
        return r.status, await r.json()

    try:
        st, d = await call("GET", "/api/directory/search?q=salah", 924100)
        check("API поиск", st == 200 and d["players"][0]["name"] == "Mohamed Salah")
        st, d = await call("GET", "/api/directory/status", 924100)
        check("API статус", st == 200 and d["count"] == 7 and d["snapshot"] == "2026-08-01")
        st, _ = await call("POST", "/api/admin/directory/import", 924100, {})
        check("импорт из аппа: не root → 403", st == 403)
        st, d = await call("POST", "/api/admin/directory/import", 924001, {"snapshot": "2026-09-01"})
        check("импорт из аппа: root", st == 200 and d["count"] == 7 and d["snapshot"] == "2026-09-01", str(d))
    finally:
        await client.close()


asyncio.run(run_api())

print()
if fails:
    print(f"GATE 24: {len(fails)} FAIL:", fails)
    sys.exit(1)
print("GATE 24: OK")
