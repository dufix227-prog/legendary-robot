"""API функций оригинала: бомбардиры, события и скрин матча, публичный профиль, «Мои матчи»,
горячие матчи, движение кэфов, награды капперам, предупреждения о рисках (админ)."""
from aiohttp import web

from .helpers import require_active_user
from .routes import _user_row, err, j  # routes.py кладёт bot/ в sys.path

import db as appdb  # noqa: E402
import league_stats as ls  # noqa: E402


def _int(v, name="id"):
    try:
        return int(v)
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text=f'{{"status":"error","error":"bad {name}"}}', content_type="application/json")


async def api_top_scorers(request):
    require_active_user(request)
    div = request.rel_url.query.get("division_id")
    return j({"status": "ok", **ls.top_scorers(_int(request.match_info["tid"]), _int(div, "division_id") if div else None)})


async def api_events(request):
    require_active_user(request)
    try:
        return j({"status": "ok", **ls.match_events(_int(request.match_info["match_id"]))})
    except LookupError as e:
        return err(str(e), 404)


async def api_photo(request):
    # фото отдаём только авторизованным (фронт грузит через fetch с initData → blob)
    require_active_user(request)
    c = appdb.db()
    row = c.execute("SELECT * FROM matches WHERE id=?", (_int(request.match_info["match_id"]),)).fetchone()
    c.close()
    f = ls.screenshot_file(dict(row)) if row else None
    if not f:
        return err("Скрина нет", 404)
    return web.FileResponse(f, headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"})


async def api_public_profile(request):
    require_active_user(request)
    try:
        return j({"status": "ok", "player": ls.public_profile(_int(request.match_info["tg"]))})
    except LookupError as e:
        return err(str(e), 404)


async def api_my_matches(request):
    u = require_active_user(request)
    return j({"status": "ok", **ls.my_matches(u["telegram_id"])})


async def api_hot(request):
    require_active_user(request)
    return j({"status": "ok", "matches": ls.hot_matches(min(_int(request.rel_url.query.get("limit", "6"), "limit"), 20))})


async def api_movers(request):
    require_active_user(request)
    return j({"status": "ok", "movers": ls.odds_movers(min(_int(request.rel_url.query.get("limit", "10"), "limit"), 30))})


async def api_season_awards(request):
    require_active_user(request)
    tid = request.rel_url.query.get("tournament_id")
    c = appdb.db()
    sql = ("SELECT a.*, u.telegram_id AS user_tg, COALESCE(u.username, u.first_name) AS name, d.name AS division, "
           "t.name AS season FROM bettor_awards a JOIN users u ON u.id=a.user_id "
           "LEFT JOIN divisions d ON d.id=a.division_id LEFT JOIN tournaments t ON t.id=a.tournament_id")
    args = []
    if tid:
        sql += " WHERE a.tournament_id=?"
        args.append(_int(tid, "tournament_id"))
    rows = [dict(r) for r in c.execute(sql + " ORDER BY a.tournament_id DESC, d.sort_order, a.place LIMIT 200", args).fetchall()]
    c.close()
    for r in rows:
        r.pop("user_id", None)
        r["title"] = ls.AWARD_TITLES.get(r["award"], r["award"])
    return j({"status": "ok", "awards": rows})


def _admin(request) -> dict:
    me = _user_row(require_active_user(request))
    if not me["is_admin"]:
        raise web.HTTPForbidden(text='{"status":"error","error":"Только админ"}', content_type="application/json")
    return me


async def api_risk_alerts(request):
    _admin(request)
    if request.rel_url.query.get("scan") == "1":
        ls.risk_scan()
    status = "ack" if request.rel_url.query.get("status") == "ack" else "open"
    return j({"status": "ok", "alerts": ls.risk_alerts(status)})


async def api_risk_ack(request):
    me = _admin(request)
    if not ls.ack_alert(_int(request.match_info["aid"]), me["telegram_id"]):
        return err("Предупреждение не найдено или уже принято", 404)
    return j({"status": "ok", "alerts": ls.risk_alerts("open")})


def setup(app: web.Application) -> None:
    r = app.router
    r.add_get("/api/tournaments/{tid}/top-scorers", api_top_scorers)
    r.add_get("/api/matches/{match_id}/events", api_events)
    r.add_get("/api/matches/{match_id}/photo", api_photo)
    r.add_get("/api/player/{tg}/public", api_public_profile)
    r.add_get("/api/cabinet/matches", api_my_matches)
    r.add_get("/api/matches-hot", api_hot)
    r.add_get("/api/odds/movers", api_movers)
    r.add_get("/api/season/awards", api_season_awards)
    r.add_get("/api/admin/risk/alerts", api_risk_alerts)
    r.add_post("/api/admin/risk/alerts/{aid}/ack", api_risk_ack)
