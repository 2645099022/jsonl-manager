/**
 * Claude Code JSONL 会话管理 — 前端
 *
 * 关键设计:
 * - rewind 后被丢弃的对话仍保留在 jsonl 中, 表现为"同一个 parentUuid 出现多个子节点"
 *   把每条从 root 到 leaf 的完整路径当作一条"分支":
 *     主分支 = jsonl 末尾节点所在分支
 *     回滚分支 = 其它叶子(被 rewind 抛弃的旧线)
 * - 侧栏列出所有分支, 主线绿色, 回滚分支橙色
 * - 切换分支即重新渲染消息流, 在分叉点高亮提示
 */

const state = {
  projects: [],
  sessions: [],
  currentProject: null,
  currentSession: null,
  currentDetail: null,
  selectedBranch: null,
  recycle: { max_items: 30, count: 0, sessions: [] },
  rollback: { count: 0, sessions: [] },
  pendingDeleteSession: null,
  deleteInFlight: false,
  globalSearch: {
    enabled: false,
    loading: false,
    query: '',
    results: [],
    error: null,
    token: 0,
  },
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const trashIcon = `
  <svg viewBox="0 0 24 24" aria-hidden="true">
    <path d="M3 6h18" />
    <path d="M8 6V4h8v2" />
    <path d="M6 6l1 15h10l1-15" />
    <path d="M10 11v6" />
    <path d="M14 11v6" />
  </svg>`;
const rollbackIcon = `
  <svg viewBox="0 0 24 24" aria-hidden="true">
    <path d="M4 7h10a6 6 0 1 1-4.2 10.2" />
    <path d="M4 7l4-4" />
    <path d="M4 7l4 4" />
    <path d="M12 9v4l3 2" />
  </svg>`;
const RECYCLE_ROW_HEIGHT = 58;
const RECYCLE_OVERSCAN = 6;
let globalSearchTimer = 0;

function openModal(dlg) {
  if (!dlg?.showModal || dlg.open) return;
  requestAnimationFrame(() => {
    if (!dlg.open) dlg.showModal();
  });
}

async function fetchJson(url, options = {}) {
  const res = await fetch(url, options);
  const text = await res.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch (err) {
      const plain = text.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
      const msg = plain || `${res.status} ${res.statusText}`;
      throw new Error(`请求返回的不是 JSON：${msg.slice(0, 120)}`);
    }
  }
  if (!res.ok) {
    throw new Error(payload?.error || `请求失败 ${res.status}: ${url}`);
  }
  return payload || {};
}

const fmt = {
  bytes(n) {
    if (n == null) return '-';
    const u = ['B', 'K', 'M', 'G'];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return n.toFixed(n >= 100 ? 0 : 1) + u[i];
  },
  ts(t) {
    if (!t) return '';
    const d = typeof t === 'number' ? new Date(t * 1000) : new Date(t);
    if (isNaN(d)) return '';
    const pad = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  },
  short(s, n = 80) {
    if (!s) return '';
    s = String(s).replace(/\s+/g, ' ').trim();
    return s.length > n ? s.slice(0, n) + '…' : s;
  },
  escape(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  },
};

// 极简 markdown: code block + inline code, 其它当纯文本
function renderText(text) {
  if (!text) return '';
  let out = '';
  let i = 0;
  const blocks = text.split(/(```[\s\S]*?```)/g);
  for (const b of blocks) {
    if (b.startsWith('```') && b.endsWith('```')) {
      const inner = b.slice(3, -3);
      const nl = inner.indexOf('\n');
      const lang = nl > 0 ? inner.slice(0, nl).trim() : '';
      const code = nl > 0 ? inner.slice(nl + 1) : inner;
      out += `<pre><code class="lang-${fmt.escape(lang)}">${fmt.escape(code)}</code></pre>`;
    } else {
      const escaped = fmt.escape(b).replace(/`([^`\n]+)`/g, '<code>$1</code>');
      out += escaped;
    }
  }
  return out;
}

// ---------------------------------------------------------------- 数据加载
async function loadConfig() {
  try {
    const r = await fetchJson('/api/config');
    const el = document.querySelector('.meta code.path');
    if (el && r.projects_dir) el.textContent = r.projects_dir;
  } catch (err) {
    console.warn('Failed to load config', err);
  }
}

async function loadProjects() {
  const r = await fetchJson('/api/projects');
  state.projects = r.projects || [];
  renderProjects();
}
async function loadSessions(pid) {
  const r = await fetchJson(`/api/projects/${encodeURIComponent(pid)}/sessions`);
  state.sessions = r.sessions || [];
  renderSessions();
}

async function loadRecycleSettings() {
  try {
    const r = await fetchJson('/api/recycle');
    state.recycle = r || { max_items: 30, count: 0, sessions: [] };
    renderRecycleSettings();
  } catch (err) {
    console.warn('Failed to load recycle settings', err);
  }
}

async function loadRollbackStatus() {
  try {
    const r = await fetchJson('/api/rollback');
    state.rollback = r || { count: 0, sessions: [] };
    renderRollbackList();
  } catch (err) {
    console.warn('Failed to load rollback status', err);
  }
}

function highlightQuery(text, query) {
  const escaped = fmt.escape(text || '');
  const q = String(query || '').trim();
  if (!q) return escaped;
  const safe = q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return escaped.replace(new RegExp(safe, 'ig'), m => `<mark>${fmt.escape(m)}</mark>`);
}

function scheduleGlobalSearch() {
  clearTimeout(globalSearchTimer);
  globalSearchTimer = setTimeout(runGlobalSearch, 360);
}

async function runGlobalSearch() {
  const query = ($('#session-filter')?.value || '').trim();
  const token = ++state.globalSearch.token;
  state.globalSearch.query = query;
  state.globalSearch.error = null;
  state.globalSearch.results = [];
  if (!state.globalSearch.enabled) return;
  if (query.length < 2) {
    state.globalSearch.loading = false;
    renderSessions();
    return;
  }

  state.globalSearch.loading = true;
  renderSessions();
  try {
    const url = `/api/search?q=${encodeURIComponent(query)}&limit=80`;
    const payload = await fetchJson(url);
    if (token !== state.globalSearch.token) return;
    state.globalSearch.loading = false;
    state.globalSearch.results = payload.results || [];
    state.globalSearch.error = payload.error || null;
  } catch (err) {
    if (token !== state.globalSearch.token) return;
    state.globalSearch.loading = false;
    state.globalSearch.results = [];
    state.globalSearch.error = err.message || '全局搜索失败';
  }
  renderSessions();
}

function setGlobalSearchEnabled(enabled) {
  state.globalSearch.enabled = !!enabled;
  state.globalSearch.error = null;
  state.globalSearch.results = [];
  state.globalSearch.loading = false;
  state.globalSearch.token++;
  const btn = $('#global-search-toggle');
  const input = $('#session-filter');
  if (btn) {
    btn.classList.toggle('active', state.globalSearch.enabled);
    btn.setAttribute('aria-pressed', state.globalSearch.enabled ? 'true' : 'false');
  }
  if (input) {
    input.placeholder = state.globalSearch.enabled ? '全局搜索对话内容' : '按标题/ID 过滤';
  }
  if (state.globalSearch.enabled) {
    scheduleGlobalSearch();
  } else if (state.currentProject) {
    loadSessions(state.currentProject).catch(err => {
      alert(err.message || '加载会话列表失败');
      renderSessions();
    });
  } else {
    renderSessions();
  }
}

function renderRecycleSettings() {
  const maxInput = $('#recycle-max');
  const count = $('#recycle-count');
  const list = $('#recycle-list');
  if (maxInput) maxInput.value = state.recycle?.max_items || 30;
  if (count) count.textContent = state.recycle?.count || 0;
  renderSessionArchiveList(list, state.recycle?.sessions || [], {
    emptyText: '回收站为空',
    timeKey: 'deleted_at',
    restoreLabel: '还原',
    onRestore: restoreRecycledSession,
    idKey: 'trash_id',
  });
}

function renderRollbackList() {
  renderSessionArchiveList($('#rollback-list'), state.rollback?.sessions || [], {
    emptyText: '暂无回档会话',
    timeKey: 'rolled_back_at',
    restoreLabel: '还原',
    onRestore: restoreRollbackSession,
    idKey: 'rollback_id',
  });
}

function createArchiveRow(item, index, opts) {
  const row = document.createElement('div');
  row.className = 'recycle-item';
  row.style.transform = `translateY(${index * RECYCLE_ROW_HEIGHT}px)`;
  row.innerHTML = `
      <div class="recycle-info">
        <div class="recycle-title" title="${fmt.escape(item.title || item.session_id)}">${fmt.escape(item.title || '(无标题)')}</div>
        <div class="recycle-meta">
          <span title="${fmt.escape(item.project_id || '')}">${fmt.escape(item.project_id || '-')}</span>
          <span>${fmt.ts(item[opts.timeKey])}</span>
          <code>${fmt.escape(item.session_id || '')}</code>
        </div>
      </div>
      <button class="restore-btn" type="button">${fmt.escape(opts.restoreLabel || '还原')}</button>`;
  $('.restore-btn', row)?.addEventListener('click', async (ev) => {
    ev.currentTarget.disabled = true;
    try {
      await opts.onRestore(item[opts.idKey]);
    } catch (err) {
      alert(err.message || '回档失败');
      ev.currentTarget.disabled = false;
    }
  });
  return row;
}

function renderSessionArchiveList(list, sessions, opts) {
  if (!list) return;
  if (!sessions.length) {
    list.onscroll = null;
    list.style.height = '';
    list.innerHTML = `<div class="recycle-empty">${fmt.escape(opts.emptyText)}</div>`;
    return;
  }

  list.onscroll = null;
  list.style.height = `${Math.min(260, sessions.length * RECYCLE_ROW_HEIGHT)}px`;
  list.innerHTML = '<div class="recycle-virtual-spacer"></div>';
  const spacer = $('.recycle-virtual-spacer', list);
  spacer.style.height = `${sessions.length * RECYCLE_ROW_HEIGHT}px`;

  let raf = 0;
  const draw = () => {
    raf = 0;
    const viewportHeight = list.clientHeight || 260;
    const start = Math.max(0, Math.floor(list.scrollTop / RECYCLE_ROW_HEIGHT) - RECYCLE_OVERSCAN);
    const end = Math.min(
      sessions.length,
      Math.ceil((list.scrollTop + viewportHeight) / RECYCLE_ROW_HEIGHT) + RECYCLE_OVERSCAN
    );
    spacer.replaceChildren();
    const frag = document.createDocumentFragment();
    for (let i = start; i < end; i++) {
      frag.appendChild(createArchiveRow(sessions[i], i, opts));
    }
    spacer.appendChild(frag);
  };

  list.onscroll = () => {
    if (!raf) raf = requestAnimationFrame(draw);
  };
  draw();
}

async function saveRecycleSettings() {
  const input = $('#recycle-max');
  const maxItems = Number(input?.value || 30);
  const payload = await fetchJson('/api/recycle/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ max_items: maxItems }),
  });
  state.recycle = payload;
  renderRecycleSettings();
}

async function restoreRecycledSession(trashId) {
  if (!trashId) return;
  const payload = await fetchJson(`/api/recycle/${encodeURIComponent(trashId)}/restore`, {
    method: 'POST',
  });

  state.recycle = payload.recycle || state.recycle;
  renderRecycleSettings();
  if (state.currentProject) {
    await loadProjects();
    await loadSessions(state.currentProject);
  }
}

async function rollbackSession(session) {
  if (!session || !state.currentProject) return;
  const projectId = state.currentProject;
  const previousSessions = state.sessions.slice();
  const wasCurrent = state.currentSession === session.session_id;

  state.sessions = state.sessions.filter(s => s.session_id !== session.session_id);
  if (wasCurrent) clearSessionDetail();
  else renderSessions();

  let payload = {};
  try {
    payload = await fetchJson(`/api/projects/${encodeURIComponent(projectId)}/sessions/${encodeURIComponent(session.session_id)}/rollback`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: session.title || session.session_id }),
    });
  } catch (err) {
    state.sessions = previousSessions;
    renderSessions();
    if (state.currentProject === projectId) loadSessions(projectId);
    throw err;
  }

  state.rollback = payload.rollback || state.rollback;
  renderRollbackList();
  loadProjects();
}

async function restoreRollbackSession(rollbackId) {
  if (!rollbackId) return;
  const payload = await fetchJson(`/api/rollback/${encodeURIComponent(rollbackId)}/restore`, {
    method: 'POST',
  });

  state.rollback = payload.rollback || state.rollback;
  renderRollbackList();
  if (state.currentProject) {
    await loadProjects();
    await loadSessions(state.currentProject);
  }
}

async function loadSessionDetail(pid, sid, branchId = null) {
  const _t0 = performance.now();
  // 切走前先记下当前分支的滚动位置 (按 sid+branchId 独立保存)
  const prev = state.currentDetail?.selected_branch;
  if (prev) {
    const key = `${state.currentSession}::${prev.branch_id}`;
    state.scrollMemo = state.scrollMemo || {};
    state.scrollMemo[key] = $('#messages')?.scrollTop || 0;
    state.sidebarScrollMemo = state.sidebarScrollMemo || {};
    state.sidebarScrollMemo[key] = $('#branch-tree')?.scrollTop || 0;
  }

  const url = `/api/projects/${encodeURIComponent(pid)}/sessions/${encodeURIComponent(sid)}` +
              (branchId ? `?branch=${encodeURIComponent(branchId)}` : '');
  const _tFetchStart = performance.now();
  const r = await fetchJson(url);
  const _tFetchEnd = performance.now();
  state.currentDetail = r;
  state.selectedBranch = r.selected_branch?.branch_id || null;
  const _tBrStart = performance.now();
  renderBranches();
  const _tBrEnd = performance.now();
  const _tMsgStart = performance.now();
  renderMessages();
  const _tMsgEnd = performance.now();
  renderSessionHeader();
  console.log(
    `[jsonl-manager][detail] sid=${sid.slice(0,8)} ` +
    `fetch=${(_tFetchEnd-_tFetchStart).toFixed(1)}ms ` +
    `branches=${(_tBrEnd-_tBrStart).toFixed(1)}ms ` +
    `messages=${(_tMsgEnd-_tMsgStart).toFixed(1)}ms ` +
    `total=${(_tMsgEnd-_t0).toFixed(1)}ms ` +
    `nodes=${(r.nodes||[]).length} ` +
    `forks_at=${Object.keys(r.forks_at||{}).length}`
  );

  const newKey = r.selected_branch
    ? `${state.currentSession}::${r.selected_branch.branch_id}`
    : null;

  // 侧栏滚动: 按分支独立恢复 (没记忆过的新分支显示顶部)
  if (newKey) {
    requestAnimationFrame(() => {
      const sb = $('#branch-tree');
      if (sb) sb.scrollTop = state.sidebarScrollMemo?.[newKey] ?? 0;
    });
  }

  // 主视图滚动: 默认进新分支滚顶, restoreScroll=true 时恢复
  if (state.restoreScroll && newKey) {
    const saved = state.scrollMemo?.[newKey];
    if (saved != null) {
      requestAnimationFrame(() => { $('#messages').scrollTop = saved; });
    }
    state.restoreScroll = false;
  } else if (!state.skipDefaultScroll) {
    requestAnimationFrame(() => {
      const m = $('#messages');
      if (m) m.scrollTop = 0;
    });
  }
  state.skipDefaultScroll = false;
}

function scrollMessageToTop(uuid, retry = 0) {
  if (!uuid) return;
  requestAnimationFrame(() => {
    const target = document.querySelector(`#messages [data-uuid="${uuid}"]`);
    if (!target) {
      // DOM 可能还没渲染完, 重试 (限 5 次)
      if (retry < 5) setTimeout(() => scrollMessageToTop(uuid, retry + 1), 30);
      return;
    }
    const container = $('#messages');
    container.scrollTo({
      top: target.offsetTop - container.offsetTop - 12,
      behavior: 'smooth',
    });
    target.classList.add('msg-flash');
    setTimeout(() => target.classList.remove('msg-flash'), 1600);
  });
}

// ---------------------------------------------------------------- 渲染: 项目
function renderProjects() {
  const ul = $('#project-list');
  $('#project-count').textContent = state.projects.length;
  ul.innerHTML = '';
  if (!state.projects.length) {
    ul.innerHTML = '<li class="empty-state">未发现项目</li>';
    return;
  }
  for (const p of state.projects) {
    const li = document.createElement('li');
    li.dataset.pid = p.project_id;
    li.innerHTML = `
      <div class="item-title" title="${fmt.escape(p.cwd)}">${fmt.escape(p.cwd)}</div>
      <div class="item-sub">
        <span class="badge">${p.session_count} 会话</span>
        <span>${fmt.bytes(p.size)}</span>
        <span>${fmt.ts(p.mtime)}</span>
      </div>`;
    if (p.project_id === state.currentProject) li.classList.add('active');
    li.addEventListener('click', () => selectProject(p.project_id));
    ul.appendChild(li);
  }
}

async function selectProject(pid) {
  state.currentProject = pid;
  state.currentSession = null;
  state.currentDetail = null;
  renderProjects();
  await loadSessions(pid);
  // 清空右侧
  $('#branch-tree').innerHTML = '';
  $('#branch-count').textContent = '—';
  $('#messages').innerHTML = '';
  $('#session-header').className = 'session-header empty';
  $('#session-header').innerHTML = '<div class="placeholder">从左侧选择一个会话开始查看</div>';
}

// ---------------------------------------------------------------- 渲染: 会话
function clearSessionDetail() {
  state.currentSession = null;
  state.currentDetail = null;
  state.selectedBranch = null;
  renderSessions();
  $('#branch-tree').innerHTML = '';
  $('#branch-count').textContent = '-';
  $('#messages').innerHTML = '';
  $('#session-header').className = 'session-header empty';
  $('#session-header').innerHTML = '<div class="placeholder">请选择一个会话开始查看</div>';
}

function askDeleteSession(session) {
  state.pendingDeleteSession = session;
  const target = $('#delete-target');
  if (target) {
    target.innerHTML = `
      <div class="target-title" title="${fmt.escape(session.title || session.session_id)}">${fmt.escape(session.title || '(无标题)')}</div>
      <div class="target-sub"><code>${fmt.escape(session.session_id)}</code></div>`;
  }
  const dlg = $('#delete-dialog');
  if (dlg?.showModal) openModal(dlg);
  else if (confirm('确定要把这个会话移入回收站吗？')) confirmDeleteSession();
}

async function confirmDeleteSession() {
  const session = state.pendingDeleteSession;
  if (!session || !state.currentProject) return;
  const projectId = state.currentProject;
  const previousSessions = state.sessions.slice();
  const wasCurrent = state.currentSession === session.session_id;

  state.sessions = state.sessions.filter(s => s.session_id !== session.session_id);
  if (wasCurrent) clearSessionDetail();
  else renderSessions();
  state.pendingDeleteSession = null;

  let payload = {};
  try {
    payload = await fetchJson(`/api/projects/${encodeURIComponent(projectId)}/sessions/${encodeURIComponent(session.session_id)}`, {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: session.title || session.session_id }),
    });
  } catch (err) {
    state.sessions = previousSessions;
    renderSessions();
    if (state.currentProject === projectId) loadSessions(projectId);
    throw err;
  }

  state.recycle = payload.recycle || state.recycle;
  renderRecycleSettings();
  loadProjects();
}

function renderGlobalSearchResults() {
  const ul = $('#session-list');
  const query = state.globalSearch.query || ($('#session-filter')?.value || '').trim();
  $('#session-count').textContent = state.globalSearch.loading ? '...' : state.globalSearch.results.length;
  ul.innerHTML = '';

  if (!query || query.length < 2) {
    ul.innerHTML = '<li class="empty-state">输入至少 2 个字开始全局搜索</li>';
    return;
  }
  if (state.globalSearch.loading) {
    ul.innerHTML = '<li class="empty-state">正在调用 Everything 搜索...</li>';
    return;
  }
  if (state.globalSearch.error) {
    ul.innerHTML = `<li class="empty-state">${fmt.escape(state.globalSearch.error)}</li>`;
    return;
  }
  if (!state.globalSearch.results.length) {
    ul.innerHTML = '<li class="empty-state">没有搜到匹配的对话</li>';
    return;
  }

  for (const item of state.globalSearch.results) {
    const li = document.createElement('li');
    li.className = 'search-result';
    li.dataset.sid = item.session_id;
    if (item.session_id === state.currentSession && item.project_id === state.currentProject) {
      li.classList.add('active');
    }
    li.innerHTML = `
      <div class="item-title" title="${fmt.escape(item.title || item.session_id)}">${fmt.escape(item.title || item.session_id)}</div>
      <div class="item-sub">
        <span class="badge">${fmt.escape(item.role || 'message')}</span>
        <span title="${fmt.escape(item.project_id || '')}">${fmt.escape(item.project_id || '-')}</span>
        <span>${fmt.ts(item.timestamp || item.mtime)}</span>
      </div>
      <div class="search-snippet">${highlightQuery(item.snippet || '', query)}</div>`;
    li.addEventListener('click', () => {
      openGlobalSearchResult(item).catch(err => alert(err.message || '打开搜索结果失败'));
    });
    ul.appendChild(li);
  }
}

function renderSessions() {
  if (state.globalSearch.enabled) {
    renderGlobalSearchResults();
    return;
  }

  const ul = $('#session-list');
  const filter = ($('#session-filter').value || '').toLowerCase();
  const items = state.sessions.filter(s =>
    !filter ||
    (s.title || '').toLowerCase().includes(filter) ||
    (s.session_id || '').toLowerCase().includes(filter)
  );
  $('#session-count').textContent = items.length;
  ul.innerHTML = '';
  if (!items.length) {
    ul.innerHTML = '<li class="empty-state">无会话</li>';
    return;
  }
  for (const s of items) {
    const li = document.createElement('li');
    li.dataset.sid = s.session_id;
    const rewind = s.has_rewind
      ? `<span class="badge rewind" title="该会话存在 rewind 分叉">↺ rewind</span>` : '';
    li.innerHTML = `
      <div class="item-title" title="${fmt.escape(s.title)}">${fmt.escape(s.title)}</div>
      <div class="item-sub">
        <span class="badge">${s.message_count} 消息</span>
        <span class="badge">${s.branch_count} 分支</span>
        ${rewind}
        <span>${fmt.ts(s.mtime)}</span>
      </div>`;
    const actions = document.createElement('div');
    actions.className = 'session-actions';

    const del = document.createElement('button');
    del.className = 'session-action session-delete';
    del.type = 'button';
    del.title = '移入回收站';
    del.setAttribute('aria-label', '移入回收站');
    del.innerHTML = trashIcon;
    del.addEventListener('click', (ev) => {
      ev.stopPropagation();
      askDeleteSession(s);
    });

    const rb = document.createElement('button');
    rb.className = 'session-action session-rollback';
    rb.type = 'button';
    rb.title = '回档';
    rb.setAttribute('aria-label', '回档');
    rb.innerHTML = rollbackIcon;
    rb.addEventListener('click', (ev) => {
      ev.stopPropagation();
      rollbackSession(s).catch(err => alert(err.message || '回档失败'));
    });

    actions.appendChild(del);
    actions.appendChild(rb);
    li.appendChild(actions);

    if (s.session_id === state.currentSession) li.classList.add('active');
    li.addEventListener('click', () => selectSession(s.session_id));
    ul.appendChild(li);
  }
}

async function selectSession(sid) {
  state.currentSession = sid;
  renderSessions();
  await loadSessionDetail(state.currentProject, sid);
}

async function openGlobalSearchResult(item) {
  if (!item?.project_id || !item?.session_id) return;
  state.currentProject = item.project_id;
  state.currentSession = item.session_id;
  renderProjects();
  renderSessions();
  await loadSessionDetail(item.project_id, item.session_id);
  if (item.uuid) scrollMessageToTop(item.uuid);
}

// ---------------------------------------------------------------- 渲染: 分支侧栏
// 时间线视图: 主线节点串成骨架, 每个分叉点处插入"回滚旧对话"折叠组
function renderBranches() {
  const root = $('#branch-tree');
  const detail = state.currentDetail;
  if (!detail || !detail.branches?.length) {
    root.innerHTML = '<div class="empty-state">无分支信息</div>';
    $('#branch-count').textContent = '—';
    return;
  }
  $('#branch-count').textContent = detail.branches.length;
  root.innerHTML = '';

  const main = detail.branches.find(b => b.is_main);
  if (!main) return;

  // 当前选中的若是主线 - 渲染时间线; 否则渲染回滚分支详情
  if (state.selectedBranch === main.branch_id) {
    renderMainTimeline(root, detail, main);
  } else {
    renderRewindDetail(root, detail, main);
  }
}

function renderMainTimeline(root, detail, main) {
  const forksAt = detail.forks_at || {};
  const forkUuids = new Set(Object.keys(forksAt));

  // 顶部主线卡片 (始终高亮)
  const head = document.createElement('div');
  head.className = 'tl-head';
  head.innerHTML = `
    <div class="tl-head-row">
      <span class="tag main">● 主线</span>
      <span class="tl-head-id">#${main.branch_id}</span>
    </div>
    <div class="tl-head-title" title="${fmt.escape(main.title)}">${fmt.escape(main.title)}</div>
    <div class="tl-head-stats">${main.length} 节点 · ${fmt.ts(main.ended_at)}</div>`;
  root.appendChild(head);

  // 时间线
  const tl = document.createElement('div');
  tl.className = 'timeline';
  // 主线骨架: 主线上所有用户消息 + 错误 assistant + 各旧分支的"独有起点"
  const mainAnchors = (detail.nodes || []).filter(n => {
    if (n.is_meta) return false;
    if (n.is_tool_result) return false;
    if (n.role === 'user') return true;
    if (n.role === 'assistant' && (n.is_failed_retry || (n.text || '').startsWith('API Error'))) return true;
    if (forkUuids.has(n.uuid)) return true;
    return false;
  });
  const extraAnchors = (detail.extra_anchors || []).filter(n => {
    if (n.is_meta || n.is_tool_result) return false;
    return n.role === 'user' || n.role === 'assistant';
  });
  const anchorNodes = [...mainAnchors, ...extraAnchors].sort((a, b) =>
    (a.timestamp || '').localeCompare(b.timestamp || '')
  );

  for (const n of anchorNodes) {
    const dot = document.createElement('div');
    const isFork = forkUuids.has(n.uuid);
    const isError = !!(n.is_failed_retry || (n.role === 'assistant' && (n.text || '').startsWith('API Error')));
    const isExtra = !mainAnchors.includes(n);  // 旧分支起点
    // 旧分支起点是否是"错误回滚"
    const extraBranch = isExtra ? (forksAt[n.uuid] || [])[0] : null;
    const isExtraError = isExtra && extraBranch?.is_error;
    const cls = ['tl-node'];
    if (isFork) cls.push('is-fork');
    if (isError || isExtraError) cls.push('is-error');
    if (isExtra) cls.push('is-extra');
    if (isExtraError) cls.push('is-extra-error');
    if (n.role === 'assistant' && !isError) cls.push('is-asst');
    dot.className = cls.join(' ');
    const label = (n.text || '').replace(/\s+/g, ' ').trim().slice(0, 60) || `(${n.type})`;
    const ts = (n.timestamp || '').slice(11, 16);
    const prefix = isExtraError ? '⚠ ' : isExtra ? '↺ ' : '';
    dot.innerHTML = `
      <span class="tl-bullet"></span>
      <span class="tl-time">${ts}</span>
      <span class="tl-label">${prefix}${fmt.escape(label)}</span>`;
    dot.addEventListener('click', () => {
      if (isExtra) {
        // 切到旧分支, 切完后自动定位到那条分支的"独有起点"
        const targetUuid = n.uuid;
        state.skipDefaultScroll = true;
        loadSessionDetail(state.currentProject, state.currentSession, extraBranch.branch_id)
          .then(() => scrollMessageToTop(targetUuid));
        return;
      }
      scrollMessageToTop(n.uuid);
    });
    tl.appendChild(dot);

    // 主线锚点处可能有回滚分叉, 插入折叠组 (extra anchor 自己就是分叉, 不需要再嵌)
    if (!isExtra && forkUuids.has(n.uuid)) {
      const branches = (forksAt[n.uuid] || []).slice()
        .sort((a, b) => (a.ended_at || '').localeCompare(b.ended_at || ''));
      const grp = document.createElement('details');
      grp.className = 'tl-rewind-group';
      const containsSelected = branches.some(b => b.branch_id === state.selectedBranch);
      if (containsSelected) grp.open = true;
      const summary = document.createElement('summary');
      summary.innerHTML = `
        <span class="caret">▸</span>
        <span class="badge-rewind">↺ rewind</span>
        <span class="grp-meta">${branches.length} 旧分支</span>`;
      grp.appendChild(summary);
      const list = document.createElement('div');
      list.className = 'tl-rewind-list';
      for (const b of branches) {
        const card = document.createElement('div');
        const cls = ['tl-rewind-card'];
        if (b.is_error) cls.push('is-error');
        if (b.branch_id === state.selectedBranch) cls.push('active');
        card.className = cls.join(' ');
        const prefix = b.is_error ? '⚠ ' : '↺ ';
        const ts = (b.ended_at || '').slice(11, 16);
        card.innerHTML = `
          <div class="tl-rewind-title" title="${fmt.escape(b.title)}">${prefix}${fmt.escape(b.title)}</div>
          <div class="tl-rewind-stats">#${b.branch_id} · ${b.length} 独有节点 · ${ts}</div>`;
        card.addEventListener('click', () => {
          loadSessionDetail(state.currentProject, state.currentSession, b.branch_id);
        });
        list.appendChild(card);
      }
      grp.appendChild(list);
      tl.appendChild(grp);
    }
  }
  root.appendChild(tl);
}

function renderRewindDetail(root, detail, main) {
  const sel = detail.selected_branch;
  // 顶部: 提示这是回滚分支 + 返回主线按钮
  const back = document.createElement('div');
  back.className = 'tl-back';
  back.innerHTML = `← 返回主线`;
  back.addEventListener('click', () => {
    state.restoreScroll = true;  // 请求恢复主线之前的滚动位置
    loadSessionDetail(state.currentProject, state.currentSession, main.branch_id);
  });
  root.appendChild(back);

  const head = document.createElement('div');
  head.className = 'tl-head' + (sel.is_error ? ' is-error' : ' is-rewound');
  const tag = sel.is_error
    ? '<span class="tag err">⚠ 错误回滚</span>'
    : '<span class="tag rewind">↺ 回滚分支</span>';
  head.innerHTML = `
    <div class="tl-head-row">
      ${tag}
      <span class="tl-head-id">#${sel.branch_id}</span>
    </div>
    <div class="tl-head-title" title="${fmt.escape(sel.title)}">${fmt.escape(sel.title)}</div>
    <div class="tl-head-stats">${sel.length} 节点 · 分叉自 ${(sel.fork_from || '').slice(0,8)}</div>`;
  root.appendChild(head);

  // 列出同一分叉点的其它兄弟分支 - 时间正序, 与主侧栏一致
  const siblings = detail.branches
    .filter(b => !b.is_main && b.fork_from === sel.fork_from)
    .sort((a, b) => (a.ended_at || '').localeCompare(b.ended_at || ''));
  if (siblings.length > 1) {
    const wrap = document.createElement('div');
    wrap.className = 'tl-siblings';
    wrap.innerHTML = `<div class="tl-section-title">同分叉点的其它分支 (按时间正序)</div>`;
    for (const b of siblings) {
      const card = document.createElement('div');
      const cls = ['tl-rewind-card'];
      if (b.is_error) cls.push('is-error');
      if (b.branch_id === sel.branch_id) cls.push('active');
      card.className = cls.join(' ');
      const prefix = b.is_error ? '⚠ ' : '↺ ';
      const ts = (b.ended_at || '').slice(11, 16);
      card.innerHTML = `
        <div class="tl-rewind-title" title="${fmt.escape(b.title)}">${prefix}${fmt.escape(b.title)}</div>
        <div class="tl-rewind-stats">#${b.branch_id} · ${b.length} 节点 · ${ts}</div>`;
      card.addEventListener('click', () => {
        // 切完后定位到该分支的独有起点 (相对主线)
        const mainSet = new Set(main.node_uuids);
        const firstUnique = (b.node_uuids || []).find(u => !mainSet.has(u));
        if (firstUnique) state.skipDefaultScroll = true;
        loadSessionDetail(state.currentProject, state.currentSession, b.branch_id)
          .then(() => { if (firstUnique) scrollMessageToTop(firstUnique); });
      });
      wrap.appendChild(card);
    }
    root.appendChild(wrap);
  }
}

// ---------------------------------------------------------------- 渲染: 会话头
function renderSessionHeader() {
  const d = state.currentDetail;
  const h = $('#session-header');
  if (!d) return;
  h.className = 'session-header';
  const versions = (d.versions || []).join(' · ') || '-';
  const branchInfo = d.selected_branch
    ? (d.selected_branch.is_main
      ? '<span style="color:var(--main)">● 主线</span>'
      : '<span style="color:var(--rewind)">↺ 回滚分支</span>') + ` #${d.selected_branch.branch_id}`
    : '';
  h.innerHTML = `
    <h1>${fmt.escape(d.title || '(无标题)')}</h1>
    <div class="meta-row">
      <span>会话 <code>${fmt.escape(d.session_id)}</code></span>
      <span>cwd <code>${fmt.escape(d.cwd || '-')}</code></span>
      <span>git <code>${fmt.escape(d.git_branch || '-')}</code></span>
      <span>cli <code>${fmt.escape(versions)}</code></span>
      <span>${branchInfo}</span>
    </div>`;
}

// ---------------------------------------------------------------- 渲染: 消息流
function renderMessages() {
  const root = $('#messages');
  const d = state.currentDetail;
  root.innerHTML = '';
  if (!d || !d.nodes?.length) {
    root.innerHTML = '<div class="empty-state">该分支无消息</div>';
    return;
  }

  const forkSet = new Set(d.fork_points || []);
  const forksAt = d.forks_at || {};

  for (const n of d.nodes) {
    root.appendChild(renderMessage(n, forkSet));
    // 主线分叉点之后, 插入回滚分支折叠面板
    const oldBranches = forksAt[n.uuid];
    if (oldBranches?.length) {
      root.appendChild(renderForkSection(n.uuid, oldBranches));
    }
  }
}

function renderForkSection(forkUuid, oldBranches) {
  const wrap = document.createElement('details');
  wrap.className = 'inline-fork';
  // 记下展开状态, 避免重渲染时丢
  if (state.openForks?.has(forkUuid)) wrap.open = true;

  const totalNodes = oldBranches.reduce((a, b) => a + b.length, 0);
  const summary = document.createElement('summary');
  summary.innerHTML = `
    <span class="caret">▸</span>
    <span class="badge-rewind">↺ 回滚旧对话</span>
    <span class="meta">${oldBranches.length} 条分支 · ${totalNodes} 节点</span>
    <span class="hint">在此处发生 rewind, 旧对话已折叠</span>`;
  wrap.appendChild(summary);

  const body = document.createElement('div');
  body.className = 'inline-fork-body';
  for (const b of oldBranches) {
    const branchDiv = document.createElement('div');
    branchDiv.className = 'inline-fork-branch';
    const head = document.createElement('div');
    head.className = 'inline-fork-branch-head';
    head.innerHTML = `
      <span class="dot"></span>
      <span class="title" title="${fmt.escape(b.title)}">${fmt.escape(b.title)}</span>
      <span class="stats">${b.length} 节点 · ${fmt.ts(b.ended_at)}</span>`;
    branchDiv.appendChild(head);
    for (const n of b.nodes) {
      branchDiv.appendChild(renderMessage(n, new Set(), { isInRewind: true }));
    }
    body.appendChild(branchDiv);
  }
  wrap.appendChild(body);

  // 用户主动展开/折叠时, 记忆状态
  wrap.addEventListener('toggle', () => {
    if (!state.openForks) state.openForks = new Set();
    if (wrap.open) state.openForks.add(forkUuid);
    else state.openForks.delete(forkUuid);
  });

  return wrap;
}

function renderMessage(n, forkSet, opts = {}) {
  const el = document.createElement('div');
  const role = n.role || n.type;
  const cls = ['message'];
  if (role === 'user') cls.push('user');
  else if (role === 'assistant') cls.push('assistant');
  else if (role === 'system') cls.push('system');
  if (n.is_tool_result) cls.push('tool-result');
  if (n.is_meta) cls.push('meta');
  if (n.is_failed_retry) cls.push('failed');
  if (forkSet.has(n.uuid)) cls.push('fork-point');
  if (opts.isInRewind) cls.push('in-rewind');
  el.className = cls.join(' ');
  if (n.uuid) el.dataset.uuid = n.uuid;

  const flags = [];
  if (forkSet.has(n.uuid)) flags.push('<span class="msg-flag fork" title="此节点产生了 rewind 分叉">⎇ rewind 起点</span>');
  if (n.is_failed_retry) flags.push('<span class="msg-flag failed" title="API 错误后被自动重试">⚠ 失败请求</span>');
  if (n.is_meta) flags.push('<span class="msg-flag">meta</span>');
  if (n.is_sidechain) flags.push('<span class="msg-flag">sidechain</span>');
  if (n.is_command) flags.push('<span class="msg-flag cmd">命令</span>');

  const roleClass = n.is_tool_result ? 'tool' : (role === 'assistant' ? 'assistant' :
                    role === 'user' ? 'user' : 'system');
  const roleText = n.is_tool_result ? 'tool' : (role || '?');

  let body = '';
  if (n.text) body += `<div class="msg-text">${renderText(n.text)}</div>`;

  for (const tc of n.tool_calls || []) {
    const inputStr = JSON.stringify(tc.input ?? {}, null, 2);
    body += `
      <details class="tool-block" open>
        <summary>调用工具 <span class="tname">${fmt.escape(tc.name || '?')}</span></summary>
        <div class="tbody">${fmt.escape(inputStr)}</div>
      </details>`;
  }
  for (const tr of n.tool_results || []) {
    const errTag = tr.is_error ? '<span class="terr">ERROR</span>' : '';
    body += `
      <details class="tool-block">
        <summary>工具结果 ${errTag}</summary>
        <div class="tbody">${fmt.escape(tr.text || '')}</div>
      </details>`;
  }

  el.innerHTML = `
    <div class="msg-header">
      <span class="msg-role ${roleClass}">${fmt.escape(roleText)}</span>
      <span class="msg-time">${fmt.ts(n.timestamp)}</span>
      ${n.model ? `<span class="msg-model">${fmt.escape(n.model)}</span>` : ''}
      <span class="msg-flags">${flags.join('')}</span>
    </div>
    ${body}`;
  return el;
}

// ---------------------------------------------------------------- 初始化
const recycleButton = $('#btn-recycle-settings');
if (recycleButton) {
  recycleButton.innerHTML = trashIcon;
  recycleButton.title = '回收站';
  recycleButton.setAttribute('aria-label', '回收站');
}
const rollbackButton = $('#btn-rollback');
if (rollbackButton) {
  rollbackButton.innerHTML = rollbackIcon;
  rollbackButton.title = '回档';
  rollbackButton.setAttribute('aria-label', '回档');
}

$('#session-filter').addEventListener('input', () => {
  if (state.globalSearch.enabled) scheduleGlobalSearch();
  else renderSessions();
});

$('#global-search-toggle')?.addEventListener('click', () => {
  setGlobalSearchEnabled(!state.globalSearch.enabled);
});

$('#btn-recycle-settings')?.addEventListener('click', () => {
  const list = $('#recycle-list');
  if (list) list.innerHTML = '<div class="recycle-empty">加载中...</div>';
  openModal($('#recycle-dialog'));
  loadRecycleSettings();
});

$('#btn-rollback')?.addEventListener('click', () => {
  const list = $('#rollback-list');
  if (list) list.innerHTML = '<div class="recycle-empty">加载中...</div>';
  openModal($('#rollback-dialog'));
  loadRollbackStatus();
});

$('#recycle-save')?.addEventListener('click', async (ev) => {
  ev.preventDefault();
  try {
    await saveRecycleSettings();
    $('#recycle-dialog')?.close();
  } catch (err) {
    alert(err.message || '保存失败');
  }
});

$('#delete-confirm')?.addEventListener('click', (ev) => {
  ev.preventDefault();
  const session = state.pendingDeleteSession;
  state.deleteInFlight = true;
  $('#delete-dialog')?.close();
  state.pendingDeleteSession = session;
  confirmDeleteSession().catch(err => {
    alert(err.message || '删除失败');
  }).finally(() => {
    state.deleteInFlight = false;
    state.pendingDeleteSession = null;
  });
});

$('#delete-dialog')?.addEventListener('close', () => {
  if (!state.deleteInFlight) state.pendingDeleteSession = null;
});

$('#btn-refresh').addEventListener('click', async () => {
  await loadProjects();
  if (state.currentProject) await loadSessions(state.currentProject);
  if (state.currentProject && state.currentSession) {
    await loadSessionDetail(state.currentProject, state.currentSession, state.selectedBranch);
  }
});

loadRecycleSettings();
loadRollbackStatus();
loadConfig();
loadProjects().then(() => {
  if (state.projects.length) selectProject(state.projects[0].project_id);
});
