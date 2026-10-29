import { useCallback, useEffect, useRef, useState } from 'react';
import { CircleDot, Download, ExternalLink, FlaskConical, Lightbulb, Loader2, Play, RefreshCw, Save, Square, Terminal, Trash2 } from 'lucide-react';
import { AnalysisPanel, analysisErrorMessages } from './analysis';
import { RepairPanel } from './repair';

type Api = <T>(path: string, body?: unknown) => Promise<T>;
const repositorySteps = [['environment', '环境准备'], ['analysis', '项目分析'], ['issues', 'Issue'], ['repair', '修复'], ['pr', 'PR']];
const repositorySection = (section: string, fallback: string) => {
  if (section === 'reproduction') return 'issues';
  return repositorySteps.some(([key]) => key === section) ? section : fallback;
};
type DesktopProfile = {
  kind: 'python_desktop'; runtime_stdlib_only: boolean; test_command: string; complete: false;
  selected: { path: string; module: string }[]; excluded: { path: string; reasons: string[] }[];
  uncovered: string[];
};
type DesktopScope = Pick<DesktopProfile, 'selected' | 'excluded' | 'uncovered' | 'complete'> & {
  kind: 'desktop_offline'; total: number | null; executed: number | null; skipped: number;
  selection: 'generated' | 'custom';
};
type Config = {
  language: string; install: string; test: string; start: string; port: number;
  install_network: boolean; detected?: boolean; reason?: string; run_mode?: 'service' | 'test_only';
  project_profile?: DesktopProfile; desktop_approved?: boolean;
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
type Pipeline = { id: string; state: string; stage: string; error: string | null; analysis_id: string | null };
const pipelineActive = (pipeline?: Pipeline | null) => !!pipeline &&
  ['queued', 'running', 'waiting_configuration', 'waiting_environment', 'waiting_analysis', 'waiting_model'].includes(pipeline.state);
const pipelineStages: Record<string, string> = {
  download: '下载源码', setup: '准备环境／复用依赖', test: '自动测试', start: '启动项目',
  access: '检查访问地址', analysis: '分析运行结果',
};
const pipelineStates: Record<string, string> = {
  queued: '排队中', running: '执行中', waiting_configuration: '等待补齐配置',
  waiting_environment: '等待 Docker', waiting_analysis: '等待或正在分析', waiting_model: '等待模型配置',
  completed: '流水线已完成', failed: '流水线已停止', cancelled: '流水线已取消',
};
type Environment = {
  id: string; revision: string | null; config: Config | null; state: string;
  version: number; installed: boolean; busy: boolean; url: string | null;
  error: string | null; created: number; expires: number | null; jobs: Job[];
  test_result: { exit_code: number; revision: string; desktop_scope?: DesktopScope } | null;
  diagnosis?: Diagnosis | null;
  preparation?: string | null; reused_environment_id?: string | null;
  pipeline?: Pipeline | null;
  access_result?: { status_code: number; path: string; authentication_required?: boolean } | null;
};
type Readiness = {
  ready: boolean; reason: string; diagnosis_model?: { enabled: boolean; model: string | null };
};
type Issue = {
  id: string; number: number; title: string; state: string; url: string | null;
  updated_at: string | null;
};
type Schedule = {
  repo_id: string; enabled: boolean; local_time: string; last_run_date: string;
  last_status: string; last_detail: string; version: number;
};
const configLabels: Record<string, string> = {
  install_network: '安装时允许联网', install: '安装命令', test: '测试 / CLI 命令',
  start: '启动命令', port: '项目端口',
};
const states: Record<string, string> = {
  queued: '等待下载', downloading: '下载中', downloaded: '源码已下载', config_required: '需要配置命令',
  installing: '安装中', ready: '环境已就绪', testing: '测试中', starting: '启动中',
  running: '项目运行中', stopped: '已停止', failed: '操作失败', setup_failed: '安装未通过',
  test_failed: '测试未通过', deleted: '环境已清理', reused: '已复用原版本环境',
};
const environmentState = (env: Environment) =>
  ['config_required', 'downloaded'].includes(env.state) && env.config?.project_profile &&
  !env.config.desktop_approved ? '待确认离线范围' : states[env.state] ?? env.state;
const actions: Record<string, string> = {
  access: '检查访问地址', download: '下载源码', setup: '安装环境', test: '执行测试', start: '启动项目', stop: '停止项目', delete: '清理环境',
};
const errors: Record<string, string> = {
  github_active_account_required: '请先在「设置 → GitHub 账号」中连接并选择账号。',
  github_invalid_token: 'GitHub 令牌已失效，请重新连接账号。',
  github_credential_missing: '无法读取账号凭据，请重新连接账号。',
  github_repository_unavailable: '仓库不存在，或所选账号没有读取权限。',
  github_permission_denied: 'GitHub 令牌无法访问所需资源，请检查目标仓库的读取授权和组织策略。',
  github_forbidden: 'GitHub 拒绝访问。请检查令牌权限、组织授权及仓库策略；这不一定是限流。',
  github_rate_limited: 'GitHub 请求已被限流，请稍后重试，避免连续重复请求。',
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
  setup_required: '请先准备运行环境；符合条件时会自动复用依赖。',
  environment_reused: '相同版本已复用，请在原环境中操作。',
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
  desktop_confirmation_required: '已识别桌面项目。请确认容器离线测试范围后继续；Windows 界面不会启动。',
  desktop_test_only_required: '桌面项目只能使用离线测试模式，不能按网页服务启动或检查地址。',
  desktop_install_command_required: '请先填写离线测试环境准备命令。',
  desktop_service_unsupported: '当前只支持桌面项目的容器离线测试，不支持启动 Windows 窗口。',
  desktop_tests_not_executed: '没有确认到实际执行的离线测试（可能全部跳过），流水线已停止，不会记为测试通过。',
  desktop_detection_unavailable: '未识别到当前支持的 Python 桌面测试方案，请手动配置。',
  configuration_detection_not_applicable: '已有安装或自定义命令，自动识别不会覆盖它们。',
  pipeline_busy: '自动流水线正在执行，请先停止流水线再手动操作。',
  pipeline_test_command_required: '请在运行配置中填写测试命令，保存后自动继续。',
  pipeline_start_command_required: '请填写启动命令；若无需服务，请明确选择“仅测试项目”。',
  pipeline_test_required: '当前版本尚未通过测试，已停止启动。',
  pipeline_account_changed: '流水线绑定的 GitHub 身份已变化，已停止。',
  pipeline_configuration_changed: '配置已变化，旧流水线已停止，可从当前有效结果继续。',
  pipeline_stopped: '流水线已停止，不会自动重新执行。',
  schedule_time_invalid: '巡检时间无效，请使用 00:00 到 23:59。',
  schedule_changed: '巡检配置已变化，请刷新后重试。',
  github_repository_required: '只有 GitHub 仓库可以启用每日巡检。',
};
const scheduleStatuses: Record<string, string> = {
  idle: '待调度', paused: '已暂停', scheduled: '已调度',
  skipped_active: '已有任务，跳过', failed: '调度失败',
};
const errorText = (value: string) => errors[value] ?? analysisErrorMessages[value]
  ?? '操作未完成，请查看对应步骤的执行记录。';

function DesktopCoverage({ profile, scope }: { profile: DesktopProfile; scope?: DesktopScope }) {
  const reasons: Record<string, string> = {
    gui: '需要图形界面', windows_native: '需要 Windows 原生功能',
    unknown_dependency: '依赖未确认', dynamic_import: '动态导入未确认',
    stdlib_shadowing: '存在同名标准库模块', ambiguous_local_import: '本地模块名称冲突',
    test_framework_unknown: '测试框架未确认',
    selection_limit: '超出自动选择上限', unsupported_module_path: '模块路径不支持自动选择',
  };
  return <section className="execution-diagnosis desktop-coverage" aria-label="桌面项目验证范围">
    <h3>Python 桌面项目 · 仅容器离线测试</h3>
    <p>不启动 Windows 桌面窗口，不检查网页地址，不访问本机客户端配置。</p>
    <p>未覆盖：{profile.uncovered.join('；')}。离线测试通过不代表整个桌面程序验收通过。</p>
    {!profile.runtime_stdlib_only && <p>未确认完整运行依赖，请检查安装命令；不会自动安装打包依赖。</p>}
    <details><summary>候选离线测试（{profile.selected.length} 个模块）</summary>
      <ul>{profile.selected.map(item => <li key={item.path}><code>{item.path}</code></li>)}</ul>
    </details>
    <details><summary>未纳入自动测试（{profile.excluded.length} 个模块）</summary>
      <ul>{profile.excluded.map(item => <li key={item.path}><code>{item.path}</code>：{
        item.reasons.map(reason => reasons[reason] ?? '未覆盖').join('、')}</li>)}</ul>
    </details>
    {scope && <p role="status">容器离线测试：实际执行 {scope.executed ?? '未确认'} 项，跳过 {scope.skipped} 项。
      {scope.selection === 'custom' && '使用了自定义命令，候选列表不等于实际覆盖。'}
      桌面界面与原生集成仍未验收。</p>}
  </section>;
}

function FailureDiagnosis({ env, locked, onApply }: {
  env: Environment; locked: boolean; onApply: () => void;
}) {
  const diagnosis = env.diagnosis;
  if (!diagnosis) return null;
  const current = diagnosis.current && !env.busy &&
    ['setup_failed', 'test_failed', 'failed'].includes(env.state) && env.error !== 'cleanup_pending';
  const content = <>
    <div className="section-heading">
      <h3><Lightbulb size={17} />{current ? '失败原因与建议' : '历史失败诊断'}</h3>
      <span className="tag neutral">{diagnosis.report.source === 'model' ? '模型诊断' : '本地规则诊断'}</span>
      {!current && <span className="tag neutral">历史记录 · 仅供回看</span>}
      {current && ['queued', 'running'].includes(diagnosis.state) &&
        <span className="diagnosis-progress"><Loader2 size={14} className="spin" />Agent 分析中</span>}
    </div>
    {!current && <p className="diagnosis-meta">这是较早任务的诊断，不代表当前环境仍然失败，也不会自动重新安装。</p>}
    <p className="diagnosis-summary">{diagnosis.report.summary}</p>
    <div className="diagnosis-evidence">
      <strong>依据</strong>
      {diagnosis.report.evidence.map((line, index) => <pre key={index}>{line}</pre>)}
    </div>
    <ol>{diagnosis.report.steps.map((step, index) => <li key={index}>{step}</li>)}</ol>
    {Object.keys(diagnosis.report.patch).length > 0 && <>
      <dl className="diagnosis-changes">{Object.entries(diagnosis.report.patch).map(([key, value]) =>
        <div key={key}><dt>{configLabels[key] ?? key}</dt>
          <dd>{typeof value === 'boolean' ? value ? '开启' : '关闭' : String(value)}</dd></div>)}</dl>
      {current && <button className="button secondary" disabled={locked || !diagnosis.applicable}
        onClick={onApply}><RefreshCw size={15} />应用建议并重新安装</button>}
    </>}
    {diagnosis.report.handoffs.length > 0 &&
      <p className="diagnosis-meta">仓库分析 → 复现辅助 → 独立审查</p>}
    {current && <p className="diagnosis-meta">建议尚未经过重试验证
      {['unavailable', 'interrupted'].includes(diagnosis.report.model_status) && ' · 模型分析未完成，保留本地诊断'}</p>}
  </>;
  if (current) return <div className="execution-diagnosis" aria-label="失败原因与建议">{content}</div>;
  return <details className="execution-diagnosis diagnosis-history" aria-label="历史失败诊断">
    <summary>历史失败诊断（非当前故障，点击查看）</summary>
    {content}
  </details>;
}

function Configuration({ env, api, onSaved }: { env: Environment; api: Api; onSaved: () => Promise<void> }) {
  const [config, setConfig] = useState<Config>(env.config!);
  const desktop = config.project_profile;
  const [confirmed, setConfirmed] = useState(!!config.desktop_approved);
  const needsDesktopInput = !!desktop && (!confirmed || !config.install.trim() || !config.test.trim());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const locked = busy || env.busy || env.state === 'running' || env.state === 'deleted' ||
    (pipelineActive(env.pipeline) && !['waiting_configuration', 'waiting_environment'].includes(env.pipeline!.state));
  return <form className="execution-config" aria-label="项目运行配置" onSubmit={e => {
    e.preventDefault(); setBusy(true); setError('');
    const { language, install, test, start, port, install_network } = config;
    void api(`/environments/${env.id}/configuration`, {
      version: env.version, language, install, test, start, port, install_network,
      run_mode: config.run_mode ?? 'service',
      ...(desktop ? { desktop_offline_confirmed: confirmed } : {}),
      continue_pipeline: (e.nativeEvent as SubmitEvent).submitter?.getAttribute('data-continue') === 'true',
    }).then(onSaved).catch(e => setError(errorText(e.message))).finally(() => setBusy(false));
  }}>
    {!desktop && !config.detected && !env.installed &&
      ![config.install, config.test, config.start].some(value => value.trim()) &&
      <button className="button secondary" type="button" disabled={locked} onClick={() => {
        setBusy(true); setError('');
        void api(`/environments/${env.id}/configuration/detect`, { version: env.version })
          .then(onSaved).catch(e => setError(errorText(e.message))).finally(() => setBusy(false));
      }}><RefreshCw size={15} />识别桌面测试配置</button>}
    {desktop && <p>首次执行前请确认下方范围。保存后仅在隔离容器中运行兼容测试，随后进行源码分析。</p>}
    <div className="execution-config-row">
      <label>语言环境<select value={config.language} disabled={locked}
        onChange={e => setConfig({ ...config, language: e.target.value })}>
        <option value="python">Python 3.13</option><option value="go">Go 1.25</option><option value="typescript">Node.js 24 / TypeScript</option>
      </select></label>
      <label>项目端口<input type="number" min={1024} max={65535} required value={config.port} disabled={locked}
        onChange={e => setConfig({ ...config, port: Number(e.target.value) })} /></label>
    </div>
    <label>项目运行类型<select value={config.run_mode ?? 'service'} disabled={locked || !!desktop}
      onChange={e => setConfig({ ...config, run_mode: e.target.value as 'service' | 'test_only' })}>
      <option value="service">服务项目：测试通过后启动并检查地址</option>
      <option value="test_only">仅测试项目：不启动服务</option>
    </select></label>
    <p className="diagnosis-meta">仅修改启动命令或端口会保留安装和测试结果；修改测试命令只清除测试结果。</p>
    {(['install', 'test', 'start'] as const).map(key => <label key={key}>
      {{ install: '安装命令', test: '测试 / CLI 命令', start: '启动命令' }[key]}
      <textarea rows={2} maxLength={4000} spellCheck={false} value={config[key]} disabled={locked}
        onChange={e => setConfig({ ...config, [key]: e.target.value })} />
    </label>)}
    {desktop && <label className="checkbox-label"><input type="checkbox" checked={confirmed}
      disabled={locked} onChange={e => setConfirmed(e.target.checked)} />
      我确认仅运行容器离线测试，Windows 桌面界面及专属功能仍未覆盖</label>}
    {desktop && config.test !== desktop.test_command &&
      <p>当前为自定义测试命令，不能把候选模块列表当作已完成的测试覆盖。</p>}
    <div className="execution-config-footer">
      <label className="checkbox-label"><input type="checkbox" checked={config.install_network} disabled={locked}
        onChange={e => setConfig({ ...config, install_network: e.target.checked })} />安装时允许联网</label>
      <button className="button secondary" disabled={locked || needsDesktopInput}><Save size={15} />
        {desktop && !env.config?.desktop_approved ? '确认离线范围并继续' : '保存配置'}</button>
      {!pipelineActive(env.pipeline) && <button className="button primary" data-continue="true" disabled={locked || needsDesktopInput}>
        <Play size={15} />保存并继续流水线</button>}
    </div>
    {error && <p className="error-inline" role="alert">{error}</p>}
  </form>;
}

function IssuesPanel({ repo, api, environment, onAnalysisSubmitted }: {
  repo: { id: string; name: string }; api: Api; environment: Environment | null;
  onAnalysisSubmitted: (analysisId: string) => void;
}) {
  const [issues, setIssues] = useState<Issue[]>([]);
  const [busy, setBusy] = useState(false);
  const [submittingIssue, setSubmittingIssue] = useState('');
  const [submitted, setSubmitted] = useState<Record<string, { id: string; status: string }>>({});
  const [error, setError] = useState('');

  useEffect(() => {
    let active = true;
    void api<Issue[]>(`/repositories/${repo.id}/issues`)
      .then(data => { if (active) setIssues(data); })
      .catch(e => { if (active) setError(errorText((e as Error).message)); });
    return () => { active = false; };
  }, [api, repo.id]);

  const sync = async () => {
    setBusy(true); setError('');
    try {
      setIssues(await api<Issue[]>(`/repositories/${repo.id}/issues/sync`, {}));
    } catch (e) {
      setError(errorText((e as Error).message));
    } finally {
      setBusy(false);
    }
  };

  const analyze = async (issue: Issue) => {
    setBusy(true); setSubmittingIssue(issue.id); setError('');
    try {
      const result = await api<{ id: string; status: string }>(
        `/repositories/${repo.id}/issues/${issue.id}/analysis`,
        environment ? { env_id: environment.id } : {});
      setSubmitted(current => ({ ...current, [issue.id]: result }));
      onAnalysisSubmitted(result.id);
    } catch (e) {
      setError(errorText((e as Error).message));
    } finally {
      setBusy(false); setSubmittingIssue('');
    }
  };

  return <section className="issue-panel" aria-label="GitHub Issues">
    <div className="section-heading">
      <h3><CircleDot size={18} />GitHub Issue</h3>
      <button className="button secondary" disabled={busy} onClick={() => void sync()}>
        <RefreshCw size={15} />同步 GitHub Issue
      </button>
    </div>
    {error && <div className="error-inline" role="alert">{error}</div>}
    {!issues.length ? <div className="empty"><CircleDot size={24} /><p>尚未同步到 GitHub Issue</p></div> :
      <div className="issue-list">{issues.map(issue =>
        <div className="issue-row" key={issue.id}>
          <span className="issue-number">#{issue.number}</span>
          <div className="issue-copy">
            <span className="tag neutral">GitHub Issue</span>
            <strong>{issue.title}</strong>
            <span>{issue.state === 'open' ? '待解决' : '已关闭'}</span>
          </div>
          <div className="issue-actions">
            {issue.url && <a className="icon-button" title="打开 Issue" href={issue.url}
              target="_blank" rel="noreferrer"><ExternalLink size={15} /></a>}
            <button className="button secondary" disabled={busy || !environment?.revision}
              onClick={() => void analyze(issue)}>
              {submittingIssue === issue.id ? <Loader2 size={15} className="spin" /> : <Play size={15} />}
              {submittingIssue === issue.id ? '提交中' : '分析'}
            </button>
          </div>
          {submitted[issue.id] && <p className="diagnosis-meta" role="status">
            {({ queued: '已排队', running: '分析中', waiting_model: '等待模型配置',
              completed: '分析已完成', failed: '分析未完成', interrupted: '分析已中断',
              superseded: '已被新分析替代' } as Record<string, string>)[submitted[issue.id].status]
              ?? '分析请求已提交'}。
            <button className="text-link" onClick={() => {
              onAnalysisSubmitted(submitted[issue.id].id);
            }}>查看分析进度与结果</button>
          </p>}
        </div>)}</div>}
  </section>;
}

function SchedulePanel({ repo, api }: { repo: { id: string }; api: Api }) {
  const [schedule, setSchedule] = useState<Schedule | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    void api<Schedule>(`/repositories/${repo.id}/schedule`)
      .then(value => { if (active) setSchedule(value); })
      .catch(e => { if (active) setError(errorText((e as Error).message)); });
    return () => { active = false; };
  }, [api, repo.id]);
  if (!schedule) return null;
  return <section className="schedule-panel" aria-label="每日巡检">
    <div className="section-heading">
      <h2>每日巡检</h2>
      <span className={`tag ${schedule.enabled ? 'green' : 'neutral'}`}>
        {schedule.enabled ? '已启用' : '已暂停'}
      </span>
    </div>
    <form className="execution-config" onSubmit={event => {
      event.preventDefault();
      setBusy(true); setError('');
      void api<Schedule>(`/repositories/${repo.id}/schedule`, {
        enabled: schedule.enabled, local_time: schedule.local_time, version: schedule.version,
      }).then(value => {
        setSchedule(value); setError('');
      }).catch(e => setError(errorText((e as Error).message))).finally(() => setBusy(false));
    }}>
      <label className="checkbox-label">
        <input type="checkbox" checked={schedule.enabled} disabled={busy}
          onChange={event => setSchedule({ ...schedule, enabled: event.target.checked })} />
        启用每日巡检
      </label>
      <label>
        巡检时间
        <input type="time" value={schedule.local_time} disabled={busy}
          onChange={event => setSchedule({ ...schedule, local_time: event.target.value })} />
      </label>
      <button className="button secondary" disabled={busy}><Save size={15} />保存巡检配置</button>
    </form>
    <p className="diagnosis-meta">
      最近调度：{schedule.last_run_date || '尚未调度'} · {scheduleStatuses[schedule.last_status] ?? schedule.last_status}
    </p>
    {error && <p className="error-inline" role="alert">{error}</p>}
  </section>;
}

export function ExecutionPanel({ repo, api, initialEnvId = '', initialSection = '', initialAnalysisId = '', initialRepairId = '' }: {
  repo: { id: string; name: string }; api: Api; initialEnvId?: string; initialSection?: string; initialAnalysisId?: string; initialRepairId?: string;
}) {
  const [environments, setEnvironments] = useState<Environment[]>([]);
  const [selected, setSelected] = useState(initialEnvId);
  const selectedRef = useRef(initialEnvId);
  const [readiness, setReadiness] = useState<Readiness | null>(null);
  const [issueAnalysisId, setIssueAnalysisId] = useState(initialAnalysisId);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [autoRun, setAutoRun] = useState(true);
  const [tab, setTab] = useState('logs');
  const [section, setSection] = useState(() => repositorySection(initialSection, initialEnvId ? 'issues' : 'environment'));
  const [issueSource, setIssueSource] = useState<'all' | 'github' | 'analysis'>('all');
  const refresh = useCallback(async () => {
    const data = await api<Environment[]>(`/repositories/${repo.id}/environments`);
    const id = data.some(env => env.id === selectedRef.current || env.reused_environment_id === selectedRef.current)
      ? selectedRef.current : data[0]?.id ?? '';
    selectedRef.current = id;
    const detail = id ? await api<Environment>(`/environments/${id}`) : null;
    if (selectedRef.current !== id) return;
    if (detail?.reused_environment_id) {
      const reused = await api<Environment>(`/environments/${detail.reused_environment_id}`);
      if (selectedRef.current !== id) return;
      selectedRef.current = reused.id;
      setEnvironments([reused, ...data.filter(env => env.id !== reused.id)]);
      setSelected(reused.id);
      setError('');
      return;
    }
    setEnvironments(detail && !data.some(env => env.id === id)
      ? [detail, ...data] : data.map(env => env.id === id && detail ? detail : env));
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
    const engineTimer = window.setInterval(() => {
      if (active) void checkEngine().catch(() => {});
    }, 10000);
    return () => {
      active = false; window.clearInterval(timer); window.clearInterval(engineTimer);
    };
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
  const chainActive = pipelineActive(env?.pipeline);
  const locked = busy || env?.busy || chainActive;
  const steps = repositorySteps;
  const stepIndex = steps.findIndex(([key]) => key === section);
  const help: Record<string, string> = {
    environment: '下载源码、准备依赖与运行测试；在这里处理环境故障、执行日志和定时巡检。',
    analysis: '查看模型分析进度、报告与任务验收范围。发现的问题统一进入 Issue 页。',
    issues: '统一查看 GitHub Issue 和项目分析发现，调查问题或标记处置结果；修复进度请进入「修复」。',
    repair: '查看有效分析发现的代码修复、补丁、测试和独立审查结果。',
    pr: '查看已创建的 PR、关联 Issue 与发布失败；合并状态请以 GitHub 为准。',
  };
  return <section className="execution-panel" aria-labelledby="execution-title">
    <div className="section-heading"><h2 id="execution-title"><Terminal size={18} />{repo.name}</h2></div>
    {env && <div className="repository-context">      <label className="execution-version">当前源码版本<select value={selected} onChange={e => {
        selectedRef.current = e.target.value; setSelected(e.target.value); setIssueAnalysisId(''); void perform(refresh);
      }}>
        {environments.map(item => <option value={item.id} key={item.id}>
          {new Date(item.created * 1000).toLocaleString('zh-CN')} · {item.revision?.slice(0, 12) ?? '待下载'} · {environmentState(item)}
        </option>)}
      </select></label>
</div>}
    <nav className="section-nav process-nav" aria-label="仓库处理流程">{steps.map(([key, label], index) => <button key={key} aria-current={section === key ? 'step' : undefined} className={section === key ? 'active' : ''} onClick={() => setSection(key)}><span>{index + 1}</span>{label}</button>)}</nav>
    <p className="section-intro">{help[section]}</p>
    {error && <div className="error-inline" role="alert">{error}</div>}
    <div hidden={section !== 'environment'} aria-label="环境准备内容">
      <div className="execution-actions">      <button className="button primary" disabled={busy || env?.busy || chainActive}
        onClick={() => void perform(async () => {
          const created = await api<Environment>(`/repositories/${repo.id}/environments`, { auto_run: autoRun });
          selectedRef.current = created.id; setSelected(created.id); setTab('logs');
        })}><Download size={15} />下载最新版本</button>
</div>
    <details className="disclosure"><summary>下载后的执行选项</summary><label className="checkbox-label"><input type="checkbox" checked={autoRun} disabled={busy || chainActive}
      onChange={e => setAutoRun(e.target.checked)} />下载后自动准备环境、测试、启动并分析</label>
    <p className="diagnosis-meta">自动流程沿用本仓库已保存的安装联网选项；缺命令或执行失败会暂停，不会跳过测试。</p>
    <div className="execution-engine">
      <span className={`tag ${readiness?.ready ? 'green' : 'neutral'}`}>
        {readiness ? readiness.ready ? 'Docker 已就绪' : 'Docker 未就绪' : '正在检查 Docker'}
      </span>
      <button className="icon-button" title="重新检查 Docker" disabled={busy}
        onClick={() => void perform(checkEngine)}><RefreshCw size={15} /></button>
      {readiness && !readiness.ready && <span>{errorText(readiness.reason)}</span>}
      <span>模型诊断：{readiness?.diagnosis_model?.enabled ? readiness.diagnosis_model.model : '未配置'}</span>
    </div>
    </details>

      {!env ? <div className="empty"><Download size={28} /><p>尚未下载源码，点击「下载最新版本」开始。</p></div> : <>
      <div className="execution-facts">
        <span className="tag neutral">{environmentState(env)}</span>
        {env.busy && <span><Loader2 size={14} className="spin" />{env.jobs.some(j => j.cancel && j.state === 'running') ? '正在停止' : '任务处理中'}</span>}
        {env.revision && <code title={env.revision}>{env.revision}</code>}
        {env.installed && <span>依赖已准备</span>}
        {env.test_result && <span>最近测试：{env.test_result.exit_code === 0 ? '通过' : `未通过（${env.test_result.exit_code}）`}</span>}
      </div>
      {environments.some(item => item.reused_environment_id === env.id) &&
        <p role="status">源码版本未变化，已复用原环境及其配置，无需重复安装。</p>}
      {env.preparation === 'dependencies_reused' &&
        <p role="status">依赖未变化，已从只读快照准备独立环境；请重新测试新源码。</p>}
      {env.preparation === 'full_install' &&
        <p className="diagnosis-meta">本版本已完成完整安装；仅兼容且已保存的依赖快照可复用。</p>}
      {env.error && <div className="error-inline" role="status">
        {env.error === 'docker_unavailable' && '上次操作失败：'}{errorText(env.error)}</div>}
      {env.pipeline && <div className="execution-diagnosis" aria-label="自动执行流水线">
        <div className="section-heading"><h3>自动执行流水线</h3>
          <span className="tag neutral">{pipelineStates[env.pipeline.state] ?? env.pipeline.state}</span></div>
        <p role="status">当前步骤：{pipelineStages[env.pipeline.stage] ?? env.pipeline.stage}</p>
        <p className="diagnosis-meta">{env.config?.project_profile
          ? '下载 → 离线环境 → 兼容测试 → 源码分析（不启动桌面、不检查地址）'
          : '下载 → 准备环境／复用依赖 → 测试 → 启动 → 检查地址 → 分析'}</p>
        {env.config?.run_mode === 'test_only' && <p>仅测试项目：明确跳过服务启动和访问检查。</p>}
        {env.config?.project_profile && env.pipeline.state === 'completed' &&
          <p>离线测试与源码分析已完成；Windows 桌面功能仍未验收。</p>}
        {env.pipeline.error && <p>{errorText(env.pipeline.error)}</p>}
        {env.pipeline.state === 'waiting_model' && <p>请配置分析所需角色的模型并启用模型工作流，流水线将自动继续。</p>}
        {env.pipeline.state === 'waiting_configuration' &&
          <button className="button secondary" onClick={() => { setTab('config'); setSection('environment'); }}>补齐运行配置</button>}
      </div>}
      {env.access_result && <p role="status">访问地址已检查：HTTP {env.access_result.status_code}
        {env.access_result.authentication_required ? '（项目需要登录）' : ''}</p>}
      {env.config?.project_profile && <DesktopCoverage profile={env.config.project_profile}
        scope={env.test_result?.desktop_scope} />}
      <FailureDiagnosis key={`${env.id}:${env.diagnosis?.job_id}:${env.diagnosis?.current}`}
        env={env} locked={!!locked} onApply={() => void perform(() => api(
          `/environments/${env.id}/diagnoses/${env.diagnosis!.job_id}/apply`, { version: env.version },
        ))} />
      <div className="execution-actions">
        <button className="button primary" disabled={busy || env.busy || chainActive || !env.revision || env.state === 'deleted'}
          onClick={() => void perform(() => api(`/environments/${env.id}/pipeline`, { version: env.version }))}>
          <Play size={15} />从当前结果继续流水线</button>
        <button className="button secondary" disabled={locked || running || !env.config?.detected || !readiness?.ready || env.state === 'deleted' ||
          (!!env.config?.project_profile && !env.config.desktop_approved)}
          onClick={() => void action('setup')}><Download size={15} />安装环境</button>
        <button className="button secondary" disabled={locked || running || !env.installed || !env.config?.test || !readiness?.ready || env.state === 'deleted'}
          onClick={() => void action('test')}><FlaskConical size={15} />执行测试</button>
        <button className="button secondary" disabled={locked || running || !env.installed || !env.config?.start || !readiness?.ready || env.state === 'deleted' || !!env.config?.project_profile}
          onClick={() => void action('start')}><Play size={15} />启动项目</button>
        <button className="icon-button" title="停止项目或任务" disabled={busy || (!env.busy && !running && !chainActive)}
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
        <div className="execution-jobs">{env.jobs.map(job => <details key={job.id}>
          <summary>{actions[job.action]} · {{ queued: '排队中', running: '执行中', completed: '已完成', failed: '未完成' }[job.state]}</summary>
          {job.error && <p className="error-inline">{errorText(job.error)}</p>}
          <pre>{job.log || (job.state === 'running' ? '正在执行，完成后更新日志。' : '暂无命令输出')}</pre>
        </details>)}</div>}

      </>}
      <details className="disclosure"><summary>定时巡检配置</summary><SchedulePanel repo={repo} api={api} /></details>
    </div>
    <div hidden={!['analysis', 'issues'].includes(section)}>
      <div hidden={section !== 'issues'}>
        <div className="section-heading"><h2><CircleDot size={18} />Issue</h2></div>
        <p className="diagnosis-meta">来源标记用于区分 GitHub 已有问题与本地分析发现；分析发现不会仅因展示在此处而自动提交到 GitHub。</p>
        <nav className="section-nav secondary-nav" aria-label="Issue 来源">
          {([['all', '全部'], ['github', 'GitHub Issue'], ['analysis', '分析发现']] as const).map(([key, label]) =>
            <button key={key} className={issueSource === key ? 'active' : ''} aria-pressed={issueSource === key}
              onClick={() => setIssueSource(key)}>{label}</button>)}
        </nav>
        <div hidden={issueSource === 'analysis'}>
          {!env?.revision && <p className="action-hint">请先到「环境准备」下载源码，再分析 GitHub Issue。</p>}
          <IssuesPanel repo={repo} api={api} environment={env ?? null}
            onAnalysisSubmitted={id => { setIssueAnalysisId(id); setSection('analysis'); }} />
        </div>
        {!env && issueSource !== 'github' && <p className="action-hint">尚无分析发现，请先准备源码并完成项目分析。</p>}
      </div>
      {env && <div hidden={section === 'issues' && issueSource === 'github'}>
        <AnalysisPanel key={env.id} envId={env.id} version={env.version} api={api}
          view={section === 'issues' ? 'findings' : 'analysis'} onNavigate={next => {
            setSection(repositorySection(next, 'issues'));
            if (next === 'issues' || next === 'reproduction') setIssueSource('analysis');
          }} focusAnalysisId={issueAnalysisId} pipelineActive={chainActive && env.pipeline?.stage !== 'analysis'} />
      </div>}
    </div>
    {env ? <>
      <div hidden={!['repair', 'pr'].includes(section)}>
        <RepairPanel key={env.id} envId={env.id} api={api} focusTaskId={initialRepairId} view={section === 'pr' ? 'pr' : 'repair'} onNavigate={setSection} />
      </div>
    </> : !['environment', 'issues'].includes(section) && <div className="empty"><p>尚无源码版本，请先完成环境准备。</p><button className="button secondary" onClick={() => setSection('environment')}>前往环境准备</button></div>}
    <div className="process-footer">
      <button className="button secondary" disabled={stepIndex === 0} onClick={() => setSection(steps[stepIndex - 1][0])}>← 上一步</button>
      <span>{stepIndex + 1} / {steps.length} · {steps[stepIndex][1]}</span>
      <button className="button secondary" disabled={stepIndex === steps.length - 1} onClick={() => setSection(steps[stepIndex + 1][0])}>下一步 →</button>
    </div>
  </section>;
}
