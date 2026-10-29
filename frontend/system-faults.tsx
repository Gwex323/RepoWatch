import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowLeft, ArrowRight, RefreshCw } from 'lucide-react';
import { CoordinationPanel } from './coordination';
import { MaintenancePanel } from './maintenance';
import { ReleasesPanel } from './releases';

type Api = <T>(path: string, body?: unknown) => Promise<T>;
type Repo = { id: string; name: string };
type Page<T> = { items: T[]; total: number; limit: number; offset: number };
type Group = {
  id: string; code: string; flow: string; stage: string | null; role: string | null;
  occurrences: number; remaining: number; state: string; repo_id: string | null;
  first_seen: number; last_seen: number; historical: boolean; category: string;
};
type Occurrence = {
  id: string; version: number; state: string; occurred: number; source_event_key: string;
  task_id: string; root_task_id: string | null; attempt: number | null; flow: string;
  role: string | null; stage: string | null; repo_id: string | null; env_id: string | null;
  account_id: number | null; code: string; category: string; execution: string;
  revision: string | null; workbench_version: string | null; trace_id: string | null;
  historical: boolean; diagnostic: Record<string, unknown>; budget: Record<string, unknown>;
  model_binding: Record<string, unknown>;
};
type Detail = { id: string; items: Occurrence[]; occurrences: number; remaining: number; state: string };
type History = { id: number; occurrence_id: string; state: string; actor: string; reason: string;
  evidence: string | null; created: number; details: Record<string, unknown> };
type Health = { enabled: boolean | null; version: number; error: string | null; pending: number | null;
  sources: { source: string; error: string | null; failures: number; last_success: number | null }[] };
type Recovery = { enabled: boolean; version: number; max_requests: number; max_seconds: number };

const states: Record<string, string> = {
  pending: '待诊断', diagnosing: '诊断中', recovering: '恢复中', verifying: '验证中',
  resolved: '已恢复', blocked: '受阻', dismissed: '已关闭',
};
const flows: Record<string, string> = {
  analysis: '项目分析', execution: '环境执行', diagnosis: '环境诊断', repair: '代码修复与审查',
  acceptance: '自动验收', publication: '远程发布', service: '工作台服务',
};
const categories: Record<string, string> = {
  model_timeout: '模型等待超时', protocol: '调用或输出约束', external_service: '外部服务',
  environment: '运行环境', target_test: '目标项目测试失败', review_rejection: '独立审查未通过',
  unknown: '原因待诊断',
};
const roles: Record<string, string> = { repository_analyst: '仓库分析', problem_investigator: '问题调查',
  reproduction_assistant: '复现辅助', independent_reviewer: '独立审查', repair_agent: '代码修复' };
const fields: Record<string, string> = {
  role: '智能体角色', stage: '流程阶段', attempt: '来源尝试', attempt_limit: '尝试上限',
  account_id: '账号引用', revision: '目标源码版本', workbench_version: '工作台版本',
  trace_id: '追踪引用', model_calls: '累计模型请求', max_model_calls: '模型请求上限',
  deadline: '来源截止时间', remaining_seconds: '剩余时间（秒）', retryable: '来源允许重试',
  calls: '调用数量', matching: '匹配数量', other: '其他调用', allowed: '允许上限',
  reason: '检查原因', recovery: '恢复决定', required: '前置要求', corrections_remaining: '剩余纠正次数',
  accepted: '是否通过', timeout_kind: '超时类型', request_timeout_seconds: '单次等待上限（秒）',
  request_elapsed_seconds: '单次请求耗时（秒）', attempt_elapsed_seconds: '整次尝试耗时（秒）',
  timing_scope: '耗时口径', stop_reason: '停止原因', requests: '累计请求', max_requests: '请求上限',
  resumptions: '累计续办', max_resumptions: '续办上限', model: '实际模型', model_version: '模型版本',
  provider_id: '供应商引用', provider_version: '供应商版本', configuration_version: '配置版本',
  path: '字段路径', type: '违反约束', expected: '预期', actual_type: '实际类型',
  expected_type: '预期类型', truncated: '诊断已截断', exception_type: '异常类型',
  fields: '出错字段', issues: '字段约束',
  timeouts: '已用超时重试', outputs: '已用输出纠正', verification_corrections: '已用验证返工',
  review_corrections: '已用审查返工', tool_selection_corrections: '已用工具选择纠正',
};
const values: Record<string, string> = {
  too_many_calls: '调用数量超过上限', request_mismatch: '调用不匹配，已停止', unknown: '未知',
  scope: '登记任务范围', source: '读取源码', execution: '读取执行记录',
  request: '单次请求', attempt: '整次尝试',
};
const stamp = (value: number) => new Date(value * 1000).toLocaleString('zh-CN');
const show = (value: unknown): string => value === null || value === undefined ? '未知' :
  typeof value === 'boolean' ? value ? '是' : '否' : values[String(value)] ?? String(value);

function Metadata({ data }: { data: Record<string, unknown> }) {
  return Object.keys(data).length ? <dl className="fault-metadata">{Object.entries(data).map(([key, value]) =>
    <div key={key}><dt>{fields[key] ?? key}</dt><dd>{typeof value === 'object' && value !== null
      ? <pre>{JSON.stringify(value, null, 2)}</pre> : show(value)}</dd></div>)}</dl> :
    <p className="action-hint">来源未记录，未知</p>;
}

export function SystemFaults({ api, repositories, onSource }: {
  api: Api; repositories: Repo[];
  onSource: (repoId: string, envId: string, flow: string, taskId: string) => void;
}) {
  const initial = new URLSearchParams(location.hash.split('?')[1] ?? '');
  const [repo, setRepo] = useState(initial.get('repo_id') ?? '');
  const [task, setTask] = useState(initial.get('task_id') ?? '');
  const [flow, setFlow] = useState('');
  const [state, setState] = useState('');
  const [offset, setOffset] = useState(0);
  const [list, setList] = useState<Page<Group> | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [recovery, setRecovery] = useState<Recovery | null>(null);
  const [selected, setSelected] = useState(initial.get('fault_id') ?? '');
  const [detail, setDetail] = useState<Detail | null>(null);
  const [detailOffset, setDetailOffset] = useState(0);
  const [timelineOffset, setTimelineOffset] = useState(0);
  const [timeline, setTimeline] = useState<Page<History> | null>(null);
  const [reason, setReason] = useState('');
  const [closing, setClosing] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const queryRef = useRef('');
  const query = new URLSearchParams({ limit: '20', offset: String(offset) });
  if (repo) query.set('repo_id', repo);
  if (task) query.set('task_id', task);
  if (flow) query.set('flow', flow);
  if (state) query.set('state', state);
  const queryString = query.toString();
  queryRef.current = queryString;
  const refresh = useCallback(async () => {
    const [next, status, policy] = await Promise.all([
      api<Page<Group>>(`/system/faults?${queryString}`), api<Health>('/system/faults/health'),
      api<Recovery>('/system/recovery'),
    ]);
    if (queryRef.current === queryString) { setList(next); setHealth(status); setRecovery(policy); }
  }, [api, queryString]);
  useEffect(() => {
    let alive = true;
    const load = () => { if (alive) void refresh().catch(e => { if (alive) setError(e.message); }); };
    load(); const timer = setInterval(load, 4000);
    return () => { alive = false; clearInterval(timer); };
  }, [refresh]);
  useEffect(() => {
    if (!selected) { setDetail(null); return; }
    let alive = true;
    void Promise.all([
      api<Detail>(`/system/faults/${selected}?limit=20&offset=${detailOffset}`),
      api<Page<History>>(`/system/faults/${selected}/history?limit=20&offset=${timelineOffset}`),
    ]).then(([value, history]) => { if (alive) { setDetail(value); setTimeline(history); } })
      .catch(e => { if (alive) setError(e.message); });
    return () => { alive = false; };
  }, [api, selected, detailOffset, timelineOffset, list]);
  const action = async (fn: () => Promise<unknown>) => {
    setBusy(true); setError('');
    try { await fn(); await refresh(); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };
  const changeRecovery = (enabled: boolean) => {
    if (!recovery) return;
    const previous = recovery;
    setRecovery({ ...previous, enabled });
    void action(async () => {
      try { await api('/system/recovery/settings', { version: previous.version, enabled }); }
      catch (e) { setRecovery(previous); throw e; }
    });
  };
  const reset = () => { setOffset(0); setSelected(''); setDetailOffset(0); setTimelineOffset(0); };
  return <section className="fault-page" aria-label="统一故障记录">
    <div className="fault-health" role="status">
      <div><strong>{health?.error || health?.sources.some(item => item.error) ? '故障采集异常' :
        health?.enabled === false ? '故障采集已停用' : '本地故障台账'}</strong>
        <small>待入账 {health?.pending ?? '未知'} 条{health?.error && ` · ${health.error}`}</small></div>
      <div className="execution-actions"><button className="button secondary" disabled={busy}
        onClick={() => void action(refresh)}><RefreshCw size={15} />刷新</button>
        <button className="button secondary" disabled={busy || !health?.enabled}
          onClick={() => void action(() => api('/system/faults/import-history', { limit: 200 }))}>补录一批历史故障</button></div>
    </div>
    {recovery && <div className="execution-actions">
      <label className="checkbox-label"><input type="checkbox" checked={recovery.enabled} disabled={busy}
        onChange={e => changeRecovery(e.target.checked)} />自动恢复新发生的流程故障</label>
      <span className="action-hint">根任务上限：{recovery.max_requests} 次请求 / {recovery.max_seconds} 秒</span>
    </div>}
    {health && <div className="execution-actions"><button className="text-link" disabled={busy}
      onClick={() => void action(() => api('/system/faults/settings', { version: health.version, enabled: !health.enabled }))}>
      {health.enabled ? '停用故障采集' : '启用故障采集'}</button>
      <details><summary>采集健康详情</summary>{health.sources.map(item => <p key={item.source}>
        {item.source} · 累计采集异常 {item.failures} 次 · {item.error ?? '正常'} · 最近成功 {item.last_success ? stamp(item.last_success) : '尚未采集'}
      </p>)}</details></div>}
    <CoordinationPanel api={api} faultId={selected} />
    <MaintenancePanel api={api} faultId={selected} />
    <ReleasesPanel api={api} />
    {error && <p className="error-inline" role="alert">{error}</p>}
    <div className="fault-filters">
      <label>仓库<select aria-label="仓库" value={repo} onChange={e => { setRepo(e.target.value); reset(); }}>
        <option value="">全部可访问仓库</option>{repositories.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
      </select></label>
      <label>流程<select aria-label="流程" value={flow} onChange={e => { setFlow(e.target.value); reset(); }}><option value="">全部流程</option>
        {Object.entries(flows).map(([key, value]) => <option key={key} value={key}>{value}</option>)}
      </select></label>
      <label>处理状态<select aria-label="处理状态" value={state} onChange={e => { setState(e.target.value); reset(); }}><option value="">全部状态</option>
        {Object.entries(states).map(([key, value]) => <option key={key} value={key}>{value}</option>)}
      </select></label>
      <label>来源任务<input aria-label="来源任务" value={task} maxLength={100} placeholder="任务 ID，可留空"
        onChange={e => { setTask(e.target.value); reset(); }} /></label>
    </div>
    <div className={`fault-layout ${selected ? 'has-detail' : ''}`}>
      <div>
        <div className="list-topline"><span>{list?.total ?? 0} 组故障</span><span>次数按来源事件计数</span></div>
        {list?.items.map(item => <button className={`fault-row ${selected === item.id ? 'selected' : ''}`} key={item.id}
          onClick={() => { setSelected(item.id); setDetailOffset(0); setTimelineOffset(0); setClosing(''); }}>
          <div><strong>{categories[item.category] ?? item.category}</strong><span className="tag neutral">{states[item.state]}</span></div>
          <code>{item.code}</code><p>{repositories.find(repo => repo.id === item.repo_id)?.name ?? '工作台服务'} · {flows[item.flow]}
            {item.role && ` · ${roles[item.role] ?? item.role}`}</p>
          <p>出现 {item.occurrences} 次 · 剩余 {item.remaining} 次待处理{item.historical && ' · 含历史补录'}</p>
          <small>首次 {stamp(item.first_seen)}<br />最近 {stamp(item.last_seen)}</small>
        </button>)}
        {list?.total === 0 && <div className="empty"><p>当前范围没有故障记录</p><small>新故障会自动记录；旧记录需要显式补录。</small></div>}
        {!list && <p>正在读取故障记录…</p>}
        {list && <div className="execution-actions"><button className="button secondary" disabled={offset === 0}
          onClick={() => setOffset(Math.max(0, offset - 20))}>上一页</button><span>{offset + 1} / {Math.max(1, list.total)}</span>
          <button className="button secondary" disabled={offset + 20 >= list.total} onClick={() => setOffset(offset + 20)}>下一页</button></div>}
      </div>
      {selected && <aside className="fault-detail" aria-label="故障详情">
        <div className="section-heading"><h2>故障详情</h2><button className="text-link" onClick={() => setSelected('')}>收起</button></div>
        {detail?.id === selected ? <><p>{states[detail.state]} · {detail.occurrences} 次出现 · {detail.remaining} 次待处理</p>
          {detail.items.map(item => <article key={item.id} className="fault-occurrence">
            <h3>{stamp(item.occurred)} · {states[item.state]}{item.historical && ' · 历史补录'}</h3>
            <p className="fault-execution">{({ not_executed: '本批调用未执行', executed: '已执行，结果见原任务', unknown: '执行结果未知' })[item.execution] ?? '执行结果未知'}</p>
            <p><code>{item.source_event_key}</code></p>
            {item.repo_id && item.env_id && <button className="text-link" onClick={() =>
              onSource(item.repo_id!, item.env_id!, item.flow,
                ['analysis', 'acceptance'].includes(item.flow) ? item.root_task_id ?? item.task_id : item.task_id)}>
              查看来源任务 {item.task_id}<ArrowRight size={14} /></button>}
            <Metadata data={{ role: roles[item.role ?? ''] ?? item.role, stage: roles[item.stage ?? ''] ?? item.stage,
              attempt: item.attempt, account_id: item.account_id, revision: item.revision,
              workbench_version: item.workbench_version, trace_id: item.trace_id }} />
            <h4>字段诊断</h4><Metadata data={item.diagnostic} />
            <h4>本次实际模型绑定</h4><Metadata data={item.model_binding} />
            <h4>预算与停止原因</h4><Metadata data={item.budget} />
            {!['dismissed', 'resolved'].includes(item.state) && <>
              {closing === item.id ? <form onSubmit={e => { e.preventDefault(); void action(async () => {
                await api(`/system/faults/${detail.id}/occurrences/${item.id}/dismiss`, { version: item.version, reason });
                setClosing(''); setReason('');
              }); }}><label>关闭原因<textarea required maxLength={512} value={reason} onChange={e => setReason(e.target.value)} /></label>
                <p>关闭表示无需继续处理，不代表已修复，原任务保持原状态。</p><button className="button secondary" disabled={busy}>确认关闭记录</button></form> :
                <button className="text-link" onClick={() => { setClosing(item.id); setReason(''); }}>关闭此出现记录</button>}
            </>}
          </article>)}
          <div className="execution-actions"><button className="text-link" disabled={detailOffset === 0} onClick={() => setDetailOffset(detailOffset - 20)}><ArrowLeft size={14} />上一批出现</button>
            <button className="text-link" disabled={detailOffset + 20 >= detail.occurrences} onClick={() => setDetailOffset(detailOffset + 20)}>下一批出现<ArrowRight size={14} /></button></div>
          <h3>处理时间线</h3><ol className="fault-timeline">{timeline?.items.map(item => <li key={item.id}>
            <strong>{states[item.state]} · {item.actor === 'owner' ? '用户' : '来源流程'}</strong><time>{stamp(item.created)}</time>
            <p>{values[item.reason] ?? item.reason}</p><small>{item.occurrence_id} · {item.evidence ?? '人工关闭，无修复证据'}</small>
            {Object.keys(item.details).length > 0 && <Metadata data={item.details} />}
          </li>)}</ol>
          <div className="execution-actions"><button className="text-link" disabled={timelineOffset === 0} onClick={() => setTimelineOffset(timelineOffset - 20)}>上一页历史</button>
            <button className="text-link" disabled={!timeline || timelineOffset + 20 >= timeline.total} onClick={() => setTimelineOffset(timelineOffset + 20)}>下一页历史</button></div>
        </> : <p>正在读取详情…</p>}
      </aside>}
    </div>
  </section>;
}
