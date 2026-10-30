import { useCallback, useEffect, useState } from 'react';
import { Check, RefreshCw } from 'lucide-react';
import { type ProviderCatalog, type ProviderChoice } from './model-providers';

type Api = <T>(path: string, body?: unknown) => Promise<T>;
type Config = { enabled: boolean; version: number; model_ready: boolean;
  fallback: ProviderChoice | null; fallback_authorized: boolean; max_requests: number | null };
type Job = { id: string; occurrence_id: string; state: string; version: number; error: string | null;
  plan: { diagnosis: string; facts: string[]; unknowns: string[]; expected: string; action: string } | null;
  requests: number; receipt: { state: string; task_id?: string } | null;
  budget: { requests: number; tokens: number; seconds: number; cost: number | null } };
export type CoordinationNotice = { id: number; state: string; created: number; job_id: string;
  error: string | null; fault_id: string };
export const coordinationStates: Record<string, string> = { diagnosing: '诊断中', waiting: '等待原步骤验证',
  blocked: '最终受阻', needs_authorization: '需要新增授权', completed: '原步骤已验证', cancelled: '已停止' };
const actions: Record<string, string> = { retry_repair: '按原策略续办修复', wait: '等待来源验证',
  request_authorization: '请求新增授权', blocked: '保留受阻状态' };
const errors: Record<string, string> = { coordination_model_required: '请先配置并启用系统协调模型。',
  coordination_configuration_changed: '协调配置已变化，请刷新后重试。',
  coordination_basis_changed: '执行依据或授权已变化，旧计划已停止。',
  coordination_model_unavailable_or_invalid: '协调模型不可用或计划无效，已降级为规则处理。',
  coordination_budget_exhausted: '协调诊断额度已用完。', no_authorized_action: '没有可安全执行的已授权动作。',
  coordination_source_inactive: '来源任务已结束或停止。',
  coordination_source_budget_exhausted: '来源流程的请求额度或期限已用完。',
  source_verification_timeout: '等待原步骤验证已超时。',
  authorization_required: '后续操作需要新增授权。', source_not_verified: '原步骤仍未通过验证。',
  coordination_interrupted: '诊断被中断，保留未知执行结果。' };
const explain = (code: string) => errors[code] ?? code;

export function CoordinationPanel({ api, faultId }: { api: Api; faultId: string }) {
  const [config, setConfig] = useState<Config | null>(null);
  const [catalog, setCatalog] = useState<ProviderCatalog | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const load = useCallback(async () => {
    const [value, providers, rows] = await Promise.all([
      api<Config>('/system/coordination'), api<ProviderCatalog>('/model-providers'),
      api<Job[]>(`/system/coordination/jobs${faultId ? `?fault_id=${encodeURIComponent(faultId)}` : ''}`),
    ]);
    return { value, providers, rows };
  }, [api, faultId]);
  useEffect(() => {
    let active = true;
    setJobs([]);
    const refresh = () => { void load().then(({ value, providers, rows }) => {
      if (active) { setConfig(value); setCatalog(providers); setJobs(rows); }
    }).catch(e => { if (active) setError(explain(e.message)); }); };
    refresh(); const timer = setInterval(refresh, 3000);
    return () => { active = false; clearInterval(timer); };
  }, [load]);
  const save = async (changes: Partial<Config>) => {
    if (!config) return;
    const previous = config;
    const next = { ...config, ...changes };
    setBusy(true); setError(''); setConfig(next);
    try { setConfig(await api<Config>('/system/coordination/settings', { version: previous.version,
      enabled: next.enabled, fallback: next.fallback, fallback_authorized: next.fallback_authorized })); }
    catch (e) { setConfig(previous); setError(explain((e as Error).message)); }
    finally { setBusy(false); }
  };
  return <section className="coordination-panel" aria-label="系统协调">
    <div className="section-heading"><h2>系统协调</h2><span className="tag neutral">{config?.model_ready ? '协调模型就绪' : '协调模型未配置或未启用'}</span>
      <button className="icon-button" title="刷新协调状态" disabled={busy} onClick={() => {
        void load().then(({ value, rows }) => { setConfig(value); setJobs(rows); }).catch(e => setError(explain(e.message)));
      }}><RefreshCw size={16} /></button></div>
    {config && <><div className="execution-actions"><label className="checkbox-label">
      <input type="checkbox" checked={config.enabled} disabled={busy || !config.model_ready && !config.enabled}
        onChange={e => void save({ enabled: e.target.checked })} />协调新发生的系统故障</label>
      <span className="diagnosis-meta">{config.max_requests === null ? '诊断请求不限额，保留累计用量' : `诊断请求上限 ${config.max_requests} 次 / 根任务`}</span></div>
      <details><summary>备用路由与授权</summary><div className="execution-config">
        <label>协调备用模型<select aria-label="协调备用模型" disabled={busy}
          value={config.fallback ? JSON.stringify([config.fallback.provider_id, config.fallback.model]) : ''}
          onChange={e => { const [provider_id, model] = e.target.value ? JSON.parse(e.target.value) : [];
            void save({ fallback: provider_id ? { provider_id, model } : null, fallback_authorized: false }); }}>
          <option value="">不使用备用路由</option>{catalog?.providers.map(provider => <optgroup key={provider.id} label={provider.name}>
            {provider.models.map(model => <option key={model.model} value={JSON.stringify([provider.id, model.model])}>{model.model}</option>)}
          </optgroup>)}</select></label>
        <label className="checkbox-label"><input type="checkbox" checked={config.fallback_authorized}
          disabled={busy || !config.fallback} onChange={e => void save({ fallback_authorized: e.target.checked })} />授权主路由失败后使用该备用模型</label>
      </div></details></>}
    {error && <p className="error-inline" role="alert">{error}</p>}
    {jobs.length === 0 ? <p className="diagnosis-meta">暂无协调任务</p> : <div className="coordination-jobs">{jobs.map(job => <article className="coordination-job" key={job.id}>
      <div className="section-heading"><h3>{coordinationStates[job.state] ?? job.state}</h3><code>{job.id}</code></div>
      {job.plan && <><p>{job.plan.diagnosis}</p><dl className="fault-metadata">
        <div><dt>下一动作</dt><dd>{actions[job.plan.action] ?? job.plan.action}</dd></div>
        <div><dt>预期验证</dt><dd>{job.plan.expected}</dd></div>
        <div><dt>事实引用</dt><dd>{job.plan.facts.join('、')}</dd></div>
        <div><dt>未知项</dt><dd>{job.plan.unknowns.join('；') || '无'}</dd></div>
      </dl></>}
      <p className="diagnosis-meta">诊断 {job.requests} 次 · 根任务请求 {job.budget.requests} 次 · 已报告令牌 {job.budget.tokens} · 耗时 {Math.ceil(job.budget.seconds)} 秒 · 费用 {job.budget.cost === null ? '未知' : job.budget.cost}</p>
      {job.receipt && <p><Check size={14} />{job.receipt.state === 'queued' ? '已排队，尚未验证通过' : job.receipt.state}</p>}
      {job.error && <p className="error-inline">{explain(job.error)}</p>}
    </article>)}</div>}
  </section>;
}
