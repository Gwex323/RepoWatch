import { useEffect, useState } from 'react';
import { ExternalLink, Loader2, RefreshCw, Save, Wrench } from 'lucide-react';
import { agentModelEvent } from './agent-models';

type Api = <T>(path: string, body?: unknown) => Promise<T>;
type Settings = { enabled: boolean; model_enabled: boolean; version: number; max_attempts: number };
type Verification = Record<string, { command?: string; exit_code: number; log: string } | boolean>;
type Repair = {
  id: string; finding_id: string; version: number; status: string; stage: string | null;
  attempt: number; error: string | null; enabled: boolean; can_retry: boolean; revision: string;
  finding_status?: string | null;
  finding_feedback?: { note: string } | null;
  timeout_recovery_available?: boolean;
  source_read_recovery_available?: boolean;
  submission_recovery_available?: boolean;
  failure_recovery_available?: boolean;
  output_recovery_available?: boolean;
  protocol_recovery_available?: boolean;
  workspace_recovery_available?: boolean;
  verification_recovery_available?: boolean;
  review_evidence_recovery_available?: boolean;
  handoff_recovery_available?: boolean;
  verification_resume_available?: boolean;
  publication_retry_available?: boolean;
  docker_recovery_available?: boolean;
  source_kind?: 'discovered' | 'existing_issue' | null;
  source_issue?: { number: number; url: string } | null;
  source_error?: string | null;
  task_scope?: { total: number; done: number; complete: boolean; reviewed: boolean; combination_pending: boolean };
  context: { finding: { title: string }; publication_retries?: number };
  patch: { summary: string; edits: { path: string }[] } | null;
  diff: string; verification: Verification | null;
  verification_rounds?: { revision: number; diff: string; verification: Verification }[];
  review: { approved: boolean; summary: string; risks: string[] } | null;
  remote: { branch: string; pr: { number: number; url: string }; issue: { number: number; url: string } } | null;
  events: { kind: string; value: string; created: number }[];
  attempts?: { attempt: number; error: string | null; diff: string;
    review: { summary: string } | null }[];
};
const labels: Record<string, string> = {
  submission_required: '探索已结束，要求提交补丁并验证',
  tool_selection_retry: '工具选择不符合当前阶段，整批未执行，正在纠正',
  submission_recovery: '已使用新版提交校验策略的一次恢复机会',
  owner_model_retry: '用户已授权更换修复模型后额外续跑一次',
  backend_handoff: '测试通过，系统已触发独立审查交接',
  backend_review_evidence: '系统正在向审查提供真实验证证据',
  handoff_recovery: '使用保存的补丁复验并恢复独立审查',
  cancelled: '已关闭', false_positive: '误报 · 用户已确认，已关闭',
  queued: '等待修复', waiting_environment: '等待 Docker 环境', running: '修复执行中',
  failed: '修复未完成', interrupted: '修复已中断',
  pr_created: 'PR 已创建', repair_agent: '修改代码', independent_reviewer: '独立审查',
  original_suite: '原版本测试', baseline: '原版本回归验证', candidate: '修复后测试',
  publishing: '发布修复结果',
  original_suite_setup: '原版本测试安装', baseline_setup: '回归验证安装',
  candidate_setup: '修复版本安装', regression_before: '修复前回归测试',
  regression_after: '修复后回归测试', full_suite_after: '修复后完整测试',
  queued_event: '进入修复队列', retry: '重新排队', model_call: '调用模型',
  patch: '补丁已生成', review_approved: '独立审查通过', failure: '本轮未完成',
  cleanup_failed: '验证资源清理未完成', timeout_retry: '模型超时重试',
  output_reasoning_reduced: '输出超限，已临时降低思考强度',
  output_retry: '重新生成完整输出', format_retry: '纠正审查格式',
  output_truncated: '模型输出达到上限，已丢弃未完成内容',
  output_recovery: '已使用新版输出策略恢复机会',
  protocol_recovery: '已使用新版补丁参数与交接策略恢复机会',
  verification_recovery: '已使用新版失败反馈与纠正策略恢复机会',
  patch_unchanged: '补丁未发生实质变化，未重复运行测试',
  workspace_recovery: '已使用新版运行环境与路径策略恢复机会',
  verification_resume: '使用已保存补丁继续真实测试',
  timeout_recovery: '已使用新版超时策略恢复机会',
  source_read_recovery: '已使用新版文件读取策略恢复机会',
  failure_recovery: '已使用新版错误处理恢复机会',
  review_evidence_retry: '审查提前返回结论，已要求先读取验证证据',
  review_evidence_recovery: '使用已保存补丁重新验证并恢复独立审查',
};
const errors: Record<string, string> = {
  repair_paused: '自动修复已暂停。请在「设置 → 自动修复」中启用后重试。',
  repair_model_required: '请先配置代码修复与独立审查模型，并启用模型工作流。',
  repair_model_retry_unavailable: '此次模型变更不满足一次续跑的授权条件。',
  repair_model_changed: '代码修复模型与此次用户授权的配置不一致，本轮已停止。',
  repair_settings_changed: '修复配置已变化，请刷新后重试。',
  repair_stale: '源码、配置或执行证据已变化，需重新分析并复现。',
  repair_source_changed: '本地源码与修复依据不一致，需重新分析。',
  repair_confirmation_required: '此问题尚无有效的本人复现确认。',
  repair_tests_failed: '回归测试未满足修复前失败、修复后通过及完整测试通过的要求。',
  repair_verification_required: '补丁尚未完成真实测试验证，已阻止提前进入独立审查。',
  repair_setup_failed: '修复验证环境安装失败，请查看测试日志。',
  repair_review_rejected: '独立审查未批准补丁，请查看审查结论。',
  repair_review_required: '缺少通过的测试或独立审查，未提交 PR。',
  repair_patch_unchanged: '补丁未发生实质变化，需要先分析失败原因再修改。',
  repair_unsafe_path: '文件路径未通过保护检查。',
  repair_source_path_rejected: '读取的文件类型不在允许的源码范围内，请从可读源码列表选择文件。',
  repair_existing_tests_protected: '补丁尝试修改既有测试，本轮已停止。',
  repair_sensitive_content: '文件包含疑似敏感内容，未继续处理。',
  repair_regression_required: '缺少有效的新增回归测试或源码修改。',
  repair_test_command_required: '请先配置项目测试命令。',
  repair_file_unavailable: '所需文件超出可处理范围或无法读取。',
  repair_model_budget: '模型已达到本轮调用预算。',
  repair_read_budget: '源码读取已达到本轮预算。',
  repair_submission_not_followed: '模型仍未按要求提交补丁，自动纠正已用完，违规调用未执行。',
  repair_tool_selection_invalid: '模型选择了当前阶段不允许的工具或多个调用，自动纠正已用完，整批未执行。',
  repair_patch_limit: '补丁超过大小限制。',
  repair_source_limit: '源码超过下载范围限制，请重新下载并分析。',
  repair_handoff_incomplete: '修复与独立审查未完成规定交接。',
  repair_review_evidence_required: '独立审查未读取验证证据，已阻止发布。',
  repair_output_invalid: '修复模型返回格式无效。',
  repair_output_truncated: '修复模型输出达到上限，本轮自动恢复已结束。请检查模型思考强度或调整修复范围后重试。',
  repair_model_timeout: '修复模型响应超时，自动重试已用完。',
  repair_tool_arguments_invalid: '模型工具参数不符合要求，自动纠正次数已用完。',
  repair_authentication_failed: '模型服务认证失败，请检查 API 密钥。',
  repair_rate_limited: '模型服务限流，请稍后重试或检查服务配额。',
  repair_provider_unreachable: '无法连接模型服务，请检查服务地址与网络。',
  repair_provider_request_rejected: '模型服务拒绝请求，请检查模型名称、接口类型和服务端配置。',
  repair_provider_forbidden: '模型服务拒绝访问，请检查账号权限。',
  repair_provider_not_found: '模型服务地址或模型不存在，请检查配置。',
  repair_provider_error: '模型服务返回错误，请检查服务状态。',
  repair_model_refused: '模型拒绝执行本轮修复。',
  repair_configuration_error: '修复工具配置异常，请查看修复执行记录。',
  repair_workspace_io_error: '本地修复文件读写失败，请检查文件权限和可用磁盘空间。',
  repair_failed: '修复流程发生未分类错误，请查看修复执行记录。',
  repair_interrupted: '服务停止或本轮被中断，可重试。',
  repair_deadline_exceeded: '本轮修复超过三十分钟时限。',
  repair_changed: '任务状态已变化或尝试次数已用完，请刷新。',
  repair_account_changed: 'GitHub 账号已切换或凭据已变化，请检查绑定账号。',
  repair_upstream_changed: '远程默认分支已更新，需下载新版本、重新分析和复现。',
  repair_fork_not_allowed: '没有上游写入权限，且当前仓库不允许本阶段的公开 fork 流程。',
  repair_fork_pending: 'GitHub fork 尚未准备好，请稍后重试。',
  repair_fork_conflict: '账号下已有同名但来源不同的仓库，未覆盖。',
  repair_remote_conflict: '远程分支或发布记录不匹配，已停止，未覆盖已有内容。',
  repair_issue_reference_invalid: '原 Issue 与仓库或分析记录不匹配，已停止；请重新同步 Issue 并分析。',
  repair_scope_changed: '范围、原问题或补丁证据已变化，原发布意图已停止；请先核对已有远程记录。',
  repair_scope_evidence_invalid: '补丁审查的逐项覆盖未通过后端校验，未判定整体完成。',
  repair_issue_publication_conflict: '旧任务已有不同的 Issue / PR 发布记录，请先人工核对远程结果；系统不会再次创建或覆盖。',
  repair_remote_scan_limit: '远程记录超过核对范围，未重复发布。',
  repair_publication_uncertain: '上次发布结果尚不能确认，未重复提交；稍后重试将再次核对。',
  github_active_account_required: '请先连接并选择可用的 GitHub 账号。',
  repair_publication_retry_limit: '仅重试发布的三次机会已用完，请检查权限和发布记录。',
  github_permission_denied: 'GitHub 令牌权限不足。请检查目标仓库授权及 Contents、Issues、Pull requests 写入权限。',
  github_forbidden: 'GitHub 拒绝访问。请检查令牌权限、组织授权及仓库策略；这不一定是限流。',
  github_rate_limited: 'GitHub 请求已被限流，请按执行记录中的等待时间稍后重试。',
  github_denied_or_rate_limited: 'GitHub 权限不足或请求被限流。',
  github_invalid_token: 'GitHub 凭据已失效，请更新账号令牌。',
  github_credential_missing: '无法读取 GitHub 凭据，请重新连接账号。',
  github_unreachable: 'GitHub 请求中断，重试前会先核对远程结果。',
  docker_unavailable: 'Docker 不可用，请启动 Docker Desktop 后重试。',
};
const message = (code: string) => errors[code] ?? '本轮修复未完成，请检查日志和配置。';
const githubDetails = (value: unknown): string => {
  if (!value || typeof value !== 'object') return '';
  const item = value as Record<string, unknown>;
  const parts: string[] = [];
  if (item.status === 403 || item.status === 429) parts.push(`GitHub HTTP ${item.status}`);
  const operations: Record<string, string> = {
    git_blob: '写入文件内容', git_tree: '写入文件树', git_commit: '创建提交',
    git_ref: '创建分支', issue: 'Issue', pull_request: 'PR', fork: 'Fork', read_access: '读取访问',
  };
  if (typeof item.operation === 'string' && operations[item.operation]) parts.push(operations[item.operation]);
  const permissionNames: Record<string, string> = {
    contents: 'Contents', issues: 'Issues', pull_requests: 'Pull requests',
    administration: 'Administration', metadata: 'Metadata',
  };
  const permissionParts = (value: unknown): string[] => {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return [];
    const permissions = value as Record<string, unknown>;
    return Object.entries(permissionNames).flatMap(([key, name]) =>
      permissions[key] === 'read' || permissions[key] === 'write'
        ? [`${name} 需要${permissions[key] === 'write' ? '写入' : '读取'}权限`] : []);
  };
  parts.push(...permissionParts(item.required_permissions));
  if (Array.isArray(item.permission_alternatives)) {
    const alternatives = item.permission_alternatives.map(value => permissionParts(value).join(' + ')).filter(Boolean);
    if (alternatives.length) parts.push(`可选权限方案：${alternatives.join(' 或 ')}`);
  }
  if (Number.isInteger(item.remaining)) parts.push(`剩余配额 ${item.remaining}`);
  if (Number.isInteger(item.retry_after)) parts.push(`至少等待 ${item.retry_after} 秒`);
  if (Number.isInteger(item.reset_at)) parts.push(`配额重置 ${new Date(Number(item.reset_at) * 1000).toLocaleString('zh-CN')}`);
  return parts.join(' · ');
};
const failureDetails = (value: string) => {
  try {
    const item = JSON.parse(value);
    const github = githubDetails(item.github);
    if (github) return github;
    const kinds = Array.isArray(item.exceptions) ? item.exceptions.join('、') : '';
    const locations = Array.isArray(item.locations) ? item.locations
      .filter((p: { file?: unknown; line?: unknown }) => typeof p.file === 'string' && Number.isInteger(p.line))
      .map((p: { file: string; line: number }) => `${p.file}:${p.line}`).join('、') : '';
    return `异常类型：${kinds || '未分类'}${locations ? ` · 定位：${locations}` : ''}`;
  } catch { return '未记录有效的异常诊断'; }
};
const toolEvent = (kind: string, value: string) => {
  const names: Record<string, string> = {
    read_repair_source: '读取源码', submit_and_test_repair: '提交修复补丁',
    read_repair_verification: '读取验证结果',
  };
  if (kind === 'tool_call') return names[value] ?? '调用修复工具';
  try {
    const item = JSON.parse(value);
    const tool = names[item.tool] ?? '修复工具';
    if (kind === 'tool_failure') return `${tool} · ${message(item.code)}`;
    if (kind === 'tool_arguments_normalized') return `${tool} · 已兼容${item.shape === 'missing_wrapper' ? '缺少外层的补丁参数' : '编码为字符串的补丁参数'}，仍执行完整校验`;
    const issues: Record<string, string> = {
      required_field: '缺少必填字段', object_required: '应为 JSON 对象',
      string_required: '应为字符串', list_required: '应为列表',
      unexpected_field: '包含未知字段', invalid_json: '无效 JSON', invalid_value: '字段值不符合要求',
    };
    const reasons = Array.isArray(item.issues) ? item.issues.map((code: string) => issues[code] ?? '字段值不符合要求').join('、') : '';
    const fields = Array.isArray(item.fields) ? item.fields.filter((f: unknown) => typeof f === 'string').join('、') : '';
    return `${tool} · ${item.reason === 'invalid_json' ? '参数 JSON 无效' : '参数结构不匹配'}${fields ? ` · ${fields}` : ''}${reasons ? ` · ${reasons}` : ''}`;
  } catch { return '修复工具参数未通过校验'; }
};
const requestEvent = (kind: string, value: string) => {
  try {
    const item = JSON.parse(value);
    const role = labels[item.role] ?? '模型';
    if (!Number.isFinite(item.timeout_seconds) || !Number.isFinite(item.request)) return '模型请求';
    if (kind === 'request_started') return `${role} · 第 ${item.request} 次请求 · 等待上限 ${item.timeout_seconds} 秒`;
    if (!Number.isFinite(item.elapsed_seconds)) return '模型请求';
    return `${role} · ${kind === 'request_timeout' ? '请求超时' : '响应完成'} · 耗时 ${item.elapsed_seconds} 秒`;
  } catch { return '模型请求'; }
};
const verificationEvent = (kind: string, value: string) => {
  try {
    const item = JSON.parse(value);
    if (!Number.isInteger(item.revision)) return '修复验证记录';
    if (kind === 'verification_round') return `第 ${item.revision} 版验证${item.passed === true ? '通过' : '未通过'}`;
    return `根据第 ${item.revision} 版失败日志分析并纠正 · 还可生成 ${item.corrections_remaining} 版补丁`;
  } catch { return '修复验证记录'; }
};
const providerEvent = (value: string) => {
  try {
    const item = JSON.parse(value);
    const counts = [
      Number.isInteger(item.output_tokens) ? `输出 ${item.output_tokens} token` : '',
      Number.isInteger(item.reasoning_tokens) ? `其中推理 ${item.reasoning_tokens} token` : '',
    ].filter(Boolean).join(' · ');
    return `${item.truncated === true ? '输出已截断' : '模型已响应'}${counts ? ` · ${counts}` : ''}`;
  } catch { return '模型响应记录'; }
};
const sourceBlock = (value: string) => {
  try {
    const item = JSON.parse(value);
    const rules: Record<string, string> = {
      binary_content: '包含二进制内容', credential_pattern: '匹配凭据格式',
      credential_literal: '疑似硬编码凭据', unclassified_assignment: '无法安全判定的赋值',
    };
    if (typeof item.path !== 'string' || !Number.isInteger(item.line) || item.line < 1) return '源码保护检查已阻断文件';
    return `${item.path}:${item.line} · ${item.phase === 'patch' ? '补丁检查' : '源码读取'} · ${rules[item.rule] ?? '疑似敏感内容'}`;
  } catch { return '源码保护检查已阻断文件'; }
};
const unavailableSource = (value: string) => {
  try {
    const item = JSON.parse(value);
    const reasons: Record<string, string> = {
      candidate_not_ready: '补丁尚未生成，应读取原版本', not_found: '文件路径不存在',
      too_large: '超过 32 KB 文件上限', invalid_utf8: '文件不是有效 UTF-8 文本',
      read_error: '文件读取失败',
    };
    if (typeof item.path !== 'string') return '源码暂时无法读取';
    return `${item.path} · ${item.candidate ? '修复版本' : '原版本'} · ${reasons[item.reason] ?? '无法读取'}`;
  } catch { return '源码暂时无法读取'; }
};
const rejectedPath = (value: string, reading = false) => {
  try {
    const item = JSON.parse(value);
    const reasons: Record<string, string> = {
      invalid_path: '路径格式无效，需使用仓库内相对路径和正斜杠',
      protected_directory: '路径位于受保护目录',
      unsupported_file_type: reading ? '文件类型不在允许读取的源码范围内' : '文件类型不允许修改',
      protected_file: '文件受保护', sensitive_name: '文件名包含受保护的敏感词',
      symlink: '路径包含符号链接', outside_root: '路径超出仓库范围',
      duplicate_path: '补丁包含重复路径', case_alias: '路径大小写与既有文件冲突',
    };
    const edit = Number.isInteger(item.edit_index) ? `第 ${item.edit_index + 1} 个修改 · ` : '';
    return `${edit}${reasons[item.reason] ?? '路径未通过保护检查'}`;
  } catch { return '路径未通过保护检查'; }
};

export function RepairSettings({ api, modelEnabled }: { api: Api; modelEnabled?: boolean }) {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  useEffect(() => {
    let active = true;
    void api<Settings>('/repair-settings').then(value => { if (active) setSettings(value); })
      .catch(() => { if (active) setNotice('无法读取自动修复配置'); });
    return () => { active = false; };
  }, [api]);
  return <section className="repair-settings" aria-label="自动修复配置">
    <div className="section-heading"><h2><Wrench size={18} />自动修复</h2>
      <span className="tag neutral">{settings?.enabled ? '已启用' : '已暂停'}</span></div>
    {settings && <form onSubmit={e => {
      e.preventDefault(); setBusy(true); setNotice('');
      void api<Settings>('/repair-settings', { enabled: settings.enabled, version: settings.version })
        .then(value => { setSettings(value); setNotice('自动修复配置已保存'); })
        .catch(e => setNotice(message(e.message))).finally(() => setBusy(false));
    }}>
      <div className="execution-config-footer">
        <label className="checkbox-label"><input type="checkbox" checked={settings.enabled} disabled={busy}
          onChange={e => setSettings({ ...settings, enabled: e.target.checked })} />自动修复并提交 Issue / PR</label>
        <button className="button secondary" disabled={busy}><Save size={15} />保存修复配置</button>
      </div>
      <p className="diagnosis-meta">已有 Issue 关联原编号；新发现的问题先查找相似 Issue，匹配时补充评论，否则新建 Issue，再提交修复 PR。</p>
      <dl className="repair-facts">
        <div><dt>模型</dt><dd>{(modelEnabled ?? settings.model_enabled) ? '代码修复与独立审查模型已就绪' : '等待修复角色模型配置或启用'}</dd></div>
        <div><dt>每个问题</dt><dd>最多 {settings.max_attempts} 次尝试</dd></div>
        <div><dt>最终合并</dt><dd>仓库拥有者审核并操作</dd></div>
      </dl>
    </form>}
    {notice && <p role="status">{notice}</p>}
  </section>;
}

export function RepairPanel({ envId, api, view = 'repair', onNavigate }: { envId: string; api: Api; view?: 'repair' | 'pr'; onNavigate?: (view: string) => void }) {
  const [tasks, setTasks] = useState<Repair[]>([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  useEffect(() => {
    let active = true;
    const load = () => {
      void api<Repair[]>(`/environments/${envId}/repairs`).then(value => {
        if (active) { setTasks(value); setError(''); }
      }).catch(() => { if (active) setError('无法读取修复任务'); });
    };
    load(); const timer = setInterval(load, 2000);
    return () => { active = false; clearInterval(timer); };
  }, [api, envId]);
  const retry = (task: Repair) => {
    setBusy(task.id); setError('');
    void api(`/repairs/${task.id}/retry`, { version: task.version })
      .then(() => api<Repair[]>(`/environments/${envId}/repairs`)).then(setTasks)
      .catch(e => setError(message(e.message))).finally(() => setBusy(''));
  };
  const publication = (task: Repair) => !!task.remote || (task.finding_status !== 'false_positive' &&
    (!!task.publication_retry_available || task.stage === 'publishing' || (task.context.publication_retries ?? 0) > 0));
  if (view === 'pr') return <section aria-label="PR 发布结果">
    <h3>PR 与发布</h3>
    {error && <p role="alert">{error}</p>}
    {!tasks.some(publication) && <p className="action-hint">尚无 PR 或发布任务。修复通过测试和独立审查后，发布结果会显示在这里。</p>}
    <div className="content-card-grid">{tasks.filter(publication).map(task => <article key={task.id}>
      <div className="section-heading"><h4>{task.context.finding.title}</h4><span className="tag neutral">{task.remote ? '已创建 PR' : task.status === 'queued' ? '等待发布' : task.status === 'running' ? '正在发布' : '发布未完成'}</span></div>
      {task.task_scope && <p>任务验收 {task.task_scope.done} / {task.task_scope.total} 项 · {task.task_scope.complete ? '范围已核验' : '仍有待验收项'}</p>}
      {task.remote ? <><code>{task.remote.branch}</code><div className="execution-actions">
        <a className="button primary" href={task.remote.pr.url} target="_blank" rel="noopener noreferrer">打开 PR #{task.remote.pr.number}<ExternalLink size={14} /></a>
        <a className="text-link" href={task.remote.issue.url} target="_blank" rel="noopener noreferrer">关联 Issue #{task.remote.issue.number}</a>
      </div><p className="diagnosis-meta">请在 GitHub 查看最新审查与合并状态。</p></> : <>
        {task.error && <p role="alert">{message(task.error)}</p>}
        {task.source_error && task.source_error !== task.error && <p role="alert">{message(task.source_error)}</p>}
        {task.can_retry && task.publication_retry_available && <button className="button secondary" disabled={!!busy || !task.enabled} onClick={() => retry(task)}>仅重试发布</button>}
      </>}
      <button className="text-link" onClick={() => onNavigate?.('repair')}>查看补丁与验证记录</button>
    </article>)}</div>
  </section>;
  if (tasks.length === 0 && !error) return <p className="action-hint">尚无修复任务。请先在「复现结果」确认问题，并在设置中启用自动修复。</p>;
  return <section className="repository-repairs" aria-label="自动修复任务">
    <div className="section-heading"><h3><Wrench size={17} />自动修复</h3>
      <span className="tag neutral">{tasks.length} 个任务</span></div>
    {error && <p role="alert">{error}</p>}
    {tasks.map(task => <article className="repair-task" key={task.id}>
      <div className="section-heading"><h4>{task.context.finding.title}</h4>
        <span className="tag neutral">{task.finding_status === 'false_positive' ? labels.false_positive : task.finding_status === 'superseded' ? '已转到新版本' : !task.enabled && task.status === 'queued'
          ? '等待启用自动修复' : labels[task.status] ?? task.status}</span></div>
      {task.finding_status === 'false_positive' && <p role="status">{task.finding_feedback?.note}。已停止该问题的修复；下方补丁与验证仅保留为历史记录。</p>}
      {task.finding_status === 'superseded' && <p role="status">已转到最新版本的修复记录；本任务保留为历史。</p>}
      <p className="diagnosis-meta">版本 {task.revision.slice(0, 12)} · 已尝试 {task.attempt} 次
        {task.stage && ` · ${labels[task.stage] ?? task.stage}`}</p>
      {task.source_kind === 'existing_issue' && task.source_issue && <p className="diagnosis-meta">
        来源：<a href={task.source_issue.url} target="_blank" rel="noopener noreferrer">
          原 Issue #{task.source_issue.number}</a> · 修复 PR 将关联原编号</p>}
      {task.source_error && task.source_error !== task.error && <p role="alert">{message(task.source_error)}</p>}
      {task.task_scope && !['false_positive', 'superseded'].includes(task.finding_status ?? '') && <p className="diagnosis-meta">任务验收 {task.task_scope.done} / {task.task_scope.total} 项
        {' · '}{task.task_scope.complete ? '当前范围已核验' : task.task_scope.reviewed
          ? '整体尚未完成，部分修复不关闭原 Issue' : '完整性未核验，不自动关闭原 Issue'}</p>}
      {task.status === 'running' && <p><Loader2 size={14} className="spin" />正在处理修复任务</p>}
      {task.error && task.finding_status !== 'false_positive' && !publication(task) && task.status !== 'waiting_environment' && <p role="alert">
        {task.error === 'docker_unavailable' && '上次失败：'}{message(task.output_recovery_available
        ? 'repair_output_truncated' : task.protocol_recovery_available
          ? 'repair_verification_required' : task.review_evidence_recovery_available
            ? 'repair_review_evidence_required' : task.error)}</p>}
      {task.review_evidence_recovery_available && task.can_retry &&
        <p>补丁已通过测试，但旧版审查遗漏证据读取。可额外恢复一次：重新验证已保存补丁后继续独立审查。</p>}
      {task.handoff_recovery_available && task.can_retry &&
        <p>补丁已验证，旧版模型未完成交接。可恢复一次：保留当前补丁和历史，复验后由系统触发独立审查。</p>}
      {task.timeout_recovery_available && task.can_retry &&
        <p>超时策略已更新，此任务可额外重试一次。</p>}
      {task.source_read_recovery_available && task.can_retry &&
        <p>文件读取策略已更新，此任务可额外重试一次。</p>}
      {task.submission_recovery_available && task.can_retry &&
        <p>旧版任务在要求提交后仍继续读取，未产出补丁。提交校验已更新，可恢复一次，原尝试记录保留。</p>}
      {task.failure_recovery_available && task.can_retry &&
        <p>错误处理已更新，此任务可额外重试一次。</p>}
      {task.output_recovery_available && task.can_retry &&
        <p>本次失败源于输出被截断。输出策略已更新，此任务可额外重试一次。</p>}
      {task.protocol_recovery_available && task.can_retry &&
        <p>补丁参数未通过校验，尚未执行回归测试。参数兼容与交接策略已更新，可额外重试一次。</p>}
      {task.workspace_recovery_available && task.can_retry &&
        <p>运行环境与路径处理已更新，此任务可额外重试一次。</p>}
      {task.verification_recovery_available && task.can_retry &&
        <p>失败反馈与自主纠正已更新，可额外重试一次，并保留之前的失败证据。</p>}
      {task.status === 'waiting_environment' &&
        <p role="status">{task.enabled
          ? '等待 Docker 恢复，系统会自动检查；本次等待不占用修复次数。'
          : '自动修复已暂停；重新启用后检查 Docker，本次等待不占用修复次数。'}</p>}
      {task.docker_recovery_available && task.can_retry &&
        <p>上次在模型调用前因 Docker 不可用而停止；恢复环境后可手动继续一次，保留原尝试历史。</p>}
      {task.verification_resume_available && task.can_retry &&
        <p>补丁已保留，重试将继续测试验证，通过后进入独立审查。</p>}
      {task.status === 'running' && task.events.at(-1)?.kind === 'request_started' &&
        <p className="diagnosis-meta">{requestEvent('request_started', task.events.at(-1)!.value)}</p>}
      {task.events.filter(event => event.kind === 'source_blocked').slice(-3).map((event, i) =>
        <p className="diagnosis-meta" key={`source-block-${i}`}>{sourceBlock(event.value)}</p>)}
      {task.events.filter(event => event.kind === 'source_unavailable').slice(-3).map((event, i) =>
        <p className="diagnosis-meta" key={`source-unavailable-${i}`}>{unavailableSource(event.value)}</p>)}
      {task.events.filter(event => event.kind === 'path_rejected').slice(-2).map((event, i) =>
        <p className="diagnosis-meta" key={`path-rejected-${i}`}>{rejectedPath(event.value)}</p>)}
      {task.events.filter(event => event.kind === 'verification_round' || event.kind === 'verification_feedback')
        .slice(-3).map((event, i) => <p className="diagnosis-meta" key={`verify-${i}`}>
          {verificationEvent(event.kind, event.value)}</p>)}
      {task.events.filter(event => event.kind === 'repair_unresolved').slice(-1).map((event, i) =>
        <p key={`unresolved-${i}`}>未解决说明（模型判断，验证未通过）：{event.value}</p>)}
      {task.events.filter(event => event.kind === 'source_path_rejected').slice(-2).map((event, i) =>
        <p className="diagnosis-meta" key={`source-path-${i}`}>源码读取 · {rejectedPath(event.value, true)}</p>)}
      {task.events.filter(event => event.kind === 'tool_arguments_invalid' || event.kind === 'tool_failure')
        .slice(-2).map((event, i) => <p className="diagnosis-meta" key={`tool-error-${i}`}>
          {toolEvent(event.kind, event.value)}</p>)}
      {task.patch && <><p>{task.patch.summary}</p>
        <ul>{task.patch.edits.map(edit => <li key={edit.path}><code>{edit.path}</code></li>)}</ul></>}
      {task.review && <p className="diagnosis-summary">独立审查：{task.review.summary}</p>}
      {task.review && task.review.risks.length > 0 && <ul>{task.review.risks.map((risk, i) => <li key={i}>{risk}</li>)}</ul>}
      {publication(task) && <button className="button secondary" onClick={() => onNavigate?.('pr')}>查看 PR 与发布状态 →</button>}
      {task.can_retry && !publication(task) && <button className="button secondary" disabled={!!busy || !task.enabled}
        onClick={() => {
          setBusy(task.id); setError('');
          void api(`/repairs/${task.id}/retry`, { version: task.version })
            .then(() => api<Repair[]>(`/environments/${envId}/repairs`)).then(setTasks)
            .catch(e => setError(message(e.message))).finally(() => setBusy(''));
        }}><RefreshCw size={15} />{task.timeout_recovery_available || task.source_read_recovery_available || task.submission_recovery_available || task.failure_recovery_available || task.output_recovery_available || task.protocol_recovery_available || task.workspace_recovery_available || task.verification_recovery_available || task.review_evidence_recovery_available || task.handoff_recovery_available
          ? '使用新策略重试' : task.publication_retry_available ? '仅重试发布' : task.docker_recovery_available ? '继续修复' : task.status === 'waiting_environment' ? '检查并继续' : '重试修复'}</button>}
      {task.diff && <details><summary>代码差异</summary><pre>{task.diff}</pre></details>}
      {task.verification && <details><summary>修复验证记录</summary>
        {Object.entries(task.verification).map(([key, value]) => typeof value === 'object' &&
            <div key={key}><strong>{labels[key] ?? key} · exit {value.exit_code}</strong>
            {value.command && <code>{value.command}</code>}<pre>{value.log}</pre></div>)}
      </details>}
      {!!task.verification_rounds?.length && <details><summary>本次候选版本</summary>
        {task.verification_rounds.map(round => <details key={round.revision}>
          <summary>第 {round.revision} 版 · {round.verification.passed ? '验证通过' : '验证未通过'}</summary>
          <pre>{round.diff}</pre>
          {Object.entries(round.verification).map(([key, value]) => typeof value === 'object' &&
            <div key={key}><strong>{labels[key] ?? key} · exit {value.exit_code}</strong>
              {value.command && <code>{value.command}</code>}<pre>{value.log}</pre></div>)}
        </details>)}
      </details>}
      {task.events.length > 0 && <details><summary>修复执行记录</summary><ol>
        {task.events.map((event, i) => <li key={i}>{new Date(event.created * 1000).toLocaleTimeString('zh-CN')}
          {' · '}{event.kind === 'failure' ? message(event.value) :
            event.kind === 'failure_details' ? failureDetails(event.value) :
            event.kind === 'verification_round' || event.kind === 'verification_feedback' ? verificationEvent(event.kind, event.value) :
            event.kind === 'repair_unresolved' ? `未解决说明（模型判断）：${event.value}` :
            event.kind === 'provider_response' ? providerEvent(event.value) :
            event.kind === 'model_configuration' ? agentModelEvent(event.value) :
            event.kind === 'source_blocked' ? sourceBlock(event.value) :
            event.kind === 'source_unavailable' ? unavailableSource(event.value) :
            event.kind === 'path_rejected' ? rejectedPath(event.value) :
            event.kind === 'source_path_rejected' ? `源码读取 · ${rejectedPath(event.value, true)}` :
            event.kind.startsWith('tool_') ? toolEvent(event.kind, event.value) :
            event.kind.startsWith('request_') ? requestEvent(event.kind, event.value) :
            event.kind === 'environment_wait' ? '等待 Docker 恢复，本次不占用修复次数' :
            event.kind === 'environment_ready' ? 'Docker 已恢复，任务重新排队' :
            event.kind === 'environment_recovery' ? 'Docker 已就绪，继续修复任务' :
            event.kind === 'publication_retry' ? '仅重试已验证补丁的发布，不重新调用模型' :
            event.kind === 'handoff' ? '代码修复 → 独立审查' :
            event.kind === 'review_evidence_retry' || event.kind === 'review_evidence_recovery'
              ? labels[event.kind] :
            labels[event.value] ?? labels[event.kind === 'queued' ? 'queued_event' : event.kind] ?? event.kind}</li>)}
      </ol></details>}
      {!!task.attempts?.length && <details><summary>历史尝试</summary>
        {task.attempts.map(attempt => <div key={attempt.attempt}>
          <strong>第 {attempt.attempt} 次尝试</strong>
          {attempt.error && <p>{message(attempt.error)}</p>}
          {attempt.review && <p>{attempt.review.summary}</p>}
          {attempt.diff && <pre>{attempt.diff}</pre>}
        </div>)}
      </details>}
    </article>)}
  </section>;
}
