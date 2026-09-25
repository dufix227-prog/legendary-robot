"""Ставки: кэшаут (сумма в купоне), имена черновиков купонов."""
from db_backend import column_exists, table_exists

SCHEMA = ""


def migrate(c) -> None:
    for table, col, coltype in (("bets", "cashout_amount", "INTEGER"),
                                ("saved_coupons", "name", "TEXT")):
        if table_exists(c, table) and not column_exists(c, table, col):
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
