"""Клубы: оформление (цвета, эмблема, девиз, стадион), доходы и время матча.

Доходы идут в clubs.budget и пишутся в club_ledger:
- стадион: каждый подтверждённый домашний матч → база × уровень стадиона × итог (W/D/L);
- спонсор: контракт на сезон (лигу), бонус за подпись + выплаты за матч/победу/ничью.
Пересчёт результата (спор) откатывает доходы матча и начисляет заново — без задвоения.
"""
import re
from datetime import datetime, timedelta, timezone

import config
import db as appdb
import settings as appsettings

STADIUM_LEVELS = {  # уровень → (вместимость, множитель дохода, цена апгрейда до уровня)
    1: (8_000, 1.0, 0),
    2: (15_000, 1.6, 8_000_000),
    3: (25_000, 2.4, 16_000_000),
    4: (40_000, 3.4, 30_000_000),
    5: (60_000, 4.6, 50_000_000),
}
RESULT_FACTOR = {"W": 1.25, "D": 1.0, "L": 0.8}

SPONSORS = {
    "reliable": {"name": "Табачная лавка", "tagline": "Стабильно: платит за каждый матч",
                 "sign": 2_000_000, "match": 600_000, "win": 0, "draw": 0},
    # ожидание за 10 матчей (40% побед / 25% ничьих): ~8 / ~8.2 / ~9 млн — риск оплачивается
    "balanced": {"name": "Сигарный дом", "tagline": "Поровну: за матч и за результат",
                 "sign": 1_000_000, "match": 300_000, "win": 900_000, "draw": 250_000},
    "risky": {"name": "Кальянная №1", "tagline": "Ва-банк: платит только за результат",
              "sign": 0, "match": 0, "win": 2_000_000, "draw": 400_000},
}

_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class ClubError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ===== права =====

def _player_id(c, tg: int) -> int | None:
    r = c.execute("SELECT id FROM players WHERE telegram_id=?", (tg,)).fetchone()
    return r["id"] if r else None


def _is_admin(c, tg: int) -> bool:
    if tg in config.ADMIN_IDS:
        return True
    r = c.execute("SELECT is_admin FROM users WHERE telegram_id=?", (tg,)).fetchone()
    return bool(r and r["is_admin"])


def can_manage(c, tg: int, club_id: int) -> bool:
    """Владелец клуба или админ."""
    if _is_admin(c, tg):
        return True
    pid = _player_id(c, tg)
    if pid is None:
        return False
    r = c.execute("SELECT 1 FROM club_players WHERE player_id=? AND club_id=? AND role='owner'",
                  (pid, club_id)).fetchone()
    if r:
        return True
    r = c.execute("SELECT 1 FROM clubs WHERE id=? AND owner_player_id=?", (club_id, pid)).fetchone()
    return bool(r)


def _club(c, club_id: int) -> dict:
    r = c.execute("SELECT * FROM clubs WHERE id=?", (club_id,)).fetchone()
    if not r:
        raise ClubError("NO_CLUB", "Клуб не найден")
    return dict(r)


# ===== оформление =====

def _clean_text(v, limit: int) -> str | None:
    v = re.sub(r"\s+", " ", str(v or "")).strip()
    if any(ch in v for ch in "<>"):
        raise ClubError("BAD_TEXT", "Без символов < и >")
    return v[:limit] or None


def update_profile(tg: int, club_id: int, data: dict) -> dict:
    c = appdb.db()
    try:
        _club(c, club_id)
        if not can_manage(c, tg, club_id):
            raise ClubError("FORBIDDEN", "Оформление меняет владелец клуба или админ")
        sets, args = [], []
        for key in ("color1", "color2"):
            if key in data:
                v = (data[key] or "").strip() or None
                if v and not _COLOR_RE.match(v):
                    raise ClubError("BAD_COLOR", "Цвет в формате #RRGGBB")
                sets.append(f"{key}=?")
                args.append(v.lower() if v else None)
        if "emblem" in data:
            v = _clean_text(data["emblem"], 16)
            if v and (len(v) > 8 or any(ch.isalnum() and ch.isascii() for ch in v)):
                raise ClubError("BAD_EMBLEM", "Эмблема — 1–2 эмодзи")
            sets.append("emblem=?")
            args.append(v)
        for key, limit in (("motto", 60), ("stadium_name", 40)):
            if key in data:
                sets.append(f"{key}=?")
                args.append(_clean_text(data[key], limit))
        if sets:
            c.execute(f"UPDATE clubs SET {', '.join(sets)} WHERE id=?", (*args, club_id))
            c.commit()
        return overview(club_id, tg, c)
    finally:
        c.close()


def public_fields(club: dict) -> dict:
    """Поля оформления для _club_public (таблицы, линия, карточка матча)."""
    return {"color1": club.get("color1"), "color2": club.get("color2"), "emblem": club.get("emblem"),
            "stadium_name": club.get("stadium_name")}


# ===== стадион и спонсоры =====

def _income_on() -> bool:
    return appsettings.setting_int("club_income_enabled", 1) == 1


def stadium_income(level: int, result: str) -> int:
    base = appsettings.setting_int("stadium_income_base", 1_000_000)
    _, mult, _ = STADIUM_LEVELS.get(level, STADIUM_LEVELS[1])
    return int(base * mult * RESULT_FACTOR[result])


def _sponsor_amount(code: str, part: str) -> int:
    scale = appsettings.setting_float("sponsor_scale_pct", 100) / 100.0
    return int(SPONSORS[code][part] * scale)


def _current_league(c, club: dict) -> int | None:
    """Сезон клуба = лига его дивизиона (не завершённая)."""
    if not club.get("division_id"):
        return None
    r = c.execute("SELECT t.id FROM divisions d JOIN tournaments t ON t.id=d.tournament_id "
                  "WHERE d.id=? AND t.stage!='finished'", (club["division_id"],)).fetchone()
    return r["id"] if r else None


def _contract(c, club_id: int, tournament_id: int | None) -> dict | None:
    """Контракт на турнир матча; для кубков — последний контракт клуба в незавершённой лиге."""
    if tournament_id is not None:
        r = c.execute("SELECT * FROM club_sponsors WHERE club_id=? AND tournament_id=?",
                      (club_id, tournament_id)).fetchone()
        if r:
            return dict(r)
    r = c.execute("SELECT s.* FROM club_sponsors s JOIN tournaments t ON t.id=s.tournament_id "
                  "WHERE s.club_id=? AND t.stage!='finished' ORDER BY s.id DESC LIMIT 1", (club_id,)).fetchone()
    return dict(r) if r else None


def _ledger(c, club_id, amount, kind, note, match_id=None, tournament_id=None) -> None:
    if not amount:
        return
    c.execute("INSERT INTO club_ledger (club_id, match_id, tournament_id, kind, amount, note) VALUES (?,?,?,?,?,?)",
              (club_id, match_id, tournament_id, kind, amount, note))
    c.execute("UPDATE clubs SET budget=budget+? WHERE id=?", (amount, club_id))


def revert_match_income(c, match_id: int) -> None:
    for r in c.execute("SELECT club_id, amount FROM club_ledger WHERE match_id=? AND kind IN ('stadium','sponsor')",
                       (match_id,)).fetchall():
        c.execute("UPDATE clubs SET budget=budget-? WHERE id=?", (r["amount"], r["club_id"]))
    c.execute("DELETE FROM club_ledger WHERE match_id=? AND kind IN ('stadium','sponsor')", (match_id,))


def apply_match_income(c, match_id: int) -> None:
    """Вызывается из results.finalize_match в его транзакции. Идемпотентно."""
    revert_match_income(c, match_id)
    if not _income_on():
        return
    m = c.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
    if not m or m["status"] != "confirmed" or m["score1"] is None:
        return
    for side, cid, gf, ga in (("home", m["home_club_id"], m["score1"], m["score2"]),
                              ("away", m["away_club_id"], m["score2"], m["score1"])):
        if cid is None:
            continue
        cl = c.execute("SELECT * FROM clubs WHERE id=?", (cid,)).fetchone()
        if not cl:
            continue
        res = "W" if gf > ga else ("D" if gf == ga else "L")
        if side == "home":
            lvl = cl["stadium_level"] or 1
            _ledger(c, cid, stadium_income(lvl, res), "stadium",
                    f"домашний матч {gf}:{ga}, стадион ур. {lvl}", match_id, m["tournament_id"])
        con = _contract(c, cid, m["tournament_id"])
        if con and con["sponsor_code"] in SPONSORS:
            code = con["sponsor_code"]
            amt = _sponsor_amount(code, "match") + (_sponsor_amount(code, "win") if res == "W" else 0) \
                + (_sponsor_amount(code, "draw") if res == "D" else 0)
            _ledger(c, cid, amt, "sponsor", f"{SPONSORS[code]['name']}: матч {gf}:{ga}", match_id, m["tournament_id"])


def upgrade_stadium(tg: int, club_id: int) -> dict:
    c = appdb.db()
    try:
        cl = _club(c, club_id)
        if not can_manage(c, tg, club_id):
            raise ClubError("FORBIDDEN", "Стадион улучшает владелец клуба")
        lvl = cl["stadium_level"] or 1
        if lvl >= max(STADIUM_LEVELS):
            raise ClubError("MAX_LEVEL", "Стадион уже максимального уровня")
        cost = STADIUM_LEVELS[lvl + 1][2]
        cur = c.execute("UPDATE clubs SET stadium_level=?, budget=budget-? WHERE id=? AND budget>=? "
                        "AND COALESCE(stadium_level,1)=?", (lvl + 1, cost, club_id, cost, lvl))
        if cur.rowcount != 1:
            raise ClubError("NO_FUNDS", f"Не хватает бюджета: нужно {cost:,}".replace(",", " "))
        c.execute("INSERT INTO club_ledger (club_id, kind, amount, note) VALUES (?,?,?,?)",
                  (club_id, "stadium_upgrade", -cost, f"стадион → ур. {lvl + 1}"))
        c.commit()
        return overview(club_id, tg, c)
    finally:
        c.close()


def sign_sponsor(tg: int, club_id: int, code: str) -> dict:
    if code not in SPONSORS:
        raise ClubError("BAD_SPONSOR", "Нет такого спонсора")
    c = appdb.db()
    try:
        cl = _club(c, club_id)
        if not can_manage(c, tg, club_id):
            raise ClubError("FORBIDDEN", "Спонсора выбирает владелец клуба")
        tid = _current_league(c, cl)
        if tid is None:
            raise ClubError("NO_SEASON", "Клуб не в дивизионе текущей лиги")
        if c.execute("SELECT 1 FROM club_sponsors WHERE club_id=? AND tournament_id=?", (club_id, tid)).fetchone():
            raise ClubError("ALREADY", "Спонсор на этот сезон уже выбран")
        c.execute("INSERT INTO club_sponsors (club_id, tournament_id, sponsor_code) VALUES (?,?,?)",
                  (club_id, tid, code))
        if _income_on():
            _ledger(c, club_id, _sponsor_amount(code, "sign"), "sponsor_sign",
                    f"{SPONSORS[code]['name']}: бонус за подпись", None, tid)
        c.commit()
        return overview(club_id, tg, c)
    finally:
        c.close()


def overview(club_id: int, tg: int | None = None, c=None) -> dict:
    own = c is None
    c = c or appdb.db()
    try:
        cl = _club(c, club_id)
        lvl = cl["stadium_level"] or 1
        cap, mult, _ = STADIUM_LEVELS.get(lvl, STADIUM_LEVELS[1])
        nxt = STADIUM_LEVELS.get(lvl + 1)
        tid = _current_league(c, cl)
        con = _contract(c, club_id, tid) if tid else None
        ledger = [dict(r) for r in c.execute(
            "SELECT kind, amount, note, match_id, created_at FROM club_ledger WHERE club_id=? ORDER BY id DESC LIMIT 15",
            (club_id,)).fetchall()]
        totals = {r["kind"]: int(r["s"]) for r in c.execute(
            "SELECT kind, SUM(amount) s FROM club_ledger WHERE club_id=? GROUP BY kind", (club_id,)).fetchall()}
        sponsors = [{"code": k, **{x: (_sponsor_amount(k, x) if x in ("sign", "match", "win", "draw") else v)
                                   for x, v in s.items()}} for k, s in SPONSORS.items()]
        return {
            "club_id": club_id, "name": cl["name"], "budget": cl["budget"],
            "can_manage": bool(tg is not None and can_manage(c, tg, club_id)),
            "income_enabled": _income_on(),
            "profile": {"color1": cl.get("color1"), "color2": cl.get("color2"), "emblem": cl.get("emblem"),
                        "motto": cl.get("motto"), "stadium_name": cl.get("stadium_name")},
            "stadium": {"level": lvl, "max_level": max(STADIUM_LEVELS), "capacity": cap,
                        "income": {r: stadium_income(lvl, r) for r in ("W", "D", "L")},
                        "next": ({"level": lvl + 1, "capacity": nxt[0], "cost": nxt[2],
                                  "income_draw": stadium_income(lvl + 1, "D")} if nxt else None)},
            "season_id": tid,
            "sponsor": ({"code": con["sponsor_code"], **SPONSORS.get(con["sponsor_code"], {}),
                         "signed_at": con["signed_at"]} if con else None),
            "sponsors": sponsors,
            "ledger": ledger,
            "totals": {"stadium": totals.get("stadium", 0),
                       "sponsor": totals.get("sponsor", 0) + totals.get("sponsor_sign", 0),
                       "upgrades": totals.get("stadium_upgrade", 0)},
        }
    finally:
        if own:
            c.close()


# ===== время матча =====

def parse_time(raw) -> str | None:
    """ISO (из браузера, UTC) или «ГГГГ-ММ-ДД ЧЧ:ММ» (UTC) → 'ГГГГ-ММ-ДД ЧЧ:ММ:00'; пусто → None."""
    s = str(raw or "").strip()
    if not s:
        return None
    s = s.replace("T", " ").rstrip("Z")
    s = re.sub(r"\.\d+$", "", s)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d %H:%M:00")
        except ValueError:
            continue
    raise ClubError("BAD_TIME", "Время в формате ГГГГ-ММ-ДД ЧЧ:ММ")


def can_schedule(c, tg: int, m: dict) -> bool:
    if _is_admin(c, tg):
        return True
    if c.execute("SELECT 1 FROM tournament_admins WHERE tournament_id=? AND telegram_id=?",
                 (m["tournament_id"], tg)).fetchone():
        return True
    pid = _player_id(c, tg)
    if pid is None:
        return False
    return bool(c.execute("SELECT 1 FROM club_players WHERE player_id=? AND club_id IN (?,?)",
                          (pid, m["home_club_id"], m["away_club_id"])).fetchone())


def set_match_time(tg: int, match_id: int, raw) -> dict:
    when = parse_time(raw)
    c = appdb.db()
    try:
        r = c.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
        if not r:
            raise ClubError("NO_MATCH", "Матч не найден")
        m = dict(r)
        if not can_schedule(c, tg, m):
            raise ClubError("FORBIDDEN", "Время матча ставят участники, судья или админ")
        if m["status"] != "pending":
            raise ClubError("PLAYED", "Матч уже сыгран")
        if when:
            dt = datetime.strptime(when, "%Y-%m-%d %H:%M:%S")
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            if dt < now - timedelta(hours=1) or dt > now + timedelta(days=60):
                raise ClubError("BAD_TIME", "Время — от текущего момента до 60 дней вперёд")
        c.execute("UPDATE matches SET scheduled_at=? WHERE id=?", (when, match_id))
        # уведомить участников второй стороны (и первой, если ставил судья)
        pid = _player_id(c, tg)
        names = {}
        for cid in (m["home_club_id"], m["away_club_id"]):
            cl = c.execute("SELECT name FROM clubs WHERE id=?", (cid,)).fetchone()
            names[cid] = cl["name"] if cl else "—"
        text = (f"🕒 Время матча {names[m['home_club_id']]} — {names[m['away_club_id']]}: "
                + (f"{when[:16]} UTC" if when else "снято"))
        for u in c.execute(
                "SELECT u.id, p.id AS pid FROM users u JOIN players p ON p.telegram_id=u.telegram_id "
                "JOIN club_players cp ON cp.player_id=p.id WHERE cp.club_id IN (?,?)",
                (m["home_club_id"], m["away_club_id"])).fetchall():
            if u["pid"] == pid:
                continue
            c.execute("INSERT INTO notifications (user_id, text, kind) VALUES (?,?, 'app')", (u["id"], text))
        c.execute("INSERT INTO tournament_audit_log (tournament_id, actor_telegram_id, action, details) "
                  "VALUES (?,?, 'match_time', ?)", (m["tournament_id"], tg, f"match={match_id} at={when}"))
        c.commit()
        return {"match_id": match_id, "scheduled_at": when}
    finally:
        c.close()
