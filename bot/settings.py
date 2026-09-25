"""bot_settings — всё регулируемое хранится здесь, правится из админки (решение 09).

get_setting ищет в bot_settings; если пусто — берёт env-дефолт из config.
"""
import json

import config
import db as appdb

# env-дефолты по ключам bot_settings (админка может переопределить любой)
DEFAULTS: dict[str, str] = {
    "min_bet": str(config.MIN_BET),
    "max_bet": str(config.MAX_BET),
    "max_payout": str(config.MAX_PAYOUT),
    "max_open_bets": str(config.MAX_OPEN_BETS),
    "max_open_exposure": str(config.MAX_OPEN_EXPOSURE),
    "max_legs": str(config.MAX_LEGS),
    "odds_margin_pct": str(config.ODDS_MARGIN_PCT),
    "goals_base_lambda": "1.5",          # голов на команду до первых матчей (1.5 ≈ 3.0 за матч, как у оригинала)
    "transfer_deal_threshold": str(config.TRANSFER_DEAL_THRESHOLD),
    "transfer_commission_pct": str(config.TRANSFER_COMMISSION_PCT),
    "free_agent_k": str(config.FREE_AGENT_K),
    "dispute_window_hours": str(config.DISPUTE_WINDOW_HOURS),
    "unplayed_fine": str(config.UNPLAYED_FINE),
    "default_tour_days": str(config.DEFAULT_TOUR_DAYS),
    "prize_champion": str(config.SEASON_PRIZE_CHAMPION),
    "prize_second": "30000000",          # per план 09: разумные дефолты, правится из админки
    "prize_third": "15000000",
    "prize_cup_winner": "50000000",
    "xp_per_win": str(config.XP_PER_WIN),
    "level_xp_step": str(config.LEVEL_XP_STEP),
    "streak_bonus_base": str(config.STREAK_BONUS_BASE),
    "streak_bonus_step": str(config.STREAK_BONUS_STEP),
    "streak_bonus_cap": str(config.STREAK_BONUS_CAP),
    "bets_paused": "0",                  # 1 = глобальная пауза приёма ставок
    "bets_paused_reason": "",
    "notify_bets_dm": "1",               # 1 = расчёт купона дублируется в ЛС бота
    "resettle_allow_negative": "0",      # 1 = пересчёт купона может увести баланс в минус; 0 = списываем до нуля
    "auction_min_step_pct": "5",         # аукцион: минимальный шаг ставки, %
    "auction_hours": "24",               # аукцион: длительность лота по умолчанию, ч
    "auction_snipe_minutes": "10",       # анти-снайпинг: ставка в последние N мин продлевает на N мин
    "training_limit_per_week": "10",     # fair-play: тренировок FC Mobile на клуб за неделю (пн–вс)
    "squad_edit_open": "1",              # 1 = владельцы могут добавлять/удалять карточки своего состава
    "squad_max_cards": "60",             # лимит карточек в составе клуба
    "cards_require_approval": "1",       # 1 = карточка владельца ждёт судью; 0 = сразу одобрена
    "cards_auto_approve_renderz": "1",   # 1 = полное совпадение с RenderZ одобряет без судьи
    "renderz_verify": "1",               # 0 = проверка по ссылке RenderZ выключена
    "cashout_enabled": "1",              # 1 = кэшаут открытых купонов, пока тур открыт
    "cashout_margin_pct": "8",           # удержание с честной стоимости купона при кэшауте, %
    "saved_coupons_max": "10",           # черновиков купонов на игрока
    "value_edge_pp": "5",                # value: модель по голам выше вероятности кэфа на N п.п.
    "value_min_games": "3",              # value: минимум сыгранных матчей у обоих клубов
    "club_income_enabled": "1",          # 1 = доход клубов со стадиона и от спонсоров
    "stadium_income_base": "1000000",    # доход с домашнего матча на стадионе 1 уровня (ничья)
    "sponsor_scale_pct": "100",          # множитель выплат спонсоров, %
}


def get_setting(key: str, default: str | None = None) -> str | None:
    c = appdb.db()
    row = c.execute("SELECT value FROM bot_settings WHERE key=?", (key,)).fetchone()
    c.close()
    if row and row["value"] is not None:
        return row["value"]
    if key in DEFAULTS:
        return DEFAULTS[key]
    return default


def set_setting(key: str, value) -> None:
    c = appdb.db()
    c.execute(
        "INSERT INTO bot_settings (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    c.commit()
    c.close()


def setting_int(key: str, default: int = 0) -> int:
    try:
        return int(get_setting(key, str(default)))
    except (TypeError, ValueError):
        return default


def setting_float(key: str, default: float = 0.0) -> float:
    try:
        return float(get_setting(key, str(default)))
    except (TypeError, ValueError):
        return default


def bets_paused() -> tuple[bool, str]:
    return setting_int("bets_paused") == 1, get_setting("bets_paused_reason") or ""


def dump_json(value) -> str:
    return json.dumps(value, ensure_ascii=False)
