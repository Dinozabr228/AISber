

   /* ═══ SBERIK COMPLETE INTERACTIVE PROTOTYPE ═══ */

'use strict';

/* ──────────────────────────────────────────────
   1. SESSION — dynamic user identity
   ────────────────────────────────────────────── */

// API key injected by server at page-serve time
let _apiKey = (typeof window !== 'undefined' && window.__SBERIK_API_KEY__) ? window.__SBERIK_API_KEY__ : '';

// Resolved after /api/v1/session
let _sessionUserId = '';

// Conversation context tracking
let _conversationId = null;

// Current mode: "banking" | "assistant"
let _currentMode = 'banking';

// Notification polling
let _notifPollInterval = null;
let _notifUnreadCount = 0;

async function _initSession() {
  try {
    const r = await fetch('/api/v1/session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-API-Key': _apiKey },
    });
    if (r.ok) {
      const data = await r.json();
      _sessionUserId = data.user_id || 'user_001';
    } else {
      _sessionUserId = 'user_001';
    }
  } catch (_) {
    _sessionUserId = 'user_001';
  }
  _startNotifPolling();
  _loadConversationHistory().catch(() => {});
}

function _startNotifPolling() {
  if (_notifPollInterval) return;
  _notifPollInterval = setInterval(_pollNotifications, 15000);
}

async function _pollNotifications() {
  if (!_sessionUserId) return;
  try {
    const r = await fetch('/api/v1/notifications?user_id=' + encodeURIComponent(_sessionUserId), {
      headers: { 'X-API-Key': _apiKey },
    });
    if (!r.ok) return;
    const data = await r.json();
    const prev = _notifUnreadCount;
    _notifUnreadCount = data.unread || 0;
    _updateNotifBadge(_notifUnreadCount);
    if (_notifUnreadCount > prev && data.notifications) {
      const newest = data.notifications.filter(n => !n.read).slice(-1)[0];
      if (newest) showToast('🔔 ' + newest.text);
    }
  } catch (_) {}
}

function _updateNotifBadge(count) {
  let badge = document.getElementById('_notifBadge');
  if (!badge) {
    const header = document.getElementById('widgetHeader');
    if (!header) return;
    badge = document.createElement('span');
    badge.id = '_notifBadge';
    badge.style.cssText = 'position:absolute;top:8px;right:44px;background:#ef4444;color:#fff;'
      + 'font-size:10px;font-weight:700;min-width:16px;height:16px;border-radius:8px;'
      + 'display:flex;align-items:center;justify-content:center;padding:0 4px;z-index:10;'
      + 'pointer-events:none;';
    header.style.position = 'relative';
    header.appendChild(badge);
  }
  badge.textContent = count > 0 ? String(count) : '';
  badge.style.display = count > 0 ? 'flex' : 'none';
}

function _updateModeIndicator(mode) {
  _currentMode = mode;
  // Badge intentionally removed — mode is not displayed in the header.
}


/* ──────────────────────────────────────────────
   2. AI SERVICE — real backend
   ────────────────────────────────────────────── */
const AIService = {
  /**
   * @param {string} userMessage
   * @returns {Promise<string
   *   | {_needsConfirm: true, token: string, message: string, draftDetails: object}
   *   | {_needsRecipientDetails: true, draftId: string, message: string, draftDetails: object, riskLevel: string}
   * >}
   */
  async getReply(userMessage) {
    const body = {
      user_id: _sessionUserId || 'user_001',
      message: userMessage,
      mode: _currentMode,
    };
    if (_conversationId) body.conversation_id = _conversationId;

    const r = await fetch('/api/v1/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-API-Key': _apiKey },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${r.status}`);
    }
    const data = await r.json();
    if (data.conversation_id) _conversationId = data.conversation_id;

    // HIGH-01: new counterparty — show details form before confirmation
    if (data.requires_recipient_details && data.pending_draft_id) {
      return {
        _needsRecipientDetails: true,
        draftId: data.pending_draft_id,
        message: data.user_message,
        draftDetails: data.draft_details || null,
        riskLevel: data.risk_level || 'HIGH',
      };
    }

    if (data.requires_confirmation && data.confirmation_token) {
      return {
        _needsConfirm: true,
        token: data.confirmation_token,
        message: data.user_message,
        draftDetails: data.draft_details || null,
        riskAction: data.risk_action || null,
        convId: data.conversation_id || null,
      };
    }
    let msg = data.user_message || 'Готово.';
    if (data.action_result) {
      msg += '\n\n' + _formatResult(data.action_result);
      if (data.action_result.matched_section) {
        _navigateToSection(data.action_result.matched_section.section_id);
      }
    }
    return msg;
  }
};

function _navigateToSection(sectionId) {
  if (!sectionId) return;
  try {
    var nameEl = document.querySelector('[data-name="' + sectionId + '"]');
    if (!nameEl) return;
    var li = nameEl.closest ? nameEl.closest('li[class*="leftMenuItem-wrapper"]') : null;
    if (li) { li.click(); } else { nameEl.click(); }
  } catch (_e) {}
}

function _formatResult(r) {
  if (r.currency && r.company_name && r.as_of) {
    var balanceStr = (r.balance != null && r.balance !== '***')
      ? Number(r.balance).toLocaleString('ru-RU', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' ' + r.currency
      : (r.balance_range || '***');
    return '🏦 ' + r.company_name
      + '\nБаланс: ' + balanceStr
      + '\nНа дату: ' + r.as_of;
  }
  if (r.transactions) {
    let lines = ['📋 ' + (r.company_name ? r.company_name + ' — ' : '') + 'Операции по счёту:'];
    r.transactions.forEach(function(tx) {
      let dir = tx.type === 'income' ? '⬆️ +' : '⬇️ −';
      let amt = tx.amount != null
        ? Number(tx.amount).toLocaleString('ru-RU', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' BYN'
        : '—';
      let label = tx.label || '—';
      let date = tx.date ? tx.date.substring(0, 10) : '';
      lines.push(dir + amt + (label !== '—' ? '  ' + label : '') + (date ? '  ' + date : ''));
    });
    lines.push('\nВсего: ' + r.count + ' операц.');
    return lines.join('\n');
  }
  if (r.income !== undefined && r.expenses !== undefined) {
    var _fmtByn = function(n) {
      return Number(n).toLocaleString('ru-RU', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' BYN';
    };
    if (r.subtype === 'analysis') {
      var lines = ['📊 Анализ за ' + r.period, ''];
      if (r.top_expenses && r.top_expenses.length) {
        lines.push('📉 Основные статьи расходов:');
        r.top_expenses.forEach(function(e) {
          lines.push('• ' + e.category + ' — ' + _fmtByn(e.amount));
        });
        lines.push('');
      }
      lines.push('Доходы:    ' + _fmtByn(r.income));
      lines.push('Расходы:   ' + _fmtByn(r.expenses));
      lines.push('Налоги:    ' + _fmtByn(r.tax));
      lines.push('Остаток:   ' + _fmtByn(r.balance));
      return lines.join('\n');
    }
    var lines = [
      '📊 Финансовый отчёт',
      'Период: ' + r.period,
      '',
      'Доходы:                  ' + _fmtByn(r.income),
      'Расходы:                 ' + _fmtByn(r.expenses),
      'Налоговые отчисления:    ' + _fmtByn(r.tax),
      '─────────────────────────────',
      'Итоговый остаток:        ' + _fmtByn(r.balance),
    ];
    if (r.top_expenses && r.top_expenses.length) {
      lines.push('');
      lines.push('📉 Основные статьи расходов:');
      r.top_expenses.forEach(function(e) {
        lines.push('• ' + e.category + ' — ' + _fmtByn(e.amount));
      });
    }
    return lines.join('\n');
  }
  if (r.transfer_id) {
    var statusLabel = r.status === 'executed' ? '✅ Выполнен' : '🕐 В обработке';
    var lines = '💸 Перевод #' + r.transfer_id.substring(0, 8) + '…'
      + '\nСумма: ' + Number(r.amount_byn).toLocaleString('ru-RU', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' BYN'
      + '\nПолучатель: ' + r.recipient
      + '\nОт: ' + r.initiator
      + '\nСтатус: ' + statusLabel;
    if (r.new_balance != null) {
      lines += '\nОстаток на счёте: ' + Number(r.new_balance).toLocaleString('ru-RU', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' BYN';
    }
    return lines;
  }
  if (r.tariffs) {
    let lines = ['📑 Доступные тарифы:'];
    r.tariffs.forEach(function(t) {
      let fee = t.monthly_fee === 0 ? 'бесплатно' : t.monthly_fee + ' BYN/мес';
      let transfers = t.transfers_per_month === 'Безлимитно' ? '∞' : t.transfers_per_month;
      lines.push('• ' + t.name + ' — ' + fee + ', ' + transfers + ' перев., комиссия ' + t.transfer_fee_percent + '%');
    });
    return lines.join('\n');
  }
  if (r.bic) {
    return '🏢 Реквизиты: ' + r.company_name
      + '\nБанк: ' + r.bank
      + '\nБИК: ' + r.bic
      + '\nСчёт: ' + r.account_number
      + '\nВалюта: ' + r.currency;
  }
  if (r.sections) {
    let lines = [];
    if (r.matched_section) {
      lines.push('📍 Раздел: ' + r.matched_section.section_name);
      lines.push(r.matched_section.description);
      lines.push('');
    }
    lines.push('🗂 Все разделы СберБизнес:');
    r.sections.forEach(function(s) {
      lines.push('• ' + s.section_name + ' — ' + s.description);
    });
    return lines.join('\n');
  }
  if (r.counterparties != null) {
    let lines = ['📋 Справочник контрагентов — ' + r.company_name + ':'];
    if (r.count === 0) {
      lines.push('Нет сохранённых контрагентов.');
    } else {
      r.counterparties.forEach(function(c) {
        lines.push('');
        lines.push('• ' + c.organization_name);
        if (c.bank !== '—') lines.push('  Банк: ' + c.bank);
        if (c.account_masked !== '—') lines.push('  Счёт: ' + c.account_masked + (c.last_four !== '—' ? ' (…' + c.last_four + ')' : ''));
        if (c.last_transfer_date !== '—') lines.push('  Последний перевод: ' + c.last_transfer_date);
      });
    }
    return lines.join('\n');
  }
  return JSON.stringify(r, null, 2);
};

/* ──────────────────────────────────────────────
   2. EYE / AVATAR ENGINE
   ────────────────────────────────────────────── */
const EyeEngine = (() => {
  const BASE = { xL: 28, xR: 55, y: 26, w: 17, h: 27, rx: 6.5 };
  const CLAMP = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const LERP  = (a, b, t) => a + (b - a) * t;

  // Each avatar instance
  const instances = new Map();

  function register(id, eyeL, eyeR) {
    instances.set(id, {
      eyeL, eyeR,
      // current state
      xLc: BASE.xL, xRc: BASE.xR,
      yLc: BASE.y,  yRc: BASE.y,
      hLc: BASE.h,  hRc: BASE.h,
      rxLc: BASE.rx, rxRc: BASE.rx,
      // blink
      blinkTimer: 2 + Math.random() * 3,
      blinking: false, blinkT: 0,
      // gaze
      gazeX: 0, gazeY: 0,
      // mode
      mode: 'idle'
    });
  }

  function setGaze(id, nx, ny) {
    // nx, ny: normalized [-1,1]
    const s = instances.get(id);
    if (!s) return;
    s.gazeX = CLAMP(nx * 4.5, -4.5, 4.5);
    s.gazeY = CLAMP(ny * 3.5, -3.5, 3.5);
    s.mode = 'hover';
  }
  function clearGaze(id) {
    const s = instances.get(id);
    if (!s) return;
    s.gazeX = 0; s.gazeY = 0;
    s.mode = 'idle';
  }

  let last = performance.now();
  function tick(now) {
    const dt = Math.min((now - last) / 1000, 0.05);
    last = now;
    const t = now / 1000;

    instances.forEach((s, id) => {
      // Blink
      s.blinkTimer -= dt;
      if (s.blinkTimer <= 0 && !s.blinking) {
        s.blinking = true; s.blinkT = 0;
        s.blinkTimer = 3 + Math.random() * 4;
      }
      if (s.blinking) {
        s.blinkT += dt / 0.18; // blink takes 180ms
        if (s.blinkT >= 1) { s.blinking = false; s.blinkT = 0; }
      }

      // Target eye positions
      let txL = BASE.xL, txR = BASE.xR;
      let tyL = BASE.y,  tyR = BASE.y;
      let thL = BASE.h,  thR = BASE.h;
      let trxL = BASE.rx, trxR = BASE.rx;

      if (s.mode === 'idle') {
        // Subtle idle drift
        txL += Math.sin(t * 0.7 + 0.3) * 1.2 + Math.sin(t * 0.3) * 0.6;
        txR += Math.sin(t * 0.7 + 0.8) * 1.0 + Math.sin(t * 0.3 + 0.4) * 0.5;
        tyL += Math.sin(t * 0.5 + 1.0) * 0.8;
        tyR += Math.sin(t * 0.5 + 1.5) * 0.7;
      } else if (s.mode === 'hover') {
        txL += s.gazeX;
        txR += s.gazeX;
        tyL += s.gazeY;
        tyR += s.gazeY;
      } else if (s.mode === 'thinking') {
        txL += Math.cos(t * 1.8) * 3 + Math.sin(t * 0.9) * 1.5;
        txR += Math.cos(t * 1.8 + 0.5) * 2.5 + Math.sin(t * 0.9 + 0.3) * 1.2;
        tyL += Math.sin(t * 1.3) * 2;
        tyR += Math.sin(t * 1.3 + 0.4) * 1.8;
      }

      // Blink squish
      if (s.blinking) {
        const bf = Math.sin(s.blinkT * Math.PI); // 0→1→0
        const squish = 1 - bf * 0.82;
        thL = BASE.h * squish;
        thR = BASE.h * squish;
        const dy = (BASE.h - thL) / 2;
        tyL += dy; tyR += dy;
        trxL = Math.min(BASE.rx, thL * 0.34);
        trxR = Math.min(BASE.rx, thR * 0.34);
      }

      // Smooth interpolation
      const sp = 14 * dt;
      const clamp01 = v => CLAMP(v, 0, 1);
      const k = clamp01(sp);
      s.xLc  = LERP(s.xLc,  txL,  k);
      s.xRc  = LERP(s.xRc,  txR,  k);
      s.yLc  = LERP(s.yLc,  tyL,  k);
      s.yRc  = LERP(s.yRc,  tyR,  k);
      s.hLc  = LERP(s.hLc,  thL,  k);
      s.hRc  = LERP(s.hRc,  thR,  k);
      s.rxLc = LERP(s.rxLc, trxL, k);
      s.rxRc = LERP(s.rxRc, trxR, k);

      // Write to DOM
      const setRect = (el, x, y, h, rx) => {
        el.setAttribute('x',      x.toFixed(2));
        el.setAttribute('y',      y.toFixed(2));
        el.setAttribute('height', h.toFixed(2));
        el.setAttribute('rx',     rx.toFixed(2));
      };
      setRect(s.eyeL, s.xLc, s.yLc, s.hLc, s.rxLc);
      setRect(s.eyeR, s.xRc, s.yRc, s.hRc, s.rxRc);
    });

    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);

  return { register, setGaze, clearGaze,
    setMode(id, mode) { const s = instances.get(id); if(s) s.mode = mode; }
  };
})();

/* ──────────────────────────────────────────────
   3. DOM REFS
   ────────────────────────────────────────────── */
const $ = id => document.getElementById(id);
const widget        = $('widget');
const launcher      = $('launcher');
const chatInput     = $('chatInput');
const composer      = $('composer');
const sendBtn       = $('sendBtn');
const sendBtnIcon   = $('sendBtnIcon');
const SEND_ICON_IDLE   = 'icon/tabler_square-rounded-arrow-up.svg';
const SEND_ICON_ACTIVE = 'icon/tabler_square-rounded-arrow-up-1.svg';
const screenWelcome = $('screenWelcome');
const screenMsg     = $('screenMessages');
const messagesScrollWrap = $('messagesScrollWrap');
const settingsPanel = $('settingsPanel');
const composerSettingsBtn = $('composerSettingsBtn');
const financeToggle = $('financeToggle');
const deleteChatBtn = $('deleteChatBtn');
const chatTitle     = $('chatTitle');
const chatTitleBtn  = $('chatTitleBtn');
const chatDropdown  = $('chatDropdown');
const minimizeBtn   = $('minimizeBtn');
const modeMenuBtn   = $('modeMenuBtn');
const sourcesMenuBtn = $('sourcesMenuBtn');
const modeFlyout    = $('modeFlyout');
const sourcesFlyout = $('sourcesFlyout');
const modeValue     = $('modeValue');
const sourcesCount  = $('sourcesCount');
const operatorBtn   = $('operatorBtn');
const screenOperator = $('screenOperator');
const opSearching   = $('opSearching');
const opMessages    = $('opMessages');
const widgetHeader  = $('widgetHeader');
const quickChips    = $('quickChips');

const DEFAULT_CORNER = { right: 35, bottom: 28 };

/* ──────────────────────────────────────────────
   4. REGISTER AVATARS
   ────────────────────────────────────────────── */
EyeEngine.register('launcher', $('launcherEyeL'), $('launcherEyeR'));
EyeEngine.register('welcome',  $('welcomeEyeL'),  $('welcomeEyeR'));
EyeEngine.register('header',   $('headerEyeL'),   $('headerEyeR'));

/* ──────────────────────────────────────────────
   5. MOUSE GAZE TRACKING
   ────────────────────────────────────────────── */
function trackGaze(id, el) {
  if (!el) return;
  el.addEventListener('pointermove', e => {
    const r = el.getBoundingClientRect();
    if (r.width === 0) return;
    const nx = ((e.clientX - r.left) / r.width  - 0.5) * 2;
    const ny = ((e.clientY - r.top)  / r.height - 0.5) * 2;
    EyeEngine.setGaze(id, nx, ny);
  });
  el.addEventListener('pointerleave', () => EyeEngine.clearGaze(id));
}
trackGaze('launcher', launcher);
trackGaze('welcome',  $('welcomeAvatarSvg'));
trackGaze('header',   $('headerAvatarSvg'));

document.addEventListener('mousemove', e => {
  if (!launcher.classList.contains('hidden')) {
    const r = launcher.getBoundingClientRect();
    if (r.width === 0) return;
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    const dx = e.clientX - cx;
    const dy = e.clientY - cy;
    const dist = Math.hypot(dx, dy);
    const maxDist = 900;
    if (dist < maxDist) {
      const strength = 1 - (dist / maxDist);
      EyeEngine.setGaze('launcher', (dx / 120) * strength, (dy / 120) * strength);
    } else {
      EyeEngine.clearGaze('launcher');
    }
  }
});

/* ──────────────────────────────────────────────
   6. WIDGET STATE
   ────────────────────────────────────────────── */
const state = {
  isOpen: false,
  hasHistory: false,
  viewMode: 'welcome', // welcome | chat | operator
  messages: [],
  financeOn: true,
  isTyping: false,
  savedTitle: 'Новый ИИ Чат',
  operatorTimer: null,
  // Persisted widget position across minimize/restore
  savedRight:  null,
  savedBottom: null,
};

function resetCornerPosition() {
  syncCornerPosition(DEFAULT_CORNER.right, DEFAULT_CORNER.bottom);
}

function syncCornerPosition(right, bottom) {
  widget.style.left = '';
  widget.style.top = '';
  widget.style.right = right + 'px';
  widget.style.bottom = bottom + 'px';
  launcher.style.left = '';
  launcher.style.top = '';
  launcher.style.right = right + 'px';
  launcher.style.bottom = bottom + 'px';
}

function positionChatDropdown() {
  if (window.innerWidth <= 520) return;
  const anchor = chatTitleBtn.getBoundingClientRect();
  const panel = chatDropdown;
  panel.style.left = Math.max(8, anchor.left) + 'px';
  panel.style.top = (anchor.bottom + 6) + 'px';
}

function positionSettingsPanel() {
  if (window.innerWidth <= 520) return;
  const anchor = composerSettingsBtn.getBoundingClientRect();
  const panel = settingsPanel;
  const pw = panel.offsetWidth || 320;
  const ph = panel.offsetHeight || 200;
  let left = anchor.left - 6;
  let top = anchor.top - ph - 10;
  left = Math.max(8, Math.min(left, window.innerWidth - pw - 8));
  top = Math.max(8, top);
  panel.style.left = left + 'px';
  panel.style.top = top + 'px';
  panel.style.right = 'auto';
  panel.style.bottom = 'auto';
}

function positionModeFlyout() {
  if (window.innerWidth <= 520) return;
  const sp = settingsPanel.getBoundingClientRect();
  const panel = modeFlyout;
  const pw = panel.offsetWidth  || 320;
  const ph = panel.offsetHeight || 200;
  const gap = 8;
  let left = sp.left - pw - gap;
  let top  = sp.top;
  // Not enough room on the left → try right
  if (left < gap) {
    const rightCandidate = sp.right + gap;
    if (rightCandidate + pw <= window.innerWidth - gap) {
      left = rightCandidate;
    } else {
      // Both sides blocked → open above, aligned to settings panel left
      left = sp.left;
      top  = sp.top - ph - gap;
    }
  }
  top = Math.max(gap, Math.min(top, window.innerHeight - ph - gap));
  panel.style.left   = left + 'px';
  panel.style.top    = top  + 'px';
  panel.style.right  = 'auto';
  panel.style.bottom = 'auto';
}

function positionSourcesFlyout() {
  if (window.innerWidth <= 520) return;
  const sp = settingsPanel.getBoundingClientRect();
  const panel = sourcesFlyout;
  const pw = panel.offsetWidth  || 320;
  const ph = panel.offsetHeight || 200;
  const gap = 8;
  let left = sp.left - pw - gap;
  let top  = sp.top;
  if (left < gap) {
    const rightCandidate = sp.right + gap;
    if (rightCandidate + pw <= window.innerWidth - gap) {
      left = rightCandidate;
    } else {
      left = sp.left;
      top  = sp.top - ph - gap;
    }
  }
  top = Math.max(gap, Math.min(top, window.innerHeight - ph - gap));
  panel.style.left   = left + 'px';
  panel.style.top    = top  + 'px';
  panel.style.right  = 'auto';
  panel.style.bottom = 'auto';
}

function positionOpenOverlays() {
  if (chatDropdown.classList.contains('open')) positionChatDropdown();
  if (settingsPanel.classList.contains('open')) {
    positionSettingsPanel();
    if (modeFlyout.classList.contains('open')) positionModeFlyout();
    if (sourcesFlyout.classList.contains('open')) positionSourcesFlyout();
  }
}

function openWidget() {
  state.isOpen = true;
  widget.style.left   = '';
  widget.style.top    = '';
  _loadConversationHistory().catch(() => {});
  launcher.style.left = '';
  launcher.style.top  = '';
  if (state.savedRight !== null && state.savedBottom !== null) {
    syncCornerPosition(state.savedRight, state.savedBottom);
  }
  widget.classList.add('open');
  widget.setAttribute('aria-hidden', 'false');
  launcher.classList.add('hidden');
  setTimeout(() => chatInput.focus(), 280);
}

function closeWidget() {
  const r = widget.getBoundingClientRect();
  state.savedRight  = window.innerWidth  - r.right;
  state.savedBottom = window.innerHeight - r.bottom;

  state.isOpen = false;
  widget.classList.remove('open');
  widget.setAttribute('aria-hidden', 'true');
  launcher.classList.remove('hidden');
  closeSettings();
  closeChatDropdown();
  exitOperator(false);


  launcher.style.left   = '';
  launcher.style.top    = '';
  launcher.style.right  = DEFAULT_CORNER.right  + 'px';
  launcher.style.bottom = DEFAULT_CORNER.bottom + 'px';


  widget.style.left   = '';
  widget.style.top    = '';
  widget.style.right  = DEFAULT_CORNER.right  + 'px';
  widget.style.bottom = DEFAULT_CORNER.bottom + 'px';
}

launcher.addEventListener('click', openWidget);
minimizeBtn.addEventListener('click', closeWidget);

// Close on Escape
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    if (state.viewMode === 'operator') exitOperator(true);
    else if (chatDropdown.classList.contains('open')) closeChatDropdown();
    else if (settingsPanel.classList.contains('open')) closeSettings();
    else if (state.isOpen) closeWidget();
  }
});

/* ──────────────────────────────────────────────
   7. DRAGGING
   ────────────────────────────────────────────── */
let drag = { active: false, startX: 0, startY: 0, startLeft: 0, startTop: 0, pointerId: null, raf: 0, pendingX: 0, pendingY: 0 };

function getWidgetPos() {
  const r = widget.getBoundingClientRect();
  return {
    right:  window.innerWidth  - r.right,
    bottom: window.innerHeight - r.bottom
  };
}

function applyDragPosition(x, y) {
  const ww = widget.offsetWidth;
  const wh = widget.offsetHeight;
  const clampedX = Math.max(0, Math.min(x, window.innerWidth - ww));
  const clampedY = Math.max(0, Math.min(y, window.innerHeight - wh));
  widget.style.left = clampedX + 'px';
  widget.style.top = clampedY + 'px';
  widget.style.right = 'auto';
  widget.style.bottom = 'auto';
  positionOpenOverlays();
}

function onDragMove(e) {
  if (!drag.active) return;
  drag.pendingX = drag.startLeft + (e.clientX - drag.startX);
  drag.pendingY = drag.startTop + (e.clientY - drag.startY);
  if (drag.raf) return;
  drag.raf = requestAnimationFrame(() => {
    drag.raf = 0;
    applyDragPosition(drag.pendingX, drag.pendingY);
  });
}

function stopDrag() {
  if (!drag.active) return;
  drag.active = false;
  if (drag.raf) { cancelAnimationFrame(drag.raf); drag.raf = 0; }
  widget.classList.remove('dragging');
  document.removeEventListener('pointermove', onDragMove);
  document.removeEventListener('pointerup', stopDrag);
  document.removeEventListener('pointercancel', stopDrag);
  if (drag.pointerId != null) {
    try { widgetHeader.releasePointerCapture(drag.pointerId); } catch (_) {}
    drag.pointerId = null;
  }
  widget.style.transition = '';
  const r = widget.getBoundingClientRect();
  syncCornerPosition(window.innerWidth - r.right, window.innerHeight - r.bottom);
}

widgetHeader.addEventListener('pointerdown', e => {
  if (e.button !== 0) return;
  if (e.target.closest('button')) return;
  if (window.innerWidth <= 520) return;
  const r = widget.getBoundingClientRect();
  drag.active = true;
  drag.pointerId = e.pointerId;
  drag.startLeft = r.left;
  drag.startTop = r.top;
  drag.startX = e.clientX;
  drag.startY = e.clientY;
  widget.classList.add('dragging');
  widget.style.transition = 'none';
  widget.style.left = r.left + 'px';
  widget.style.top = r.top + 'px';
  widget.style.right = 'auto';
  widget.style.bottom = 'auto';
  widgetHeader.setPointerCapture(e.pointerId);
  document.addEventListener('pointermove', onDragMove);
  document.addEventListener('pointerup', stopDrag);
  document.addEventListener('pointercancel', stopDrag);
  e.preventDefault();
});

/* ──────────────────────────────────────────────
   8. CHAT MESSAGING
   ────────────────────────────────────────────── */
function now() {
  return new Date().toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
}

function appendMessage(role, content, confirmData = null) {
  const time = now();
  const msgIndex = state.messages.length;
  state.messages.push({ role, content, time });

  const div = document.createElement('div');
  div.className = `msg msg-${role}`;

  const bubble = document.createElement('div');
  bubble.className = 'bubble';

  const text = document.createElement('span');
  text.className = 'bubble-text';
  text.textContent = content;

  const ts = document.createElement('span');
  ts.className = 'msg-time';
  ts.textContent = time;

  bubble.appendChild(text);

  // HIGH-01: recipient details form
  if (confirmData && confirmData._needsRecipientDetails) {
    const rdPanel = _buildRecipientDetailsPanel(
      confirmData.draftId,
      confirmData.draftDetails,
      confirmData.riskLevel,
      bubble,
    );
    bubble.appendChild(rdPanel);
  }

  if (confirmData && !confirmData._needsRecipientDetails) {
    const panel = document.createElement('div');
    panel.className = 'confirm-panel';

    // HIGH-03 / MEDIUM-01: full payment confirmation screen
    if (confirmData.draftDetails && confirmData.draftDetails.action === 'initiate_transfer') {
      const d = confirmData.draftDetails;
      const ri = d.recipient_info || {};
      const draftEl = document.createElement('div');
      draftEl.className = 'draft-info';

      function addRow(label, valueHtml) {
        const row = document.createElement('div');
        row.className = 'draft-row';
        row.innerHTML = '<span class="draft-label">' + label + ':</span>'
          + '<span class="draft-value">' + valueHtml + '</span>';
        draftEl.appendChild(row);
      }

      // Title
      const titleEl = document.createElement('div');
      titleEl.className = 'draft-title';
      titleEl.textContent = 'Черновик платёжного поручения';
      draftEl.appendChild(titleEl);

      // Recipient name
      addRow('Получатель', d.recipient || '—');

      // Recipient status badge (HIGH-02)
      const isExisting = (d.recipient_status || '').indexOf('Существующий') >= 0;
      const statusClass = isExisting ? 'draft-status-existing' : 'draft-status-new';
      addRow('Статус', '<span class="draft-status-badge ' + statusClass + '">'
        + (d.recipient_status || '—') + '</span>');

      // Bank and account (HIGH-02)
      if (ri.bank && ri.bank !== '—') addRow('Банк получателя', ri.bank);
      if (ri.account_masked && ri.account_masked !== '—') {
        addRow('Счёт', ri.account_masked);
      }

      // Section divider: amount + purpose + risk
      const secEl = document.createElement('div');
      secEl.className = 'draft-section';
      draftEl.appendChild(secEl);

      // Amount (HIGH-03)
      const amtStr = (d.amount != null)
        ? Number(d.amount).toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
          + ' ' + (d.currency || 'BYN')
        : '—';
      const amtRow = document.createElement('div');
      amtRow.className = 'draft-row';
      amtRow.innerHTML = '<span class="draft-label">Сумма:</span>'
        + '<span class="draft-value draft-amount-val">' + amtStr + '</span>';
      secEl.appendChild(amtRow);

      // Payment purpose (MEDIUM-01)
      if (d.purpose) {
        const purposeRow = document.createElement('div');
        purposeRow.className = 'draft-row';
        purposeRow.innerHTML = '<span class="draft-label">Назначение:</span>'
          + '<span class="draft-value">' + d.purpose + '</span>';
        secEl.appendChild(purposeRow);
      }


      // Security warnings (MEDIUM-01)
      const reasons = d.risk_reasons || [];
      if (reasons.length > 0) {
        const warnEl = document.createElement('div');
        warnEl.className = 'draft-warnings';
        const warnTitle = document.createElement('div');
        warnTitle.className = 'draft-warn-title';
        warnTitle.textContent = 'Предупреждения безопасности:';
        warnEl.appendChild(warnTitle);
        reasons.forEach(function(r) {
          const item = document.createElement('div');
          item.className = 'draft-warning-item';
          item.textContent = '• ' + r;
          warnEl.appendChild(item);
        });
        draftEl.appendChild(warnEl);
      }

      panel.appendChild(draftEl);
    }

    const btnsEl = document.createElement('div');
    btnsEl.className = 'confirm-btns';

    const yesBtn = document.createElement('button');
    yesBtn.className = 'confirm-btn confirm-yes';
    yesBtn.textContent = 'Подтвердить';
    yesBtn.onclick = () => handleConfirm(confirmData.token, true, panel);

    const noBtn = document.createElement('button');
    noBtn.className = 'confirm-btn confirm-no';
    noBtn.textContent = 'Отменить';
    noBtn.onclick = () => handleConfirm(confirmData.token, false, panel);

    btnsEl.appendChild(yesBtn);
    btnsEl.appendChild(noBtn);
    panel.appendChild(btnsEl);
    bubble.appendChild(panel);
  }

  bubble.appendChild(ts);
  div.appendChild(bubble);
  screenMsg.appendChild(div);
  screenMsg.scrollTop = screenMsg.scrollHeight;
}

/* ──────────────────────────────────────────────
   HIGH-01: Recipient details form
   ────────────────────────────────────────────── */

function _buildRecipientDetailsPanel(draftId, draftDetails, riskLevel, bubble) {
  const panel = document.createElement('div');
  panel.className = 'confirm-panel recipient-details-panel';

  // Header
  const hdr = document.createElement('div');
  hdr.className = 'draft-title';
  hdr.textContent = 'Реквизиты нового контрагента';
  panel.appendChild(hdr);

  // Show parsed amount + recipient from draftDetails
  if (draftDetails) {
    const infoEl = document.createElement('div');
    infoEl.style.cssText = 'font-size:13px;color:#374151;margin-bottom:12px;padding:8px 10px;background:#f0fdf4;border-radius:6px;';
    const amt = draftDetails.amount != null
      ? Number(draftDetails.amount).toLocaleString('ru-RU', { minimumFractionDigits: 2 }) + ' ' + (draftDetails.currency || 'BYN')
      : '—';
    infoEl.textContent = 'Перевод: ' + amt + ' → ' + (draftDetails.recipient || '—');
    panel.appendChild(infoEl);
  }

  function makeField(labelText, inputId, placeholder, type) {
    const wrap = document.createElement('div');
    wrap.style.cssText = 'margin-bottom:10px;';
    const lbl = document.createElement('label');
    lbl.htmlFor = inputId;
    lbl.style.cssText = 'display:block;font-size:12px;color:#6b7280;margin-bottom:3px;';
    lbl.textContent = labelText;
    const inp = document.createElement(type === 'select' ? 'select' : 'input');
    inp.id = inputId;
    inp.style.cssText = 'width:100%;padding:7px 10px;border:1px solid #d1d5db;border-radius:6px;'
      + 'font-size:13px;box-sizing:border-box;font-family:inherit;';
    if (type !== 'select') {
      inp.type = 'text';
      inp.placeholder = placeholder;
    }
    wrap.appendChild(lbl);
    wrap.appendChild(inp);
    return { wrap, inp };
  }

  const uid = '_rd_' + Math.random().toString(36).slice(2, 8);
  const { wrap: w1, inp: accountInp }  = makeField('Номер расчётного счёта (IBAN BY или иной)', uid + '_acc', 'BY13PJCB30130001234567890000');
  const { wrap: w2, inp: bankInp }     = makeField('Банк получателя', uid + '_bank', 'ОАО «Приорбанк»');
  const { wrap: w3, inp: currInp }     = makeField('Валюта счёта', uid + '_cur', '', 'select');
  const { wrap: w4, inp: purposeInp }  = makeField('Назначение платежа', uid + '_pur', 'test');

  ['BYN', 'USD', 'EUR', 'RUB'].forEach(c => {
    const opt = document.createElement('option');
    opt.value = c;
    opt.textContent = c;
    currInp.appendChild(opt);
  });

  // Real-time BY IBAN hint shown under the account field
  const ibanHint = document.createElement('div');
  ibanHint.style.cssText = 'font-size:11px;margin-top:3px;min-height:15px;color:#6b7280;';
  w1.appendChild(ibanHint);

  accountInp.addEventListener('input', () => {
    const norm = accountInp.value.trim().toUpperCase().replace(/\s/g, '');
    if (!norm) { ibanHint.textContent = ''; accountInp.style.borderColor = ''; return; }
    if (norm.startsWith('BY')) {
      const len = norm.length;
      const valid = /^BY\d{2}[A-Z0-9]{24}$/.test(norm);
      if (valid) {
        ibanHint.style.color = '#16a34a';
        ibanHint.textContent = '✓ Формат BY IBAN корректен';
        accountInp.style.borderColor = '#16a34a';
      } else {
        ibanHint.style.color = len < 28 ? '#d97706' : '#dc2626';
        ibanHint.textContent = `BY IBAN: ${len}/28 символов — BY + 2 цифры + 24 буквы/цифры`;
        accountInp.style.borderColor = len < 28 ? '#d97706' : '#dc2626';
      }
    } else {
      const validOther = /^[A-Za-z0-9 \-\.]{5,100}$/.test(accountInp.value.trim());
      if (validOther) {
        ibanHint.style.color = '#16a34a';
        ibanHint.textContent = '✓ Формат счёта принят';
        accountInp.style.borderColor = '#16a34a';
      } else {
        ibanHint.style.color = '#dc2626';
        ibanHint.textContent = 'Разрешены: буквы, цифры, дефис, точка (мин. 5 символов)';
        accountInp.style.borderColor = '#dc2626';
      }
    }
  });

  panel.appendChild(w1);
  panel.appendChild(w2);
  panel.appendChild(w3);
  panel.appendChild(w4);

  const errEl = document.createElement('div');
  errEl.style.cssText = 'color:#dc2626;font-size:12px;margin-top:4px;min-height:16px;';
  panel.appendChild(errEl);

  const btnsEl = document.createElement('div');
  btnsEl.className = 'confirm-btns';

  const submitBtn = document.createElement('button');
  submitBtn.className = 'confirm-btn confirm-yes';
  submitBtn.textContent = 'Проверить и продолжить';

  const cancelBtn = document.createElement('button');
  cancelBtn.className = 'confirm-btn confirm-no';
  cancelBtn.textContent = 'Отменить';

  cancelBtn.onclick = () => {
    panel.remove();
    appendMessage('ai', 'Перевод отменён.');
  };

  submitBtn.onclick = async () => {
    errEl.textContent = '';
    const account  = accountInp.value.trim();
    const bankName = bankInp.value.trim();
    const currency = currInp.value;
    const purpose  = purposeInp.value.trim();

    if (!account) { errEl.textContent = 'Укажите номер счёта.'; return; }
    const accountNorm = account.toUpperCase().replace(/\s/g, '');
    if (accountNorm.startsWith('BY')) {
      if (!/^BY\d{2}[A-Z0-9]{24}$/.test(accountNorm)) {
        errEl.textContent = 'Неверный формат BY IBAN. Требуется 28 символов: BY + 2 цифры + 24 буквы/цифры. Пример: BY13PJCB30130001234567890000';
        return;
      }
    }
    if (!bankName) { errEl.textContent = 'Укажите название банка.'; return; }
    if (!purpose)  { errEl.textContent = 'Укажите назначение платежа.'; return; }

    submitBtn.disabled = true;
    cancelBtn.disabled = true;
    submitBtn.textContent = 'Проверяем…';

    try {
      const resp = await fetch('/api/v1/transfer/recipient-details', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-API-Key': _apiKey },
        body: JSON.stringify({
          user_id: _sessionUserId || 'user_001',
          draft_id: draftId,
          account_number: account,
          bank_name: bankName,
          currency: currency,
          purpose: purpose,
        }),
      });
      const rdata = await resp.json();
      if (!resp.ok) {
        const det = rdata.detail;
        errEl.textContent = Array.isArray(det)
          ? det.map(e => e.msg || e.message || JSON.stringify(e)).join('; ')
          : (det || 'Ошибка при проверке реквизитов.');
        submitBtn.disabled = false;
        cancelBtn.disabled = false;
        submitBtn.textContent = 'Проверить и продолжить';
        return;
      }
      // Swap the details form for the confirmation panel
      panel.remove();
      appendMessage('ai', rdata.message || 'Реквизиты приняты. Подтвердите перевод.', {
        token: rdata.confirmation_token,
        draftDetails: rdata.draft_details || null,
      });
    } catch (e) {
      errEl.textContent = 'Ошибка соединения. Попробуйте ещё раз.';
      submitBtn.disabled = false;
      cancelBtn.disabled = false;
      submitBtn.textContent = 'Проверить и продолжить';
    }
  };

  btnsEl.appendChild(submitBtn);
  btnsEl.appendChild(cancelBtn);
  panel.appendChild(btnsEl);
  return panel;
}

async function handleConfirm(token, confirmed, panel) {
  panel.querySelectorAll('.confirm-btn').forEach(b => { b.disabled = true; });
  try {
    const r = await fetch('/api/v1/confirm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-API-Key': _apiKey },
      body: JSON.stringify({ user_id: _sessionUserId || 'user_001', confirmation_token: token, confirmed }),
    });
    const data = await r.json();
    panel.remove();
    let resultText = data.message || 'Готово.';
    if (data.result && typeof data.result === 'object') {
      resultText += '\n\n' + _formatResult(data.result);
      if (data.result.matched_section) {
        _navigateToSection(data.result.matched_section.section_id);
      }
    }
    appendMessage('ai', resultText);
  } catch {
    panel.remove();
    appendMessage('ai', 'Ошибка при обработке подтверждения.');
  }
}

function showTyping() {
  if ($('typingIndicator')) return;
  const wrap = document.createElement('div');
  wrap.className = 'msg typing-wrap';
  wrap.id = 'typingIndicator';
  wrap.innerHTML = `<div class="typing"><span></span><span></span><span></span></div>`;
  screenMsg.appendChild(wrap);
  screenMsg.scrollTop = screenMsg.scrollHeight;
}
function hideTyping() {
  const el = $('typingIndicator');
  if (el) el.remove();
}

function showMainView(mode) {
  state.viewMode = mode;
  screenWelcome.classList.toggle('hidden', mode !== 'welcome');
  screenMsg.classList.toggle('visible', mode === 'chat');
  messagesScrollWrap.classList.toggle('visible', mode === 'chat');
  screenOperator.classList.toggle('visible', mode === 'operator');
  operatorBtn.classList.toggle('active', mode === 'operator');
  const operatorBtnImg = operatorBtn.querySelector('img');
  if (operatorBtnImg) {
    operatorBtnImg.src = mode === 'operator'
      ? 'icon/tabler_sberik.svg'
      : 'icon/tabler_user-question.svg';
  }
}

function switchToMessages() {
  if (state.hasHistory && state.viewMode === 'chat') return;
  state.hasHistory = true;
  showMainView('chat');
  widget.classList.add('has-history');
  widget.style.width = '407px';
  chatTitle.textContent = state.savedTitle || 'Новый ИИ Чат';
  updateChatTitleTooltip();
}

async function sendMessage(text) {
  if (!text.trim() || state.isTyping) return;
  if (state.viewMode === 'operator') exitOperator(true);
  switchToMessages();
  appendMessage('user', text);
  chatInput.value = '';
  updateSendBtn();
  state.isTyping = true;
  EyeEngine.setMode('header', 'thinking');
  showTyping();

  try {
    const reply = await AIService.getReply(text);
    hideTyping();
    if (reply && reply._needsRecipientDetails) {
      // HIGH-01: unknown counterparty — show details form
      appendMessage('ai', reply.message, {
        _needsRecipientDetails: true,
        draftId: reply.draftId,
        draftDetails: reply.draftDetails,
        riskLevel: reply.riskLevel,
      });
    } else if (reply && reply._needsConfirm) {
      appendMessage('ai', reply.message, { token: reply.token, draftDetails: reply.draftDetails });
    } else {
      appendMessage('ai', reply);
    }
    EyeEngine.setMode('header', 'idle');
    _loadConversationHistory().catch(() => {});
  } catch (err) {
    hideTyping();
    appendMessage('ai', 'Ошибка: ' + (err.message || 'Попробуйте ещё раз.'));
  } finally {
    state.isTyping = false;
    EyeEngine.setMode('header', 'idle');
  }
}

/* Input events */
function updateComposerLayout() {
  const hasText = chatInput.value.trim().length > 0;
  composer.classList.toggle('is-expanded', hasText);
}

function autoResizeTextarea(el) {
  updateComposerLayout();
  if (!el.value.trim()) {
    el.classList.remove('has-overflow');
    el.style.overflowY = 'hidden';
    return;
  }
  el.style.overflowY = 'auto';
  requestAnimationFrame(() => {
    const needsScroll = el.scrollHeight > el.clientHeight + 1;
    el.classList.toggle('has-overflow', needsScroll);
    if (!needsScroll) el.style.overflowY = 'auto';
  });
}

chatInput.addEventListener('input', () => {
  autoResizeTextarea(chatInput);
  updateSendBtn();
});
chatInput.addEventListener('focus', () => autoResizeTextarea(chatInput));
chatInput.addEventListener('blur', () => {
  if (!chatInput.value.trim()) updateComposerLayout();
});
chatInput.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    if (state.viewMode === 'operator') sendOperatorMessage();
    else sendMessage(chatInput.value);
  }
});
sendBtn.addEventListener('click', () => {
  if (state.viewMode === 'operator') sendOperatorMessage();
  else sendMessage(chatInput.value);
});

function updateSendBtn() {
  const has = chatInput.value.trim().length > 0;
  sendBtn.disabled = !has;
  sendBtn.classList.toggle('active', has);
  if (sendBtnIcon) sendBtnIcon.src = has ? SEND_ICON_ACTIVE : SEND_ICON_IDLE;
  if (!has) {
    autoResizeTextarea(chatInput);
    updateComposerLayout();
  }
}
updateSendBtn();
updateComposerLayout();

function updateChatTitleTooltip() {
  const text = chatTitle.textContent.trim();
  chatTitle.title = text;
  chatTitleBtn.title = text;
}
updateChatTitleTooltip();

chatTitle.addEventListener('dblclick', e => {
  e.stopPropagation();
  if (!_conversationId) return;
  const prev = chatTitle.textContent.trim();
  const inp = document.createElement('input');
  inp.className = 'chat-title-edit';
  inp.value = prev;
  inp.maxLength = 50;
  chatTitle.textContent = '';
  chatTitle.appendChild(inp);
  inp.focus();
  inp.select();
  let committed = false;
  const commit = async () => {
    if (committed) return;
    committed = true;
    const next = inp.value.trim();
    chatTitle.textContent = next || prev;
    state.savedTitle = chatTitle.textContent;
    updateChatTitleTooltip();
    if (next && next !== prev && _conversationId) {
      try {
        await fetch('/api/v1/conversations/' + encodeURIComponent(_conversationId) + '/title', {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json', 'X-API-Key': _apiKey },
          body: JSON.stringify({ user_id: _sessionUserId, title: next }),
        });
        _loadConversationHistory().catch(() => {});
      } catch (_) {}
    }
  };
  const cancel = () => { chatTitle.textContent = prev; };
  inp.addEventListener('keydown', e2 => {
    if (e2.key === 'Enter') { e2.preventDefault(); commit(); }
    if (e2.key === 'Escape') { committed = true; cancel(); }
  });
  inp.addEventListener('blur', commit);
});

function initDropdownTooltips() {
  chatDropdown.querySelectorAll('.dropdown-item > span:first-child').forEach(el => {
    el.title = el.textContent.trim();
  });
}
initDropdownTooltips();

/* ──────────────────────────────────────────────
   CONVERSATION HISTORY
   ────────────────────────────────────────────── */
function _escHtml(str) {
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

async function _loadConversationHistory() {
  if (!_sessionUserId) return;
  const historyList = $('historyList');
  const historyEmpty = $('historyEmpty');
  if (!historyList) return;
  try {
    const r = await fetch('/api/v1/conversations?user_id=' + encodeURIComponent(_sessionUserId), {
      headers: { 'X-API-Key': _apiKey },
    });
    if (!r.ok) return;
    const data = await r.json();
    const convs = data.conversations || [];
    historyList.querySelectorAll('[data-conv-id]').forEach(el => el.remove());
    if (convs.length === 0) {
      if (historyEmpty) historyEmpty.style.display = '';
      return;
    }
    if (historyEmpty) historyEmpty.style.display = 'none';
    convs.forEach(conv => {
      const btn = document.createElement('button');
      btn.className = 'dropdown-item';
      btn.type = 'button';
      btn.dataset.convId = conv.conversation_id;
      btn.title = conv.title || 'Диалог';
      btn.dataset.convTitle = conv.title || 'Диалог';
      btn.innerHTML =
        '<span class="history-title">' + _escHtml(conv.title || 'Диалог') + '</span>' +
        '<span class="dropdown-spacer"></span>' +
        '<span class="history-rename-icon" role="button" tabindex="0" title="Переименовать" ' +
          'data-rename-conv-id="' + _escHtml(conv.conversation_id) + '">✏</span>';
      historyList.appendChild(btn);
    });
  } catch (_) {}
}

async function _openConversation(convId, title) {
  if (!_sessionUserId) return;
  const displayTitle = (title || 'Диалог').trim();
  state.messages = [];
  state.hasHistory = true;
  screenMsg.innerHTML = '';
  _conversationId = convId;
  chatTitle.textContent = displayTitle;
  state.savedTitle = displayTitle;
  showMainView('chat');
  widget.classList.add('has-history');
  widget.style.width = '407px';
  updateChatTitleTooltip();
  appendMessage('ai', 'История переписки недоступна для просмотра. Вы можете продолжить этот диалог — контекст сохранён.');
}

/* Quick chips */
quickChips.addEventListener('click', e => {
  const btn = e.target.closest('[data-prompt]');
  if (!btn) return;
  sendMessage(btn.dataset.prompt);
});

/* ──────────────────────────────────────────────
   9. SETTINGS
   ────────────────────────────────────────────── */
function toggleChatDropdown() {
  const open = !chatDropdown.classList.contains('open');
  closeSettings();
  chatDropdown.classList.toggle('open', open);
  chatTitleBtn.setAttribute('aria-expanded', String(open));
  if (open) requestAnimationFrame(positionChatDropdown);
}
function closeChatDropdown() {
  chatDropdown.classList.remove('open');
  chatTitleBtn.setAttribute('aria-expanded', 'false');
}
function toggleSettings() {
  const open = !settingsPanel.classList.contains('open');
  closeChatDropdown();
  settingsPanel.classList.toggle('open', open);
  if (open) requestAnimationFrame(positionSettingsPanel);
  else closeFlyouts();
}
function closeSettings() {
  settingsPanel.classList.remove('open');
  closeFlyouts();
}
function closeFlyouts() {
  modeFlyout.classList.remove('open');
  sourcesFlyout.classList.remove('open');
  modeMenuBtn.classList.remove('active');
  sourcesMenuBtn.classList.remove('active');
  modeMenuBtn.setAttribute('aria-expanded', 'false');
  sourcesMenuBtn.setAttribute('aria-expanded', 'false');
}
chatTitleBtn.addEventListener('click', e => { e.stopPropagation(); toggleChatDropdown(); });
composerSettingsBtn.addEventListener('click', e => { e.stopPropagation(); toggleSettings(); });
document.addEventListener('click', e => {
  const insideSettings = settingsPanel.contains(e.target) || modeFlyout.contains(e.target) || sourcesFlyout.contains(e.target) || e.target === composerSettingsBtn || composerSettingsBtn.contains(e.target);
  const insideChats = chatDropdown.contains(e.target) || e.target === chatTitleBtn || chatTitleBtn.contains(e.target);
  if (!insideSettings) closeSettings();
  if (!insideChats) closeChatDropdown();
});

$('settingFinance').addEventListener('click', e => {
  if (e.target.closest('.toggle-wrap')) e.preventDefault();
  state.financeOn = !state.financeOn;
  financeToggle.classList.toggle('on', state.financeOn);
});

modeMenuBtn.addEventListener('click', e => {
  e.stopPropagation();
  const open = !modeFlyout.classList.contains('open');
  closeFlyouts();
  modeFlyout.classList.toggle('open', open);
  modeMenuBtn.classList.toggle('active', open);
  modeMenuBtn.setAttribute('aria-expanded', String(open));
  if (open) requestAnimationFrame(positionModeFlyout);
});

sourcesMenuBtn.addEventListener('click', e => {
  e.stopPropagation();
  const open = !sourcesFlyout.classList.contains('open');
  closeFlyouts();
  sourcesFlyout.classList.toggle('open', open);
  sourcesMenuBtn.classList.toggle('active', open);
  sourcesMenuBtn.setAttribute('aria-expanded', String(open));
  if (open) requestAnimationFrame(positionSourcesFlyout);
});

modeFlyout.addEventListener('click', e => {
  const btn = e.target.closest('.mode-choice');
  if (!btn) return;
  modeFlyout.querySelectorAll('.mode-choice').forEach(item => {
    const selected = item === btn;
    item.classList.toggle('active', selected);
    item.setAttribute('aria-checked', String(selected));
  });
  modeValue.textContent = btn.dataset.mode;
  const rawMode = (btn.dataset.mode || '').toLowerCase().trim();
  const backendMode = rawMode.includes('помощник') || rawMode === 'assistant' ? 'assistant' : 'banking';
  _updateModeIndicator(backendMode);
  showToast(`Режим: ${btn.querySelector('strong').textContent}`);
});

sourcesFlyout.addEventListener('click', e => {
  const btn = e.target.closest('.source-row');
  if (!btn) return;
  const toggle = btn.querySelector('.toggle-wrap');

  if (btn.dataset.source === 'all') {
    toggle.classList.toggle('on');
    const isOn = toggle.classList.contains('on');
    sourcesFlyout.querySelectorAll('.source-row:not([data-source="all"]) .toggle-wrap')
      .forEach(t => t.classList.toggle('on', isOn));
  } else {
    // Individual toggle: flip it
    toggle.classList.toggle('on');
    // Master is ON only when ALL individuals are ON; off the moment any single one goes off
    const individualToggles = [...sourcesFlyout.querySelectorAll('.source-row:not([data-source="all"]) .toggle-wrap')];
    const allOn = individualToggles.every(t => t.classList.contains('on'));
    const masterToggle = sourcesFlyout.querySelector('.source-row[data-source="all"] .toggle-wrap');
    if (masterToggle) masterToggle.classList.toggle('on', allOn);
  }

  // Recount from individual toggles only
  const active = [...sourcesFlyout.querySelectorAll('.source-row:not([data-source="all"]) .toggle-wrap.on')].length;
  sourcesCount.textContent = active;
});

chatDropdown.addEventListener('click', async e => {
  const renameIcon = e.target.closest('[data-rename-conv-id]');
  if (renameIcon) {
    e.stopPropagation();
    _startInlineRename(renameIcon);
    return;
  }
  const btn = e.target.closest('[data-conv-id]');
  if (!btn) return;
  closeChatDropdown();
  await _openConversation(btn.dataset.convId, btn.dataset.convTitle);
});

function _startInlineRename(renameIcon) {
  const item = renameIcon.closest('[data-conv-id]');
  if (!item) return;
  const titleSpan = item.querySelector('.history-title');
  const convId = renameIcon.dataset.renameConvId;
  const prev = titleSpan.textContent.trim();

  const inp = document.createElement('input');
  inp.className = 'history-rename-input';
  inp.value = prev;
  inp.maxLength = 50;
  titleSpan.replaceWith(inp);
  inp.focus();
  inp.select();

  let committed = false;
  const commit = async () => {
    if (committed) return;
    committed = true;
    const next = inp.value.trim();
    const finalTitle = next || prev;
    const newSpan = document.createElement('span');
    newSpan.className = 'history-title';
    newSpan.textContent = finalTitle;
    inp.replaceWith(newSpan);
    item.title = finalTitle;
    if (next && next !== prev) {
      try {
        await fetch('/api/v1/conversations/' + encodeURIComponent(convId) + '/title', {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json', 'X-API-Key': _apiKey },
          body: JSON.stringify({ user_id: _sessionUserId, title: next }),
        });
        if (_conversationId === convId) {
          chatTitle.textContent = next;
          state.savedTitle = next;
          updateChatTitleTooltip();
        }
        _loadConversationHistory().catch(() => {});
      } catch (_) {}
    }
  };
  const cancel = () => {
    committed = true;
    const origSpan = document.createElement('span');
    origSpan.className = 'history-title';
    origSpan.textContent = prev;
    inp.replaceWith(origSpan);
  };
  inp.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); commit(); }
    if (e.key === 'Escape') cancel();
  });
  inp.addEventListener('blur', commit);
}

function _doNewChat() {
  _conversationId = null;
  state.messages = [];
  state.hasHistory = false;
  state.isTyping = false;
  state.savedTitle = 'Новый ИИ Чат';
  hideTyping();
  screenMsg.innerHTML = '';
  showMainView('welcome');
  widget.classList.remove('has-history', 'operator-active', 'operator-connected');
  widget.style.width = '';
  chatInput.value = '';
  updateSendBtn();
  closeSettings();
  closeChatDropdown();
  chatTitle.textContent = 'Новый ИИ Чат';
  updateChatTitleTooltip();
  exitOperator(false);
}

if (deleteChatBtn) {
  deleteChatBtn.addEventListener('click', async () => {
    const convIdToDelete = _conversationId;
    _doNewChat();
    if (convIdToDelete && _sessionUserId) {
      try {
        await fetch(
          '/api/v1/conversations/' + encodeURIComponent(convIdToDelete) +
          '?user_id=' + encodeURIComponent(_sessionUserId),
          { method: 'DELETE', headers: { 'X-API-Key': _apiKey } }
        );
        _loadConversationHistory().catch(() => {});
      } catch (_) {}
    }
  });
}

const chatDropdownNewBtn = $('chatDropdownNewBtn');
if (chatDropdownNewBtn) {
  chatDropdownNewBtn.addEventListener('click', () => {
    _doNewChat();
  });
}

/* ──────────────────────────────────────────────
   10. OPERATOR (single-window)
   ────────────────────────────────────────────── */
function addOpBubble(role, text) {
  const b = document.createElement('div');
  b.className = `op-bubble ${role}`;
  b.textContent = text;
  opMessages.appendChild(b);
  opMessages.scrollTop = opMessages.scrollHeight;
}

function showOperatorChat() {
  opSearching.style.display = 'none';
  opMessages.classList.add('visible');
  widget.classList.add('operator-connected');
  opMessages.innerHTML = '';
}

function enterOperator() {
  closeSettings();
  closeChatDropdown();
  state.savedTitle = chatTitle.textContent;
  chatTitle.textContent = 'Оператор №23';
  updateChatTitleTooltip();
  widget.classList.add('operator-active');
  widget.classList.remove('operator-connected');
  showMainView('operator');
  opSearching.style.display = 'flex';
  opMessages.classList.remove('visible');
  opMessages.innerHTML = '';
  if (state.operatorTimer) clearTimeout(state.operatorTimer);
  state.operatorTimer = setTimeout(() => {
    if (state.viewMode !== 'operator') return;
    showOperatorChat();
    addOpBubble('agent', 'Здравствуйте! Я оператор поддержки Сбербанк. Чем могу помочь?');
  }, 2500);
}

function exitOperator(restoreTitle) {
  if (state.operatorTimer) { clearTimeout(state.operatorTimer); state.operatorTimer = null; }
  if (state.viewMode !== 'operator') return;
  opMessages.innerHTML = '';
  opMessages.classList.remove('visible');
  opSearching.style.display = 'flex';
  widget.classList.remove('operator-active', 'operator-connected');
  if (restoreTitle) chatTitle.textContent = state.savedTitle || 'Новый ИИ Чат';
  updateChatTitleTooltip();
  showMainView(state.hasHistory ? 'chat' : 'welcome');
}

function sendOperatorMessage() {
  const v = chatInput.value.trim();
  if (!v) return;
  if (!opMessages.classList.contains('visible')) {
    showOperatorChat();
  }
  addOpBubble('user', v);
  chatInput.value = '';
  updateSendBtn();
  setTimeout(() => addOpBubble('agent', 'Спасибо, передаю ваш запрос. Ожидайте пожалуйста…'), 800);
}

operatorBtn.addEventListener('click', () => {
  if (state.viewMode === 'operator') exitOperator(true);
  else enterOperator();
});

/* ──────────────────────────────────────────────
   11. UTILS & TOASTS
   ────────────────────────────────────────────── */
function showToast(text) {
  let container = $('toastContainer');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toastContainer';
    container.style.cssText = 'position:fixed;top:20px;left:50%;transform:translateX(-50%);z-index:10000;display:flex;flex-direction:column;gap:8px;pointer-events:none;';
    document.body.appendChild(container);
  }
  const t = document.createElement('div');
  t.style.cssText = 'background:rgba(0,0,0,.8);color:white;padding:8px 16px;border-radius:20px;font-size:14px;animation:toast-in .3s ease both;';
  t.textContent = text;
  container.appendChild(t);
  setTimeout(() => {
    t.style.animation = 'toast-out .3s ease both';
    setTimeout(() => t.remove(), 300);
  }, 2500);
}

// Add keyframes for toast + confirm panel + feedback styles
const style = document.createElement('style');
style.textContent = `
@keyframes toast-in { from { opacity:0; transform:translateY(-20px); } to { opacity:1; transform:none; } }
@keyframes toast-out { to { opacity:0; transform:translateY(-20px); } }
.confirm-panel { display:flex; flex-direction:column; margin-top:10px; }
.confirm-btns  { display:flex; gap:8px; margin-top:8px; }
.confirm-btn { padding:7px 18px; border:none; border-radius:20px; font-size:13px; cursor:pointer; font-family:inherit; }
.confirm-btn:disabled { opacity:.45; cursor:default; }
.confirm-yes { background:#1dbfb0; color:#fff; }
.confirm-yes:hover:not(:disabled) { background:#17a99c; }
.confirm-no  { background:#f0f0f0; color:#444; }
.confirm-no:hover:not(:disabled)  { background:#e0e0e0; }
.draft-info { background:#f0fdfa; border:1px solid #a7f3d0; border-radius:10px; padding:12px 14px; margin-bottom:4px; font-size:13px; }
.draft-title { font-weight:700; color:#065f46; font-size:13px; margin-bottom:9px; padding-bottom:7px; border-bottom:1px solid #d1fae5; }
.draft-row  { display:flex; justify-content:space-between; align-items:flex-start; gap:8px; padding:3px 0; }
.draft-label { color:#6b7280; flex-shrink:0; min-width:120px; }
.draft-value { font-weight:600; color:#1f2937; text-align:right; word-break:break-word; }
.draft-amount-val { font-size:15px; font-weight:700; color:#107f8c; }
.draft-status-badge { display:inline-block; padding:2px 9px; border-radius:12px; font-size:11px; font-weight:700; }
.draft-status-existing { background:#d1fae5; color:#065f46; }
.draft-status-new      { background:#fef3c7; color:#92400e; }
.draft-section { border-top:1px solid #d1fae5; margin-top:8px; padding-top:8px; }
.risk-badge { display:inline-block; padding:2px 9px; border-radius:12px; font-size:11px; font-weight:700; }
.risk-low    { background:#d1fae5; color:#065f46; }
.risk-medium { background:#fef3c7; color:#92400e; }
.risk-high   { background:#fee2e2; color:#991b1b; }
.draft-warnings { background:#fffbeb; border:1px solid #fde68a; border-radius:8px; padding:8px 10px; margin-top:8px; font-size:12px; color:#92400e; }
.draft-warn-title { font-weight:700; margin-bottom:4px; }
.draft-warning-item { margin:2px 0; }
`;
document.head.appendChild(style);

// Attach to mock composer buttons (except settings)
document.querySelectorAll('.composer-icon-btn:not(#composerSettingsBtn)').forEach(btn => {
  btn.addEventListener('click', () => {
    const label = btn.getAttribute('aria-label') || 'Действие';
    showToast(`${label} пока не реализован в прототипе`);
  });
});

/* ──────────────────────────────────────────────
   12. WINDOW RESIZE — keep widget in bounds
   ────────────────────────────────────────────── */
window.addEventListener('resize', () => {
  if (window.innerWidth <= 520) {
    positionOpenOverlays();
    return;
  }
  const r = widget.getBoundingClientRect();
  const ww = widget.offsetWidth, wh = widget.offsetHeight;
  const maxR = window.innerWidth  - ww;
  const maxB = window.innerHeight - wh;
  const curR = window.innerWidth  - r.right;
  const curB = window.innerHeight - r.bottom;
  const newR = Math.max(0, Math.min(curR, maxR));
  const newB = Math.max(0, Math.min(curB, maxB));
  if (curR !== newR || curB !== newB) syncCornerPosition(newR, newB);
  positionOpenOverlays();
});

/* Public API */
window.Sberik = {
  open:  openWidget,
  close: closeWidget,
  send:  sendMessage,
  setAIService: fn => { AIService.getReply = fn; }
};

// Kick off session init asynchronously — widget is usable before it resolves
_initSession();
_updateModeIndicator('banking');