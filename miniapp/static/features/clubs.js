/* Клубы: цвета/эмблема/девиз/стадион, доход со стадиона и спонсоров, журнал; время матча. */
import { actions, api, esc, fmt, fmtTime, hooks, state, toast } from '../lib.js';

const MONEY = '₼';
const money = (v) => `${v < 0 ? '−' : ''}${fmt(Math.abs(v))} ${MONEY}`;
const KIND = { stadium: '🏟 Стадион', sponsor: '🤝 Спонсор', sponsor_sign: '✍️ Подпись', stadium_upgrade: '🏗 Апгрейд' };
const isColor = (c) => /^#[0-9a-f]{6}$/i.test(c || '');

/* ===== линия и таблицы: цвета клубов, время матча ===== */

hooks.lineCards.push((el, m) => {
  const c1 = isColor(m.home?.color1) ? m.home.color1 : null;
  const c2 = isColor(m.away?.color1) ? m.away.color1 : null;
  if (c1 || c2) {
    el.classList.add('club-colored');
    el.style.setProperty('--home-c', c1 || 'transparent');
    el.style.setProperty('--away-c', c2 || 'transparent');
  }
  if (m.scheduled_at) {
    el.querySelector('.mc-meta')?.insertAdjacentHTML('afterbegin', `<span class="kickoff">🕒 ${esc(fmtTime(m.scheduled_at))}</span>`);
  }
});

/* ===== карточка матча: время ===== */

function toLocalInput(utc) {
  if (!utc) return '';
  const d = new Date(utc.replace(' ', 'T') + 'Z');
  if (Number.isNaN(d.getTime())) return '';
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

hooks.matchSheet.push((md, data) => {
  if (!data?.home || !data?.away) return;
  const stadium = data.home?.stadium_name;
  const box = document.createElement('div');
  box.className = 'kickoff-box';
  box.innerHTML = `<div class="kickoff-line">🕒 <span id="ko-text">${data.scheduled_at ? esc(fmtTime(data.scheduled_at)) : 'время не назначено'}</span>
    ${stadium ? `<span class="sub">· 🏟 ${esc(stadium)}</span>` : ''}</div>`;
  md.querySelector('.mc-teams')?.after(box);
  if (data.status !== 'pending') return;
  api(`/api/matches/${data.id}/schedule`).then((s) => {
    if (!s.can_edit || state.matchDetail?.id !== data.id) return;
    box.insertAdjacentHTML('beforeend', `<div class="kickoff-edit">
      <input type="datetime-local" id="ko-input" value="${toLocalInput(s.scheduled_at)}">
      <button class="chip" id="ko-save">Сохранить</button>
      ${s.scheduled_at ? '<button class="chip" id="ko-clear" aria-label="Снять время">✕</button>' : ''}
    </div><div class="sub">Сопернику придёт уведомление. На приём ставок время не влияет.</div>`);
    const send = async (value) => {
      try {
        const r = await api(`/api/matches/${data.id}/schedule`, { method: 'POST', body: JSON.stringify({ scheduled_at: value }) });
        data.scheduled_at = r.scheduled_at;
        const lm = state.lineMatches.find((x) => x.id === data.id);
        if (lm) lm.scheduled_at = r.scheduled_at;
        box.querySelector('#ko-text').textContent = r.scheduled_at ? fmtTime(r.scheduled_at) : 'время не назначено';
        toast(r.scheduled_at ? 'Время матча сохранено' : 'Время снято');
      } catch (e) { toast(e.message); }
    };
    box.querySelector('#ko-save').onclick = () => {
      const v = box.querySelector('#ko-input').value;
      if (!v) return toast('Выбери дату и время');
      send(new Date(v).toISOString());
    };
    box.querySelector('#ko-clear')?.addEventListener('click', () => send(''));
  }).catch(() => {});
});

/* ===== вкладка «Клуб» ===== */

function applyHeroColors(root, p) {
  const hero = root.querySelector('.club-hero');
  if (!hero) return;
  if (isColor(p.color1)) {
    hero.classList.add('club-colored-hero');
    hero.style.setProperty('--c1', p.color1);
    hero.style.setProperty('--c2', isColor(p.color2) ? p.color2 : p.color1);
  }
  if (p.motto && !hero.querySelector('.club-motto')) {
    hero.insertAdjacentHTML('beforeend', `<div class="club-motto">«${esc(p.motto)}»</div>`);
  }
}

function economyHtml(e) {
  const st = e.stadium;
  const sp = e.sponsor;
  return `
    <div class="card-title">🏟 ${esc(e.profile.stadium_name || 'Стадион')} <span class="sub">ур. ${st.level}/${st.max_level}</span></div>
    <div class="stat-grid">
      <div class="stat-box"><div class="v">${fmt(st.capacity)}</div><div class="k">мест</div></div>
      <div class="stat-box"><div class="v">${fmt(Math.round(st.income.D / 1000))}к</div><div class="k">за дом. матч</div></div>
      <div class="stat-box"><div class="v">${fmt(Math.round(e.totals.stadium / 1_000_000))}м</div><div class="k">заработано</div></div>
    </div>
    <div class="sub" style="margin:8px 0">Победа ×1.25, ничья ×1, поражение ×0.8 — ${money(st.income.W)} / ${money(st.income.D)} / ${money(st.income.L)}</div>
    ${st.next ? (e.can_manage
      ? `<button class="bonus-btn" id="stadium-up" ${e.budget < st.next.cost ? 'disabled' : ''}>Расширить до ${fmt(st.next.capacity)} мест · ${money(st.next.cost)}</button>`
      : `<div class="sub">Следующий уровень: ${fmt(st.next.capacity)} мест за ${money(st.next.cost)}</div>`)
      : '<div class="sub">Стадион максимального уровня.</div>'}
    <div class="card-title" style="margin-top:16px">🤝 Спонсор сезона</div>
    ${sp ? `<div class="sponsor-cur"><b>${esc(sp.name)}</b><div class="sub">${esc(sp.tagline)}</div>
        <div class="sub">подписан ${esc(fmtTime(sp.signed_at))} · заработано ${money(e.totals.sponsor)}</div></div>`
      : (e.season_id == null ? '<div class="sub">Клуб не в дивизионе текущей лиги — спонсора выбрать нельзя.</div>'
        : `<div class="sub" style="margin-bottom:8px">${e.can_manage ? 'Выбери одного на сезон — сменить до конца сезона нельзя.' : 'Владелец ещё не выбрал спонсора.'}</div>
        <div class="sponsor-list">${e.sponsors.map((s) => `<div class="sponsor-opt">
          <div><b>${esc(s.name)}</b><div class="sub">${esc(s.tagline)}</div>
            <div class="sponsor-terms">${s.sign ? `подпись ${money(s.sign)} · ` : ''}${s.match ? `матч ${money(s.match)} · ` : ''}${s.win ? `победа +${money(s.win)} · ` : ''}${s.draw ? `ничья +${money(s.draw)}` : ''}</div></div>
          ${e.can_manage ? `<button class="chip" data-sponsor="${esc(s.code)}">Выбрать</button>` : ''}
        </div>`).join('')}</div>`)}
    ${e.income_enabled ? '' : '<div class="sub" style="margin-top:8px">⏸ Доходы клубов сейчас выключены админом.</div>'}
    ${e.ledger.length ? `<div class="card-title" style="margin-top:16px">📒 Доходы и расходы</div>
      ${e.ledger.map((l) => `<div class="ledger-row"><span>${KIND[l.kind] || esc(l.kind)}<small>${esc(l.note || '')}</small></span>
        <b class="${l.amount >= 0 ? 'pos' : 'neg'}">${l.amount >= 0 ? '+' : ''}${money(l.amount)}</b></div>`).join('')}` : ''}`;
}

function profileForm(e) {
  const p = e.profile;
  return `<div class="card-title">🎨 Оформление клуба</div>
    <div class="cp-grid">
      <label>Эмблема<input id="cp-emblem" maxlength="8" placeholder="🦁" value="${esc(p.emblem || '')}"></label>
      <label>Основной цвет<input id="cp-color1" type="color" value="${esc(isColor(p.color1) ? p.color1 : '#2ecc8f')}" ${isColor(p.color1) ? 'data-initial="1"' : ''}></label>
      <label>Второй цвет<input id="cp-color2" type="color" value="${esc(isColor(p.color2) ? p.color2 : '#ffffff')}" ${isColor(p.color2) ? 'data-initial="1"' : ''}></label>
    </div>
    <label class="cp-wide">Стадион<input id="cp-stadium" maxlength="40" placeholder="Название стадиона" value="${esc(p.stadium_name || '')}"></label>
    <label class="cp-wide">Девиз<input id="cp-motto" maxlength="60" placeholder="Девиз клуба" value="${esc(p.motto || '')}"></label>
    <div class="cp-actions">
      <button class="bonus-btn" id="cp-save">Сохранить</button>
      <button class="chip" id="cp-reset-colors">Сбросить цвета</button>
    </div>
    <div class="sub">Эмблема — 1–2 эмодзи, видна в линии, таблицах и карточке матча вместо логотипа.</div>`;
}

async function renderEconomy(root, overview) {
  const clubId = overview?.club?.id;
  if (!clubId) return;
  let e;
  try { e = await api(`/api/clubs/${clubId}/economy`); } catch (_) { return; }
  applyHeroColors(root, e.profile);
  let card = root.querySelector('#club-economy');
  if (!card) {
    card = document.createElement('div');
    card.id = 'club-economy';
    card.className = 'card';
    // сразу под карточкой бюджета: деньги клуба рядом с тем, откуда они берутся
    const budgetCard = root.querySelector('.club-hero')?.nextElementSibling;
    if (budgetCard) budgetCard.after(card); else root.appendChild(card);
  }
  card.innerHTML = economyHtml(e);
  card._club = clubId;
  let form = root.querySelector('#club-profile-form');
  if (e.can_manage) {
    if (!form) {
      form = document.createElement('div');
      form.id = 'club-profile-form';
      form.className = 'card';
      card.after(form);
    }
    form.innerHTML = profileForm(e);
    form._club = clubId;
  } else form?.remove();
}

hooks.clubCards.push((root, overview) => { renderEconomy(root, overview); });

async function clubPost(path, body) {
  const r = await api(path, { method: 'POST', body: JSON.stringify(body || {}) });
  actions.showView('club');  // бюджет в шапке клуба тоже поменялся — перерисовать вкладку
  return r;
}

// цветовой инпут всегда имеет значение: отправляем цвет, только если его трогали или он уже задан
document.addEventListener('input', (ev) => {
  if (ev.target.matches?.('#cp-color1, #cp-color2')) ev.target.dataset.touched = '1';
});

document.addEventListener('click', async (ev) => {
  const card = document.getElementById('club-economy');
  const clubId = card?._club;
  if (ev.target.closest('#stadium-up') && clubId) {
    if (!window.confirm('Расширить стадион? Деньги спишутся из бюджета клуба.')) return;
    try { await clubPost(`/api/clubs/${clubId}/stadium/upgrade`); toast('Стадион расширен 🏗'); } catch (e) { toast(e.message); }
    return;
  }
  const sp = ev.target.closest('[data-sponsor]');
  if (sp && clubId) {
    if (!window.confirm('Подписать спонсора на весь сезон?')) return;
    try { await clubPost(`/api/clubs/${clubId}/sponsor`, { code: sp.dataset.sponsor }); toast('Контракт подписан ✍️'); } catch (e) { toast(e.message); }
    return;
  }
  if (ev.target.closest('#cp-reset-colors') && clubId) {
    try { await clubPost(`/api/clubs/${clubId}/profile`, { color1: '', color2: '' }); toast('Цвета сброшены'); } catch (e) { toast(e.message); }
    return;
  }
  if (ev.target.closest('#cp-save') && clubId) {
    const v = (id) => document.getElementById(id)?.value ?? '';
    const body = { emblem: v('cp-emblem'), stadium_name: v('cp-stadium'), motto: v('cp-motto') };
    for (const key of ['color1', 'color2']) {
      const el = document.getElementById(`cp-${key}`);
      if (el?.dataset.touched || el?.dataset.initial) body[key] = el.value;
    }
    try {
      await clubPost(`/api/clubs/${clubId}/profile`, body);
      toast('Оформление сохранено');
    } catch (e) { toast(e.message); }
  }
});
