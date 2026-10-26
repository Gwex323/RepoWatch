import { useEffect, useState } from 'react';
import { Check, FileSearch, Loader2, RefreshCw, X } from 'lucide-react';
import { agentModelEvent } from './agent-models';
import { TaskScopePanel, type TaskScope } from './task-scope';

type Api = <T>(path: string, body?: unknown) => Promise<T>;
type Finding = {
  id: string; status: string; version: number;
  data: {
    title: string; severity: string; explanation: string; expected: string; suspected_actual: string;
    reproduction: string[]; evidence: { path: string; start_line: number; end_line: number; quote: string }[];
  };
};
type HistoricalOutcome = 'still_present' | 'fixed' | 'suspected_false_positive' | 'not_covered';
type HistoricalReview = {
  total: number; included: number; omitted: number;
  items: {
    finding_id: string; version: number; human_status: string; analysis_id: string;
    revision: string; content_clipped: boolean; data: Finding['data'];
    result: { outcome: HistoricalOutcome; rationale: string; evidence: Finding['data']['evidence'] } | null;
    live_status?: string | null; live_version?: number | null;
  }[];
};
const historicalOutcomes: Record<HistoricalOutcome, string> = {
  still_present: '仍存在', fixed: '已修复',
  suspected_false_positive: '疑似误报', not_covered: '本轮未覆盖',
};
type Analysis = {
  id: string; status: string; error: string | null; version: number; revision: string;
  current: boolean; created: number; findings: Finding[];
  historical_review?: HistoricalReview | null;
  task_scope?: TaskScope;
  report_recovery?: { available: boolean };
  report: { summary: string; limitations: string[]; files_read: string[]; model?: string;
    execution_scope?: { kind: string; executed: number | null; skipped: number;
      uncovered: string[]; complete: boolean } } | null;
  events: { kind: string; agent: string; created: number }[];
  coverage: { captured: number; eligible: number; complete: boolean } | null;
  progress?: { stage: string | null; model_calls: number; source_reads: number };
  failure_details?: { stage: string | null; elapsed_seconds: number; http_status: number | null } | null;
  provider_response?: { finish_reason?: string; output_tokens?: number; reasoning_tokens?: number; truncated?: boolean } | null;
};
const statuses: Record<string, string> = {
  cancelled: '已取消', waiting_model: '等待模型配置', queued: '等待分析', running: 'Agent 分析中',
  completed: '分析已完成', failed: '分析未完成', interrupted: '分析已中断',
  superseded: '已被新执行结果替代',
  waiting_reproduction: '待人工复现', confirmed: '复现成功', not_reproduced: '未能复现',
  resolved: '已解决',
};
export const analysisErrorMessages: Readonly<Partial<Record<string, string>>> = {
  analysis_model_invalid: '请检查模型名称、接口类型和服务地址。',
  scope_review_incomplete: '范围审查遗漏验收项或版本不一致；原范围已保留，请继续分析与范围审查。',
  scope_mapping_invalid: '问题与验收项的关联不完整，请继续范围审查。',
  scope_changed: '任务范围已变化，请刷新后重新审查。',
  analysis_model_changed: '模型配置已变化，请刷新后重试；分析将使用当前配置重新执行。',
  analysis_model_key_required: '请填写分析模型的 API Key。',
  analysis_model_new_key_required: '更换服务地址时，请重新填写对应的 API Key。',
  analysis_model_key_unavailable: '无法读取模型密钥，请在「设置 → 模型与供应商」中重新保存。',
  secure_storage_unavailable: '本机系统凭据库不可用，密钥尚未保存。',
  analysis_execution_required: '先完成一次测试或启动，系统会自动安排分析。',
  environment_busy: '环境正在执行任务，完成后将自动安排分析。',
  environment_changed: '环境状态已变化，请刷新后重试。',
  analysis_stale: '源码或运行配置已变化，请查看新一轮分析。',
  analysis_changed: '分析状态已变化，请刷新后重试。',
  finding_changed: '该问题已经提交过反馈，请刷新。',
  personal_reproduction_required: '请由本人确认复现结果。',
  analysis_failed: '分析遇到未分类错误。请查看失败阶段和服务日志；旧记录可能没有详细原因。',
  analysis_timeout_or_stopped: '分析已超时或停止，可以重新尝试。',
  analysis_interrupted: '上次分析因服务中断而停止，可以重新尝试。',
  analysis_authentication_failed: '模型服务拒绝了密钥，请在「设置 → 模型与供应商」中更新。',
  analysis_rate_limited: '模型服务限流或额度不足，请检查账户后重试。',
  analysis_provider_timeout: '模型服务响应超时。请检查模型服务负载或网络；旧失败记录不会因配置更新而自动消失。',
  analysis_provider_timeout_exhausted: '模型请求超过等待时限，且本次允许的自动重试已用完。请检查模型服务负载或网络后再重试。',
  analysis_provider_unreachable: '无法连接模型服务，请检查地址与网络。',
  analysis_evidence_unverified: '模型返回的源码证据未通过校验，本次未保存问题，请重试。',
  analysis_review_incomplete: 'Agent 未完成规定的源码读取与交接，本次未保存问题，请重试。',
  analysis_execution_unread: 'Agent 未读取已保存的测试／启动记录，报告未保存。请重试分析。',
  analysis_source_unread: 'Agent 未成功读取任何源码，报告未保存。请重试分析。',
  analysis_handoff_incomplete: 'Agent 未完成规定的阶段交接，报告未保存。请重试分析。',
  analysis_execution_read_not_followed: '模型返回的执行记录读取请求未通过校验，分析已提前停止。请查看必读请求检查记录后重试分析，无需重新安装。',
  analysis_source_read_not_followed: '模型返回的源码读取请求未通过校验，分析已提前停止。请查看必读请求检查记录后重试分析，无需重新安装。',
  finding_closed: '该旧问题已记录处置结论，无需重复操作。',
  repair_running: '该问题正在修复中，暂不能改判；请等修复结束或停止任务。',
  resolution_note_required: '手动标记解决必须填写处置说明。',
  resolution_pr_not_merged: '修复 PR 尚未合并，请先在 GitHub 完成合并。',
  resolution_verification_failed: '无法核验修复 PR 的合并状态，请检查 GitHub 账号与网络后重试。',
  analysis_max_turns_exceeded: '分析已达到总调用轮数上限，尚未生成完整报告。',
  analysis_stage_budget_exceeded: '当前阶段已耗尽预算，尚未完成交接或报告。',
  analysis_handoff_not_followed: '已要求交接，但模型未返回有效的交接调用。请检查模型服务是否支持指定工具调用。',
  analysis_finalization_not_followed: '已要求提交报告，但模型仍尝试调用工具。请检查模型服务的工具选择支持。',
  analysis_model_output_invalid: '模型返回了无效的工具调用或输出格式，本次未保存问题。',
  analysis_output_truncated: '模型输出未能在有限额度内完整生成，扩容重试仍未完成，本次未保存问题。',
  analysis_provider_empty_response: '模型服务没有返回可用的结果，请检查服务兼容性和状态。',
  analysis_tool_arguments_invalid: '模型工具参数未通过 JSON 或字段校验，本次未执行该调用。',
  analysis_tool_arguments_exhausted: '模型工具参数仍不符合 JSON 或字段要求，自动纠正次数已用完，未执行未通过校验的调用。',
  analysis_tool_not_allowed: '模型请求了当前角色无权使用的工具，该调用已阻止。请查看角色与工具记录。',
  analysis_scope_unregistered: '仓库分析尚未成功登记任务范围，阶段预算已耗尽，本轮未进入后续处理。',
  analysis_scope_read_not_followed: '模型未按要求登记任务范围，本轮未进入后续处理。',
  analysis_json_invalid: '模型返回内容未通过 JSON 校验，本次未保存问题。',
  analysis_report_invalid: '模型报告字段不符合要求，本次未保存问题。',
  analysis_model_refused: '模型拒绝完成本次分析，尚未生成报告。',
  analysis_deadline_exceeded: '分析超过十分钟时限，已停止本轮调用。',
  analysis_service_stopped: '工作台服务停止，分析随之结束，可以重试。',
  analysis_model_disabled: '分析模型已被停用，当前任务停止。重新启用后可重试。',
  analysis_provider_request_rejected: '模型服务拒绝了请求参数，请检查工具调用、指定工具选择和结构化输出的兼容性。',
  analysis_provider_forbidden: '模型服务拒绝访问，请检查密钥权限和模型权限。',
  analysis_provider_not_found: '模型服务未找到接口或模型，请检查地址、接口类型和模型名称。',
  analysis_provider_error: '模型服务返回错误，请检查服务状态后重试。',
};
const errorMessage = (value: string) => analysisErrorMessages[value] ?? '操作未完成，请检查配置后重试。';
const names: Record<string, string> = {
  repository_analyst: '仓库分析', problem_investigator: '问题调查',
  reproduction_assistant: '复现辅助', independent_reviewer: '独立审查',
};
function eventLabel(item: Analysis['events'][number]) {
  const name = names[item.agent] ?? item.agent;
  if (item.kind === 'model_configuration') return agentModelEvent(item.agent);
  if (item.kind === 'attempt') return '开始新一次分析';
  if (item.kind === 'report_checkpoint_saved') return '已保存最终审查输入及真实读取、交接记录';
  if (item.kind === 'report_checkpoint_invalidated') return '已保存的审查依据发生变化，重新执行完整分析';
  if (item.kind === 'report_resumed') return '从已保存证据恢复最终报告，仅调用独立审查员';
  if (item.kind === 'report_budget') {
    try {
      const budget = JSON.parse(item.agent) as { max_tokens: number; reserve_seconds: number; review_items: number };
      return `报告预算：${budget.review_items} 项复核 · 输出上限 ${budget.max_tokens} tokens · 预留 ${budget.reserve_seconds} 秒`;
    } catch { return '已分配最终报告预算'; }
  }
  if (item.kind === 'handoff_recovery') return `${name} · 提前结束，保留本轮证据并补一次真实交接`;
  if (item.kind === 'handoff_retry') return `${name} · 指定交接未通过，使用同一轮证据重试一次`;
  if (item.kind === 'handoff_validation') {
    try {
      const detail = JSON.parse(item.agent) as {
        expected?: string; calls?: number; matching?: number; names?: string[]; accepted?: boolean;
      };
      const actual = detail.names?.length ? detail.names.join('、') : '无工具调用';
      return `交接调用检查：期望 ${detail.expected ?? '指定工具'} · 收到 ${actual} · ${
        detail.accepted ? '通过' : `未通过（${detail.matching ?? 0}/${detail.calls ?? 0} 匹配）`}`;
    } catch { return '交接调用检查已记录'; }
  }
  if (item.kind === 'model_call') return `${name} · 请求模型`;
  if (item.kind === 'timeout_retry') return `${name} · 响应超时，准备重试当前请求`;
  if (item.kind === 'retry_call') return `${name} · 正在重试当前请求`;
  if (item.kind === 'output_retry') return `${name} · 输出被截断，增加额度后重试当前请求`;
  if (item.kind === 'format_retry') return `${name} · 报告格式不合格，正在纠正一次`;
  if (item.kind === 'tool_arguments_retry') return `${name} · 保留已有证据，正在自动纠正工具参数`;
  if (item.kind === 'scope_registered') return `任务范围已登记：${item.agent} 项`;
  if (item.kind === 'tool_output_budget' || item.kind === 'tool_output_retry') {
    try {
      const budget = JSON.parse(item.agent) as { role?: string; max_tokens?: number; from?: number; to?: number };
      return `${names[budget.role ?? ''] ?? '当前角色'} · ${budget.to
        ? `疑似输出额度不足，扩容 ${budget.from} → ${budget.to}` : `工具输出预算 ${budget.max_tokens}`} tokens · 保持已配置推理强度`;
    } catch { return '已调整工具输出预算'; }
  }
  if (item.kind === 'tool_access_rejected') {
    try {
      const denied = JSON.parse(item.agent) as { role?: string; tool?: string };
      return `${names[denied.role ?? ''] ?? '当前角色'} · 已阻止越权工具 ${denied.tool ?? ''}`;
    } catch { return '已阻止越权工具调用'; }
  }
  if (item.kind === 'tool_arguments_invalid') {
    try {
      const detail = JSON.parse(item.agent) as {
        role?: string; tool?: string; reason?: string; fields?: string[];
        recoverable?: boolean; corrections_remaining?: number;
        issues?: { field: string; code: string }[]; line?: number; column?: number;
        suspected_output_limit?: boolean;
      };
      const reason = detail.reason === 'invalid_json' ? 'JSON 格式不合法'
        : detail.reason === 'object_required' ? '参数应为 JSON 对象'
          : detail.reason === 'unexpected_field' ? '包含多余字段' : '字段不符合要求';
      const labels: Record<string, string> = { required_field: '缺少必填字段', enum_value: '不在允许选项中',
        string_required: '应为文本', integer_required: '应为整数', list_required: '应为列表',
        object_required: '应为对象', unexpected_field: '多余字段', too_long: '超过长度上限',
        too_short: '未达到最小长度', pattern: '格式不符合要求', invalid_value: '值不符合要求' };
      const fields = detail.issues?.length
        ? detail.issues.map(issue => `${issue.field}：${labels[issue.code] ?? '不符合要求'}`).join('；')
        : detail.fields?.join('、');
      return `${names[detail.role ?? ''] ?? '当前角色'} · ${detail.tool ?? '工具'}：${reason}${
        fields ? `（${fields}）` : ''}${detail.line ? ` · 第 ${detail.line} 行第 ${detail.column} 列` : ''}${
        detail.suspected_output_limit ? ' · 疑似达到输出额度' : ''}；本批调用未执行，${
        detail.recoverable ? `准备自动纠正，此后剩余 ${detail.corrections_remaining ?? 0} 次` : '本次自动纠正已停止'}`;
    } catch { return '工具参数校验未通过，本批调用未执行'; }
  }
  if (item.kind === 'evidence_retry') return `${name} · 源码证据不匹配，正在纠正一次`;
  if (item.kind === 'evidence_normalized') return `源码引用已去除显示行号：${item.agent} 处`;
  if (item.kind === 'evidence_validation') {
    try {
      const detail = JSON.parse(item.agent) as { finding?: number; evidence?: number; reason?: string; historical?: boolean };
      const reason = ({ unknown_file: '文件不存在', unread_file: '文件未读取',
        invalid_range: '行号范围无效', unread_range: '范围包含未读行',
        quote_mismatch: '引用文本与源码不一致',
        unknown_historical_finding: '旧问题编号不在本轮范围内',
        duplicate_historical_finding: '旧问题被重复复核',
        missing_current_evidence: '缺少当前源码证据' } as Record<string, string>)[detail.reason ?? ''] ?? '证据未匹配';
      return `${detail.historical ? '旧问题' : '问题'} ${detail.finding} · 证据 ${detail.evidence}：${reason}`;
    } catch { return '源码证据检查未通过'; }
  }
  if (item.kind === 'report_finalization') return `${name} · 使用已读取证据生成最终报告`;
  if (item.kind === 'finalization_thinking') return '最终报告请求：已关闭额外思考';
  if (item.kind === 'report_validation') return item.agent === 'invalid_json'
    ? '报告格式检查：不是合法 JSON' : item.agent === 'scope_review_required'
      ? '报告范围检查：缺少已登记验收项的审查结论' : item.agent === 'scope_review_incomplete'
        ? '报告范围检查：版本、验收项或运行限制未完整覆盖' : '报告格式检查：字段不符合要求';
  if (item.kind === 'report_diagnostics') {
    try {
      const detail = JSON.parse(item.agent) as { category?: string; text_chars?: number; line?: number; column?: number };
      const category = detail.category === 'empty' ? '空输出'
        : detail.category === 'textual_tool_call' ? '工具调用被当作正文返回'
        : detail.category === 'schema_mismatch' ? '字段不符合要求' : 'JSON 语法错误';
      return `报告诊断：${category} · 正文 ${detail.text_chars ?? 0} 字符${
        detail.line !== undefined ? ` · 第 ${detail.line} 行，第 ${detail.column ?? 1} 列` : ''}`;
    } catch { return '报告诊断已记录'; }
  }
  if (item.kind === 'output_protocol') return `输出协议：${item.agent === 'json_object' ? 'JSON Object' : 'JSON Schema'}`;
  if (item.kind === 'provider_compatibility' && item.agent === 'deepseek_chat') {
    return 'DeepSeek 兼容模式：所有阶段关闭思考，推理强度设置不生效';
  }
  if (item.kind === 'provider_response') {
    try {
      const metadata = JSON.parse(item.agent) as { truncated?: boolean; output_tokens?: number; finish_reason?: string };
      return `${metadata.truncated ? '模型输出被截断' : '收到模型响应'}${metadata.output_tokens !== undefined ? ` · 输出 ${metadata.output_tokens} tokens` : ''}`;
    } catch { return '收到模型响应'; }
  }
  if (item.kind === 'budget') return `${name} · 当前阶段收束，进入交接或报告`;
  if (item.kind === 'time_budget') return `${name} · 收束当前阶段，为报告预留时间`;
  if (item.kind === 'reasoning_setting') return `请求推理强度：${({ default: '服务默认', low: '低', medium: '中', high: '高', max: '最高' } as Record<string, string>)[item.agent] ?? item.agent}`;
  if (item.kind === 'failure') {
    try {
      const failure = JSON.parse(item.agent) as { code?: string; stage?: string };
      return `${names[failure.stage ?? ''] ?? '准备分析'} · ${errorMessage(failure.code ?? '')}`;
    } catch { return '本次分析未完成'; }
  }
  if (item.kind === 'required_read_validation') {
    try {
      const check = JSON.parse(item.agent) as {
        required?: string; calls?: number; matching?: number; other?: number; allowed?: number; messages?: number;
      };
      return `前置请求检查（${check.required === 'execution' ? '运行记录' : check.required === 'scope' ? '任务范围' : '源码'}）：${
        check.calls ?? 0} 条调用 · 匹配 ${check.matching ?? 0} · 其他 ${check.other ?? 0} · 上限 ${check.allowed ?? 0} · 正文 ${check.messages ?? 0}`;
    } catch { return '必读请求检查已记录'; }
  }
  if (item.kind === 'required_read') return item.agent === 'read_execution_records'
    ? '必读检查：先读取已保存的测试／启动记录' : item.agent === 'register_task_scope'
      ? '范围检查：仓库分析交接前先登记验收项（每批最多三项）' : '必读检查：先读取当前源码';
  if (item.kind === 'evidence') return '读取运行记录';
  if (item.kind === 'handoff') return item.agent.split(' -> ').map(value => names[value] ?? value).join(' → ');
  return name;
}

function FindingItem({ analysis, finding, api, refresh }: {
  analysis: Analysis; finding: Finding; api: Api; refresh: () => Promise<void>;
}) {
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const submit = (decision: string) => {
    setBusy(true); setError('');
    void api(`/analyses/${analysis.id}/findings/${finding.id}/feedback`, {
      version: finding.version, decision, note, personally_reproduced: decision === 'confirmed',
    }).then(refresh).catch(e => setError(errorMessage(e.message))).finally(() => setBusy(false));
  };
  return <article className="analysis-finding">
    <div className="section-heading"><h3>{finding.data.severity} · {finding.data.title}</h3>
      <span className="tag neutral">{statuses[finding.status]}</span></div>
    <p>{finding.data.explanation}</p>
    <dl><dt>预期行为</dt><dd>{finding.data.expected}</dd><dt>疑似实际行为</dt><dd>{finding.data.suspected_actual}</dd></dl>
    {finding.data.evidence.map((item, index) => <div className="diagnosis-evidence" key={index}>
      <strong>{item.path}:{item.start_line}–{item.end_line}</strong><pre>{item.quote}</pre>
    </div>)}
    <strong>复现步骤</strong><ol>{finding.data.reproduction.map((step, index) => <li key={index}>{step}</li>)}</ol>
    {finding.status === 'waiting_reproduction' && analysis.current && <div className="analysis-feedback">
      <label>复现说明（选填）<textarea maxLength={2000} rows={3} value={note}
        onChange={e => setNote(e.target.value)} disabled={busy} /></label>
      <div className="execution-actions">
        <button className="button secondary" disabled={busy}
          onClick={() => submit('confirmed')}><Check size={15} />复现成功</button>
        <button className="button secondary" disabled={busy} onClick={() => submit('not_reproduced')}>
          <X size={15} />未能复现</button>
      </div>
    </div>}
    {error && <p role="alert">{error}</p>}
  </article>;
}

function HistoricalItem({ item, api, refresh }: {
  item: NonNullable<Analysis['historical_review']>['items'][number];
  api: Api; refresh: () => Promise<void>;
}) {
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const canDispose = item.live_status === 'waiting_reproduction' || item.live_status === 'confirmed';
  const submit = (decision: 'not_reproduced' | 'resolved') => {
    setBusy(true); setError('');
    void api(`/analyses/${item.analysis_id}/findings/${item.finding_id}/disposition`, {
      version: item.live_version, decision, note,
    }).then(refresh).catch(e => setError(errorMessage(e.message))).finally(() => setBusy(false));
  };
  return <article className="analysis-finding">
    <div className="section-heading"><h4>{item.data.title}</h4>
      <span className="tag neutral">{item.result ? historicalOutcomes[item.result.outcome] : '本轮未覆盖'}</span></div>
    <p className="diagnosis-meta">原版本 {item.revision.slice(0, 12)} · 原人工状态：{statuses[item.human_status] ?? item.human_status}
      {' · '}记录 {item.finding_id} / v{item.version}</p>
    {item.live_status && item.live_status !== item.human_status &&
      <p className="diagnosis-meta">当前状态：{statuses[item.live_status] ?? item.live_status}</p>}
    {item.content_clipped && <p className="diagnosis-meta">历史描述已截断，复核须考虑信息不完整。</p>}
    <p>{item.result?.rationale ?? '本轮没有该旧问题的复核结论，不能据此认定已修复。'}</p>
    {item.result?.evidence.map((evidence, index) => <details key={index}>
      <summary>当前源码证据：{evidence.path}:{evidence.start_line}–{evidence.end_line}</summary>
      <pre>{evidence.quote}</pre>
    </details>)}
    {canDispose && item.live_version !== null && item.live_version !== undefined && <div className="analysis-feedback">
      <label>处置说明（选填）<textarea maxLength={2000} rows={3} value={note}
        onChange={e => setNote(e.target.value)} disabled={busy} /></label>
      <div className="execution-actions">
        <button className="button secondary" disabled={busy} onClick={() => submit('not_reproduced')}>
          <X size={15} />标记未能复现（含误报）
        </button>
        <button className="button secondary" disabled={busy} onClick={() => submit('resolved')}>
          <Check size={15} />标记已解决
        </button>
      </div>
    </div>}
    {error && <p role="alert">{error}</p>}
  </article>;
}

export function AnalysisPanel({ envId, version, api, pipelineActive = false, focusAnalysisId, view = 'analysis', onNavigate }: {
  envId: string; version: number; api: Api; pipelineActive?: boolean; focusAnalysisId?: string; view?: 'analysis' | 'reproduction'; onNavigate?: (view: string) => void;
}) {
  const [analyses, setAnalyses] = useState<Analysis[]>([]);
  const [selected, setSelected] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const refresh = async () => setAnalyses(await api<Analysis[]>(`/environments/${envId}/analyses`));
  useEffect(() => {
    let active = true;
    const load = () => { void api<Analysis[]>(`/environments/${envId}/analyses`)
      .then(value => { if (active) setAnalyses(value); }).catch(() => { if (active) setError('无法读取分析状态'); }); };
    load(); const timer = window.setInterval(load, 2000);
    return () => { active = false; window.clearInterval(timer); };
  }, [api, envId]);
  useEffect(() => {
    if (focusAnalysisId) setSelected('');
  }, [focusAnalysisId]);
  const analysis = analyses.find(item => item.id === selected)
    ?? analyses.find(item => item.id === focusAnalysisId) ?? analyses[0];
  const perform = (path: string, body: unknown) => {
    setBusy(true); setError('');
    void api(path, body).then(refresh).catch(e => setError(errorMessage(e.message))).finally(() => setBusy(false));
  };
  const active = analyses.some(item => ['queued', 'running', 'waiting_model'].includes(item.status));
  const stage = analysis?.progress?.stage ??
    analysis?.events.filter(event => event.kind === 'agent').at(-1)?.agent;
  return <section id="repository-analysis" className="repository-analysis" aria-label={view === 'analysis' ? '仓库自动分析' : '复现结果'}>
    <div className="section-heading"><h3><FileSearch size={18} />{view === 'analysis' ? '项目分析' : '复现结果'}</h3>
      <span className="tag neutral">{analysis ? statuses[analysis.status] : '等待执行结果'}</span>
      {view === 'analysis' && <button className="button secondary" disabled={busy || active || pipelineActive}
        onClick={() => perform(`/environments/${envId}/analyses`, { version })}><RefreshCw size={15} />分析当前版本</button>}
    </div>
    {analyses.length > 1 && <label>分析记录<select value={analysis?.id} onChange={e => setSelected(e.target.value)}>
      {analyses.map(item => <option key={item.id} value={item.id}>{new Date(item.created * 1000).toLocaleString('zh-CN')} · {statuses[item.status]}</option>)}
    </select></label>}
    {error && <p role="alert">{error}</p>}
    <div hidden={view !== 'analysis'}>
    {!analysis && <p className="action-hint">尚无分析记录，可分析当前版本或从 Issue 页选择问题。</p>}
    {analysis?.status === 'waiting_model' && <p className="error-inline">请在“设置 → 模型与供应商 → 分配模型”配置分析所需的四个角色，并启用模型工作流，任务将自动继续。</p>}
    {analysis?.status === 'running' && <p><Loader2 size={14} className="spin" /> {names[stage ?? ''] ?? '正在准备源码与执行证据'}</p>}
    {analysis?.status === 'running' &&
      ['timeout_retry', 'retry_call', 'output_retry', 'format_retry', 'evidence_retry'].includes(analysis.events.at(-1)?.kind ?? '') &&
      <p role="status">正在重试当前模型请求，已完成的阶段保留。</p>}
    {analysis?.status === 'running' && analysis.events.at(-1)?.kind === 'handoff_recovery' &&
      <p role="status">当前角色提前结束，正在尝试补齐交接；本轮已读取的证据保留。</p>}
    {analysis?.progress && analysis.progress.model_calls > 0 && <p className="diagnosis-meta">
      本次模型调用 {analysis.progress.model_calls} 轮 · 已读取源码 {analysis.progress.source_reads} 次
    </p>}
    {analysis && !analysis.current && <p className="diagnosis-meta">历史分析 · 当前配置已变化</p>}
    {analysis?.error && <p role="alert">{errorMessage(analysis.error)}</p>}
    {analysis?.error && analysis.failure_details && <p className="diagnosis-meta">
      失败阶段：{names[analysis.failure_details.stage ?? ''] ?? '准备分析'}
      {' · '}耗时 {analysis.failure_details.elapsed_seconds} 秒
      {analysis.failure_details.http_status !== null && ` · HTTP ${analysis.failure_details.http_status}`}
    </p>}
    {analysis?.error && analysis.provider_response?.finish_reason && <p className="diagnosis-meta">
      模型结束原因：{analysis.provider_response.finish_reason}
      {analysis.provider_response.output_tokens !== undefined && ` · 输出 ${analysis.provider_response.output_tokens} tokens`}
      {analysis.provider_response.reasoning_tokens !== undefined && ` · 推理 ${analysis.provider_response.reasoning_tokens} tokens`}
    </p>}
    {analysis && ['failed', 'interrupted'].includes(analysis.status) &&
      <button className="button secondary" disabled={busy || !analysis.current}
        onClick={() => perform(`/analyses/${analysis.id}/retry`, { version: analysis.version })}><RefreshCw size={15} />
        {analysis.report_recovery?.available ? '从已保存证据恢复报告' : '重试分析'}</button>}
    {analysis?.report_recovery?.available && <p className="diagnosis-meta">
      已保存审查输入。恢复时会再次检查源码、执行记录、需求和模型配置；依据变化后将重新分析。
    </p>}
    {analysis?.task_scope && <details className="disclosure"><summary>任务验收范围 · {analysis.task_scope.done} / {analysis.task_scope.total} 项已完成</summary><TaskScopePanel key={analysis.id} scope={analysis.task_scope} analysisId={analysis.id}
      analysisVersion={analysis.version} analysisStatus={analysis.status} current={analysis.current}
      envId={envId} api={api} refresh={refresh} /></details>}
    {analysis?.report && <>
      <p>{analysis.report.summary}</p>
      {analysis.report.execution_scope?.kind === 'desktop_offline' &&
        <p role="status">本报告仅结合容器离线测试与源码分析。
          未验证：{analysis.report.execution_scope.uncovered.join('；')}。
          不能据此认定整个 Windows 桌面程序验收通过。</p>}
      <p className="diagnosis-meta">版本 {analysis.revision.slice(0, 12)} · 已读取 {analysis.report.files_read.length} 个文件
        {analysis.report.model && ` · ${analysis.report.model}`}</p>
      <ul>{analysis.report.limitations.map((item, index) => <li key={index}>{item}</li>)}</ul>
      {analysis.coverage && <p className="diagnosis-meta">源码快照：{analysis.coverage.captured} / {analysis.coverage.eligible} 个可分析文件
        {!analysis.coverage.complete && ' · 存在截断或未采集文件'}</p>}
      {analysis.findings.length === 0 && <p>本轮未提出有充分依据的疑似问题；这不代表旧问题已解决或整个项目不存在问题。</p>}
      {!!analysis.findings.length && <p>{analysis.findings.length} 个疑似问题等待核验。<button className="text-link" onClick={() => onNavigate?.('reproduction')}>前往复现结果</button></p>}
    </>}
    {analysis && analysis.events.length > 0 && <details><summary>Agent 分析记录（{analysis.events.length}）</summary>
      <ol className="analysis-events">{analysis.events.map((item, index) => <li key={index}>
        {new Date(item.created * 1000).toLocaleTimeString('zh-CN')} · {eventLabel(item)}
      </li>)}</ol></details>}
    </div>
    <div hidden={view !== 'reproduction'}>
      {!analysis?.report ? <p className="action-hint">尚无可复现的分析报告，请先完成项目分析。</p> : <>
      {analysis.findings.length === 0 && <p>本轮没有待反馈的新问题。</p>}
      <div className="content-card-grid">{analysis.findings.map(finding => <FindingItem key={finding.id} analysis={analysis} finding={finding} api={api} refresh={refresh} />)}</div>      {analysis.historical_review && <section className="historical-review" aria-label="旧问题逐条复核">
        <h4>旧问题逐条复核</h4>
        <p className="diagnosis-meta">同仓库待复核 {analysis.historical_review.total} 项 · 本轮带入 {analysis.historical_review.included} 项。
          这些是模型复核意见，不会自动更改人工确认，也不会自动修复或发布；你可以在条目下方标记处置结论。</p>
        {analysis.historical_review.omitted > 0 && <p role="status">
          另有 {analysis.historical_review.omitted} 项受本轮数量限制未带入，均未覆盖，不能认定已修复。
        </p>}
        {analysis.historical_review.total === 0 && <p>没有可带入的历史未解决问题。</p>}
        {analysis.historical_review.items.map(item =>
          <HistoricalItem key={item.finding_id} item={item} api={api} refresh={refresh} />)}
      </section>}

      <button className="button secondary" onClick={() => onNavigate?.('repair')}>查看修复进度 →</button>
      </>}
    </div>
  </section>;
}
