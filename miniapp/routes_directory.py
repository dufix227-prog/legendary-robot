"""API справочника реальных игроков: поиск (автокомплит карточек) и импорт (root)."""
import asyncio

from aiohttp import web

from .helpers import require_active_user
from .routes import _audit_now, err, j  # routes.py кладёт bot/ в sys.path

import config  # noqa: E402
import players_directory as pd  # noqa: E402


async def api_search(request):
    require_active_user(request)
    q = request.rel_url.query.get("q", "")[:60]
    return j({"status": "ok", "players": pd.search(q)})


async def api_status(request):
    require_active_user(request)
    return j({"status": "ok", **pd.status()})


async def api_import(request):
    u = require_active_user(request)
    if u["telegram_id"] not in config.ADMIN_IDS:
        return err("Только root", 403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    snap = str((body or {}).get("snapshot") or "").strip() or None
    try:
        r = await asyncio.to_thread(pd.import_players, None, snap)
    except pd.DirectoryError as e:
        return err(str(e), 400)
    except Exception as e:
        return err(f"Импорт не удался: {e}", 502)
    _audit_now(u["telegram_id"], "directory_import", f"{r['count']} игроков, снимок {r['snapshot']}")
    return j({"status": "ok", **r})


def setup(app: web.Application) -> None:
    r = app.router
    r.add_get("/api/directory/search", api_search)
    r.add_get("/api/directory/status", api_status)
    r.add_post("/api/admin/directory/import", api_import)
