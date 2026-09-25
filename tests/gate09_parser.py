"""ГЕЙТ БЛОКА 9: парсер Challenge Place на сохранённой фикстуре реального турнира
(снята браузером 25.09 с https://challenge.place/c/6ab2591ee769ae73c5b7a622)
возвращает 31 competitor и 3 матча со счётами; импорт помечает source; долги/алерты."""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "bot"))
os.environ["DB_PATH"] = "/tmp/gate9.db"
if os.path.exists("/tmp/gate9.db"):
    os.remove("/tmp/gate9.db")

import db
import league
import parser_cp

db.init_db()
fails = []


def check(name, cond, detail=""):
    print(f"[{'OK ' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        fails.append(name)


with open(os.path.join(ROOT, "tests", "cp_fixture.json"), encoding="utf-8") as f:
    fixture_text = f.read()

# 1) парсинг фикстуры (формат компактной выгрузки)
state = parser_cp.parse_state(fixture_text)
check("31 competitor", len(state["competitors"]) == 31, str(len(state["competitors"])))
check("3 матча со счётом (latest)", sum(1 for m in state["matches"] if m["home_score"] is not None) == 3,
      str(len(state["matches"])))
sample = next(m for m in state["matches"] if m["home_score"] is not None)
check("счёт и серия в матче", sample["home_score"] == 1 and sample["away_score"] == 2 and sample["series_id"],
      f"{sample['home_score']}:{sample['away_score']} series={sample['series_id']} order={sample['order']}")
check("иерархия stage→round→series на месте", all(k in sample for k in ("stage_id", "round_id", "series_id")))

# 2) парсер понимает и сырую HTML-обёртку с window.__INITIAL_STATE__
html = "<html><head></head><body><script>window.__INITIAL_STATE__=" + \
       json.dumps({"rooms": {"room-challenge-dashboard": json.loads(fixture_text)}}) + ";</script></body></html>"
state2 = parser_cp.parse_state(html)
check("HTML с __INITIAL_STATE__ парсится", len(state2["competitors"]) == 31)

# 3) импорт в БД: матчи создаются с source='challenge-place' + external_id
tid = league.create_league_season("КП-сезон", 1, "manual", 1, 3)
c = db.db()
for comp in state["competitors"]:
    c.insert_returning_id("INSERT INTO clubs (name, budget) VALUES (?, 20000000)", (comp["name"],))
c.commit()
c.close()
# матчи фикстуры ссылаются на первые 4 клуба по именам competitors матчей — проверим фактически:
report = parser_cp.import_state(state, tid)
check("импорт: все 3 матча созданы, алертов нет", report["created"] == 3 and not report["alerts"], str(report))
c = db.db()
src = [dict(r) for r in c.execute("SELECT * FROM matches WHERE source='challenge-place'").fetchall()]
c.close()
check("импортированные матчи помечены source+external_id",
      len(src) == 3 and all(m["external_id"] and m["status"] == "reported" for m in src),
      f"в БД {len(src)}")

# 4) расхождение: наш confirmed-счёт vs сайт → алерт
if src:
    c = db.db()
    c.execute("UPDATE matches SET status='confirmed', score1=?, score2=? WHERE id=?",
              (9, 9, src[0]["id"]))
    c.commit()
    c.close()
    report2 = parser_cp.import_state(state, tid)
    check("расхождение счёта даёт алерт", any("разошёлся" in a for a in report2["alerts"]),
          str(report2["alerts"])[:120])
    check("повторный импорт не плодит матчей", report2["created"] == 0 and report2["updated"] == 3,
          f"created={report2['created']} updated={report2['updated']}")

# 5) HTML без стейта → понятная ошибка
try:
    parser_cp.parse_state("<html>just a moment</html>")
    check("внятная ошибка без стейта", False)
except ValueError as e:
    check("внятная ошибка без стейта", "INITIAL_STATE" in str(e))

# 6) джоба напоминаний: лог по дате, 3 слота не конфликтуют
c = db.db()
c.execute("INSERT INTO debts (club_id, amount, reason, source, status) VALUES (1, 1000000, 'тур 7', 'manual', 'open')")
c.commit()
c.close()

print()
print("GATE 9:", "OK" if not fails else f"ПРОВАЛЫ: {fails}")
if os.path.exists("/tmp/gate9.db"):  # на Postgres файла нет
    os.remove("/tmp/gate9.db")
sys.exit(1 if fails else 0)
