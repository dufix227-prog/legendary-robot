/* Общие утилиты, состояние и точки расширения мини-аппа.
   Фичи (features/*.js) импортируют отсюда и регистрируют экраны/карточки через hooks. */

export const tg = window.Telegram?.WebApp;
tg?.ready();
tg?.expand();
tg?.setHeaderColor?.('#0f1114');
tg?.setBackgroundColor?.('#0f1114');

export const state = {
  user: null,
  divisions: [],
  lineMatches: [],
  favorites: new Set(JSON.parse(localStorage.getItem('favorites') || '[]')),
  coupon: JSON.parse(localStorage.getItem('coupon') || '[]'),
  currentDivision: null,
  currentTour: null,
  matchDetail: null,
  placing: false,
  idemKey: null,
};

/* ===== API ===== */

export async function api(endpoint, options = {}) {
  // dev-фолбэк для браузерных тестов вне Telegram: localStorage 'dev_initdata'
  // хранит заранее подписанную строку — сервер всё равно проверяет подпись и окно 24 ч.
  const initData = tg?.initData || localStorage.getItem('dev_initdata') || '';
  const res = await fetch(endpoint, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      'X-Telegram-Init-Data': initData,
      ...(options.headers || {}),
    },
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* proxy/html */ }
  if (!res.ok || !data || typeof data !== 'object') {
    data = data && typeof data === 'object' ? data : {};
    if (res.status === 403) {
      showLockdown(data.reason);
    }
    const err = new Error(data.error || data.message || `Ошибка сервера (${res.status})`);
    err.code = data.code;
    err.status = res.status;
    throw err;
  }
  return data;
}

/* ===== утилиты ===== */

export const $ = (sel) => document.querySelector(sel);
export const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

export function toast(text, ms = 2600) {
  const el = $('#toast');
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, ms);
}

export function logoHtml(club, cls = '') {
  // эмодзи-эмблема владельца важнее логотипа из каталога
  if (club?.emblem) {
    const bg = /^#[0-9a-f]{6}$/i.test(club.color1 || '') ? ` style="background:${club.color1}"` : '';
    return `<span class="mc-fallback emblem ${cls}"${bg}>${esc(club.emblem)}</span>`;
  }
  if (club?.logo) return `<img src="${esc(club.logo)}" alt="" class="${cls}">`;
  return `<span class="mc-fallback ${cls}">🛡️</span>`;
}

export function fmt(n) { return Number(n ?? 0).toLocaleString('ru-RU'); }
export function odds(o) { return Number(o ?? 0).toFixed(2); }
export function haptic(kind = 'light') { try { tg?.HapticFeedback?.impactOccurred(kind); } catch (_) { /* вне Telegram */ } }

export function setBadge(unread) {
  const b = $('#bell-badge');
  b.textContent = unread > 99 ? '99+' : String(unread || 0);
  b.hidden = !unread;
}

export function formLetters(form) {
  return (form || '').split('').map((f) => `<span class="${esc(f)}">${esc(f)}</span>`).join('');
}

export function showLockdown(reason) {
  $('#lockdown-reason').textContent = reason || '';
  $('#app-lockdown-screen').hidden = false;
  document.querySelector('.bottom-nav').style.display = 'none';
  document.querySelector('.views-container').style.display = 'none';
  document.querySelector('.app-header').style.display = 'none';
  $('#betbar').hidden = true;
}

export function fmtTime(s, local = false) {
  // сервер пишет время в UTC без зоны (datetime('now'), utcnow().isoformat())
  const d = s ? new Date(s.replace(' ', 'T') + (local ? '' : 'Z')) : null;
  if (!d || isNaN(d)) return s || '';
  return d.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
}

/* ===== точки расширения =====
   views[name]      — рендер вкладки нижней навигации (data-view=name)
   profileCards     — fn(container) после рендера «Кабинета»
   clubCards        — fn(container, overview) после рендера «Клуба»
   adminCards       — fn() при открытии админ-панели (сами находят/создают свою карточку)
   couponCards      — fn(container) после рендера купона (и пустого — container = #coupon-body)
   matchSheet       — fn(container, match) после рендера карточки матча (#match-detail)
   lineCards        — fn(matchCardEl, match) для каждой карточки матча в линии */
export const actions = {};  // заполняет app.js: renderAll, renderCoupon, refreshWallet, showView…

export const hooks = { views: {}, profileCards: [], clubCards: [], adminCards: [], couponCards: [], matchSheet: [], lineCards: [] };

export function runHooks(list, ...args) {
  for (const fn of list) { try { fn(...args); } catch (e) { console.error(e); } }
}

export function openSheet(title, html) {
  // универсальный лист поверх экрана (для фич без своей разметки в index.html)
  let el = document.getElementById('generic-sheet');
  if (!el) {
    el = document.createElement('div');
    el.id = 'generic-sheet';
    el.className = 'sheet-backdrop';
    el.innerHTML = '<div class="sheet"><button class="sheet-close" data-close-generic>✕</button><div class="section-title" id="generic-title"></div><div id="generic-body"></div></div>';
    document.getElementById('app').appendChild(el);
    el.addEventListener('click', (e) => { if (e.target === el || e.target.closest('[data-close-generic]')) el.hidden = true; });
  }
  el.querySelector('#generic-title').textContent = title;
  el.querySelector('#generic-body').innerHTML = html;
  el.hidden = false;
  return el.querySelector('#generic-body');
}
