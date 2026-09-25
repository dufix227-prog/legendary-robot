"""Турнирное ядро: календарь round-robin, кубковые сетки, таблицы.

Чистая логика без telegram — вызывается и ботом, и мини-аппом (блок 6).
"""
import random
import sqlite3
import logging
import threading
from datetime import datetime, timedelta

import db as appdb

log = logging.getLogger("bot.league")


# ===== вспомогательные =====

def get_tournament(tournament_id: int) -> dict | None:
    c = appdb.db()
    row = c.execute("SELECT * FROM tournaments WHERE id=?", (tournament_id,)).fetchone()
    c.close()
    return dict(row) if row else None


def get_club(club_id: int) -> dict | None:
    c = appdb.db()
    row = c.execute("SELECT * FROM clubs WHERE id=?", (club_id,)).fetchone()
    c.close()
    return dict(row) if row else None


def tournament_divisions(tournament_id: int) -> list[dict]:
    c = appdb.db()
    rows = c.execute(
        "SELECT * FROM divisions WHERE tournament_id=? AND is_active=1 ORDER BY sort_order",
        (tournament_id,),
    ).fetchall()
    c.close()
    return [dict(r) for r in rows]


def division_clubs(division_id: int) -> list[dict]:
    c = appdb.db()
    rows = c.execute(
        "SELECT * FROM clubs WHERE division_id=? ORDER BY id", (division_id,)
    ).fetchall()
    c.close()
    return [dict(r) for r in rows]


def all_active_tournaments() -> list[dict]:
    c = appdb.db()
    rows = c.execute("SELECT * FROM tournaments WHERE stage!='finished' ORDER BY id").fetchall()
    c.close()
    return [dict(r) for r in rows]


# ===== сезоны / дивизионы =====

def create_league_season(name: str, divisions: int | list[str] = 1, tour_mode: str = "manual",
                         rounds: int = 2, tour_days: int = 3, n_divisions: int | None = None) -> int:
    """Лига = контейнер, внутри 1..N дивизионов (решение 09). divisions — число
    («Дивизион 1…N») или список имён («Ла Лига», «Серия А»); порядок = иерархия.
    n_divisions — старое имя параметра (совместимость)."""
    if n_divisions is not None:
        divisions = n_divisions
    names = ([f"Дивизион {i}" for i in range(1, int(divisions) + 1)]
             if isinstance(divisions, int) else list(divisions))
    c = appdb.db()
    tid = c.insert_returning_id(
        "INSERT INTO tournaments (name, format, tour_mode, rounds, tour_days, total_tours, current_tour, stage) "
        "VALUES (?, 'league', ?, ?, ?, 0, 0, 'groups')",
        (name, tour_mode, int(rounds), int(tour_days)),
    )
    for i, dname in enumerate(names, start=1):
        c.execute(
            "INSERT INTO divisions (tournament_id, name, code, sort_order) VALUES (?,?,?,?)",
            (tid, dname, f"D{i}", i),
        )
    c.commit()
    c.close()
    return tid


def create_cup(name: str, fmt: str, tour_mode: str = "manual", tour_days: int = 3) -> int:
    """Кубок ЛЧ/ЛЕ/ЛК — отдельный турнир, сетка генерится жеребьёвкой."""
    c = appdb.db()
    tid = c.insert_returning_id(
        "INSERT INTO tournaments (name, format, tour_mode, rounds, tour_days, total_tours, current_tour, stage) "
        "VALUES (?, ?, ?, 1, ?, 0, 0, 'playoff')",
        (name, fmt, tour_mode, int(tour_days)),
    )
    c.commit()
    c.close()
    return tid


def create_club(name: str, logo_path: str | None, division_id: int | None, budget: int) -> int:
    c = appdb.db()
    cid = c.insert_returning_id(
        "INSERT INTO clubs (name, logo_path, division_id, budget, elo) VALUES (?,?,?,?,1000)",
        (name, logo_path, division_id, budget),
    )
    c.commit()
    c.close()
    return cid


def assign_club_owner(club_id: int, player_id: int, role: str = "owner") -> None:
    """Один клуб на человека (UNIQUE player_id в club_players)."""
    c = appdb.db()
    c.execute(
        "INSERT INTO club_players (player_id, club_id, role) VALUES (?,?,?) "
        "ON CONFLICT(player_id) DO UPDATE SET club_id=excluded.club_id, role=excluded.role",
        (player_id, club_id, role),
    )
    c.execute("UPDATE clubs SET owner_player_id=? WHERE id=?", (player_id, club_id))
    c.commit()
    c.close()


# ===== календарь лиги =====

def round_robin(team_ids: list[int]) -> list[list[tuple[int, int]]]:
    """Круговой турнир методом «карусели»; None-пары (нечётное) отброшены."""
    ids = list(team_ids)
    if len(ids) < 2:
        return []
    if len(ids) % 2:
        ids.append(None)  # фиктивный «пасс»
    n = len(ids)
    rounds: list[list[tuple[int, int]]] = []
    for r in range(n - 1):
        pairs = []
        for i in range(n // 2):
            a, b = ids[i], ids[n - 1 - i]
            if a is None or b is None:
                continue
            pairs.append((a, b) if (r + i) % 2 == 0 else (b, a))
        rounds.append(pairs)
        ids = [ids[0], ids[-1], *ids[1:-1]]
    return rounds


def generate_league_calendar(tournament_id: int, division_id: int) -> int:
    """Генерит все туры дивизиона: (клубов−1) туров × круги. Тур 1 открыт,
    остальные — locked. Возвращает число матчей."""
    t = get_tournament(tournament_id)
    clubs = division_clubs(division_id)
    if not t or len(clubs) < 2:
        return 0
    ids = [cl["id"] for cl in clubs]
    owners = {cl["id"]: cl["owner_player_id"] for cl in clubs}
    rounds_needed = int(t["rounds"] or 1)
    tour_days = int(t["tour_days"] or 3)

    c = appdb.db()
    # чистим старый календарь дивизиона (перегенерация до открытия тура)
    c.execute(
        "DELETE FROM matches WHERE tournament_id=? AND stage='group' AND ("
        " home_club_id IN (SELECT id FROM clubs WHERE division_id=?) OR"
        " away_club_id IN (SELECT id FROM clubs WHERE division_id=?))",
        (tournament_id, division_id, division_id),
    )
    # туры общие на все дивизионы лиги: не удаляем, а дополняем (иначе второй дивизион
    # стирал туры первого, и матчи длинного календаря оставались без тура)
    schedule = round_robin(ids)
    total = 0
    now = datetime.utcnow()
    for tour_no in range(1, len(schedule) * rounds_needed + 1):
        leg = (tour_no - 1) // len(schedule)
        pairs = schedule[(tour_no - 1) % len(schedule)]
        if leg % 2 == 1:
            pairs = [(b, a) for a, b in pairs]  # ответка: гости дома
        c.execute(
            "INSERT INTO tours (tournament_id, tour_number, status, deadline) VALUES (?,?,?,?) "
            "ON CONFLICT(tournament_id, tour_number) DO NOTHING",
            (tournament_id, tour_no,
             "open" if tour_no == 1 else "locked",
             (now + timedelta(days=tour_days)).isoformat() if tour_no == 1 else None),
        )
        for home, away in pairs:
            c.execute(
                "INSERT INTO matches (tournament_id, home_club_id, away_club_id, home_player_id,"
                " away_player_id, stage, tour_number, status) VALUES (?,?,?,?,?,'group',?,'pending')",
                (tournament_id, home, away, owners.get(home), owners.get(away), tour_no),
            )
            total += 1
    c.execute(
        "UPDATE tournaments SET total_tours=MAX(COALESCE(total_tours,0), ?), current_tour=1 WHERE id=?",
        (len(schedule) * rounds_needed, tournament_id),
    )
    c.commit()
    c.close()
    # тур 1 открывается сразу — без кэфов линия пустая, пока админ не обновит их руками
    try:
        import markets as markets_engine
        markets_engine.refresh_tour(tournament_id, 1)
    except Exception:
        log.exception("кэфы тура 1 не сгенерированы (турнир %s)", tournament_id)
    return total


def ensure_tour(tournament_id: int, tour_number: int) -> dict:
    c = appdb.db()
    row = c.execute(
        "SELECT * FROM tours WHERE tournament_id=? AND tour_number=?",
        (tournament_id, tour_number),
    ).fetchone()
    c.close()
    return dict(row) if row else {}


def open_tour(tournament_id: int, tour_number: int, tour_days: int | None = None) -> bool:
    """Открыть тур (дедлайн = tour_days от сейчас), предыдущие open → locked.
    Кэфы тура пересчитываются по актуальному Elo."""
    t = get_tournament(tournament_id)
    if not t:
        return False
    days = tour_days or int(t["tour_days"] or 3)
    now = datetime.utcnow()
    c = appdb.db()
    c.execute(
        "UPDATE tours SET status='locked' WHERE tournament_id=? AND status='open'",
        (tournament_id,),
    )
    c.execute(
        "INSERT INTO tours (tournament_id, tour_number, status, deadline) VALUES (?,?,?,?) "
        "ON CONFLICT(tournament_id, tour_number) DO UPDATE SET status='open', deadline=excluded.deadline",
        (tournament_id, tour_number, "open", (now + timedelta(days=days)).isoformat()),
    )
    c.execute(
        "UPDATE tournaments SET current_tour=MAX(current_tour, ?) WHERE id=?",
        (tour_number, tournament_id),
    )
    c.commit()
    c.close()
    try:
        import markets as markets_engine
        markets_engine.refresh_tour(tournament_id, tour_number)
    except Exception:
        pass
    return True


def rotate_tours() -> list[tuple[int, int, int]]:
    """Джоба tour_rotate: у открытого тура вышел дедлайн → lock; открываем следующий
    (неигранные матчи остаются доигрываться — решение 09).
    Возвращает (турнир, открыт тур, закрыт тур)."""
    now = datetime.utcnow().isoformat()
    rotated: list[tuple[int, int, int]] = []
    for t in all_active_tournaments():
        if t["format"] != "league" or t["tour_mode"] != "manual":
            continue
        c = appdb.db()
        open_tour_row = c.execute(
            "SELECT * FROM tours WHERE tournament_id=? AND status='open' ORDER BY tour_number LIMIT 1",
            (t["id"],),
        ).fetchone()
        c.close()
        if not open_tour_row or not open_tour_row["deadline"]:
            continue
        if open_tour_row["deadline"] > now:
            continue
        nxt = open_tour_row["tour_number"] + 1
        if nxt <= (t["total_tours"] or 0):
            open_tour(t["id"], nxt)
            rotated.append((t["id"], nxt, open_tour_row["tour_number"]))
    return rotated


# ===== кубковые сетки =====

# порядок стадий плей-офф (ранние → финал)
CUP_STAGES = ["r64", "r32", "r16", "qf", "sf", "final"]
CUP_STAGE_NAMES = {"r64": "1/32 финала", "r32": "1/16 финала", "r16": "1/8 финала",
                   "qf": "1/4 финала", "sf": "1/2 финала", "final": "Финал"}

# одна синхронизация сетки за раз: бот и мини-апп живут в одном процессе
_cup_lock = threading.Lock()


def _cup_stage_for(n: int) -> str:
    return {2: "final", 4: "sf", 8: "qf", 16: "r16", 32: "r32", 64: "r64"}.get(n, f"r{n}")


def _stage_rank(stage: str) -> int:
    return CUP_STAGES.index(stage) if stage in CUP_STAGES else -1


def create_cup_bracket(tournament_id: int, club_ids: list[int], stage: str | None = None) -> list[dict]:
    """Жеребьёвка: случайные пары, серии до 2 побед (план 07). Создаёт ties +
    первые игры (+ рынки). Некратное степени двойки число участников — «пассы»:
    серия с одним клубом, проход без игры (bye). Возвращает созданные ties."""
    ids = list(dict.fromkeys(club_ids))
    random.shuffle(ids)
    if len(ids) < 2:
        return []
    size = 2
    while size < len(ids):
        size *= 2
    if stage is None:
        stage = _cup_stage_for(size)
    n_pairs = size // 2
    byes = size - len(ids)
    # пассы равномерно по сетке; byes < n_pairs → в каждой паре максимум один пропуск
    bye_pos = {i * n_pairs // byes for i in range(byes)} if byes else set()
    c = appdb.db()
    if c.execute("SELECT 1 FROM ties WHERE tournament_id=? LIMIT 1", (tournament_id,)).fetchone():
        c.close()
        log.warning("кубок %s: сетка уже есть, повторная жеребьёвка пропущена", tournament_id)
        return []
    c.execute("UPDATE tournaments SET stage='playoff' WHERE id=?", (tournament_id,))
    ties = []
    it = iter(ids)
    for pos in range(n_pairs):
        a = next(it)
        b = None if pos in bye_pos else next(it)
        tie_id, _ = _create_tie(c, tournament_id, stage, pos, a, b)
        ties.append({"tie_id": tie_id, "club_a": a, "club_b": b, "stage": stage, "bye": b is None})
    c.commit()
    c.close()
    # рынки — только после commit: генераторы читают ties/matches своим соединением
    ensure_cup_markets(tournament_id)
    return ties


def _create_tie(c, tournament_id: int, stage: str, pos: int, a: int, b: int | None) -> tuple[int, int | None]:
    """Серия на позиции pos стадии. b=None — пасс: победитель сразу, без игр.
    → (tie_id, id первой игры | None)."""
    tie_id = c.insert_returning_id(
        "INSERT INTO ties (tournament_id, stage, club_a_id, club_b_id, winner_club_id, bracket_pos) "
        "VALUES (?,?,?,?,?,?)",
        (tournament_id, stage, a, b, a if b is None else None, pos),
    )
    if b is None:
        return tie_id, None
    return tie_id, _insert_tie_game(c, tournament_id, tie_id, a, b, stage, 1)


def ensure_cup_markets(tournament_id: int) -> int:
    """Рынки на ещё не выставленные игры кубка: 1х2/тоталы на каждую pending-игру,
    рынки серии — на первую. Уже выставленные кэфы не трогаем. → игр обработано."""
    import markets as markets_engine
    c = appdb.db()
    rows = [dict(r) for r in c.execute(
        "SELECT m.id, m.tie_id, m.game_in_tie, "
        " (SELECT COUNT(*) FROM markets k WHERE k.match_id=m.id AND k.code NOT LIKE 'tie_%') AS n_main, "
        " (SELECT COUNT(*) FROM markets k WHERE k.match_id=m.id AND k.code LIKE 'tie_%') AS n_tie "
        "FROM matches m WHERE m.tournament_id=? AND m.tie_id IS NOT NULL AND m.status='pending' ORDER BY m.id",
        (tournament_id,)).fetchall()]
    c.close()
    n = 0
    for r in rows:
        try:
            if not r["n_main"]:
                markets_engine.generate_markets(r["id"])
            if r["game_in_tie"] == 1 and not r["n_tie"]:
                markets_engine.generate_tie_markets(r["id"], r["tie_id"])
            n += 1
        except Exception:
            log.exception("рынки кубковой игры %s", r["id"])
    return n


def _insert_tie_game(c, tournament_id: int, tie_id: int, club_a: int, club_b: int,
                     stage: str, game_no: int) -> int:
    """Игра серии: дом/гости чередуются (нечётная — клуб А дома)."""
    home, away = (club_a, club_b) if game_no % 2 == 1 else (club_b, club_a)
    return c.insert_returning_id(
        "INSERT INTO matches (tournament_id, home_club_id, away_club_id, stage, tie_id,"
        " game_in_tie, status) VALUES (?,?,?,?,?,?, 'pending')",
        (tournament_id, home, away, stage, tie_id, game_no),
    )


def _game_winner_side(tie, g) -> str | None:
    """'a'/'b' — кто выиграл игру серии; None — ничья без пенальти (серия продолжается)."""
    home_is_a = g["home_club_id"] == tie["club_a_id"]
    sa, sb = (g["score1"], g["score2"]) if home_is_a else (g["score2"], g["score1"])
    if sa == sb:
        # ничья в серии до 2 побед → пенальти решают игру (план 07/FC-практика)
        if g["pens1"] is None or g["pens2"] is None:
            return None
        sa, sb = (g["pens1"], g["pens2"]) if home_is_a else (g["pens2"], g["pens1"])
        if sa == sb:
            return None
    return "a" if sa > sb else "b"


def advance_tie(c, match_row: dict, score_home: int, score_away: int,
                pens_home: int | None, pens_away: int | None) -> list[int]:
    """После финализации игры серии (в т.ч. повторной — спор/правка): пересчитать
    wins с нуля по сыгранным играм, создать следующую игру или зафиксировать
    победителя. Счёт матча уже записан в c. → id отменённых лишних игр."""
    tie_id = match_row["tie_id"]
    if tie_id is None:
        return []
    return recompute_tie(c, tie_id)


def recompute_tie(c, tie_id: int) -> list[int]:
    tie = c.execute("SELECT * FROM ties WHERE id=?", (tie_id,)).fetchone()
    if not tie:
        return []
    a, b = tie["club_a_id"], tie["club_b_id"]
    if b is None:
        return []  # пасс: проход без игр
    games = c.execute(
        "SELECT * FROM matches WHERE tie_id=? ORDER BY game_in_tie, id", (tie_id,)).fetchall()
    wins = {"a": 0, "b": 0}
    last_played = 0
    for g in games:
        if g["status"] not in ("confirmed", "disputed") or g["score1"] is None:
            continue
        if max(wins.values()) >= 2:
            break  # серия уже решена — поздние игры не считаем
        last_played = g["game_in_tie"] or 1
        side = _game_winner_side(tie, g)
        if side:
            wins[side] += 1
    winner = a if wins["a"] >= 2 else (b if wins["b"] >= 2 else None)
    c.execute("UPDATE ties SET wins_a=?, wins_b=?, winner_club_id=? WHERE id=?",
              (wins["a"], wins["b"], winner, tie_id))
    pending = [g for g in games if g["status"] == "pending"]
    cancelled: list[int] = []
    if winner:
        # серия решена (например, после правки счёта) — лишние игры отменяем
        for g in pending:
            c.execute("UPDATE matches SET status='cancelled' WHERE id=?", (g["id"],))
            cancelled.append(g["id"])
    elif not pending:
        _insert_tie_game(c, tie["tournament_id"], tie_id, a, b, tie["stage"], last_played + 1)
    return cancelled


def cup_stage_winners(tournament_id: int, stage: str) -> list[int]:
    c = appdb.db()
    rows = c.execute(
        "SELECT winner_club_id FROM ties WHERE tournament_id=? AND stage=? AND winner_club_id IS NOT NULL",
        (tournament_id, stage),
    ).fetchall()
    c.close()
    return [r["winner_club_id"] for r in rows]


def next_cup_stage(stage: str) -> str | None:
    r = _stage_rank(stage)
    return CUP_STAGES[r + 1] if 0 <= r < len(CUP_STAGES) - 1 else None


def draw_next_cup_stage(tournament_id: int) -> int:
    """Следующая стадия, если текущая решена (обёртка над sync_cup). → ties создано."""
    return len(sync_cup(tournament_id).get("created", []))


def _tie_has_played(c, tie_id: int) -> bool:
    return bool(c.execute(
        "SELECT 1 FROM matches WHERE tie_id=? AND status IN ('confirmed','disputed','reported') LIMIT 1",
        (tie_id,)).fetchone())


def _cup_warn(c, tournament_id: int, details: str, res: dict) -> None:
    """Предупреждение в аудит (без дублей при повторных синхронизациях)."""
    res["warnings"].append(details)
    if c.execute("SELECT 1 FROM tournament_audit_log WHERE tournament_id=? AND action='cup_warning' "
                 "AND details=?", (tournament_id, details)).fetchone():
        return
    log.warning("кубок %s: %s", tournament_id, details)
    c.execute("INSERT INTO tournament_audit_log (tournament_id, actor_telegram_id, action, details) "
              "VALUES (?, NULL, 'cup_warning', ?)", (tournament_id, details))


def _club_name(c, club_id: int | None) -> str:
    if club_id is None:
        return "пасс"
    row = c.execute("SELECT name FROM clubs WHERE id=?", (club_id,)).fetchone()
    return row["name"] if row else f"#{club_id}"


def _stage_ties(c, tournament_id: int, stage: str) -> list[dict]:
    return [dict(r) for r in c.execute(
        "SELECT * FROM ties WHERE tournament_id=? AND stage=? ORDER BY bracket_pos, id",
        (tournament_id, stage)).fetchall()]


def sync_cup(tournament_id: int, actor_tg: int | None = None) -> dict:
    """Автопрогресс сетки (вызывается после каждой финализации игры серии).

    - все серии стадии решены → следующая стадия: победитель пары 2k против 2k+1;
    - победитель серии сменился (спор/правка), а в следующей серии ещё никто не играл →
      пара пересобирается (старые игры cancelled, ставки на них — void);
      уже играют → оставляем, предупреждение в tournament_audit_log;
    - финал решён → кубок finished + cup_results (призовые платит finalize_season).
    Идемпотентно: повторный вызов ничего не создаёт."""
    t = get_tournament(tournament_id)
    if not t or t["format"] == "league":
        return {"ok": False, "error": "кубок не найден"}
    res = {"ok": True, "created": [], "rebuilt": [], "removed": [], "warnings": [],
           "cancelled": [], "finished": None, "reopened": False}
    with _cup_lock:
        c = appdb.db()
        try:
            _sync_cup(c, t, res)
            if res["created"] or res["rebuilt"] or res["removed"] or res["finished"]:
                c.execute(
                    "INSERT INTO tournament_audit_log (tournament_id, actor_telegram_id, action, details) "
                    "VALUES (?,?, 'cup_sync', ?)",
                    (tournament_id, actor_tg,
                     f"создано={res['created']} пересобрано={res['rebuilt']} снято={res['removed']} "
                     f"финал={res['finished']}"))
            c.commit()
        finally:
            c.close()
    # после commit: ставки на отменённые игры → void/возврат, рынки на новые игры
    if res["cancelled"]:
        import bets_engine
        for mid in res["cancelled"]:
            try:
                bets_engine.settle_match(mid)
            except Exception:
                log.exception("settle_match отменённой игры %s", mid)
    ensure_cup_markets(tournament_id)
    return res


def _sync_cup(c, t: dict, res: dict) -> None:
    tid = t["id"]
    stages = [r["stage"] for r in c.execute(
        "SELECT DISTINCT stage FROM ties WHERE tournament_id=?", (tid,)).fetchall()]
    stages = sorted((s for s in stages if s in CUP_STAGES), key=_stage_rank)
    if not stages:
        return
    stage = stages[0]
    while stage and stage != "final":
        nxt = next_cup_stage(stage)
        cur = _stage_ties(c, tid, stage)
        existing = {x["bracket_pos"]: x for x in _stage_ties(c, tid, nxt)}
        if not cur and not existing:
            break
        next_exists = bool(existing)
        all_done = bool(cur) and all(x["winner_club_id"] for x in cur)
        for k in range((len(cur) + 1) // 2):
            src = cur[2 * k:2 * k + 2]
            winners = [x["winner_club_id"] for x in src]
            expected = None
            if all(winners):
                expected = (winners[0], winners[1] if len(winners) > 1 else None)
            ex = existing.pop(k, None)
            if ex:
                if expected == (ex["club_a_id"], ex["club_b_id"]):
                    continue
                if _tie_has_played(c, ex["id"]):
                    want = " — ".join(_club_name(c, x) for x in expected) if expected else "не решено"
                    _cup_warn(c, tid, f"{CUP_STAGE_NAMES.get(nxt, nxt)} #{k + 1}: победитель прошлой "
                                      f"стадии изменился (по сетке: {want}), но серия #{ex['id']} уже "
                                      f"играется — пара оставлена", res)
                    continue
                for g in c.execute("SELECT id FROM matches WHERE tie_id=? AND status='pending'",
                                   (ex["id"],)).fetchall():
                    c.execute("UPDATE matches SET status='cancelled' WHERE id=?", (g["id"],))
                    res["cancelled"].append(g["id"])
                if expected is None:
                    c.execute("DELETE FROM ties WHERE id=?", (ex["id"],))
                    res["removed"].append(ex["id"])
                    continue
                a, b = expected
                c.execute("UPDATE ties SET club_a_id=?, club_b_id=?, wins_a=0, wins_b=0, winner_club_id=? "
                          "WHERE id=?", (a, b, a if b is None else None, ex["id"]))
                if b is not None:
                    _insert_tie_game(c, tid, ex["id"], a, b, nxt, 1)
                res["rebuilt"].append(ex["id"])
            elif expected and (all_done or next_exists):
                tie_id, _ = _create_tie(c, tid, nxt, k, *expected)
                res["created"].append(tie_id)
        # позиции, которых в новой сетке нет (сетка стадии сжалась) — снять, если не играли
        for k, ex in existing.items():
            if _tie_has_played(c, ex["id"]):
                _cup_warn(c, tid, f"{CUP_STAGE_NAMES.get(nxt, nxt)}: лишняя серия #{ex['id']} уже играется", res)
                continue
            for g in c.execute("SELECT id FROM matches WHERE tie_id=? AND status='pending'", (ex["id"],)).fetchall():
                c.execute("UPDATE matches SET status='cancelled' WHERE id=?", (g["id"],))
                res["cancelled"].append(g["id"])
            c.execute("DELETE FROM ties WHERE id=?", (ex["id"],))
            res["removed"].append(ex["id"])
        stage = nxt
    _sync_cup_final(c, t, res)


def _sync_cup_final(c, t: dict, res: dict) -> None:
    """Финал решён → кубок finished + победитель в cup_results; решение отменено → назад в playoff."""
    tid = t["id"]
    fin = c.execute("SELECT winner_club_id FROM ties WHERE tournament_id=? AND stage='final' "
                    "ORDER BY bracket_pos, id LIMIT 1", (tid,)).fetchone()
    winner = fin["winner_club_id"] if fin else None
    row = c.execute("SELECT * FROM cup_results WHERE tournament_id=?", (tid,)).fetchone()
    stage_now = c.execute("SELECT stage FROM tournaments WHERE id=?", (tid,)).fetchone()["stage"]
    if winner:
        if not row:
            # кубок закрыт старым finalize_season без cup_results — призовые уже выплачены
            legacy_paid = 1 if stage_now == "finished" else 0
            c.execute("INSERT INTO cup_results (tournament_id, winner_club_id, prize_paid) VALUES (?,?,?)",
                      (tid, winner, legacy_paid))
        elif row["winner_club_id"] != winner:
            if row["prize_paid"]:
                _cup_warn(c, tid, f"победитель финала изменился ({row['winner_club_id']} → {winner}) "
                                  f"после выплаты призовых клубу {row['prize_club_id']}", res)
            c.execute("UPDATE cup_results SET winner_club_id=? WHERE tournament_id=?", (winner, tid))
        if stage_now != "finished":
            c.execute("UPDATE tournaments SET stage='finished' WHERE id=?", (tid,))
            res["finished"] = winner
    elif row:
        if row["prize_paid"]:
            _cup_warn(c, tid, "финал снова не решён, но призовые уже выплачены — кубок оставлен закрытым", res)
            return
        c.execute("DELETE FROM cup_results WHERE tournament_id=?", (tid,))
        c.execute("UPDATE tournaments SET stage='playoff' WHERE id=?", (tid,))
        res["reopened"] = True


def cup_bracket(tournament_id: int) -> dict | None:
    """Сетка кубка по стадиям: серии, клубы, счёт серии, игры (без отменённых)."""
    t = get_tournament(tournament_id)
    if not t or t["format"] == "league":
        return None
    c = appdb.db()
    ties = [dict(r) for r in c.execute(
        "SELECT * FROM ties WHERE tournament_id=? ORDER BY bracket_pos, id", (tournament_id,)).fetchall()]
    games = {}
    for g in c.execute("SELECT * FROM matches WHERE tournament_id=? AND tie_id IS NOT NULL "
                       "AND status!='cancelled' ORDER BY game_in_tie, id", (tournament_id,)).fetchall():
        games.setdefault(g["tie_id"], []).append(dict(g))
    res = c.execute("SELECT * FROM cup_results WHERE tournament_id=?", (tournament_id,)).fetchone()
    c.close()
    by_stage: dict[str, list] = {}
    for x in ties:
        x["games"] = games.get(x["id"], [])
        by_stage.setdefault(x["stage"], []).append(x)
    stages = [{"code": s, "name": CUP_STAGE_NAMES.get(s, s), "ties": by_stage[s]}
              for s in sorted(by_stage, key=_stage_rank)]
    return {"tournament": t, "stages": stages, "winner_club_id": res["winner_club_id"] if res else None,
            "prize_paid": bool(res and res["prize_paid"])}


def format_bracket(tournament_id: int) -> str:
    """Сетка кубка текстом (для /bracket)."""
    br = cup_bracket(tournament_id)
    if not br:
        return "Кубок не найден."
    names: dict = {}

    def nm(cid):
        if cid is None:
            return "—"
        if cid not in names:
            cl = get_club(cid)
            names[cid] = cl["name"] if cl else f"#{cid}"
        return names[cid]

    t = br["tournament"]
    lines = [f"🏆 «{t['name']}» (#{t['id']})" + (" — завершён" if t["stage"] == "finished" else "")]
    if not br["stages"]:
        lines.append("Сетка ещё не сгенерирована.")
    for st in br["stages"]:
        lines.append(f"\n{st['name']}:")
        for x in st["ties"]:
            if x["club_b_id"] is None:
                lines.append(f"  {nm(x['club_a_id'])} — проход без игры")
                continue
            mark_a = "✅ " if x["winner_club_id"] == x["club_a_id"] else ""
            mark_b = " ✅" if x["winner_club_id"] == x["club_b_id"] else ""
            detail = ", ".join(
                f"{sa}:{sb}" + (f" (пен. {pa}:{pb})" if pa is not None else "") + (" спор" if st_ == "disputed" else "")
                for sa, sb, pa, pb, st_ in (_game_for_a(x, g) for g in x["games"] if g["score1"] is not None))
            lines.append(f"  {mark_a}{nm(x['club_a_id'])} {x['wins_a']}:{x['wins_b']} {nm(x['club_b_id'])}{mark_b}"
                         + (f"  [{detail}]" if detail else ""))
    if br["winner_club_id"]:
        lines.append(f"\n🥇 Победитель: {nm(br['winner_club_id'])}")
    return "\n".join(lines)


def _game_for_a(tie: dict, g: dict) -> tuple:
    """Счёт игры с точки зрения клуба А серии: (голы А, голы Б, пен. А, пен. Б, статус)."""
    if g["home_club_id"] == tie["club_a_id"]:
        return g["score1"], g["score2"], g["pens1"], g["pens2"], g["status"]
    return g["score2"], g["score1"], g["pens2"], g["pens1"], g["status"]


# ===== таблицы =====

def division_standings(division_id: int) -> list[dict]:
    """Таблица дивизиона по подтверждённым матчам: очки/И/В/Н/П/мячи."""
    clubs = division_clubs(division_id)
    if not clubs:
        return []
    ids = [cl["id"] for cl in clubs]
    stats = {cid: {"club_id": cid, "games": 0, "wins": 0, "draws": 0, "losses": 0,
                   "gf": 0, "ga": 0, "points": 0} for cid in ids}
    c = appdb.db()
    div = c.execute("SELECT tournament_id FROM divisions WHERE id=?", (division_id,)).fetchone()
    tournament_id = div["tournament_id"] if div else None
    placeholders = ",".join("?" * len(ids))
    # только матчи сезона этого дивизиона — прошлые сезоны не подмешиваем
    rows = c.execute(
        f"SELECT home_club_id, away_club_id, score1, score2 FROM matches "
        f"WHERE status='confirmed' AND stage='group' AND tournament_id=? "
        f"AND (home_club_id IN ({placeholders}) OR away_club_id IN ({placeholders}))",
        (tournament_id, *ids, *ids),
    ).fetchall()
    c.close()
    for m in rows:
        if m["score1"] is None:
            continue
        h, a = m["home_club_id"], m["away_club_id"]
        if h not in stats or a not in stats:
            continue
        stats[h]["games"] += 1
        stats[a]["games"] += 1
        stats[h]["gf"] += m["score1"]
        stats[h]["ga"] += m["score2"]
        stats[a]["gf"] += m["score2"]
        stats[a]["ga"] += m["score1"]
        if m["score1"] > m["score2"]:
            stats[h]["wins"] += 1; stats[h]["points"] += 3; stats[a]["losses"] += 1
        elif m["score1"] < m["score2"]:
            stats[a]["wins"] += 1; stats[a]["points"] += 3; stats[h]["losses"] += 1
        else:
            stats[h]["draws"] += 1; stats[a]["draws"] += 1
            stats[h]["points"] += 1; stats[a]["points"] += 1
    table = [dict(v) for v in stats.values()]
    table.sort(key=lambda r: (-r["points"], -(r["gf"] - r["ga"]), -r["gf"], r["club_id"]))
    for i, row in enumerate(table, 1):
        row["position"] = i
    return table


def tour_matches(tournament_id: int, tour_number: int) -> list[dict]:
    c = appdb.db()
    rows = c.execute(
        "SELECT * FROM matches WHERE tournament_id=? AND tour_number=? ORDER BY id",
        (tournament_id, tour_number),
    ).fetchall()
    c.close()
    return [dict(r) for r in rows]


def format_matches_for_tour(tournament_id: int, tour_number: int) -> str:
    lines = []
    names = {}
    c = appdb.db()
    for m in tour_matches(tournament_id, tour_number):
        for cid in (m["home_club_id"], m["away_club_id"]):
            if cid not in names:
                r = c.execute("SELECT name FROM clubs WHERE id=?", (cid,)).fetchone()
                names[cid] = r["name"] if r else f"#{cid}"
        score = f" {m['score1']}:{m['score2']}" if m["score1"] is not None else ""
        if m["status"] == "disputed":
            score += " (спор)"
        lines.append(f"#{m['id']} {names[m['home_club_id']]} — {names[m['away_club_id']]}{score}")
    c.close()
    return "\n".join(lines) if lines else "Матчей нет."


# ===== финал сезона (решение 09: применяет АДМИН кнопкой, не фоновый job) =====

def season_final_preview(tournament_id: int) -> dict:
    """Что произойдёт при финализации: чемпионы, призовые, повышения/вылеты."""
    import settings as appsettings
    t = get_tournament(tournament_id)
    if not t or t["format"] != "league":
        return {"error": "турнир не найден или не лига"}
    divs = tournament_divisions(tournament_id)
    c = appdb.db()
    prizes = {
        "champion": appsettings.setting_int("prize_champion", 100_000_000),
        "second": appsettings.setting_int("prize_second", 30_000_000),
        "third": appsettings.setting_int("prize_third", 15_000_000),
    }
    c.close()
    rows = []
    pc = _promote_count(t)
    for i, d in enumerate(divs):
        table = division_standings(d["id"])
        for r in table:
            row = {"division": d["name"], "position": r["position"],
                   "club": get_club(r["club_id"])["name"], "points": r["points"]}
            if i == 0 and r["position"] == 1:
                row["prize"] = prizes["champion"]
            elif i == 0 and r["position"] == 2:
                row["prize"] = prizes["second"]
            elif (i == 0 and r["position"] == 3) or (i > 0 and r["position"] == 1):
                row["prize"] = prizes["third"]
            if pc and r["position"] <= pc and i > 0:
                row["move"] = f"↑ в {divs[i-1]['name']}"
            if pc and r["position"] > len(table) - pc and i < len(divs) - 1:
                row["move"] = f"↓ в {divs[i+1]['name']}"
            rows.append(row)
    return {"tournament": t["name"], "prizes": prizes, "rows": rows, "promote_count": pc}


def _promote_count(t: dict) -> int:
    """Сколько клубов меняются между соседними дивизионами (0 — независимые лиги)."""
    v = t.get("promote_count")
    return 3 if v is None else int(v)


def finalize_season(tournament_id: int, actor_tg: int | None = None) -> dict:
    """Итоги сезона: призовые в бюджеты клубов, 3↑/3↓, турнир → finished.
    Elo нового сезона стартует с 1000 автоматически (новый турнир = новые строки)."""
    import settings as appsettings
    preview = season_final_preview(tournament_id)
    if preview.get("error"):
        return preview
    if get_tournament(tournament_id)["stage"] == "finished":
        return {"error": "сезон уже финализирован — призовые повторно не выплачиваются"}
    divs = tournament_divisions(tournament_id)
    c = appdb.db()
    # защита от гонки двух подтверждений: захватываем турнир атомарно
    cur = c.execute("UPDATE tournaments SET stage='finished' WHERE id=? AND stage!='finished'",
                    (tournament_id,))
    if cur.rowcount == 0:
        c.close()
        return {"error": "сезон уже финализирован — призовые повторно не выплачиваются"}
    paid, moves = [], []
    pc = _promote_count(get_tournament(tournament_id))
    for i, d in enumerate(divs):
        table = division_standings(d["id"])
        for r in table:
            club = get_club(r["club_id"])
            prize = 0
            if i == 0 and r["position"] == 1:
                prize = appsettings.setting_int("prize_champion", 100_000_000)
            elif i == 0 and r["position"] == 2:
                prize = appsettings.setting_int("prize_second", 30_000_000)
            elif (i == 0 and r["position"] == 3) or (i > 0 and r["position"] == 1):
                prize = appsettings.setting_int("prize_third", 15_000_000)
            if prize:
                c.execute("UPDATE clubs SET budget=budget+? WHERE id=?", (prize, r["club_id"]))
                c.execute("INSERT INTO balance_history (user_id, delta, reason) VALUES (NULL, ?, ?)",
                          (prize, f"призовое за сезон: {club['name']} ({d['name']}, {r['position']} место)"))
                paid.append({"club": club["name"], "prize": prize, "note": f"{d['name']} {r['position']} место"})
            # pc вверх / pc вниз (только между существующими дивизионами; 0 — без движений)
            if pc and r["position"] <= pc and i > 0:
                c.execute("UPDATE clubs SET division_id=? WHERE id=?", (divs[i-1]["id"], r["club_id"]))
                moves.append({"club": club["name"], "move": f"↑ {d['name']} → {divs[i-1]['name']}"})
            elif pc and r["position"] > len(table) - pc and i < len(divs) - 1:
                c.execute("UPDATE clubs SET division_id=? WHERE id=?", (divs[i+1]["id"], r["club_id"]))
                moves.append({"club": club["name"], "move": f"↓ {d['name']} → {divs[i+1]['name']}"})
    # призовые за кубки сезона (победители final). Кубок, чей финал решён, уже finished
    # (sync_cup), но в cup_results prize_paid=0 → платим здесь один раз. Старые кубки без
    # cup_results: платим, пока не finished. Выплату «захватываем» атомарно через prize_paid.
    cup_prize = appsettings.setting_int("prize_cup_winner", 50_000_000)
    season_id = get_tournament(tournament_id).get("season_id")
    cup_sql = ("SELECT t.id, t.name, t.format FROM tournaments t "
               "LEFT JOIN cup_results r ON r.tournament_id=t.id WHERE t.format!='league' AND "
               "((r.tournament_id IS NULL AND t.stage!='finished') OR "
               " (r.tournament_id IS NOT NULL AND COALESCE(r.prize_paid,0)=0))")
    cup_args: tuple = ()
    if season_id is not None:
        cup_sql += " AND (t.season_id IS NULL OR t.season_id=?)"
        cup_args = (season_id,)
    cups = [dict(r) for r in c.execute(cup_sql, cup_args).fetchall()]
    cup_winners = []
    for cup in cups:
        row = c.execute(
            "SELECT winner_club_id FROM ties WHERE tournament_id=? AND stage='final' AND winner_club_id IS NOT NULL",
            (cup["id"],)).fetchone()
        if not row:
            continue
        win = row["winner_club_id"]
        c.execute("INSERT INTO cup_results (tournament_id, winner_club_id, prize_paid) VALUES (?,?,0) "
                  "ON CONFLICT(tournament_id) DO NOTHING", (cup["id"], win))
        claim = c.execute(
            "UPDATE cup_results SET prize_paid=1, winner_club_id=?, prize_club_id=?, prize_amount=?, "
            "prize_paid_at=datetime('now') WHERE tournament_id=? AND COALESCE(prize_paid,0)=0",
            (win, win, cup_prize, cup["id"]))
        if claim.rowcount == 0:
            continue
        club = get_club(win)
        c.execute("UPDATE clubs SET budget=budget+? WHERE id=?", (cup_prize, win))
        c.execute("INSERT INTO balance_history (user_id, delta, reason) VALUES (NULL, ?, ?)",
                  (cup_prize, f"призовое за кубок: {club['name']} ({cup['name']})"))
        c.execute("UPDATE tournaments SET stage='finished' WHERE id=?", (cup["id"],))
        cup_winners.append({"club": club["name"], "cup": cup["name"], "prize": cup_prize})
    c.execute(
        "INSERT INTO tournament_audit_log (tournament_id, actor_telegram_id, action, details) "
        "VALUES (?, ?, 'season_finalized', ?)",
        (tournament_id, actor_tg, f"призовых={len(paid)} обменов={len(moves)} кубков={len(cup_winners)}"),
    )
    c.commit()
    c.close()
    return {"ok": True, "tournament": preview["tournament"],
            "prize_rows": paid, "moves": moves, "cup_winners": cup_winners}
