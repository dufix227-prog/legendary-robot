"""ГЕЙТ 23: полный тестовый сезон на реальном коде.

Лига 2×6 клубов, владельцы с никами, 2 круга (10 туров), 16 капперов с разными стратегиями
(случайные, фавориты, экспрессы, «аналитики», считающие шансы по голам сами), кэшауты, черновики, спонсоры, апгрейды
стадиона, правки счёта, финал сезона с призовыми и 3↑/3↓.
Счёт матча разыгрывается по скрытой «настоящей силе» клуба (Elo её не знает на старте).

Проверяются инварианты денег, в конце печатается отчёт по экономике — по нему подстраивать
настройки в админке. SEASON_SEED — другой сезон, SEASON_QUIET=1 — без отчёта.
"""
import math
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "bot"))
os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ["ADMIN_IDS"] = "923001"
os.environ["DB_PATH"] = "/tmp/gate23.db"
if os.path.exists("/tmp/gate23.db"):
    os.remove("/tmp/gate23.db")

import bet_tools  # noqa: E402
import bets_engine  # noqa: E402
import club_economy as ce  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import league  # noqa: E402
import league_admin  # noqa: E402
import markets  # noqa: E402
import match_insights  # noqa: E402
import results  # noqa: E402
from bets_engine import BetError  # noqa: E402

rng = random.Random(int(os.environ.get("SEASON_SEED", "2026")))
QUIET = os.environ.get("SEASON_QUIET") == "1"
fails = []


def check(name, cond, detail=""):
    print(f"[{'OK ' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        fails.append(name)


def q(sql, args=()):
    c = db.db()
    rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    c.close()
    return rows


def q1(sql, args=()):
    r = q(sql, args)
    return r[0] if r else None


def poisson(lam):
    # Кнут: для λ ≤ 4.5 хватает
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= L:
            return k
        k += 1


def true_lambdas(sh, sa):
    """«Настоящие» ожидаемые голы по скрытой силе: не зависит от модели кэфов (FIFA ≈ 3.2 гола)."""
    d = (sh - sa) / 800
    return max(0.2, 1.6 * 10 ** d), max(0.2, 1.6 * 10 ** -d)


db.init_db()

# ===== лига, клубы, владельцы, ники =====
tid = league_admin.create_league("Тестовый сезон", ["Высший", "Первый"], rounds=2, promote_count=2)
divs = league.tournament_divisions(tid)
NAMES = [["Аякс", "Милан", "Порту", "Бенфика", "Ливерпуль", "Марсель"],
         ["Лидс", "Ренн", "Ницца", "Вест Хэм", "Лилль", "Севилья"]]
strength, clubs, owners = {}, [], {}
tg = 923100
for d, names in zip(divs, NAMES):
    for n in names:
        info = league_admin.add_club(d["id"], n, allow_custom=True)
        cid = info["id"] if "id" in info else info["club"]["id"]
        tg += 1
        league_admin.set_nick(tg, f"nick{tg}")
        league_admin.assign_owner(cid, tg)
        owners[cid] = tg
        clubs.append(cid)
        strength[cid] = rng.gauss(1000 if d is divs[0] else 930, 110)
check("12 клубов с владельцами и никами",
      q1("SELECT COUNT(*) n FROM players WHERE game_nickname IS NOT NULL")["n"] == 12
      and q1("SELECT COUNT(*) n FROM club_players")["n"] == 12)

cal = league_admin.generate_calendar(tid)
total_tours = q1("SELECT total_tours FROM tournaments WHERE id=?", (tid,))["total_tours"]
check("календарь: 2 круга по 5 туров", total_tours == 10 and sum(cal["matches"].values()) == 60, str(cal))
if not league.ensure_tour(tid, 1).get("status") == "open":
    league.open_tour(tid, 1)

# спонсоры: каждый владелец выбирает; стадион расширяют двое
codes = list(ce.SPONSORS)
for i, cid in enumerate(clubs):
    ce.sign_sponsor(owners[cid], cid, codes[i % 3])
for cid in clubs[:2]:
    ce.upgrade_stadium(owners[cid], cid)
budget0 = {cid: q1("SELECT budget FROM clubs WHERE id=?", (cid,))["budget"] for cid in clubs}

# ===== капперы =====
STRATS = ["random"] * 4 + ["favorite"] * 4 + ["express"] * 4 + ["analyst"] * 4
bettors = []
c = db.db()
for i, s in enumerate(STRATS):
    t = 923500 + i
    c.execute("INSERT INTO users (telegram_id, username, balance) VALUES (?,?,?)",
              (t, f"{s}{i}", config.START_BALANCE))
    bettors.append({"tg": t, "strat": s})
c.commit()
c.close()
START = config.START_BALANCE


def urow(t):
    return q1("SELECT * FROM users WHERE telegram_id=?", (t,))


def open_matches():
    return q("SELECT m.* FROM matches m JOIN tours t ON t.tournament_id=m.tournament_id "
             "AND t.tour_number=m.tour_number WHERE m.tournament_id=? AND t.status='open' AND m.status='pending' "
             "ORDER BY m.id", (tid,))


def mkts(mid):
    return {r["code"]: r["odds"] for r in q("SELECT code, odds FROM markets WHERE match_id=? AND is_active=1", (mid,))}


errors = {}
placed = cashed = drafts = 0
value_legs = []   # (bet_id, match_id, code) — ставки value-охотников


def place(b, legs, amount):
    global placed
    u = urow(b["tg"])
    amount = max(10, min(int(amount), u["balance"]))
    if u["balance"] < 10:
        return None
    try:
        r = bets_engine.place_bet(u, amount, legs)
        placed += 1
        return r["bet_id"]
    except BetError as e:
        errors[e.code] = errors.get(e.code, 0) + 1
        return None


def goals_model(m):
    """Модель «аналитика»: средние голы клубов в турнире (атака + оборона соперника), от 3 матчей."""
    st = {}
    for cid in (m["home_club_id"], m["away_club_id"]):
        r = q1("SELECT COUNT(*) n, COALESCE(SUM(CASE WHEN home_club_id=? THEN score1 ELSE score2 END),0) gf, "
               "COALESCE(SUM(CASE WHEN home_club_id=? THEN score2 ELSE score1 END),0) ga FROM matches "
               "WHERE tournament_id=? AND status='confirmed' AND (home_club_id=? OR away_club_id=?)",
               (cid, cid, tid, cid, cid))
        if r["n"] < 3:
            return None
        st[cid] = (r["gf"] / r["n"], r["ga"] / r["n"])
    h, a = st[m["home_club_id"]], st[m["away_club_id"]]
    return markets.market_probs_from_lambdas(max(0.2, (h[0] + a[1]) / 2), max(0.2, (a[0] + h[1]) / 2))


def bet_round():
    global drafts
    ms = open_matches()
    for b in bettors:
        u = urow(b["tg"])
        if u["balance"] < 10:
            continue
        stake = u["balance"] * rng.uniform(0.03, 0.1)
        s = b["strat"]
        if s == "random":
            for m in rng.sample(ms, min(2, len(ms))):
                code = rng.choice(list(mkts(m["id"])))
                place(b, [{"match_id": m["id"], "market_code": code}], stake)
        elif s == "favorite":
            for m in rng.sample(ms, min(2, len(ms))):
                k = mkts(m["id"])
                code = min(("1x2_p1", "1x2_p2"), key=lambda x: k[x])
                place(b, [{"match_id": m["id"], "market_code": code}], stake)
        elif s == "express":
            pick = rng.sample(ms, min(3, len(ms)))
            legs = [{"match_id": m["id"], "market_code": rng.choice(["1x2_p1", "dc_1x", "tb15", "btts_yes", "dc_x2"])}
                    for m in pick]
            place(b, legs, stake / 2)
            if rng.random() < 0.3:
                try:
                    bet_tools.save_draft(u, legs, int(stake), "авто")
                    drafts += 1
                except BetError:
                    pass
        elif s == "analyst":
            # сам считает шансы по голам клубов из статистики (без Elo) и берёт перевес 5+ п.п.
            picks = []
            for m in ms:
                probs = goals_model(m)
                if not probs:
                    continue
                for code, odd in mkts(m["id"]).items():
                    edge = probs.get(code, 0) - 1 / odd
                    if edge >= 0.05:
                        picks.append((edge, m["id"], code))
            for _, mid, code in sorted(picks, reverse=True)[:3]:
                bid = place(b, [{"match_id": mid, "market_code": code}], stake)
                if bid:
                    value_legs.append((bid, mid, code))


def cashout_round():
    global cashed
    for b in bettors:
        u = urow(b["tg"])
        for bid, qt in bet_tools.cashout_quotes(u).items():
            # берут кэшаут, когда квота уже дала прибыль, или изредка «страхуются»
            amt = q1("SELECT amount FROM bets WHERE id=?", (bid,))["amount"]
            if qt["available"] and (qt["amount"] > amt * 1.3 or rng.random() < 0.05):
                bet_tools.cashout(u, bid, qt["amount"])
                cashed += 1


edits = 0
for tour in range(1, total_tours + 1):
    if league.ensure_tour(tid, tour).get("status") != "open":
        league.open_tour(tid, tour)
    bet_round()
    ms = [m for m in open_matches() if m["tour_number"] == tour]
    # половина матчей тура играется, потом часть капперов кэшаутит экспрессы
    half = len(ms) // 2
    for i, m in enumerate(ms):
        lh, la = true_lambdas(strength[m["home_club_id"]], strength[m["away_club_id"]])
        s1, s2 = poisson(lh), poisson(la)
        results.finalize_match(m["id"], s1, s2, None, None, [], actor="manual")
        if rng.random() < 0.08:  # спор/правка счёта судьёй
            results.finalize_match(m["id"], s1 + 1, s2, None, None, [], actor="manual")
            edits += 1
        if i == half:
            cashout_round()

check("все 60 матчей сыграны", q1("SELECT COUNT(*) n FROM matches WHERE tournament_id=? AND status='confirmed'",
                                   (tid,))["n"] == 60)
check("открытых купонов не осталось", q1("SELECT COUNT(*) n FROM bets WHERE status='open'")["n"] == 0,
      str(q1("SELECT COUNT(*) n FROM bets WHERE status='open'")))

# ===== инварианты ставок =====
bad = []
for b in q("SELECT * FROM bets"):
    legs = q("SELECT l.*, m.score1, m.score2 FROM bet_legs l JOIN matches m ON m.id=l.match_id WHERE l.bet_id=?",
             (b["id"],))
    if b["status"] == "cashout":
        continue
    res = [markets.resolve_leg(0, l["market_code"], l["score1"], l["score2"]) for l in legs]
    want = "lost" if "lost" in res else "won"
    if b["status"] != want:
        bad.append((b["id"], b["status"], want))
check("исход каждого купона = пересчёт по финальному счёту", not bad, str(bad[:5]))
neg = q("SELECT telegram_id, balance FROM users WHERE balance < 0")
check("балансы не уходят в минус", not neg, str(neg))
drift = []
for b in bettors:
    u = urow(b["tg"])
    hist = q1("SELECT COALESCE(SUM(delta),0) s FROM balance_history WHERE user_id=?", (u["id"],))["s"]
    if u["balance"] != START + hist:
        drift.append((u["username"], u["balance"], START + hist))
check("баланс = старт + журнал операций (без потерь и задвоений)", not drift, str(drift[:3]))
staked = q1("SELECT COALESCE(SUM(amount),0) s FROM bets")["s"]
returned = sum(bet_tools.bet_return(b) for b in q("SELECT * FROM bets"))
bal_sum = sum(urow(b["tg"])["balance"] for b in bettors)
check("деньги сходятся: Σбалансов = Σстарта − ставки + выплаты",
      bal_sum == START * len(bettors) - staked + returned, f"{bal_sum} vs {START * len(bettors) - staked + returned}")
lb = bet_tools.leaderboard("season", tid)
check("рейтинг сезона: Σприбыли = Σ(выплат − ставок)",
      sum(r["profit"] for r in lb["leaders"]) == returned - staked)

# ===== инварианты клубов =====
cdrift = []
for cid in clubs:
    ledger = q1("SELECT COALESCE(SUM(amount),0) s FROM club_ledger WHERE club_id=? AND kind IN ('stadium','sponsor')",
                (cid,))["s"]
    now = q1("SELECT budget FROM clubs WHERE id=?", (cid,))["budget"]
    if now != budget0[cid] + ledger:
        cdrift.append((cid, now, budget0[cid] + ledger))
check("бюджет клуба = до сезона + журнал доходов (правки счёта без задвоения)", not cdrift, str(cdrift[:3]))
dup = q("SELECT club_id, match_id, kind, COUNT(*) n FROM club_ledger WHERE match_id IS NOT NULL "
        "GROUP BY club_id, match_id, kind HAVING COUNT(*) > 1")
check("по одной записи дохода на клуб/матч/вид", not dup, str(dup[:3]))
home_games = q1("SELECT COUNT(*) n FROM club_ledger WHERE kind='stadium'")["n"]
check("доход со стадиона за каждый домашний матч", home_games == 60, str(home_games))

# ===== финал сезона =====
pre = {cid: q1("SELECT budget, division_id FROM clubs WHERE id=?", (cid,)) for cid in clubs}
fin = league.finalize_season(tid, 923001)
check("финал сезона: призовые и 2↑/2↓", fin.get("ok") and len(fin["prize_rows"]) == 4 and len(fin["moves"]) == 4,
      str({k: v for k, v in fin.items() if k != "tournament"})[:300])
prize_sum = sum(q1("SELECT budget FROM clubs WHERE id=?", (cid,))["budget"] - pre[cid]["budget"] for cid in clubs)
check("призовые зачислены ровно", prize_sum == sum(p["prize"] for p in fin["prize_rows"]))
check("повторный финал не платит", league.finalize_season(tid, 923001).get("error"))

# ===== отчёт =====
if not QUIET:
    bets = q("SELECT * FROM bets")
    print("\n================ ОТЧЁТ ПО СЕЗОНУ ================")
    print(f"Купонов: {len(bets)} (принято попыток {placed}), отказов: {errors}")
    print(f"Кэшаутов: {cashed}, черновиков: {drafts}, правок счёта: {edits}")
    hold = (staked - returned) / staked * 100 if staked else 0
    print(f"Поставлено {staked}, вернулось {returned} → доход «букмекера» {hold:.1f}% "
          f"(маржа линии {markets.appsettings.setting_float('odds_margin_pct', 5)}%)")
    co = [b for b in bets if b["status"] == "cashout"]
    if co:
        print(f"Кэшаут: средне {sum(b['cashout_amount'] for b in co) / sum(b['amount'] for b in co) * 100:.0f}% от ставки")
    print("\nСтратегии (баланс в конце, старт %d):" % START)
    for s in dict.fromkeys(STRATS):
        bs = [urow(b["tg"])["balance"] for b in bettors if b["strat"] == s]
        st = bet_tools.summarize([b for b in bets if b["user_id"] in
                                  {urow(x["tg"])["id"] for x in bettors if x["strat"] == s}])
        print(f"  {s:9} баланс {min(bs)}…{max(bs)} (сред. {sum(bs) // len(bs)}), ROI {st['roi']}%, "
              f"проходимость {st['hit_rate']}%, ср. кэф {st['avg_odds']}")
    print(f"  в плюсе: {sum(urow(b['tg'])['balance'] > START for b in bettors)} из {len(bettors)}")
    groups = {}
    for leg in q("SELECT l.market_code, l.odds, l.result, b.amount, b.bet_type FROM bet_legs l "
                 "JOIN bets b ON b.id=l.bet_id WHERE b.bet_type='single' AND b.status IN ('won','lost')"):
        g = groups.setdefault(bet_tools.market_group(leg["market_code"]), [0, 0])
        g[0] += leg["amount"]
        g[1] += int(leg["amount"] * leg["odds"]) if leg["result"] == "won" else 0
    print("\nОрдинары по рынкам (доход букмекера):")
    for k, (st_, ret) in sorted(groups.items(), key=lambda x: -x[1][0]):
        print(f"  {k:14} ставок {st_:6}  → {((st_ - ret) / st_ * 100):6.1f}%")
    vb = [q1("SELECT status FROM bets WHERE id=?", (b,))["status"] for b, _, _ in value_legs]
    print(f"\nСтавки аналитика: {len(vb)}, зашло {vb.count('won')}, кэшаут {vb.count('cashout')}")
    print("\nКлубы (до финала):")
    tot = {k: q1("SELECT COALESCE(SUM(amount),0) s FROM club_ledger WHERE kind=?", (k,))["s"]
           for k in ("stadium", "sponsor", "sponsor_sign", "stadium_upgrade")}
    print(f"  стадион Σ{tot['stadium']:,}  спонсоры Σ{tot['sponsor'] + tot['sponsor_sign']:,}  "
          f"апгрейды Σ{tot['stadium_upgrade']:,}".replace(",", " "))
    for code in codes:
        ids = [cid for i, cid in enumerate(clubs) if codes[i % 3] == code]
        s = q1(f"SELECT COALESCE(SUM(amount),0) s FROM club_ledger WHERE kind LIKE 'sponsor%' "
               f"AND club_id IN ({','.join('?' * len(ids))})", ids)["s"]
        print(f"  спонсор {ce.SPONSORS[code]['name']:15} в среднем {s // len(ids):,} на клуб".replace(",", " "))
    inc = [q1("SELECT COALESCE(SUM(amount),0) s FROM club_ledger WHERE club_id=? AND kind IN ('stadium','sponsor','sponsor_sign')",
              (cid,))["s"] for cid in clubs]
    print(f"  доход клуба за сезон: {min(inc):,}…{max(inc):,}; призовые чемпиону {fin['prize_rows'][0]['prize']:,}; "
          f"стартовый бюджет {config.CLUB_START_BUDGET:,}".replace(",", " "))
    print("=================================================\n")

if fails:
    print(f"GATE 23: {len(fails)} FAIL:", fails)
    sys.exit(1)
print("GATE 23: OK")
