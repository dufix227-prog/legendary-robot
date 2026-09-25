/* KURILKA SIGARKI — SPA ядра: линия, таблицы, купон, кабинет, клуб. Валюта «дым». */

import {
  $, actions, api, esc, fmt, fmtTime, formLetters, haptic, hooks, logoHtml, odds, runHooks, setBadge, showLockdown, state, tg, toast,
} from './lib.js';
import './features/index.js';

const EXACT_SCORES = ['1_0', '2_0', '2_1', '3_0', '3_1', '3_2', '0_0', '1_1', '2_2', '0_1', '0_2', '1_2', '0_3', '1_3', '2_3'];
const MARKET_GROUPS = [
  ['Исход', ['1x2_p1', '1x2_x', '1x2_p2']],
  ['Двойной шанс', ['dc_1x', 'dc_12', 'dc_x2']],
  ['Тотал', ['tb15', 'tm15', 'tb25', 'tm25', 'tb35', 'tm35', 'tb45', 'tm45']],
  ['Обе забьют', ['btts_yes', 'btts_no']],
  ['Забьёт', ['itb_h05', 'itb_a05']],
  ['Индивидуальный тотал', ['itb_h15', 'itb_a15']],
  ['Фора', ['ah_h15', 'ah_a15']],
  ['Точный счёт', EXACT_SCORES.map((s) => `cs_${s}`)],
  ['Серия', ['tie_2_0', 'tie_2_1', 'tie_1_2', 'tie_0_2']],
];

/* ===== купон (локальное состояние) ===== */

function couponAdd(match, market) {
  if (state.frozen) return;
  const same = state.coupon.findIndex((l) => l.match_id === match.id);
  if (same >= 0 && state.coupon[same].market_code === market.code) {
    state.coupon.splice(same, 1);
  } else if (same >= 0) {
    state.coupon[same] = {
      match_id: match.id, market_code: market.code, label: market.label, odds: market.odds,
      match_label: `${match.home.name} — ${match.away.name}`,
    };
  } else {
    const limits = state.user?.bet_limits || {};
    if (state.coupon.length >= (limits.max_legs || 5)) {
      toast(`Экспресс — максимум ${limits.max_legs || 5} ног`);
      return;
    }
    state.coupon.push({
      match_id: match.id, market_code: market.code, label: market.label, odds: market.odds,
      match_label: `${match.home.name} — ${match.away.name}`,
    });
  }
  saveCoupon();
  renderAll();
}

function saveCoupon() {
  localStorage.setItem('coupon', JSON.stringify(state.coupon));
}

function couponTotals(amount) {
  const odds = state.coupon.reduce((acc, l) => acc * l.odds, 1);
  const maxPayout = state.user?.bet_limits?.max_payout || 10000;
  const raw = Math.floor(amount * odds);
  const payout = Math.min(raw, maxPayout);
  return { odds, raw, payout, trimmed: raw > maxPayout };
}

/* ===== рендеры ===== */

function renderHeader() {
  $('#balance-value').textContent = fmt(state.user?.balance);
}

function renderLine() {
  const wrap = $('#line-matches');
  const tours = [...new Set(state.lineMatches.map((m) => m.tour_number ?? '—'))];
  $('#line-tours').innerHTML = tours.length > 1
    ? [null, ...tours].map((t) => `<button class="chip ${state.currentTour === t ? 'active' : ''}" data-tour="${t ?? ''}">${t === null ? 'Все' : (t === '—' ? 'Кубки' : `Тур ${esc(t)}`)}</button>`).join('')
    : '';
  if (!state.lineMatches.length) {
    wrap.innerHTML = '<div class="empty-note">Открытых матчей нет — линия появится, когда root откроет тур.</div>';
    return;
  }
  const visible = state.lineMatches.filter((m) => state.currentTour === null || (m.tour_number ?? '—') === state.currentTour);
  let lastGroup = null;
  wrap.innerHTML = visible.map((m) => {
    const isFav = state.favorites.has(m.id);
    const main = m.markets.filter((k) => ['1x2_p1', '1x2_x', '1x2_p2'].includes(k.code));
    const sel = (code) => state.coupon.find((l) => l.match_id === m.id && l.market_code === code);
    const group = `${m.tournament_name || ''} · ${m.tour_number != null ? `Тур ${m.tour_number}` : 'Кубок'}`;
    const dl = m.deadline ? ` · до ${fmtTime(m.deadline)}` : '';
    const header = group !== lastGroup ? `<div class="group-title">${esc(group + dl)}</div>` : '';
    lastGroup = group;
    return `${header}<div class="match-card" data-open="${m.id}">
      <div class="mc-top">
        <span class="mc-meta">${m.markets.length > 3 ? `+${m.markets.length - 3} рынков` : 'Основные рынки'}</span>
        <button class="fav-star ${isFav ? 'on' : ''}" data-fav="${m.id}">${isFav ? '★' : '☆'}</button>
      </div>
      <div class="mc-teams">
        <div class="mc-team">${logoHtml(m.home)}<span>${esc(m.home?.name || '—')}</span></div>
        ${m.status === 'confirmed' ? `<div class="mc-score">${m.score_home}:${m.score_away}</div>` : '<div class="mc-score vs">VS</div>'}
        <div class="mc-team right"><span>${esc(m.away?.name || '—')}</span>${logoHtml(m.away)}</div>
      </div>
      <div class="mc-markets">
        ${main.map((k) => `<button class="odd-btn ${sel(k.code) ? 'selected' : ''}" data-match="${m.id}" data-code="${k.code}">
            <span class="lbl">${esc(k.label)}</span><span class="val">${odds(k.odds)}</span>
          </button>`).join('')}
      </div>
    </div>`;
  }).join('');
  if (hooks.lineCards.length) {
    for (const el of wrap.querySelectorAll('[data-open]')) {
      const m = visible.find((x) => x.id === Number(el.dataset.open));
      if (m) runHooks(hooks.lineCards, el, m);
    }
  }
}

function renderTables() {
  // чипы дивизионов
  const chips = $('#division-chips');
  const many = new Set(state.divisions.map((d) => d.tournament_id)).size > 1;
  chips.innerHTML = state.divisions.map((d) =>
    `<button class="chip ${state.currentDivision == d.id ? 'active' : ''}" data-div="${d.id}">${esc(many && d.tournament_name ? `${d.tournament_name} · ${d.name}` : d.name)}</button>`).join('')
    || '<span class="sub">Дивизионов пока нет.</span>';

  const sw = $('#standings-wrap');
  sw._data = sw._data || {};
  if (sw._currentDiv !== state.currentDivision) {
    sw._currentDiv = state.currentDivision;
    sw.innerHTML = '<div class="empty-note">Загружаю таблицу…</div>';
    api(`/api/standings?division_id=${state.currentDivision}`)
      .then(({ standings, zones }) => {
        sw._data[state.currentDivision] = { standings, zones };
        sw.innerHTML = standingsTable(standings, zones);
      })
      .catch((e) => { sw.innerHTML = `<div class="empty-note">${esc(e.message)}</div>`; });
  } else if (sw._data[state.currentDivision]) {
    const cached = sw._data[state.currentDivision];
    sw.innerHTML = standingsTable(cached.standings, cached.zones);
  }
}

function standingsTable(rows, zones = { up: 0, down: 0 }) {
  if (!rows?.length) return '<div class="empty-note">В дивизионе ещё нет клубов.</div>';
  // зоны приходят с сервера: у высшего дивизиона нет повышения, у низшего — вылета
  const up = zones?.up || 0, down = zones?.down || 0;
  return `<table class="standings">
    <tr><th>#</th><th class="team-th">Клуб</th><th>И</th><th>В</th><th>Н</th><th>П</th><th>М</th><th>О</th></tr>
    ${rows.map((r) => `<tr class="${r.position <= up ? 'promo' : (down && r.position > rows.length - down ? 'releg' : '')}">
      <td class="pos">${r.position}</td>
      <td><div class="team-cell">${logoHtml(r)}<span>${esc(r.name)}</span></div></td>
      <td>${r.games}</td><td>${r.wins}</td><td>${r.draws}</td><td>${r.losses}</td>
      <td class="gd">${r.gf}:${r.ga}</td><td class="pts">${r.points}</td>
    </tr>`).join('')}
  </table>
  ${up || down ? `<div class="table-legend">${up ? '<span><i style="background:var(--green)"></i>Повышение</span>' : ''}${down ? '<span><i style="background:var(--red)"></i>Вылет</span>' : ''}</div>` : ''}`;
}

function renderCoupon() {
  const body = $('#coupon-body');
  if (!state.coupon.length) {
    body.innerHTML = '<div class="coupon-empty"><div class="big">🧾</div>Купон пуст.<br>Выбери исход на линии 🔥</div>';
    runHooks(hooks.couponCards, body);
    return;
  }
  const limits = state.user?.bet_limits || {};
  const min = limits.min_bet ?? 10, max = limits.max_bet ?? 50000;
  const amount = couponAmount();
  const t = couponTotals(amount);
  body.innerHTML = `
    ${state.coupon.map((l, i) => `<div class="coupon-leg">
      <div><div class="leg-main">${esc(l.label)}</div><div class="leg-sub">${esc(l.match_label)}</div></div>
      <div class="leg-right">
        <span class="leg-odd">${odds(l.odds)}</span>
        <button class="leg-remove" data-rm="${i}" aria-label="Убрать">✕</button>
      </div>
    </div>`).join('')}
    <div class="coupon-summary">
      <div class="coupon-row"><span class="k">${state.coupon.length > 1 ? `Экспресс · ${state.coupon.length} ${legsWord(state.coupon.length)}` : 'Ординар'}</span><span>кэф ${odds(t.odds)}</span></div>
      <input class="amount-input" id="coupon-amount" type="number" inputmode="numeric" min="${min}" max="${max}" value="${amount}" placeholder="Сумма">
      <div class="quick-amounts">
        ${[50, 100, 500].map((v) => `<button data-quick="${v}">+${v}</button>`).join('')}
        <button data-quick="max">Макс</button>
      </div>
      <div class="coupon-row"><span class="k">Лимиты</span><span class="sub">${fmt(min)}–${fmt(max)} 🚬</span></div>
      <div class="coupon-row total"><span>Выплата</span><span id="coupon-payout">${fmt(t.payout)} 🚬</span></div>
      <div class="coupon-row" id="coupon-trim" ${t.trimmed ? '' : 'hidden'}><span class="trim">Обрезано по лимиту выплаты (${fmt(limits.max_payout)})</span></div>
      <button class="place-btn" id="place-bet" ${state.placing ? 'disabled' : ''}>${state.placing ? 'Ставим…' : 'Поставить'}</button>
    </div>`;
  runHooks(hooks.couponCards, body);
}

function legsWord(n) {
  const d = n % 10, dd = n % 100;
  if (d === 1 && dd !== 11) return 'событие';
  if (d >= 2 && d <= 4 && (dd < 12 || dd > 14)) return 'события';
  return 'событий';
}

function couponAmount() {
  const el = $('#coupon-amount');
  const limits = state.user?.bet_limits || {};
  if (el) return Number(el.value);
  return Number(localStorage.getItem('coupon_amount')) || (limits.min_bet ?? 10);
}

function refreshCouponTotals() {
  const t = couponTotals(couponAmount());
  const p = $('#coupon-payout');
  if (p) p.textContent = `${fmt(t.payout)} 🚬`;
  const tr = $('#coupon-trim');
  if (tr) tr.hidden = !t.trimmed;
  updateBetbar();
}

function renderProfile() {
  api('/api/progression').then((p) => {
    const pct = Math.min(100, Math.round((p.xp / Math.max(1, p.xp_needed)) * 100));
    $('#profile-body').innerHTML = `
      <div class="profile-hero">
        <div class="ph-top">
          <div class="avatar">${state.user?.photo_url ? `<img src="${esc(state.user.photo_url)}" alt="">` : '👤'}</div>
          <div>
            <div class="ph-name">${esc(state.user?.first_name || 'Игрок')}</div>
            <div class="ph-rank">${esc(p.rank)} · уровень ${p.level}</div>
          </div>
        </div>
        <div class="xp-bar"><div class="xp-fill" style="width:${pct}%"></div></div>
        <div class="xp-label"><span>${fmt(p.xp)} XP</span><span>до уровня ${p.level + 1}: ${fmt(Math.max(0, p.xp_needed - p.xp))} XP</span></div>
        <div class="stat-grid">
          <div class="stat-box"><div class="v">${fmt(p.balance)}</div><div class="k">дым</div></div>
          <div class="stat-box"><div class="v">${fmt(p.bets_count)}</div><div class="k">ставок</div></div>
          <div class="stat-box"><div class="v">${fmt(p.total_won)}</div><div class="k">выиграно</div></div>
        </div>
      </div>
      <div class="card">
        <div class="card-title">🎁 Бонус за серию входов</div>
        <div class="sub" style="margin-bottom:10px">Серия: ${p.streak_days} дн. · каждый день бонус растёт</div>
        <button class="bonus-btn" id="streak-btn">Забрать бонус</button>
      </div>
      <div class="card">
        <div class="card-title">🎟 Промокод</div>
        <div class="promo-row">
          <input class="amount-input" id="promo-code" placeholder="КОД" autocomplete="off" style="margin:0;font-size:15px">
          <button class="bonus-btn" id="promo-btn" style="width:auto;padding:12px 18px">Ввести</button>
        </div>
      </div>
      <div class="card" id="bets-history-card">
        <div class="card-title">📜 История ставок</div>
        <div class="empty-note">Загрузка…</div>
      </div>`;
    $('#streak-btn').onclick = () => claimStreak();
    $('#promo-btn').onclick = () => applyPromo();
    hooks.profileCards.forEach((fn) => { try { fn($('#profile-body'), p); } catch (e) { console.error(e); } });
    api('/api/predictions?limit=10').then(({ predictions }) => {
      const el = $('#bets-history-card');
      el.querySelector('.empty-note')?.remove();
      el.insertAdjacentHTML('beforeend', predictions.length ? predictions.map((b) => `
        <div class="bet-history-item">
          <div><div>#${b.id} · ${b.bet_type === 'express' ? `Экспресс ×${b.legs}` : 'Ординар'} · ${fmt(b.amount)} × ${odds(b.total_odds)}</div>
          <div class="sub">${esc(b.legs_label || '')}</div></div>
          <div class="bh-status ${esc(b.status)}">${({won: '+' + fmt(b.potential_win), lost: 'проигрыш', void: 'возврат', open: 'в игре', cashout: 'кэшаут +' + fmt(b.cashout_amount)})[b.status] || esc(b.status)}</div>
        </div>`).join('') : '<div class="empty-note">Ставок ещё нет.</div>');
    }).catch(() => {});
  }).catch((e) => { $('#profile-body').innerHTML = `<div class="empty-note">${esc(e.message)}</div>`; });
}

function renderClub() {
  api('/api/cabinet/overview').then((data) => {
    const el = $('#club-body');
    if (!data.club) {
      el.innerHTML = `
        <div class="card club-hero">
          <div style="font-size:40px">🛡️</div>
          <div class="club-name">Клуба нет</div>
          <div class="club-division" style="margin-bottom:14px">Попроси root выдать клуб — или оставь заявку</div>
          ${data.club_request === 'pending'
            ? '<button class="req-btn" disabled>Заявка отправлена — ждём</button>'
            : '<button class="req-btn" id="req-club">Запросить клуб</button>'}
        </div>`;
      $('#req-club')?.addEventListener('click', requestClub);
      return;
    }
    const cl = data.club;
    el.innerHTML = `
      <div class="card club-hero">
        ${logoHtml(cl)}
        <div class="club-name">${esc(cl.name)}</div>
        <div class="club-division">${esc(cl.division || '—')} · Elo ${cl.elo}</div>
        <div class="form-letters" style="justify-content:center;margin-top:10px">
          ${formLetters(cl.form) || '<span class="sub">форма не набрана</span>'}
        </div>
      </div>
      <div class="card"><div class="stat-grid two">
        <div class="stat-box"><div class="v">${fmt(cl.budget)}</div><div class="k">бюджет ₼</div></div>
        <div class="stat-box"><div class="v">${cl.squad_size}</div><div class="k">карт в составе</div></div>
      </div></div>
      <div class="card" id="squad-card"><div class="card-title">Состав</div><div class="empty-note">Загрузка…</div></div>`;
    hooks.clubCards.forEach((fn) => { try { fn(el, data); } catch (e) { console.error(e); } });
    api('/api/cabinet/squad').then(({ squad }) => {
      const sc = $('#squad-card');
      sc.querySelector('.empty-note')?.remove();
      sc.insertAdjacentHTML('beforeend', squad.length ? squad.map((p) => `
        <div class="squad-item">
          <div><span class="squad-pos">${esc(p.position || '—')}</span>${esc(p.name)}</div>
          <div class="squad-rating">${p.rating}</div>
        </div>`).join('') : '<div class="empty-note">Состав пуст — подпиши свободных агентов во вкладке «Рынок».</div>');
    }).catch(() => { $('#squad-card .empty-note')?.replaceChildren('Не удалось загрузить состав'); });
  }).catch((e) => { $('#club-body').innerHTML = `<div class="empty-note">${esc(e.message)}</div>`; });
}

/* ===== карточка матча ===== */

async function openMatch(matchId) {
  let data;
  try { data = await api(`/api/matches/${matchId}`); } catch (e) { toast(e.message); return; }
  // в карточке матча API не отдаёт название турнира — берём из линии
  data.tournament_name ??= state.lineMatches.find((m) => m.id === data.id)?.tournament_name;
  state.matchDetail = data;
  renderMatchDetail();
  $('#match-sheet').hidden = false;
}

function renderMatchDetail() {
  const data = state.matchDetail;
  if (!data) return;
  const md = $('#match-detail');
  const hist = {};
  for (const h of data.odds_history || []) {
    (hist[h.market_code] = hist[h.market_code] || []).push(h.odds);
  }
  const moves = data.markets
    .filter((k) => (hist[k.code] || []).length >= 2)
    .map((k) => `${esc(k.label)}: ${hist[k.code].map(odds).join(' → ')}`);
  const sel = (code) => state.coupon.some((l) => l.match_id === data.id && l.market_code === code);
  const byCode = Object.fromEntries(data.markets.map((k) => [k.code, k]));
  const known = new Set(MARKET_GROUPS.flatMap(([, codes]) => codes));
  const groups = [...MARKET_GROUPS, ['Другие', data.markets.map((k) => k.code).filter((c) => !known.has(c))]]
    .map(([title, codes]) => [title, codes.map((c) => byCode[c]).filter(Boolean)])
    .filter(([, list]) => list.length);
  const pending = data.status === 'pending';
  md.innerHTML = `
    <div class="sub" style="text-align:center;margin-bottom:12px;font-weight:700">${esc([data.tournament_name, data.tour_number != null ? `Тур ${data.tour_number}` : 'Кубок'].filter(Boolean).join(' · '))}</div>
    <div class="mc-teams">
      <div class="mc-team">${logoHtml(data.home)}<span>${esc(data.home?.name || '—')}</span></div>
      ${data.status === 'confirmed'
        ? `<div class="mc-score">${data.score_home}:${data.score_away}${data.pens_home != null ? `<span class="pens">пен. ${data.pens_home}:${data.pens_away}</span>` : ''}</div>`
        : '<div class="mc-score vs">VS</div>'}
      <div class="mc-team right"><span>${esc(data.away?.name || '—')}</span>${logoHtml(data.away)}</div>
    </div>
    ${(data.home?.form || data.away?.form) ? `<div class="form-row">
      <div class="form-letters">${formLetters(data.home?.form)}</div>
      <span class="sub">форма</span>
      <div class="form-letters">${formLetters(data.away?.form)}</div>
    </div>` : ''}
    ${data.goals?.length ? `<div class="card" style="margin-top:14px">${data.goals.map((g) => `
      <div class="goal-row"><span>${g.minute != null ? g.minute + "'" : '•'} ${esc(g.raw_name)}</span>
      <span class="sub">${g.side === 'home' ? esc(data.home?.name || 'дома') : esc(data.away?.name || 'гости')}</span></div>`).join('')}</div>` : ''}
    ${groups.length ? groups.map(([title, list]) => `<div class="markets-group">
      <div class="sub">${esc(title)}</div>
      <div class="mc-markets">
        ${list.map((k) => `<button class="odd-btn ${sel(k.code) ? 'selected' : ''}" data-match="${data.id}" data-code="${esc(k.code)}" ${pending ? '' : 'disabled'}>
          <span class="lbl">${esc(k.label)}</span><span class="val">${odds(k.odds)}</span></button>`).join('')}
      </div>
    </div>`).join('') : '<div class="empty-note">Рынков нет.</div>'}
    ${moves.length ? `<div class="odds-move" style="margin-top:14px">📈 Движение кэфов: ${moves.join(' · ')}</div>` : ''}
  `;
  runHooks(hooks.matchSheet, md, data);
}

/* ===== действия ===== */

async function claimStreak() {
  try {
    const r = await api('/api/bonus/streak', { method: 'POST' });
    state.user.balance = r.balance;
    renderHeader();
    toast(`+${r.amount} дыма! Серия: ${r.streak_days} дн.`);
    renderProfile();
  } catch (e) { toast(e.message); }
}

async function applyPromo() {
  const code = $('#promo-code')?.value.trim();
  if (!code) return toast('Введи код');
  try {
    const r = await api('/api/promo/redeem', { method: 'POST', body: JSON.stringify({ code }) });
    state.user.balance = r.balance;
    renderHeader();
    toast(`+${r.amount} дыма по промокоду!`);
  } catch (e) { toast(e.message); }
}

async function requestClub() {
  try {
    const r = await api('/api/club-request', { method: 'POST' });
    toast(r.status === 'pending' ? 'Заявка отправлена root — ждём' : 'Заявка уже есть: ' + r.status);
    renderClub();
  } catch (e) { toast(e.message); }
}

async function placeBet() {
  if (state.placing) return;
  const amount = couponAmount();
  const limits = state.user?.bet_limits || {};
  if (!amount || amount < (limits.min_bet ?? 10)) return toast(`Минимум ${fmt(limits.min_bet ?? 10)} дыма`);
  if (amount > (limits.max_bet ?? 50000)) return toast(`Максимум ${fmt(limits.max_bet ?? 50000)} дыма`);
  if (amount > (state.user?.balance ?? 0)) return toast('Недостаточно дыма');
  // один ключ на купон, пока он не принят: двойной тап / ретрай после сети не спишет дважды
  const sig = JSON.stringify([amount, state.coupon.map((l) => [l.match_id, l.market_code])]);
  if (!state.idemKey || state.idemKey.sig !== sig) {
    state.idemKey = { sig, key: crypto.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}` };
  }
  state.placing = true;
  renderCoupon();
  try {
    const r = await api('/api/predictions', {
      method: 'POST',
      body: JSON.stringify({ amount, selections: state.coupon, idempotency_key: state.idemKey.key }),
    });
    if (r.balance != null) state.user.balance = r.balance;
    else refreshWallet();
    state.coupon = [];
    state.idemKey = null;
    saveCoupon();
    localStorage.setItem('coupon_amount', String(amount));
    renderHeader();
    renderLine();
    haptic('medium');
    toast(r.duplicate ? 'Эта ставка уже принята' : `Ставка #${r.bet_id} принята! 🔥`);
  } catch (e) {
    if (e.code === 'ODDS_CHANGED') {
      await refreshLine();
      toast('Кэф изменился — купон обновлён, проверь и поставь ещё раз');
    } else toast(e.message);
  } finally {
    state.placing = false;
    renderCoupon();
    updateBetbar();
  }
}

async function refreshWallet() {
  try {
    const w = await api('/api/wallet');
    state.user.balance = w.balance;
    renderHeader();
  } catch (_) { /* не критично */ }
}

async function refreshLine() {
  try {
    const line = await api('/api/line');
    state.lineMatches = line.matches;
    // подтягиваем свежие кэфы в купон
    for (const l of state.coupon) {
      const k = state.lineMatches.find((m) => m.id === l.match_id)?.markets.find((x) => x.code === l.market_code);
      if (k) l.odds = k.odds;
    }
    saveCoupon();
    renderLine();
  } catch (_) { /* покажем старую линию */ }
}

/* ===== избранное ===== */

async function toggleFav(matchId, btn) {
  const on = state.favorites.has(matchId);
  try {
    if (on) {
      await api(`/api/favorites/${matchId}`, { method: 'DELETE' });
      state.favorites.delete(matchId);
    } else {
      await api('/api/favorites', { method: 'POST', body: JSON.stringify({ match_id: matchId }) });
      state.favorites.add(matchId);
    }
    localStorage.setItem('favorites', JSON.stringify([...state.favorites]));
    btn.textContent = on ? '☆' : '★';
    btn.classList.toggle('on', !on);
  } catch (e) { toast(e.message); }
}

/* ===== уведомления ===== */

async function openNotifications() {
  let data;
  try { data = await api('/api/notifications'); } catch (e) { toast(e.message); return; }
  $('#notif-list').innerHTML = data.notifications.length
    ? data.notifications.map((n) => `<div class="notif-item ${n.is_read ? '' : 'unread'}">
        <div class="n-text">${esc(n.text)}</div><div class="n-time">${esc(fmtTime(n.created_at))}</div></div>`).join('')
    : '<div class="empty-note">Пока пусто.</div>';
  $('#notif-sheet').hidden = false;
  api('/api/notifications/read', { method: 'POST' }).then(() => setBadge(0)).catch(() => {});
}

/* ===== betbar ===== */

function updateBetbar() {
  const bar = $('#betbar');
  const view = document.querySelector('.view.active')?.id;
  if (!state.coupon.length || view !== 'view-line') { bar.hidden = true; return; }
  const t = couponTotals(couponAmount());
  $('#betbar-count').textContent = state.coupon.length;
  $('#betbar-odds').textContent = odds(t.odds);
  bar.hidden = false;
}

/* ===== переключение экранов ===== */

function showView(name) {
  document.querySelectorAll('.view').forEach((v) => v.classList.remove('active'));
  $(`#view-${name}`).classList.add('active');
  document.querySelectorAll('.nav-btn').forEach((b) => b.classList.toggle('active', b.dataset.view === name));
  window.scrollTo(0, 0);
  updateBetbar();
  if (name === 'tables') renderTables();
  if (name === 'coupon') renderCoupon();
  if (name === 'profile') renderProfile();
  if (name === 'club') renderClub();
  hooks.views[name]?.();
}

function renderAll() {
  renderHeader();
  renderLine();
  renderCoupon();
  updateBetbar();
}

// для фич: перерисовка после внешней правки купона (черновики), баланс, переходы
Object.assign(actions, { renderAll, renderCoupon, renderProfile, refreshWallet, refreshLine, showView, saveCoupon });

/* ===== init ===== */

async function init() {
  $('#line-matches').innerHTML = '<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>';
  try {
    const boot = await api('/api/bootstrap');
    state.user = boot.user;
    state.divisions = boot.divisions;
    state.currentDivision = boot.divisions[0]?.id ?? null;
    document.documentElement.dataset.theme = localStorage.getItem('theme') || 'emerald';

    const [line, favs] = await Promise.all([
      api('/api/line'),
      api('/api/favorites'),
    ]);
    state.lineMatches = line.matches;
    state.favorites = new Set(favs.favorites);
    // купон из localStorage мог устареть: убираем ноги на закрытые матчи, освежаем кэфы
    state.coupon = state.coupon.filter((l) => state.lineMatches.some((m) => m.id === l.match_id));
    for (const l of state.coupon) {
      const m = state.lineMatches.find((x) => x.id === l.match_id);
      const k = m.markets.find((x) => x.code === l.market_code);
      if (k) { l.odds = k.odds; l.label = k.label; }
      l.match_label = `${m.home?.name || '—'} — ${m.away?.name || '—'}`;
    }
    saveCoupon();
    const notif = await api('/api/notifications');
    setBadge(notif.unread);

    renderAll();
  } catch (e) {
    $('#line-matches').innerHTML = '';
    if (e.status !== 401) toast('Не удалось загрузиться: ' + e.message);
    else $('#line-matches').innerHTML = '<div class="empty-note">Открой мини-апп из бота в Telegram — так мы узнаем, кто ты.</div>';
  }
}

/* ===== события ===== */

document.addEventListener('click', (ev) => {
  const nav = ev.target.closest('.nav-btn');
  if (nav) return showView(nav.dataset.view);

  const fav = ev.target.closest('[data-fav]');
  if (fav) return toggleFav(Number(fav.dataset.fav), fav);

  const odd = ev.target.closest('[data-match][data-code]');
  if (odd) {
    const m = state.lineMatches.find((x) => x.id === Number(odd.dataset.match))
      || (state.matchDetail?.id === Number(odd.dataset.match) ? state.matchDetail : null);
    const k = m?.markets.find((x) => x.code === odd.dataset.code);
    if (m && k) { haptic(); couponAdd(m, k); renderMatchDetail(); }
    return;
  }

  const card = ev.target.closest('[data-open]');
  if (card && !ev.target.closest('button')) {
    openMatch(Number(card.dataset.open));
    return;
  }

  const tourChip = ev.target.closest('[data-tour]');
  if (tourChip) {
    const v = tourChip.dataset.tour;
    state.currentTour = v === '' ? null : (Number.isNaN(Number(v)) ? v : Number(v));
    renderLine();
    return;
  }

  const quick = ev.target.closest('[data-quick]');
  if (quick) {
    const input = $('#coupon-amount');
    const limits = state.user?.bet_limits || {};
    const cap = Math.min(limits.max_bet ?? 50000, state.user?.balance ?? 0);
    input.value = quick.dataset.quick === 'max' ? cap : Math.min(cap, (Number(input.value) || 0) + Number(quick.dataset.quick));
    localStorage.setItem('coupon_amount', input.value);
    refreshCouponTotals();
    return;
  }

  const rm = ev.target.closest('[data-rm]');
  if (rm) {
    state.coupon.splice(Number(rm.dataset.rm), 1);
    saveCoupon(); renderCoupon(); renderLine(); updateBetbar();
    return;
  }

  const chip = ev.target.closest('[data-div]');
  if (chip) {
    state.currentDivision = Number(chip.dataset.div);
    $('#standings-wrap')._currentDiv = null;
    renderTables();
    return;
  }
});

$('#betbar').addEventListener('click', () => showView('coupon'));
$('#bell-btn').addEventListener('click', openNotifications);
$('#notif-close').addEventListener('click', () => { $('#notif-sheet').hidden = true; });
$('#match-close').addEventListener('click', () => { $('#match-sheet').hidden = true; });
$('#match-sheet').addEventListener('click', (e) => { if (e.target.id === 'match-sheet') $('#match-sheet').hidden = true; });

document.addEventListener('click', (ev) => {
  if (ev.target.id === 'place-bet') placeBet();
});
document.addEventListener('input', (ev) => {
  if (ev.target.id === 'coupon-amount') {
    localStorage.setItem('coupon_amount', ev.target.value);
    refreshCouponTotals();
  }
});

// вкладки таблиц
$('#tabs-tables')?.addEventListener('click', (ev) => {
  const tab = ev.target.closest('.tab');
  if (!tab) return;
  document.querySelectorAll('#tabs-tables .tab').forEach((t) => t.classList.remove('active'));
  tab.classList.add('active');
  const isStandings = tab.dataset.tab === 'standings';
  $('#standings-wrap').hidden = !isStandings;
  $('#results-wrap').hidden = isStandings;
  if (!isStandings) {
    api(`/api/results?division_id=${state.currentDivision}`).then(({ results }) => {
      $('#results-wrap').innerHTML = results.length ? results.map((m) => `
        <div class="match-card" data-open="${m.id}" style="margin-bottom:8px">
          <div class="mc-top"><span>${m.tour_number != null ? `Тур ${m.tour_number}` : 'Кубок'}</span><span class="mc-status">${({confirmed: 'сыгран', disputed: 'спор'})[m.status] || m.status}</span></div>
          <div class="mc-teams">
            <div class="mc-team">${logoHtml(m.home)}<span>${esc(m.home?.name || '')}</span></div>
            <div class="mc-score">${m.score_home}:${m.score_away}${m.pens_home != null ? `<span class="pens">пен. ${m.pens_home}:${m.pens_away}</span>` : ''}</div>
            <div class="mc-team right"><span>${esc(m.away?.name || '')}</span>${logoHtml(m.away)}</div>
          </div>
        </div>`).join('') : '<div class="empty-note">Сыгранных матчей ещё нет.</div>';
    }).catch((e) => { $('#results-wrap').innerHTML = `<div class="empty-note">${esc(e.message)}</div>`; });
  }
});

init();
