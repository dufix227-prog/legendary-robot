/* Прогресс: достижения и Зал Славы в «Кабинете», fair-play учёт тренировок в «Клубе»,
   нарушения лимита тренировок в админке. */
import { $, api, esc, fmt, fmtTime, hooks, openSheet, toast, state } from '../lib.js';

if (!document.querySelector('link[href*="features/progress.css"]')) {
  document.head.insertAdjacentHTML('beforeend', '<link rel="stylesheet" href="/static/features/progress.css?v=1">');
}

const SMOKE = '🚬';
const PREVIEW_COUNT = 6;
let achCache = null;

/* ===== достижения ===== */

function achOrder(list) {
  // сначала «забрать», потом ближайшие к открытию, потом забранные
  const rank = (a) => (a.is_unlocked && !a.is_claimed ? 0 : !a.is_unlocked ? 1 : 2);
  return [...list].sort((a, b) => rank(a) - rank(b)
    || (b.progress / b.target) - (a.progress / a.target));
}

function achTile(a) {
  const claimable = a.is_unlocked && !a.is_claimed;
  const cls = claimable ? 'claimable' : a.is_unlocked ? 'claimed' : 'locked';
  const pct = Math.min(100, Math.round((a.progress / Math.max(1, a.target)) * 100));
  const foot = claimable
    ? `<button class="ach-claim" data-ach-claim="${esc(a.code)}">Забрать +${fmt(a.reward_coins)} ${SMOKE}</button>`
    : a.is_unlocked
      ? '<div class="ach-done">✓ Получено</div>'
      : `<div class="ach-bar"><div style="width:${pct}%"></div></div>
         <div class="ach-prog">${fmt(a.progress)} / ${fmt(a.target)}</div>`;
  return `<div class="ach-tile ${cls} r-${esc(a.rarity)}">
    <div class="ach-icon">${esc(a.badge_icon)}</div>
    <div class="ach-name">${esc(a.name)}</div>
    <div class="ach-desc">${esc(a.description)}</div>
    <div class="ach-reward">+${fmt(a.reward_coins)} ${SMOKE} · +${fmt(a.reward_xp)} XP · <span class="ach-rarity">${esc(a.rarity_name)}</span></div>
    ${foot}
  </div>`;
}

function renderAchCard(card) {
  const { achievements: list, summary } = achCache;
  const shown = achOrder(list).slice(0, PREVIEW_COUNT);
  card.innerHTML = `
    <div class="card-title">🏅 Достижения <span class="ach-count">${summary.unlocked}/${summary.total}</span>
      ${summary.claimable ? `<span class="ach-badge">${summary.claimable} к получению</span>` : ''}</div>
    <div class="ach-grid">${shown.map(achTile).join('')}</div>
    <button class="bonus-btn" id="ach-all" style="margin-top:10px">Показать все (${summary.total})</button>`;
  card.querySelector('#ach-all').onclick = openAllAchievements;
}

function openAllAchievements() {
  const list = achOrder(achCache.achievements);
  const body = openSheet(`🏅 Достижения · ${achCache.summary.unlocked}/${achCache.summary.total}`,
    `<div class="ach-grid" id="ach-sheet-grid">${list.map(achTile).join('')}</div>`);
  body.querySelector('#ach-sheet-grid').dataset.achSheet = '1';
}

async function loadAchievements() {
  achCache = await api('/api/achievements');
  const card = document.getElementById('ach-card');
  if (card) renderAchCard(card);
  const grid = document.getElementById('ach-sheet-grid');
  if (grid && !document.getElementById('generic-sheet')?.hidden) openAllAchievements();
}

async function claimAchievement(code, btn) {
  btn.disabled = true;
  try {
    const r = await api(`/api/achievements/${encodeURIComponent(code)}/claim`, { method: 'POST' });
    if (state.user) {
      state.user.balance = r.balance;
      state.user.xp = r.xp;
      state.user.level = r.level;
    }
    const bal = $('#balance-value');
    if (bal) bal.textContent = fmt(r.balance);
    toast(r.already_claimed ? 'Награда уже получена' : `🏅 +${fmt(r.reward_coins)} дыма, +${fmt(r.reward_xp)} XP`);
    await loadAchievements();
  } catch (e) {
    toast(e.message);
    btn.disabled = false;
  }
}

document.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-ach-claim]');
  if (btn) claimAchievement(btn.dataset.achClaim, btn);
});

/* ===== Зал Славы ===== */

const HALL_TABS = [['balance', '💨 Баланс'], ['won', '💰 Выиграно'], ['champions', '🏆 Чемпионы']];
const MEDALS = ['🥇', '🥈', '🥉'];

function hallLeaders(data) {
  const L = data.leaders || [];
  if (!L.length) return '<div class="empty-note">Пока никого нет.</div>';
  const podium = L.slice(0, 3).map((u, i) => `
    <div class="hall-podium-item p${i + 1}${u.is_me ? ' me' : ''}" data-profile="${u.user_id}">
      <div class="hall-medal">${MEDALS[i]}</div>
      <div class="hall-pname">${esc(u.name)}</div>
      <div class="hall-pval">${fmt(u.value)} ${SMOKE}</div>
      ${u.team_name ? `<div class="hall-team">${esc(u.team_name)}</div>` : ''}
    </div>`).join('');
  const rows = L.slice(3).map((u) => `
    <div class="hall-row${u.is_me ? ' me' : ''}" data-profile="${u.user_id}">
      <span class="hall-pos">#${u.position}</span>
      <span class="hall-name">${esc(u.name)}${u.team_name ? `<span class="sub"> · ${esc(u.team_name)}</span>` : ''}</span>
      <span class="hall-val">${fmt(u.value)} ${SMOKE}</span>
    </div>`).join('');
  const me = data.me ? `<div class="hall-row me"><span class="hall-pos">#${data.me.position}</span>
      <span class="hall-name">Ты</span><span class="hall-val">${fmt(data.me.value)} ${SMOKE}</span></div>` : '';
  return `<div class="hall-podium">${podium}</div><div class="hall-list">${rows}${me}</div>`;
}

function hallChampions(data) {
  const C = data.champions || [];
  if (!C.length) return '<div class="empty-note">Чемпионов пока нет — первый сезон ещё идёт.</div>';
  return C.map((ch) => `
    <div class="hall-champ ${esc(ch.kind)}">
      ${ch.logo ? `<img src="${esc(ch.logo)}" alt="">` : '<span class="hall-champ-icon">🛡️</span>'}
      <div class="hall-champ-body">
        <div class="hall-champ-club">${ch.kind === 'cup' ? '🏆' : '🥇'} ${esc(ch.club)}</div>
        <div class="sub">${esc(ch.title)}${ch.owner ? ` · @${esc(ch.owner)}` : ''}${ch.points != null ? ` · ${ch.points} очк.` : ''}</div>
      </div>
    </div>`).join('');
}

async function renderHallTab(body, tab) {
  body.querySelectorAll('[data-hall-tab]').forEach((b) => b.classList.toggle('active', b.dataset.hallTab === tab));
  const out = body.querySelector('#hall-content');
  out.innerHTML = '<div class="empty-note">Загрузка…</div>';
  try {
    const data = await api(`/api/leaderboard/hall?tab=${tab}`);
    out.innerHTML = tab === 'champions' ? hallChampions(data) : hallLeaders(data);
  } catch (e) { out.innerHTML = `<div class="empty-note">${esc(e.message)}</div>`; }
}

function openHall() {
  const body = openSheet('🏆 Зал Славы Лиги', `
    <div class="chips hall-tabs">${HALL_TABS.map(([k, t]) => `<button class="chip" data-hall-tab="${k}">${t}</button>`).join('')}</div>
    <div id="hall-content"></div>`);
  body.querySelectorAll('[data-hall-tab]').forEach((b) => { b.onclick = () => renderHallTab(body, b.dataset.hallTab); });
  renderHallTab(body, 'balance');
}

hooks.profileCards.push((container) => {
  const card = document.createElement('div');
  card.className = 'card';
  card.id = 'ach-card';
  card.innerHTML = '<div class="card-title">🏅 Достижения</div><div class="empty-note">Загрузка…</div>';
  const hallBtn = document.createElement('button');
  hallBtn.className = 'hall-open-btn';
  hallBtn.id = 'hall-open';
  hallBtn.textContent = '🏆 Открыть Зал Славы Лиги';
  hallBtn.onclick = openHall;
  const history = container.querySelector('#bets-history-card');
  if (history) {
    container.insertBefore(card, history);
    history.after(hallBtn);
  } else {
    container.append(card, hallBtn);
  }
  loadAchievements().catch((e) => { card.querySelector('.empty-note').textContent = e.message; });
});

/* ===== тренировки (Клуб) ===== */

function trainingHtml(t) {
  const pct = Math.min(100, Math.round((t.week_total / Math.max(1, t.limit)) * 100));
  const over = t.week_total > t.limit;
  const kinds = Object.entries(t.kinds).map(([k, v]) => `<option value="${esc(k)}">${esc(v)}</option>`).join('');
  const cards = t.cards.map((c) => `<option value="${c.id}">${esc(c.position || '—')} · ${esc(c.name)} (${c.rating})</option>`).join('');
  const top = t.cards.filter((c) => c.total).sort((a, b) => b.total - a.total).slice(0, 5);
  const openV = t.violations.filter((v) => v.status === 'open').length;
  return `
    <div class="card-title">🏋️ Тренировки · fair-play</div>
    <div class="sub">Тренируешь футболистов в самой FC Mobile — здесь только честный учёт. Лимит клуба: ${t.limit} за неделю (с пн).</div>
    <div class="tr-week${over ? ' over' : ''}">
      <div class="tr-week-top"><span>Эта неделя</span><b>${t.week_total} / ${t.limit}</b></div>
      <div class="tr-bar"><div style="width:${pct}%"></div></div>
      ${over ? '<div class="tr-warn">⚠️ Лимит превышен — админ рассмотрит нарушение</div>' : ''}
      ${openV && !over ? `<div class="tr-warn">⚠️ Открытых нарушений: ${openV}</div>` : ''}
    </div>
    ${t.cards.length ? `
    <div class="tr-form">
      <select id="tr-card" class="tr-input">${cards}</select>
      <div class="tr-form-row">
        <select id="tr-kind" class="tr-input">${kinds}</select>
        <input id="tr-count" class="tr-input tr-count" type="number" min="1" max="${t.max_per_entry}" value="1" inputmode="numeric">
      </div>
      <button class="bonus-btn" id="tr-add">Записать тренировку</button>
    </div>` : '<div class="empty-note">В составе нет карточек — записывать нечего.</div>'}
    ${top.length ? `<div class="tr-sub-title">Прокачка по карточкам · всего ${t.club_total}</div>
      ${top.map((c) => `<div class="tr-level-row"><span>${esc(c.name)}</span>
        <span class="tr-lvl">ур. ${c.level}</span><span class="sub">${c.total}${c.next_at ? ` / ${c.next_at}` : ''}</span></div>`).join('')}` : ''}
    ${t.history.length ? `<div class="tr-sub-title">История</div>
      ${t.history.slice(0, 8).map((h) => `<div class="bet-history-item">
        <div><div>${esc(h.card_name)} · ${esc(h.kind_name)}</div><div class="sub">${esc(fmtTime(h.created_at))}</div></div>
        <div class="bh-status open">×${h.count}</div></div>`).join('')}` : ''}`;
}

async function loadTraining(card) {
  try {
    const t = await api('/api/training');
    if (!t.club_id) { card.remove(); return; }
    card.innerHTML = trainingHtml(t);
    const add = card.querySelector('#tr-add');
    if (add) add.onclick = () => addTraining(card, add);
  } catch (e) { card.innerHTML = `<div class="card-title">🏋️ Тренировки</div><div class="empty-note">${esc(e.message)}</div>`; }
}

async function addTraining(card, btn) {
  btn.disabled = true;
  try {
    const r = await api('/api/training', { method: 'POST', body: JSON.stringify({
      card_id: Number(card.querySelector('#tr-card').value),
      kind: card.querySelector('#tr-kind').value,
      count: Number(card.querySelector('#tr-count').value),
    }) });
    toast(r.entry.violation_id
      ? `⚠️ Записано, но лимит превышен: ${r.entry.week_total}/${r.entry.limit}`
      : `✅ Записано: ${r.entry.week_total}/${r.entry.limit} за неделю`, 3500);
    await loadTraining(card);
  } catch (e) { toast(e.message); btn.disabled = false; }
}

hooks.clubCards.push((el, overview) => {
  if (!overview?.club) return;
  const card = document.createElement('div');
  card.className = 'card';
  card.id = 'training-card';
  card.innerHTML = '<div class="card-title">🏋️ Тренировки · fair-play</div><div class="empty-note">Загрузка…</div>';
  el.append(card);
  loadTraining(card);
});

/* ===== админка: нарушения лимита тренировок ===== */

const V_STATUS = { open: 'открыто', resolved: 'решено', penalized: 'наказан' };

async function renderAdminTraining() {
  const sheet = document.querySelector('#admin-sheet .sheet');
  if (!sheet) return;
  document.getElementById('admin-training')?.remove();
  const card = document.createElement('div');
  card.className = 'card';
  card.id = 'admin-training';
  card.innerHTML = '<div class="card-title">🏋️ Нарушения лимита тренировок</div><div class="empty-note">Загрузка…</div>';
  const auditCard = $('#admin-audit')?.closest('.card');
  if (auditCard && auditCard.parentElement === sheet) sheet.insertBefore(card, auditCard);
  else sheet.append(card);
  try {
    const { violations, limit } = await api('/api/admin/training/violations');
    const open = violations.filter((v) => v.status === 'open');
    card.innerHTML = `<div class="card-title">🏋️ Нарушения лимита тренировок
        ${open.length ? `<span class="ach-badge">${open.length}</span>` : ''}</div>
      <div class="sub" style="margin-bottom:6px">Лимит: ${limit} за неделю на клуб (настройка training_limit_per_week)</div>
      ${violations.length ? violations.slice(0, 15).map((v) => `
        <div class="tr-viol ${esc(v.status)}">
          <div class="tr-viol-top"><b>${esc(v.club_name || `клуб #${v.club_id}`)}</b>
            <span class="bh-status ${v.status === 'open' ? 'lost' : 'void'}">${v.total}/${v.limit_value} · ${esc(V_STATUS[v.status] || v.status)}</span></div>
          <div class="sub">Неделя с ${esc(v.period_start)} · ${v.entries.map((e) => `${esc(e.card_name)} ×${e.count}`).join(', ')}</div>
          ${v.admin_note ? `<div class="sub">📝 ${esc(v.admin_note)}</div>` : ''}
          ${v.status === 'open' ? `<div class="tr-viol-actions">
            <button class="chip" data-viol="${v.id}" data-act="resolved">✓ Решено</button>
            <button class="chip" data-viol="${v.id}" data-act="penalized">⚖️ Наказан</button></div>` : ''}
        </div>`).join('') : '<div class="empty-note">Нарушений нет.</div>'}`;
    card.querySelectorAll('[data-viol]').forEach((b) => {
      b.onclick = async () => {
        const note = prompt(b.dataset.act === 'penalized' ? 'Наказание (что назначено):' : 'Комментарий (необязательно):', '');
        if (note === null) return;
        try {
          await api(`/api/admin/training/violations/${b.dataset.viol}/resolve`, {
            method: 'POST', body: JSON.stringify({ action: b.dataset.act, note }) });
          toast('Нарушение закрыто');
          renderAdminTraining();
        } catch (e) { toast(e.message); }
      };
    });
  } catch (e) { card.querySelector('.empty-note').textContent = e.message; }
}

hooks.adminCards.push(() => { renderAdminTraining(); });
