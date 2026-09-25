"""ГЕЙТ БЛОКА 5: 11 эталонов «samples/fc27-screens» прогнаны через OCR-конвейер (промпт,
нормализация, merge, дедуп), кейс 120:00+пенальти обязателен, полная финализация
с Elo/таблицей/спором на тестовой БД.

Провайдеры без ключей пропускаются каскадом; здесь каскад подменяется
MockProvider с ground truth из references.py — проверяется ВЕСЬ конвейер после
сетевого вызова (extract_json → normalize → merge → finalize).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot"))

os.environ["DB_PATH"] = "/tmp/gate5.db"
if os.path.exists("/tmp/gate5.db"):
    os.remove("/tmp/gate5.db")

import db
import league
import ocr
import results
import sys as _sys

_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from references import REFERENCE_SHOTS, REFERENCE_PACKS

db.init_db()
fails = []


def check(name, cond, detail=""):
    status = "OK " if cond else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not cond:
        fails.append(name)


# 1) 11 эталонов: normalize каждого ground truth → счёт/таймер/пенальти на месте
for fname, gt in REFERENCE_SHOTS.items():
    n = ocr.normalize_result(dict(gt))
    ok = (n["score_home"], n["score_away"]) == (gt["score_home"], gt["score_away"])
    ok = ok and n["timer"] == gt["timer"]
    exp_pens = gt["penalties"]
    got_pens = (n["penalties"]["home"], n["penalties"]["away"]) if n["penalties"] else None
    exp_pens_t = (exp_pens["home"], exp_pens["away"]) if exp_pens else None
    ok = ok and got_pens == exp_pens_t
    check(f"эталон {fname}", ok, f"{n['score_home']}:{n['score_away']} {n['timer']} пен={got_pens}")

# 2) обязательный кейс 120:00 + пенальти: пенальти НЕ входят в счёт
n = ocr.normalize_result(dict(REFERENCE_SHOTS["photo_2_2026-09-24_19-49-40.jpg"]))
check("кейс 120:00+пенальти: счёт 3:3, пен 5:4 отдельно",
      (n["score_home"], n["score_away"]) == (3, 3) and n["penalties"] == {"home": 5, "away": 4})

# 3) extract_json терпит ```json-заборы и мусор
messy = "Вот результат:\n```json\n{\"score_home\": 2, \"score_away\": 3, \"timer\": \"90:00\"}\n```\nЧто-то ещё"
j = ocr.extract_json(messy)
check("extract_json с забором", bool(j) and j["score_home"] == 2)

# 4) heuristic_parse: текст OCR.space/tesseract (счёт из шапки, пенальти в скобках)
h = ocr.heuristic_parse("Real Betis (5) 3 | 3 (4) Liverpool\n120:00\nLo Celso 113\nChiesa")
check("heuristic_parse с пенальти", bool(h) and h["score_home"] == 3 and h["penalties"] == {"home": 5, "away": 4}, str(h))
h2 = ocr.heuristic_parse("quete-основа 3 - 1 KadyrFc\n90:00")
check("heuristic_parse 3 - 1", bool(h2) and (h2["score_home"], h2["score_away"]) == (3, 1))

# 5) промпт содержит критичные правила плана 06
for needle in ("120:00", "penalties", "ТОЛЬКО", "90:00", "score_home"):
    check(f"промпт упоминает {needle}", needle in ocr.SYSTEM_PROMPT)

# 6) пачки: merge + дедуп голов (MockProvider возвращает ground truth пачки)
class MockCascade:
    def __init__(self, gt_by_file):
        self.gt = gt_by_file

    def __iter__(self):
        return iter([("mock", self._call)])

    def _call(self, image_or_name):
        # в тесте передаём «имя файла» вместо байтов
        return __import__("json").dumps(self.gt[image_or_name], ensure_ascii=False)

for pack in REFERENCE_PACKS:
    gt = {f: REFERENCE_SHOTS[f] for f in pack["files"]}
    merged = ocr.merge_results([ocr.normalize_result(dict(gt[f])) for f in pack["files"]])
    exp = pack["expect"]
    got_score = (merged["score_home"], merged["score_away"])
    ok = got_score == exp["score"]
    if "goals" in exp:
        ok = ok and len(merged["goal_events"]) == exp["goals"]
    if "pens" in exp:
        ok = ok and merged["penalties"] == {"home": exp["pens"][0], "away": exp["pens"][1]}
    check(f"пачка {len(pack['files'])} скринов", ok,
          f"{got_score} голов={len(merged['goal_events'])}")

# 7) ПОЛНАЯ финализация на тестовой БД: сезон → матч → OCR → Elo/таблица/голы → спор → решение
tid = league.create_league_season("Гейт-сезон", 1, "manual", 1, 3)
divs = league.tournament_divisions(tid)
betis = league.create_club("Real Betis", None, divs[0]["id"], 20000000)
liverpool = league.create_club("Liverpool", None, divs[0]["id"], 20000000)
c = db.db()
p1 = c.insert_returning_id("INSERT INTO players (username, telegram_id) VALUES ('temiyy', 1001)")
p2 = c.insert_returning_id("INSERT INTO players (username, telegram_id) VALUES ('Rusli', 1002)")
c.commit()
c.close()
league.assign_club_owner(betis, p1)
league.assign_club_owner(liverpool, p2)
league.generate_league_calendar(tid, divs[0]["id"])
m = league.tour_matches(tid, 1)[0]
mid = m["id"]

gt = REFERENCE_SHOTS["photo_2_2026-09-24_19-49-40.jpg"]  # 3:3, пен 5:4, 6 голов
parsed = ocr.normalize_result(dict(gt))
summary = results.finalize_match(
    mid, parsed["score_home"], parsed["score_away"],
    parsed["penalties"]["home"], parsed["penalties"]["away"],
    parsed["goal_events"], actor="ocr")

c = db.db()
mm = dict(c.execute("SELECT * FROM matches WHERE id=?", (mid,)).fetchone())
check("финализация: счёт+пенальти+статус",
      (mm["score1"], mm["score2"], mm["pens1"], mm["pens2"], mm["status"]) == (3, 3, 5, 4, "confirmed"))
goals_db = c.execute("SELECT COUNT(*) n FROM match_goals WHERE match_id=?", (mid,)).fetchone()["n"]
check("финализация: 6 голов в БД", goals_db == 6, str(goals_db))
elo1 = c.execute("SELECT elo FROM tournament_elo WHERE tournament_id=? AND player_id=1001", (tid,)).fetchone()
check("tournament_elo начислено (ничья ~1000)", elo1 is not None and abs(elo1["elo"] - 1000) < 5, str(elo1))
elo_before_snap = mm["elo_before"]
c.close()

# 8) спор: судья решает иначе (3:3 → 2:3), откат снапшота + перерасчёт
summary2 = results.resolve_dispute(mid, 2, 3, None, None, parsed["goal_events"], decided_by=6163072393)
c = db.db()
mm2 = dict(c.execute("SELECT * FROM matches WHERE id=?", (mid,)).fetchone())
check("спор: новый счёт 2:3, статус confirmed", (mm2["score1"], mm2["score2"], mm2["status"]) == (2, 3, "confirmed"))
e1 = dict(c.execute("SELECT * FROM tournament_elo WHERE player_id=1001").fetchone())
check("спор: Elo откатился и пересчитался (поражение <1000)", e1["elo"] < 1000, str(e1["elo"]))
t = league.division_standings(divs[0]["id"])
rows = {r["club_id"]: r for r in t}
check("таблица после спора: у Betis 0 очк., у Liverpool 3", rows[betis]["points"] == 0 and rows[liverpool]["points"] == 3)
c.close()

# 9) штраф за неигранный матч (4 клуба → в туре 2 матча, один оставляем pending)
tid4 = league.create_league_season("Гейт-штраф", 1, "manual", 1, 3)
div4 = league.tournament_divisions(tid4)[0]
ids4 = [league.create_club(n, None, div4["id"], 20000000)
        for n in ("Порту", "Бенфика", "Ланс", "Лилль")]
league.generate_league_calendar(tid4, div4["id"])
results.finalize_match(league.tour_matches(tid4, 1)[0]["id"], 1, 0, None, None, [], actor="manual")
fined = results.fine_unplayed(tid4, 1)
check("штраф за неигранный: 2 клуба по 1М", len(fined) == 2 and all(f == 1000000 for _, f in fined), str(fined))

print()
print("GATE 5:", "OK" if not fails else f"ПРОВАЛЫ: {fails}")
if os.path.exists("/tmp/gate5.db"):  # на Postgres файла нет
    os.remove("/tmp/gate5.db")
sys.exit(1 if fails else 0)
