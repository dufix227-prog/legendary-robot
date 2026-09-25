"""Функции оригинала: статистика игроков матча (бомбардиры/ассистенты), скрин результата,
награды капперам за сезон, предупреждения о рисках."""
from db_backend import column_exists, table_exists

SCHEMA = """
-- голы и передачи игроков в матче (таблица статистики со скрина или /manual goals=)
CREATE TABLE IF NOT EXISTS match_player_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id INTEGER NOT NULL,
    side TEXT NOT NULL,                          -- home/away
    club_id INTEGER,
    name TEXT NOT NULL,
    goals INTEGER DEFAULT 0,
    assists INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_match_player_stats_match ON match_player_stats(match_id);
-- награды капперам по итогам сезона (дивизион): дым + значок, выдаются один раз
CREATE TABLE IF NOT EXISTS bettor_awards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id INTEGER NOT NULL,
    division_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    place INTEGER, award TEXT, coins INTEGER DEFAULT 0,
    profit INTEGER, roi REAL,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE (tournament_id, division_id, user_id)
);
-- автоматические предупреждения о рисках (линия): одно открытое на матч+вид
CREATE TABLE IF NOT EXISTS risk_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id INTEGER NOT NULL,
    kind TEXT NOT NULL,                          -- liability/one_sided/whale
    level TEXT DEFAULT 'warn',                   -- warn/high
    message TEXT,
    value REAL,
    status TEXT DEFAULT 'open',                  -- open/ack
    acked_by INTEGER, acked_at TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_risk_alerts_status ON risk_alerts(status, match_id);
"""


def migrate(c) -> None:
    if table_exists(c, "matches") and not column_exists(c, "matches", "screenshot_path"):
        c.execute("ALTER TABLE matches ADD COLUMN screenshot_path TEXT")
