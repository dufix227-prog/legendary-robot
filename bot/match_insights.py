"""Аналитика матча: H2H, форма и статы клубов, подсказки, «как получен кэф», value.

Value: вторая модель — ожидаемые голы по фактической результативности клубов в турнире,
смешанная с Elo-λ по числу сыгранных матчей. Рынок «выгодный», если её вероятность выше
вероятности, заложенной в кэф (1/кэф, с маржой), на value_edge_pp п.п.
"""
import db as appdb
import markets as markets_engine
import settings as appsettings

EMP_FULL_WEIGHT_GAMES = 10   # столько матчей — и фактическая результативность весит 70%
EMP_MAX_WEIGHT = 0.7


def _club(c, cid):
    r = c.execute("SELECT id, name, elo, form FROM clubs WHERE id=?", (cid,)).fetchone()
    return dict(r) if r else {"id": cid, "name": "—", "elo": 1000, "form": ""}


def _confirmed(c, club_id: int, tournament_id: int | None = None, limit: int | None = None) -> list[dict]:
    sql = ("SELECT * FROM matches WHERE status='confirmed' AND score1 IS NOT NULL "
           "AND (home_club_id=? OR away_club_id=?)")
    args: list = [club_id, club_id]
    if tournament_id is not None:
        sql += " AND tournament_id=?"
        args.append(tournament_id)
    sql += " ORDER BY COALESCE(played_at, created_at) DESC, id DESC"
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    return [dict(r) for r in c.execute(sql, args).fetchall()]


def _persp(m: dict, club_id: int) -> tuple[int, int, int]:
    """(забито, пропущено, id соперника) с точки зрения клуба."""
    if m["home_club_id"] == club_id:
        return m["score1"], m["score2"], m["away_club_id"]
    return m["score2"], m["score1"], m["home_club_id"]


def club_stats(c, club_id: int, tournament_id: int | None = None) -> dict:
    ms = _confirmed(c, club_id, tournament_id)
    n = len(ms)
    gf = ga = w = d = l = btts = over25 = cs = 0
    for m in ms:
        f, a, _ = _persp(m, club_id)
        gf += f
        ga += a
        w += f > a
        d += f == a
        l += f < a
        btts += f > 0 and a > 0
        over25 += f + a > 2
        cs += a == 0
    pct = lambda x: round(x / n * 100) if n else 0  # noqa: E731
    return {"games": n, "wins": w, "draws": d, "losses": l,
            "gf_avg": round(gf / n, 2) if n else 0.0, "ga_avg": round(ga / n, 2) if n else 0.0,
            "btts_pct": pct(btts), "over25_pct": pct(over25), "clean_sheets": cs}


def recent(c, club_id: int, limit: int = 5) -> list[dict]:
    out = []
    for m in _confirmed(c, club_id, limit=limit):
        f, a, opp = _persp(m, club_id)
        out.append({"match_id": m["id"], "opponent": _club(c, opp)["name"], "home": m["home_club_id"] == club_id,
                    "score": f"{f}:{a}", "result": "W" if f > a else ("D" if f == a else "L")})
    return out


def h2h(c, home_id: int, away_id: int, limit: int = 10) -> dict:
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM matches WHERE status='confirmed' AND score1 IS NOT NULL AND "
        "((home_club_id=? AND away_club_id=?) OR (home_club_id=? AND away_club_id=?)) "
        "ORDER BY COALESCE(played_at, created_at) DESC, id DESC LIMIT ?",
        (home_id, away_id, away_id, home_id, limit)).fetchall()]
    hw = dr = aw = goals = 0
    meetings = []
    for m in rows:
        f, a, _ = _persp(m, home_id)
        hw += f > a
        dr += f == a
        aw += f < a
        goals += f + a
        meetings.append({"match_id": m["id"], "tour_number": m["tour_number"],
                         "home": _club(c, m["home_club_id"])["name"], "away": _club(c, m["away_club_id"])["name"],
                         "score": f"{m['score1']}:{m['score2']}", "played_at": m["played_at"]})
    return {"total": len(rows), "home_wins": hw, "draws": dr, "away_wins": aw,
            "avg_goals": round(goals / len(rows), 2) if rows else 0.0, "meetings": meetings}


def _streak_text(name: str, rec: list[dict]) -> list[str]:
    out = []
    if len(rec) >= 3:
        unbeaten = next((i for i, r in enumerate(rec) if r["result"] == "L"), len(rec))
        winless = next((i for i, r in enumerate(rec) if r["result"] == "W"), len(rec))
        wins = next((i for i, r in enumerate(rec) if r["result"] != "W"), len(rec))
        if wins >= 3:
            out.append(f"🔥 {name}: {wins} побед подряд")
        elif unbeaten >= 3:
            out.append(f"🛡 {name} без поражений в последних {unbeaten} играх")
        if winless >= 3:
            out.append(f"🥶 {name} не побеждает {winless} игр подряд")
        scored = next((i for i, r in enumerate(rec) if r["score"].startswith("0:")), len(rec))
        if scored >= 4:
            out.append(f"⚽ {name} забивает в {scored} матчах подряд")
    return out


def model_lambdas(c, m: dict) -> dict:
    """Elo-λ, фактические λ по голам в турнире и их смесь (для value)."""
    home, away = _club(c, m["home_club_id"]), _club(c, m["away_club_id"])
    lh_elo, la_elo = markets_engine.lambdas(home["elo"], away["elo"])
    sh = club_stats(c, home["id"], m["tournament_id"])
    sa = club_stats(c, away["id"], m["tournament_id"])
    games = min(sh["games"], sa["games"])
    w = EMP_MAX_WEIGHT * min(games, EMP_FULL_WEIGHT_GAMES) / EMP_FULL_WEIGHT_GAMES
    lh_emp = (sh["gf_avg"] + sa["ga_avg"]) / 2 if games else lh_elo
    la_emp = (sa["gf_avg"] + sh["ga_avg"]) / 2 if games else la_elo
    lh = max(0.2, min(4.5, (1 - w) * lh_elo + w * lh_emp))
    la = max(0.2, min(4.5, (1 - w) * la_elo + w * la_emp))
    return {"games": games, "weight": round(w, 2), "lambda_home": round(lh, 3), "lambda_away": round(la, 3),
            "lambda_home_emp": round(lh_emp, 3), "lambda_away_emp": round(la_emp, 3)}


def value_flags(c, m: dict, mkts: list[dict]) -> dict[str, dict]:
    """Код рынка → {model_prob, implied_prob, edge_pp} для рынков с перевесом модели."""
    if m["status"] != "pending" or m["home_club_id"] is None or m["away_club_id"] is None:
        return {}
    lam = model_lambdas(c, m)
    if lam["games"] < appsettings.setting_int("value_min_games", 3):
        return {}
    probs = markets_engine.market_probs_from_lambdas(lam["lambda_home"], lam["lambda_away"])
    edge_min = appsettings.setting_float("value_edge_pp", 5)
    out = {}
    for k in mkts:
        p = probs.get(k["code"])
        if p is None or not k["odds"]:
            continue
        implied = 1 / k["odds"]
        edge = (p - implied) * 100
        if edge >= edge_min:
            out[k["code"]] = {"model_prob": round(p * 100, 1), "implied_prob": round(implied * 100, 1),
                              "edge_pp": round(edge, 1)}
    return out


def _match(c, match_id: int) -> dict:
    r = c.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
    if not r:
        raise LookupError("Матч не найден")
    return dict(r)


def _markets(c, match_id: int) -> list[dict]:
    return [dict(r) for r in c.execute(
        "SELECT code, label, odds FROM markets WHERE match_id=? AND is_active=1 ORDER BY id", (match_id,)).fetchall()]


def insights(match_id: int) -> dict:
    c = appdb.db()
    try:
        m = _match(c, match_id)
        home, away = _club(c, m["home_club_id"]), _club(c, m["away_club_id"])
        rh, ra = recent(c, home["id"]), recent(c, away["id"])
        hh = h2h(c, home["id"], away["id"])
        tips = _streak_text(home["name"], rh) + _streak_text(away["name"], ra)
        if hh["total"] >= 2:
            if hh["home_wins"] > hh["away_wins"] + hh["draws"]:
                tips.append(f"📚 В личных встречах перевес у «{home['name']}»: {hh['home_wins']} из {hh['total']}")
            elif hh["away_wins"] > hh["home_wins"] + hh["draws"]:
                tips.append(f"📚 В личных встречах перевес у «{away['name']}»: {hh['away_wins']} из {hh['total']}")
            if hh["avg_goals"] >= 3.5:
                tips.append(f"🎯 Личные встречи результативные: в среднем {hh['avg_goals']} гола")
        mk = _markets(c, match_id)
        value = value_flags(c, m, mk)
        return {"match_id": match_id,
                "home": {"name": home["name"], "elo": home["elo"], "recent": rh,
                         "stats": club_stats(c, home["id"], m["tournament_id"])},
                "away": {"name": away["name"], "elo": away["elo"], "recent": ra,
                         "stats": club_stats(c, away["id"], m["tournament_id"])},
                "h2h": hh, "insights": tips, "value": value}
    finally:
        c.close()


def explain(match_id: int) -> dict:
    c = appdb.db()
    try:
        m = _match(c, match_id)
        home, away = _club(c, m["home_club_id"]), _club(c, m["away_club_id"])
        base = markets_engine.explain(home["elo"], away["elo"])
        mk = _markets(c, match_id)
        value = value_flags(c, m, mk)
        rows = []
        for k in mk:
            e = base["markets"].get(k["code"])
            rows.append({"code": k["code"], "label": k["label"], "odds": k["odds"],
                         "prob": round(e["prob"] * 100, 1) if e else None,
                         "fair_odds": e["fair_odds"] if e else None,
                         "value": value.get(k["code"])})
        return {"match_id": match_id, "home": home["name"], "away": away["name"],
                **{k: v for k, v in base.items() if k != "markets"},
                "model": model_lambdas(c, m), "markets": rows,
                "value_edge_pp": appsettings.setting_float("value_edge_pp", 5)}
    finally:
        c.close()


def value_radar(limit: int = 15) -> list[dict]:
    """Выгодные кэфы по всей открытой линии (лига — открытые туры, кубки — pending)."""
    c = appdb.db()
    try:
        rows = [dict(r) for r in c.execute(
            "SELECT m.* FROM matches m LEFT JOIN tours t ON t.tournament_id=m.tournament_id "
            "AND t.tour_number=m.tour_number WHERE m.status='pending' "
            "AND (m.tour_number IS NULL OR t.status='open')").fetchall()]
        picks = []
        for m in rows:
            mk = _markets(c, m["id"])
            labels = {k["code"]: k for k in mk}
            for code, v in value_flags(c, m, mk).items():
                picks.append({"match_id": m["id"], "market_code": code, "label": labels[code]["label"],
                              "odds": labels[code]["odds"], "home": _club(c, m["home_club_id"])["name"],
                              "away": _club(c, m["away_club_id"])["name"], "tour_number": m["tour_number"], **v})
        picks.sort(key=lambda p: -p["edge_pp"])
        return picks[:limit]
    finally:
        c.close()
