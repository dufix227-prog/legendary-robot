"""Справочник реальных игроков — снимок открытого датасета Transfermarkt (план 08, раздел D).

Источник: github.com/dcaribou/transfermarkt-datasets, таблица players (лицензия CC0 1.0).
Сайт Transfermarkt не парсим. Снимок фиксируется при импорте (дата — в bot_settings
directory_snapshot): реальные трансферы в течение сезона ничего не меняют, пока админ сам
не обновит справочник. Фото с CDN Transfermarkt не берём.

Импорт:  venv/bin/python bot/players_directory.py [путь.csv(.gz) | URL] [ГГГГ-ММ-ДД]
или кнопкой в админке мини-аппа (root).
"""
import csv
import gzip
import io
import logging
import re
import sys
import unicodedata
import urllib.request
from datetime import date

import db as appdb
import settings as appsettings

log = logging.getLogger("bot.directory")

DATASET_URL = "https://pub-e682421888d945d684bcae8890b0ec20.r2.dev/data/players.csv.gz"
SOURCE = "transfermarkt-datasets (CC0)"
MAX_DOWNLOAD = 64 * 1024 * 1024

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS players_directory (
    tm_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL, first_name TEXT, last_name TEXT,
    search_key TEXT NOT NULL,                  -- без диакритики, нижний регистр
    position TEXT, sub_position TEXT,
    foot TEXT, height_cm INTEGER, birth_date TEXT,
    nation TEXT, real_club TEXT, league_code TEXT,
    market_value INTEGER, last_season INTEGER
);
CREATE INDEX IF NOT EXISTS idx_players_directory_key ON players_directory(search_key);
"""

POSITIONS = {  # sub_position Transfermarkt → код FC Mobile
    "Goalkeeper": "GK", "Centre-Back": "CB", "Left-Back": "LB", "Right-Back": "RB",
    "Defensive Midfield": "CDM", "Central Midfield": "CM", "Attacking Midfield": "CAM",
    "Left Midfield": "LM", "Right Midfield": "RM", "Left Winger": "LW", "Right Winger": "RW",
    "Second Striker": "CF", "Centre-Forward": "ST",
}
FALLBACK_POS = {"Goalkeeper": "GK", "Defender": "CB", "Midfield": "CM", "Attack": "ST"}
LEAGUES = {
    "GB1": "Premier League", "ES1": "LaLiga", "IT1": "Serie A", "L1": "Bundesliga", "FR1": "Ligue 1",
    "PO1": "Liga Portugal", "NL1": "Eredivisie", "TR1": "Süper Lig", "BE1": "Jupiler Pro League",
    "SC1": "Scottish Premiership", "RU1": "Премьер-лига", "UKR1": "Premier Liha", "GR1": "Super League",
    "DK1": "Superliga", "MLS1": "MLS", "BRA1": "Série A", "ARG1": "Liga Profesional", "SA1": "Saudi Pro League",
    "JAP1": "J1 League", "PL1": "Ekstraklasa", "SER1": "SuperLiga", "A1": "Bundesliga (AT)", "C1": "Super League (CH)",
}


class DirectoryError(Exception):
    pass


def ensure_schema(c) -> None:
    c.executescript(SCHEMA_SQL)


def search_key(text: str) -> str:
    s = unicodedata.normalize("NFKD", str(text or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("ø", "o").replace("Ø", "o").replace("ß", "ss").replace("ł", "l").replace("đ", "d")
    return " ".join(re.sub(r"[^\w\s-]", " ", s.casefold()).split())


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _rows_from(source: str | None):
    """CSV-строки датасета: путь к .csv/.csv.gz или URL (по умолчанию — официальная выгрузка)."""
    source = source or DATASET_URL
    if re.match(r"^https?://", source):
        if not source.startswith("https://"):
            raise DirectoryError("Только https")
        req = urllib.request.Request(source, headers={"User-Agent": "KurilkaSigarkiBot/1.0 (dataset import)"})
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read(MAX_DOWNLOAD + 1)
        if len(raw) > MAX_DOWNLOAD:
            raise DirectoryError("Файл датасета слишком большой")
    else:
        with open(source, "rb") as f:
            raw = f.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    if not reader.fieldnames or "player_id" not in reader.fieldnames or "name" not in reader.fieldnames:
        raise DirectoryError("Это не таблица players из transfermarkt-datasets")
    return list(reader)


def import_players(source: str | None = None, snapshot: str | None = None, active_seasons: int = 2) -> dict:
    """Полная замена справочника. Берём игроков, игравших в последние active_seasons сезонов."""
    rows = _rows_from(source)
    last = max((_int(r.get("last_season")) or 0) for r in rows) if rows else 0
    keep = [r for r in rows if (_int(r.get("last_season")) or 0) > last - active_seasons and r.get("name")]
    if not keep:
        raise DirectoryError("В файле нет активных игроков")
    snapshot = snapshot or date.today().isoformat()
    c = appdb.db()
    try:
        ensure_schema(c)
        c.execute("DELETE FROM players_directory")
        for r in keep:
            sub = (r.get("sub_position") or "").strip()
            c.execute(
                "INSERT INTO players_directory (tm_id, name, first_name, last_name, search_key, position, sub_position, "
                "foot, height_cm, birth_date, nation, real_club, league_code, market_value, last_season) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_int(r["player_id"]), r["name"].strip(), (r.get("first_name") or "").strip() or None,
                 (r.get("last_name") or "").strip() or None, search_key(r["name"]),
                 POSITIONS.get(sub) or FALLBACK_POS.get((r.get("position") or "").strip()), sub or None,
                 {"left": "L", "right": "R", "both": "R"}.get((r.get("foot") or "").strip().lower()),
                 _int(r.get("height_in_cm")), (r.get("date_of_birth") or "")[:10] or None,
                 (r.get("country_of_citizenship") or "").strip() or None,
                 (r.get("current_club_name") or "").strip() or None,
                 (r.get("current_club_domestic_competition_id") or "").strip() or None,
                 _int(r.get("market_value_in_eur")), _int(r.get("last_season"))))
        c.commit()
    finally:
        c.close()
    appsettings.set_setting("directory_snapshot", snapshot)
    appsettings.set_setting("directory_count", str(len(keep)))
    return {"count": len(keep), "snapshot": snapshot, "last_season": last, "source": SOURCE}


def status() -> dict:
    c = appdb.db()
    try:
        ensure_schema(c)
        n = c.execute("SELECT COUNT(*) n FROM players_directory").fetchone()["n"]
    finally:
        c.close()
    return {"count": n, "snapshot": appsettings.get_setting("directory_snapshot") if n else None,
            "source": SOURCE, "dataset_url": DATASET_URL}


def _public(r: dict) -> dict:
    lc = r.get("league_code")
    return {"tm_id": r["tm_id"], "name": r["name"], "position": r["position"], "foot": r["foot"],
            "height_cm": r["height_cm"], "birth_date": r["birth_date"], "nation": r["nation"],
            "real_club": r["real_club"], "league": LEAGUES.get(lc, lc) if lc else None,
            "market_value": r["market_value"]}


_RU = dict(zip("абвгдезийклмнопрстуфхцыэ", "abvgdeziyklmnoprstufhcye"))
_RU.update({"ё": "e", "ж": "zh", "ч": "ch", "ш": "sh", "щ": "sch", "ю": "yu", "я": "ya", "ь": "", "ъ": ""})


def translit(text: str) -> str:
    """«Мбаппе» → «mbappe»: в справочнике имена латиницей."""
    return "".join(_RU.get(ch, ch) for ch in str(text or "").casefold())


def search(query: str, limit: int = 12) -> list[dict]:
    """Все слова запроса — в имени (без диакритики). Сначала точное начало фамилии, потом по стоимости."""
    if re.search(r"[а-яё]", str(query or ""), re.I):
        query = translit(query)
    words = search_key(query).split()[:4]
    if not words or sum(len(w) for w in words) < 2:
        return []
    where = " AND ".join("search_key LIKE ?" for _ in words)
    args = [f"%{w}%" for w in words]
    c = appdb.db()
    try:
        ensure_schema(c)
        rows = [dict(r) for r in c.execute(
            f"SELECT * FROM players_directory WHERE {where} ORDER BY COALESCE(market_value,0) DESC LIMIT 200",
            args).fetchall()]
    finally:
        c.close()
    last = words[-1]
    rows.sort(key=lambda r: (not any(p.startswith(last) for p in r["search_key"].split()),
                             -(r["market_value"] or 0)))
    return [_public(r) for r in rows[:max(1, min(limit, 30))]]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    appdb.init_db()
    src = sys.argv[1] if len(sys.argv) > 1 else None
    snap = sys.argv[2] if len(sys.argv) > 2 else None
    print(import_players(src, snap))
