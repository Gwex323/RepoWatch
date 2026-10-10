import { StrictMode, useCallback, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import {
  Activity, ArrowLeft, ArrowRight, Bell, BookOpen, Check, CheckCircle2,
  ChevronRight, Circle, CircleHelp, Clock3, Code2, ExternalLink, FileCode2,
  FolderGit2, GitBranch, GitPullRequest, LayoutDashboard, Loader2, LockKeyhole,
  LogOut, Menu, Play, Plus, RefreshCw, Search, Settings2, ShieldCheck,
  Terminal, Workflow, X, XCircle,
} from 'lucide-react';
import './styles.css';
import { MemoryReads, MemoryView, type MemoryRead } from './memory';

type View = 'overview' | 'repositories' | 'findings' | 'memory' | 'activity' | 'settings';
type Session = { authenticated: boolean; setup_required: boolean; csrf: string | null };
type Repo = { id: string; name: string; url: string | null; kind: string; language: string };
type Observation = {
  input: { total: number; page: number; size: number };
  expected: number[]; actual: number[]; reproduced: boolean; revision: string; at: number;
};
type Event = { id: number; run_id: string; kind: string; agent: string; message: string; created: number };
type Artifact = { kind: string; content: Record<string, unknown>; created: number };
type Run = {
  id: string; repo_id: string; repository: string; status: string; agent: string;
  revision: string; created: number; updated: number; title: string;
  has_snapshot: boolean; error: string | null; note: string;
  observation: Observation | null;
  source_kind: 'discovered' | 'existing_issue'; issue_ref: string | null;
  automatic_validation: { decision: string; at: number; observation: Observation } | null;
  confirmation: { decision: string; at: number } | null;
  events?: Event[]; artifacts?: Artifact[];
  memory_reads?: MemoryRead[];
};
type Dashboard = { repositories: Repo[]; runs: Run[]; events: Event[] };
type System = { sdk_version: string; platform: string; roles: string[]; max_turns: number; max_attempts: number };
const roles = [
  ['repository_analyst', '仓库分析', FolderGit2],
  ['problem_investigator', '问题发现', Search],
  ['reproduction_assistant', '复现辅助', Terminal],
  ['repair_agent', '代码修复', Code2],
  ['independent_reviewer', '独立审查', ShieldCheck],
  ['pr_author', 'PR 整理', GitPullRequest],
] as const;
const roleName = (name: string) => roles.find(r => r[0] === name)?.[1] ?? (name === 'owner' ? '你' : name);
const labels: Record<string, string> = {
  queued: '排队中', running: '执行中', waiting: '待人工复现', confirmed: '等待恢复',
  not_reproduced: '未能复现', demo_completed: '样例已完成', failed: '执行失败',
  validation_blocked: '自动验证暂停',
};
const time = (value: number) => new Date(value * 1000).toLocaleString('zh-CN', {
  month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  timeZone: 'Asia/Shanghai',
});
let csrf: string | null = null;

async function api<T>(path: string, body?: unknown): Promise<T> {
  const result = await fetch(`/api${path}`, {
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? {} : {
      'Content-Type': 'application/json', 'X-CSRF-Token': csrf ?? '',
    },
    credentials: 'same-origin',
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await result.json();
  if (!result.ok) {
    const message = typeof data.detail === 'string' ? data.detail : '提交内容不符合要求';
    throw new Error(message);
  }
  return data as T;
}

function Status({ status }: { status: string }) {
  return <span className={`status status-${status}`}><span />{labels[status] ?? status}</span>;
}

function Empty({ icon: Icon = FolderGit2, title }: { icon?: typeof FolderGit2; title: string }) {
  return <div className="empty"><Icon size={30} strokeWidth={1.4} /><p>{title}</p></div>;
}

function Login({ session, onLogin }: { session: Session; onLogin: (s: Session) => void }) {
  const [password, setPassword] = useState('');
  const [repeat, setRepeat] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  return <div className="login-page">
    <div className="login-brand"><Workflow size={28} /><strong>RepoWatch</strong></div>
    <form className="login-form" onSubmit={async e => {
      e.preventDefault();
      if (session.setup_required && repeat !== password) { setError('两次输入的密码不一致'); return; }
      setBusy(true); setError('');
      try { onLogin(await api<Session>(session.setup_required ? '/setup' : '/login', { password })); }
      catch (e) { setError((e as Error).message); }
      finally { setBusy(false); }
    }}>
      <div className="login-lock"><LockKeyhole size={24} /></div>
      <span className="eyebrow">LOCAL WORKSPACE</span>
      <h1>{session.setup_required ? '创建本地工作空间' : '欢迎回来'}</h1>
      <label>管理密码<input type="password" autoComplete={session.setup_required ? 'new-password' : 'current-password'}
        minLength={10} maxLength={128} required value={password} onChange={e => setPassword(e.target.value)}
        placeholder="至少 10 个字符" /></label>
      {session.setup_required && <label>确认密码<input type="password" autoComplete="new-password"
        minLength={10} required value={repeat} onChange={e => setRepeat(e.target.value)} /></label>}
      {error && <div className="error-inline" role="alert">{error}</div>}
      <button className="button primary login-submit" disabled={busy}>
        {busy ? <Loader2 size={16} className="spin" /> : <ArrowRight size={16} />}
        {session.setup_required ? '创建并进入' : '进入工作空间'}
      </button>
      <div className="login-meta"><span className="live-dot" />本地实例<span>阶段 01</span></div>
    </form>
    <div className="login-footer">RepoWatch · Agents SDK workbench</div>
  </div>;
}

function AgentRail({ run }: { run?: Run }) {
  const activeIndex = run ? roles.findIndex(r => r[0] === run.agent) : -1;
  return <div className="agent-rail">
    {roles.map(([key, name, Icon], index) => {
      const complete = run?.status === 'demo_completed' || index < activeIndex;
      const active = index === activeIndex && !complete;
      return <div key={key} className={`agent-step ${complete ? 'done' : ''} ${active ? 'current' : ''}`}>
        <div className="agent-circle">{complete ? <Check size={16} /> : <Icon size={17} />}</div>
        <span>{name}</span>
        <small>{complete ? '已交接' : active ? (run?.status === 'waiting' ? '等待你确认' : labels[run?.status ?? '']) : '待执行'}</small>
      </div>;
    })}
  </div>;
}

function EventFeed({ events, onSelect }: { events: Event[]; onSelect?: (id: string) => void }) {
  if (!events.length) return <Empty icon={Activity} title="暂无运行记录" />;
  return <div className="event-feed">{events.map(event => <div className="event-row" key={event.id}>
    <div className={`event-icon ${event.kind}`}>
      {event.kind === 'handoff' ? <ArrowRight size={15} /> : event.kind === 'human' ? <CheckCircle2 size={15} /> :
        event.kind === 'checkpoint' ? <Clock3 size={15} /> : <Activity size={15} />}
    </div>
    <div className="event-copy">
      <strong>{event.kind === 'handoff' ? event.message.split(' -> ').map(roleName).join(' → ') :
        event.kind === 'checkpoint' ? '运行已暂停 · 等待人工复现' :
        event.kind === 'human' ? `你记录了${event.message === 'confirmed' ? '复现成功' : '未能复现'}` :
        event.kind === 'resume' ? '从已保存的 SDK 状态恢复' :
        event.kind === 'complete' ? '样例流程完成 · 草稿未发布' :
        event.kind === 'validation' ? '已有 Issue 样例 · 自动验证通过' :
        event.kind === 'validation_blocked' ? '自动验证暂停 · 等待重试' :
        event.kind === 'evidence' ? '样例证据已记录' : `${roleName(event.agent)}开始执行`}</strong>
      <button className="text-link mono" onClick={() => onSelect?.(event.run_id)} disabled={!onSelect}>
        {event.run_id.slice(0, 16)}
      </button>
    </div>
    <time>{time(event.created)}</time>
  </div>)}</div>;
}

function FindingDetail({ run, busy, onAction, onBack }: {
  run: Run; busy: boolean; onAction: (fn: () => Promise<unknown>) => Promise<void>; onBack: () => void;
}) {
  const [tab, setTab] = useState('reproduce');
  const [total, setTotal] = useState(20);
  const [page, setPage] = useState(3);
  const [size, setSize] = useState(10);
  const [note, setNote] = useState('');
  const [checked, setChecked] = useState(false);
  const observation = run.observation;
  const existingIssue = run.source_kind === 'existing_issue';
  const automatic = run.automatic_validation;
  const pending = run.status === 'waiting';
  const confirm = (decision: 'confirmed' | 'not_reproduced') => onAction(() =>
    api(`/runs/${run.id}/confirmation`, {
      decision, revision: run.revision, personally_reproduced: decision === 'confirmed' && checked, note,
    }));
  return <section className="finding-detail">
    <button className="text-link mobile-back" onClick={onBack}><ArrowLeft size={15} />返回问题列表</button>
    <div className="detail-heading">
      <div className="detail-meta"><span className="severity">P2</span><span className="tag">受控样例</span>
        <span className="tag neutral">{existingIssue ? '已有 Issue' : '系统发现'}</span><Status status={run.status} /></div>
      <h2>越界分页返回了上一页数据</h2>
      <div className="detail-repo"><FolderGit2 size={14} />{run.repository}<span className="divider-dot">·</span>
        <GitBranch size={14} /><code title={run.revision}>{run.revision.slice(0, 8)}</code></div>
    </div>
    <div className="tabs" role="tablist">
      {[['reproduce', existingIssue ? '自动验证' : '复现与确认'], ['handoffs', '交接记录'], ['artifacts', '本地草稿'], ['memory', '记忆引用']].map(([key, name]) =>
        <button key={key} role="tab" aria-selected={tab === key} className={tab === key ? 'active' : ''}
          onClick={() => setTab(key)}>{name}{key === 'artifacts' && !!run.artifacts?.length && <span>{run.artifacts.length}</span>}</button>)}
    </div>
    {run.error && <div className="error-banner"><XCircle size={18} /><span>
      {run.status === 'validation_blocked' ? '自动验证未通过，任务已暂停。无需人工复现确认。' : run.error}</span>
      <button className="icon-button" title="重试任务" disabled={busy}
        onClick={() => onAction(() => api(`/runs/${run.id}/retry`, {}))}><RefreshCw size={16} /></button></div>}
    {tab === 'reproduce' && <div className="detail-body">
      <div className="section-label"><span className="step-number">01</span><h3>问题证据</h3><span className="muted">page_window.py</span></div>
      <div className="evidence-facts">
        <div><span>触发条件</span><p>请求页码超出最后一页</p></div>
        <div><span>预期行为</span><p>返回空列表 <code>[]</code></p></div>
        <div><span>已观察行为</span><p>返回上一页的 10 条数据</p></div>
      </div>
      <pre className="code-panel"><span className="code-line"><em>01</em>start = (page - 1) * size</span>
        <span className="code-line highlighted"><em>02</em>if start &gt;= len(items) and items:</span>
        <span className="code-line highlighted"><em>03</em>    start = max(0, len(items) - size)</span>
        <span className="code-line"><em>04</em>return items[start : start + size]</span></pre>
      {existingIssue ? <>
        <div className="section-label"><span className="step-number">02</span><h3>已有 Issue 自动验证</h3></div>
        <dl className="settings-list">
          <div><dt>来源</dt><dd className="issue-reference">{run.issue_ref}</dd></div>
          <div><dt>验证状态</dt><dd>{automatic ? (automatic.decision === 'verified' ? '自动验证通过' : '自动验证未通过') : '待执行'}</dd></div>
          <div><dt>人工复现</dt><dd>不需要</dd></div>
        </dl>
        {automatic && <div className="result-grid">
          <div><span>预期返回</span><code>{JSON.stringify(automatic.observation.expected)}</code></div>
          <div className={automatic.observation.reproduced ? 'mismatch' : ''}><span>实际返回</span>
            <code>{JSON.stringify(automatic.observation.actual)}</code></div>
        </div>}
      </> : <>
      <div className="section-label"><span className="step-number">02</span><h3>本地样例复现</h3><span className="tag neutral">内置函数</span></div>
      <form className="experiment" onSubmit={e => {
        e.preventDefault(); setChecked(false);
        void onAction(() => api(`/runs/${run.id}/experiment`, { total, page, size }));
      }}>
        <div className="experiment-inputs">
          <label>总条数<input aria-label="总条数" type="number" min={0} max={10000} required value={total}
            disabled={!pending} onChange={e => setTotal(Number(e.target.value))} /></label>
          <label>页码<input aria-label="页码" type="number" min={1} max={1000} required value={page}
            disabled={!pending} onChange={e => setPage(Number(e.target.value))} /></label>
          <label>每页条数<input aria-label="每页条数" type="number" min={1} max={100} required value={size}
            disabled={!pending} onChange={e => setSize(Number(e.target.value))} /></label>
          <button className="button secondary" disabled={!pending || busy}><Play size={15} />执行复现</button>
        </div>
        <div className="result-grid">
          <div><span>预期返回</span><code>{observation ? JSON.stringify(observation.expected) : '—'}</code></div>
          <div className={observation?.reproduced ? 'mismatch' : ''}><span>实际返回</span>
            <code>{observation ? JSON.stringify(observation.actual) : '—'}</code></div>
        </div>
        {observation && <div className={`observation ${observation.reproduced ? 'found' : 'matched'}`}>
          {observation.reproduced ? <XCircle size={15} /> : <CheckCircle2 size={15} />}
          {observation.reproduced ? '观察到预期与实际结果不一致' : '本次输入未复现问题'}
          <time>{time(observation.at)}</time>
        </div>}
      </form>
      <div className="section-label"><span className="step-number">03</span><h3>人工确认</h3>
        <LockKeyhole size={14} className="muted" /></div>
      {pending ? <div className="confirmation">
        <label className="checkbox-label"><input type="checkbox" checked={checked}
          disabled={!observation?.reproduced} onChange={e => setChecked(e.target.checked)} />
          我已亲自操作并确认该问题能够复现</label>
        <textarea aria-label="复现备注" placeholder="复现备注（可选）" maxLength={2000}
          value={note} onChange={e => setNote(e.target.value)} rows={3} />
        <div className="confirmation-actions">
          <button className="button secondary" disabled={busy} onClick={() => confirm('not_reproduced')}>
            <X size={15} />未能复现</button>
          <button className="button primary" disabled={!checked || !observation?.reproduced || busy}
            onClick={() => confirm('confirmed')}><Check size={16} />确认已复现并继续</button>
        </div>
      </div> : run.confirmation ? <div className="saved-confirmation">
        <CheckCircle2 size={20} /><div><strong>{run.confirmation.decision === 'confirmed' ? '复现成功已记录' : '未能复现已记录'}</strong>
          <p>{time(run.confirmation.at)} · 本地所有者</p>{run.note && <p>{run.note}</p>}</div>
      </div> : <div className="pending-placeholder"><Clock3 size={17} />{labels[run.status]}</div>}
      </>}
    </div>}
    {tab === 'handoffs' && <div className="detail-body"><AgentRail run={run} />
      <div className="section-label"><h3>SDK 执行记录</h3><span className="tag neutral">持久化</span></div>
      <EventFeed events={run.events ?? []} /></div>}
    {tab === 'memory' && <div className="detail-body">
      <MemoryReads reads={run.memory_reads ?? []} roleName={roleName} /></div>}
    {tab === 'artifacts' && <div className="detail-body">
      {!run.artifacts?.length ? <Empty icon={FileCode2} title="暂无草稿" /> :
        run.artifacts.map(artifact => <section className="artifact" key={artifact.kind}>
          <div className="section-label"><FileCode2 size={17} /><h3>{({
            issue_draft: 'Issue 草稿', patch_draft: '样例补丁', verification: '样例验证结果', pr_draft: 'PR 草稿',
          } as Record<string, string>)[artifact.kind]}</h3><span className="tag neutral">仅本地</span></div>
          {artifact.kind === 'patch_draft' ? <pre className="diff">{String(artifact.content.diff).split('\n').map((line, i) =>
            <span key={i} className={line.startsWith('+') ? 'addition' : line.startsWith('-') ? 'deletion' : ''}>{line}{'\n'}</span>)}</pre> :
            artifact.kind === 'verification' ? <div className="verification"><CheckCircle2 size={20} />
              <strong>{String(artifact.content.passed)} 项样例断言通过</strong><span>仅限内置函数</span></div> :
              <div className="draft-content"><strong>{String(artifact.content.title)}</strong><p>{String(artifact.content.body)}</p>
                <span className="status status-queued"><span />未发布至 GitHub</span></div>}
        </section>)}
    </div>}
  </section>;
}

function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [data, setData] = useState<Dashboard>({ repositories: [], runs: [], events: [] });
  const [system, setSystem] = useState<System | null>(null);
  const [view, setView] = useState<View>('overview');
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<Run | null>(null);
  const [filter, setFilter] = useState('all');
  const [query, setQuery] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState(false);
  const [url, setUrl] = useState('');
  const [menuOpen, setMenuOpen] = useState(false);
  const [loaded, setLoaded] = useState(false);
  useEffect(() => { void api<Session>('/session').then(s => { csrf = s.csrf; setSession(s); })
    .catch(e => setError(e.message)); }, []);
  const refresh = useCallback(async () => {
    const [dashboard, config] = await Promise.all([api<Dashboard>('/dashboard'), api<System>('/system')]);
    setData(dashboard); setSystem(config); setLoaded(true);
    if (selected) setDetail(await api<Run>(`/runs/${selected}`));
  }, [selected]);
  useEffect(() => {
    if (!session?.authenticated) return;
    let active = true;
    const update = () => { if (active) void refresh().catch(e => setError(e.message)); };
    update();
    const timer = setInterval(update, 2000);
    return () => { active = false; clearInterval(timer); };
  }, [session?.authenticated, refresh]);
  const action = async (fn: () => Promise<unknown>) => {
    setBusy(true); setError('');
    try { await fn(); await refresh(); } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };
  const selectRun = (id: string) => { setDetail(null); setSelected(id); setView('findings'); setMenuOpen(false); };
  const start = (repoId = 'repo_fixture') => action(async () => {
    const result = await api<{ run_id: string }>(`/repositories/${repoId}/runs`, {});
    selectRun(result.run_id);
  });
  const startIssue = () => action(async () => {
    const result = await api<{ run_id: string }>('/samples/existing-issue/runs', {});
    selectRun(result.run_id);
  });
  const navigate = (next: View) => { setView(next); setMenuOpen(false); };
  const waiting = data.runs.filter(r => r.status === 'waiting');
  const attention = data.runs.filter(r => ['waiting', 'validation_blocked', 'failed'].includes(r.status));
  const activeRuns = data.runs.filter(r => ['running', 'queued', 'confirmed'].includes(r.status));
  const matchingRuns = data.runs.filter(r =>
    `${r.repository} ${r.title} ${r.id}`.toLowerCase().includes(query.toLowerCase()));
  const filteredRuns = matchingRuns.filter(r => filter === 'all' ||
    (filter === 'attention' ? ['waiting', 'validation_blocked', 'failed'].includes(r.status) : r.status === filter));
  const nav = [
    ['overview', '工作台', LayoutDashboard],
    ['repositories', '仓库', FolderGit2],
    ['findings', '问题与复现', CircleHelp],
    ['memory', '仓库记忆', BookOpen],
    ['activity', '运行记录', Activity],
    ['settings', '运行配置', Settings2],
  ] as const;
  if (!session) return <div className="boot"><Workflow size={30} /><span>{error || '连接本地工作空间…'}</span>
    {error && <button className="button secondary" onClick={() => location.reload()}>重新连接</button>}</div>;
  if (!session.authenticated) return <Login session={session} onLogin={s => { csrf = s.csrf; setSession(s); }} />;
  return <div className="app-shell">
    {menuOpen && <button className="mobile-overlay" aria-label="关闭导航" onClick={() => setMenuOpen(false)} />}
    <aside className={`sidebar ${menuOpen ? 'open' : ''}`}>
      <div className="brand"><span className="brand-mark"><Workflow size={24} /></span><strong>RepoWatch</strong></div>
      <div className="workspace"><span className="workspace-avatar">L</span><div><strong>本地工作空间</strong><small>个人 · {system?.platform ?? '本机'}</small></div>
        <LockKeyhole size={13} /></div>
      <div className="nav-label">工作空间</div>
      <nav>{nav.map(([key, name, Icon]) => <button key={key} className={view === key ? 'active' : ''}
        onClick={() => navigate(key)}><Icon size={18} /><span>{name}</span>
        {key === 'findings' && attention.length > 0 && <b>{attention.length}</b>}</button>)}</nav>
      <div className="sidebar-bottom">
        <div className="environment-state"><span className="live-dot" /><span>本地服务在线</span></div>
        <div className="sidebar-version">阶段 01 <span>SDK {system?.sdk_version ?? '—'}</span></div>
        <div className="owner"><span className="owner-avatar">你</span><div><strong>本地所有者</strong><small>单用户工作空间</small></div>
          <button className="icon-button" title="退出登录" onClick={async () => {
            try {
              await api('/logout', {}); csrf = null; setError('');
              setSession({ authenticated: false, setup_required: false, csrf: null });
            } catch (e) { setError((e as Error).message); }
          }}><LogOut size={17} /></button></div>
      </div>
    </aside>
    <div className="workspace-main">
      <header className="topbar">
        <button className="icon-button menu-toggle" title="打开导航" onClick={() => setMenuOpen(true)}><Menu size={19} /></button>
        <div className="breadcrumb">工作空间<ChevronRight size={13} /><strong>{nav.find(n => n[0] === view)?.[1]}</strong></div>
        <label className="global-search"><Search size={16} /><input aria-label="搜索仓库或问题" placeholder="搜索仓库或问题"
          value={query} onChange={e => setQuery(e.target.value)} /></label>
        <button className="icon-button notifications" title={`${attention.length} 个问题待处理`} onClick={() => {
          setSelected(null); setDetail(null); setFilter('attention'); navigate('findings');
        }}><Bell size={18} />{attention.length > 0 && <i />}</button>
        <div className="top-avatar">L</div>
      </header>
      {error && <div className="global-error" role="alert"><XCircle size={17} /><span>{error}</span>
        <button className="icon-button" title="关闭错误提示" onClick={() => setError('')}><X size={16} /></button></div>}
      <main className={`main-content ${view === 'findings' ? 'findings-page' : ''}`}>
        <div className="page-heading">
          <div><div className="eyebrow">{view === 'overview' ? 'OVERVIEW' : view.toUpperCase()}</div>
            <h1>{nav.find(n => n[0] === view)?.[1]}{view === 'findings' && <span className="heading-count">{data.runs.length}</span>}</h1>
          </div>
          <div className="heading-actions">
            <span className="mode-label"><span />受控样例模式</span>
            <button className="button secondary" onClick={() => setAdding(true)}><Plus size={16} />添加仓库</button>
            <button className="button secondary" disabled={busy} onClick={startIssue}>
              <Play size={15} />已有 Issue 样例</button>
            <button className="button primary" disabled={busy} onClick={() => start()}>
              {busy ? <Loader2 size={16} className="spin" /> : <Play size={15} />}运行样例</button>
          </div>
        </div>
        {!loaded ? <div className="loading"><Loader2 className="spin" size={22} />正在载入工作空间</div> : <>
          {view === 'overview' && <>
            <div className="metrics">
              {[
                ['待人工复现', waiting.length, 'waiting', Clock3],
                ['运行中', activeRuns.length, 'running', Workflow],
                ['已完成样例', data.runs.filter(r => r.status === 'demo_completed').length, 'done', CheckCircle2],
                ['已登记仓库', data.repositories.length, 'repos', FolderGit2],
              ].map(([label, count, cls, Icon]) => {
                const MetricIcon = Icon as typeof Clock3;
                return <div className={`metric ${cls}`} key={String(label)}>
                  <div className="metric-label">{String(label)}<MetricIcon size={17} /></div>
                  <strong>{String(count)}</strong><span>{cls === 'repos' ? '含本地受控样例' :
                    cls === 'waiting' ? '等待你的复现结果' : cls === 'running' ? '当前执行队列' : '仅本地草稿'}</span>
                </div>;
              })}
            </div>
            <section className="pipeline-section">
              <div className="section-heading"><h2><Workflow size={18} />Agent 流程</h2>
                <span className="section-meta">6 个角色<span className="divider-dot">·</span>原生 handoff</span></div>
              <AgentRail run={data.runs[0]} />
            </section>
            <div className="overview-columns">
              <div>
                <section>
                  <div className="section-heading"><h2><Clock3 size={18} />待人工复现<span className="small-count">{waiting.length}</span></h2>
                    <button className="text-link" onClick={() => { setFilter('waiting'); navigate('findings'); }}>全部问题<ArrowRight size={14} /></button></div>
                  {waiting.length ? <div className="waiting-list">{waiting.slice(0, 4).map(run => <button className="waiting-item" key={run.id} onClick={() => selectRun(run.id)}>
                    <span className="severity">P2</span><div><strong>越界分页返回了上一页数据</strong>
                      <span>{run.repository}<i>·</i>{time(run.created)}</span></div><ChevronRight size={17} /></button>)}</div> :
                    <Empty icon={CheckCircle2} title="暂无待确认问题" />}
                </section>
                <section className="recent-section">
                  <div className="section-heading"><h2><FolderGit2 size={18} />仓库</h2>
                    <button className="text-link" onClick={() => navigate('repositories')}>查看全部<ArrowRight size={14} /></button></div>
                  <div className="repository-table">{data.repositories.slice(0, 4).map(repo => <div className="repository-row" key={repo.id}>
                    <span className={`repo-icon ${repo.kind}`}><FolderGit2 size={20} /></span><div><strong>{repo.name}</strong>
                      <span><i className={`language-dot ${repo.language}`} />{repo.language === 'Unknown' ? '待识别' : repo.language}
                        <i>·</i>{repo.kind === 'fixture' ? '本地样例' : 'GitHub'}</span></div>
                    <span className={`tag ${repo.kind === 'fixture' ? 'green' : 'neutral'}`}>{repo.kind === 'fixture' ? '可运行' : '仅登记'}</span>
                    <button className="icon-button" title={repo.kind === 'fixture' ? '运行样例' : '外部仓库执行尚未接入'}
                      disabled={repo.kind !== 'fixture' || busy} onClick={() => start(repo.id)}><Play size={15} /></button>
                  </div>)}</div>
                </section>
              </div>
              <aside className="overview-aside">
                <section><div className="section-heading"><h2><ShieldCheck size={18} />运行边界</h2></div>
                  <dl className="boundary-list"><div><dt>系统发现</dt><dd className="green-text"><LockKeyhole size={13} />人工复现</dd></div>
                    <div><dt>已有 Issue</dt><dd>自动验证</dd></div>
                    <div><dt>GitHub 发布</dt><dd>未启用</dd></div><div><dt>自动合并</dt><dd className="red-text">禁止</dd></div>
                    <div><dt>外部模型调用</dt><dd>关闭</dd></div><div><dt>每日调度</dt><dd>未启用</dd></div></dl>
                </section>
                <section className="recent-section"><div className="section-heading"><h2><Activity size={18} />最近活动</h2></div>
                  <EventFeed events={data.events.slice(0, 5)} onSelect={selectRun} /></section>
              </aside>
            </div>
          </>}
          {view === 'repositories' && <section className="repo-page">
            <div className="list-topline"><span>{data.repositories.length} 个仓库</span><span>登记状态</span></div>
            {data.repositories.filter(r => r.name.toLowerCase().includes(query.toLowerCase())).map(repo =>
              <div className="repository-row large" key={repo.id}><span className={`repo-icon ${repo.kind}`}><FolderGit2 size={23} /></span>
                <div><strong>{repo.name}</strong><span>{repo.url ?? 'fixture://page-window'}<i>·</i>{repo.language}</span></div>
                <span className={`tag ${repo.kind === 'fixture' ? 'green' : 'neutral'}`}>{repo.kind === 'fixture' ? '受控样例' : '执行未接入'}</span>
                {repo.url && <a className="icon-button" title="在 GitHub 打开" href={repo.url} target="_blank" rel="noreferrer"><ExternalLink size={16} /></a>}
                <button className="icon-button" title={repo.kind === 'fixture' ? '运行样例' : '外部仓库执行尚未接入'}
                  disabled={repo.kind !== 'fixture' || busy} onClick={() => start(repo.id)}><Play size={17} /></button>
              </div>)}
          </section>}
          {view === 'findings' && <div className={`findings-layout ${selected ? 'has-selection' : ''}`}>
            <aside className="findings-list"><div className="segmented">
              <button className={filter === 'all' ? 'active' : ''} onClick={() => setFilter('all')}>全部</button>
              <button className={filter === 'waiting' ? 'active' : ''} onClick={() => setFilter('waiting')}>待复现 <span>{waiting.length}</span></button>
              <button className={filter === 'attention' ? 'active' : ''} onClick={() => setFilter('attention')}>待处理</button>
            </div>
              {!filteredRuns.length ? <Empty icon={Search} title="暂无匹配的问题" /> : filteredRuns.map(run =>
                <button className={`finding-item ${selected === run.id ? 'selected' : ''}`} key={run.id} onClick={() => selectRun(run.id)}>
                  <div><span className="severity">P2</span><Status status={run.status} /></div><strong>越界分页返回了上一页数据</strong>
                  <span className="finding-repo">{run.repository}</span><small>{time(run.created)}
                    <span>{run.source_kind === 'existing_issue' ? '已有 Issue 样例' : '系统发现样例'}</span></small>
                </button>)}
            </aside>
            {detail && detail.id === selected ? <FindingDetail key={detail.id} run={detail} busy={busy} onAction={action}
              onBack={() => { setSelected(null); setDetail(null); }} /> : selected ? <div className="loading"><Loader2 size={20} className="spin" /></div> :
              <div className="no-selection"><BookOpen size={34} strokeWidth={1.3} /><h3>问题详情</h3><span>{filteredRuns.length} 条记录</span></div>}
          </div>}
          {view === 'activity' && <section className="activity-page">
            <div className="section-heading"><h2>执行时间线</h2><span className="section-meta">最近 {data.events.length} 条</span></div>
            <EventFeed events={data.events.filter(e => !query || `${e.message} ${e.run_id}`.includes(query))} onSelect={selectRun} /></section>}
          {view === 'memory' && <MemoryView repositories={data.repositories} api={api} onRun={selectRun}
            revisions={Object.fromEntries([...data.runs].reverse().map(run => [run.repo_id, run.revision]))} />}
          {view === 'settings' && <section className="settings-page">
            <div className="section-heading"><h2>运行环境</h2><span className="tag neutral">阶段 01</span></div>
            <dl className="settings-list">
              {[['Agent 框架', `OpenAI Agents SDK ${system?.sdk_version}`], ['后端', 'Python · FastAPI'],
                ['状态存储', 'SQLite'], ['仓库记忆', '共享 · 仓库与版本隔离'],
                ['模型模式', '受控响应 · 不调用外部模型'], ['GitHub 写入', '禁用'],
                ['系统发现问题', '人工复现确认'], ['已有 Issue', '自动验证（仅样例）'],
                ['真实 Issue 接入', '未接入'],
                ['外部仓库执行', '未接入'], ['每日检查', '未启用'], ['自动合并', '禁止'],
                ['每次任务最大轮数', String(system?.max_turns)], ['任务最大尝试次数', String(system?.max_attempts)],
                ['外部 Trace 上传', '关闭'], ['访问范围', '本机 · 所有者登录'],
              ].map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}
            </dl>
            <div className="section-heading role-config-heading"><h2>Agent 角色</h2><span className="section-meta">6 个已注册角色</span></div>
            <div className="roles-list">{roles.map(([key, name, Icon]) => <div key={key}><Icon size={19} /><strong>{name}</strong>
              <code>{key}</code><span className="tag neutral">受控模型</span></div>)}</div>
          </section>}
        </>}
        <footer className="page-footer"><span><ShieldCheck size={13} />所有合并由项目拥有者执行</span><span>RepoWatch / 0.1.0</span></footer>
      </main>
    </div>
    {adding && <div className="modal-backdrop" onClick={e => { if (e.target === e.currentTarget) setAdding(false); }}>
      <form className="modal" role="dialog" aria-modal="true" aria-labelledby="add-title" onKeyDown={e => {
        if (e.key === 'Escape') { setAdding(false); return; }
        if (e.key !== 'Tab') return;
        const controls = Array.from(e.currentTarget.querySelectorAll<HTMLElement>(
          'button:not(:disabled), input:not(:disabled), textarea:not(:disabled), a[href]',
        ));
        const first = controls[0], last = controls[controls.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus(); }
        if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus(); }
      }} onSubmit={e => {
        e.preventDefault(); void action(async () => { await api('/repositories', { url }); setAdding(false); setUrl(''); setView('repositories'); });
      }}>
        <div className="modal-heading"><h2 id="add-title">添加仓库</h2><button type="button" className="icon-button" title="关闭"
          onClick={() => setAdding(false)}><X size={18} /></button></div>
        <label>GitHub 仓库链接<input autoFocus type="url" required placeholder="https://github.com/owner/repository"
          value={url} onChange={e => setUrl(e.target.value)} /></label>
        <div className="modal-state"><Circle size={12} />初始状态：仅登记</div>
        {error && <div className="error-inline" role="alert">{error}</div>}
        <div className="modal-actions"><button type="button" className="button secondary" onClick={() => setAdding(false)}>取消</button>
          <button className="button primary" disabled={busy}><Plus size={15} />添加仓库</button></div>
      </form>
    </div>}
  </div>;
}

createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>);
