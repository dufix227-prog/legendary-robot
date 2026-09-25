"""API-маршруты мини-аппа, ядро экранов (блок 6) + ставка/промо (блок 7).

Все эндпоинты (кроме статики) требуют валидный initData (окно 24 ч) и
отдают 403 замороженным. Формат ответов совместим с контрактом logovo-copy.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

from aiohttp import web

import club_economy  # noqa: E402
import config  # noqa: E402
import db as appdb  # noqa: E402
import elo as elo_engine  # noqa: E402
import league  # noqa: E402
import markets as markets_engine  # noqa: E402
import settings as appsettings  # noqa: E402
from bet_tools import rank_by_level  # noqa: E402,F401
from clubs_catalog import FORMAT_NAMES  # noqa: E402

from .helpers import require_active_user  # noqa: E402

LOGO_DIR = config.PROJECT_ROOT / "logovo-copy" / "src" / "assets" / "logos"


def j(data, status=200):
    return web.json_response(data, status=status)


def err(text, status=400, code=None):
    return web.json_response({"status": "error", "error": text, "code": code}, status=status)


# ===== служебные =====

def _club_row(c, club_id):
    if club_id is None:
        return None
    row = c.execute("SELECT * FROM clubs WHERE id=?", (club_id,)).fetchone()
    return dict(row) if row else None


def _club_public(club: dict | None) -> dict | None:
    if not club:
        return None
    logo = club.get("logo_path") or ""
    return {
        "id": club["id"], "name": club["name"], "elo": club["elo"], "form": club["form"] or "",
        "logo": f"/assets/logos/{Path(logo).name}" if logo else None,
        "budget": club["budget"],
        **club_economy.public_fields(club),
    }


def _match_public(c, m: dict) -> dict:
    home = _club_public(_club_row(c, m["home_club_id"]))
    away = _club_public(_club_row(c, m["away_club_id"]))
    mkts = [dict(r) for r in c.execute(
        "SELECT code, label, odds FROM markets WHERE match_id=? AND is_active=1 ORDER BY id",
        (m["id"],)).fetchall()]
    return {
        "id": m["id"],
        "tournament_id": m["tournament_id"],
        "tour_number": m["tour_number"],
        "stage": m["stage"],
        "status": m["status"],
        "score_home": m["score1"], "score_away": m["score2"],
        "pens_home": m["pens1"], "pens_away": m["pens2"],
        "scheduled_at": m.get("scheduled_at"),
        "home": home, "away": away,
        "markets": [{"code": k["code"], "label": k["label"], "odds": k["odds"]} for k in mkts],
    }


# ===== блок 6: ядро экранов =====

async def api_leaderboard(request):
    require_active_user(request)
    c = appdb.db()
    rows = c.execute(
        "SELECT telegram_id, username, first_name, balance, xp, level, total_won, bets_count "
        "FROM users WHERE is_frozen=0 ORDER BY balance DESC LIMIT 50").fetchall()
    c.close()
    return j({"leaderboard": [{
        "user_id": r["telegram_id"],
        "name": r["username"] or r["first_name"] or f"Игрок {r['telegram_id']}",
        "balance": r["balance"], "level": r["level"], "xp": r["xp"],
        "total_won": r["total_won"], "bets_count": r["bets_count"],
    } for r in rows]})


async def api_tournaments(request):
    require_active_user(request)
    out = []
    for t in league.all_active_tournaments():
        d = {
            "id": t["id"], "name": t["name"], "format": t["format"],
            "format_name": FORMAT_NAMES.get(t["format"], t["format"]),
            "tour_mode": t["tour_mode"], "current_tour": t["current_tour"],
            "total_tours": t["total_tours"], "stage": t["stage"],
        }
        if t["format"] == "league":
            d["divisions"] = league.tournament_divisions(t["id"])
        out.append(d)
    return j({"tournaments": out})


async def api_divisions(request):
    require_active_user(request)
    c = appdb.db()
    rows = [dict(r) for r in c.execute(
        "SELECT d.*, COUNT(cl.id) AS clubs_count FROM divisions d "
        "LEFT JOIN clubs cl ON cl.division_id=d.id "
        "WHERE d.is_active=1 GROUP BY d.id ORDER BY d.tournament_id, d.sort_order").fetchall()]
    c.close()
    return j({"divisions": rows})


async def api_standings(request):
    u = require_active_user(request)
    q = request.rel_url.query
    division_id = q.get("division_id")
    if not division_id:
        # дефолт: дивизион клуба юзера, иначе первый
        player = _player_by_tg(u["telegram_id"])
        club = core_club_of_player(player["id"]) if player else None
        if club and club["division_id"]:
            division_id = club["division_id"]
        else:
            c = appdb.db()
            row = c.execute("SELECT id FROM divisions WHERE is_active=1 ORDER BY tournament_id, sort_order LIMIT 1").fetchone()
            c.close()
            division_id = row["id"] if row else None
    if not division_id:
        return j({"standings": [], "division": None})
    table = league.division_standings(int(division_id))
    c = appdb.db()
    enriched = []
    for row in table:
        cl = _club_row(c, row["club_id"])
        enriched.append({**row, **(_club_public(cl) or {})})
    div = dict(c.execute("SELECT * FROM divisions WHERE id=?", (division_id,)).fetchone() or {})
    # зоны повышения/вылета: только если есть дивизион выше/ниже и лига их включила
    zones = {"up": 0, "down": 0}
    if div:
        t = c.execute("SELECT promote_count FROM tournaments WHERE id=?", (div["tournament_id"],)).fetchone()
        pc = 3 if not t or t["promote_count"] is None else int(t["promote_count"])
        sibs = [r["id"] for r in c.execute(
            "SELECT id FROM divisions WHERE tournament_id=? AND is_active=1 ORDER BY sort_order, id",
            (div["tournament_id"],)).fetchall()]
        if div["id"] in sibs and len(enriched) > 2 * pc:
            i = sibs.index(div["id"])
            zones = {"up": pc if i > 0 else 0, "down": pc if i < len(sibs) - 1 else 0}
    c.close()
    return j({"standings": enriched, "division": div, "zones": zones})


def _player_by_tg(telegram_id):
    c = appdb.db()
    row = c.execute("SELECT * FROM players WHERE telegram_id=?", (telegram_id,)).fetchone()
    c.close()
    return dict(row) if row else None


def core_club_of_player(player_id):
    import core
    return core.club_of_player(player_id)


async def api_line(request):
    """Линия: матчи открытых туров (лига) + кубковые игры в статусе pending."""
    u = require_active_user(request)
    q = request.rel_url.query
    division_id = q.get("division_id")
    c = appdb.db()
    # открытые туры
    if division_id:
        tours = c.execute(
            "SELECT t.*, tr.name AS tname, tr.format FROM tours t "
            "JOIN tournaments tr ON tr.id=t.tournament_id "
            "JOIN divisions d ON d.tournament_id=t.tournament_id AND d.id=? "
            "WHERE t.status='open' ORDER BY t.tournament_id LIMIT 5",
            (division_id,)).fetchall()
    else:
        tours = c.execute(
            "SELECT t.*, tr.name AS tname, tr.format FROM tours t "
            "JOIN tournaments tr ON tr.id=t.tournament_id "
            "WHERE t.status='open' ORDER BY t.tournament_id LIMIT 5").fetchall()
    matches = []
    for t in tours:
        rows = c.execute(
            "SELECT * FROM matches WHERE tournament_id=? AND tour_number=? AND status='pending'",
            (t["tournament_id"], t["tour_number"])).fetchall()
        for m in rows:
            matches.append({**_match_public(c, dict(m)), "tour_status": t["status"],
                            "deadline": t["deadline"], "tournament_name": t["tname"]})
    # кубки
    cup_rows = c.execute(
        "SELECT m.* FROM matches m JOIN tournaments tr ON tr.id=m.tournament_id "
        "WHERE tr.format!='league' AND m.status='pending'").fetchall()
    for m in cup_rows:
        tr = dict(c.execute("SELECT name FROM tournaments WHERE id=?", (m["tournament_id"],)).fetchone())
        matches.append({**_match_public(c, dict(m)), "tour_status": "open",
                        "deadline": None, "tournament_name": tr["name"]})
    c.close()
    return j({"matches": matches, "open_tours_count": len(tours)})


async def api_matches(request):
    require_active_user(request)
    q = request.rel_url.query
    c = appdb.db()
    sql = "SELECT * FROM matches WHERE 1=1"
    args: list = []
    if q.get("tour"):
        sql += " AND tour_number=?"
        args.append(int(q["tour"]))
    if q.get("status"):
        sql += " AND status=?"
        args.append(q["status"])
    if q.get("division_id"):
        sql += " AND (home_club_id IN (SELECT id FROM clubs WHERE division_id=?) OR away_club_id IN (SELECT id FROM clubs WHERE division_id=?))"
        args += [int(q["division_id"]), int(q["division_id"])]
    sql += " ORDER BY id DESC LIMIT 100"
    rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    out = [_match_public(c, m) for m in rows]
    c.close()
    return j({"matches": out})


async def api_match_detail(request):
    require_active_user(request)
    mid = int(request.match_info["match_id"])
    c = appdb.db()
    row = c.execute("SELECT * FROM matches WHERE id=?", (mid,)).fetchone()
    if not row:
        c.close()
        return err("Матч не найден", 404)
    m = dict(row)
    data = _match_public(c, m)
    goals = [dict(r) for r in c.execute(
        "SELECT raw_name, minute, side, ord FROM match_goals WHERE match_id=? ORDER BY ord",
        (mid,)).fetchall()]
    data["goals"] = goals
    hist = [dict(r) for r in c.execute(
        "SELECT market_code, odds, recorded_at FROM odds_history WHERE match_id=? ORDER BY id",
        (mid,)).fetchall()]
    data["odds_history"] = hist
    import league_stats
    data["has_photo"] = bool(league_stats.screenshot_file(m))
    c.close()
    return j(data)


async def api_results(request):
    require_active_user(request)
    q = request.rel_url.query
    limit = min(int(q.get("limit", "30")), 100)
    c = appdb.db()
    sql = "SELECT * FROM matches WHERE status='confirmed'"
    args: list = []
    if q.get("division_id"):
        sql += (" AND (home_club_id IN (SELECT id FROM clubs WHERE division_id=?) "
                "OR away_club_id IN (SELECT id FROM clubs WHERE division_id=?))")
        args += [int(q["division_id"]), int(q["division_id"])]
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    out = [_match_public(c, m) for m in rows]
    c.close()
    return j({"results": out})


# ===== избранное =====

async def api_favorites(request):
    u = require_active_user(request)
    c = appdb.db()
    uid = _uid(c, u["telegram_id"])
    if request.method == "POST":
        body = await request.json()
        c.execute("INSERT OR IGNORE INTO favorites (user_id, match_id) VALUES (?,?)",
                  (uid, int(body.get("match_id", 0))))
        c.commit()
        c.close()
        return j({"status": "ok"})
    if request.method == "DELETE":
        fid = int(request.match_info["id"])
        c.execute("DELETE FROM favorites WHERE user_id=? AND match_id=?", (uid, fid))
        c.commit()
        c.close()
        return j({"status": "ok"})
    rows = [r["match_id"] for r in c.execute(
        "SELECT match_id FROM favorites WHERE user_id=?", (uid,)).fetchall()]
    c.close()
    return j({"favorites": rows})


def _uid(c, telegram_id) -> int:
    return c.execute("SELECT id FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()["id"]


# ===== уведомления (апп-центр) =====

async def api_notifications(request):
    u = require_active_user(request)
    c = appdb.db()
    uid = _uid(c, u["telegram_id"])
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 50", (uid,)).fetchall()]
    unread = c.execute("SELECT COUNT(*) n FROM notifications WHERE user_id=? AND is_read=0", (uid,)).fetchone()["n"]
    if request.method == "POST":
        c.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (uid,))
        c.commit()
        c.close()
        return j({"status": "ok"})
    c.close()
    return j({"notifications": rows, "unread": unread})


# ===== прогрессия (кабинет) =====

async def api_progression(request):
    u = require_active_user(request)
    c = appdb.db()
    uid = _uid(c, u["telegram_id"])
    row = dict(c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())
    streak = dict(c.execute("SELECT * FROM user_streaks WHERE user_id=?", (uid,)).fetchone() or {"streak_days": 0, "last_bonus_date": None})
    c.close()
    xp_total = row["xp"]
    level = max(1, row["level"])
    need = level * appsettings.setting_int("level_xp_step", 500)
    return j({
        "balance": row["balance"], "xp": xp_total, "level": level,
        "xp_needed": need, "rank": rank_by_level(level),
        "streak_days": streak["streak_days"], "last_bonus_date": streak["last_bonus_date"],
        "total_wagered": row["total_wagered"], "total_won": row["total_won"],
        "bets_count": row["bets_count"], "bets_won": row["bets_won"],
    })


# ===== мой клуб =====

async def api_cabinet_overview(request):
    u = require_active_user(request)
    player = _player_by_tg(u["telegram_id"])
    club = core_club_of_player(player["id"]) if player else None
    c = appdb.db()
    req = c.execute("SELECT status FROM club_requests WHERE telegram_id=?", (u["telegram_id"],)).fetchone()
    c.close()
    if not club:
        return j({"club": None, "club_request": req["status"] if req else None})
    c = appdb.db()
    squad = c.execute("SELECT COUNT(*) n FROM club_cards WHERE club_id=?", (club["id"],)).fetchone()["n"]
    division = c.execute("SELECT name FROM divisions WHERE id=?", (club["division_id"],)).fetchone() if club["division_id"] else None
    c.close()
    return j({
        "club": {**_club_public(club), "squad_size": squad,
                 "division": division["name"] if division else None},
        "club_request": None,
    })


async def api_cabinet_squad(request):
    u = require_active_user(request)
    player = _player_by_tg(u["telegram_id"])
    club = core_club_of_player(player["id"]) if player else None
    if not club:
        return j({"squad": [], "club": None})
    c = appdb.db()
    rows = [dict(r) for r in c.execute(
        "SELECT id, name, position, rating FROM club_cards WHERE club_id=? "
        "ORDER BY rating DESC, name", (club["id"],)).fetchall()]
    c.close()
    return j({"squad": rows, "club": _club_public(club)})


async def api_club_request(request):
    """«Запросить клуб» → заявка + ЛС root (решение 09)."""
    u = require_active_user(request)
    c = appdb.db()
    row = c.execute("SELECT status FROM club_requests WHERE telegram_id=?", (u["telegram_id"],)).fetchone()
    if row:
        c.close()
        return j({"status": row["status"]})
    c.execute("INSERT INTO club_requests (telegram_id) VALUES (?)", (u["telegram_id"],))
    c.commit()
    c.close()
    # уведомить root в ЛС (бот живёт в том же процессе; в standalone молча пропускаем)
    try:
        from telegram import Bot
        bot = Bot(config.BOT_TOKEN)
        name = u.get("username") or u.get("first_name") or str(u["telegram_id"])
        await bot.send_message(
            next(iter(config.ADMIN_IDS)),
            f"⚽ Заявка на клуб от {name} (tg {u['telegram_id']}). Выдать: /club {u['telegram_id']} <клуб>",
        )
    except Exception:
        pass
    return j({"status": "pending"})


async def api_wallet(request):  # переопределяется в server.py — здесь не регистрируем
    raise web.HTTPNotImplemented()


def setup_routes(app: web.Application) -> None:
    r = app.router
    r.add_get("/api/leaderboard", api_leaderboard)
    r.add_get("/api/tournaments", api_tournaments)
    r.add_get("/api/divisions", api_divisions)
    r.add_get("/api/standings", api_standings)
    r.add_get("/api/line", api_line)
    r.add_get("/api/matches", api_matches)
    r.add_get("/api/matches/{match_id}", api_match_detail)
    r.add_get("/api/matches/{match_id}/markets", api_match_detail)
    r.add_get("/api/results", api_results)
    r.add_get("/api/favorites", api_favorites)
    r.add_post("/api/favorites", api_favorites)
    r.add_delete("/api/favorites/{id}", api_favorites)
    r.add_get("/api/notifications", api_notifications)
    r.add_post("/api/notifications/read", api_notifications)
    r.add_get("/api/progression", api_progression)
    r.add_get("/api/cabinet/overview", api_cabinet_overview)
    r.add_get("/api/cabinet/squad", api_cabinet_squad)
    r.add_post("/api/club-request", api_club_request)
    # блок 7: ставки
    r.add_post("/api/predictions", api_place_prediction)
    r.add_get("/api/predictions", api_predictions)
    r.add_get("/api/predictions/{bet_id}", api_prediction_detail)
    r.add_post("/api/predictions/{bet_id}/repeat", api_repeat_prediction)
    r.add_post("/api/bonus/streak", api_streak_bonus)
    r.add_post("/api/promo/redeem", api_promo_redeem)
    r.add_post("/api/admin/bets/{bet_id}/void", api_admin_void_bet)
    # блок 8: трансферы
    r.add_get("/api/transfers/view", api_transfers_view)
    r.add_get("/api/transfers/market", api_transfers_market)
    r.add_post("/api/transfers/lots", api_transfer_lot_create)
    r.add_delete("/api/transfers/lots/{lot_id}", api_transfer_lot_cancel)
    r.add_post("/api/transfers/lots/{lot_id}/buy", api_transfer_lot_buy)
    r.add_post("/api/transfers/free-agent/{card_id}/sign", api_free_agent_sign)
    r.add_post("/api/transfers/exchange", api_transfer_exchange)
    r.add_post("/api/transfers/exchange/{transfer_id}/accept", api_transfer_exchange_accept)
    r.add_post("/api/transfers/deals/{transfer_id}/decide", api_transfer_decide)
    # блок 10: админка
    r.add_get("/api/admin/dashboard", api_admin_dashboard)
    r.add_get("/api/admin/bets", api_admin_bets)
    r.add_post("/api/admin/pause", api_admin_pause)
    r.add_get("/api/admin/players", api_admin_players)
    r.add_post("/api/admin/players/{tg}", api_admin_player_action)
    r.add_get("/api/admin/settings", api_admin_settings)
    r.add_post("/api/admin/settings", api_admin_settings)
    r.add_get("/api/admin/exposure", api_admin_exposure)
    r.add_get("/api/admin/audit", api_admin_audit)
    r.add_post("/api/admin/recalc", api_admin_recalc)
    r.add_post("/api/admin/markets/refresh", api_admin_markets_refresh)
    r.add_post("/api/admin/season/finalize", api_admin_season_finalize)
    _setup_ocr_routes(app)


# ===== блок 7: ставки, бонус-серия, промокоды =====

import bets_engine  # noqa: E402


def _user_row(u: dict) -> dict:
    c = appdb.db()
    row = c.execute("SELECT * FROM users WHERE telegram_id=?", (u["telegram_id"],)).fetchone()
    c.close()
    return dict(row)


async def api_place_prediction(request):
    u = require_active_user(request)
    body = await request.json()
    try:
        r = bets_engine.place_bet(
            _user_row(u), int(body.get("amount", 0)),
            body.get("selections") or [], body.get("idempotency_key"),
        )
    except bets_engine.BetError as e:
        return err(e.message if hasattr(e, "message") else str(e), 400, e.code)
    return j({"status": "ok", **r})


async def api_predictions(request):
    u = require_active_user(request)
    q = request.rel_url.query
    preds = bets_engine.user_predictions(_user_row(u), q.get("status"), int(q.get("limit", "50")))
    return j({"predictions": preds})


async def api_prediction_detail(request):
    u = require_active_user(request)
    preds = [p for p in bets_engine.user_predictions(_user_row(u), None, 200) if p["id"] == int(request.match_info["bet_id"])]
    if not preds:
        return err("Купон не найден", 404)
    return j(preds[0])


async def api_repeat_prediction(request):
    u = require_active_user(request)
    preds = [p for p in bets_engine.user_predictions(_user_row(u), None, 200)
             if p["id"] == int(request.match_info["bet_id"])]
    if not preds:
        return err("Купон не найден", 404)
    # повторы: фронт соберёт купон заново из legs (возвращаем legs_label для UI)
    return j({"status": "ok", "legs_label": preds[0]["legs_label"]})


async def api_streak_bonus(request):
    u = require_active_user(request)
    try:
        r = bets_engine.claim_streak(_user_row(u))
    except bets_engine.BetError as e:
        return err(str(e), 400, e.code)
    return j({"status": "ok", **r})


async def api_promo_redeem(request):
    u = require_active_user(request)
    body = await request.json()
    try:
        r = bets_engine.redeem_promo(_user_row(u), body.get("code", ""))
    except bets_engine.BetError as e:
        return err(str(e), 400, e.code)
    return j({"status": "ok", **r})


async def api_admin_void_bet(request):
    u = require_active_user(request)
    me = _user_row(u)
    if not me["is_admin"]:
        return err("Только админ", 403)
    body = await request.json()
    try:
        r = bets_engine.void_bet(int(request.match_info["bet_id"]),
                                 body.get("reason", "решение админа"), me["telegram_id"])
    except bets_engine.BetError as e:
        return err(str(e), 400, e.code)
    return j({"status": "ok", **r})


setup_routes._block7 = True  # маркер, что расширение применено


# ===== блок 8: трансферы =====

import transfers as transfers_engine  # noqa: E402


def _my_club_id(u: dict) -> int | None:
    c = appdb.db()
    row = c.execute(
        "SELECT cp.club_id FROM club_players cp JOIN players p ON p.id=cp.player_id "
        "WHERE p.telegram_id=?", (u["telegram_id"],)).fetchone()
    c.close()
    return row["club_id"] if row else None


def _club_guard(u: dict) -> int:
    club_id = _my_club_id(u)
    if not club_id:
        raise transfers_engine.TransferError("NO_CLUB", "У тебя нет клуба — попроси root выдать")
    return club_id


async def api_transfers_view(request):
    u = require_active_user(request)
    club_id = _my_club_id(u)
    if not club_id:
        return j({"no_club": True})
    return j(transfers_engine.club_transfers_view(club_id))


async def api_transfers_market(request):
    u = require_active_user(request)
    q = request.rel_url.query

    def num(*keys):
        for k in keys:
            if (q.get(k) or "").strip():
                return int(q[k])
        return None

    try:
        data = transfers_engine.market_list(
            position=q.get("position") or None,
            q=(q.get("q") or "").strip()[:60] or None,
            alt=q.get("alt") in ("1", "true"),
            ovr_min=num("ovr_min", "min_rating"), ovr_max=num("ovr_max"),
            price_min=num("price_min"), price_max=num("price_max", "max_price"),
            kind=q.get("kind") or None,
            verified=q.get("verified") in ("1", "true"),
            nation=q.get("nation") or None, league=q.get("league") or None,
            sort=q.get("sort") or None,
            stat_mins={s: num(f"{s}_min") for s in ("pac", "sho", "pas", "dri", "def", "phy")},
        )
    except ValueError:
        return err("Фильтр: нужно целое число", 400, "BAD_FILTER")
    except transfers_engine.TransferError as e:
        return err(str(e), 400, e.code)
    my_club = _my_club_id(u)
    if my_club:
        data["lots"] = [r for r in data["lots"] if r["seller_club_id"] != my_club]
    return j(data)


async def api_transfer_lot_create(request):
    u = require_active_user(request)
    try:
        body = await request.json()
        club_id = _club_guard(u)
        r = transfers_engine.create_lot(club_id, int(body["card_id"]), body.get("kind", "fix"),
                                        body.get("price"), body.get("buyout_price"), u["telegram_id"],
                                        hours=body.get("hours"))
    except transfers_engine.TransferError as e:
        return err(str(e), 400, e.code)
    except (KeyError, TypeError, ValueError, AttributeError):
        return err("card_id и price — целые числа", 400, "BAD_INPUT")
    return j({"status": "ok", **r})


async def api_transfer_lot_cancel(request):
    u = require_active_user(request)
    try:
        club_id = _club_guard(u)
        r = transfers_engine.cancel_lot(club_id, int(request.match_info["lot_id"]))
    except transfers_engine.TransferError as e:
        return err(str(e), 400, e.code)
    return j({"status": "ok", **r})


async def api_transfer_lot_buy(request):
    u = require_active_user(request)
    try:
        club_id = _club_guard(u)
        r = transfers_engine.buy_lot(club_id, int(request.match_info["lot_id"]), u["telegram_id"])
    except transfers_engine.TransferError as e:
        return err(str(e), 400, e.code)
    return j({"status": "ok", **r})


async def api_free_agent_sign(request):
    u = require_active_user(request)
    try:
        club_id = _club_guard(u)
        r = transfers_engine.sign_free_agent(club_id, int(request.match_info["card_id"]), u["telegram_id"])
    except transfers_engine.TransferError as e:
        return err(str(e), 400, e.code)
    return j({"status": "ok", **r})


async def api_transfer_exchange(request):
    u = require_active_user(request)
    try:
        body = await request.json()
        club_id = _club_guard(u)
        give = body.get("give_card_id")
        r = transfers_engine.propose_exchange(
            club_id, int(body["to_club_id"]), int(give) if give else None,
            int(body["want_card_id"]), int(body.get("money") or 0), u["telegram_id"])
    except transfers_engine.TransferError as e:
        return err(str(e), 400, e.code)
    except (KeyError, TypeError, ValueError, AttributeError):
        return err("to_club_id, want_card_id — целые; give_card_id и/или money", 400, "BAD_INPUT")
    return j({"status": "ok", **r})


async def api_transfer_exchange_accept(request):
    u = require_active_user(request)
    try:
        r = transfers_engine.accept_exchange(int(request.match_info["transfer_id"]), u["telegram_id"])
    except transfers_engine.TransferError as e:
        return err(str(e), 400, e.code)
    return j({"status": "ok", **r})


async def api_transfer_decide(request):
    u = require_active_user(request)
    me = _user_row(u)
    try:
        body = await request.json()
        approve = bool(body.get("approve"))
        transfer_id = int(request.match_info["transfer_id"])
    except (TypeError, ValueError, AttributeError):
        return err("Нужен JSON {approve: true|false}", 400, "BAD_INPUT")
    # судья турнира этой сделки / root / админ аппа; участник сделки не судит свою
    ok, why = transfers_engine.judge_rights(me["telegram_id"], transfer_id)
    if not ok:
        return err(why, 403, "NOT_JUDGE")
    try:
        r = transfers_engine.judge_decide(transfer_id, approve, me["telegram_id"])
    except transfers_engine.TransferError as e:
        return err(str(e), 400, e.code)
    return j({"status": "ok", **r})




# ===== блок 10: админка аппа =====

def _require_admin(u: dict) -> dict:
    me = _user_row(u)
    if not me["is_admin"]:
        raise PermissionError("Только админ")
    return me


def _audit(c, actor_tg: int, action: str, details: str) -> None:
    c.execute("INSERT INTO tournament_audit_log (tournament_id, actor_telegram_id, action, details) "
              "VALUES (NULL, ?, ?, ?)", (actor_tg, action, details))


def _audit_now(actor_tg: int, action: str, details: str) -> None:
    """Аудит отдельной транзакцией (без общего соединения) — с commit."""
    c = appdb.db()
    try:
        _audit(c, actor_tg, action, details)
        c.commit()
    finally:
        c.close()


async def api_admin_dashboard(request):
    u = require_active_user(request)
    try:
        me = _require_admin(u)
    except PermissionError as e:
        return err(str(e), 403)
    c = appdb.db()
    d = {
        "users": c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"],
        "frozen": c.execute("SELECT COUNT(*) n FROM users WHERE is_frozen=1").fetchone()["n"],
        "open_bets": c.execute("SELECT COUNT(*) n FROM bets WHERE status='open'").fetchone()["n"],
        "exposure": c.execute("SELECT COALESCE(SUM(potential_win),0) s FROM bets WHERE status='open'").fetchone()["s"],
        "wagered_total": c.execute("SELECT COALESCE(SUM(total_wagered),0) s FROM users").fetchone()["s"],
        "pending_matches": c.execute("SELECT COUNT(*) n FROM matches WHERE status='pending'").fetchone()["n"],
        "disputed_matches": c.execute("SELECT COUNT(*) n FROM matches WHERE status='disputed'").fetchone()["n"],
        "open_debts": c.execute("SELECT COALESCE(SUM(amount),0) s FROM debts WHERE status='open'").fetchone()["s"],
        "clubs": c.execute("SELECT COUNT(*) n FROM clubs").fetchone()["n"],
        "paused": bool(appsettings.setting_int("bets_paused")),
        "paused_reason": appsettings.get_setting("bets_paused_reason") or "",
    }
    c.close()
    return j({"dashboard": d})


async def api_admin_bets(request):
    u = require_active_user(request)
    try:
        _require_admin(u)
    except PermissionError as e:
        return err(str(e), 403)
    q = request.rel_url.query
    c = appdb.db()
    sql = ("SELECT b.*, us.username, us.first_name FROM bets b "
           "JOIN users us ON us.id=b.user_id")
    args: list = []
    if q.get("status"):
        sql += " WHERE b.status=?"
        args.append(q["status"])
    sql += " ORDER BY b.id DESC LIMIT 100"
    rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    c.close()
    return j({"bets": rows})


async def api_admin_pause(request):
    u = require_active_user(request)
    try:
        me = _require_admin(u)
    except PermissionError as e:
        return err(str(e), 403)
    body = await request.json()
    scope = body.get("scope", "global")
    on = bool(body.get("on"))
    reason = body.get("reason", "")
    if scope == "global":
        appsettings.set_setting("bets_paused", 1 if on else 0)
        appsettings.set_setting("bets_paused_reason", reason)
    elif scope == "match":
        paused = json.loads(appsettings.get_setting("bets_paused_matches", "[]") or "[]")
        mid = int(body.get("match_id", 0))
        if on and mid not in paused:
            paused.append(mid)
        if not on and mid in paused:
            paused.remove(mid)
        appsettings.set_setting("bets_paused_matches", json.dumps(paused))
    else:
        return err("scope = global|match")
    _audit_now(me["telegram_id"], "pause" if on else "unpause", f"{scope} {reason}")
    return j({"status": "ok"})


async def api_admin_players(request):
    u = require_active_user(request)
    try:
        _require_admin(u)
    except PermissionError as e:
        return err(str(e), 403)
    q = request.rel_url.query
    c = appdb.db()
    sql = "SELECT telegram_id, username, first_name, balance, is_admin, is_frozen, freeze_reason, xp, level FROM users"
    args: list = []
    if q.get("q"):
        sql += " WHERE username LIKE ? OR first_name LIKE ? OR CAST(telegram_id AS TEXT) LIKE ?"
        args += [f"%{q['q']}%"] * 3
    sql += " ORDER BY id LIMIT 200"
    rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    c.close()
    return j({"players": rows})


async def api_admin_player_action(request):
    u = require_active_user(request)
    try:
        me = _require_admin(u)
    except PermissionError as e:
        return err(str(e), 403)
    body = await request.json()
    tg = int(request.match_info["tg"])
    action = body.get("action")
    # права на права — только root; себе баланс не крутим
    if action in ("make_admin", "unmake_admin") and me["telegram_id"] not in config.ADMIN_IDS:
        return err("Назначать админов может только root", 403)
    if action == "unmake_admin" and tg in config.ADMIN_IDS:
        return err("root из ADMIN_IDS снимается только в bot/.env", 400)
    if action == "adjust" and tg == me["telegram_id"]:
        return err("Нельзя корректировать собственный баланс", 403)
    c = appdb.db()
    row = c.execute("SELECT * FROM users WHERE telegram_id=?", (tg,)).fetchone()
    if not row:
        c.close()
        return err("Юзер не найден", 404)
    if action == "adjust":
        try:
            delta = int(body.get("delta", 0))
        except (TypeError, ValueError):
            c.close()
            return err("delta — целое число")
        reason = body.get("reason", "корректировка админа")
        if delta:
            c.execute("UPDATE users SET balance=balance+? WHERE telegram_id=?", (delta, tg))
            c.execute("INSERT INTO balance_history (user_id, delta, reason) VALUES (?,?,?)",
                      (row["id"], delta, reason))
    elif action == "ban":
        c.execute("UPDATE users SET is_frozen=1, freeze_reason=? WHERE telegram_id=?",
                  (body.get("reason", "нарушение правил"), tg))
    elif action == "unban":
        c.execute("UPDATE users SET is_frozen=0, freeze_reason=NULL WHERE telegram_id=?", (tg,))
    elif action == "make_admin":
        c.execute("UPDATE users SET is_admin=1 WHERE telegram_id=?", (tg,))
    elif action == "unmake_admin":
        c.execute("UPDATE users SET is_admin=0 WHERE telegram_id=?", (tg,))
    else:
        c.close()
        return err("action = adjust|ban|unban|make_admin|unmake_admin")
    _audit(c, me["telegram_id"], f"player_{action}", f"tg={tg} {body.get('reason', '')}")
    c.commit()
    fresh = dict(c.execute("SELECT balance, is_frozen, freeze_reason FROM users WHERE telegram_id=?", (tg,)).fetchone())
    c.close()
    return j({"status": "ok", **fresh})


_TEXT_SETTINGS = {"bets_paused_reason"}


async def api_admin_settings(request):
    u = require_active_user(request)
    try:
        me = _require_admin(u)
    except PermissionError as e:
        return err(str(e), 403)
    if request.method == "POST":
        body = await request.json()
        changes, bad = {}, []
        for key, value in (body.get("settings") or {}).items():
            if key in ("bets_paused", "bets_paused_reason"):
                continue  # только через /api/admin/pause
            if key not in appsettings.DEFAULTS:
                bad.append(f"{key}: неизвестная настройка")
                continue
            value = str(value).strip()
            if key not in _TEXT_SETTINGS:
                try:
                    if float(value) < 0:
                        raise ValueError
                except ValueError:
                    bad.append(f"{key}: нужно неотрицательное число")
                    continue
            changes[key] = value
        if bad:
            return err("; ".join(bad))
        for key, value in changes.items():
            appsettings.set_setting(key, value)
        if changes:
            _audit_now(me["telegram_id"], "settings", json.dumps(changes, ensure_ascii=False))
    from .helpers import load_settings_map
    return j({"settings": {**appsettings.DEFAULTS, **load_settings_map()}})


async def api_admin_exposure(request):
    u = require_active_user(request)
    try:
        _require_admin(u)
    except PermissionError as e:
        return err(str(e), 403)
    c = appdb.db()
    rows = [dict(r) for r in c.execute(
        "SELECT m.id AS match_id, m.status, clh.name AS home, cla.name AS away, "
        "COALESCE(SUM(b.potential_win),0) AS exposure, COUNT(b.id) AS open_bets "
        "FROM matches m "
        "LEFT JOIN clubs clh ON clh.id=m.home_club_id LEFT JOIN clubs cla ON cla.id=m.away_club_id "
        "LEFT JOIN bet_legs bl ON bl.match_id=m.id LEFT JOIN bets b ON b.id=bl.bet_id AND b.status='open' "
        "GROUP BY m.id HAVING open_bets > 0 ORDER BY exposure DESC LIMIT 50").fetchall()]
    c.close()
    return j({"exposure": rows})


async def api_admin_audit(request):
    u = require_active_user(request)
    try:
        _require_admin(u)
    except PermissionError as e:
        return err(str(e), 403)
    c = appdb.db()
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM tournament_audit_log ORDER BY id DESC LIMIT 100").fetchall()]
    c.close()
    return j({"audit": rows})


async def api_admin_recalc(request):
    u = require_active_user(request)
    try:
        me = _require_admin(u)
    except PermissionError as e:
        return err(str(e), 403)
    body = await request.json()
    mid = int(body.get("match_id", 0))
    c = appdb.db()
    m = c.execute("SELECT status, score1 FROM matches WHERE id=?", (mid,)).fetchone()
    c.close()
    if not m or m["score1"] is None:
        return err("Матч не финализирован")
    r = bets_engine.settle_match(mid)
    _audit_now(me["telegram_id"], "recalc", f"match={mid} settled={r.get('settled')}")
    return j({"status": "ok", **r})


async def api_admin_markets_refresh(request):
    """markets+action (контракт admin.js): перегенерация кэфов тура/матча по Elo."""
    u = require_active_user(request)
    try:
        me = _require_admin(u)
    except PermissionError as e:
        return err(str(e), 403)
    body = await request.json()
    mid = body.get("match_id")
    tid = body.get("tournament_id")
    if mid:
        n = markets_engine.generate_markets(int(mid))
    elif tid:
        n = markets_engine.refresh_tour(int(tid), body.get("tour"))
    else:
        return err("match_id или tournament_id")
    _audit_now(me["telegram_id"], "markets_refresh", f"match={mid} tournament={tid} markets={n}")
    return j({"status": "ok", "markets": n})


# ===== финал сезона (решение 09: кнопка админа с подтверждением) =====

async def api_admin_season_finalize(request):
    u = require_active_user(request)
    try:
        me = _require_admin(u)
    except PermissionError as e:
        return err(str(e), 403)
    body = await request.json()
    tid = int(body.get("tournament_id", 0))
    if not body.get("confirm"):
        return j({"status": "preview", **league.season_final_preview(tid)})
    r = league.finalize_season(tid, me["telegram_id"])
    if r.get("error"):
        return err(r["error"], 400)
    return j({"status": "finalized", **r})


# ===== свои OCR-провайдеры (только root: ключи API) =====

import asyncio  # noqa: E402

import ocr  # noqa: E402
import ocr_providers  # noqa: E402


def _require_root(request) -> dict:
    u = require_active_user(request)
    me = _user_row(u)
    if me["telegram_id"] not in config.ADMIN_IDS:
        raise PermissionError("Только root (ADMIN_IDS)")
    return me


def _builtin_status() -> list[dict]:
    """Встроенные провайдеры каскада и есть ли у них ключ — для экрана админки."""
    import shutil
    return [
        {"name": "Gemini", "configured": bool(config.GEMINI_API_KEY), "hint": "GEMINI_API_KEY"},
        {"name": "OpenRouter :free", "configured": bool(config.OPENROUTER_API_KEY), "hint": "OPENROUTER_API_KEY"},
        {"name": "NVIDIA NIM", "configured": bool(config.NIM_API_KEY), "hint": "NIM_API_KEY"},
        {"name": "Ollama (локально)", "configured": bool(config.OLLAMA_URL), "hint": "OLLAMA_URL"},
        {"name": "OCR.space", "configured": bool(config.OCRSPACE_API_KEY), "hint": "OCRSPACE_API_KEY"},
        {"name": "tesseract", "configured": bool(shutil.which("tesseract")), "hint": "apt install tesseract-ocr"},
    ]


async def api_admin_ocr_providers(request):
    try:
        me = _require_root(request)
    except PermissionError as e:
        return err(str(e), 403)
    if request.method == "POST":
        body = await request.json()
        try:
            prov = ocr_providers.create_provider(body.get("name"), body.get("base_url"), body.get("model"),
                                                 body.get("api_key"), body.get("position", "first"))
        except ocr_providers.ProviderError as e:
            return err(str(e))
        _audit_now(me["telegram_id"], "ocr_provider_add", f"{prov['name']} {prov['base_url']} {prov['model']}")
        return j({"status": "ok", "provider": prov})
    return j({"providers": ocr_providers.list_providers(),
              "builtin": _builtin_status(),
              "cascade": [n for n, _ in ocr.build_cascade()]})


async def api_admin_ocr_provider(request):
    try:
        me = _require_root(request)
    except PermissionError as e:
        return err(str(e), 403)
    try:
        pid = int(request.match_info["pid"])
    except ValueError:
        return err("id провайдера — число")
    if not ocr_providers.get_provider(pid):
        return err("Провайдер не найден", 404)
    if request.method == "DELETE":
        ocr_providers.delete_provider(pid)
        _audit_now(me["telegram_id"], "ocr_provider_delete", f"id={pid}")
        return j({"status": "ok"})
    body = await request.json()
    try:
        prov = ocr_providers.update_provider(
            pid, name=body.get("name"), base_url=body.get("base_url"), model=body.get("model"),
            api_key=body.get("api_key"), enabled=body.get("enabled"), position=body.get("position"))
    except ocr_providers.ProviderError as e:
        return err(str(e))
    _audit_now(me["telegram_id"], "ocr_provider_update", f"id={pid} fields={sorted(k for k, v in body.items() if v is not None and k != 'api_key')}")
    return j({"status": "ok", "provider": prov})


async def api_admin_ocr_provider_test(request):
    try:
        _require_root(request)
    except PermissionError as e:
        return err(str(e), 403)
    try:
        row = ocr_providers.get_provider(int(request.match_info["pid"]))
    except ValueError:
        row = None
    if not row:
        return err("Провайдер не найден", 404)
    r = await asyncio.to_thread(ocr.test_provider, row)
    ocr_providers.save_test_result(row["id"], r["text"])
    return j({"status": "ok", **r})


def _setup_ocr_routes(app: web.Application) -> None:
    r = app.router
    r.add_get("/api/admin/ocr-providers", api_admin_ocr_providers)
    r.add_post("/api/admin/ocr-providers", api_admin_ocr_providers)
    r.add_post("/api/admin/ocr-providers/{pid}", api_admin_ocr_provider)
    r.add_delete("/api/admin/ocr-providers/{pid}", api_admin_ocr_provider)
    r.add_post("/api/admin/ocr-providers/{pid}/test", api_admin_ocr_provider_test)
