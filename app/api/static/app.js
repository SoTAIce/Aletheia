'use strict';
// Aletheia web page (D-047; editorial restyle D-049). Talks only to this server's /api. Every
// model, tool or user text is put on the page as text nodes, never parsed as HTML.

const TOKEN_KEY = 'aletheia-token';
const RUN_KEY = 'aletheia-runs';          // session id -> run id still being followed
const OPTIONS_KEY = 'aletheia-options';   // this tab's choice of mode, runner, writes and memory
const SVG_NS = 'http://www.w3.org/2000/svg';   // a namespace name, not a resource that is loaded
const TAG = {completed: ['done', '已完成'], blocked: ['ask', '等待你回答'], failed: ['fail', '未完成'],
             cancelled: ['', '已停止'], running: ['run', '进行中']};
const OUTCOME = {succeeded: ['done', 'i-check', '完成'], failed: ['fail', 'i-x', '失败'],
                 timeout: ['fail', 'i-clock', '超时'], blocked: ['ask', 'i-question', '需要你回答']};
// What a side-effect tool call is about to do, for the approval card (D-061).
const APPROVAL_ACTIONS = {write_file: '写入文件', run_python: '运行 Python 代码', run_tests: '运行测试'};
const APPROVAL_RESULTS = {allow: '已允许', allow_all: '已允许（本次运行都允许）', deny: '已拒绝',
                          timeout: '没有及时回答，已拒绝'};

function approvalText(event) {
  const args = event.args_summary || {};
  const action = APPROVAL_ACTIONS[event.tool] || `调用 ${event.tool}`;
  const details = [];
  if (typeof args.path === 'string') details.push(args.path);
  const size = args.content?.chars ?? args.code?.chars;
  if (typeof size === 'number') details.push(`${size.toLocaleString('zh-CN')} 字符`);
  return details.length ? `${action}：${details.join('，')}` : action;
}

const EXAMPLES = [
  '读取 README.md，用三句话概括这个项目是做什么的',
  '解释 app/agent/runtime.py 里一个任务是怎样被规划和执行的',
  '写一个 Python 函数，判断字符串是否为回文，并给出三个例子',
  '帮我翻译一句话（它会先问你是哪一句）',
];

const $ = (id) => document.getElementById(id);
const state = {token: '', session: null, sessions: [], turns: [], waiting: null, run: null,
               caps: null, options: null};   // caps: health.options; options: what the next run asks for

// ---- small helpers --------------------------------------------------------------------

function storage() { try { return window.sessionStorage; } catch (e) { return null; } }
function saved(key, fallback) {
  try { const v = storage()?.getItem(key); return v === null || v === undefined ? fallback : JSON.parse(v); }
  catch (e) { return fallback; }
}
function save(key, value) { try { storage()?.setItem(key, JSON.stringify(value)); } catch (e) { /* unavailable */ } }

function newId(prefix) {
  if (window.crypto && crypto.randomUUID) return prefix + crypto.randomUUID();
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return prefix + Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}
function icon(name) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  const use = document.createElementNS(SVG_NS, 'use');
  use.setAttribute('href', '#' + name);
  svg.append(use);
  return svg;
}
function tag(status) {
  const [tone, text] = TAG[status] || ['', status];
  return el('span', 'tag ' + tone, text);
}

function timeText(iso, withDate = true) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return '';
  return d.toLocaleString('zh-CN', withDate ? {month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit'}
                                             : {hour: '2-digit', minute: '2-digit'});
}
function dayGroup(iso) {
  const d = new Date(iso), now = new Date();
  const start = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((start(now) - start(d)) / 86400000);
  return days <= 0 ? '今天' : days === 1 ? '昨天' : days < 7 ? '最近 7 天' : '更早';
}
function firstLine(text, max = 60) { return String(text || '').split('\n')[0].slice(0, max); }
function narrow() { return window.matchMedia('(max-width: 860px)').matches; }

let toastTimer = null;
function toast(text) {
  clearTimeout(toastTimer);
  $('toast').textContent = text || '';
  $('toast').hidden = !text;
  if (text) toastTimer = setTimeout(() => { $('toast').hidden = true; }, 7000);
}

// ---- API ------------------------------------------------------------------------------

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

// ---- reply formatting (text nodes only) -----------------------------------------------

const INLINE = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|\[([^\]\n]+)\]\((\S+?)\)|(?<![\w*])\*([^*\s][^*\n]*?)\*(?![\w*])/g;

function inline(text) {
  const frag = document.createDocumentFragment();
  let last = 0;
  for (const m of text.matchAll(INLINE)) {
    if (m.index > last) frag.append(text.slice(last, m.index));
    if (m[1]) frag.append(el('code', '', m[1].slice(1, -1)));
    else if (m[2]) frag.append(el('strong', '', m[2].slice(2, -2)));
    else if (m[5]) frag.append(el('em', '', m[5]));
    else if (/^https?:\/\/[^\s<>"']+$/i.test(m[4])) {
      const a = el('a', '', m[3]);
      a.href = m[4];
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      frag.append(a);
    } else frag.append(m[0]);
    last = m.index + m[0].length;
  }
  if (last < text.length) frag.append(text.slice(last));
  return frag;
}

function copyButton(getText, label = '复制') {
  const button = el('button', 'mini-btn');
  button.type = 'button';
  button.append(icon('i-copy'), document.createTextNode(label));
  button.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(getText());
      button.lastChild.textContent = '已复制';
      setTimeout(() => { button.lastChild.textContent = label; }, 1500);
    } catch (e) { toast('无法写入剪贴板。'); }
  });
  return button;
}

function codeBlock(code, label) {
  const block = el('div', 'codeblock');
  const head = el('div', 'codeblock-head');
  head.append(el('span', '', label || 'code'), copyButton(() => code));
  const pre = el('pre');
  pre.append(el('code', '', code));
  block.append(head, pre);
  return block;
}

// SVG from the model is shown only through <img>: an image document runs no scripts and loads
// nothing external, so markup in it cannot reach this page. It is never parsed into this page;
// a malformed one fails to load and the figure falls back to the code. <img> needs the
// namespace, so a missing one is added.
const SVG_LIMIT = 500000;
function svgSource(code) {
  const start = code.search(/<svg[\s>]/i), end = code.toLowerCase().lastIndexOf('</svg>');
  if (start < 0 || end < start || code.length > SVG_LIMIT) return null;
  const svg = code.slice(start, end + 6);
  return /^<svg[^>]*\sxmlns\s*=/i.test(svg) ? svg : svg.replace(/^<svg/i, `<svg xmlns="${SVG_NS}"`);
}

function svgFigure(code) {
  const svg = svgSource(code);
  if (!svg) return codeBlock(code, 'svg');
  const figure = el('figure', 'svg-figure');
  const frame = el('div', 'svg-frame');
  const img = document.createElement('img');
  img.alt = 'SVG 图片';
  img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
  frame.append(img);
  const bar = el('figcaption', 'svg-bar');
  const toggle = el('button', 'mini-btn', '查看代码');
  toggle.type = 'button';
  const source = codeBlock(svg, 'svg');
  source.hidden = true;
  toggle.addEventListener('click', () => {
    source.hidden = !source.hidden;
    toggle.textContent = source.hidden ? '查看代码' : '收起代码';
  });
  const download = el('button', 'mini-btn', '下载');
  download.type = 'button';
  download.addEventListener('click', () => {
    const url = URL.createObjectURL(new Blob([svg], { type: 'image/svg+xml' }));
    const a = document.createElement('a');
    a.href = url; a.download = 'aletheia.svg';
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  bar.append(el('span', '', 'SVG 图片'), toggle, copyButton(() => svg, '复制代码'), download);
  figure.append(frame, bar, source);
  img.addEventListener('error', () => {
    // not a valid SVG: show what the model wrote, as code
    const fallback = el('div');
    fallback.append(el('p', 'muted', '这段 SVG 无法显示成图片，下面是原始代码。'), codeBlock(code, 'svg'));
    figure.replaceWith(fallback);
  });
  return figure;
}

function renderMarkdown(text) {
  const root = el('div', 'md');
  const lines = String(text || '').replace(/\r\n/g, '\n').split('\n');
  let i = 0, para = [], list = null;
  const flush = () => {
    if (para.length) { const p = el('p'); p.append(inline(para.join('\n'))); root.append(p); para = []; }
    list = null;
  };
  while (i < lines.length) {
    const line = lines[i];
    const fence = line.match(/^\s*```\s*([\w+-]*)\s*$/);
    if (fence) {
      flush();
      const body = [];
      i += 1;
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) { body.push(lines[i]); i += 1; }
      i += 1;
      const code = body.join('\n');
      const lang = fence[1].toLowerCase();
      const looksSvg = /^\s*(<\?xml[^>]*>\s*)?(<!--[\s\S]*?-->\s*)*<svg[\s>]/i.test(code);
      root.append(looksSvg && ['', 'svg', 'xml', 'html'].includes(lang) ? svgFigure(code) : codeBlock(code, fence[1]));
      continue;
    }
    if (/^\s*(<\?xml[^>]*>\s*)?<svg[\s>]/i.test(line)) {
      // a bare SVG outside a code fence: take everything up to the closing tag
      const body = [];
      let j = i;
      while (j < lines.length) { body.push(lines[j]); if (/<\/svg>/i.test(lines[j])) break; j += 1; }
      if (j < lines.length) {
        flush();
        root.append(svgFigure(body.join('\n')));
        i = j + 1;
        continue;
      }
    }
    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    const bullet = line.match(/^\s*[-*+]\s+(.+)$/);
    const number = line.match(/^\s*\d+[.)]\s+(.+)$/);
    const quote = line.match(/^>\s?(.*)$/);
    if (heading) {
      flush();
      const h = el('h' + Math.min(5, heading[1].length + 2));
      h.append(inline(heading[2]));
      root.append(h);
    } else if (bullet || number) {
      if (para.length) flush();
      const kind = bullet ? 'ul' : 'ol';
      if (!list || list.tagName.toLowerCase() !== kind) { list = el(kind); root.append(list); }
      const li = el('li');
      li.append(inline((bullet || number)[1]));
      list.append(li);
    } else if (quote) {
      flush();
      const q = el('blockquote');
      q.append(inline(quote[1]));
      root.append(q);
    } else if (!line.trim()) {
      flush();
    } else {
      list = null;
      para.push(line);
    }
    i += 1;
  }
  flush();
  return root;
}

// ---- sign-in --------------------------------------------------------------------------

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

// ---- status ---------------------------------------------------------------------------

async function refreshHealth() {
  const health = await api('/api/health');
  $('status-write').textContent = health.write_enabled ? '开启' : '关闭';
  $('dot-write').className = 'dot' + (health.write_enabled ? ' warn' : '');
  $('status-memory').textContent = health.longterm_memory ? '可用' : '未开启';
  $('dot-memory').className = 'dot' + (health.longterm_memory ? ' on' : '');
  $('status-runs').textContent = String(health.active_runs);
  $('dot-runs').className = 'dot' + (health.active_runs ? ' busy' : '');
  $('toggle-memory').hidden = !health.longterm_memory;
  applyCaps(health.options);
  return health;
}

// ---- per-run options (mode, writes, long-term memory) ------------------------------------

const MODE_NAMES = {single: '普通模式', team: '团队模式', auto: '自动选择'};

function applyCaps(caps) {
  // An older server without per-run options keeps the controls hidden.
  if (!caps || !Array.isArray(caps.modes)) { state.caps = null; $('run-options').hidden = true; return; }
  // A server from before D-065 offers no runner choice: treat it as plan only and never send one.
  const runnerSupported = Array.isArray(caps.runners) && caps.runners.length > 0;
  const runners = runnerSupported ? caps.runners : ['plan'];
  caps = {...caps, runners, runner_supported: runnerSupported,
          default_runner: runners.includes(caps.default_runner) ? caps.default_runner : runners[0]};
  const first = !state.caps;
  state.caps = caps;
  if (first) {
    const kept = saved(OPTIONS_KEY, {}) || {};
    state.options = {
      mode: caps.modes.includes(kept.mode) ? kept.mode : caps.default_mode,
      runner: caps.runners.includes(kept.runner) ? kept.runner : caps.default_runner,
      write: caps.write_available ? (typeof kept.write === 'boolean' ? kept.write : Boolean(caps.write_default)) : false,
      memory: caps.memory_available ? (typeof kept.memory === 'boolean' ? kept.memory : Boolean(caps.memory_default)) : false,
    };
  }
  if (!caps.runners.includes(state.options.runner)) state.options.runner = caps.default_runner;
  for (const option of $('opt-mode').options) {
    option.disabled = !caps.modes.includes(option.value);
    option.textContent = MODE_NAMES[option.value] + (option.disabled ? '（未开启）' : '');
  }
  $('run-options').hidden = false;
  renderOptions();
}

function renderOptions() {
  if (!state.caps) return;
  const caps = state.caps, opts = state.options, answering = Boolean(state.waiting);
  $('opt-mode').value = opts.mode;
  $('opt-runner').value = opts.runner;
  $('runner-option').hidden = caps.runners.length === 1;
  $('opt-runner').disabled = answering || opts.mode === 'team';
  $('runner-option').title = answering ? '回答问题时沿用原任务的设置'
    : opts.mode === 'team' ? '团队模式使用自己的执行方式'
    : '按计划：先列步骤再逐步执行；单一循环：模型边做边决定，调用更少';
  for (const [id, key, available, off] of [
    ['opt-write', 'write', caps.write_available, '启动服务时没有指定可写目录，写文件不可用'],
    ['opt-memory', 'memory', caps.memory_available, '服务没有配置长期记忆'],
  ]) {
    const button = $(id);
    button.setAttribute('aria-pressed', String(Boolean(opts[key])));
    button.disabled = answering || !available;
    button.title = answering ? '回答问题时沿用原任务的设置' : !available ? off
      : opts[key] ? '已开启，点一下关闭' : '已关闭，点一下开启';
  }
  $('opt-mode').disabled = answering;
  $('opt-mode').parentElement.title = answering ? '回答问题时沿用原任务的设置'
    : '普通：一个执行者按计划逐步完成；团队：负责人把任务委派给执行者；自动：每个新任务先判断用哪种';
}

function setOption(key, value) {
  state.options[key] = value;
  save(OPTIONS_KEY, state.options);
  renderOptions();
}

// ---- sessions -------------------------------------------------------------------------

async function loadSessions() {
  const {sessions} = await api('/api/sessions?limit=200');
  state.sessions = sessions;
  renderSessions();
  return sessions;
}

function renderSessions() {
  const term = $('session-search').value.trim().toLowerCase();
  const list = $('session-list');
  list.replaceChildren();
  const shown = state.sessions.filter((s) => !term || (s.last_message || '').toLowerCase().includes(term));
  let group = null;
  for (const s of shown) {
    const name = dayGroup(s.updated_at);
    if (name !== group) { group = name; list.append(el('div', 'session-group', name)); }
    const button = el('button', 'session-item' + (s.session_id === state.session ? ' active' : ''));
    button.type = 'button';
    button.append(el('span', 'title', firstLine(s.last_message, 80) || '（空会话）'), el('span', 'time', timeText(s.updated_at)));
    button.addEventListener('click', () => openSession(s.session_id));
    list.append(button);
  }
  if (!shown.length) list.append(el('p', 'side-empty', term ? '没有匹配的会话。' : '还没有会话。发一条消息，它就会出现在这里。'));
}

async function openSession(sessionId) {
  state.session = sessionId;
  state.run = null;
  closePanels();
  toast('');
  renderSessions();
  await loadTurns();
  const runId = saved(RUN_KEY, {})[sessionId];
  setBusy(false);
  if (runId) resumeFollowing(runId);
  $('input').focus();
}

function newSession() {
  state.session = newId('web-');
  state.run = null;
  state.turns = [];
  closePanels();
  toast('');
  renderSessions();
  renderTurns();
  setBusy(false);
  $('input').focus();
}

// ---- conversation as a document -------------------------------------------------------

async function loadTurns() {
  const {turns} = await api(`/api/sessions/${encodeURIComponent(state.session)}/turns?limit=100`);
  state.turns = turns;
  renderTurns();
}

function article() { return $('conversation').querySelector('.article'); }
function turnsBox() { return $('conversation').querySelector('.turns'); }

function welcome() {
  const box = el('div', 'article welcome');
  box.append(el('h1', 'display', '欢迎使用 Aletheia'),
             el('p', '', 'Aletheia 会先把你的目标拆成几个步骤，再逐步完成；需要的时候会读取文件、查找资料。信息不够时，它会停下来问你，回答之后同一个任务接着做。'),
             el('p', '', '可以从这些例子开始：'));
  const list = el('ul', 'coral-list');
  for (const text of EXAMPLES) {
    const item = el('li');
    const button = el('button', '', text);
    button.type = 'button';
    button.addEventListener('click', () => {
      $('input').value = text.replace(/（.*）$/, '');
      autosize();
      $('input').focus();
    });
    item.append(button);
    list.append(item);
  }
  box.append(list, el('hr', 'rule'),
             el('p', 'muted', '每个任务的执行过程会列在回复下面：计划了哪几步、每一步用了哪些工具、花了多久。'));
  return box;
}

function documentFrame() {
  const box = el('div', 'article');
  const title = el('h1', 'display', firstLine(state.turns[0]?.user) || '新会话');
  title.id = 'doc-title';
  const byline = el('p', 'byline');
  byline.id = 'doc-byline';
  box.append(title, byline, el('hr', 'rule'), el('div', 'turns'));
  return box;
}

function renderByline() {
  const byline = $('doc-byline');
  if (!byline) return;
  const turns = state.turns;
  const last = turns[turns.length - 1];
  byline.textContent = last ? `${turns.length} 轮对话 · 最近更新 ${timeText(last.finished_at || last.created_at)}` : '';
}

function renderTurns() {
  const conversation = $('conversation');
  conversation.replaceChildren();
  state.waiting = null;
  if (!state.turns.length) {
    conversation.append(welcome());
  } else {
    conversation.append(documentFrame());
    renderByline();
    state.turns.forEach((turn, n) => {
      const box = turnBlock(turn.user, turn.created_at, n);
      if (turn.status !== 'received') box.append(agentPart(turn));
      turnsBox().append(box);
    });
    const last = state.turns[state.turns.length - 1];
    if (last.status === 'blocked') {
      if (last.resumable) state.waiting = {task_id: last.task_id, question: last.reply};
      else toast('上面的问题已经不能接着回答了（服务重启过），请重新提问。');
    }
  }
  renderOutline();
  updateComposer();
  conversation.scrollTop = conversation.scrollHeight;
}

function speaker(name, kind, iso, status) {
  const row = el('div', 'speaker');
  const avatar = el('span', 'avatar ' + kind, kind === 'agent' ? '' : '你');
  if (kind === 'agent') avatar.append(icon('i-logo'));
  avatar.setAttribute('aria-hidden', 'true');
  row.append(avatar, el('span', 'who' + (kind === 'agent' ? ' agent' : ''), name));
  if (iso) row.append(el('span', '', timeText(iso, false)));
  if (status) row.append(tag(status));
  return row;
}

function turnBlock(text, iso, index) {
  const box = el('section', 'turn');
  box.id = 'turn-' + index;
  const card = el('div', 'user-card');
  card.append(speaker('你', 'user', iso), el('p', 'question-text', text));
  box.append(card);
  return box;
}

// turn: {status, reply, finished_at}; extra: {message, usage, trace}
function agentPart(turn, extra = {}) {
  const part = el('div', 'agent-part');
  part.append(speaker('Aletheia', 'agent', turn.finished_at, turn.status));
  const reply = el('div', 'reply' + (turn.status === 'blocked' ? ' blocked' : ''));
  if (turn.status === 'completed' || turn.status === 'blocked') {
    reply.append(renderMarkdown(turn.reply));
  } else if (turn.status === 'cancelled') {
    reply.append(el('p', 'note', '任务已停止。停止前调用过的工具可能已经产生了副作用。'));
  } else {
    // An archived failure reply is the host's English diagnostic; show the Chinese explanation.
    reply.append(el('p', 'note', extra.message || '任务没有完成，可以换个说法或拆小一点再试。'));
  }
  part.append(reply);
  const usage = extra.usage;
  if (turn.status === 'completed' || (usage && usage.model_calls)) {
    const foot = el('div', 'reply-foot');
    if (usage && usage.model_calls) {
      for (const [name, text] of [['i-logo', `模型调用 ${usage.model_calls} 次`],
                                  ['i-copy', `令牌 ${(usage.total_tokens || 0).toLocaleString('zh-CN')}`],
                                  ['i-clock', `${usage.seconds || 0} 秒`]]) {
        const stat = el('span', 'stat');
        stat.append(icon(name), document.createTextNode(text));
        foot.append(stat);
      }
      // D-054: shown only when the provider reports cache hits; "not reported" is not "0".
      if (usage.calls_with_cache_report && usage.prompt_tokens) {
        const cached = usage.cached_prompt_tokens || 0;
        const stat = el('span', 'stat', `命中缓存 ${Math.floor(cached * 100 / usage.prompt_tokens)}%`);
        stat.title = `输入令牌 ${usage.prompt_tokens.toLocaleString('zh-CN')}，其中 ${cached.toLocaleString('zh-CN')} 命中服务商缓存`;
        foot.append(stat);
      }
    }
    if (turn.status === 'completed') foot.append(copyButton(() => turn.reply || '', '复制回复'));
    part.append(foot);
  }
  if (turn.task_id && ['completed', 'blocked', 'failed', 'cancelled'].includes(turn.status)) {
    let foot = part.querySelector('.reply-foot');
    if (!foot) { foot = el('div', 'reply-foot'); part.append(foot); }
    foot.append(traceButton(part, turn.task_id));
  }
  if (extra.trace) part.append(extra.trace);
  return part;
}

// ---- run record: the trace of a finished task, from its audit files (trace-001, D-073) ----

const SPAN_TEXT = {task: '任务', route: '路由', plan: '规划', step: '步骤', member: '成员', model: '模型',
                   tool: '工具', todo: '清单'};
const SPAN_STATUS = {failed: '失败', error: '出错', denied: '被拒绝', unfinished: '未结束', blocked: '等待回答'};

function number(n) { return (n || 0).toLocaleString('zh-CN'); }
function secondsText(s) { return typeof s !== 'number' ? '—' : `${s < 1 ? s.toFixed(2) : s.toFixed(1)} 秒`; }

function traceButton(part, taskId) {
  const button = el('button', 'mini-btn');
  button.type = 'button';
  button.append(icon('i-clock'), document.createTextNode('执行记录'));
  button.setAttribute('aria-expanded', 'false');
  let panel = null;
  button.addEventListener('click', async () => {
    if (panel) {
      panel.hidden = !panel.hidden;
      button.setAttribute('aria-expanded', String(!panel.hidden));
      return;
    }
    button.disabled = true;
    try {
      const {runs} = await api(`/api/sessions/${encodeURIComponent(state.session)}/tasks/${encodeURIComponent(taskId)}/trace`);
      panel = tracePanel(runs);
    } catch (e) {
      if (e.status === 401) return;
      panel = el('div', 'run-trace');
      panel.append(el('p', 'note', e.message || '读取执行记录失败。'));
    } finally {
      button.disabled = false;
    }
    part.querySelector('.reply-foot').after(panel);
    button.setAttribute('aria-expanded', 'true');
  });
  return button;
}

function spanHead(span) {
  const a = span.attrs || {};
  if (span.kind === 'plan') return a.revision ? `规划 第 ${a.revision} 版` : '规划';
  if (span.kind === 'route') return '选择执行方式';
  if (span.kind === 'todo') return `清单 ${a.items || 0} 项（完成 ${a.done || 0}）`;
  if (span.kind === 'task') return `任务 ${String(span.label).slice(0, 8)}`;
  return `${SPAN_TEXT[span.kind] || span.kind} ${span.label}`;
}

function spanDetail(span) {
  const a = span.attrs || {};
  const parts = [];
  if (span.kind === 'model') {
    if (a.prompt_tokens != null) parts.push(`输入 ${number(a.prompt_tokens)}`);
    if (a.completion_tokens != null) parts.push(`输出 ${number(a.completion_tokens)}`);
    if (a.cached_tokens) parts.push(`缓存 ${number(a.cached_tokens)}`);
    if (a.tool_calls) parts.push(`请求工具 ${a.tool_calls}`);
    if (a.provider_calls > 1) parts.push(`请求 ${a.provider_calls} 次`);
    if (a.response_model) parts.push(`服务商模型 ${a.response_model}`);
  }
  if (span.kind === 'tool') {
    if (span.status === 'ok' && a.chars != null) parts.push(`${number(a.chars)} 字符`);
    for (const [key, value] of Object.entries(a.args_summary || {})) {
      if (value === null || typeof value !== 'object') parts.push(`${key}=${value}`);
    }
  }
  if (span.kind === 'step' && a.runner) parts.push(`执行方式 ${a.runner}`);
  if (SPAN_STATUS[span.status]) parts.push(SPAN_STATUS[span.status]);
  if (a.denied_by) parts.push(`由 ${a.denied_by}`);
  if (a.error) parts.push(`错误类型 ${a.error}`);
  return parts.join(' · ');
}

function flatSpans(spans, depth = 0, out = []) {
  for (const span of spans || []) {
    out.push([span, depth]);
    flatSpans(span.children, depth + 1, out);
  }
  return out;
}

function tracePanel(runs) {
  const box = el('div', 'run-trace');
  runs.forEach((run, index) => {
    const section = el('section', 'trace-run');
    if (runs.length > 1) section.append(el('h4', '', `第 ${index + 1} 次运行`));
    const t = run.totals || {};
    section.append(el('p', 'trace-totals',
      [secondsText(run.seconds), `模型调用 ${t.model_calls || 0} 次`, `工具调用 ${t.tool_calls || 0} 次`,
       `输入 ${number(t.prompt_tokens)}`, `输出 ${number(t.completion_tokens)}`, `缓存命中 ${number(t.cached_tokens)}`].join(' · ')));
    if (!run.complete) section.append(el('p', 'trace-warning', '记录不完整：有调用没有结束记录、事件超过上限被丢弃，或运行没有正常结束。'));
    if (run.paired_by_order) section.append(el('p', 'trace-warning', '这是旧版记录，没有调用之间的关系，按出现顺序平铺显示。'));
    const rows = flatSpans(run.spans);
    // The bars share one time axis: from the first span's start to the last span's end.
    let first = Infinity, last = 0;
    for (const [span] of rows) {
      first = Math.min(first, span.start || 0);
      last = Math.max(last, (span.start || 0) + (span.seconds || 0));
    }
    const range = Math.max(last - first, 0.001);
    const list = el('ol', 'trace-rows');
    for (const [span, depth] of rows) {
      const row = el('li', `trace-row kind-${span.kind} status-${span.status}`);
      const name = el('div', 'trace-name');
      name.style.setProperty('--depth', String(depth));
      name.append(el('span', 'trace-head', spanHead(span)));
      const detail = spanDetail(span);
      if (detail) name.append(el('span', 'trace-detail', detail));
      const time = el('span', 'trace-time', span.status === 'unfinished' ? '未结束' : secondsText(span.seconds));
      const lane = el('div', 'trace-lane');
      const bar = el('span', 'trace-bar');
      const left = ((span.start || 0) - first) / range * 100;
      bar.style.setProperty('left', `${Math.min(Math.max(left, 0), 100)}%`);
      bar.style.setProperty('width', `${Math.max((span.seconds || 0) / range * 100, 0.5)}%`);
      lane.append(bar);
      row.append(name, time, lane);
      list.append(row);
    }
    section.append(list);
    box.append(section);
  });
  return box;
}

function renderOutline() {
  const list = $('outline-list');
  list.replaceChildren();
  if (!state.turns.length) { list.append(el('p', 'outline-empty', '还没有内容')); return; }
  state.turns.forEach((turn, n) => {
    const button = el('button', '', firstLine(turn.user, 40));
    button.type = 'button';
    button.title = turn.user;
    button.addEventListener('click', () => $('turn-' + n)?.scrollIntoView({behavior: 'smooth', block: 'start'}));
    list.append(button);
  });
  markOutline();
}

function markOutline() {
  const container = $('conversation');
  const buttons = $('outline-list').querySelectorAll('button');
  const top = container.getBoundingClientRect().top;
  const sections = container.querySelectorAll('.turn');
  let current = 0;
  sections.forEach((section, n) => {
    if (section.getBoundingClientRect().top - top <= 120) current = n;
  });
  if (container.scrollTop + container.clientHeight >= container.scrollHeight - 4) current = sections.length - 1;
  buttons.forEach((b, n) => b.classList.toggle('current', n === current));
}
$('conversation').addEventListener('scroll', markOutline, {passive: true});

function updateComposer() {
  const waiting = state.waiting;
  $('composer').classList.toggle('waiting-on', Boolean(waiting));
  $('waiting-banner').hidden = !waiting;
  $('waiting-question').textContent = waiting ? waiting.question || '' : '';
  $('input').placeholder = waiting ? '在这里回答上面的问题' : '描述你想完成的事情';
  renderOptions();
}

function setBusy(busy) {
  $('send').disabled = busy;
  $('input').disabled = busy;
  $('cancel').hidden = !busy;
  $('cancel').disabled = false;
  $('run-chip').hidden = !busy;
}

function autosize() {
  const input = $('input');
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 220) + 'px';
}

// ---- live runs ------------------------------------------------------------------------

class LiveView {
  constructor(turnBox) {
    this.turn = turnBox;
    this.part = el('div', 'agent-part');
    this.part.append(speaker('Aletheia', 'agent', null, 'running'));
    this.status = el('div', 'thinking');
    this.status.append(el('span', 'pulse'), el('span', '', '正在理解你的需求'));
    this.route = el('p', 'route');
    this.steps = el('ol', 'steps');
    this.steps.hidden = true;
    this.items = new Map();       // step_id -> step item
    this.approvals = new Map();   // approval_id -> card (D-061)
    this.runId = null;
    this.order = [];
    this.part.append(this.status, this.route, this.steps);
    turnBox.append(this.part);
  }

  say(text) { this.status.lastChild.textContent = text; }

  item(title, stepId) {
    const li = el('li', 'step pending');
    const body = el('div', 'step-body');
    body.append(el('div', 'step-title', title), el('div', 'step-meta'));
    li.append(el('span', 'step-mark'), body);
    this.steps.append(li);
    this.steps.hidden = false;
    if (stepId) this.items.set(stepId, li);
    this.order.push(li);
    return li;
  }

  activateNext() {
    for (const li of this.order) li.classList.remove('active');
    const next = this.order.find((li) => li.classList.contains('pending'));
    if (next) { next.classList.remove('pending'); next.classList.add('active'); }
  }

  show(event) {
    switch (event.type) {
      case 'route': {
        const loop = event.mode !== 'team' && event.options?.runner === 'loop';
        this.route.textContent = (event.mode === 'team' ? '团队模式'
          : '普通模式' + (loop ? '（单一循环）' : '（按计划）'))
          + (event.read_only ? '，只读' : '');
        this.say(loop ? '正在执行，不单独规划步骤' : '正在规划步骤');   // the loop makes no planning call
        break;
      }
      case 'plan': {
        if (event.revision > 1) {
          for (const li of this.order) if (!li.classList.contains('done') && !li.classList.contains('fail')) li.remove();
          this.order = this.order.filter((li) => li.isConnected);
          this.steps.append(el('li', 'route', `第 ${event.revision} 次规划`));
        }
        const ids = event.step_ids || [];
        event.steps.forEach((title, n) => this.item(title, ids[n]));
        this.say(`共 ${event.steps.length} 步，正在执行`);
        this.activateNext();
        break;
      }
      case 'step': {
        const li = this.items.get(event.step_id) || this.order.find((x) => x.classList.contains('active'))
                   || this.item(`第 ${event.step} 步`);
        const [tone, name, text] = OUTCOME[event.outcome] || ['fail', 'i-x', event.outcome];
        li.className = 'step ' + tone;
        li.querySelector('.step-mark').replaceChildren(icon(name));
        const meta = li.querySelector('.step-meta');
        meta.replaceChildren(el('span', '', `${text}，${event.seconds} 秒`));
        for (const t of event.tools) meta.append(el('span', 'tool' + (t.success ? '' : ' bad'), t.tool + (t.success ? '' : '（失败）')));
        if (event.outcome === 'succeeded') this.activateNext();
        break;
      }
      case 'approval_required':
        this.askApproval(event);
        this.say('等待你确认');
        break;
      case 'approval_resolved': {
        const card = this.approvals.get(event.approval_id);
        if (card) {
          card.classList.add('answered');
          card.querySelector('.approval-actions').replaceChildren(
            el('span', 'approval-result', APPROVAL_RESULTS[event.decision] || event.decision));
        }
        this.say('正在执行');
        break;
      }
      default:
        return this.finish(event);
    }
    scrollToEnd();
    return false;
  }

  askApproval(event) {
    const card = el('div', 'approval');
    card.append(el('div', 'approval-title', '这一步要执行有副作用的操作，需要你确认'),
                el('div', 'approval-what', approvalText(event)));
    const actions = el('div', 'approval-actions');
    const choices = [['allow', '允许', 'pill pill-sm'], ['deny', '拒绝', 'ghost-btn'],
                     ['allow_all', '本次运行都允许', 'link-btn']];
    for (const [decision, label, cls] of choices) {
      const button = el('button', cls, label);
      button.type = 'button';
      button.addEventListener('click', async () => {
        for (const b of actions.querySelectorAll('button')) b.disabled = true;
        try {
          await api(`/api/runs/${encodeURIComponent(this.runId)}/approvals`,
                    {method: 'POST', body: {approval_id: event.approval_id, decision}});
        } catch (e) {
          for (const b of actions.querySelectorAll('button')) b.disabled = false;
          toast(e.message);
        }
      });
      actions.append(button);
    }
    card.append(actions);
    this.approvals.set(event.approval_id, card);
    this.part.append(card);
  }

  finish(event) {
    if (!['completed', 'blocked', 'failed', 'cancelled'].includes(event.type)) return false;
    for (const li of this.order) li.classList.remove('active');
    let trace = null;
    if (this.order.length) {
      trace = el('details', 'trace');
      const done = this.order.filter((li) => li.classList.contains('done')).length;
      trace.append(el('summary', '', `执行过程，${done}/${this.order.length} 步完成`));
      if (this.route.textContent) trace.append(this.route);
      trace.append(this.steps);
      for (const card of this.approvals.values()) trace.append(card);   // what the user decided (D-061)
      trace.open = event.type === 'failed';
    }
    const turn = {status: event.type, reply: event.content || event.question || '', finished_at: new Date().toISOString(),
                  task_id: event.task_id};
    this.part.replaceWith(agentPart(turn, {message: event.message, usage: event.usage, trace}));
    if (event.type === 'blocked') { state.waiting = {task_id: event.task_id, question: event.question}; updateComposer(); }
    scrollToEnd();
    return true;
  }
}

function scrollToEnd() { const c = $('conversation'); c.scrollTop = c.scrollHeight; }

function rememberRun(runId) {
  const runs = saved(RUN_KEY, {});
  if (runId) runs[state.session] = runId; else delete runs[state.session];
  save(RUN_KEY, runs);
}

function appendTurn(text) {
  if (!turnsBox()) {
    $('conversation').replaceChildren(documentFrame());
    $('doc-title').textContent = firstLine(text) || '新会话';
  }
  const box = turnBlock(text, new Date().toISOString(), state.turns.length);
  turnsBox().append(box);
  scrollToEnd();
  return box;
}

async function send(text) {
  const body = {content: text, request_id: newId('')};
  if (state.waiting) body.resume_task_id = state.waiting.task_id;      // a resumed task keeps its options
  else if (state.caps && state.options) {
    body.options = {...state.options};
    if (!state.caps.runner_supported) delete body.options.runner;   // an older server refuses unknown fields
  }
  if (!state.session) state.session = newId('web-');
  setBusy(true);
  toast('');
  const box = appendTurn(text);
  try {
    const {run_id} = await api(`/api/sessions/${encodeURIComponent(state.session)}/messages`, {method: 'POST', body});
    $('input').value = '';
    autosize();
    state.turns.push({user: text, status: 'received', created_at: new Date().toISOString()});
    renderByline();
    renderOutline();
    state.waiting = null;
    updateComposer();
    rememberRun(run_id);
    refreshHealth().catch(() => {});
    await follow(run_id, new LiveView(box));
  } catch (e) {
    setBusy(false);
    box.remove();
    if (!state.turns.length) renderTurns();
    if (e.code === 'task_not_resumable') { state.waiting = null; updateComposer(); }
    toast(e.message);
  }
}

async function resumeFollowing(runId) {
  try {
    const snapshot = await api(`/api/runs/${encodeURIComponent(runId)}`);
    if (snapshot.status !== 'running') { rememberRun(null); return; }
    setBusy(true);
    const last = turnsBox()?.lastElementChild || appendTurn('');
    await follow(runId, new LiveView(last));
  } catch (e) {
    rememberRun(null);
  }
}

async function follow(runId, view) {
  state.run = runId;
  view.runId = runId;
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
          // The turn is archived before routing, so the session list can show it now.
          if (event.type === 'route') loadSessions().catch(() => {});
          if (view.show(event)) { finish(runId); return; }
        }
      }
      failures = 0;
    } catch (e) {
      if (e.status === 401 || e.status === 404) { finish(runId); if (e.status === 404) toast('这次运行已经过期。'); return; }
      failures += 1;
      if (failures > 5) { finish(runId); toast('和服务的连接断了，刷新页面可以看到最新结果。'); return; }
      await new Promise((r) => setTimeout(r, 1000 * failures));
    }
  }
}

function finish(runId) {
  if (state.run === runId) state.run = null;
  rememberRun(null);
  setBusy(false);
  api(`/api/sessions/${encodeURIComponent(state.session)}/turns?limit=100`)
    .then(({turns}) => { state.turns = turns; renderByline(); }).catch(() => {});
  loadSessions().catch(() => {});
  refreshHealth().catch(() => {});
  $('input').focus();
}

// ---- panels and keys ------------------------------------------------------------------

function closePanels() {
  $('sessions').classList.remove('open');
  $('memory').hidden = true;
  $('toggle-memory').classList.remove('on');
  $('scrim').hidden = true;
}
$('open-sessions').addEventListener('click', () => {
  closePanels();
  $('sessions').classList.add('open');
  $('scrim').hidden = false;
});
$('close-sessions').addEventListener('click', closePanels);
$('scrim').addEventListener('click', closePanels);
$('toggle-memory').addEventListener('click', () => {
  const opening = $('memory').hidden;
  closePanels();
  if (!opening) return;
  $('memory').hidden = false;
  $('toggle-memory').classList.add('on');
  if (narrow()) $('scrim').hidden = false;
  loadMemories().catch((e) => toast(e.message));
});
$('close-memory').addEventListener('click', closePanels);
$('session-search').addEventListener('input', renderSessions);
$('new-session').addEventListener('click', newSession);
$('opt-mode').addEventListener('change', (e) => setOption('mode', e.target.value));
$('opt-runner').addEventListener('change', (e) => setOption('runner', e.target.value));
$('opt-write').addEventListener('click', () => setOption('write', !state.options.write));
$('opt-memory').addEventListener('click', () => setOption('memory', !state.options.memory));
document.addEventListener('keydown', (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
    event.preventDefault();
    if (narrow()) { $('open-sessions').click(); return; }
    $('session-search').focus();
  } else if (event.key === 'Escape') closePanels();
});

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
$('input').addEventListener('input', autosize);
$('cancel').addEventListener('click', async () => {
  if (!state.run) return;
  $('cancel').disabled = true;
  try { await api(`/api/runs/${encodeURIComponent(state.run)}/cancel`, {method: 'POST'}); }
  catch (e) { toast(e.message); $('cancel').disabled = false; }
});

// ---- long-term memory -----------------------------------------------------------------

async function loadMemories() {
  const {memories} = await api('/api/memories?limit=100');
  const list = $('memory-list');
  list.replaceChildren();
  for (const m of memories) {
    const item = el('li', 'memory-item');
    const meta = el('div', 'meta');
    const remove = el('button', 'mini-btn', '删除');
    remove.type = 'button';
    remove.addEventListener('click', async () => {
      try { await api(`/api/memories/${encodeURIComponent(m.memory_id)}`, {method: 'DELETE'}); await loadMemories(); }
      catch (e) { toast(e.message); }
    });
    meta.append(el('span', '', m.category === 'fact' ? '事实' : m.category), el('span', '', timeText(m.created_at)), remove);
    item.append(el('p', '', m.content), meta);
    list.append(item);
  }
  $('memory-empty').hidden = memories.length > 0;
  $('memory-count').textContent = memories.length ? ` 共 ${memories.length} 条。` : '';
}

$('memory-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const content = $('memory-input').value.trim();
  if (!content) return;
  try {
    await api('/api/memories', {method: 'POST', body: {content}});
    $('memory-input').value = '';
    await loadMemories();
  } catch (e) { toast(e.message); }
});

// ---- start ----------------------------------------------------------------------------

async function start() {
  await refreshHealth();
  $('login').hidden = true;
  $('app').hidden = false;
  const sessions = await loadSessions();
  if (sessions.length) await openSession(sessions[0].session_id);
  else newSession();
}

state.token = saved(TOKEN_KEY, '');
start().catch((e) => { if (e.status !== 401) showLogin(e.message); });
