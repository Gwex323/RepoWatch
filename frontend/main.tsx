import { StrictMode, useCallback, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import {
  Activity, ArrowRight, Bell, BookOpen, CheckCircle2,
  ChevronRight, Circle, CircleHelp, Clock3, ExternalLink, FileCode2,
  FolderGit2, LayoutDashboard, Loader2, LockKeyhole,
  LogOut, Menu, Play, Plus, Search, Settings2, ShieldCheck,
  Workflow, X, XCircle,
} from 'lucide-react';
import './styles.css';
import { MemoryView } from './memory';
import { GitHubAccounts } from './github';
import { ExecutionPanel } from './execution';
import { AgentModelSettings, AgentRoleCards } from './agent-models';
import type { AgentModels } from './agent-models';
import { RepairSettings } from './repair';

type View = 'overview' | 'repositories' | 'findings' | 'memory' | 'activity' | 'settings';
type Session = { authenticated: boolean; setup_required: boolean; csrf: string | null };
type Repo = { id: string; name: string; url: string | null; kind: string; language: string };
type Event = {
  id: string; repo_id: string; repository: string; task_id: string; env_id: string;
  source: 'analysis' | 'repair' | 'execution'; kind: string; detail: string; created: number;
};
type Finding = {
  id: string; repo_id: string; repository: string; analysis_id: string; env_id: string;
  title: string; severity: string; status: string; revision: string;
  repair_status: string | null; created: number; updated: number;
};
type Dashboard = {
  repositories: Repo[]; findings: Finding[]; events: Event[];
  counts: { waiting: number; active: number; published_prs: number };
};
type AnalysisNotice = {
  repo_id: string; repository: string; status: string; waiting: number;
  repairs_enabled?: boolean; repair_queued?: number; repair_running?: number;
  repair_failed?: number; repair_published?: number;
};
type System = {
  sdk_version: string; platform: string; roles: string[]; agent_models?: AgentModels; max_turns: number; max_attempts: number;
  external_model_calls: boolean; execution_diagnosis?: { enabled: boolean; model: string | null };
  repository_analysis?: { enabled: boolean; model: string };
  automatic_repair?: { enabled: boolean; model_enabled?: boolean };
  schedule?: { enabled: boolean; enabled_repositories: number };
};
type ScheduleOverview = {
  repo_id: string; repository: string; kind: string; enabled: boolean;
  local_time: string; last_run_date: string; last_status: string; last_detail: string;
};
const roleNames: Record<string, string> = {
  repository_analyst: '仓库分析', problem_investigator: '问题调查',
  reproduction_assistant: '复现辅助', repair_agent: '代码修复', independent_reviewer: '独立审查',
};
const roleName = (name: string) => roleNames[name] ?? (name === 'owner' ? '你' : name);
const labels: Record<string, string> = {
  waiting_reproduction: '待人工复现', confirmed: '已确认复现',
  not_reproduced: '未能复现', resolved: '已解决', queued: '排队中',
  running: '执行中', failed: '执行失败', interrupted: '已中断', pr_created: '已提交 PR',
};
const scheduleStatuses: Record<string, string> = {
  idle: '待调度', paused: '已暂停', scheduled: '已调度',
  skipped_active: '活动任务中', failed: '调度失败',
};
const scheduleDetail = (item: ScheduleOverview) => {
  if (item.last_status === 'failed') {
    const details: Record<string, string> = {
      github_active_account_required: '需要激活 GitHub 账号',
      schedule_execution_failed: '巡检执行失败',
    };
    return details[item.last_detail] ?? item.last_detail;
  }
  if (item.last_status === 'skipped_active') return '已有活动任务，跳过本次调度';
  if (item.last_status === 'scheduled') return '已创建运行环境';
  return item.last_run_date ? '调度已完成' : '尚未调度';
};
const scheduleStatusClass = (item: ScheduleOverview) =>
  item.last_status === 'failed' ? 'red' : item.enabled ? 'green' : 'neutral';
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
      <div className="login-meta"><span className="live-dot" />本地实例<span>阶段 03</span></div>
    </form>
    <div className="login-footer">RepoWatch · Agents SDK workbench</div>
  </div>;
}

function EventFeed({ events, onSelect }: { events: Event[]; onSelect: (id: string, envId?: string) => void }) {
  if (!events.length) return <Empty icon={Activity} title="暂无运行记录" />;
  const sources = { analysis: '仓库分析', repair: '自动修复', execution: '仓库执行' };
  return <div className="event-feed">{events.map(event => <div className="event-row" key={event.id}>
    <div className={`event-icon ${event.kind}`}><Activity size={15} /></div>
    <div className="event-copy">
      <strong>{sources[event.source]} · {labels[event.kind] ?? event.kind}</strong>
      <span>{event.detail.split(' -> ').map(roleName).join(' → ')}</span>
      <button className="text-link mono" onClick={() => onSelect(event.repo_id, event.env_id)}>
        {event.repository} · {event.task_id.slice(0, 16)}
      </button>
    </div><time>{time(event.created)}</time>
  </div>)}</div>;
}

function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [data, setData] = useState<Dashboard>({ repositories: [], findings: [], events: [], counts: { waiting: 0, active: 0, published_prs: 0 } });
  const [system, setSystem] = useState<System | null>(null);
  const [analysisNotices, setAnalysisNotices] = useState<AnalysisNotice[]>([]);
  const [scheduleOverview, setScheduleOverview] = useState<ScheduleOverview[]>([]);
  const [view, setView] = useState<View>('overview');
  const [executionRepo, setExecutionRepo] = useState<string | null>(null);
  const [executionEnv, setExecutionEnv] = useState('');
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
    const [dashboard, config, notices, schedules] = await Promise.all([
      api<Dashboard>('/dashboard'), api<System>('/system'),
      api<AnalysisNotice[]>('/analysis-notifications'),
      api<ScheduleOverview[]>('/schedules'),
    ]);
    setData(dashboard); setSystem(config); setLoaded(true);
    setAnalysisNotices(notices);
    setScheduleOverview(schedules);
  }, []);
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
  const openRepository = (id: string, envId = '') => {
    setExecutionEnv(envId); setExecutionRepo(id); setView('repositories'); setMenuOpen(false);
  };
  const navigate = (next: View) => { setView(next); setMenuOpen(false); };
  const waiting = data.findings.filter(f => f.status === 'waiting_reproduction');
  const needsAttention = (f: Finding) => f.status === 'waiting_reproduction' ||
    (f.status !== 'resolved' && ['failed', 'interrupted'].includes(f.repair_status ?? ''));
  const attention = data.findings.filter(needsAttention);
  const filteredFindings = data.findings.filter(f =>
    `${f.repository} ${f.title} ${f.id}`.toLowerCase().includes(query.toLowerCase()) &&
    (filter === 'all' || (filter === 'attention' ? needsAttention(f) : f.status === 'waiting_reproduction')));
  const registeredRoles = system?.roles ?? [];
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
        <div className="sidebar-version">阶段 03 <span>SDK {system?.sdk_version ?? '—'}</span></div>
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
          setFilter('attention'); navigate('findings');
        }}><Bell size={18} />{attention.length > 0 && <i />}</button>
        <div className="top-avatar">L</div>
      </header>
      {error && <div className="global-error" role="alert"><XCircle size={17} /><span>{error}</span>
        <button className="icon-button" title="关闭错误提示" onClick={() => setError('')}><X size={16} /></button></div>}
      <main className={`main-content ${view === 'findings' ? 'findings-page' : ''}`}>
        <div className="page-heading">
          <div><div className="eyebrow">{view === 'overview' ? 'OVERVIEW' : view.toUpperCase()}</div>
            <h1>{nav.find(n => n[0] === view)?.[1]}{view === 'findings' && <span className="heading-count">{data.findings.length}</span>}</h1>
          </div>
          <div className="heading-actions">
            <span className="mode-label"><span />{system?.repository_analysis?.enabled ? '仓库自动分析已启用' : '仓库分析待配置'}</span>
            <button className="button secondary" onClick={() => setAdding(true)}><Plus size={16} />添加仓库</button>
          </div>
        </div>
        {!loaded ? <div className="loading"><Loader2 className="spin" size={22} />正在载入工作空间</div> : <>
          {view !== 'settings' && analysisNotices.map(notice => <div className="analysis-notice" key={notice.repo_id}>
            <FileCode2 size={17} /><span><strong>{notice.repository}</strong> · {
              notice.status === 'waiting_model' ? '分析已排队，等待模型配置' :
              notice.status === 'running' ? 'Agent 正在自动分析' :
              notice.waiting > 0 ? `${notice.waiting} 个疑似问题待人工复现` :
              notice.repair_running ? `${notice.repair_running} 个问题正在自动修复` :
              notice.repair_failed ? `${notice.repair_failed} 个修复任务需要处理` :
              notice.repair_queued ? (notice.repairs_enabled ? '已确认问题等待自动修复' : '已确认问题等待启用自动修复') :
              notice.repair_published ? `${notice.repair_published} 个 PR 已提交，等待仓库拥有者审核` :
              notice.status === 'completed' ? '源码分析已完成' : '分析需要处理'
            }</span>
            <button className="text-link" onClick={() => {
              if (notice.status === 'waiting_model') navigate('settings');
              else { openRepository(notice.repo_id); }
            }}>{notice.status === 'waiting_model' ? '配置模型' :
              notice.repair_queued || notice.repair_running || notice.repair_failed || notice.repair_published
                ? '查看修复' : '查看分析'}<ArrowRight size={14} /></button>
          </div>)}
          {view === 'overview' && <>
            <div className="metrics">
              {[
                ['待人工复现', data.counts.waiting, 'waiting', Clock3],
                ['运行中', data.counts.active, 'running', Workflow],
                ['已提交 PR', data.counts.published_prs, 'done', CheckCircle2],
                ['已登记仓库', data.repositories.length, 'repos', FolderGit2],
              ].map(([label, count, cls, Icon]) => {
                const MetricIcon = Icon as typeof Clock3;
                return <div className={`metric ${cls}`} key={String(label)}>
                  <div className="metric-label">{String(label)}<MetricIcon size={17} /></div>
                  <strong>{String(count)}</strong><span>{cls === 'repos' ? '已登记 GitHub 仓库' :
                    cls === 'waiting' ? '等待你的复现结果' : cls === 'running' ? '当前执行队列'
                      : '等待仓库拥有者审核'}</span>
                </div>;
              })}
            </div>
            <section className="pipeline-section">
              <div className="section-heading"><h2><Workflow size={18} />Agent 角色</h2>
                <span className="section-meta">{registeredRoles.length} 个角色<span className="divider-dot">·</span>按流程复用</span></div>
              <AgentRoleCards models={system?.agent_models} onConfigure={() => navigate('settings')} />
            </section>
            <div className="overview-columns">
              <div>
                <section>
                  <div className="section-heading"><h2><Clock3 size={18} />待人工复现<span className="small-count">{waiting.length}</span></h2>
                    <button className="text-link" onClick={() => { setFilter('waiting'); navigate('findings'); }}>全部问题<ArrowRight size={14} /></button></div>
                  {waiting.length ? <div className="waiting-list">{waiting.slice(0, 4).map(run => <button className="waiting-item" key={run.id} onClick={() => openRepository(run.repo_id, run.env_id)}>
                    <span className="severity">{run.severity}</span><div><strong>{run.title}</strong>
                      <span>{run.repository}<i>·</i>{time(run.created)}</span></div><ChevronRight size={17} /></button>)}</div> :
                    <Empty icon={CheckCircle2} title="暂无待确认问题" />}
                </section>
                <section className="recent-section">
                  <div className="section-heading"><h2><FolderGit2 size={18} />仓库</h2>
                    <button className="text-link" onClick={() => navigate('repositories')}>查看全部<ArrowRight size={14} /></button></div>
                  <div className="repository-table">{data.repositories.slice(0, 4).map(repo => <div className="repository-row" key={repo.id}>
                    <span className={`repo-icon ${repo.kind}`}><FolderGit2 size={20} /></span><div><strong>{repo.name}</strong>
                      <span><i className={`language-dot ${repo.language}`} />{repo.language === 'Unknown' ? '待识别' : repo.language}
                        <i>·</i>GitHub</span></div>
                    <span className="tag neutral">项目环境</span>
                    <button className="icon-button" title="管理运行环境"
                      disabled={busy} onClick={() => openRepository(repo.id)}><Play size={15} /></button>
                  </div>)}</div>
                </section>
              </div>
              <aside className="overview-aside">
                <section><div className="section-heading"><h2><ShieldCheck size={18} />运行边界</h2></div>
                  <dl className="boundary-list"><div><dt>系统发现</dt><dd className="green-text"><LockKeyhole size={13} />人工复现</dd></div>
                    <div><dt>已有 Issue</dt><dd>同步与源码分析</dd></div>
                    <div><dt>GitHub 发布</dt><dd>{system?.automatic_repair?.enabled ? '已启用' : '未启用'}</dd></div><div><dt>自动合并</dt><dd className="red-text">禁止</dd></div>
                    <div><dt>外部模型调用</dt><dd>{system?.external_model_calls ? '已启用' : '关闭'}</dd></div>
                    <div><dt>每日调度</dt><dd>{system?.schedule?.enabled_repositories
                      ? `${system.schedule.enabled_repositories} 个仓库` : '未启用'}</dd></div></dl>
                </section>
                <section className="recent-section"><div className="section-heading"><h2><Activity size={18} />最近活动</h2></div>
                  <EventFeed events={data.events.slice(0, 5)} onSelect={openRepository} /></section>
              </aside>
            </div>
          </>}
          {view === 'repositories' && <section className="repo-page">
            <section className="schedule-overview">
              <div className="section-heading">
                <h2><Clock3 size={18} />每日巡检</h2>
                <span className="section-meta">
                  {scheduleOverview.filter(item => item.enabled).length} 个启用
                </span>
              </div>
              <div className="schedule-grid schedule-grid-header">
                <span>仓库</span><span>巡检时间</span><span>最近调度</span><span />
              </div>
              {scheduleOverview.filter(item =>
                item.repository.toLowerCase().includes(query.toLowerCase())).map(item => (
                <div className="schedule-grid schedule-grid-row" key={item.repo_id}>
                  <div><strong>{item.repository}</strong>
                    <span>GitHub</span></div>
                  <div><Clock3 size={14} />{item.local_time}
                    <span className={`tag ${scheduleStatusClass(item)}`}>
                      {scheduleStatuses[item.last_status] ?? item.last_status}
                    </span></div>
                  <div><strong>{item.last_run_date || '尚未调度'}</strong>
                    <span>{scheduleDetail(item)}</span></div>
                  <button className="icon-button" title="管理巡检" disabled={item.kind !== 'github'}
                    onClick={() => openRepository(item.repo_id)}><ChevronRight size={16} /></button>
                </div>
              ))}
            </section>
            <div className="list-topline"><span>{data.repositories.length} 个仓库</span><span>登记状态</span></div>
            {data.repositories.filter(r => r.name.toLowerCase().includes(query.toLowerCase())).map(repo =>
              <div className="repository-row large" key={repo.id}><span className={`repo-icon ${repo.kind}`}><FolderGit2 size={23} /></span>
                <div><strong>{repo.name}</strong><span>{repo.url}<i>·</i>{repo.language}</span></div>
                <span className="tag neutral">GitHub</span>
                {repo.url && <a className="icon-button" title="在 GitHub 打开" href={repo.url} target="_blank" rel="noreferrer"><ExternalLink size={16} /></a>}
                <button className="icon-button" title="管理运行环境"
                  disabled={busy} onClick={() => openRepository(repo.id)}><Play size={17} /></button>
              </div>)}
            {data.repositories.filter(repo => repo.id === executionRepo && repo.kind === 'github').map(repo =>
              <ExecutionPanel key={`${repo.id}:${executionEnv}`} repo={repo} api={api} initialEnvId={executionEnv} />)}
          </section>}
          {view === 'findings' && <section className="activity-page">
            <div className="segmented">
              <button className={filter === 'all' ? 'active' : ''} onClick={() => setFilter('all')}>全部</button>
              <button className={filter === 'waiting' ? 'active' : ''} onClick={() => setFilter('waiting')}>待复现 <span>{data.counts.waiting}</span></button>
              <button className={filter === 'attention' ? 'active' : ''} onClick={() => setFilter('attention')}>待处理</button>
            </div>
            {!filteredFindings.length ? <Empty icon={Search} title="暂无匹配的问题" /> :
              <div className="waiting-list">{filteredFindings.map(finding =>
                <button className="waiting-item" key={finding.id} onClick={() => openRepository(finding.repo_id, finding.env_id)}>
                  <span className="severity">{finding.severity}</span>
                  <div><strong>{finding.title}</strong><span>{finding.repository}<i>·</i>{time(finding.created)}</span></div>
                  <Status status={finding.status} /><ChevronRight size={17} />
                </button>)}</div>}
          </section>}
          {view === 'activity' && <section className="activity-page">
            <div className="section-heading"><h2>执行时间线</h2><span className="section-meta">最近 {data.events.length} 条</span></div>
            <EventFeed events={data.events.filter(e => !query || `${e.repository} ${e.detail} ${e.task_id}`.includes(query))} onSelect={openRepository} /></section>}
          {view === 'memory' && <MemoryView repositories={data.repositories} api={api} />}
          {view === 'settings' && <section className="settings-page">
            <AgentModelSettings api={api} onChange={() => { void refresh().catch(e => setError(e.message)); }} />
            <RepairSettings api={api} modelEnabled={system?.automatic_repair?.model_enabled} />
            <GitHubAccounts api={api} repositories={data.repositories}
              publicationEnabled={system?.automatic_repair?.enabled} />
            <div className="section-heading"><h2>运行环境</h2><span className="tag neutral">阶段 03 / 04 · 修复与发布</span></div>
            <dl className="settings-list">
              {[['Agent 框架', `OpenAI Agents SDK ${system?.sdk_version}`], ['后端', 'Python · FastAPI'],
                ['状态存储', 'SQLite'], ['仓库记忆', '共享 · 仓库与版本隔离'],
                ['GitHub 写入', system?.automatic_repair?.enabled ? '已启用 · 测试与审查通过后提交' : '自动修复开关未启用'],
                ['仓库分析模型', system?.repository_analysis?.enabled ? '按角色配置 · 已就绪' : '等待角色模型配置或启用'],
                ['环境诊断模型', system?.execution_diagnosis?.enabled ? '按角色配置 · 已就绪' : '使用本地规则'],
                ['系统发现问题', '人工复现确认'], ['已有 Issue', '同步与源码分析'],
                ['真实 Issue 接入', '已接入'],
                ['外部仓库执行', '本机 Docker · 按需启动'],
                ['每日检查', system?.schedule?.enabled_repositories
                  ? `${system.schedule.enabled_repositories} 个仓库` : '未启用'],
                ['自动合并', '禁止'],
                ['分析任务最大轮数', String(system?.max_turns)], ['修复任务最大尝试次数', String(system?.max_attempts)],
                ['外部 Trace 上传', '关闭'], ['访问范围', '本机 · 所有者登录'],
              ]
                .map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}
            </dl>
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
