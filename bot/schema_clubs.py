"""Клубы: цвета/эмблема/стадион, доходы (стадион, спонсоры), время матча."""
from db_backend import column_exists, table_exists

SCHEMA = """
-- оборот клуба вне трансферов: доход со стадиона, спонсоры, апгрейды стадиона
CREATE TABLE IF NOT EXISTS club_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    club_id INTEGER NOT NULL,
    match_id INTEGER,                           -- NULL для подписания/апгрейда
    tournament_id INTEGER,
    kind TEXT NOT NULL,                         -- stadium/sponsor/sponsor_sign/stadium_upgrade
    amount INTEGER NOT NULL,
    note TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_club_ledger_club ON club_ledger(club_id, id);
CREATE INDEX IF NOT EXISTS idx_club_ledger_match ON club_ledger(match_id);
-- спонсор на сезон (лигу): один контракт на клуб, выбирает владелец
CREATE TABLE IF NOT EXISTS club_sponsors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    club_id INTEGER NOT NULL,
    tournament_id INTEGER NOT NULL,
    sponsor_code TEXT NOT NULL,
    signed_at TEXT DEFAULT (datetime('now')),
    UNIQUE (club_id, tournament_id)
);
"""

CLUB_COLUMNS = [
    ("color1", "TEXT"), ("color2", "TEXT"), ("emblem", "TEXT"), ("motto", "TEXT"),
    ("stadium_name", "TEXT"), ("stadium_level", "INTEGER DEFAULT 1"),
]


def migrate(c) -> None:
    if table_exists(c, "clubs"):
        for col, coltype in CLUB_COLUMNS:
            if not column_exists(c, "clubs", col):
                c.execute(f"ALTER TABLE clubs ADD COLUMN {col} {coltype}")
    if table_exists(c, "matches") and not column_exists(c, "matches", "scheduled_at"):
        # согласованное время матча (UTC); на приём ставок не влияет — окно по локу тура
        c.execute("ALTER TABLE matches ADD COLUMN scheduled_at TEXT")
