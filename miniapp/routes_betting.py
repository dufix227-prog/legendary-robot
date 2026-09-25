"""API ставок: кэшаут, черновики купонов, статистика каппера, рейтинги, аналитика матча, value."""
from aiohttp import web

from .helpers import require_active_user
from .routes import _user_row, err, j  # routes.py кладёт bot/ в sys.path

import bet_tools  # noqa: E402
import db as appdb  # noqa: E402
import match_insights  # noqa: E402
from bets_engine import BetError  # noqa: E402


async def _body(request) -> dict:
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _bet_err(e: BetError):
    status = 404 if e.code in ("NO_BET", "NO_DRAFT", "NO_DIVISION") else 400
    return web.json_response({"status": "error", "error": str(e), "code": e.code, **e.extra}, status=status)


def _int(v, name="id"):
    try:
        return int(v)
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text=f'{{"status":"error","error":"bad {name}"}}', content_type="application/json")


# ===== кэшаут =====

async def api_cashout_quote(request):
    me = _user_row(require_active_user(request))
    try:
        return j({"status": "ok", **bet_tools.cashout_quote(me, _int(request.match_info["bet_id"]))})
    except BetError as e:
        return _bet_err(e)


async def api_cashout(request):
    me = _user_row(require_active_user(request))
    b = await _body(request)
    try:
        r = bet_tools.cashout(me, _int(request.match_info["bet_id"]), b.get("amount"))
    except BetError as e:
        return _bet_err(e)
    return j({"status": "ok", **r})


async def api_cashout_quotes(request):
    me = _user_row(require_active_user(request))
    return j({"status": "ok", "quotes": {str(k): v for k, v in bet_tools.cashout_quotes(me).items()}})


# ===== черновики =====

async def api_saved_coupons(request):
    me = _user_row(require_active_user(request))
    if request.method == "POST":
        b = await _body(request)
        try:
            d = bet_tools.save_draft(me, b.get("legs"), b.get("amount"), b.get("name"))
        except BetError as e:
            return _bet_err(e)
        return j({"status": "ok", "coupon": d})
    return j({"status": "ok", "saved_coupons": bet_tools.list_drafts(me)})


async def api_saved_coupon_delete(request):
    me = _user_row(require_active_user(request))
    try:
        bet_tools.delete_draft(me, _int(request.match_info["cid"]))
    except BetError as e:
        return _bet_err(e)
    return j({"status": "ok"})


# ===== статистика и рейтинги =====

async def api_bet_stats(request):
    me = _user_row(require_active_user(request))
    return j({"status": "ok", **bet_tools.bet_stats(me)})


async def api_leaderboard_scopes(request):
    require_active_user(request)
    c = appdb.db()
    seasons = [dict(r) for r in c.execute(
        "SELECT id, name, stage FROM tournaments WHERE format='league' ORDER BY id DESC").fetchall()]
    divisions = [dict(r) for r in c.execute(
        "SELECT d.id, d.name, d.tournament_id, t.name AS tournament_name FROM divisions d "
        "JOIN tournaments t ON t.id=d.tournament_id WHERE d.is_active=1 ORDER BY t.id DESC, d.sort_order, d.id"
    ).fetchall()]
    c.close()
    return j({"status": "ok", "seasons": seasons, "divisions": divisions})


def _leaderboard(scope: str, param: str):
    async def handler(request):
        me = _user_row(require_active_user(request))
        sid = request.rel_url.query.get(param)
        if not sid:
            return err(f"Нужен {param}", 400)
        try:
            return j({"status": "ok", **bet_tools.leaderboard(scope, _int(sid, param), viewer_id=me["id"])})
        except BetError as e:
            return _bet_err(e)
    return handler


# ===== аналитика матча =====

async def api_match_insights(request):
    require_active_user(request)
    try:
        return j({"status": "ok", **match_insights.insights(_int(request.match_info["match_id"]))})
    except LookupError as e:
        return err(str(e), 404)


async def api_odds_explain(request):
    require_active_user(request)
    try:
        return j({"status": "ok", **match_insights.explain(_int(request.match_info["match_id"]))})
    except LookupError as e:
        return err(str(e), 404)


async def api_value(request):
    require_active_user(request)
    picks = match_insights.value_radar(limit=min(_int(request.rel_url.query.get("limit", "30"), "limit"), 100))
    return j({"status": "ok", "count": len(picks), "picks": picks})


def setup(app: web.Application) -> None:
    r = app.router
    r.add_get("/api/predictions/{bet_id}/cashout-quote", api_cashout_quote)
    r.add_post("/api/predictions/{bet_id}/cashout", api_cashout)
    r.add_get("/api/cashout/quotes", api_cashout_quotes)
    r.add_get("/api/saved-coupons", api_saved_coupons)
    r.add_post("/api/saved-coupons", api_saved_coupons)
    r.add_delete("/api/saved-coupons/{cid}", api_saved_coupon_delete)
    r.add_get("/api/profile/bet-stats", api_bet_stats)
    r.add_get("/api/leaderboard/scopes", api_leaderboard_scopes)
    r.add_get("/api/leaderboard/season", _leaderboard("season", "tournament_id"))
    r.add_get("/api/leaderboard/division", _leaderboard("division", "division_id"))
    r.add_get("/api/matches/{match_id}/insights", api_match_insights)
    r.add_get("/api/matches/{match_id}/odds-explain", api_odds_explain)
    r.add_get("/api/intelligence/value", api_value)
