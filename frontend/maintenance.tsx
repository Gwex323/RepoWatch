import { useCallback, useEffect, useState } from 'react';
import { ExternalLink, RefreshCw, RotateCcw } from 'lucide-react';

type Api = <T>(path: string, body?: unknown) => Promise<T>;
type Config = { enabled: boolean; version: number; model_ready: boolean;
  repository: string; account: string; merge: string };
type Link = { number: number; url: string };
type Task = { id: string; version: number; occurrence_id: string; state: string; error: string | null;
  can_reconcile: boolean; can_retry_publication: boolean;
  branch: string | null; base: string | null; requests: number; issue: Link | null; remote: Link | null;
  diagnosis: { title: string; root_cause: string; classification: string } | null;
  artifacts: { diff: string; approved_digest?: string; verification: { passed?: boolean } | null;
    review: { approved: boolean; summary: string } | null } | null;
  budget: { requests: number; tokens: number; seconds: number } };
const states: Record<string, string> = { running: '维护中', not_internal: '未确认内部缺陷', blocked: '维护受阻',
  pr_created: '待所有者处理 PR', linked_pr: '已关联现有 PR', waiting_deployment: '已合并，待部署验证' };
const errors: Record<string, string> = { maintenance_model_required: '修复与独立审查模型尚未就绪。',
  maintenance_identity_required: '当前 GitHub 账号必须为 Gwex323。',
  maintenance_repository_permission_required: '维护仓库身份或写入权限不满足要求。',
  maintenance_configuration_changed: '配置已变化，请刷新。',
  maintenance_basis_changed: '授权或来源依据已变化，维护已停止。',
  maintenance_non_internal_category: '此类来源故障不能直接证明自身实现缺陷。',
  maintenance_internal_evidence_required: '缺少与来源事实对应的精确实现证据。',
  maintenance_related_work_requires_review: '发现可能相关的已有工作，需要核对根因与验收范围。',
  maintenance_upstream_changed: '默认分支已变化，旧补丁不再发布。',
  maintenance_interrupted: '维护被中断，已保留工作区和远程回执。' };

export function MaintenancePanel({ api, faultId }: { api: Api; faultId: string }) {
  const [config, setConfig] = useState<Config | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const load = useCallback(async () => {
    const values = await Promise.all([api<Config>('/system/maintenance'),
      api<Task[]>(`/system/maintenance/tasks${faultId ? `?fault_id=${encodeURIComponent(faultId)}` : ''}`)]);
    return { config: values[0], tasks: values[1] };
  }, [api, faultId]);
  useEffect(() => {
    let active = true;
    setTasks([]);
    const refresh = () => { void load().then(value => {
      if (active) { setConfig(value.config); setTasks(value.tasks); }
    }).catch(e => { if (active) setError(errors[e.message] ?? e.message); }); };
    refresh(); const timer = setInterval(refresh, 3000);
    return () => { active = false; clearInterval(timer); };
  }, [load]);
  const toggle = async (enabled: boolean) => {
    if (!config) return;
    setBusy(true); setError('');
    try { setConfig(await api<Config>('/system/maintenance/settings', { version: config.version, enabled })); }
    catch (e) { const code = (e as Error).message; setError(errors[code] ?? code); }
    finally { setBusy(false); }
  };
  const action = async (task: Task, command: string) => {
    setBusy(true); setError('');
    try { await api(`/system/maintenance/tasks/${task.id}/${command}`, { version: task.version });
      const value = await load(); setConfig(value.config); setTasks(value.tasks); }
    catch (e) { const code = (e as Error).message; setError(errors[code] ?? code); }
    finally { setBusy(false); }
  };
  return <section className="coordination-panel" aria-label="自身代码维护">
    <div className="section-heading"><h2>自身代码维护</h2><span className="tag neutral">{config?.enabled ? '已授权' : '未启用'}</span>
      <button className="icon-button" title="刷新维护状态" disabled={busy} onClick={() => {
        void load().then(value => { setConfig(value.config); setTasks(value.tasks); }).catch(e => setError(e.message));
      }}><RefreshCw size={16} /></button></div>
    {config && <><div className="execution-actions"><label className="checkbox-label">
      <input type="checkbox" checked={config.enabled} disabled={busy || !config.enabled && !config.model_ready}
        onChange={e => void toggle(e.target.checked)} />授权维护新发生的内部缺陷</label></div>
      <dl className="fault-metadata"><div><dt>维护仓库</dt><dd>{config.repository}</dd></div>
        <div><dt>写入身份</dt><dd>{config.account}</dd></div><div><dt>合并与部署</dt><dd>仅所有者合并；不自动部署</dd></div></dl></>}
    {error && <p className="error-inline" role="alert">{error}</p>}
    {tasks.length === 0 ? <p className="diagnosis-meta">暂无维护任务</p> : tasks.map(task => <article className="coordination-job" key={task.id}>
      <div className="section-heading"><h3>{task.diagnosis?.title ?? states[task.state] ?? task.state}</h3>
        <span className="tag neutral">{states[task.state] ?? task.state}</span>
        {task.can_reconcile && <button className="icon-button" title="核对远程回执" disabled={busy}
          onClick={() => void action(task, 'reconcile')}><RefreshCw size={15} /></button>}
        {task.can_retry_publication && <button className="icon-button" title="重试已审查补丁的发布" disabled={busy}
          onClick={() => void action(task, 'retry-publication')}><RotateCcw size={15} /></button>}</div>
      <p>{task.diagnosis?.root_cause}</p><dl className="fault-metadata">
        <div><dt>任务</dt><dd><code>{task.id}</code></dd></div><div><dt>分支</dt><dd><code>{task.branch ?? '尚未创建'}</code></dd></div>
        <div><dt>验证</dt><dd>{task.artifacts?.verification?.passed ? '通过' : '未通过或未执行'}</dd></div>
        <div><dt>独立 review</dt><dd>{task.artifacts?.review?.approved ? '通过' : '未通过或未执行'}</dd></div>
        <div><dt>基础提交</dt><dd><code>{task.base ?? '未知'}</code></dd></div></dl>
      <div className="execution-actions">{task.issue && <a href={task.issue.url} target="_blank" rel="noreferrer">Issue #{task.issue.number} <ExternalLink size={13} /></a>}
        {task.remote && <a href={task.remote.url} target="_blank" rel="noreferrer">PR #{task.remote.number} <ExternalLink size={13} /></a>}</div>
      <p className="diagnosis-meta">维护请求 {task.requests} · 根任务请求 {task.budget.requests} · 已报告令牌 {task.budget.tokens} · 耗时 {Math.ceil(task.budget.seconds)} 秒</p>
      {task.artifacts?.diff && <details><summary>补丁与 review</summary><pre>{task.artifacts.diff}</pre><p>{task.artifacts.review?.summary}</p></details>}
      {task.error && <p className="error-inline">{errors[task.error] ?? task.error}</p>}
    </article>)}
  </section>;
}
