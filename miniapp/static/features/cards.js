/* Карточки игроков: характеристики, добавление (фото → OCR, RenderZ, вручную), лист карточки,
   очередь проверки. Хелперы cardTile/openCardSheet переиспользует рынок (transfers.js). */
import { $, api, esc, fmt, haptic, hooks, openSheet, state, toast } from '../lib.js';

export const POSITIONS = [
  ['GK', 'вратарь'], ['CB', 'центр. защитник'], ['LB', 'левый защитник'], ['RB', 'правый защитник'],
  ['LWB', 'левый вингбек'], ['RWB', 'правый вингбек'], ['CDM', 'опорный п/з'], ['CM', 'центр. п/з'],
  ['CAM', 'атакующий п/з'], ['LM', 'левый п/з'], ['RM', 'правый п/з'], ['LW', 'левый вингер'],
  ['RW', 'правый вингер'], ['CF', 'оттянутый форвард'], ['ST', 'нападающий'],
];
export const POS_RU = Object.fromEntries(POSITIONS);
// старые карточки сида хранят русские сокращения
const RU2EN = { ВРТ: 'GK', ЦЗ: 'CB', ЛЗ: 'LB', ПЗ: 'RB', ЛАЗ: 'LWB', ПАЗ: 'RWB', ЦОП: 'CDM', ЦП: 'CM', ЦАП: 'CAM', ЛП: 'LM', ПП: 'RM', ЛВ: 'LW', ПВ: 'RW', ФРВ: 'CF', НАП: 'ST' };
export const posCode = (p) => (p ? RU2EN[String(p).toUpperCase()] || String(p).toUpperCase() : '');
export const STATS = [['pac', 'PAC', 'скорость'], ['sho', 'SHO', 'удар'], ['pas', 'PAS', 'пас'], ['dri', 'DRI', 'дриблинг'], ['def', 'DEF', 'защита'], ['phy', 'PHY', 'физика']];
const TEXT_FIELDS = [['nation', 'Сборная'], ['league', 'Лига'], ['real_club', 'Клуб (реальный)'], ['program', 'Программа / событие']];

export function tier(ovr) {
  const n = Number(ovr) || 0;
  if (n >= 110) return 'tier-icon';
  if (n >= 100) return 'tier-gold';
  if (n >= 90) return 'tier-silver';
  return 'tier-bronze';
}

/* сервер может отдать статы плоско (pac…) или в stats{}; статус проверки — verify_status */
export function normCard(c = {}) {
  const s = c.stats || {};
  const stats = {};
  STATS.forEach(([k]) => { stats[k] = c[k] ?? s[k] ?? s[k.toUpperCase()] ?? null; });
  let alt = c.alt_positions ?? [];
  if (typeof alt === 'string') {
    // сырые строки club_cards: JSON-массив или «LW,ST»
    try { alt = alt.trim().startsWith('[') ? JSON.parse(alt) : alt.split(/[,\s]+/); } catch (_) { alt = []; }
    alt = alt.filter(Boolean);
  }
  let checks = c.checks ?? {};
  if (typeof checks === 'string') { try { checks = JSON.parse(checks) || {}; } catch (_) { checks = {}; } }
  return {
    ...c,
    id: c.card_id ?? c.id,
    rating: c.rating ?? c.ovr ?? null,
    position: posCode(c.position),
    alt: alt.map(posCode),
    stats,
    checks,
    height: c.height_cm ?? c.height ?? null,
    vstatus: c.verify_status ?? 'approved',
  };
}

const VERIFY = {
  approved: ['✅', 'проверена', 'ok'],
  pending: ['⏳', 'на проверке', 'wait'],
  rejected: ['❌', 'отклонена', 'bad'],
};
export const verifyBadge = (st, withText = false) => {
  const [ico, txt, cls] = VERIFY[st] || VERIFY.pending;
  return `<span class="ct-verify ${cls}" title="${txt}">${ico}${withText ? ` ${txt}` : ''}</span>`;
};
const hasStats = (c) => STATS.some(([k]) => c.stats[k] != null);
const stars = (n) => (n ? '★'.repeat(n) + '<i>' + '★'.repeat(Math.max(0, 5 - n)) + '</i>' : '—');
const plural = (n, one, few, many) => {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  return m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? few : many;
};

/* компактная строка карточки: opts.right — цена/кнопки, opts.sub — доп. строка, opts.meta — хвост строки позиций */
export function cardTile(raw, opts = {}) {
  const c = normCard(raw);
  const pos = c.position || '—';
  const alt = c.alt.filter((p) => p && p !== pos);
  const badge = c.image_url
    ? `<div class="ct-thumb ${tier(c.rating)}"><img src="${esc(c.image_url)}" alt="" loading="lazy"><b>${esc(c.rating ?? '—')}</b></div>`
    : `<div class="ct-ovr ${tier(c.rating)}"><b>${esc(c.rating ?? '—')}</b><span>${esc(pos)}</span></div>`;
  const statsLine = hasStats(c)
    ? `<div class="ct-stats">${STATS.map(([k, lbl]) => `<span><i>${lbl}</i>${esc(c.stats[k] ?? '–')}</span>`).join('')}</div>`
    : '';
  const open = opts.noOpen || !c.id ? '' : ` data-card-open="${esc(c.id)}"`;
  return `<div class="lot-card ct-tile ${opts.cls || ''}"${open}${opts.attrs || ''}>
    ${badge}
    <div class="lot-main">
      <div class="lot-name ct-name"><span>${esc(c.name)}</span>${opts.noVerify ? '' : verifyBadge(c.vstatus)}</div>
      <div class="lot-sub ct-pos"><b>${esc(pos)}</b>${alt.length ? ` · ${alt.map(esc).join(', ')}` : ''}${opts.meta ? ` · ${opts.meta}` : ''}</div>
      ${statsLine}${opts.sub || ''}
    </div>
    ${opts.right ? `<div class="ct-right">${opts.right}</div>` : ''}
  </div>`;
}

/* ===== сеть ===== */

const initData = () => window.Telegram?.WebApp?.initData || localStorage.getItem('dev_initdata') || '';

// api() из lib.js ставит JSON Content-Type — для multipart свой вызов, boundary выставит браузер
async function postForm(endpoint, form) {
  const res = await fetch(endpoint, { method: 'POST', body: form, headers: { 'X-Telegram-Init-Data': initData() } });
  let data = null;
  try { data = await res.json(); } catch (_) { /* html/proxy */ }
  if (!res.ok || !data || typeof data !== 'object') {
    const err = new Error(data?.error || data?.message || `Ошибка сервера (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return data;
}

function openLink(url) {
  const tgw = window.Telegram?.WebApp;
  if (tgw?.openLink && tgw.initData) tgw.openLink(url);
  else window.open(url, '_blank', 'noopener');
}

function fmtUtc(s) {
  const d = s ? new Date(String(s).replace(' ', 'T') + (/[zZ]|[+-]\d\d:?\d\d$/.test(s) ? '' : 'Z')) : null;
  return d && !isNaN(d) ? d.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) : (s || '');
}

/* ===== лист карточки ===== */

const cs = { id: null, extra: '', card: null };

function bigCard(c) {
  if (c.image_url) {
    return `<div class="cs-visual"><img class="cs-photo" src="${esc(c.image_url)}" alt="${esc(c.name)}"></div>`;
  }
  const st = STATS.map(([k, lbl]) => `<div><b>${esc(c.stats[k] ?? '–')}</b><span>${lbl}</span></div>`).join('');
  return `<div class="cs-visual"><div class="fc-card ${tier(c.rating)}">
    <div class="fc-top"><div class="fc-ovr">${esc(c.rating ?? '—')}</div><div class="fc-pos">${esc(c.position || '—')}</div></div>
    ${c.program ? `<div class="fc-prog">${esc(c.program)}</div>` : ''}
    <div class="fc-silhouette" aria-hidden="true"></div>
    <div class="fc-name">${esc(c.name)}</div>
    <div class="fc-stats">${st}</div>
    <div class="fc-meta">${[c.nation, c.real_club].filter(Boolean).map(esc).join(' · ')}</div>
  </div></div>`;
}

function verifyBlock(c) {
  const ch = c.checks || {};
  const rzUrl = c.renderz_url;
  const row = (ok, label, detail = '') => `<div class="cs-vrow"><span class="cs-vico ${ok === true ? 'ok' : ok === false ? 'bad' : ''}">${ok === true ? '✓' : ok === false ? '✗' : '—'}</span><span>${label}</span>${detail ? `<span class="sub">${detail}</span>` : ''}</div>`;
  let rzRow;
  if (!rzUrl) rzRow = row(null, 'RenderZ', 'ссылки нет');
  else if (ch.renderz === 'match') rzRow = row(true, 'RenderZ', 'имя, OVR и позиция совпали');
  else if (ch.renderz === 'partial') rzRow = row(null, 'RenderZ', 'имя и OVR совпали, позиция не прочиталась');
  else if (ch.renderz === 'mismatch') {
    const lbl = { name: 'имя', rating: 'OVR', position: 'позиция' };
    const bad = Object.entries(ch.renderz_match || {}).filter(([k, m]) => !['all', 'partial', 'name_score'].includes(k) && m === false).map(([k]) => lbl[k] || k);
    rzRow = row(false, 'RenderZ', `не совпало${bad.length ? `: ${esc(bad.join(', '))}` : ''}`);
  } else rzRow = row(null, 'RenderZ', 'не проверено');
  const ocrTxt = ch.ocr === true ? 'совпало с фото' : ch.ocr === false ? 'данные расходятся с фото' : 'не распознавалось';
  return `<div class="group-title">Проверка</div>
    <div class="cs-verify">
      <div class="cs-vstatus">${verifyBadge(c.vstatus, true)}${c.verified_at ? `<span class="sub">· ${esc(fmtUtc(c.verified_at))}</span>` : ''}</div>
      ${c.verify_note ? `<div class="cs-vnote">💬 ${esc(c.verify_note)}</div>` : ''}
      ${row(ch.photo ? true : c.image_url ? true : null, 'Фото карточки', ch.photo || c.image_url ? 'загружено' : 'нет')}
      ${row(ch.ocr ?? null, 'OCR по фото', ocrTxt)}
      ${rzRow}
      ${rzUrl ? `<button class="lot-btn secondary cs-rz-link" data-card-link="${esc(rzUrl)}">Открыть на RenderZ ↗</button>` : ''}
    </div>`;
}

const HIST_KIND = { created: 'Карточка добавлена', free_agent: 'Подписан как свободный агент', fix: 'Покупка лота', auction: 'Аукцион', exchange: 'Обмен', deal: 'Сделка' };
const HIST_ST = { approved: '', rejected: ' · отклонено', needs_judge: ' · у судьи', cancelled: ' · отозвано', pending: ' · на проверке' };

function historyBlock(list) {
  if (!list?.length) return '';
  return '<div class="group-title">История</div><div class="tr-list cs-hist">' + list.map((h) => {
    const move = h.kind === 'created' ? (h.to_club || h.from_club || '') : `${h.from_club || 'свободный агент'} → ${h.to_club || 'свободный агент'}`;
    return `<div class="bet-history-item"><div><div>${esc(HIST_KIND[h.kind] || h.kind || 'Событие')}${esc(HIST_ST[h.status] ?? '')}</div>
      <div class="sub">${move ? `${esc(move)} · ` : ''}${esc(fmtUtc(h.date))}</div></div>
      ${h.amount ? `<div class="lot-price">${fmt(Math.abs(h.amount))} ₼</div>` : ''}</div>`;
  }).join('') + '</div>';
}

/* opts.actions — доп. html действий (например, «Купить» с рынка), обрабатывает вызывающая фича */
export async function openCardSheet(cardId, opts = {}) {
  cs.id = String(cardId);
  cs.extra = opts.actions || '';
  const body = openSheet('Карточка игрока', '<div class="empty-note">Загрузка…</div>');
  let r;
  try { r = await api(`/api/cards/${encodeURIComponent(cardId)}`); } catch (e) {
    if (cs.id === String(cardId)) body.innerHTML = `<div class="empty-note">${esc(e.message)}</div>${cs.extra}`;
    return;
  }
  if (cs.id !== String(cardId)) return;
  const flat = r.card ? { ...r, ...r.card } : r;
  const c = normCard(flat);
  cs.card = c;
  $('#generic-title').textContent = c.name || 'Карточка игрока';
  const alt = c.alt.filter((p) => p !== c.position);
  const chars = [
    ['Позиция', `${esc(c.position || '—')}${POS_RU[c.position] ? ` <span class="sub">${POS_RU[c.position]}</span>` : ''}`],
    ['Доп. позиции', alt.length ? alt.map(esc).join(', ') : '—'],
    ['Сборная', esc(c.nation || '—')], ['Лига', esc(c.league || '—')],
    ['Клуб (реальный)', esc(c.real_club || '—')], ['Программа', esc(c.program || '—')],
    ['Финты', `<span class="cs-stars">${stars(Number(c.skill_moves) || 0)}</span>`],
    ['Слабая нога', `<span class="cs-stars">${stars(Number(c.weak_foot) || 0)}</span>`],
    ['Рабочая нога', c.foot ? (String(c.foot).toUpperCase().startsWith('L') ? 'Левая' : 'Правая') : '—'],
    ['Рост', c.height ? `${esc(c.height)} см` : '—'],
    ['Владелец', esc(c.club_name || (c.club_id ? `клуб #${c.club_id}` : 'свободный агент'))],
  ];
  const actions = [];
  if (c.can_decide && c.vstatus === 'pending') {
    actions.push(`<div class="cs-decide"><input class="amount-input cq-note" maxlength="200" placeholder="Комментарий (необязательно)">
      <div class="cs-btns"><button class="lot-btn" data-card-decide="${esc(c.id)}" data-ok="1">✅ Одобрить</button>
      <button class="lot-btn secondary" data-card-decide="${esc(c.id)}" data-ok="0">❌ Отклонить</button></div></div>`);
  }
  const own = [];
  if (c.can_edit) own.push(`<button class="lot-btn secondary" data-card-edit="${esc(c.id)}">✏️ Изменить</button>`);
  if (c.can_delete) own.push(`<button class="lot-btn secondary danger" data-card-del="${esc(c.id)}">🗑 Удалить</button>`);
  if (own.length) actions.push(`<div class="cs-btns">${own.join('')}</div>`);
  body.innerHTML = `${bigCard(c)}
    ${hasStats(c) && c.image_url ? `<div class="cs-statrow">${STATS.map(([k, lbl]) => `<div class="stat-box"><div class="v">${esc(c.stats[k] ?? '–')}</div><div class="k">${lbl}</div></div>`).join('')}</div>` : ''}
    ${cs.extra ? `<div class="cs-extra">${cs.extra}</div>` : ''}
    <div class="group-title">Характеристики</div>
    <div class="cs-table">${chars.map(([k, v]) => `<span>${k}</span><b>${v}</b>`).join('')}</div>
    ${verifyBlock(c)}
    ${actions.length ? `<div class="cs-actions">${actions.join('')}</div>` : ''}
    ${historyBlock(c.history)}`;
}

/* ===== форма добавления / правки ===== */

const cf = { edit: null, token: null, alt: new Set(), skill: 0, weak: 0, foot: '', rz: null, busy: false, ocrBusy: false, clubs: null, preview: null };

async function loadClubs() {
  if (cf.clubs) return cf.clubs;
  const byId = new Map();
  const add = (c) => { if (c?.id && !byId.has(c.id)) byId.set(c.id, { id: c.id, name: c.name }); };
  try { (await api('/api/admin/leagues')).leagues.forEach((t) => t.divisions.forEach((d) => d.clubs.forEach(add))); } catch (_) { /* нет прав */ }
  try { (await api('/api/transfers/clubs')).clubs.forEach(add); } catch (_) { /* нет клуба */ }
  cf.clubs = [...byId.values()].sort((a, b) => String(a.name).localeCompare(String(b.name), 'ru'));
  return cf.clubs;
}

function starSeg(key, val) {
  return `<div class="cf-stars" data-card-starset="${key}">${[1, 2, 3, 4, 5].map((n) => `<button type="button" class="${n <= val ? 'on' : ''}" data-card-star="${key}" data-v="${n}" aria-label="${n}">★</button>`).join('')}</div>`;
}

function altChips() {
  const main = $('#cf-position')?.value;
  return POSITIONS.map(([p]) => `<button type="button" class="chip ${cf.alt.has(p) ? 'active' : ''}" data-card-alt="${p}" ${p === main ? 'disabled' : ''}>${p}</button>`).join('');
}

/* opts.club — предвыбор клуба для админа: '' — свой, '0' — свободный агент */
export async function openCardForm(card = null, opts = {}) {
  const isAdmin = !!state.user?.is_admin;
  const c = card ? normCard(card) : null;
  if (cf.preview) URL.revokeObjectURL(cf.preview);
  Object.assign(cf, {
    edit: c, token: null, alt: new Set(c?.alt || []), rz: null, busy: false, ocrBusy: false, preview: null,
    skill: Number(c?.skill_moves) || 0, weak: Number(c?.weak_foot) || 0,
    foot: c?.foot ? (String(c.foot).toUpperCase().startsWith('L') ? 'L' : 'R') : '',
  });
  const v = (k) => esc(c?.[k] ?? '');
  const n0 = c ? 0 : 1;
  const body = openSheet(c ? `Изменить: ${c.name}` : 'Новая карточка', `<form class="cf" id="cf-form" autocomplete="off" novalidate>
    ${isAdmin ? `<div class="cf-step cf-club"><div class="cf-lbl">Клуб</div>
      <select class="amount-input cf-in" id="cf-club"><option value="">Мой клуб</option><option value="0">Свободный агент</option></select></div>` : ''}
    ${c ? '' : `<div class="cf-step">
      <div class="cf-h"><span class="cf-num">1</span>📸 Фото карточки <span class="sub">— поля заполнятся сами</span></div>
      <label class="cf-drop" for="cf-file" id="cf-drop">
        <span class="cf-drop-ico">🖼</span>
        <span class="cf-drop-ph"><b>Выбери скрин карточки</b><span class="sub">экран игрока в FC Mobile, целиком</span></span>
      </label>
      <input type="file" id="cf-file" class="cf-file" accept="image/*">
      <div id="cf-ocr"></div>
    </div>`}
    <div class="cf-step">
      <div class="cf-h"><span class="cf-num">${n0 + 1}</span>🔗 Ссылка RenderZ <span class="sub">— необязательно</span></div>
      <div class="cf-row-btn"><input class="amount-input cf-in" id="cf-rz" type="url" inputmode="url" placeholder="https://renderz.app/…" value="${esc(c?.renderz_url || '')}">
      <button type="button" class="lot-btn secondary" data-card-rz>Проверить</button></div>
      <div id="cf-rz-res"></div>
    </div>
    <div class="cf-step">
      <div class="cf-h"><span class="cf-num">${n0 + 2}</span>✍️ Данные карточки</div>
      <label class="cf-full"><span class="cf-lbl">Имя игрока</span>
      <input class="amount-input cf-in" id="cf-name" maxlength="40" placeholder="Например: Mbappé" value="${v('name')}"></label>
      <div class="cf-dir" id="cf-dir" hidden></div>
      <div class="cf-grid2 cf-ovrpos">
        <label><span class="cf-lbl">OVR</span><input class="amount-input cf-in" id="cf-rating" type="number" inputmode="numeric" min="40" max="150" placeholder="105" value="${v('rating')}"></label>
        <label><span class="cf-lbl">Позиция</span><select class="amount-input cf-in" id="cf-position">
          <option value="">—</option>${POSITIONS.map(([p, ru]) => `<option value="${p}" ${c?.position === p ? 'selected' : ''}>${p} — ${ru}</option>`).join('')}</select></label>
      </div>
      <div class="cf-lbl">Доп. позиции <span class="sub">до 4</span></div>
      <div class="cf-alt" id="cf-alt"></div>
      <div class="cf-lbl">Характеристики</div>
      <div class="cf-stats">${STATS.map(([k, lbl, ru]) => `<label><span>${lbl}<i>${ru}</i></span><input class="amount-input cf-in" id="cf-${k}" type="number" inputmode="numeric" min="0" max="200" value="${esc(c?.stats[k] ?? '')}"></label>`).join('')}</div>
      <div class="cf-grid2">
        <div><div class="cf-lbl">Финты</div>${starSeg('skill', cf.skill)}</div>
        <div><div class="cf-lbl">Слабая нога</div>${starSeg('weak', cf.weak)}</div>
      </div>
      <div class="cf-grid2">
        <div><div class="cf-lbl">Рабочая нога</div><div class="tr-seg cf-foot">
          <button type="button" class="chip ${cf.foot === 'L' ? 'active' : ''}" data-card-foot="L">Левая</button>
          <button type="button" class="chip ${cf.foot === 'R' ? 'active' : ''}" data-card-foot="R">Правая</button></div></div>
        <label><span class="cf-lbl">Рост, см</span><input class="amount-input cf-in" id="cf-height_cm" type="number" inputmode="numeric" min="140" max="230" placeholder="180" value="${esc(c?.height ?? '')}"></label>
      </div>
      <div class="cf-grid2">${TEXT_FIELDS.map(([k, lbl]) => `<label><span class="cf-lbl">${lbl}</span><input class="amount-input cf-in" id="cf-${k}" maxlength="60" value="${v(k)}"></label>`).join('')}</div>
    </div>
    <div class="sub tr-note">${isAdmin ? 'Ты админ — карточка сразу станет проверенной.' : 'Карточку проверит судья или админ; до проверки её нельзя выставить на рынок.'}</div>
    <button type="submit" class="place-btn" id="cf-submit">${c ? 'Сохранить' : isAdmin ? 'Добавить карточку' : 'Отправить на проверку'}</button>
  </form>`);
  $('#cf-alt').innerHTML = altChips();
  if (isAdmin) {
    loadClubs().then((clubs) => {
      const sel = $('#cf-club');
      if (!sel) return;
      sel.insertAdjacentHTML('beforeend', clubs.map((cl) => `<option value="${esc(cl.id)}">${esc(cl.name)}</option>`).join(''));
      if (c) sel.value = c.club_id ? String(c.club_id) : '0';
      else if (opts.club) sel.value = opts.club;
    });
  }
  return body;
}

function readForm() {
  const val = (id) => $(`#cf-${id}`)?.value.trim() ?? '';
  const num = (id) => { const n = parseInt(val(id), 10); return Number.isFinite(n) ? n : null; };
  const out = {
    name: val('name'), rating: num('rating'), position: val('position') || null,
    alt_positions: [...cf.alt].filter((p) => p !== val('position')),
    skill_moves: cf.skill || null, weak_foot: cf.weak || null, foot: cf.foot || null, height_cm: num('height_cm'),
    renderz_url: val('rz') || null,
  };
  STATS.forEach(([k]) => { out[k] = num(k); });
  TEXT_FIELDS.forEach(([k]) => { out[k] = val(k) || null; });
  const club = $('#cf-club');
  if (club && club.value !== '') out.club_id = Number(club.value) || null;
  if (cf.token) out.token = cf.token;
  return out;
}

/* заполнить поля из OCR/RenderZ; mark — css-класс подсветки заполненного */
function fillForm(fields = {}, mark = null) {
  const set = (k, val) => {
    const el = $(`#cf-${k}`);
    if (!el || val == null || val === '') return 0;
    el.value = k === 'position' ? posCode(val) : val;
    if (el.value === '') return 0;  // select без такого варианта
    if (mark) el.classList.add(mark);
    return 1;
  };
  const n = normCard(fields);
  let filled = set('name', fields.name) + set('rating', n.rating) + set('position', fields.position) + set('height_cm', n.height);
  STATS.forEach(([k]) => { filled += set(k, n.stats[k]); });
  TEXT_FIELDS.forEach(([k]) => { filled += set(k, fields[k]); });
  const marks = [];
  if (fields.skill_moves) { cf.skill = Number(fields.skill_moves) || 0; filled++; marks.push('skill'); }
  if (fields.weak_foot) { cf.weak = Number(fields.weak_foot) || 0; filled++; marks.push('weak'); }
  if (fields.foot) { cf.foot = String(fields.foot).toUpperCase().startsWith('L') ? 'L' : 'R'; filled++; }
  if (n.alt.length) { n.alt.slice(0, 4).forEach((p) => cf.alt.add(p)); filled++; }
  syncWidgets();
  if (mark) marks.forEach((k) => $(`[data-card-starset="${k}"]`)?.classList.add(mark));
  return filled;
}

function syncWidgets() {
  const alt = $('#cf-alt');
  if (alt) alt.innerHTML = altChips();
  document.querySelectorAll('[data-card-starset]').forEach((box) => {
    const val = box.dataset.cardStarset === 'skill' ? cf.skill : cf.weak;
    box.querySelectorAll('button').forEach((b) => b.classList.toggle('on', Number(b.dataset.v) <= val));
  });
  document.querySelectorAll('[data-card-foot]').forEach((b) => b.classList.toggle('active', b.dataset.cardFoot === cf.foot));
}

async function shrink(file) {
  // тяжёлые фото ужимаем, чтобы не упереться в лимит загрузки; OCR хватает 2000 px
  if (file.size < 3.5e6 || !/^image\/(jpeg|png|webp)$/.test(file.type)) return file;
  try {
    const bmp = await createImageBitmap(file);
    const k = Math.min(1, 2000 / Math.max(bmp.width, bmp.height));
    const cv = document.createElement('canvas');
    cv.width = Math.round(bmp.width * k); cv.height = Math.round(bmp.height * k);
    cv.getContext('2d').drawImage(bmp, 0, 0, cv.width, cv.height);
    const blob = await new Promise((res) => cv.toBlob(res, 'image/jpeg', 0.9));
    return blob ? new File([blob], 'card.jpg', { type: 'image/jpeg' }) : file;
  } catch (_) { return file; }
}

function setSubmitLock(on) {
  cf.ocrBusy = on;
  const b = $('#cf-submit');
  if (b) { b.disabled = on; b.textContent = on ? 'Ждём распознавание…' : (cf.edit ? 'Сохранить' : state.user?.is_admin ? 'Добавить карточку' : 'Отправить на проверку'); }
}

async function onPhoto(input) {
  const file = input.files?.[0];
  if (!file) return;
  if (!file.type.startsWith('image/')) { toast('Нужна картинка'); return; }
  if (cf.preview) URL.revokeObjectURL(cf.preview);
  cf.preview = URL.createObjectURL(file);
  const drop = $('#cf-drop');
  drop.classList.add('has-img');
  drop.innerHTML = `<img src="${cf.preview}" alt="" class="cf-preview"><span class="cf-drop-change">сменить фото</span>`;
  const box = $('#cf-ocr');
  box.innerHTML = '<div class="cf-ocr-wait"><span class="cf-spin"></span><div><b>Распознаю…</b><div class="sub">Обычно 10–60 секунд. Пока можно вставить ссылку RenderZ.</div></div></div>';
  document.querySelectorAll('#cf-form .cf-ocr').forEach((el) => el.classList.remove('cf-ocr'));
  cf.token = null;
  setSubmitLock(true);
  let r;
  try {
    const fd = new FormData();
    fd.append('image', await shrink(file));
    r = await postForm('/api/cards/ocr', fd);
  } catch (e) {
    if (!box.isConnected) return;
    // лимит OCR — фото всё равно прикрепляем, поля вручную
    let attached = false;
    if (e.status === 429) {
      try {
        const fd = new FormData();
        fd.append('image', await shrink(file));
        cf.token = (await postForm('/api/cards/upload', fd)).token || null;
        attached = !!cf.token;
      } catch (_) { /* лимит загрузок тоже */ }
    }
    box.innerHTML = `<div class="cf-warn bad">Не удалось распознать: ${esc(e.message)}.${attached ? ' Фото прикреплено —' : ''} Заполни поля вручную.</div>`;
    return;
  } finally {
    if (box.isConnected) setSubmitLock(false);
  }
  if (!box.isConnected) return;  // лист уже закрыт или переоткрыт
  cf.token = r.token || r.upload_token || null;
  const filled = fillForm(r.fields || r.card || {}, 'cf-ocr');
  const warns = r.warnings || [];
  const conf = r.confidence != null ? Math.round(Number(r.confidence) * (r.confidence <= 1 ? 100 : 1)) : null;
  haptic('light');
  box.innerHTML = filled
    ? `<div class="cf-warn ok">✨ Заполнено по фото: ${filled} ${plural(filled, 'поле', 'поля', 'полей')}${conf != null ? ` · уверенность ${conf}%` : ''}. Подсвеченные поля проверь.</div>`
    : `<div class="cf-warn">${cf.token ? 'Фото прикреплено, но' : 'Фото не сохранилось, и'} текст распознать не вышло — заполни поля вручную.</div>`;
  if (warns.length) box.insertAdjacentHTML('beforeend', warns.map((w) => `<div class="cf-warn">⚠️ ${esc(w)}</div>`).join(''));
}

const RZ_LBL = { name: 'Имя', rating: 'OVR', position: 'Позиция', alt_positions: 'Доп. поз.' };

async function checkRenderz(btn) {
  const url = $('#cf-rz').value.trim();
  const box = $('#cf-rz-res');
  if (!url) { box.innerHTML = '<div class="cf-warn bad">Вставь ссылку на карточку RenderZ</div>'; return; }
  const form = readForm();
  const fields = {};
  ['name', 'rating', 'position'].forEach((k) => { if (form[k] != null && form[k] !== '') fields[k] = form[k]; });
  if (form.alt_positions.length) fields.alt_positions = form.alt_positions;
  btn.disabled = true;
  box.innerHTML = '<div class="cf-ocr-wait"><span class="cf-spin"></span><b>Ищу карточку на RenderZ…</b></div>';
  let r;
  try {
    r = await api('/api/cards/renderz-check', { method: 'POST', body: JSON.stringify({ url, fields }) });
  } catch (e) {
    box.innerHTML = `<div class="cf-warn bad">❌ ${esc(e.message)}</div>`;
    return;
  } finally { btn.disabled = false; }
  if (r.ok === false || r.error) { box.innerHTML = `<div class="cf-warn bad">❌ ${esc(r.error || 'Карточка не найдена')}</div>`; return; }
  cf.rz = r.renderz || {};
  const match = r.match || {};
  const rows = Object.entries(match).filter(([k]) => !['all', 'partial', 'name_score'].includes(k)).map(([k, m]) => {
    const obj = typeof m === 'object' && m !== null;
    const ok = obj ? m.match : m;
    const theirs = obj ? (m.renderz ?? m.theirs ?? cf.rz[k]) : cf.rz[k];
    return `<div class="cf-rz-row"><span class="cs-vico ${ok ? 'ok' : ok === false ? 'bad' : ''}">${ok ? '✓' : ok === false ? '✗' : '—'}</span><span>${esc(RZ_LBL[k] || k)}</span><b>${esc(k === 'alt_positions' && Array.isArray(theirs) ? theirs.join(', ') : theirs ?? '—')}</b></div>`;
  }).join('');
  const any = cf.rz.name || cf.rz.rating || cf.rz.position;
  const allOk = r.match ? !!r.match.all : null;
  box.innerHTML = `<div class="cf-rz-box">
    <div class="cf-rz-title">${allOk ? '✅ Совпадает с RenderZ' : r.match?.partial ? '🟡 Имя и OVR совпали, позицию RenderZ не показал — проверит судья' : allOk === false ? '⚠️ Есть расхождения с RenderZ' : 'RenderZ нашёл карточку'}${cf.rz.name ? `: <b>${esc(cf.rz.name)}</b>` : ''}${cf.rz.rating ? ` · ${esc(cf.rz.rating)}` : ''}${cf.rz.position ? ` ${esc(posCode(cf.rz.position))}` : ''}</div>
    ${rows ? `<div class="cf-rz-grid">${rows}</div>` : ''}
    ${any ? '<button type="button" class="lot-btn secondary" data-card-rz-fill>Подставить данные RenderZ</button>' : ''}
  </div>`;
}

async function submitForm() {
  if (cf.ocrBusy) { toast('Дождись распознавания фото'); return; }
  if (cf.busy) return;
  const p = readForm();
  if (!p.name || p.name.length < 2) { toast('Укажи имя игрока'); $('#cf-name')?.focus(); return; }
  if (!p.rating || p.rating < 40 || p.rating > 150) { toast('OVR — число от 40 до 150'); $('#cf-rating')?.focus(); return; }
  if (!p.position) { toast('Выбери позицию'); return; }
  const btn = $('#cf-submit');
  btn.disabled = true;
  cf.busy = true;
  try {
    const r = cf.edit
      ? await api(`/api/cards/${encodeURIComponent(cf.edit.id)}`, { method: 'POST', body: JSON.stringify(p) })
      : await api('/api/cards', { method: 'POST', body: JSON.stringify(p) });
    const st = r.card?.verify_status;
    const warn = (r.warnings || []).filter(Boolean);
    toast((st === 'approved' ? (cf.edit ? 'Сохранено ✅' : 'Добавлена ✅') : 'Карточка отправлена на проверку') + (warn.length ? ` · ${warn[0]}` : ''), warn.length ? 4200 : 2600);
    $('#generic-sheet').hidden = true;
    refreshAll();
  } catch (e) { toast(e.message); } finally { cf.busy = false; if (btn.isConnected) btn.disabled = false; }
}

/* ===== состав во вкладке «Клуб» ===== */

async function renderSquad(card) {
  let r;
  try { r = await api('/api/cards/my'); } catch (e) {
    card.innerHTML = `<div class="card-title">Состав</div><div class="empty-note">${esc(e.message)}</div>`;
    return;
  }
  if (!card.isConnected) return;
  const cards = (r.cards || r.squad || []).map(normCard);
  const max = r.squad_max ?? r.max;
  const pending = cards.filter((c) => c.vstatus === 'pending').length;
  const canAdd = r.can_edit ?? r.can_add ?? true;
  const full = max != null && cards.length >= max;
  card.innerHTML = `<div class="card-title">Состав <span class="cs-count">${cards.length}${max != null ? `/${esc(max)}` : ''}</span>
      ${pending ? `<span class="cs-pending">⏳ ${pending} на проверке</span>` : ''}</div>
    ${canAdd ? `<button class="bonus-btn cs-add" data-card-add ${full ? 'disabled' : ''}>${full ? 'Состав заполнен' : '＋ Добавить карточку'}</button>` : ''}
    ${cards.length ? `<div class="cs-list">${cards.map((c) => cardTile(c, { cls: 'in-card', sub: c.vstatus === 'rejected' && c.verify_note ? `<div class="ct-note bad">❌ ${esc(c.verify_note)}</div>` : '' })).join('')}</div>`
      : `<div class="empty-note">Состав пуст${canAdd ? ' — добавь свои карточки по фото или подпиши агентов на «Рынке».' : '.'}</div>`}`;
}

hooks.clubCards.push((el, overview) => {
  const sc = el.querySelector('#squad-card');
  if (!sc || !overview?.club) return;
  // app.js после хуков дописывает старый список в #squad-card — уводим карточку под своим id
  sc.id = 'cards-squad';
  sc.innerHTML = '<div class="card-title">Состав</div><div class="empty-note">Загрузка…</div>';
  renderSquad(sc);
});

/* ===== очередь проверки ===== */

export async function loadQueue() {
  try {
    const r = await api('/api/cards/queue');
    return (r.cards || r.queue || []).map(normCard);
  } catch (_) { return null; }  // нет прав
}

const qButtons = (id) => `<button class="lot-btn" data-card-decide="${esc(id)}" data-ok="1">Одобрить</button>
  <button class="lot-btn secondary" data-card-reject="${esc(id)}">Отклонить</button>`;

export function queueHtml(list) {
  if (!list?.length) return '<div class="empty-note">Очередь пуста — все карточки проверены.</div>';
  return list.map((c) => {
    const rz = c.checks.renderz;
    const flags = [
      c.image_url ? '📸 фото' : '✍️ вручную',
      c.checks.ocr === true ? 'OCR ✓' : c.checks.ocr === false ? 'OCR ✗' : '',
      c.renderz_url ? (rz === 'match' ? 'RenderZ ✓' : rz === 'mismatch' ? 'RenderZ ✗' : 'RenderZ ?') : '',
    ].filter(Boolean).join(' · ');
    return cardTile(c, {
      cls: 'cq-item', noVerify: true,
      sub: `<div class="ct-note">${esc(c.club_name || 'свободный агент')}${c.owner?.username ? ` · @${esc(c.owner.username)}` : c.owner?.telegram_id ? ` · id ${esc(c.owner.telegram_id)}` : ''} · ${flags}</div>
        <div class="cq-actions" data-card-qbox="${esc(c.id)}">${qButtons(c.id)}</div>`,
    });
  }).join('');
}

async function renderAdminQueue() {
  const box = $('#admin-cards-queue');
  if (!box) return;
  const list = await loadQueue();
  if (!box.isConnected) return;
  const cnt = $('#admin-cards-count');
  if (cnt) { cnt.textContent = list?.length || ''; cnt.hidden = !list?.length; }
  box.innerHTML = list ? queueHtml(list) : '<div class="empty-note">Нет доступа к очереди.</div>';
}

async function renderDirectoryCard() {
  if (!state.user?.is_root) return;
  let card = $('#admin-directory-card');
  if (!card) {
    card = document.createElement('div');
    card.className = 'card';
    card.id = 'admin-directory-card';
    const audit = $('#admin-audit')?.parentElement;
    if (audit) audit.parentElement.insertBefore(card, audit); else $('#admin-sheet .sheet')?.appendChild(card);
  }
  let st;
  try { st = await api('/api/directory/status'); } catch (_) { card.remove(); return; }
  card.innerHTML = `<div class="card-title">📚 Справочник реальных игроков</div>
    <div class="sub" style="margin-bottom:8px">${st.count ? `${fmt(st.count)} игроков · снимок на ${esc(st.snapshot)}` : 'Пусто — загрузи снимок'}.
      Источник: открытый датасет Transfermarkt (${esc(st.source)}). Трансферы в сезоне ничего не меняют, пока не обновишь.</div>
    <div class="promo-row"><input class="amount-input" id="dir-snap" type="date" style="margin:0;font-size:14px" value="${new Date().toISOString().slice(0, 10)}">
      <button class="bonus-btn" id="dir-import" style="width:auto;padding:12px 16px">${st.count ? 'Обновить' : 'Загрузить'}</button></div>`;
  card.querySelector('#dir-import').onclick = async (e) => {
    if (st.count && !confirm('Заменить справочник свежим снимком?')) return;
    e.target.disabled = true;
    e.target.textContent = 'Загружаю…';
    try {
      const r = await api('/api/admin/directory/import', { method: 'POST', body: JSON.stringify({ snapshot: $('#dir-snap').value }) });
      toast(`Справочник: ${fmt(r.count)} игроков`);
    } catch (err) { toast(err.message); }
    renderDirectoryCard();
  };
}

hooks.adminCards.push(renderDirectoryCard);

hooks.adminCards.push(() => {
  let card = $('#admin-cards-card');
  if (!card) {
    card = document.createElement('div');
    card.className = 'card';
    card.id = 'admin-cards-card';
    card.innerHTML = `<div class="card-title">🃏 Проверка карточек <span class="tr-badge" id="admin-cards-count" hidden></span></div>
      <button class="bonus-btn cs-add" data-card-add="0">＋ Карточка в клуб или свободный агент</button>
      <div id="admin-cards-queue"><div class="empty-note">Загрузка…</div></div>`;
    const audit = $('#admin-audit')?.parentElement;
    if (audit) audit.parentElement.insertBefore(card, audit);
    else $('#admin-sheet .sheet')?.appendChild(card);
  }
  renderAdminQueue();
});

/* ===== события ===== */

const listeners = new Set();
// рынок подписывается, чтобы обновиться после одобрения/правки/удаления
export function onCardsChanged(fn) { listeners.add(fn); }
function refreshAll() {
  const sq = $('#cards-squad');
  if (sq?.isConnected) renderSquad(sq);
  renderAdminQueue();
  listeners.forEach((fn) => { try { fn(); } catch (e) { console.error(e); } });
}

async function decide(id, ok, note, btns) {
  btns.forEach((b) => { b.disabled = true; });
  try {
    await api(`/api/cards/${encodeURIComponent(id)}/decide`, { method: 'POST', body: JSON.stringify({ approve: ok, note: note || null }) });
    toast(ok ? 'Карточка одобрена ✅' : 'Карточка отклонена');
    haptic('light');
    const sheet = $('#generic-sheet');
    if (cs.id === String(id) && sheet && !sheet.hidden && $('#generic-body .cs-verify')) openCardSheet(id, { actions: cs.extra });
    refreshAll();
  } catch (e) {
    toast(e.message);
    btns.forEach((b) => { b.disabled = false; });
  }
}

document.addEventListener('change', (ev) => {
  if (ev.target.id === 'cf-file') onPhoto(ev.target);
  else if (ev.target.id === 'cf-position') { cf.alt.delete(ev.target.value); $('#cf-alt').innerHTML = altChips(); }
});

/* ===== справочник реальных игроков: автокомплит имени ===== */

const dir = { timer: null, seq: 0, items: [] };

function dirSearch(q) {
  clearTimeout(dir.timer);
  const box = $('#cf-dir');
  if (!box) return;
  if (q.trim().length < 3) { box.hidden = true; return; }
  dir.timer = setTimeout(async () => {
    const seq = ++dir.seq;
    let players;
    try { ({ players } = await api(`/api/directory/search?q=${encodeURIComponent(q)}`)); } catch (_) { return; }
    if (seq !== dir.seq || !$('#cf-dir')) return;
    dir.items = players;
    box.innerHTML = players.map((p, i) => `<button type="button" class="cf-dir-item" data-dir-pick="${i}">
      <b>${esc(p.name)}</b><span>${esc([p.position, p.real_club, p.nation].filter(Boolean).join(' · '))}</span></button>`).join('');
    box.hidden = !players.length;
  }, 250);
}

function dirPick(p) {
  // из справочника — только реальные данные; OVR и характеристики карточки FC Mobile вводятся отдельно
  fillForm({ name: p.name, position: p.position, nation: p.nation, real_club: p.real_club, league: p.league,
    foot: p.foot, height_cm: p.height_cm }, 'cf-ocr');
  $('#cf-dir').hidden = true;
  toast('Данные игрока подставлены из справочника — добавь OVR и характеристики карточки');
}

document.addEventListener('input', (ev) => {
  // правка руками снимает подсветку «заполнено автоматически»
  if (ev.target.classList?.contains('cf-ocr')) ev.target.classList.remove('cf-ocr');
  if (ev.target.id === 'cf-name') dirSearch(ev.target.value);
});

document.addEventListener('submit', (ev) => {
  if (ev.target.id !== 'cf-form') return;
  ev.preventDefault();
  submitForm();
});

document.addEventListener('click', async (ev) => {
  const t = ev.target;
  const q = (sel) => t.closest(sel);
  let el;
  if ((el = q('[data-dir-pick]'))) return dirPick(dir.items[Number(el.dataset.dirPick)]);
  if (!q('#cf-dir') && $('#cf-dir')) $('#cf-dir').hidden = true;
  if ((el = q('[data-card-add]'))) return openCardForm(null, { club: el.dataset.cardAdd || '' });
  if ((el = q('[data-card-view]'))) return openCardSheet(el.dataset.cardView);
  if ((el = q('[data-card-star]'))) {
    const n = Number(el.dataset.v);
    if (el.dataset.cardStar === 'skill') cf.skill = cf.skill === n ? 0 : n; else cf.weak = cf.weak === n ? 0 : n;
    el.parentElement.classList.remove('cf-ocr');
    return syncWidgets();
  }
  if ((el = q('[data-card-foot]'))) { cf.foot = cf.foot === el.dataset.cardFoot ? '' : el.dataset.cardFoot; return syncWidgets(); }
  if ((el = q('[data-card-alt]'))) {
    const p = el.dataset.cardAlt;
    if (cf.alt.has(p)) cf.alt.delete(p); else if (cf.alt.size < 4) cf.alt.add(p); else toast('Не больше 4 доп. позиций');
    el.classList.toggle('active', cf.alt.has(p));
    return;
  }
  if ((el = q('[data-card-rz]'))) return checkRenderz(el);
  if (q('[data-card-rz-fill]')) {
    const rz = cf.rz || {};
    const n = fillForm({ name: rz.name, rating: rz.rating, position: rz.position, nation: rz.nation }, 'cf-ocr');
    toast(n ? 'Данные RenderZ подставлены' : 'В ответе RenderZ нет данных');
    return;
  }
  if ((el = q('[data-card-link]'))) return openLink(el.dataset.cardLink);
  if ((el = q('[data-card-edit]'))) {
    if (cs.card && String(cs.card.id) === el.dataset.cardEdit) openCardForm(cs.card);
    return;
  }
  if ((el = q('[data-card-del]'))) {
    if (el.dataset.confirm !== '1') { el.dataset.confirm = '1'; el.textContent = 'Точно удалить?'; return; }
    el.disabled = true;
    try {
      await api(`/api/cards/${encodeURIComponent(el.dataset.cardDel)}`, { method: 'DELETE' });
      toast('Карточка удалена');
      $('#generic-sheet').hidden = true;
      refreshAll();
    } catch (e) { toast(e.message); el.disabled = false; }
    return;
  }
  if ((el = q('[data-card-reject]'))) {
    // первое нажатие раскрывает поле причины
    const id = el.dataset.cardReject;
    const box = el.closest('[data-card-qbox]');
    box.classList.add('rejecting');
    box.innerHTML = `<input class="amount-input cq-note" maxlength="200" placeholder="Причина (необязательно)">
      <div class="cs-btns"><button class="lot-btn secondary danger" data-card-decide="${esc(id)}" data-ok="0">Отклонить</button>
      <button class="lot-btn secondary" data-card-cancel="${esc(id)}">Отмена</button></div>`;
    box.querySelector('input').focus();
    return;
  }
  if ((el = q('[data-card-cancel]'))) {
    const box = el.closest('[data-card-qbox]');
    box.classList.remove('rejecting');
    box.innerHTML = qButtons(el.dataset.cardCancel);
    return;
  }
  if ((el = q('[data-card-decide]'))) {
    const scope = el.closest('[data-card-qbox]') || el.closest('.cs-decide');
    const note = scope?.querySelector('input')?.value.trim();
    return decide(el.dataset.cardDecide, el.dataset.ok === '1', note, [...(scope?.querySelectorAll('button') || [el])]);
  }
  if ((el = q('[data-card-open]')) && !q('button, a, input, select, label, [data-card-qbox]')) {
    return openCardSheet(el.dataset.cardOpen, { actions: el.dataset.cardActions ? decodeURIComponent(el.dataset.cardActions) : '' });
  }
});

export { renderSquad };
