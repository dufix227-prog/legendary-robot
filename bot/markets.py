"""Кэфы из Elo + публичная маржа 5% (план 09).

Модель: разница Elo → ожидаемые голы команд (симметрично), матрица Пуассона
0..8 голов → вероятности всех рынков (1х2, двойной шанс, тоталы 1.5/2.5/3.5, БТТС,
ИТБ, AH, точный счёт). Кэф = fair/(1+маржа). Исход ноги и вероятность считаются по
одной таблице _WINS — расчёт ставки не может разойтись с моделью.
Пишется в markets + снапшот в odds_history (публичная история движения).
"""
import math

import db as appdb
import elo as elo_engine
import settings as appsettings

MAX_GOALS = 8          # глубина пуассоновской матрицы
BASE_LAMBDA = 1.35     # базовые ожидаемые голы команды при равных Elo


def lambdas(elo_home: float, elo_away: float) -> tuple[float, float]:
    diff = (elo_home - elo_away) / 400.0
    lh = BASE_LAMBDA * (10 ** (diff * 0.5))
    la = BASE_LAMBDA * (10 ** (-diff * 0.5))
    return max(0.2, min(lh, 4.5)), max(0.2, min(la, 4.5))


def _pois(k: int, lam: float) -> float:
    return math.exp(-lam) * lam ** k / math.factorial(k)


def probabilities(elo_home: float, elo_away: float) -> dict[str, float]:
    """Вероятности исходов из пуассоновской матрицы."""
    lh, la = lambdas(elo_home, elo_away)
    ph = [_pois(k, lh) for k in range(MAX_GOALS + 1)]
    pa = [_pois(k, la) for k in range(MAX_GOALS + 1)]
    p1 = px = p2 = p_tb25 = p_btts = p_itb_h15 = p_itb_a15 = p_ah_h15 = 0.0
    for i in range(MAX_GOALS + 1):
        for j in range(MAX_GOALS + 1):
            w = ph[i] * pa[j]
            if i > j:
                p1 += w
                p_ah_h15 += w if i - j >= 2 else 0
            elif i == j:
                px += w
            else:
                p2 += w
            if i + j > 2.5:
                p_tb25 += w
            if i > 0 and j > 0:
                p_btts += w
            if i > 1.5:
                p_itb_h15 += w
            if j > 1.5:
                p_itb_a15 += w
    return {
        "p1": p1, "px": px, "p2": p2,
        "tb25": p_tb25, "tm25": 1 - p_tb25,
        "btts_yes": p_btts, "btts_no": 1 - p_btts,
        "itb_h15": p_itb_h15, "itb_a15": p_itb_a15,
        "ah_h15": p_ah_h15, "ah_a15": 1 - p_ah_h15,  # AH +1.5 гостей = не проиграть в 2+ (дополнение к ah_h15)
    }


MARKET_LABELS = {
    "1x2_p1": "П1", "1x2_x": "Ничья", "1x2_p2": "П2",
    "tb25": "ТБ 2.5", "tm25": "ТМ 2.5",
    "btts_yes": "БТТС да", "btts_no": "БТТС нет",
    "itb_h15": "ИТБ хоз 1.5", "itb_a15": "ИТБ гост 1.5",
    "ah_h15": "AH хоз -1.5", "ah_a15": "AH гост +1.5",
    # расширение линии: двойной шанс, другие тоталы, «забьёт», точный счёт
    "dc_1x": "1X", "dc_12": "12", "dc_x2": "X2",
    "tb15": "ТБ 1.5", "tm15": "ТМ 1.5", "tb35": "ТБ 3.5", "tm35": "ТМ 3.5",
    "itb_h05": "Хозяева забьют", "itb_a05": "Гости забьют",
}
EXACT_SCORES = [(1, 0), (2, 0), (2, 1), (3, 0), (3, 1), (3, 2), (0, 0), (1, 1), (2, 2),
                (0, 1), (0, 2), (1, 2), (0, 3), (1, 3), (2, 3)]
for _h, _a in EXACT_SCORES:
    MARKET_LABELS[f"cs_{_h}_{_a}"] = f"Счёт {_h}:{_a}"

# исход ноги по счёту — одна таблица и для расчёта ставок, и для вероятностей
_WINS = {
    "1x2_p1": lambda h, a: h > a,
    "1x2_x": lambda h, a: h == a,
    "1x2_p2": lambda h, a: a > h,
    "tb25": lambda h, a: h + a > 2.5,
    "tm25": lambda h, a: h + a < 2.5,
    "btts_yes": lambda h, a: h > 0 and a > 0,
    "btts_no": lambda h, a: h == 0 or a == 0,
    "itb_h15": lambda h, a: h > 1.5,
    "itb_a15": lambda h, a: a > 1.5,
    "ah_h15": lambda h, a: h - a >= 2,
    "ah_a15": lambda h, a: a - h >= -1,   # +1.5: не проиграть в 2+ мяча
    "dc_1x": lambda h, a: h >= a,
    "dc_12": lambda h, a: h != a,
    "dc_x2": lambda h, a: a >= h,
    "tb15": lambda h, a: h + a > 1.5,
    "tm15": lambda h, a: h + a < 1.5,
    "tb35": lambda h, a: h + a > 3.5,
    "tm35": lambda h, a: h + a < 3.5,
    "itb_h05": lambda h, a: h > 0,
    "itb_a05": lambda h, a: a > 0,
}
for _h, _a in EXACT_SCORES:
    _WINS[f"cs_{_h}_{_a}"] = (lambda x, y: lambda h, a: h == x and a == y)(_h, _a)

ODDS_FLOOR = 1.01


def score_matrix(lh: float, la: float) -> list[list[float]]:
    """P(хозяева i, гости j), i,j = 0..MAX_GOALS, нормированная (хвост за 8 голов раскидан пропорционально)."""
    ph = [_pois(k, lh) for k in range(MAX_GOALS + 1)]
    pa = [_pois(k, la) for k in range(MAX_GOALS + 1)]
    mx = [[ph[i] * pa[j] for j in range(MAX_GOALS + 1)] for i in range(MAX_GOALS + 1)]
    total = sum(map(sum, mx)) or 1.0
    return [[v / total for v in row] for row in mx]


def market_probs_from_lambdas(lh: float, la: float) -> dict[str, float]:
    mx = score_matrix(lh, la)
    out = {}
    for code, won in _WINS.items():
        out[code] = sum(mx[i][j] for i in range(MAX_GOALS + 1) for j in range(MAX_GOALS + 1) if won(i, j))
    return out


def market_probs(elo_home: float, elo_away: float) -> dict[str, float]:
    """Код рынка → честная вероятность (без маржи) по Elo."""
    return market_probs_from_lambdas(*lambdas(elo_home, elo_away))


def price(p: float, margin: float) -> float:
    p = max(0.01, min(0.97, p))
    return max(ODDS_FLOOR, round(1.0 / p / (1 + margin), 2))


def compute_odds(elo_home: float, elo_away: float) -> dict[str, float]:
    """Код рынка → кэф с маржой (маржа публичная, план 09)."""
    probs = market_probs(elo_home, elo_away)
    margin = appsettings.setting_float("odds_margin_pct", 5) / 100.0
    return {code: price(probs[code], margin) for code in MARKET_LABELS}


def explain(elo_home: float, elo_away: float) -> dict:
    """«Как получен кэф»: все промежуточные числа формулы для экрана матча."""
    lh, la = lambdas(elo_home, elo_away)
    margin = appsettings.setting_float("odds_margin_pct", 5) / 100.0
    probs = market_probs_from_lambdas(lh, la)
    return {
        "elo_home": round(elo_home), "elo_away": round(elo_away),
        "elo_diff": round(elo_home - elo_away),
        "base_lambda": BASE_LAMBDA,
        "lambda_home": round(lh, 3), "lambda_away": round(la, 3),
        "margin_pct": round(margin * 100, 2),
        "markets": {code: {"prob": round(probs[code], 4),
                           "fair_odds": round(1 / max(probs[code], 0.01), 2),
                           "odds": price(probs[code], margin)} for code in MARKET_LABELS},
    }


def generate_markets(match_id: int) -> int:
    """Сгенерить/обновить markets для матча (по Elo клубов). → число рынков."""
    c = appdb.db()
    m = c.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
    if not m or m["home_club_id"] is None or m["away_club_id"] is None:
        c.close()
        return 0
    elo_h = c.execute("SELECT elo FROM clubs WHERE id=?", (m["home_club_id"],)).fetchone()
    elo_a = c.execute("SELECT elo FROM clubs WHERE id=?", (m["away_club_id"],)).fetchone()
    c.close()
    odds = compute_odds(elo_h["elo"] if elo_h else 1000, elo_a["elo"] if elo_a else 1000)
    c = appdb.db()
    n = 0
    for code, odd in odds.items():
        row = c.execute(
            "SELECT id, odds FROM markets WHERE match_id=? AND code=?", (match_id, code)
        ).fetchone()
        if row:
            if abs((row["odds"] or 0) - odd) >= 0.01:
                c.execute("UPDATE markets SET odds=?, updated_at=datetime('now') WHERE id=?", (odd, row["id"]))
                c.execute(
                    "INSERT INTO odds_history (match_id, market_code, odds) VALUES (?,?,?)",
                    (match_id, code, odd),
                )
        else:
            c.execute(
                "INSERT INTO markets (match_id, code, label, odds) VALUES (?,?,?,?)",
                (match_id, code, MARKET_LABELS[code], odd),
            )
            c.execute(
                "INSERT INTO odds_history (match_id, market_code, odds) VALUES (?,?,?)",
                (match_id, code, odd),
            )
        n += 1
    c.commit()
    c.close()
    return n


def refresh_tour(tournament_id: int, tour_number: int | None = None) -> int:
    """Обновить кэфы тура (после Elo-сдвигов / при открытии тура)."""
    c = appdb.db()
    if tour_number is None:
        rows = [dict(r) for r in c.execute(
            "SELECT id FROM matches WHERE tournament_id=? AND status='pending'", (tournament_id,)
        ).fetchall()]
    else:
        rows = [dict(r) for r in c.execute(
            "SELECT id FROM matches WHERE tournament_id=? AND tour_number=? AND status='pending'",
            (tournament_id, tour_number),
        ).fetchall()]
    c.close()
    return sum(generate_markets(r["id"]) for r in rows)


def generate_tie_markets(match_id: int, tie_id: int) -> int:
    """Рынок «исход противостояния» (2:0/2:1/1:2/0:2 — серии до 2 побед, план 09).
    Вешается на первую игру серии."""
    c = appdb.db()
    tie = c.execute("SELECT * FROM ties WHERE id=?", (tie_id,)).fetchone()
    if not tie:
        c.close()
        return 0
    elo_a = c.execute("SELECT elo FROM clubs WHERE id=?", (tie["club_a_id"],)).fetchone()
    elo_b = c.execute("SELECT elo FROM clubs WHERE id=?", (tie["club_b_id"],)).fetchone()
    c.close()
    pw = max(0.05, min(0.95, elo_engine.expected_score(elo_a["elo"] if elo_a else 1000,
                                                       elo_b["elo"] if elo_b else 1000)))
    qw = 1 - pw
    probs = {
        "tie_2_0": pw * pw,
        "tie_2_1": 2 * pw * pw * qw,
        "tie_1_2": 2 * pw * qw * qw,
        "tie_0_2": qw * qw,
    }
    margin = appsettings.setting_float("odds_margin_pct", 5) / 100.0
    c = appdb.db()
    n = 0
    for code, p in probs.items():
        odd = round(1.0 / max(p, 0.01) / (1 + margin), 2)
        row = c.execute("SELECT id FROM markets WHERE match_id=? AND code=?", (match_id, code)).fetchone()
        if not row:
            c.execute("INSERT INTO markets (match_id, code, label, odds) VALUES (?,?,?,?)",
                      (match_id, code, f"Серия {code[4:].replace('_', ':')}", odd))
            c.execute("INSERT INTO odds_history (match_id, market_code, odds) VALUES (?,?,?)",
                      (match_id, code, odd))
            n += 1
    c.commit()
    c.close()
    return n


def resolve_leg(match_id: int, market_code: str, score1: int, score2: int) -> str:
    """Исход ноги: won/lost; неизвестный рынок → void."""
    won = _WINS.get(market_code)
    if won is None:
        return "void"
    return "won" if won(score1, score2) else "lost"
