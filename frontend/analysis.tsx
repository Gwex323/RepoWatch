import { useEffect, useState } from 'react';
import { Check, FileSearch, Loader2, RefreshCw, X } from 'lucide-react';
import { agentModelEvent } from './agent-models';
import { TaskScopePanel, type TaskScope } from './task-scope';

type Api = <T>(path: string, body?: unknown) => Promise<T>;
type Finding = {
  id: string; status: string; version: number;
  feedback?: { note: string; decision: string } | null;
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
    live_feedback?: { note: string; decision: string } | null;
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
  automatic_recovery?: {
    state: string; requests: number; max_requests: number; resumptions: number; max_resumptions: number;
    retry_after: number; deadline: number; remaining_seconds: number; last_error: string | null;
    stop_reason: string | null; retry_budget: Record<string, number>;
  } | null;
  report: { summary: string; limitations: string[]; files_read: string[]; model?: string;
    execution_scope?: { kind: string; executed: number | null; skipped: number;
      uncovered: string[]; complete: boolean } } | null;
  events: { kind: string; agent: string; created: number }[];
  coverage: { captured: number; eligible: number; complete: boolean } | null;
  progress?: { stage: string | null; model_calls: number; source_reads: number };
  failure_details?: { stage: string | null; elapsed_seconds: number; http_status: number | null } | null;
  provider_response?: { finish_reason?: string; output_tokens?: number; reasoning_tokens?: number; payload_tokens?: number; truncated?: boolean } | null;
};
const statuses: Record<string, string> = {
  cancelled: '已取消', waiting_model: '等待模型配置', queued: '等待分析', running: 'Agent 分析中',
  completed: '分析已完成', failed: '分析未完成', interrupted: '分析已中断',
  superseded: '已由新记录继续处理',
  waiting_reproduction: '待人工复现', confirmed: '复现成功', not_reproduced: '未能复现',
  resolved: '已解决', false_positive: '误报 · 用户已确认，已关闭',
};
const recoveryStopReasons: Record<string, string> = {
  total_deadline: '整条分析的总时限已到', request_limit: '累计模型请求额度已用完',
  resumption_limit: '续办次数已用完', basis_changed: '源码或执行依据已变化',
  checkpoint_invalid: '检查点与当前任务不再匹配', pipeline_stopped: '流程已停止或账号已变化',
  review_not_started: '尚未形成可恢复的最终审查', non_recoverable: '该故障不允许自动恢复',
};
export const analysisErrorMessages: Readonly<Partial<Record<string, string>>> = {
  analysis_model_invalid: '请检查模型名称、接口类型和服务地址。',
  scope_review_incomplete: '范围审查遗漏验收项或版本不一致；原范围已保留，请继续分析与范围审查。',
  scope_mapping_invalid: '问题与验收项的关联引用未通过校验，报告未保存。请查看具体原因；若可恢复报告，可从已保存证据继续。',
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
  analysis_recovery_budget_exhausted: '本条分析的累计恢复额度已用完。已有证据和失败记录已保留；重试不会重置额度。',
  analysis_recovery_basis_changed: '恢复依据已变化，本次自动续办停止，未复用过期证据。请确认当前任务后重新分析。',
  analysis_provider_unreachable: '无法连接模型服务，请检查地址与网络。',
  analysis_evidence_unverified: '源码证据未通过完整校验，本次未保存问题。请查看具体原因；若可恢复报告，可从已保存证据继续。',
  analysis_review_incomplete: 'Agent 未完成规定的源码读取与交接，本次未保存问题，请重试。',
  analysis_execution_unread: 'Agent 未读取已保存的测试／启动记录，报告未保存。请重试分析。',
  analysis_source_unread: 'Agent 未成功读取任何源码，报告未保存。请重试分析。',
  analysis_handoff_incomplete: 'Agent 未完成规定的阶段交接，报告未保存。请重试分析。',
  analysis_execution_read_not_followed: '模型返回的执行记录读取请求未通过校验，分析已提前停止。请查看必读请求检查记录后重试分析，无需重新安装。',
  analysis_source_read_not_followed: '模型返回的源码读取请求未通过校验，分析已提前停止。请查看必读请求检查记录后重试分析，无需重新安装。',
  finding_closed: '该旧问题已记录处置结论，无需重复操作。',
  repair_running: '该问题正在修复中，暂不能改判；请等修复结束或停止任务。',
  resolution_note_required: '手动标记解决必须填写处置说明。',
  false_positive_note_required: '请填写确认误报的原因。',
  finding_continuation_required: '请填写当前版本续办说明。',
  finding_recheck_required: '需要最新独立复核确认该问题仍存在，并具备当前源码证据。',
  finding_scope_required: '该问题尚未关联当前已确认的验收项，请先继续范围审查。',
  repair_confirmation_required: '该问题需要有效的原用户复现确认，不能仅凭模型意见开始修复。',
  resolution_pr_not_merged: '修复 PR 尚未合并，请先在 GitHub 完成合并。',
  resolution_verification_failed: '无法核验修复 PR 的合并状态，请检查 GitHub 账号与网络后重试。',
  analysis_max_turns_exceeded: '分析已达到总调用轮数上限，尚未生成完整报告。',
  analysis_stage_budget_exceeded: '当前阶段已耗尽预算，尚未完成交接或报告。',
  analysis_handoff_not_followed: '指定角色交接未通过，本轮分析停止。请查看交接调用检查中的原因与纠正记录。',
  analysis_finalization_not_followed: '已要求提交报告，但模型仍尝试调用工具。请检查模型服务的工具选择支持。',
  analysis_model_output_invalid: '模型返回了无效的工具调用或输出格式，本次未保存问题。',
  analysis_tool_call_protocol_invalid: '模型工具调用的协议字段不符合要求，无法安全兼容，本批调用未执行。请查看协议兼容检查记录。',
  analysis_output_truncated: '模型输出达到生成上限，扩容空间或重试额度已用完，本次未保存问题。',
  analysis_provider_empty_response: '模型服务没有返回可用的结果，请检查服务兼容性和状态。',
  analysis_tool_arguments_invalid: '模型工具参数未通过 JSON 或字段校验，本次未执行该调用。',
  analysis_tool_arguments_exhausted: '工具调用的自动纠正次数已用完，本批调用未执行。请查看最后一次参数校验的具体原因。',
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
type ToolArgumentDetail = {
  role?: string; tool?: string; reason?: string; fields?: string[];
  recoverable?: boolean; corrections_remaining?: number;
  issues?: { field: string; code: string }[]; line?: number; column?: number;
  suspected_output_limit?: boolean;
};
const toolNames: Record<string, string> = {
  register_task_scope: '登记任务范围', read_source_file: '读取源码',
  read_execution_records: '读取运行记录',
};
function toolArgumentReason(detail: ToolArgumentDetail) {
  const reasons: Record<string, string> = {
    invalid_json: 'JSON 语法错误', object_required: '参数应为 JSON 对象',
    unexpected_field: '包含多余字段', arguments_schema: 'JSON 可解析，但字段不符合要求',
    repair_batch_mismatch: '未按要求纠正原工具批次，不能改为交接、报告或其他调用',
    repair_valid_fields_changed: '纠正时改动了原有合法字段或条目数量',
  };
  const labels: Record<string, string> = {
    required_field: '缺少必填字段', enum_value: '不在允许选项中',
    string_required: '应为文本', integer_required: '应为整数', list_required: '应为列表',
    object_required: '应为对象', unexpected_field: '多余字段', too_long: '超过长度上限',
    too_short: '未达到最小长度', pattern: '格式不符合要求', invalid_value: '值不符合要求',
  };
  const fields = detail.issues?.length
    ? detail.issues.map(issue => `${issue.field}：${labels[issue.code] ?? '不符合要求'}`).join('；')
    : detail.fields?.join('、');
  return `${reasons[detail.reason ?? ''] ?? '字段不符合要求'}${fields ? `（${fields}）` : ''}${
    detail.line ? ` · 第 ${detail.line} 行第 ${detail.column} 列` : ''}`;
}
function analysisFailureMessage(analysis: Analysis) {
  const message = errorMessage(analysis.error ?? '');
  if (['analysis_execution_read_not_followed', 'analysis_source_read_not_followed',
    'analysis_scope_read_not_followed', 'analysis_model_refused'].includes(analysis.error ?? '')) {
    for (let index = analysis.events.length - 1; index >= 0; index--) {
      const event = analysis.events[index];
      if (event.kind === 'attempt') break;
      if (event.kind === 'required_read_validation' || event.kind === 'handoff_validation') return `${message} ${eventLabel(event)}。`;
    }
    return message;
  }
  if (analysis.error === 'analysis_handoff_not_followed') {
    for (let index = analysis.events.length - 1; index >= 0; index--) {
      const event = analysis.events[index];
      if (event.kind === 'attempt') break;
      if (event.kind === 'handoff_validation' || event.kind === 'handoff_recovery_stopped') {
        return `${message} ${eventLabel(event)}。`;
      }
    }
    return message;
  }
  if (['analysis_evidence_unverified', 'analysis_report_invalid', 'scope_mapping_invalid'].includes(analysis.error ?? '')) {
    const kind = analysis.error === 'scope_mapping_invalid' ? 'scope_mapping_validation'
      : analysis.error === 'analysis_evidence_unverified' ? 'evidence_validation' : 'report_validation';
    for (let index = analysis.events.length - 1; index >= 0; index--) {
      const event = analysis.events[index];
      if (event.kind === 'attempt') break;
      if (event.kind === kind) return `${message} ${eventLabel(event)}。`;
    }
    return message;
  }
  if (analysis.error === 'analysis_output_truncated') {
    const response = analysis.provider_response;
    const content = response?.payload_tokens ?? (response?.output_tokens !== undefined && response.reasoning_tokens !== undefined
      ? Math.max(0, response.output_tokens - response.reasoning_tokens) : undefined);
    return `${message}${response?.reasoning_tokens !== undefined
      ? ` 最后一次推理 ${response.reasoning_tokens} tokens${content !== undefined ? `，非推理输出 ${content} tokens` : ''}。` : ''}`;
  }
  if (!['analysis_tool_arguments_invalid', 'analysis_tool_arguments_exhausted'].includes(analysis.error ?? '')) return message;
  for (let index = analysis.events.length - 1; index >= 0; index--) {
    const event = analysis.events[index];
    if (event.kind === 'attempt') break;
    if (event.kind !== 'tool_arguments_invalid') continue;
    try {
      const detail = JSON.parse(event.agent) as ToolArgumentDetail;
      return `${message} ${toolNames[detail.tool ?? ''] ?? '工具调用'}：${toolArgumentReason(detail)}。`;
    } catch { return message; }
  }
  return message;
}
function eventLabel(item: Analysis['events'][number]) {
  if (item.kind === 'finding_continued') return '历史确认问题已在当前版本继续修复';
  const name = names[item.agent] ?? item.agent;
  if (item.kind === 'model_configuration') return agentModelEvent(item.agent);
  if (item.kind === 'attempt') return '开始新一次分析';
  if (item.kind === 'report_checkpoint_saved') return '已保存最终审查输入及真实读取、交接记录';
  if (item.kind === 'report_checkpoint_invalidated') return '已保存的审查依据发生变化，重新执行完整分析';
  if (item.kind === 'report_resumed') return '从已保存证据恢复最终报告，仅调用独立审查员';
  if (item.kind === 'report_recovery_scheduled') {
    try {
      const detail = JSON.parse(item.agent) as { attempt: number; delay_seconds: number };
      return `已安排第 ${detail.attempt} 次审查续办，等待 ${detail.delay_seconds} 秒，沿用已有证据与累计额度`;
    } catch { return '已安排自动恢复最终审查'; }
  }
  if (item.kind === 'report_recovery_stopped') return `自动恢复已停止：${recoveryStopReasons[item.agent] ?? '请查看失败记录'}`;
  if (item.kind === 'report_recovery_new_scope') return '用户发起新范围审查，上一条恢复链额度已归档；新范围按新任务计额';
  if (item.kind === 'report_budget') {
    try {
      const budget = JSON.parse(item.agent) as { max_tokens: number; reserve_seconds: number; review_items: number };
      return `报告预算：${budget.review_items} 项复核 · 输出上限 ${budget.max_tokens} tokens · 预留 ${budget.reserve_seconds} 秒`;
    } catch { return '已分配最终报告预算'; }
  }
  if (item.kind === 'handoff_recovery') return `${name} · 提前结束，保留本轮证据并补一次真实交接`;
  if (item.kind === 'handoff_retry') return `${name} · 指定交接未通过，使用同一轮证据重试一次`;
  if (item.kind === 'handoff_recovered') return `${name} · 交接纠正已通过校验，等待实际交接`;
  if (item.kind === 'handoff_recovery_stopped') return '交接纠正已停止：阶段时限已到，本批未执行';
  if (item.kind === 'handoff_validation') {
    try {
      const detail = JSON.parse(item.agent) as {
        expected?: string; calls?: number; matching?: number; accepted?: boolean;
        reason?: string; recovery?: string; corrections_remaining?: number;
      };
      const target = names[(detail.expected ?? '').replace(/^transfer_to_/, '')] ?? '指定角色';
      const reasons: Record<string, string> = { missing_call: '没有返回交接调用', wrong_tool: '调用不符合指定交接',
        too_many_calls: '返回了多条调用', refusal: '模型明确拒绝' };
      const recovery: Record<string, string> = { retry: '准备有限纠正', retry_exhausted: '本阶段交接纠正机会已用完',
        shared_budget_exhausted: '共享工具纠正额度已用完', stage_deadline: '阶段时限已到',
        argument_correction_active: '不能改变原参数纠正批次', refusal: '已停止，不重试拒绝' };
      return `交接调用检查：交给${target} · 匹配 ${detail.matching ?? 0}/${detail.calls ?? 0} · ${
        detail.accepted ? '通过' : reasons[detail.reason ?? ''] ?? '未通过'}${
          recovery[detail.recovery ?? ''] ? ` · ${recovery[detail.recovery ?? '']}` : ''}${
          detail.corrections_remaining !== undefined ? ` · 共享纠正剩余 ${detail.corrections_remaining} 次` : ''}`;
    } catch { return '交接调用检查已记录'; }
  }
  if (item.kind === 'model_call') return `${name} · 请求模型`;
  if (item.kind === 'timeout_retry') return `${name} · 响应超时，准备重试当前请求`;
  if (item.kind === 'retry_call') return `${name} · 正在重试当前请求`;
  if (item.kind === 'output_retry') return `${name} · 输出被截断，增加额度后重试当前请求`;
  if (item.kind === 'format_retry') return `${name} · 报告格式不合格，正在纠正一次`;
  if (item.kind === 'tool_arguments_retry') return `${name} · 保留已有证据，正在纠正原工具批次`;
  if (item.kind === 'tool_arguments_repaired') return `${name} · 参数纠正通过校验，等待工具实际执行`;
  if (item.kind === 'required_read_retry') return '必读工具调用缺失，正在有限纠正；此前没有执行读取';
  if (item.kind === 'required_read_recovered') return '必读调用已通过检查，等待工具实际执行';
  if (item.kind === 'tool_arguments_preserved') {
    try {
      const detail = JSON.parse(item.agent) as { role?: string; calls?: { tool?: string; fields?: string[] }[] };
      const fields = detail.calls?.flatMap(call => call.fields ?? []).join('、');
      return `${names[detail.role ?? ''] ?? '当前角色'} · 已保留原合法参数，仅采纳错误字段的修正${fields ? `（${fields}）` : ''}，等待工具实际执行`;
    } catch { return '已保留原合法参数，仅采纳错误字段的修正，等待工具实际执行'; }
  }
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
      const detail = JSON.parse(item.agent) as ToolArgumentDetail;
      return `${names[detail.role ?? ''] ?? '当前角色'} · ${toolNames[detail.tool ?? ''] ?? '工具调用'}：${toolArgumentReason(detail)}${
        detail.suspected_output_limit ? ' · 疑似达到输出额度' : ''}；本批调用未执行，${
        detail.recoverable ? `准备自动纠正，此后剩余 ${detail.corrections_remaining ?? 0} 次` : '本次自动纠正已停止'}`;
    } catch { return '工具参数校验未通过，本批调用未执行'; }
  }
  if (item.kind === 'evidence_retry') return `${name} · 源码证据不匹配，正在纠正一次`;
  if (item.kind === 'evidence_correction') {
    try {
      const detail = JSON.parse(item.agent) as { role?: string; items?: number; issues?: number; protocol?: string };
      return `${names[detail.role ?? ''] ?? '独立审查'} · 保留报告其他内容，仅纠正 ${detail.items ?? 0} 项中的 ${detail.issues ?? 0} 处证据${detail.protocol === 'source_references' ? '；选择已读片段，由程序复制源码原文' : ''}`;
    } catch { return '保留报告其他内容，仅纠正未通过校验的证据'; }
  }
  if (item.kind === 'evidence_correction_applied') return `${name} · 已合并证据纠正，正在检查完整报告`;
  if (item.kind === 'scope_mapping_validation') {
    try {
      const detail = JSON.parse(item.agent) as { section?: string; item?: number; reason?: string; positions?: number[] };
      const target = detail.section === 'findings' ? '新问题' : '范围验收项';
      const reason = detail.reason === 'unknown_scope_key' ? '引用了未登记的验收项'
        : detail.reason === 'unknown_finding_id' ? '引用了本任务中不存在的问题 ID；新问题应通过验收项 key 关联' : '关联引用无效';
      return `${target} ${detail.item ?? ''} · ${reason}${detail.positions?.length ? `（第 ${detail.positions.join('、')} 个引用）` : ''}`;
    } catch { return '问题与验收项关联检查未通过'; }
  }
  if (item.kind === 'scope_mapping_correction') {
    try {
      const detail = JSON.parse(item.agent) as { role?: string; items?: number };
      return `${names[detail.role ?? ''] ?? '独立审查'} · 正在纠正 ${detail.items ?? 0} 项关联，保留问题正文与源码证据`;
    } catch { return '正在纠正问题与验收项关联'; }
  }
  if (item.kind === 'scope_mapping_correction_applied') return `${name} · 已合并关联纠正，正在重新校验完整报告`;
  if (item.kind === 'output_limit') {
    try {
      const detail = JSON.parse(item.agent) as { role?: string; phase?: string; max_tokens?: number; reasoning_tokens?: number; payload_tokens?: number; can_expand?: boolean };
      const phase = ({ evidence_correction: '局部证据纠正', scope_mapping_correction: '范围关联纠正', tool_arguments: '工具参数纠正', report: '最终报告', exploration: '分析探索' } as Record<string, string>)[detail.phase ?? ''] ?? '模型请求';
      return `${names[detail.role ?? ''] ?? '当前角色'} · ${phase}达到 ${detail.max_tokens ?? 0} tokens 上限${detail.reasoning_tokens !== undefined ? ` · 推理 ${detail.reasoning_tokens}` : ''}${detail.payload_tokens !== undefined ? ` · 非推理输出 ${detail.payload_tokens}` : ''} · ${detail.can_expand ? '按剩余额度扩容' : '扩容空间或重试额度已用完'}`;
    } catch { return '模型输出达到本次生成上限'; }
  }
  if (item.kind === 'evidence_normalized') return `源码引用已去除显示行号：${item.agent} 处`;
  if (item.kind === 'evidence_validation') {
    try {
      const detail = JSON.parse(item.agent) as { finding?: number; evidence?: number; reason?: string; historical?: boolean; scope?: string };
      const reason = ({ unknown_file: '文件不存在', unread_file: '文件未读取',
        invalid_range: '行号范围无效', unread_range: '范围包含未读行',
        quote_mismatch: '引用文本与源码不一致',
        unknown_historical_finding: '旧问题编号不在本轮范围内',
        duplicate_historical_finding: '旧问题被重复复核',
        missing_current_evidence: '缺少当前源码证据' } as Record<string, string>)[detail.reason ?? ''] ?? '证据未匹配';
      const target = detail.historical ? '旧问题' : detail.scope === 'items' ? '范围验收项' : detail.scope === 'exclusions' ? '运行限制' : '问题';
      return `${target} ${detail.finding} · 证据 ${detail.evidence}：${reason}`;
    } catch { return '源码证据检查未通过'; }
  }
  if (item.kind === 'report_finalization') return `${name} · 使用已读取证据生成最终报告`;
  if (item.kind === 'finalization_thinking') return '最终报告请求：已关闭额外思考';
  if (item.kind === 'report_validation') {
    const selectionReason = ({ evidence_correction_unknown_source: '所选源码片段编号不在实际读取目录中',
      evidence_correction_source_range: '所选行区间越界、跨越未读间隙或超过行数上限',
      evidence_correction_source_length: '所选源码片段为空白或超过 1200 字符',
      scope_correction_targets_mismatch: '未按原错误项目逐项纠正关联',
      scope_correction_unknown_reference: '所选验收项或问题编号不在本次目录中',
      scope_correction_invalid_selection: '关联选择重复、为空或不符合项目要求',
      scope_correction_unresolved_has_links: '待审查关联同时提交了确定引用' } as Record<string, string>)[item.agent];
    if (selectionReason) return `证据选择检查：${selectionReason}`;
    return item.agent === 'invalid_json'
    ? '报告格式检查：不是合法 JSON' : item.agent === 'scope_review_required'
      ? '报告范围检查：缺少已登记验收项的审查结论' : item.agent === 'scope_review_incomplete'
        ? '报告范围检查：版本、验收项或运行限制未完整覆盖' : '报告格式检查：字段不符合要求';
  }
  if (item.kind === 'report_diagnostics') {
    try {
      const detail = JSON.parse(item.agent) as { category?: string; text_chars?: number; line?: number; column?: number;
        reason?: string; fields?: { path: string; reason: string }[] };
      const category = detail.category === 'empty' ? '空输出'
        : detail.category === 'textual_tool_call' ? '工具调用被当作正文返回'
        : detail.category === 'schema_mismatch' ? '字段不符合要求' : 'JSON 语法错误';
      const reasons: Record<string, string> = { expected_delimiter: '缺少分隔符', expected_colon: '缺少冒号',
        expected_value: '缺少合法值', expected_property: '属性名需要双引号', extra_data: 'JSON 后存在额外内容',
        invalid_escape: '转义字符不合法' };
      return `报告诊断：${category} · 正文 ${detail.text_chars ?? 0} 字符${
        detail.line !== undefined ? ` · 第 ${detail.line} 行，第 ${detail.column ?? 1} 列` : ''}${
        reasons[detail.reason ?? ''] ? ` · ${reasons[detail.reason ?? '']}` : ''}${
        detail.fields?.length ? ` · ${detail.fields.map(field => `${field.path || '报告根节点'} (${field.reason})`).join('；')}` : ''}`;
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
  if (item.kind === 'provider_tool_call_compatibility') {
    try {
      const detail = JSON.parse(item.agent) as { status?: string; reason?: string; normalized?: number };
      const reasons: Record<string, string> = {
        ambiguous_legacy_call: '同时包含旧式函数调用', tools_closed: '当前请求未开放函数工具',
        unsupported_type: '调用类型无法识别', ambiguous_custom_call: '混有自定义工具字段',
        invalid_call_shape: '调用项不是有效对象',
        invalid_call_id: '调用 ID 缺失或重复', invalid_function: '函数结构不完整',
        tool_not_offered: '工具不在本次开放范围内', invalid_arguments_type: '参数字段不是字符串',
      };
      return detail.status === 'normalized'
        ? `协议兼容：已修正 ${detail.normalized ?? 0} 条函数调用的类型标记，继续权限与参数校验`
        : `协议兼容检查未通过：${reasons[detail.reason ?? ''] ?? '调用结构不符合要求'}，本批未执行`;
    } catch { return '工具调用协议兼容检查已记录'; }
  }
  if (item.kind === 'provider_structure') {
    try {
      const detail = JSON.parse(item.agent) as { protocol?: string; tool_calls?: number;
        content_chars?: number; content_parts?: number; legacy_call?: boolean; content_state?: string;
        call_item_state?: string; call_type_state?: string; call_id_state?: string;
        function_state?: string; function_name_state?: string; arguments_state?: string;
        arguments_json_state?: string; finish_reason?: string };
      const content = ({ missing: '缺少正文字段', null: '正文为空', empty: '正文为空文本',
        text: `正文 ${detail.content_chars ?? 0} 字符`, parts: `正文 ${detail.content_parts ?? 0} 段`,
        invalid_type: '正文字段类型不符合协议' } as Record<string, string>)[detail.content_state ?? ''];
      const shapes = [
        detail.call_item_state && `调用项 ${detail.call_item_state}`,
        detail.call_type_state && `type ${detail.call_type_state}`,
        detail.call_id_state && `id ${detail.call_id_state}`,
        detail.function_state && `function ${detail.function_state}`,
        detail.function_name_state && `name ${detail.function_name_state}`,
        detail.arguments_state && `arguments ${detail.arguments_state}`,
        detail.arguments_json_state && `参数 JSON ${detail.arguments_json_state}`,
        detail.finish_reason && `finish_reason ${detail.finish_reason}`,
      ].filter(Boolean).join(' · ');
      return `供应商响应结构：${detail.tool_calls === undefined ? '未提供工具调用计数' : `工具调用 ${detail.tool_calls} 条`}${
        content ? ` · ${content}` : ''}${shapes ? ` · ${shapes}` : ''}${
          detail.legacy_call ? ' · 含旧式函数调用字段' : ''}`;
    } catch { return '供应商响应结构已记录'; }
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
        reason?: string; recovery?: string; wire_tool_calls?: number;
      };
      const reason = ({ missing_call: '没有返回必读工具调用', wrong_tool: '返回了其他工具或交接',
        too_many_calls: '调用数量超过允许上限', refusal: '模型明确拒绝',
      } as Record<string, string>)[check.reason ?? ''];
      const recovery = ({ retry: '准备有限纠正', request_mismatch: '调用不匹配，已停止',
        retry_exhausted: '必读调用纠正机会已用完', shared_budget_exhausted: '共享工具纠正额度已用完',
        stage_deadline: '阶段时限已到', argument_correction_active: '原参数纠正不能跳过绑定批次',
        refusal: '已停止，不自动重试拒绝',
      } as Record<string, string>)[check.recovery ?? ''];
      return `前置请求检查（${check.required === 'execution' ? '运行记录' : check.required === 'scope' ? '任务范围' : '源码'}）：${
        check.calls ?? 0} 条调用 · 匹配 ${check.matching ?? 0} · 其他 ${check.other ?? 0} · 上限 ${check.allowed ?? 0} · 正文 ${check.messages ?? 0}`
        + `${check.wire_tool_calls !== undefined ? ` · 供应商工具调用 ${check.wire_tool_calls} 条` : ''}${reason ? ` · ${reason}` : ''}${recovery ? ` · ${recovery}` : ''}`;
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
  const closeFalsePositive = () => {
    setBusy(true); setError('');
    void api(`/analyses/${analysis.id}/findings/${finding.id}/disposition`, {
      version: finding.version, decision: 'false_positive', note,
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
    {['waiting_reproduction', 'confirmed', 'not_reproduced'].includes(finding.status) && <div className="analysis-feedback">
      <label>处置说明（确认误报时必填）<textarea maxLength={2000} rows={3} value={note}
        onChange={e => setNote(e.target.value)} disabled={busy} /></label>
      <div className="execution-actions">
        {finding.status === 'waiting_reproduction' && analysis.current && <>
        <button className="button secondary" disabled={busy}
          onClick={() => submit('confirmed')}><Check size={15} />复现成功</button>
        <button className="button secondary" disabled={busy} onClick={() => submit('not_reproduced')}>
          <X size={15} />未能复现</button>
        </>}
        <button className="button secondary" disabled={busy || !note.trim()} onClick={closeFalsePositive}>
          确认误报并关闭</button>
      </div>
    </div>}
    {finding.status === 'false_positive' && <p role="status">{finding.feedback?.note}</p>}
    {error && <p role="alert">{error}</p>}
  </article>;
}

function HistoricalItem({ item, analysis, api, refresh }: {
  item: NonNullable<Analysis['historical_review']>['items'][number];
  analysis: Analysis;
  api: Api; refresh: () => Promise<void>;
}) {
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const canDispose = ['waiting_reproduction', 'confirmed', 'not_reproduced'].includes(item.live_status ?? '');
  const dispositionLabel = item.live_status && ['false_positive', 'resolved', 'not_reproduced', 'superseded'].includes(item.live_status)
    ? statuses[item.live_status] : null;
  const submit = (decision: 'not_reproduced' | 'resolved' | 'false_positive') => {
    setBusy(true); setError('');
    void api(`/analyses/${item.analysis_id}/findings/${item.finding_id}/disposition`, {
      version: item.live_version, decision, note,
    }).then(refresh).catch(e => setError(errorMessage(e.message))).finally(() => setBusy(false));
  };
  const continueRepair = () => {
    setBusy(true); setError('');
    void api(`/analyses/${item.analysis_id}/findings/${item.finding_id}/continue`, {
      version: item.live_version, target_analysis_id: analysis.id,
      analysis_version: analysis.version, note,
    }).then(refresh).catch(e => setError(errorMessage(e.message))).finally(() => setBusy(false));
  };
  return <article className="analysis-finding">
    <div className="section-heading"><h4>{item.data.title}</h4>
      <span className="tag neutral">{dispositionLabel ??
        (item.result ? historicalOutcomes[item.result.outcome] : '本轮未覆盖')}</span></div>
    {dispositionLabel && <p role="status">{item.live_feedback?.note}</p>}
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
      <label>处置说明（确认误报或手动标记解决时必填）<textarea maxLength={2000} rows={3} value={note}
        onChange={e => setNote(e.target.value)} disabled={busy} /></label>
      <div className="execution-actions">
        {item.live_status !== 'not_reproduced' && <>
        <button className="button secondary" disabled={busy} onClick={() => submit('not_reproduced')}>
          <X size={15} />标记未能复现
        </button>
        <button className="button secondary" disabled={busy} onClick={() => submit('resolved')}>
          <Check size={15} />标记已解决
        </button>
        </>}
        <button className="button secondary" disabled={busy || !note.trim()} onClick={() => submit('false_positive')}>
          确认误报并关闭</button>
        {item.live_status === 'confirmed' && item.result?.outcome === 'still_present' && analysis.current &&
          <button className="button primary" disabled={busy || !note.trim()} onClick={continueRepair}>
            在当前版本继续修复</button>}
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
  const analysisPath = `/environments/${envId}/analyses${focusAnalysisId ? `?focus=${encodeURIComponent(focusAnalysisId)}` : ''}`;
  const refresh = async () => setAnalyses(await api<Analysis[]>(analysisPath));
  useEffect(() => {
    let active = true;
    const load = () => { void api<Analysis[]>(analysisPath)
      .then(value => { if (active) setAnalyses(value); }).catch(() => { if (active) setError('无法读取分析状态'); }); };
    load(); const timer = window.setInterval(load, 2000);
    return () => { active = false; window.clearInterval(timer); };
  }, [api, analysisPath]);
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
  const recovery = analysis?.automatic_recovery;
  const recoveryExhausted = !!recovery && (recovery.requests >= recovery.max_requests
    || recovery.resumptions >= recovery.max_resumptions || recovery.remaining_seconds <= 0);
  const stage = analysis?.progress?.stage ??
    analysis?.events.filter(event => event.kind === 'agent').at(-1)?.agent;
  return <section id="repository-analysis" className="repository-analysis" aria-label={view === 'analysis' ? '仓库自动分析' : '复现结果'}>
    <div className="section-heading"><h3><FileSearch size={18} />{view === 'analysis' ? '项目分析' : '复现结果'}</h3>
      <span className="tag neutral">{analysis ? statuses[analysis.status] : '等待执行结果'}</span>
      {view === 'analysis' && <button className="button secondary" disabled={busy || active || pipelineActive}
        onClick={() => perform(`/environments/${envId}/analyses`, { version })}><RefreshCw size={15} />分析当前版本</button>}
    </div>
    {analyses.length > 1 && <label>分析记录<select aria-label="分析记录" value={analysis?.id} onChange={e => setSelected(e.target.value)}>
      {analyses.map(item => <option key={item.id} value={item.id}>{new Date(item.created * 1000).toLocaleString('zh-CN')} · {statuses[item.status]}</option>)}
    </select></label>}
    {error && <p role="alert">{error}</p>}
    {analysis && <a className="text-link" href={`#system-faults?task_id=${encodeURIComponent(analysis.id)}`}>查看此任务的系统故障记录</a>}
    <div hidden={view !== 'analysis'}>
    {!analysis && <p className="action-hint">尚无分析记录，可分析当前版本或从 Issue 页选择问题。</p>}
    {recovery && <div className="diagnosis-meta" role="status">
      <p>累计模型请求 {recovery.requests} / {recovery.max_requests} 次 · 续办 {recovery.resumptions} / {recovery.max_resumptions} 次
        {recovery.state !== 'completed' && ` · 总时限剩余 ${Math.ceil(recovery.remaining_seconds / 60)} 分钟`}</p>
      {recovery.state === 'waiting' && <p>已安排自动恢复审查，无需点击重试。
        {recovery.remaining_seconds > 0 && ` 预计 ${new Date(recovery.retry_after * 1000).toLocaleTimeString('zh-CN')} 后继续；模型工作流需保持启用。`}
        已读取证据与累计额度保留。</p>}
      {recovery.state === 'stopped' && <p>自动恢复停止：{recoveryStopReasons[recovery.stop_reason ?? ''] ?? '请查看失败记录'}。
        已有证据及历史记录保留，尚未生成完整报告。</p>}
    </div>}
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
    {analysis?.error && <p role="alert">{analysisFailureMessage(analysis)}</p>}
    {analysis?.error && analysis.failure_details && <p className="diagnosis-meta">
      失败阶段：{names[analysis.failure_details.stage ?? ''] ?? '准备分析'}
      {' · '}耗时 {analysis.failure_details.elapsed_seconds} 秒
      {analysis.failure_details.http_status !== null && ` · HTTP ${analysis.failure_details.http_status}`}
    </p>}
    {analysis?.error && analysis.provider_response?.finish_reason && <p className="diagnosis-meta">
      模型结束原因：{analysis.provider_response.finish_reason}
      {analysis.provider_response.output_tokens !== undefined && ` · 输出 ${analysis.provider_response.output_tokens} tokens`}
      {analysis.provider_response.reasoning_tokens !== undefined && ` · 推理 ${analysis.provider_response.reasoning_tokens} tokens`}
      {analysis.provider_response.payload_tokens !== undefined && ` · 非推理输出 ${analysis.provider_response.payload_tokens} tokens`}
    </p>}
    {analysis && ['failed', 'interrupted'].includes(analysis.status) &&
      <button className="button secondary" disabled={busy || !analysis.current || recoveryExhausted}
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
      {!!analysis.findings.length && <p>{analysis.findings.filter(item => item.status === 'waiting_reproduction').length} 个疑似问题等待核验。<button className="text-link" onClick={() => onNavigate?.('reproduction')}>前往复现结果</button></p>}
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
          <HistoricalItem key={item.finding_id} item={item} analysis={analysis} api={api} refresh={refresh} />)}
      </section>}

      <button className="button secondary" onClick={() => onNavigate?.('repair')}>查看修复进度 →</button>
      </>}
    </div>
  </section>;
}
