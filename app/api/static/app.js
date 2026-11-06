'use strict';
// Aletheia web page (D-047). Talks only to this server's /api; every model or tool text is
// written with textContent, never as HTML.

const TOKEN_KEY = 'aletheia-token';
const RUN_KEY = 'aletheia-runs';          // session id -> run id still being followed
const STATUS_TEXT = {completed: '已完成', blocked: '等待你的回答', failed: '未完成',
                     cancelled: '已取消', received: '进行中'};
const OUTCOME_TEXT = {succeeded: '完成', failed: '失败', blocked: '需要你回答', timeout: '超时'};

const $ = (id) => document.getElementById(id);
const state = {token: '', session: null, waiting: null, run: null, health: null};

function storage() {
  try { return window.sessionStorage; } catch (e) { return null; }
}
function saved(key, fallback) {
  try { const v = storage()?.getItem(key); return v === null || v === undefined ? fallback : JSON.parse(v); }
  catch (e) { return fallback; }
}
function save(key, value) {
  try { storage()?.setItem(key, JSON.stringify(value)); } catch (e) { /* storage unavailable */ }
}

function newId(prefix) {
  if (window.crypto && crypto.randomUUID) return prefix + crypto.randomUUID();
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return prefix + Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

class ApiError extends Error {
  constructor(status, code, message) { super(message); this.status = status; this.code = code; }
}

async function api(path, options = {}) {
  const headers = {...(options.headers || {})};
  if (state.token) headers.Authorization = 'Bearer ' + state.token;
  if (options.body !== undefined) headers['Content-Type'] = 'application/json';
  let response;
  try {
    response = await fetch(path, {...options, headers,
                                  body: options.body === undefined ? undefined : JSON.stringify(options.body)});
  } catch (e) {
    throw new ApiError(0, 'network', '连不上服务，请确认后端还在运行。');
  }
  if (response.status === 401) {
    showLogin(state.token ? '令牌不对或已失效，请重新输入。' : '');
    throw new ApiError(401, 'unauthorized', '需要访问令牌。');
  }
  if (options.stream) return response;
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(response.status, data.code || 'http_error', data.message || '请求失败。');
  return data;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function timeText(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return isNaN(d) ? '' : d.toLocaleString('zh-CN', {month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit'});
}

function narrow() {
  return window.matchMedia('(max-width: 800px)').matches;
}

function notice(text) {
  $('notice').textContent = text || '';
  $('notice').hidden = !text;
}

// ---- sign-in -------------------------------------------------------------------------

function showLogin(message) {
  state.token = '';
  try { storage()?.removeItem(TOKEN_KEY); } catch (e) { /* ignore */ }
  $('app').hidden = true;
  $('login').hidden = false;
  $('login-error').textContent = message || '';
  $('login-error').hidden = !message;
  $('login-token').focus();
}

$('login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  state.token = $('login-token').value.trim();
  $('login-token').value = '';
  try {
    await start();
    save(TOKEN_KEY, state.token);
  } catch (e) {
    if (e.status !== 401) showLogin(e.message);
  }
});

// ---- sessions ------------------------------------------------------------------------

async function loadSessions() {
  const {sessions} = await api('/api/sessions');
  const list = $('session-list');
  list.replaceChildren();
  for (const s of sessions) {
    const item = el('li');
    const button = el('button', s.session_id === state.session ? 'active' : '');
    button.type = 'button';
    button.append(el('span', 'title', s.last_message || s.session_id), el('span', 'time', timeText(s.updated_at)));
    button.addEventListener('click', () => openSession(s.session_id));
    item.append(button);
    list.append(item);
  }
  $('sessions-empty').hidden = sessions.length > 0;
  return sessions;
}

async function openSession(sessionId) {
  state.session = sessionId;
  state.run = null;
  $('sessions').classList.remove('open');
  notice('');
  await Promise.all([loadTurns(), loadSessions()]);
  const runId = saved(RUN_KEY, {})[sessionId];
  if (runId) resumeFollowing(runId);
  setBusy(false);
  $('input').focus();
}

function newSession() {
  state.session = newId('web-');
  state.waiting = null;
  state.run = null;
  notice('');
  renderTurns([]);
  loadSessions();
  setBusy(false);
  $('input').focus();
}

// ---- conversation --------------------------------------------------------------------

async function loadTurns() {
  if (!state.session) { renderTurns([]); return; }
  const {turns} = await api(`/api/sessions/${encodeURIComponent(state.session)}/turns?limit=50`);
  renderTurns(turns);
}

function renderTurns(turns) {
  const box = $('conversation');
  box.replaceChildren();
  state.waiting = null;
  if (!turns.length) {
    box.append(el('p', 'empty-chat', '输入一个目标开始。任务需要更多信息时会停下来问你，直接回答即可继续。'));
  }
  for (const turn of turns) {
    box.append(el('div', 'message user', turn.user));
    if (turn.status === 'received') continue;       // the live run draws its own bubble
    box.append(agentBubble(turn));
  }
  const last = turns[turns.length - 1];
  if (last && last.status === 'blocked') {
    if (last.resumable) state.waiting = last.task_id;
    else notice('上面的问题已经不能接着回答了（服务重启过），请重新提问。');
  }
  updateComposer();
  box.scrollTop = box.scrollHeight;
}

function agentBubble(turn) {
  const bubble = el('div', 'message agent' + (turn.status === 'blocked' ? ' blocked' : ''));
  const tag = el('span', 'tag' + (turn.status === 'failed' ? ' fail' : ''), STATUS_TEXT[turn.status] || '');
  bubble.append(tag);
  if (turn.status === 'failed' || turn.status === 'cancelled') {
    // The archived reply of a failed or cancelled turn is the host's English diagnostic; show the Chinese summary.
    bubble.append(el('span', '', turn.status === 'cancelled' ? '任务已取消，工具可能已经产生了副作用。' : '任务没有完成。'));
  } else {
    bubble.append(el('span', '', turn.reply || ''));
  }
  return bubble;
}

function updateComposer() {
  const waiting = Boolean(state.waiting);
  $('composer').classList.toggle('waiting', waiting);
  $('input').placeholder = waiting ? '回答上面的问题，回车发送' : '输入目标，回车发送，Shift+回车换行';
}

function setBusy(busy) {
  $('send').disabled = busy;
  $('input').disabled = busy;
  $('cancel').hidden = !busy;
  $('cancel').disabled = false;
}

// ---- runs ----------------------------------------------------------------------------

function liveBubble() {
  const bubble = el('div', 'message agent');
  const tag = el('span', 'tag');
  tag.append(el('span', 'spinner'), document.createTextNode('处理中'));
  const plan = el('ol', 'plan');
  const progress = el('ul', 'progress');
  bubble.append(tag, plan, progress);
  $('conversation').append(bubble);
  return {bubble, tag, plan, progress};
}

function rememberRun(runId) {
  const runs = saved(RUN_KEY, {});
  if (runId) runs[state.session] = runId; else delete runs[state.session];
  save(RUN_KEY, runs);
}

async function send(text) {
  const body = {content: text, request_id: newId('')};
  if (state.waiting) body.resume_task_id = state.waiting;
  const sessionId = state.session || (state.session = newId('web-'));
  setBusy(true);
  notice('');
  const box = $('conversation');
  box.querySelector('.empty-chat')?.remove();
  box.append(el('div', 'message user', text));
  try {
    const {run_id} = await api(`/api/sessions/${encodeURIComponent(sessionId)}/messages`, {method: 'POST', body});
    $('input').value = '';
    state.waiting = null;
    updateComposer();
    rememberRun(run_id);
    loadSessions();
    await follow(run_id, liveBubble());
  } catch (e) {
    setBusy(false);
    box.lastChild?.remove();
    if (e.code === 'task_not_resumable') { state.waiting = null; updateComposer(); }
    notice(e.message);
  }
}

async function resumeFollowing(runId) {
  try {
    const snapshot = await api(`/api/runs/${encodeURIComponent(runId)}`);
    if (snapshot.status !== 'running') { rememberRun(null); return; }
    setBusy(true);
    await follow(runId, liveBubble());
  } catch (e) {
    rememberRun(null);
  }
}

async function follow(runId, view) {
  state.run = runId;
  let lastId = 0, failures = 0;
  while (state.run === runId) {
    try {
      const headers = lastId ? {'Last-Event-ID': String(lastId)} : {};
      const response = await api(`/api/runs/${encodeURIComponent(runId)}/events`, {headers, stream: true});
      if (!response.ok) throw new ApiError(response.status, 'http_error', '读取进度失败。');
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      for (;;) {
        const {value, done} = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, {stream: true});
        let cut;
        while ((cut = buffer.indexOf('\n\n')) >= 0) {
          const block = buffer.slice(0, cut);
          buffer = buffer.slice(cut + 2);
          const data = block.split('\n').filter((l) => l.startsWith('data: ')).map((l) => l.slice(6)).join('\n');
          if (!data) continue;
          const event = JSON.parse(data);
          lastId = event.id;
          if (show(event, view)) { finish(runId); return; }
        }
      }
      failures = 0;
    } catch (e) {
      if (e.status === 401 || e.status === 404) { finish(runId); notice(e.status === 404 ? '这次运行已经过期。' : ''); return; }
      failures += 1;
      if (failures > 5) { finish(runId); notice('和服务的连接断了，刷新页面可以看到最新结果。'); return; }
      await new Promise((r) => setTimeout(r, 1000 * failures));
    }
  }
}

function show(event, view) {
  const box = $('conversation');
  switch (event.type) {
    case 'route':
      view.tag.lastChild.textContent = (event.mode === 'team' ? '团队模式' : '普通模式') + (event.read_only ? '（只读）' : '') + '，处理中';
      break;
    case 'plan':
      view.plan.replaceChildren(...event.steps.map((s) => el('li', '', s)));
      break;
    case 'step': {
      const item = el('li');
      const tone = event.outcome === 'succeeded' ? 'ok' : event.outcome === 'blocked' ? 'ask' : 'bad';
      item.append(el('span', tone, `第 ${event.step} 步${OUTCOME_TEXT[event.outcome] || event.outcome}`),
                  document.createTextNode(`，${event.seconds} 秒`));
      if (event.tools.length) {
        item.append(document.createTextNode('；工具：' + event.tools.map((t) => t.tool + (t.success ? '' : '（失败）')).join('、')));
      }
      view.progress.append(item);
      break;
    }
    case 'completed':
    case 'blocked':
    case 'failed':
    case 'cancelled': {
      const turn = {status: event.type, reply: event.content || event.question || null};
      const bubble = agentBubble(turn);
      if (event.type === 'failed') bubble.querySelector('span:not(.tag)').textContent = event.message;
      const usage = event.usage;
      if (usage && usage.model_calls) {
        bubble.append(el('span', 'detail', `模型调用 ${usage.model_calls} 次，令牌 ${usage.total_tokens || 0}，耗时 ${usage.seconds || 0} 秒`));
      }
      if (view.progress.childElementCount) bubble.append(view.progress);
      view.bubble.replaceWith(bubble);
      if (event.type === 'blocked') { state.waiting = event.task_id; updateComposer(); }
      box.scrollTop = box.scrollHeight;
      return true;
    }
    default:
      break;
  }
  box.scrollTop = box.scrollHeight;
  return false;
}

function finish(runId) {
  if (state.run === runId) state.run = null;
  rememberRun(null);
  setBusy(false);
  loadSessions().catch(() => {});
  $('input').focus();
}

$('composer').addEventListener('submit', (event) => {
  event.preventDefault();
  const text = $('input').value.trim();
  if (text && !state.run) send(text);
});
$('input').addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    $('composer').requestSubmit();
  }
});
$('cancel').addEventListener('click', async () => {
  if (!state.run) return;
  $('cancel').disabled = true;
  try { await api(`/api/runs/${encodeURIComponent(state.run)}/cancel`, {method: 'POST'}); }
  catch (e) { notice(e.message); $('cancel').disabled = false; }
});
$('new-session').addEventListener('click', newSession);
$('toggle-sessions').addEventListener('click', () => {
  $('sessions').classList.toggle('open');
  if (narrow()) $('memory').hidden = true;
});
$('toggle-memory').addEventListener('click', () => {
  $('memory').hidden = !$('memory').hidden;
  if (narrow()) $('sessions').classList.remove('open');
  if (!$('memory').hidden) loadMemories().catch((e) => notice(e.message));
});

// ---- long-term memory ----------------------------------------------------------------

async function loadMemories() {
  const {memories} = await api('/api/memories?limit=100');
  const list = $('memory-list');
  list.replaceChildren();
  for (const m of memories) {
    const item = el('li');
    const row = el('div', 'row');
    const remove = el('button', 'danger', '删除');
    remove.type = 'button';
    remove.addEventListener('click', async () => {
      try { await api(`/api/memories/${encodeURIComponent(m.memory_id)}`, {method: 'DELETE'}); await loadMemories(); }
      catch (e) { notice(e.message); }
    });
    row.append(el('span', 'muted', timeText(m.created_at)), remove);
    item.append(el('div', '', m.content), row);
    list.append(item);
  }
  $('memory-empty').hidden = memories.length > 0;
}

$('memory-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const content = $('memory-input').value.trim();
  if (!content) return;
  try {
    await api('/api/memories', {method: 'POST', body: {content}});
    $('memory-input').value = '';
    await loadMemories();
  } catch (e) { notice(e.message); }
});

// ---- start ---------------------------------------------------------------------------

async function start() {
  const health = await api('/api/health');
  state.health = health;
  $('login').hidden = true;
  $('app').hidden = false;
  const write = $('badge-write');
  write.textContent = health.write_enabled ? '写文件：开启' : '写文件：关闭';
  write.classList.toggle('on', health.write_enabled);
  $('badge-memory').textContent = health.longterm_memory ? '长期记忆：可用' : '长期记忆：未开启';
  $('toggle-memory').hidden = !health.longterm_memory;
  const sessions = await loadSessions();
  if (sessions.length) await openSession(sessions[0].session_id);
  else newSession();
}

state.token = saved(TOKEN_KEY, '');
start().catch((e) => { if (e.status !== 401) showLogin(e.message); });
