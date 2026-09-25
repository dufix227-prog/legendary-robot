/* Функции оригинала: бомбардиры, события и скрин матча, профиль игрока, «Мои матчи»,
   горячие матчи и движение кэфов в линии, награды капперам, предупреждения о рисках (админ). */
import { $, actions, api, esc, fmt, fmtTime, hooks, odds, openSheet, state, tg, toast } from '../lib.js';

const SMOKE = '🚬';
const pct = (v) => `${Number(v ?? 0).toLocaleString('ru-RU', { maximumFractionDigits: 1 })}%`;
const signed = (v) => `${v > 0 ? '+' : ''}${fmt(v)}`;
const AWARD_ICON = { champion: '👑', top3: '🥈', top10: '🎖' };
const AWARD_NAME = { champion: 'Чемпион дивизиона', top3: 'Призёр сезона', top10: 'Элита сезона' };

/* ===== бомбардиры ===== */

hooks.views['tables:scorers'] = renderScorers;

async function renderScorers() {
  const wrap = $('#scorers-wrap');
  const div = state.divisions.find((d) => d.id == state.currentDivision);
  if (!div) { wrap.innerHTML = '<div class="empty-note">Дивизионов пока нет.</div>'; return; }
  wrap.innerHTML = '<div class="empty-note">Загрузка…</div>';
  let d;
  try { d = await api(`/api/tournaments/${div.tournament_id}/top-scorers?division_id=${div.id}`); } catch (e) {
    wrap.innerHTML = `<div class="empty-note">${esc(e.message)}</div>`; return;
  }
  if (!d.scorers.length) {
    wrap.innerHTML = '<div class="empty-note">Голов со скринов ещё нет — таблица заполнится после первых репортов.</div>';
    return;
  }
  wrap.innerHTML = `<table class="standings scorers">
      <tr><th>#</th><th class="team-th">Игрок</th><th>И</th><th>⚽</th><th>🅰️</th></tr>
      ${d.scorers.map((s) => `<tr><td class="pos">${s.position <= 3 ? ['🥇', '🥈', '🥉'][s.position - 1] : s.position}</td>
        <td><div class="sc-name">${esc(s.name)}<small>${esc(s.club)}</small></div></td>
        <td>${s.matches}</td><td class="pts">${s.goals}</td><td>${s.assists}</td></tr>`).join('')}
    </table>
    ${d.assists.length ? `<div class="card" style="margin-top:12px"><div class="card-title">🅰️ Ассистенты</div>
      ${d.assists.map((a, i) => `<div class="sc-row"><span>${i + 1}. ${esc(a.name)} <small>${esc(a.club)}</small></span><b>${a.assists}</b></div>`).join('')}</div>` : ''}`;
}

document.addEventListener('click', (ev) => {
  if (ev.target.closest('#view-tables [data-div]') && !$('#scorers-wrap').hidden) setTimeout(renderScorers, 0);
});

/* ===== карточка матча: события и скрин ===== */

hooks.matchSheet.push(async (md, data) => {
  const slot = md.querySelector('#match-events-slot');
  if (!slot || data.status !== 'confirmed') return;
  let ev;
  try { ev = await api(`/api/matches/${data.id}/events`); } catch (_) { return; }
  if (state.matchDetail?.id !== data.id) return;
  const side = (s) => (s === 'home' ? data.home?.name : data.away?.name) || '';
  const players = (s) => ev.players.filter((p) => p.side === s);
  const col = (s) => players(s).map((p) => `<div class="ev-p">${esc(p.name)}
      ${p.goals ? `<span>⚽${p.goals > 1 ? `×${p.goals}` : ''}</span>` : ''}${p.assists ? `<span>🅰️${p.assists > 1 ? `×${p.assists}` : ''}</span>` : ''}</div>`).join('')
    || '<div class="sub">—</div>';
  slot.innerHTML = `<div class="card ev-card">
    <div class="card-title">⚡ События матча
      ${ev.has_photo ? `<button class="chip ev-photo" data-match-photo="${data.id}">📷 Скрин</button>` : ''}</div>
    ${ev.goals.length ? ev.goals.map((g) => `<div class="goal-row ${g.side}"><span>${g.minute != null ? `${g.minute}'` : '⚽'} ${esc(g.name)}${g.is_penalty ? ' (пен.)' : ''}</span>
      <span class="sub">${esc(side(g.side))}</span></div>`).join('') : ''}
    ${ev.players.length ? `<div class="ev-cols"><div>${col('home')}</div><div>${col('away')}</div></div>` : ''}
    ${!ev.goals.length && !ev.players.length ? '<div class="sub">Авторов голов на скрине не видно.</div>' : ''}
  </div>`;
});

async function openPhoto(matchId) {
  const initData = tg?.initData || localStorage.getItem('dev_initdata') || '';
  try {
    const r = await fetch(`/api/matches/${matchId}/photo`, { headers: { 'X-Telegram-Init-Data': initData } });
    if (!r.ok) throw new Error('Скрин не найден');
    const url = URL.createObjectURL(await r.blob());
    const body = openSheet('Скрин результата', `<img class="match-photo" src="${url}" alt="Скрин матча">`);
    body.querySelector('img').onload = () => setTimeout(() => URL.revokeObjectURL(url), 60_000);
  } catch (e) { toast(e.message); }
}

/* ===== профиль игрока ===== */

async function openProfile(tgId) {
  let p;
  try { ({ player: p } = await api(`/api/player/${tgId}/public`)); } catch (e) { toast(e.message); return; }
  const s = p.stats;
  openSheet(p.name, `
    <div class="pp-head">
      <div class="pp-rank">${esc(p.rank_title)} · уровень ${p.level}${p.club ? ` · ${p.club.emblem ? esc(p.club.emblem) + ' ' : ''}${esc(p.club.name)}` : ''}</div>
      <div class="sub">${fmt(p.balance)} ${SMOKE} · #${p.balance_rank} по балансу</div>
    </div>
    ${p.awards.length ? `<div class="pp-awards">${p.awards.map((a) => `<span class="pp-award" title="${esc(a.season || '')} · ${esc(a.division || '')}">
      ${AWARD_ICON[a.award] || '🏅'} ${esc(AWARD_NAME[a.award] || a.award)}<small>${esc(a.season || '')}</small></span>`).join('')}</div>` : ''}
    ${s.settled ? `<div class="stat-grid">
        <div class="stat-box"><div class="v ${s.roi >= 0 ? 'pos' : 'neg'}">${s.roi > 0 ? '+' : ''}${pct(s.roi)}</div><div class="k">ROI</div></div>
        <div class="stat-box"><div class="v">${pct(s.hit_rate)}</div><div class="k">проходимость</div></div>
        <div class="stat-box"><div class="v">${odds(s.avg_odds)}</div><div class="k">средний кэф</div></div>
      </div>
      <div class="bs-lines">
        <div><span>Ставок рассчитано</span><b>${s.settled} (в игре ${s.open})</b></div>
        <div><span>Прибыль</span><b class="${s.profit >= 0 ? 'pos' : 'neg'}">${signed(s.profit)} ${SMOKE}</b></div>
        <div><span>Лучшая серия</span><b>${s.best_streak}</b></div>
        ${s.biggest_win ? `<div><span>Лучший выигрыш</span><b>+${fmt(s.biggest_win.profit)} (кэф ${odds(s.biggest_win.odds)})</b></div>` : ''}
        ${s.by_market.length ? `<div><span>Любимый рынок</span><b>${esc(s.by_market[0].group)} · ${pct(s.by_market[0].hit_rate)}</b></div>` : ''}
      </div>` : '<div class="empty-note">Рассчитанных ставок пока нет.</div>'}
    <div class="card-title" style="margin-top:14px">🏅 Достижения <span class="sub">${p.achievements.count}</span></div>
    ${p.achievements.recent.length ? `<div class="pp-ach">${p.achievements.recent.map((a) => `<span class="r-${esc(a.rarity)}" title="${esc(a.name)}">${esc(a.icon)} ${esc(a.name)}</span>`).join('')}</div>`
      : '<div class="sub">Пока нет.</div>'}`);
}

/* ===== «Мои матчи» во вкладке «Клуб» ===== */

const STATE_LABEL = { overdue: '⏰ просрочен', disputed: '⚔️ спор', open: '🟢 тур открыт', upcoming: 'позже' };

hooks.clubCards.unshift(async (root) => {
  let d;
  try { d = await api('/api/cabinet/matches'); } catch (_) { return; }
  if (!d.registered) return;
  const card = document.createElement('div');
  card.className = 'card';
  card.id = 'my-matches-card';
  card.innerHTML = `<div class="card-title">📅 Мои матчи</div>
    ${d.matches.length ? d.matches.map((m) => `<div class="mm-row ${m.state}" data-open="${m.match_id}">
      <div><b>${m.home ? 'дома' : 'в гостях'} · ${esc(m.opponent)}</b>
        <div class="sub">${m.tour_number != null ? `Тур ${m.tour_number}` : esc(m.tournament || 'Кубок')}
        ${m.scheduled_at ? ` · 🕒 ${esc(fmtTime(m.scheduled_at))}` : ''}${m.deadline ? ` · до ${esc(fmtTime(m.deadline))}` : ''}</div></div>
      <span class="mm-state">${STATE_LABEL[m.state]}</span></div>`).join('') : '<div class="empty-note">Несыгранных матчей нет.</div>'}
    ${d.more_upcoming ? `<div class="sub" style="padding-top:6px">и ещё ${d.more_upcoming} в следующих турах</div>` : ''}
    ${d.recent.length ? `<div class="sub" style="margin:10px 0 4px">Последние</div><div class="mm-recent">${d.recent.map((r) =>
      `<span class="rc ${r.result}" data-open="${r.match_id}" title="${esc(r.opponent)}">${esc(r.score)}</span>`).join('')}</div>` : ''}`;
  root.querySelector('#my-matches-card')?.remove();
  const hero = root.querySelector('.club-hero');
  if (hero) hero.after(card); else root.prepend(card);
});

/* ===== линия: горячие матчи и движение кэфов ===== */

let extrasAt = 0;
let extrasOpen = localStorage.getItem('line_extras') !== '0';

async function renderLineExtras(force = false) {
  const box = $('#line-extras');
  if (!box || (!force && Date.now() - extrasAt < 30_000)) return;
  extrasAt = Date.now();
  let hot = [], movers = [];
  try {
    [{ matches: hot }, { movers }] = await Promise.all([api('/api/matches-hot?limit=5'), api('/api/odds/movers?limit=6')]);
  } catch (_) { return; }
  if (!hot.length && !movers.length) { box.innerHTML = ''; return; }
  box.innerHTML = `<div class="card lx-card">
    <button class="lx-toggle" id="lx-toggle">${extrasOpen ? '▾' : '▸'} 🔥 Горячее и движение кэфов</button>
    <div class="lx-body" ${extrasOpen ? '' : 'hidden'}>
      ${hot.length ? `<div class="sub lx-h">Больше всего ставок</div><div class="lx-scroll">${hot.map((h) => `
        <div class="lx-hot" data-open="${h.match_id}"><b>${esc(h.home)} — ${esc(h.away)}</b>
          <span class="sub">${h.bettors} капп. · ${fmt(h.stake)} ${SMOKE}</span>
          <span class="lx-pop">${h.popular.share}% на «${esc(h.popular.label)}»${h.popular.odds ? ` ${odds(h.popular.odds)}` : ''}</span></div>`).join('')}</div>` : ''}
      ${movers.length ? `<div class="sub lx-h">Кэф изменился</div>${movers.map((m) => `
        <div class="lx-mv" data-open="${m.match_id}"><span>${esc(m.home)} — ${esc(m.away)} · <b>${esc(m.label)}</b></span>
          <span class="${m.change_pct < 0 ? 'down' : 'up'}">${odds(m.from)} → ${odds(m.to)} ${m.change_pct < 0 ? '▼' : '▲'}</span></div>`).join('')}` : ''}
    </div></div>`;
}

// линия перерисовывается целиком: обновляем блок сверху не чаще раза в 30 с
hooks.lineCards.push(() => renderLineExtras());

/* ===== кабинет: мои награды ===== */

hooks.profileCards.push(async (root) => {
  if (!state.user?.user_id) return;
  let p;
  try { ({ player: p } = await api(`/api/player/${state.user.user_id}/public`)); } catch (_) { return; }
  if (!p.awards.length) return;
  const card = document.createElement('div');
  card.className = 'card';
  card.id = 'my-awards-card';
  card.innerHTML = `<div class="card-title">🏆 Награды сезонов</div><div class="pp-awards">${p.awards.map((a) => `
    <span class="pp-award">${AWARD_ICON[a.award] || '🏅'} ${esc(AWARD_NAME[a.award] || a.award)}
    <small>${esc(a.season || '')} · ${esc(a.division || '')} · ${a.place} место · +${fmt(a.coins)} ${SMOKE}</small></span>`).join('')}</div>`;
  root.querySelector('#my-awards-card')?.remove();
  root.querySelector('.profile-hero')?.after(card);
});

/* ===== админка: предупреждения о рисках ===== */

const KIND = { liability: '💰 Крупная выплата', one_sided: '⚖️ Перекос', whale: '🐋 Один игрок' };

async function renderRisk(scan = false) {
  let card = $('#admin-risk-card');
  if (!card) {
    card = document.createElement('div');
    card.className = 'card';
    card.id = 'admin-risk-card';
    const sheet = $('#admin-sheet .sheet');
    const after = sheet?.querySelector('.section-title');
    if (after) after.after(card); else sheet?.appendChild(card);
  }
  let alerts;
  try { ({ alerts } = await api(`/api/admin/risk/alerts${scan ? '?scan=1' : ''}`)); } catch (e) { card.remove(); return; }
  card.innerHTML = `<div class="card-title">⚠️ Риски линии ${alerts.length ? `<span class="tr-badge">${alerts.length}</span>` : ''}
      <button class="chip" id="risk-scan" style="margin-left:auto">Проверить</button></div>
    ${alerts.length ? alerts.map((a) => `<div class="risk-row ${esc(a.level)}">
      <div><b>${KIND[a.kind] || esc(a.kind)}</b>${a.level === 'high' ? ' · высокий' : ''}<div class="sub">${esc(a.message)}</div>
        <div class="sub">${esc(fmtTime(a.updated_at))}</div></div>
      <div class="risk-act"><button class="chip" data-open-admin-match="${a.match_id}">Матч</button>
        <button class="chip" data-risk-ack="${a.id}">Принято</button></div></div>`).join('')
      : '<div class="sub">Всё спокойно. Пороги: <code>risk_liability</code>, <code>risk_one_sided_pct</code>, <code>risk_whale_pct</code>, <code>risk_min_stake</code> в настройках.</div>'}`;
}

hooks.adminCards.push(() => renderRisk());

/* ===== события ===== */

document.addEventListener('click', async (ev) => {
  const photo = ev.target.closest('[data-match-photo]');
  if (photo) { openPhoto(photo.dataset.matchPhoto); return; }
  const prof = ev.target.closest('[data-profile]');
  if (prof && prof.dataset.profile && prof.dataset.profile !== 'undefined') { openProfile(prof.dataset.profile); return; }
  if (ev.target.closest('#lx-toggle')) {
    extrasOpen = !extrasOpen;
    localStorage.setItem('line_extras', extrasOpen ? '1' : '0');
    renderLineExtras(true);
    return;
  }
  if (ev.target.closest('#risk-scan')) { renderRisk(true); return; }
  const ack = ev.target.closest('[data-risk-ack]');
  if (ack) {
    try { await api(`/api/admin/risk/alerts/${ack.dataset.riskAck}/ack`, { method: 'POST' }); renderRisk(); } catch (e) { toast(e.message); }
    return;
  }
  const am = ev.target.closest('[data-open-admin-match]');
  if (am) {
    $('#admin-sheet').hidden = true;
    actions.openMatch(Number(am.dataset.openAdminMatch));
  }
});
