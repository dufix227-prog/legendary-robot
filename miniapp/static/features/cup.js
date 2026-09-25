import { $, api, esc, logoHtml, hooks } from '../lib.js';

/* ===== кубки: вкладка «Кубки» в «Таблицах» — сетка по стадиям =====
   index.html/app.js не трогаем: кнопку и контейнер добавляем сами, клик по «Кубкам»
   перехватываем в capture-фазе (иначе app.js покажет «Результаты»). */

const STAGES = ['r64', 'r32', 'r16', 'qf', 'sf', 'final'];
const STAGE_NAMES = { r64: '1/32 финала', r32: '1/16 финала', r16: '1/8 финала', qf: '1/4 финала', sf: '1/2 финала', final: 'Финал' };
const cupState = { active: false, cups: null, currentId: null, loading: false };

function ensureUi() {
  const tabs = $('#tabs-tables');
  if (!tabs || $('#cups-tab')) return;
  const btn = document.createElement('button');
  btn.className = 'tab';
  btn.id = 'cups-tab';
  btn.dataset.tab = 'cups';
  btn.textContent = 'Кубки';
  tabs.appendChild(btn);
  const wrap = document.createElement('div');
  wrap.id = 'cups-wrap';
  wrap.hidden = true;
  ($('#results-wrap') || tabs).after(wrap);
  tabs.addEventListener('click', onTabClick, true);
  wrap.addEventListener('click', (ev) => {
    const chip = ev.target.closest('[data-cup]');
    if (!chip) return;
    cupState.currentId = Number(chip.dataset.cup);
    render();
  });
}

function setActive(on) {
  cupState.active = on;
  $('#view-tables')?.classList.toggle('cup-mode', on);
  $('#cups-wrap').hidden = !on;
  if (on) document.querySelectorAll('#view-tables [data-tab-wrap]').forEach((w) => { w.hidden = true; });
}

function onTabClick(ev) {
  const tab = ev.target.closest('.tab');
  if (!tab) return;
  if (tab.dataset.tab !== 'cups') { setActive(false); return; }
  ev.stopPropagation();
  document.querySelectorAll('#tabs-tables .tab').forEach((t) => t.classList.toggle('active', t === tab));
  setActive(true);
  load();
}

async function load() {
  const wrap = $('#cups-wrap');
  if (!cupState.cups) wrap.innerHTML = '<div class="empty-note">Загружаю сетку…</div>';
  if (cupState.loading) return;
  cupState.loading = true;
  try {
    const { cups } = await api('/api/cups');
    cupState.cups = cups || [];
    if (!cupState.cups.some((c) => c.id === cupState.currentId)) cupState.currentId = cupState.cups[0]?.id ?? null;
    render();
  } catch (e) {
    wrap.innerHTML = `<div class="empty-note">${esc(e.message)}</div>`;
  } finally {
    cupState.loading = false;
  }
}

function teamRow(club, wins, tie, placeholder = 'Ожидает') {
  if (!club) return `<div class="cup-row tbd"><span class="mc-fallback cup-logo">·</span><span class="cup-name">${placeholder}</span><span class="cup-wins"></span></div>`;
  const decided = tie.winner_club_id != null;
  const cls = decided ? (tie.winner_club_id === club.id ? 'win' : 'lose') : '';
  return `<div class="cup-row ${cls}">${logoHtml(club, 'cup-logo')}<span class="cup-name">${esc(club.name)}</span><span class="cup-wins">${tie.bye ? '' : wins}</span></div>`;
}

function gameChip(g) {
  if (g.score_a == null) {
    return `<span class="cup-game pending" data-open="${g.id}">И${g.game_no} · ждёт</span>`;
  }
  const pens = g.pens_a != null ? `<small>пен. ${g.pens_a}:${g.pens_b}</small>` : '';
  const cls = g.status === 'disputed' ? 'disputed' : '';
  return `<span class="cup-game ${cls}" data-open="${g.id}">${g.score_a}:${g.score_b}${pens}${g.status === 'disputed' ? ' ⚠️' : ''}</span>`;
}

function tieCard(t) {
  if (t.bye) {
    return `<div class="cup-tie done bye">${teamRow(t.club_a, '', t)}
      <div class="cup-row tbd"><span class="cup-name">проход без игры</span></div></div>`;
  }
  const games = t.games.length ? `<div class="cup-games">${t.games.map(gameChip).join('')}</div>` : '';
  return `<div class="cup-tie ${t.winner_club_id != null ? 'done' : ''}">
    ${teamRow(t.club_a, t.wins_a, t)}${teamRow(t.club_b, t.wins_b, t)}${games}</div>`;
}

function placeholderCard() {
  const blank = { winner_club_id: null };
  return `<div class="cup-tie tbd">${teamRow(null, '', blank)}${teamRow(null, '', blank)}</div>`;
}

function column(title, cards, isFinal) {
  return `<div class="cup-col ${isFinal ? 'final' : ''}">
    <div class="cup-col-title">${esc(title)}</div>
    <div class="cup-col-body">${cards.join('')}</div></div>`;
}

function bracketHtml(cup) {
  const cols = cup.stages.map((s) => column(s.name, s.ties.map(tieCard), s.code === 'final'));
  // будущие стадии — пустые слоты, чтобы сетка читалась целиком
  const last = cup.stages[cup.stages.length - 1];
  if (last && last.code !== 'final') {
    let n = last.ties.length;
    for (let i = STAGES.indexOf(last.code) + 1; i < STAGES.length && i > 0; i++) {
      n = Math.max(1, Math.ceil(n / 2));
      cols.push(column(STAGE_NAMES[STAGES[i]], Array.from({ length: n }, placeholderCard), STAGES[i] === 'final'));
    }
  }
  return `<div class="cup-bracket">${cols.join('')}</div>`;
}

function render() {
  const wrap = $('#cups-wrap');
  if (!wrap || !cupState.active) return;
  const cups = cupState.cups || [];
  if (!cups.length) {
    wrap.innerHTML = '<div class="empty-note">Кубков пока нет.</div>';
    return;
  }
  const cup = cups.find((c) => c.id === cupState.currentId) || cups[0];
  const chips = cups.length > 1 ? `<div class="chips cup-chips">${cups.map((c) =>
    `<button class="chip ${c.id === cup.id ? 'active' : ''}" data-cup="${c.id}">${esc(c.name)}${c.finished ? ' ✓' : ''}</button>`).join('')}</div>` : '';
  const current = cup.stages.find((s) => s.ties.some((t) => t.winner_club_id == null));
  const status = cup.finished ? 'завершён' : (current ? `идёт ${current.name.toLowerCase()}` : 'сетка не сгенерирована');
  const champion = cup.winner ? `<div class="cup-champion">${logoHtml(cup.winner, 'cup-logo-lg')}
      <div><div class="cup-champion-label">Победитель</div><div class="cup-champion-name">${esc(cup.winner.name)}</div></div>
      <span class="cup-trophy">🏆</span></div>` : '';
  wrap.innerHTML = `${chips}
    <div class="cup-head"><div class="cup-title">${esc(cup.name)}</div>
      <div class="cup-meta"><span class="cup-badge">${esc(cup.format_name)}</span>${esc(status)} · серии до 2 побед</div></div>
    ${champion}
    ${cup.stages.length ? bracketHtml(cup) : '<div class="empty-note">Жеребьёвки ещё не было.</div>'}`;
}

ensureUi();

// возврат на «Таблицы» с открытой вкладкой кубков — освежаем сетку
const prevTables = hooks.views.tables;
hooks.views.tables = () => {
  prevTables?.();
  if (cupState.active) load();
};
