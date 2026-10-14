import { useCallback, useEffect, useRef, useState } from 'react';
import { Download, ExternalLink, FlaskConical, Lightbulb, Loader2, Play, RefreshCw, Save, Square, Terminal, Trash2 } from 'lucide-react';
import { AnalysisPanel } from './analysis';

type Api = <T>(path: string, body?: unknown) => Promise<T>;
type Config = {
  language: string; install: string; test: string; start: string; port: number;
  install_network: boolean; detected?: boolean; reason?: string;
};
type Job = { id: string; action: string; state: string; log: string; error: string | null; cancel: number };
type Diagnosis = {
  job_id: string; state: string; current: boolean; applicable: boolean;
  report: {
    source: string; code: string; summary: string; confidence: string;
    evidence: string[]; steps: string[]; patch: Partial<Config>; handoffs: string[];
    model_status: string;
  };
};
type Environment = {
  id: string; revision: string | null; config: Config | null; state: string;
  version: number; installed: boolean; busy: boolean; url: string | null;
  error: string | null; created: number; expires: number | null; jobs: Job[];
  test_result: { exit_code: number; revision: string } | null;
  diagnosis?: Diagnosis | null;
};
type Readiness = {
  ready: boolean; reason: string; diagnosis_model?: { enabled: boolean; model: string | null };
};
const configLabels: Record<string, string> = {
  install_network: '安装时允许联网', install: '安装命令', test: '测试 / CLI 命令',
  start: '启动命令', port: '项目端口',
};
const states: Record<string, string> = {
  queued: '等待下载', downloading: '下载中', downloaded: '源码已下载', config_required: '需要配置命令',
  installing: '安装中', ready: '环境已就绪', testing: '测试中', starting: '启动中',
  running: '项目运行中', stopped: '已停止', failed: '操作失败', setup_failed: '安装未通过',
  test_failed: '测试未通过', deleted: '环境已清理',
};
const actions: Record<string, string> = {
  download: '下载源码', setup: '安装环境', test: '执行测试', start: '启动项目', stop: '停止项目', delete: '清理环境',
};
const errors: Record<string, string> = {
  github_active_account_required: '请先在运行配置中连接并选择 GitHub 账号。',
  github_invalid_token: 'GitHub 令牌已失效，请重新连接账号。',
  github_credential_missing: '无法读取账号凭据，请重新连接账号。',
  github_repository_unavailable: '仓库不存在，或所选账号没有读取权限。',
  github_denied_or_rate_limited: 'GitHub 拒绝访问或触发限流，请稍后重试。',
  github_unreachable: '无法连接 GitHub，请检查网络。',
  secure_storage_unavailable: '本机凭据库不可用。',
  'GitHub account changed. Refresh and try again.': '下载期间账号凭据已变化，请重新下载。',
  docker_unavailable: 'Docker 不可用，请启动 Docker Desktop 后重新检查。',
  docker_command_failed: 'Docker 操作未完成，请检查引擎、镜像下载和可用磁盘。',
  docker_linux_required: '请将 Docker 切换为 Linux 容器模式。',
  docker_local_engine_required: '当前 Docker 连接不是本机引擎。',
  execution_config_required: '需要先保存项目命令。',
  environment_changed: '环境状态已变化，请刷新后重试。',
  environment_busy: '环境正在执行任务，请等待完成或停止。',
  environment_not_ready: '环境尚未就绪，请先下载并安装。',
  environment_not_configurable: '当前环境不能修改，请先停止项目。',
  setup_required: '请先完成环境安装。',
  execution_command_required: '请先填写对应的执行命令。',
  setup_failed: '安装未通过。',
  test_failed: '测试未通过，结果已记录。',
  start_failed: '项目启动后退出，请检查启动命令。',
  start_not_listening: '120 秒内未检测到端口监听，请检查命令、端口和监听地址。',
  download_denied: '源码下载被拒绝，请检查令牌的仓库内容读取权限。',
  download_unreachable: '源码下载失败，请检查网络。',
  archive_limit: '仓库源码超过当前大小或文件数量上限。',
  unsafe_archive: '源码包含不支持的链接、特殊文件或路径。',
  empty_archive: '下载的源码为空。',
  download_redirect_rejected: '源码下载地址未通过验证。',
  execution_timeout: '执行已超时，项目容器已停止。',
  execution_cancelled: '操作已停止。',
  execution_interrupted: '上次执行被中断，可以重新安装或启动。',
  cleanup_pending: '容器清理尚未完成，请恢复 Docker 连接。',
  execution_failed: '操作未完成，请检查 Docker 状态后重试。',
  diagnosis_stale: '配置或执行记录已变化，请使用当前失败记录的诊断。',
  diagnosis_no_patch: '这份诊断没有可直接应用的配置，请按建议检查。',
};
const errorText = (value: string) => errors[value] ?? '操作未完成，请刷新状态后重试。';

function Configuration({ env, api, onSaved }: { env: Environment; api: Api; onSaved: () => Promise<void> }) {
  const [config, setConfig] = useState<Config>(env.config!);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const locked = busy || env.busy || env.state === 'running' || env.state === 'deleted';
  return <form className="execution-config" aria-label="项目运行配置" onSubmit={e => {
    e.preventDefault(); setBusy(true); setError('');
    const { language, install, test, start, port, install_network } = config;
    void api(`/environments/${env.id}/configuration`, {
      version: env.version, language, install, test, start, port, install_network,
    }).then(onSaved).catch(e => setError(errorText(e.message))).finally(() => setBusy(false));
  }}>
    <div className="execution-config-row">
      <label>语言环境<select value={config.language} disabled={locked}
        onChange={e => setConfig({ ...config, language: e.target.value })}>
        <option value="python">Python 3.13</option><option value="go">Go 1.25</option><option value="typescript">Node.js 24 / TypeScript</option>
      </select></label>
      <label>项目端口<input type="number" min={1024} max={65535} required value={config.port} disabled={locked}
        onChange={e => setConfig({ ...config, port: Number(e.target.value) })} /></label>
    </div>
    {(['install', 'test', 'start'] as const).map(key => <label key={key}>
      {{ install: '安装命令', test: '测试 / CLI 命令', start: '启动命令' }[key]}
      <textarea rows={2} maxLength={4000} spellCheck={false} value={config[key]} disabled={locked}
        onChange={e => setConfig({ ...config, [key]: e.target.value })} />
    </label>)}
    <div className="execution-config-footer">
      <label className="checkbox-label"><input type="checkbox" checked={config.install_network} disabled={locked}
        onChange={e => setConfig({ ...config, install_network: e.target.checked })} />安装时允许联网</label>
      <button className="button secondary" disabled={locked}><Save size={15} />保存配置</button>
    </div>
    {error && <p className="error-inline" role="alert">{error}</p>}
  </form>;
}

export function ExecutionPanel({ repo, api }: { repo: { id: string; name: string }; api: Api }) {
  const [environments, setEnvironments] = useState<Environment[]>([]);
  const [selected, setSelected] = useState('');
  const selectedRef = useRef('');
  const [readiness, setReadiness] = useState<Readiness | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [tab, setTab] = useState('logs');
  const refresh = useCallback(async () => {
    const data = await api<Environment[]>(`/repositories/${repo.id}/environments`);
    const id = data.some(env => env.id === selectedRef.current) ? selectedRef.current : data[0]?.id ?? '';
    selectedRef.current = id;
    const detail = id ? await api<Environment>(`/environments/${id}`) : null;
    if (selectedRef.current !== id) return;
    setEnvironments(data.map(env => env.id === id && detail ? detail : env));
    setSelected(id);
  }, [api, repo.id]);
  const checkEngine = useCallback(async () =>
    setReadiness(await api<Readiness>('/execution/readiness')), [api]);
  useEffect(() => {
    let active = true;
    const poll = () => { if (active) void refresh().catch(e => setError(errorText(e.message))); };
    poll();
    void checkEngine().catch(e => setError(errorText(e.message)));
    const timer = window.setInterval(poll, 2000);
    return () => { active = false; window.clearInterval(timer); };
  }, [refresh, checkEngine]);
  const env = environments.find(item => item.id === selected);
  const perform = async (work: () => Promise<unknown>) => {
    setBusy(true); setError('');
    try { await work(); await refresh(); }
    catch (e) { setError(errorText((e as Error).message)); await refresh().catch(() => {}); }
    finally { setBusy(false); }
  };
  const action = (name: string) => perform(() => api(`/environments/${env!.id}/${name}`, { version: env!.version }));
  const running = env?.state === 'running';
  const locked = busy || env?.busy;
  return <section className="execution-panel" aria-labelledby="execution-title">
    <div className="section-heading">
      <h2 id="execution-title"><Terminal size={18} />{repo.name}</h2>
      <button className="button primary" disabled={busy || env?.busy}
        onClick={() => void perform(async () => {
          const created = await api<Environment>(`/repositories/${repo.id}/environments`, {});
          selectedRef.current = created.id; setSelected(created.id); setTab('logs');
        })}><Download size={15} />下载最新版本</button>
    </div>
    <div className="execution-engine">
      <span className={`tag ${readiness?.ready ? 'green' : 'neutral'}`}>
        {readiness ? readiness.ready ? 'Docker 已就绪' : 'Docker 未就绪' : '正在检查 Docker'}
      </span>
      <button className="icon-button" title="重新检查 Docker" disabled={busy}
        onClick={() => void perform(checkEngine)}><RefreshCw size={15} /></button>
      {readiness && !readiness.ready && <span>{errorText(readiness.reason)}</span>}
      <span>模型诊断：{readiness?.diagnosis_model?.enabled ? readiness.diagnosis_model.model : '未配置'}</span>
    </div>
    {error && <div className="error-inline" role="alert">{error}</div>}
    {!env ? <div className="empty"><Download size={28} /><p>尚未下载项目源码</p></div> : <>
      <label className="execution-version">环境版本<select value={selected} onChange={e => {
        selectedRef.current = e.target.value; setSelected(e.target.value); void perform(refresh);
      }}>
        {environments.map(item => <option value={item.id} key={item.id}>
          {new Date(item.created * 1000).toLocaleString('zh-CN')} · {item.revision?.slice(0, 12) ?? '待下载'} · {states[item.state] ?? item.state}
        </option>)}
      </select></label>
      <div className="execution-facts">
        <span className="tag neutral">{states[env.state] ?? env.state}</span>
        {env.busy && <span><Loader2 size={14} className="spin" />{env.jobs.some(j => j.cancel && j.state === 'running') ? '正在停止' : '任务处理中'}</span>}
        {env.revision && <code title={env.revision}>{env.revision}</code>}
        {env.test_result && <span>最近测试：{env.test_result.exit_code === 0 ? '通过' : `未通过（${env.test_result.exit_code}）`}</span>}
      </div>
      {env.error && <div className="error-inline" role="status">{errorText(env.error)}</div>}
      <AnalysisPanel key={env.id} envId={env.id} version={env.version} api={api} />
      {env.diagnosis && <div className="execution-diagnosis" aria-label="失败原因与建议">
        <div className="section-heading">
          <h3><Lightbulb size={17} />失败原因与建议</h3>
          <span className="tag neutral">{env.diagnosis.report.source === 'model' ? '模型诊断' : '本地规则诊断'}</span>
          {!env.diagnosis.current && <span className="tag neutral">历史记录</span>}
          {['queued', 'running'].includes(env.diagnosis.state) &&
            <span className="diagnosis-progress"><Loader2 size={14} className="spin" />Agent 分析中</span>}
        </div>
        <p className="diagnosis-summary">{env.diagnosis.report.summary}</p>
        <div className="diagnosis-evidence">
          <strong>依据</strong>
          {env.diagnosis.report.evidence.map((line, index) => <pre key={index}>{line}</pre>)}
        </div>
        <ol>{env.diagnosis.report.steps.map((step, index) => <li key={index}>{step}</li>)}</ol>
        {Object.keys(env.diagnosis.report.patch).length > 0 && <>
          <dl className="diagnosis-changes">{Object.entries(env.diagnosis.report.patch).map(([key, value]) =>
            <div key={key}><dt>{configLabels[key] ?? key}</dt>
              <dd>{typeof value === 'boolean' ? value ? '开启' : '关闭' : String(value)}</dd></div>)}</dl>
          <button className="button secondary" disabled={locked || !env.diagnosis.applicable}
            onClick={() => void perform(() => api(
              `/environments/${env.id}/diagnoses/${env.diagnosis!.job_id}/apply`, { version: env.version },
            ))}><RefreshCw size={15} />应用建议并重新安装</button>
        </>}
        {env.diagnosis.report.handoffs.length > 0 &&
          <p className="diagnosis-meta">仓库分析 → 复现辅助 → 独立审查</p>}
        <p className="diagnosis-meta">建议尚未经过重试验证
          {['unavailable', 'interrupted'].includes(env.diagnosis.report.model_status) && ' · 模型分析未完成，保留本地诊断'}</p>
      </div>}
      <div className="execution-actions">
        <button className="button secondary" disabled={locked || running || !env.config?.detected || !readiness?.ready || env.state === 'deleted'}
          onClick={() => void action('setup')}><Download size={15} />安装环境</button>
        <button className="button secondary" disabled={locked || running || !env.installed || !env.config?.test || !readiness?.ready || env.state === 'deleted'}
          onClick={() => void action('test')}><FlaskConical size={15} />执行测试</button>
        <button className="button secondary" disabled={locked || running || !env.installed || !env.config?.start || !readiness?.ready || env.state === 'deleted'}
          onClick={() => void action('start')}><Play size={15} />启动项目</button>
        <button className="icon-button" title="停止项目或任务" disabled={busy || (!env.busy && !running)}
          onClick={() => void action('stop')}><Square size={17} /></button>
        <button className="icon-button" title="清理环境" disabled={locked || running || env.state === 'deleted'}
          onClick={() => void action('delete')}><Trash2 size={17} /></button>
      </div>
      {env.url && <div className="execution-access">
        <a className="button primary" href={env.url} target="_blank" rel="noreferrer"><ExternalLink size={15} />打开项目</a>
        <code>{env.url}</code>
        <span>自动停止：{new Date(env.expires! * 1000).toLocaleTimeString('zh-CN')}</span>
      </div>}
      <div className="tabs" role="tablist">
        <button role="tab" aria-selected={tab === 'logs'} className={tab === 'logs' ? 'active' : ''} onClick={() => setTab('logs')}>执行记录</button>
        <button role="tab" aria-selected={tab === 'config'} className={tab === 'config' ? 'active' : ''} disabled={!env.config}
          onClick={() => setTab('config')}>运行配置</button>
      </div>
      {tab === 'config' && env.config ? <Configuration key={env.id + JSON.stringify(env.config)} env={env} api={api} onSaved={refresh} /> :
        <div className="execution-jobs">{env.jobs.map(job => <details key={job.id} open={job.id === env.jobs[0]?.id}>
          <summary>{actions[job.action]} · {{ queued: '排队中', running: '执行中', completed: '已完成', failed: '未完成' }[job.state]}</summary>
          {job.error && <p className="error-inline">{errorText(job.error)}</p>}
          <pre>{job.log || (job.state === 'running' ? '正在执行，完成后更新日志。' : '暂无命令输出')}</pre>
        </details>)}</div>}
    </>}
  </section>;
}
