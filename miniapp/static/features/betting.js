/* Ставки: черновики купонов, кэшаут, статистика каппера, рейтинги по сезону/дивизиону,
   H2H и аналитика матча, «как получен кэф», подсветка выгодных кэфов (value). */
import { $, actions, api, esc, fmt, fmtTime, haptic, hooks, odds, openSheet, state, toast } from '../lib.js';

const SMOKE = '🚬';

function confirmDialog(text) {
  // как в остальных фичах: window.confirm работает и в Telegram, и в браузере
  return Promise.resolve(window.confirm(text));
}

function legsWord(n) {
  const d = n % 10, dd = n % 100;
  if (d === 1 && dd !== 11) return 'нога';
  if (d >= 2 && d <= 4 && (dd < 12 || dd > 14)) return 'ноги';
  return 'ног';
}

const pct = (v) => `${Number(v ?? 0).toLocaleString('ru-RU', { maximumFractionDigits: 1 })}%`;
const signed = (v) => `${v > 0 ? '+' : ''}${fmt(v)}`;

/* ===== value: радар по линии (кэш 60 с) ===== */

let radar = { at: 0, byMatch: new Map(), loading: null };

function loadRadar() {
  if (Date.now() - radar.at < 60_000) return Promise.resolve(radar.byMatch);
  radar.loading ??= api('/api/intelligence/value?limit=100').then(({ picks }) => {
    const byMatch = new Map();
    for (const p of picks) {
      if (!byMatch.has(p.match_id)) byMatch.set(p.match_id, new Map());
      byMatch.get(p.match_id).set(p.market_code, p);
    }
    radar = { at: Date.now(), byMatch, loading: null };
    return byMatch;
  }).catch(() => { radar.loading = null; return radar.byMatch; });
  return radar.loading;
}

function markValue(root, matchId, picks) {
  if (!picks) return;
  root.querySelectorAll(`.odd-btn[data-match="${matchId}"]`).forEach((b) => {
    const p = picks.get(b.dataset.code);
    if (!p) return;
    b.classList.add('value');
    b.title = `Модель ${pct(p.model_prob)} против ${pct(p.implied_prob)} в кэфе`;
  });
}

hooks.lineCards.push((el, m) => {
  loadRadar().then((byMatch) => {
    const picks = byMatch.get(m.id);
    if (!picks?.size) return;
    markValue(el, m.id, picks);
    const meta = el.querySelector('.mc-meta');
    if (meta && !meta.querySelector('.value-chip')) {
      meta.insertAdjacentHTML('beforeend', `<span class="value-chip">💎 value ×${picks.size}</span>`);
    }
  });
});

/* ===== карточка матча: аналитика, H2H, «как получен кэф» ===== */

const insightsCache = new Map();

function loadInsights(id) {
  const hit = insightsCache.get(id);
  if (hit && Date.now() - hit.at < 60_000) return hit.p;
  const p = api(`/api/matches/${id}/insights`);
  insightsCache.set(id, { at: Date.now(), p });
  p.catch(() => insightsCache.delete(id));
  return p;
}

function statRow(label, a, b, better = 'high') {
  const na = Number(a), nb = Number(b);
  const win = na === nb ? 0 : ((na > nb) === (better === 'high') ? 1 : 2);
  return `<div class="an-row"><span class="${win === 1 ? 'lead' : ''}">${esc(a)}</span>
    <span class="k">${esc(label)}</span><span class="${win === 2 ? 'lead' : ''}">${esc(b)}</span></div>`;
}

function recentHtml(list) {
  if (!list.length) return '<span class="sub">нет матчей</span>';
  return list.map((r) => `<span class="rc ${r.result}" title="${esc(`${r.home ? 'дома' : 'в гостях'} · ${r.opponent}`)}">${esc(r.score)}</span>`).join('');
}

function renderInsights(md, data, ins) {
  md.querySelector('#match-analytics')?.remove();
  const hs = ins.home.stats, as = ins.away.stats, h = ins.h2h;
  const box = document.createElement('div');
  box.id = 'match-analytics';
  box.innerHTML = `
    <div class="an-actions">
      <button class="an-btn" data-explain="${data.id}">ℹ️ Как получен кэф</button>
      ${Object.keys(ins.value || {}).length ? `<span class="value-chip big">💎 Выгодных кэфов: ${Object.keys(ins.value).length}</span>` : ''}
    </div>
    ${ins.insights.length ? `<div class="card an-tips">${ins.insights.map((t) => `<div>${esc(t)}</div>`).join('')}</div>` : ''}
    <div class="card an-card">
      <div class="card-title">📊 Аналитика</div>
      <div class="an-row head"><span>${esc(ins.home.name)}</span><span class="k"></span><span>${esc(ins.away.name)}</span></div>
      ${statRow('Elo', ins.home.elo, ins.away.elo)}
      ${statRow('Матчей', hs.games, as.games)}
      ${statRow('Забивает за игру', hs.gf_avg, as.gf_avg)}
      ${statRow('Пропускает за игру', hs.ga_avg, as.ga_avg, 'low')}
      ${statRow('Обе забили, %', hs.btts_pct, as.btts_pct)}
      ${statRow('ТБ 2.5, %', hs.over25_pct, as.over25_pct)}
      ${statRow('На ноль', hs.clean_sheets, as.clean_sheets)}
      <div class="an-recent"><div>${recentHtml(ins.home.recent)}</div><span class="k">последние</span><div>${recentHtml(ins.away.recent)}</div></div>
    </div>
    <div class="card an-card">
      <div class="card-title">⚔️ Личные встречи</div>
      ${h.total ? `
        <div class="h2h-sum">
          <div><b>${h.home_wins}</b><span>${esc(ins.home.name)}</span></div>
          <div><b>${h.draws}</b><span>ничьи</span></div>
          <div><b>${h.away_wins}</b><span>${esc(ins.away.name)}</span></div>
        </div>
        <div class="sub" style="text-align:center;margin:4px 0 8px">${h.total} встреч · в среднем ${h.avg_goals} гола</div>
        ${h.meetings.map((x) => `<div class="h2h-row"><span>${esc(x.home)}</span><b>${esc(x.score)}</b><span>${esc(x.away)}</span>
          <small>${x.tour_number != null ? `тур ${x.tour_number}` : 'кубок'}${x.played_at ? ` · ${esc(fmtTime(x.played_at))}` : ''}</small></div>`).join('')}`
        : '<div class="empty-note">Клубы ещё не встречались.</div>'}
    </div>`;
  md.appendChild(box);
  markValue(md, data.id, new Map(Object.entries(ins.value || {})));
}

hooks.matchSheet.push((md, data) => {
  if (!data?.home || !data?.away) return;
  loadInsights(data.id).then((ins) => {
    if (state.matchDetail?.id === data.id) renderInsights(md, data, ins);
  }).catch(() => {});
});

async function openExplain(matchId) {
  let e;
  try { e = await api(`/api/matches/${matchId}/odds-explain`); } catch (err) { toast(err.message); return; }
  const rows = e.markets.map((k) => `<tr class="${k.value ? 'is-value' : ''}">
      <td>${esc(k.label)}${k.value ? ' 💎' : ''}</td><td>${k.prob != null ? pct(k.prob) : '—'}</td>
      <td>${k.fair_odds != null ? odds(k.fair_odds) : '—'}</td><td><b>${odds(k.odds)}</b></td></tr>`).join('');
  const mdl = e.model;
  openSheet('Как получен кэф', `
    <div class="ex-steps">
      <div class="ex-step"><b>1. Сила клубов (Elo)</b>
        <div>${esc(e.home)}: <b>${e.elo_home}</b> · ${esc(e.away)}: <b>${e.elo_away}</b> · разница ${signed(e.elo_diff)}</div></div>
      <div class="ex-step"><b>2. Ожидаемые голы</b>
        <div>λ = ${e.base_lambda} × 10<sup>±разница/800</sup> → ${esc(e.home)} <b>${e.lambda_home}</b>, ${esc(e.away)} <b>${e.lambda_away}</b></div></div>
      <div class="ex-step"><b>3. Вероятности</b>
        <div>Пуассон: вероятность каждого счёта 0:0…8:8, из них складываются все рынки.</div></div>
      <div class="ex-step"><b>4. Маржа ${e.margin_pct}%</b>
        <div>кэф = 1 / вероятность / (1 + ${e.margin_pct / 100}), не ниже 1.01.</div></div>
      <div class="ex-step value"><b>💎 Value</b>
        <div>Вторая модель — фактические голы клубов в турнире (сыграно матчей: ${mdl.games}, вес ${Math.round(mdl.weight * 100)}%):
        λ ${mdl.lambda_home} / ${mdl.lambda_away}. Если её вероятность выше заложенной в кэф на ${e.value_edge_pp}+ п.п., кэф подсвечен.</div></div>
    </div>
    <table class="ex-table"><tr><th>Рынок</th><th>Шанс</th><th>Честный</th><th>Кэф</th></tr>${rows}</table>`);
}

/* ===== купон: черновики ===== */

async function renderDrafts(container) {
  let card = container.querySelector('#drafts-card');
  if (!card) {
    card = document.createElement('div');
    card.id = 'drafts-card';
    card.className = 'card drafts-card';
    container.appendChild(card);
  }
  if (state.coupon.length && !container.querySelector('#draft-save')) {
    container.querySelector('.coupon-summary')?.insertAdjacentHTML('beforeend',
      '<button class="draft-save-btn" id="draft-save">💾 Сохранить в черновики</button>');
  }
  let list;
  try { ({ saved_coupons: list } = await api('/api/saved-coupons')); } catch (_) { card.remove(); return; }
  card.innerHTML = `<div class="card-title">🗂 Черновики купонов <span class="sub">${list.length}</span></div>
    ${list.length ? list.map((d) => `<div class="draft-item">
      <div class="draft-main">
        <div><b>${esc(d.name)}</b> · ${d.legs.length} ${legsWord(d.legs.length)}${d.amount ? ` · ${fmt(d.amount)} ${SMOKE}` : ''}</div>
        <div class="sub">${d.legs.map((l) => `<span class="${l.available ? '' : 'gone'}">${esc(l.label)} (${esc(l.match_label)})</span>`).join(' + ')}</div>
        ${d.available_legs ? `<div class="sub">сейчас кэф ${odds(d.total_odds)}${d.available_legs < d.legs.length ? ` · закрыто ног: ${d.legs.length - d.available_legs}` : ''}</div>`
          : '<div class="sub gone">все матчи закрыты</div>'}
      </div>
      <div class="draft-actions">
        <button class="chip" data-draft-load="${d.id}" ${d.available_legs ? '' : 'disabled'}>В купон</button>
        <button class="chip" data-draft-del="${d.id}" aria-label="Удалить">✕</button>
      </div>
    </div>`).join('') : '<div class="empty-note">Собери купон и сохрани его на потом — кэфы подтянутся свежие.</div>'}`;
  card._drafts = list;
}

hooks.couponCards.push((container) => { renderDrafts(container); });

async function saveDraft() {
  const name = (await promptName())?.trim();
  if (name === undefined) return;
  const amount = Number($('#coupon-amount')?.value) || 0;
  try {
    await api('/api/saved-coupons', { method: 'POST', body: JSON.stringify({
      name, amount, legs: state.coupon.map((l) => ({ match_id: l.match_id, market_code: l.market_code })),
    }) });
    toast('Черновик сохранён');
    actions.renderCoupon();
  } catch (e) { toast(e.message); }
}

function promptName() {
  return new Promise((resolve) => {
    const body = openSheet('Сохранить черновик', `
      <input class="amount-input" id="draft-name" maxlength="40" placeholder="Название, например «Вечерний экспресс»" style="font-size:15px">
      <button class="place-btn" id="draft-name-ok">Сохранить</button>`);
    const sheet = document.getElementById('generic-sheet');
    const input = body.querySelector('#draft-name');
    input.focus();
    const done = (v) => { sheet.hidden = true; resolve(v); };
    body.querySelector('#draft-name-ok').onclick = () => done(input.value);
    input.onkeydown = (e) => { if (e.key === 'Enter') done(input.value); };
    const watch = new MutationObserver(() => { if (sheet.hidden) { watch.disconnect(); resolve(undefined); } });
    watch.observe(sheet, { attributes: true, attributeFilter: ['hidden'] });
  });
}

function loadDraft(d) {
  const legs = d.legs.filter((l) => l.available).map((l) => ({
    match_id: l.match_id, market_code: l.market_code, label: l.label, odds: l.odds, match_label: l.match_label,
  }));
  state.coupon = legs;
  state.idemKey = null;
  if (d.amount) {
    localStorage.setItem('coupon_amount', String(d.amount));
    const input = $('#coupon-amount');  // купон берёт сумму из открытого поля, если оно есть
    if (input) input.value = d.amount;
  }
  actions.saveCoupon();
  actions.renderAll();
  haptic();
  toast(legs.length < d.legs.length ? `В купоне ${legs.length} из ${d.legs.length}: остальные матчи закрыты` : 'Черновик в купоне');
}

/* ===== кабинет: кэшаут, статистика, рейтинги ===== */

async function renderOpenBets(root) {
  let card = root.querySelector('#cashout-card');
  let preds, quotes;
  try {
    [{ predictions: preds }, { quotes }] = await Promise.all([
      api('/api/predictions?status=open&limit=20'), api('/api/cashout/quotes'),
    ]);
  } catch (_) { return; }
  if (!preds.length) { card?.remove(); return; }
  if (!card) {
    card = document.createElement('div');
    card.id = 'cashout-card';
    card.className = 'card';
    const hist = root.querySelector('#bets-history-card');
    if (hist) hist.before(card); else root.appendChild(card);
  }
  card.innerHTML = `<div class="card-title">💸 Открытые купоны</div>
    ${preds.map((b) => {
      const q = quotes[String(b.id)] || {};
      return `<div class="co-item">
        <div><div>#${b.id} · ${fmt(b.amount)} × ${odds(b.total_odds)} → ${fmt(b.potential_win)} ${SMOKE}</div>
          <div class="sub">${esc(b.legs_label || '')}</div></div>
        ${q.available
          ? `<button class="co-btn" data-cashout="${b.id}" data-amount="${q.amount}">Кэшаут<br><b>${fmt(q.amount)}</b></button>`
          : `<span class="co-na" title="${esc(q.reason || '')}">${esc(q.reason || 'в игре')}</span>`}
      </div>`;
    }).join('')}
    <div class="sub" style="margin-top:6px">Кэшаут — пока тур открыт: честная стоимость купона по текущим шансам минус небольшая комиссия.</div>`;
}

async function doCashout(btn) {
  const id = Number(btn.dataset.cashout);
  let amount = Number(btn.dataset.amount);
  if (!await confirmDialog(`Забрать ${fmt(amount)} дыма по купону #${id}? Купон закроется.`)) return;
  btn.disabled = true;
  try {
    let r;
    try {
      r = await api(`/api/predictions/${id}/cashout`, { method: 'POST', body: JSON.stringify({ amount }) });
    } catch (e) {
      if (e.code !== 'CASHOUT_CHANGED') throw e;
      const q = await api(`/api/predictions/${id}/cashout-quote`);
      if (!q.available) throw new Error(q.reason);
      if (!await confirmDialog(`Сумма изменилась: теперь ${fmt(q.amount)}. Забрать?`)) return;
      amount = q.amount;
      r = await api(`/api/predictions/${id}/cashout`, { method: 'POST', body: JSON.stringify({ amount }) });
    }
    state.user.balance = r.balance;
    $('#balance-value').textContent = fmt(r.balance);
    haptic('medium');
    toast(`+${fmt(r.amount)} дыма — купон #${id} выкуплен`);
    actions.renderProfile();
  } catch (e) {
    toast(e.message);
  } finally {
    btn.disabled = false;
  }
}

async function renderStats(root) {
  let s;
  try { s = await api('/api/profile/bet-stats'); } catch (_) { return; }
  const card = document.createElement('div');
  card.className = 'card';
  card.id = 'bet-stats-card';
  card.innerHTML = `<div class="card-title">📈 Моя статистика</div>
    ${s.settled ? `
      <div class="stat-grid">
        <div class="stat-box"><div class="v ${s.roi >= 0 ? 'pos' : 'neg'}">${s.roi > 0 ? '+' : ''}${pct(s.roi)}</div><div class="k">ROI</div></div>
        <div class="stat-box"><div class="v">${pct(s.hit_rate)}</div><div class="k">проходимость</div></div>
        <div class="stat-box"><div class="v">${odds(s.avg_odds)}</div><div class="k">средний кэф</div></div>
      </div>
      <div class="bs-lines">
        <div><span>Прибыль</span><b class="${s.profit >= 0 ? 'pos' : 'neg'}">${signed(s.profit)} ${SMOKE}</b></div>
        <div><span>Поставлено / вернулось</span><b>${fmt(s.staked)} / ${fmt(s.returned)}</b></div>
        <div><span>Выигрыши / проигрыши / возвраты / кэшауты</span><b>${s.won} / ${s.lost} / ${s.void} / ${s.cashout}</b></div>
        <div><span>Ординары / экспрессы</span><b>${s.singles} / ${s.expresses}</b></div>
        <div><span>Серия побед: сейчас / лучшая</span><b>${s.current_streak} / ${s.best_streak}</b></div>
        ${s.biggest_win ? `<div><span>Лучший выигрыш</span><b>+${fmt(s.biggest_win.profit)} (кэф ${odds(s.biggest_win.odds)})</b></div>` : ''}
      </div>
      ${s.by_market.length ? `<div class="sub" style="margin:10px 0 6px">По рынкам (ноги)</div>
        ${s.by_market.map((g) => `<div class="bm-row"><span>${esc(g.group)}</span>
          <div class="bm-bar"><div style="width:${g.hit_rate}%"></div></div><b>${g.won}/${g.won + g.lost}</b></div>`).join('')}` : ''}`
      : `<div class="empty-note">Статистика появится после первой рассчитанной ставки${s.open ? ` (в игре: ${s.open})` : ''}.</div>`}`;
  const before = root.querySelector('#cashout-card') || root.querySelector('#bets-history-card');
  root.querySelector('#bet-stats-card')?.remove();
  before ? before.before(card) : root.appendChild(card);
}

const lbState = { scope: 'season', id: null, scopes: null };

async function renderLeaderboard(root) {
  let card = root.querySelector('#bettors-card');
  if (!card) {
    card = document.createElement('div');
    card.id = 'bettors-card';
    card.className = 'card';
    root.appendChild(card);
  }
  try { lbState.scopes ??= await api('/api/leaderboard/scopes'); } catch (_) { card.remove(); return; }
  const { seasons, divisions } = lbState.scopes;
  const options = lbState.scope === 'season'
    ? seasons.map((s) => ({ id: s.id, name: s.name + (s.stage === 'finished' ? ' ✓' : '') }))
    : divisions.map((d) => ({ id: d.id, name: divisions.some((x) => x.tournament_id !== d.tournament_id) ? `${d.tournament_name} · ${d.name}` : d.name }));
  if (!options.some((o) => o.id === lbState.id)) {
    const mine = lbState.scope === 'division' ? options.find((o) => o.id === state.currentDivision) : null;
    lbState.id = (mine || options[0])?.id ?? null;
  }
  card.innerHTML = `<div class="card-title">🏆 Рейтинг капперов</div>
    <div class="tabs lb-tabs">
      <button class="tab ${lbState.scope === 'season' ? 'active' : ''}" data-lb-scope="season">По сезону</button>
      <button class="tab ${lbState.scope === 'division' ? 'active' : ''}" data-lb-scope="division">По дивизиону</button>
    </div>
    <div class="chips lb-chips">${options.map((o) => `<button class="chip ${o.id === lbState.id ? 'active' : ''}" data-lb-id="${o.id}">${esc(o.name)}</button>`).join('')
      || '<span class="sub">Пока нет сезонов.</span>'}</div>
    <div id="lb-body"><div class="empty-note">Загрузка…</div></div>`;
  if (lbState.id == null) { card.querySelector('#lb-body').innerHTML = ''; return; }
  const param = lbState.scope === 'season' ? 'tournament_id' : 'division_id';
  try {
    const data = await api(`/api/leaderboard/${lbState.scope}?${param}=${lbState.id}`);
    const row = (r, me) => `<div class="lb-row ${me ? 'me' : ''}">
      <span class="pos">${r.position <= 3 ? ['🥇', '🥈', '🥉'][r.position - 1] : r.position}</span>
      <span class="nm">${esc(r.name)}<small>${r.bets} ст. · ${pct(r.hit_rate)} · кэф ${odds(r.avg_odds)}</small></span>
      <span class="pr ${r.profit >= 0 ? 'pos' : 'neg'}">${signed(r.profit)}<small>ROI ${pct(r.roi)}</small></span></div>`;
    card.querySelector('#lb-body').innerHTML = data.leaders.length
      ? data.leaders.map((r) => row(r, data.me && r.user_id === data.me.user_id)).join('')
        + (data.me && !data.leaders.some((r) => r.user_id === data.me.user_id) ? `<div class="lb-sep">…</div>${row(data.me, true)}` : '')
      : '<div class="empty-note">Рассчитанных ставок на эти матчи пока нет.</div>';
  } catch (e) { card.querySelector('#lb-body').innerHTML = `<div class="empty-note">${esc(e.message)}</div>`; }
}

hooks.profileCards.push((root) => {
  renderStats(root);
  renderOpenBets(root);
  renderLeaderboard(root);
});

/* ===== события ===== */

document.addEventListener('click', (ev) => {
  const ex = ev.target.closest('[data-explain]');
  if (ex) { openExplain(Number(ex.dataset.explain)); return; }
  if (ev.target.closest('#draft-save')) { saveDraft(); return; }
  const load = ev.target.closest('[data-draft-load]');
  if (load) {
    const d = document.getElementById('drafts-card')?._drafts?.find((x) => x.id === Number(load.dataset.draftLoad));
    if (d) loadDraft(d);
    return;
  }
  const del = ev.target.closest('[data-draft-del]');
  if (del) {
    api(`/api/saved-coupons/${del.dataset.draftDel}`, { method: 'DELETE' })
      .then(() => { toast('Черновик удалён'); actions.renderCoupon(); })
      .catch((e) => toast(e.message));
    return;
  }
  const co = ev.target.closest('[data-cashout]');
  if (co) { doCashout(co); return; }
  const sc = ev.target.closest('[data-lb-scope]');
  if (sc) { lbState.scope = sc.dataset.lbScope; lbState.id = null; renderLeaderboard($('#profile-body')); return; }
  const lid = ev.target.closest('[data-lb-id]');
  if (lid) { lbState.id = Number(lid.dataset.lbId); renderLeaderboard($('#profile-body')); }
});

// после ставки/расчёта радар и аналитика устаревают
document.addEventListener('visibilitychange', () => { if (!document.hidden) { radar.at = 0; insightsCache.clear(); } });
