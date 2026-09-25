"""API клубов: оформление, стадион, спонсор, доходы; время матча."""
from aiohttp import web

from .helpers import require_active_user
from .routes import err, j  # routes.py кладёт bot/ в sys.path

import club_economy as ce  # noqa: E402
import db as appdb  # noqa: E402


async def _body(request) -> dict:
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _cid(request, key="club_id") -> int:
    try:
        return int(request.match_info[key])
    except (TypeError, ValueError):
        raise web.HTTPNotFound()


def _club_err(e: ce.ClubError):
    status = {"FORBIDDEN": 403, "NO_CLUB": 404, "NO_MATCH": 404}.get(e.code, 400)
    return err(e.message, status, e.code)


async def api_economy(request):
    u = require_active_user(request)
    try:
        return j({"status": "ok", **ce.overview(_cid(request), u["telegram_id"])})
    except ce.ClubError as e:
        return _club_err(e)


async def api_profile(request):
    u = require_active_user(request)
    b = await _body(request)
    try:
        return j({"status": "ok", **ce.update_profile(u["telegram_id"], _cid(request), b)})
    except ce.ClubError as e:
        return _club_err(e)


async def api_stadium_upgrade(request):
    u = require_active_user(request)
    try:
        return j({"status": "ok", **ce.upgrade_stadium(u["telegram_id"], _cid(request))})
    except ce.ClubError as e:
        return _club_err(e)


async def api_sponsor(request):
    u = require_active_user(request)
    b = await _body(request)
    try:
        return j({"status": "ok", **ce.sign_sponsor(u["telegram_id"], _cid(request), str(b.get("code") or ""))})
    except ce.ClubError as e:
        return _club_err(e)


async def api_match_schedule(request):
    u = require_active_user(request)
    mid = _cid(request, "match_id")
    if request.method == "POST":
        b = await _body(request)
        try:
            return j({"status": "ok", **ce.set_match_time(u["telegram_id"], mid, b.get("scheduled_at"))})
        except ce.ClubError as e:
            return _club_err(e)
    c = appdb.db()
    try:
        row = c.execute("SELECT * FROM matches WHERE id=?", (mid,)).fetchone()
        if not row:
            return err("Матч не найден", 404)
        m = dict(row)
        return j({"status": "ok", "match_id": mid, "scheduled_at": m.get("scheduled_at"),
                  "can_edit": m["status"] == "pending" and ce.can_schedule(c, u["telegram_id"], m)})
    finally:
        c.close()


def setup(app: web.Application) -> None:
    r = app.router
    r.add_get("/api/clubs/{club_id}/economy", api_economy)
    r.add_post("/api/clubs/{club_id}/profile", api_profile)
    r.add_post("/api/clubs/{club_id}/stadium/upgrade", api_stadium_upgrade)
    r.add_post("/api/clubs/{club_id}/sponsor", api_sponsor)
    r.add_get("/api/matches/{match_id}/schedule", api_match_schedule)
    r.add_post("/api/matches/{match_id}/schedule", api_match_schedule)
