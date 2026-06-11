/* ── Home Page - Global Chat & Dashboard ──────────────────────────────────── */

marked.use({ breaks: true, gfm: true });

/* ── State ────────────────────────────────────────────────────────────────── */

const _homeState = {
  conversationId: null,
  conversations: [],
  requestId: null,
  currentMsgWrap: null,
  currentChunks: [],
  currentToolCalls: [],
  busy: false,
};

let _sse = null;

/* ── Utilities ────────────────────────────────────────────────────────────── */

function escapeHtml(s) {
  const div = document.createElement('div');
  div.textContent = s;
  return div.innerHTML;
}

function renderMd(text) {
  return marked.parse(text || '');
}

function _addCodeCopyButtons(container) {
  container.querySelectorAll('pre').forEach(pre => {
    if (pre.querySelector('.code-copy-btn')) return;
    const btn = document.createElement('button');
    btn.className = 'code-copy-btn';
    btn.innerHTML = '<i class="fa-regular fa-copy"></i> Copy';
    btn.addEventListener('click', e => {
      e.stopPropagation();
      const code = pre.querySelector('code')?.innerText || pre.innerText;
      navigator.clipboard.writeText(code).then(() => {
        btn.classList.add('copied');
        btn.innerHTML = '<i class="fa-solid fa-check"></i> Copied';
        setTimeout(() => {
          btn.classList.remove('copied');
          btn.innerHTML = '<i class="fa-regular fa-copy"></i> Copy';
        }, 1500);
      });
    });
    pre.appendChild(btn);
  });
}

function _toolDisplayName(name) {
  const map = {
    get_screenshot: 'Screenshot',
    search_transcripts: 'Search Transcripts',
    semantic_search: 'Semantic Search',
    get_session_detail: 'Load Session',
    list_speakers: 'List Speakers',
    get_speaker_history: 'Speaker History',
    web_search: 'Web Search',
  };
  return map[name] || name;
}

function _toolInputSummary(name, input) {
  if (name === 'search_transcripts' && input?.query) return `"${input.query}"`;
  if (name === 'semantic_search' && input?.query) return `"${input.query}"`;
  if (name === 'get_session_detail' && input?.session_id) return input.session_id.substring(0, 8) + '...';
  if (name === 'list_speakers') return 'Voice Library';
  if (name === 'get_speaker_history' && input?.speaker_name) return `"${input.speaker_name}"`;
  if (name === 'web_search' && input?.query) return `"${input.query}"`;
  if (name === 'web_search') return 'searching…';
  return JSON.stringify(input || {});
}

function _timeAgo(isoDate) {
  const d = new Date(isoDate + 'Z');
  const now = Date.now();
  const diff = now - d.getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 7) return `${days}d ago`;
  return d.toLocaleDateString();
}

function _formatDuration(seconds) {
  if (!seconds || seconds <= 0) return '0m';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function _formatCompactNumber(n) {
  if (n >= 1000000) return (n / 1000000).toFixed(1).replace(/\.0$/, '') + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k';
  return String(n);
}

/* ── Chat Rendering ───────────────────────────────────────────────────────── */

const _chatContainer = () => document.getElementById('global-chat-messages');

let _globalChatAtBottom = true;
const _GLOBAL_SCROLL_THRESHOLD = 60;

(function _initGlobalScrollTracking() {
  const el = _chatContainer();
  if (el) el.addEventListener('scroll', () => {
    _globalChatAtBottom = el.scrollHeight - el.scrollTop - el.clientHeight < _GLOBAL_SCROLL_THRESHOLD;
  }, { passive: true });
})();

function _scrollChatToBottom(force = false) {
  if (!force && !_globalChatAtBottom) return;
  const el = _chatContainer();
  if (el) el.scrollTop = el.scrollHeight;
}

function _hideWelcome() {
  const w = document.getElementById('home-chat-welcome');
  if (w) w.style.display = 'none';
}

function _showWelcome() {
  const w = document.getElementById('home-chat-welcome');
  if (w) w.style.display = '';
}

function _appendUserBubble(text) {
  _hideWelcome();
  const container = _chatContainer();
  const wrap = document.createElement('div');
  wrap.className = 'chat-msg user';
  wrap.innerHTML = `
    <div class="chat-msg-header">
      <span class="chat-avatar user-avatar">U</span>
      <span class="chat-msg-role">You</span>
    </div>
    <div class="chat-msg-body">${escapeHtml(text)}</div>`;
  container.appendChild(wrap);
  // User sent a message — reset flag and force-scroll
  _globalChatAtBottom = true;
  _scrollChatToBottom();
}

function _createAssistantBubble() {
  _hideWelcome();
  const container = _chatContainer();
  const wrap = document.createElement('div');
  wrap.className = 'chat-msg assistant';
  wrap.innerHTML = `
    <div class="chat-msg-header">
      <span class="chat-avatar assistant-avatar"><i class="fa-solid fa-robot"></i></span>
      <span class="chat-msg-role">Assistant</span>
      <div class="chat-msg-actions">
        <button class="chat-msg-action-btn" title="Copy" onclick="_copyChatMsg(this)">
          <i class="fa-regular fa-copy"></i>
        </button>
      </div>
    </div>
    <div class="chat-msg-body markdown-body" style="display:none"></div>
    <div class="chat-processing">
      <span class="chat-processing-label">Thinking</span>
      <span class="chat-processing-dots"><span></span><span></span><span></span></span>
    </div>`;
  container.appendChild(wrap);
  _scrollChatToBottom();
  return wrap;
}

function _setAssistantProcessing(msgWrap, active, label) {
  const proc = msgWrap.querySelector('.chat-processing');
  if (!proc) return;
  if (active && label) proc.querySelector('.chat-processing-label').textContent = label;
  proc.classList.toggle('active', active);
}

function _updateAssistantBody(msgWrap, text) {
  const body = msgWrap.querySelector('.chat-msg-body');
  if (!body) return;
  body.style.display = '';
  body.innerHTML = renderMd(text);
  body.querySelectorAll('pre code').forEach(block => {
    try { hljs.highlightElement(block); } catch {}
  });
  _addCodeCopyButtons(body);
}

function _renderToolWidget(msgWrap, toolCalls, isFinal = false) {
  let widget = msgWrap.querySelector('.chat-tool-widget');
  if (!widget) {
    widget = document.createElement('div');
    widget.className = 'chat-tool-widget';
    const body = msgWrap.querySelector('.chat-msg-body');
    body.parentNode.insertBefore(widget, body);
  }
  const count = toolCalls.length;
  const doneCount = toolCalls.filter(tc => tc.result).length;
  // isFinal=true is used by the hydration path (loading saved messages from
  // the DB). The response has already completed, so any tool entry whose
  // result wasn't persisted (older sessions saved before the parallel-tool
  // pairing fix) must still render as "completed" \u2014 the spinner state would
  // be permanently stuck otherwise.
  const allDone = isFinal || doneCount === count;
  const isOpen = widget.classList.contains('open');

  let itemsHtml = '';
  for (const tc of toolCalls) {
    const hasResult = !!tc.result;
    let icon, iconCls, detail;
    if (hasResult) {
      icon = tc.result.success ? '\u2713' : '\u2717';
      iconCls = tc.result.success ? 'success' : 'error';
      detail = tc.result.summary;
    } else if (isFinal) {
      icon = '\u2713';
      iconCls = 'success';
      detail = '(no details saved)';
    } else {
      icon = '\u23F3';
      iconCls = 'pending';
      detail = _toolInputSummary(tc.name, tc.input);
    }
    const label = _toolDisplayName(tc.name);
    itemsHtml += `<div class="chat-tool-item">
      <div class="chat-tool-left">
        <div class="row1">
          <span class="chat-tool-icon ${iconCls}">${icon}</span>
          <span class="chat-tool-label">${escapeHtml(label)}</span>
        </div>
        <span class="chat-tool-detail">${escapeHtml(detail)}</span>
      </div>
    </div>`;
  }

  const statusIcon = allDone ? '<i class="fa-solid fa-wrench"></i>' : '<span class="chat-tool-spinner"></span>';
  const statusText = allDone
    ? `${count} tool use${count > 1 ? 's' : ''}`
    : `Using tools (${doneCount}/${count})`;

  widget.innerHTML = `
    <button class="chat-tool-toggle" onclick="this.closest('.chat-tool-widget').classList.toggle('open')">
      ${statusIcon}
      <span>${statusText}</span>
      <i class="fa-solid fa-chevron-right chat-tool-chevron"></i>
    </button>
    <div class="chat-tool-details">${itemsHtml}</div>`;

  // Auto-expand while tools are in progress, preserve manual toggle otherwise.
  // Keep 'streaming' even after all tools complete - it's only removed on
  // first chat_chunk so the collapse fires at the right time.
  // Hydrated (isFinal) widgets skip the streaming class entirely \u2014 they're
  // rendered after the response completed and should stay collapsed unless
  // the user expands them.
  if (isFinal) {
    if (isOpen) widget.classList.add('open');
  } else if (!allDone) {
    widget.classList.add('open', 'streaming');
  } else if (widget.classList.contains('streaming')) {
    widget.classList.add('open');
  } else if (isOpen) {
    widget.classList.add('open');
  }
}

function _copyChatMsg(btn) {
  const body = btn.closest('.chat-msg').querySelector('.chat-msg-body');
  if (!body) return;
  const html = body.innerHTML;
  const plain = body.innerText || '';
  navigator.clipboard.write([
    new ClipboardItem({
      'text/html': new Blob([html], { type: 'text/html' }),
      'text/plain': new Blob([plain], { type: 'text/plain' }),
    }),
  ]).catch(() => navigator.clipboard.writeText(plain)).then(() => {
    btn.classList.add('copied');
    btn.innerHTML = '<i class="fa-solid fa-check"></i>';
    setTimeout(() => {
      btn.classList.remove('copied');
      btn.innerHTML = '<i class="fa-regular fa-copy"></i>';
    }, 1500);
  });
}

/* ── Chat Input ───────────────────────────────────────────────────────────── */

function handleGlobalChatKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendGlobalMessage();
  }
}

function autogrowGlobalInput() {
  const ta = document.getElementById('global-chat-input');
  ta.style.height = 'auto';
  ta.style.height = Math.min(ta.scrollHeight, 200) + 'px';
}

function _setChatBusy(busy) {
  _homeState.busy = busy;
  document.getElementById('global-send-btn').classList.toggle('hidden', busy);
  document.getElementById('global-stop-btn').classList.toggle('hidden', !busy);
  document.getElementById('global-chat-input').disabled = busy;
}

/* ── Send / Stop ──────────────────────────────────────────────────────────── */

async function sendGlobalMessage() {
  const input = document.getElementById('global-chat-input');
  const question = input.value.trim();
  if (!question || _homeState.busy) return;

  input.value = '';
  input.style.height = 'auto';
  _appendUserBubble(question);

  const msgWrap = _createAssistantBubble();
  _setAssistantProcessing(msgWrap, true, 'Thinking');
  _scrollChatToBottom();
  _homeState.currentMsgWrap = msgWrap;
  _homeState.currentChunks = [];
  _homeState.currentToolCalls = [];
  _setChatBusy(true);

  try {
    const res = await fetch('/api/global-chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        conversation_id: _homeState.conversationId,
        question,
      }),
    });
    const data = await res.json();
    _homeState.requestId = data.request_id;
    if (!_homeState.conversationId && data.conversation_id) {
      _homeState.conversationId = data.conversation_id;
    }
  } catch (e) {
    _setAssistantProcessing(msgWrap, false);
    _updateAssistantBody(msgWrap, `*Error: ${e.message}*`);
    _setChatBusy(false);
  }
}

async function stopGlobalChat() {
  if (_homeState.requestId) {
    await fetch('/api/global-chat/stop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ request_id: _homeState.requestId }),
    });
  }
}

/* ── SSE Event Handlers ───────────────────────────────────────────────────── */

function _onGlobalChatChunk(data) {
  if (data.request_id !== _homeState.requestId) return;
  _homeState.currentChunks.push(data.text);
  const full = _homeState.currentChunks.join('');
  if (_homeState.currentMsgWrap) {
    _setAssistantProcessing(_homeState.currentMsgWrap, false);
    // Collapse tool widget when response starts streaming
    const tw = _homeState.currentMsgWrap.querySelector('.chat-tool-widget.streaming');
    if (tw) tw.classList.remove('open', 'streaming');
    _updateAssistantBody(_homeState.currentMsgWrap, full);
    const body = _homeState.currentMsgWrap.querySelector('.chat-msg-body');
    if (body) {
      _ensureTypingCursor(body);
      _chunkArrived();
    }
    _scrollChatToBottom();
  }
}

function _onGlobalToolEvent(data) {
  if (data.request_id !== _homeState.requestId) return;
  if (data.type === 'tool_call') {
    _homeState.currentToolCalls.push({
      id: data.id,
      name: data.name,
      input: data.input,
      result: null,
    });
    if (_homeState.currentMsgWrap) {
      _setAssistantProcessing(_homeState.currentMsgWrap, true, 'Using tools');
    }
  } else if (data.type === 'tool_result') {
    // Match the result to its call by id — required when tools execute in
    // parallel and results return out of order. Fall back to the first
    // still-pending call if no id is present (backward compat).
    let target = null;
    if (data.id != null) {
      target = _homeState.currentToolCalls.find(tc => tc.id === data.id && !tc.result);
    }
    if (!target) {
      target = _homeState.currentToolCalls.find(tc => !tc.result);
    }
    if (target) {
      target.result = { success: data.success, summary: data.summary };
    }
  }
  if (_homeState.currentMsgWrap) {
    _renderToolWidget(_homeState.currentMsgWrap, _homeState.currentToolCalls);
    _scrollChatToBottom();
  }
}

function _onGlobalChatDone(data) {
  if (data.request_id !== _homeState.requestId) return;
  // Remove typing cursor from finished message
  if (_homeState.currentMsgWrap) {
    _removeTypingCursor();
  }
  _setChatBusy(false);
  _homeState.requestId = null;
  _homeState.currentMsgWrap = null;
  loadConversations();
}

function _onGlobalChatTitle(data) {
  if (data.conversation_id === _homeState.conversationId) {
    const el = document.getElementById('home-chat-title');
    if (el) el.textContent = data.title;
  }
  loadConversations();
}

/* ── SSE Setup ────────────────────────────────────────────────────────────── */

function _initSSE() {
  // Reuse app.js's SSE connection - never open a second one.
  const src = _sseSource || _sse;
  if (!src) return;  // should not happen; app.js always runs first
  _sse = src;

  src.addEventListener('global_chat_chunk', e => {
    try { _onGlobalChatChunk(JSON.parse(e.data)); } catch {}
  });
  src.addEventListener('global_chat_tool_event', e => {
    try { _onGlobalToolEvent(JSON.parse(e.data)); } catch {}
  });
  src.addEventListener('global_chat_done', e => {
    try { _onGlobalChatDone(JSON.parse(e.data)); } catch {}
  });
  src.addEventListener('global_chat_title', e => {
    try { _onGlobalChatTitle(JSON.parse(e.data)); } catch {}
  });
  src.addEventListener('global_chat_start', () => {});
}

/* ── Conversation Management ──────────────────────────────────────────────── */

async function loadConversations() {
  try {
    const res = await fetch('/api/global-chat/conversations');
    _homeState.conversations = await res.json();
    _renderConversationList();
  } catch {}
}

function _renderConversationList() {
  const list = document.getElementById('home-conv-list');
  if (!_homeState.conversations.length) {
    list.innerHTML = '<p class="home-conv-empty">No conversations yet</p>';
    return;
  }

  let html = '';
  for (const conv of _homeState.conversations) {
    const active = conv.id === _homeState.conversationId ? ' active' : '';
    const msgCount = conv.message_count || 0;
    html += `
      <div class="home-conv-item${active}" data-id="${conv.id}"
           onclick="switchConversation('${conv.id}')"
           oncontextmenu="_convContextMenu(event, '${conv.id}')">
        <div class="home-conv-item-title">${escapeHtml(conv.title)}</div>
        <div class="home-conv-item-meta">
          <span>${msgCount} msg${msgCount !== 1 ? 's' : ''}</span>
          <span>${_timeAgo(conv.updated_at)}</span>
        </div>
      </div>`;
  }
  list.innerHTML = html;
}

async function switchConversation(convId) {
  if (_homeState.busy) return;
  _homeState.conversationId = convId;
  _renderConversationList();

  const container = _chatContainer();
  container.querySelectorAll('.chat-msg').forEach(el => el.remove());

  try {
    const res = await fetch(`/api/global-chat/conversations/${convId}`);
    const conv = await res.json();
    document.getElementById('home-chat-title').textContent = conv.title || 'Global Chat';

    if (!conv.messages || conv.messages.length === 0) {
      _showWelcome();
      return;
    }
    _hideWelcome();

    for (const msg of conv.messages) {
      if (msg.role === 'user') {
        _appendUserBubble(msg.content);
      } else {
        const wrap = _createAssistantBubble();
        _updateAssistantBody(wrap, msg.content);
        if (msg.tool_calls) {
          try {
            const tcs = typeof msg.tool_calls === 'string' ? JSON.parse(msg.tool_calls) : msg.tool_calls;
            if (tcs.length) _renderToolWidget(wrap, tcs, true);
          } catch {}
        }
      }
    }
    _globalChatAtBottom = true;
    _scrollChatToBottom();
  } catch {}
}

async function newGlobalConversation() {
  if (_homeState.busy) return;
  _homeState.conversationId = null;
  document.getElementById('home-chat-title').textContent = 'Global Chat';
  const container = _chatContainer();
  container.querySelectorAll('.chat-msg').forEach(el => el.remove());
  _showWelcome();
  _renderConversationList();
  document.getElementById('global-chat-input').focus();
}

async function clearGlobalChat() {
  // Cancel any in-flight response
  if (_homeState.busy) {
    await stopGlobalChat();
    _homeState.busy = false;
    _homeState.currentMsgWrap = null;
    _homeState.currentChunks = [];
    _homeState.currentToolCalls = [];
    _setChatBusy(false);
  }
  if (!_homeState.conversationId) {
    const container = _chatContainer();
    container.querySelectorAll('.chat-msg').forEach(el => el.remove());
    _showWelcome();
    return;
  }
  try {
    await fetch('/api/global-chat/clear', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conversation_id: _homeState.conversationId }),
    });
  } catch {}
  const container = _chatContainer();
  container.querySelectorAll('.chat-msg').forEach(el => el.remove());
  _showWelcome();
}

function _convContextMenu(e, convId) {
  e.preventDefault();
  document.querySelectorAll('.home-conv-ctx').forEach(el => el.remove());

  const menu = document.createElement('div');
  menu.className = 'home-conv-ctx';
  menu.style.left = e.clientX + 'px';
  menu.style.top = e.clientY + 'px';
  menu.innerHTML = `
    <button onclick="_renameConversation('${convId}')"><i class="fa-solid fa-pen"></i> Rename</button>
    <button class="danger" onclick="_deleteConversation('${convId}')"><i class="fa-solid fa-trash"></i> Delete</button>`;
  document.body.appendChild(menu);

  const dismiss = () => { menu.remove(); document.removeEventListener('click', dismiss); };
  setTimeout(() => document.addEventListener('click', dismiss), 10);
}

async function _renameConversation(convId) {
  document.querySelectorAll('.home-conv-ctx').forEach(el => el.remove());
  const conv = _homeState.conversations.find(c => c.id === convId);
  const title = prompt('Rename conversation:', conv?.title || '');
  if (!title || !title.trim()) return;
  try {
    await fetch(`/api/global-chat/conversations/${convId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: title.trim() }),
    });
    if (convId === _homeState.conversationId) {
      document.getElementById('home-chat-title').textContent = title.trim();
    }
    loadConversations();
  } catch {}
}

async function _deleteConversation(convId) {
  document.querySelectorAll('.home-conv-ctx').forEach(el => el.remove());
  if (!confirm('Delete this conversation?')) return;
  try {
    await fetch(`/api/global-chat/conversations/${convId}`, { method: 'DELETE' });
    if (convId === _homeState.conversationId) {
      newGlobalConversation();
    }
    loadConversations();
  } catch {}
}

function useSuggestion(btn) {
  const input = document.getElementById('global-chat-input');
  input.value = btn.textContent;
  sendGlobalMessage();
}

// Trim pasted text
document.getElementById('global-chat-input')?.addEventListener('paste', e => {
  const ta = e.target;
  setTimeout(() => { ta.value = ta.value.trim(); }, 0);
});

/* ── Conversation Sidebar Toggle ──────────────────────────────────────────── */

function toggleConvSidebar() {
  const sidebar = document.getElementById('home-conv-sidebar');
  sidebar.classList.toggle('collapsed');
  localStorage.setItem('home_conv_sidebar_collapsed', sidebar.classList.contains('collapsed') ? '1' : '');
}

function _restoreConvSidebar() {
  if (localStorage.getItem('home_conv_sidebar_collapsed') !== '0') {
    document.getElementById('home-conv-sidebar').classList.add('collapsed');
  }
}

/* ── Dashboard Widgets ────────────────────────────────────────────────────── */

async function loadAnalytics() {
  try {
    const res = await fetch('/api/analytics');
    const data = await res.json();

    // Hero stats
    document.getElementById('stat-sessions').textContent = data.total_sessions || 0;
    document.getElementById('stat-time').textContent = _formatDuration(data.total_seconds);
    document.getElementById('stat-speakers').textContent = data.speaker_count || 0;
    document.getElementById('stat-words').textContent = _formatCompactNumber(data.total_words || 0);

    // Activity chart
    _renderActivityChart(data.activity || [], data.sessions_this_week || 0);

    // Top speakers
    _renderTopSpeakers(data.top_speakers || []);

    // Recent sessions (from enhanced API)
    if (data.recent_sessions) {
      _renderRecentSessions(data.recent_sessions);
    }
  } catch {}
}

function _renderActivityChart(activity, thisWeek) {
  const chart = document.getElementById('home-activity-chart');
  const summary = document.getElementById('home-activity-summary');

  if (summary) {
    summary.textContent = `${thisWeek} session${thisWeek !== 1 ? 's' : ''} this week`;
  }

  if (!activity.length) {
    chart.innerHTML = '<span style="color:var(--fg-subtle);font-size:11px;">No activity data yet</span>';
    return;
  }

  const maxCount = Math.max(...activity.map(a => a.count), 1);
  const today = new Date().toISOString().slice(0, 10);
  const dayNames = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

  let html = '';
  for (const a of activity) {
    const pct = (a.count / maxCount) * 100;
    const d = new Date(a.day + 'T12:00:00');
    const dayLabel = dayNames[d.getDay()];
    const isToday = a.day === today;
    const tooltip = `${a.day}: ${a.count} session${a.count !== 1 ? 's' : ''}`;
    html += `
      <div class="home-activity-bar-wrap" data-tooltip="${tooltip}" ${isToday ? 'data-today="true"' : ''}>
        <div class="home-activity-bar">
          <div class="home-activity-bar-fill ${a.count > 0 ? 'has-data' : ''}"
               style="height:${a.count > 0 ? Math.max(pct, 8) : 0}%"></div>
        </div>
        <span class="home-activity-day">${dayLabel}</span>
      </div>`;
  }
  chart.innerHTML = html;
}

function _renderTopSpeakers(speakers) {
  const list = document.getElementById('home-speakers-list');
  if (!speakers.length) {
    list.innerHTML = '<p class="home-speakers-empty">No speakers identified yet.</p>';
    return;
  }

  const maxSessions = Math.max(...speakers.map(s => s.session_count), 1);

  let html = '';
  for (const sp of speakers) {
    const color = sp.color || '#8b949e';
    const initials = sp.name.split(/\s+/).map(w => w[0]).join('').slice(0, 2);
    const barPct = (sp.session_count / maxSessions) * 100;
    const talkTime = sp.talk_seconds ? _formatDuration(sp.talk_seconds) : '';
    const statsText = `${sp.session_count} session${sp.session_count !== 1 ? 's' : ''}${talkTime ? ' \u00b7 ' + talkTime : ''}`;

    html += `
      <div class="home-speaker-item">
        <div class="home-speaker-avatar" style="background:${escapeHtml(color)}">${escapeHtml(initials)}</div>
        <div class="home-speaker-info">
          <div class="home-speaker-name">${escapeHtml(sp.name)}</div>
          <div class="home-speaker-stats">${escapeHtml(statsText)}</div>
        </div>
        <div class="home-speaker-bar-wrap">
          <div class="home-speaker-bar" style="width:${barPct}%;background:${escapeHtml(color)}"></div>
        </div>
      </div>`;
  }
  list.innerHTML = html;
}

function _renderRecentSessions(sessions) {
  const list = document.getElementById('home-recent-list');
  if (!sessions.length) {
    list.innerHTML = '<p class="home-recent-empty">No sessions yet. Start your first recording!</p>';
    return;
  }

  let html = '';
  for (const s of sessions) {
    const duration = s.duration_seconds ? _formatDuration(s.duration_seconds) : '';
    const date = _timeAgo(s.started_at);
    const speakerCount = s.speaker_count || 0;

    html += `
      <a href="/session?id=${s.id}" class="home-recent-item">
        <div class="home-recent-indicator"></div>
        <div class="home-recent-body">
          <div class="home-recent-title">${escapeHtml(s.title)}</div>
          <div class="home-recent-meta">
            <span><i class="fa-regular fa-clock"></i>${date}</span>
            ${duration ? `<span><i class="fa-solid fa-stopwatch"></i>${duration}</span>` : ''}
            ${speakerCount > 0 ? `<span><i class="fa-solid fa-user"></i>${speakerCount}</span>` : ''}
          </div>
        </div>
      </a>`;
  }
  list.innerHTML = html;
}

async function loadRecentSessions() {
  // Recent sessions are now loaded from the analytics endpoint
  // This function is kept as a no-op for backwards compatibility
}

/* ── Search ────────────────────────────────────────────────────────────────── */

let _homeSearchDebounce = null;
let _homeSearchQuery = '';
let _homeSearchResults = new Map(); // session_id -> { title, matches[] }
let _homeSearchFtsPending = false;
let _homeSearchSemanticPending = false;
let _homeSemanticReady = false;

function _initSearch() {
  const input = document.getElementById('home-search-input');
  input.addEventListener('input', () => {
    const q = input.value.trim();
    document.getElementById('home-search-clear').classList.toggle('hidden', !q);
    _onHomeSearch(q);
  });

  // Refocus results on input focus if there's a query
  input.addEventListener('focus', () => {
    if (_homeSearchQuery && _homeSearchResults.size > 0) {
      _renderHomeSearchResults();
    }
  });

  document.addEventListener('click', e => {
    const results = document.getElementById('home-search-results');
    const searchWrap = document.querySelector('.home-search-wrap');
    if (!results) return;
    if (searchWrap?.contains(e.target) || results.contains(e.target)) return;
    results.classList.add('hidden');
  });

  // Check if semantic search is available
  _checkHomeSemanticReady();
}

async function _checkHomeSemanticReady() {
  const badge = document.getElementById('home-search-ai');
  try {
    const res = await fetch('/api/search/semantic/status');
    const data = await res.json();
    _homeSemanticReady = !!data.ready;
    if (badge) badge.classList.toggle('ready', _homeSemanticReady);
  } catch {}
  // Re-check periodically until ready
  if (!_homeSemanticReady) setTimeout(_checkHomeSemanticReady, 10000);
}

function _onHomeSearch(value) {
  _homeSearchQuery = value;
  clearTimeout(_homeSearchDebounce);

  if (!_homeSearchQuery) {
    _homeSearchResults = new Map();
    _homeSearchFtsPending = false;
    _homeSearchSemanticPending = false;
    document.getElementById('home-search-results').classList.add('hidden');
    return;
  }

  // Pulse the glow
  _pulseHomeSearchGlow();

  // Instant client-side title filter (reuse sidebar's session data if available)
  const sessions = (typeof _sidebarAllSessions !== 'undefined') ? _sidebarAllSessions : [];
  const q = _homeSearchQuery.toLowerCase();
  const titleMatches = new Map();
  for (const s of sessions) {
    if (s.title && s.title.toLowerCase().includes(q)) {
      titleMatches.set(s.id, {
        title: s.title,
        matches: [{ kind: 'title', snippet: _homeHighlight(s.title, q) }],
      });
    }
  }

  _homeSearchResults = titleMatches;
  _homeSearchFtsPending = true;
  _homeSearchSemanticPending = _homeSemanticReady;
  _renderHomeSearchResults();

  // Debounced backend searches
  _homeSearchDebounce = setTimeout(() => {
    _runHomeFtsSearch(_homeSearchQuery);
    if (_homeSemanticReady) _runHomeSemanticSearch(_homeSearchQuery);
  }, 250);
}

async function _runHomeFtsSearch(query) {
  if (query !== _homeSearchQuery) return;
  try {
    const data = await fetch(`/api/search?q=${encodeURIComponent(query)}`).then(r => r.json());
    if (query !== _homeSearchQuery) return;
    const merged = new Map(_homeSearchResults);
    for (const r of data) {
      if (merged.has(r.session_id)) {
        const existing = merged.get(r.session_id);
        const contentMatches = r.matches.filter(m => m.kind !== 'title');
        existing.matches = [...existing.matches, ...contentMatches].slice(0, 3);
      } else {
        merged.set(r.session_id, { title: r.title, matches: r.matches });
      }
    }
    _homeSearchFtsPending = false;
    _homeSearchResults = merged;
    _renderHomeSearchResults();
  } catch {
    _homeSearchFtsPending = false;
  }
}

async function _runHomeSemanticSearch(query) {
  if (query !== _homeSearchQuery) return;
  try {
    const resp = await fetch(`/api/search/semantic?q=${encodeURIComponent(query)}`);
    if (query !== _homeSearchQuery) return;
    if (!resp.ok) { _homeSearchSemanticPending = false; _renderHomeSearchResults(); return; }
    const data = await resp.json();
    if (query !== _homeSearchQuery) return;
    const merged = new Map(_homeSearchResults);
    for (const r of data) {
      if (merged.has(r.session_id)) {
        const existing = merged.get(r.session_id);
        const semMatches = (r.matches || []).filter(m => m.kind === 'semantic');
        existing.matches = [...existing.matches, ...semMatches].slice(0, 3);
      } else {
        merged.set(r.session_id, { title: r.title, matches: r.matches || [] });
      }
    }
    _homeSearchSemanticPending = false;
    _homeSearchResults = merged;
    _renderHomeSearchResults();
  } catch {
    _homeSearchSemanticPending = false;
  }
}

function _renderHomeSearchResults() {
  const container = document.getElementById('home-search-results');
  const isPending = _homeSearchFtsPending || _homeSearchSemanticPending;

  if (_homeSearchResults.size === 0 && !isPending) {
    container.innerHTML = '<div class="home-search-empty">No results found</div>';
    container.classList.remove('hidden');
    return;
  }

  let html = '<div class="home-search-glow"></div>';

  if (isPending && _homeSearchResults.size === 0) {
    html += `<div class="home-search-loading">
      <div class="home-search-spinner"></div>
      <span>Searching${_homeSearchSemanticPending ? ' with AI' : ''}...</span>
    </div>`;
  }

  let count = 0;
  for (const [sid, entry] of _homeSearchResults) {
    if (count >= 8) break;
    const title = entry.title || sid;
    const matchHtml = (entry.matches || []).slice(0, 2).map(m => {
      const kindCls = m.kind || 'content';
      const kindLabel = kindCls === 'participant' ? '<i class="fa-solid fa-user"></i> participant'
        : kindCls === 'semantic' ? 'AI' : kindCls === 'title' ? 'title' : 'content';
      const snippet = m.snippet || '';
      return `<div class="home-search-result-snippet">
        <span class="home-search-result-kind ${kindCls}">${kindLabel}</span>${snippet}
      </div>`;
    }).join('');

    html += `
      <a href="/session?id=${sid}" class="home-search-result-item">
        <div class="home-search-result-title">${escapeHtml(title)}</div>
        ${matchHtml}
      </a>`;
    count++;
  }

  if (isPending && _homeSearchResults.size > 0) {
    html += `<div class="home-search-loading">
      <div class="home-search-spinner"></div>
      <span>${_homeSearchSemanticPending ? 'AI search' : 'Searching'}...</span>
    </div>`;
  }

  container.innerHTML = html;
  container.classList.remove('hidden');
}

function _pulseHomeSearchGlow() {
  const container = document.getElementById('home-search-results');
  const glow = container.querySelector('.home-search-glow');
  if (glow) { glow.remove(); }
}

function _homeHighlight(text, query) {
  const idx = text.toLowerCase().indexOf(query.toLowerCase());
  if (idx < 0) return escapeHtml(text);
  const before = text.slice(0, idx);
  const match = text.slice(idx, idx + query.length);
  const after = text.slice(idx + query.length);
  return escapeHtml(before) + '<mark>' + escapeHtml(match) + '</mark>' + escapeHtml(after);
}

function clearHomeSearch() {
  _homeSearchQuery = '';
  _homeSearchResults = new Map();
  const input = document.getElementById('home-search-input');
  input.value = '';
  document.getElementById('home-search-clear').classList.add('hidden');
  document.getElementById('home-search-results').classList.add('hidden');
}

/* ── Initialization ───────────────────────────────────────────────────────── */

document.addEventListener('DOMContentLoaded', () => {
  _restoreConvSidebar();
  _initSSE();
  _initSearch();
  loadConversations();
  loadAnalytics();
  document.getElementById('global-chat-input').focus();
});
