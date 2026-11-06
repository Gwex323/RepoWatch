export type Amounts = Record<"active_ms" | "model_calls" | "tokens" | "repair_rounds" | "recovery_rounds", number>;
export type Budget = {
    revision: number;
    status: string;
    warning: boolean;
    limits: Amounts;
    used: Amounts;
    reserved: Amounts;
    available: Amounts;
};
export type Task = {
    id: string;
    repository_id: string | null;
    mode: string;
    state: string;
    revision: number;
    wait_reason: string | null;
    outcome: string | null;
    acceptance_revision?: number;
    acceptance?: { revision: number; kind: "bug" | "feature" | "refactor"; content: Record<string, unknown>; approved_revision: number | null; approved_by: string | null; current: boolean; approved: boolean } | null;
    triage_decisions?: Array<{ id: string; result: string; reason_code: string; evidence: Record<string, unknown>; input_revision: number; admission_revision: number }>;
    issue_context?: { digest: string; facts: { issue: Record<string, unknown>; comments: unknown[]; pull_requests: Array<{ id: number; number: number; title: string; html_url: string; state: string }> } } | null;
    publication_intents?: Array<{id: string; kind: string; state: string; request: Record<string, unknown>; result: Record<string, unknown> | null}>;
    repair_sessions?: Array<{id: string; state: string; frozen: Record<string, unknown> | null; evidence: Record<string, unknown> | null}>;
    relation_decisions?: unknown[];
    candidate_issue_decisions?: unknown[];
    distillation_intents?: unknown[];
    artifacts?: Array<{id: string; sha256: string; available: number; context: Record<string, unknown>}>;
    budget_owner_task_id: string;
    input: Record<string, unknown>;
    configuration: Record<string, unknown>;
    budget: Budget;
    runs?: Array<{
        id: string;
        state: string;
        snapshot: Record<string, unknown>;
    }>;
    progress?: unknown[];
    progress_windows?: unknown[];
    attempts?: unknown[];
};
export type Provider = {
    id: string;
    revision: number;
    revision_id: string;
    credential_revision: number;
    configuration: {
        name: string;
        base_url: string;
        api: string;
        models: Array<{
            model: string;
            reasoning_effort?: string | null;
        }>;
    };
};
export type Reason = {
    id: string;
    task_id: string | null;
    code: string;
    revision: number;
    details: Record<string, unknown>;
};
export type Renewal = {
    id: string;
    task_id: string;
    owner_task_id: string;
    revision: number;
    state: string;
    report: Record<string, unknown>;
};
export type Role = {
    binding_key: string;
    revision: number;
    definition: {
        role: string;
        provider_id: string;
        selection: {
            model: string;
            reasoning_effort: string | null;
        };
    };
};
export type State = {
    control: {
        paused: number;
        revision: number;
    };
    repositories: Array<{
        id: string;
        github_id: string;
        paused: number;
        revision: number;
        github_owner?: string;
        github_name?: string;
        target_branch?: string;
        contribution_path?: string;
        credential_reference?: string;
        import_epoch?: number;
        scan_ready_sha?: string;
        publication?: { owner: string; name: string };
        policy?: { mode: string; allowed_labels: string[]; excluded_labels: string[]; severity_labels: Record<string, number>; claim_comment: boolean; claim_template: string };
        scan_policy?: { chunk_bytes?: number; exclusions?: Record<string, string> };
    }>;
    tasks: Task[];
    providers: Provider[];
    roles: Role[];
    reasons: Reason[];
    renewals: Renewal[];
    diagnostics: Array<{
        task_id: string;
        kind: string;
        state: string;
        result: unknown;
    }>;
    capabilities: unknown[];
};
export type BusinessEvent = {
    sequence: number;
    task_id: string | null;
    kind: string;
    payload: Record<string, unknown>;
};
const errors: Record<string, string> = {
    session_required: "控制会话已失效，请从本地启动入口重新打开。",
    revision_or_operation_conflict: "记录已变化或操作标识冲突，请刷新核对后重新提交。",
    operation_not_permitted: "当前前置条件未满足，请先处理阻塞、预算或旧运行。",
    invalid_request: "提交内容无效，请核对必填项和有限额度。",
    storage_unavailable: "无法持久化，执行已停止；请修复存储并核对运行。",
    credential_or_provider_unavailable: "凭据或供应商保存未完成，请核对配置和批次状态。",
};
let csrf = "";
let initialization: Promise<void> | undefined;
export async function request<T>(path: string, options?: RequestInit): Promise<T> {
    const response = await fetch(`/api/v1${path}`, { credentials: "same-origin", ...options });
    const data = await response.json();
    if (!response.ok)
        throw new Error(errors[data.error] ?? `请求失败：${data.error ?? response.status}`);
    return data as T;
}
export function initialize(): Promise<void> {
    if (initialization)
        return initialization;
    initialization = (async () => {
        const parameters = new URLSearchParams(location.hash.slice(1));
        const token = parameters.get("session");
        if (token) { history.replaceState(null, "", location.pathname + location.search); sessionStorage.removeItem("repowatch-event-cursor"); }
        const session = token
            ? await request<{
                csrf: string;
            }>("/session/bootstrap", { method: "POST", headers: { Authorization: `Bearer ${token}` } })
            : await request<{
                csrf: string;
            }>("/session");
        csrf = session.csrf;
    })();
    return initialization;
}
export function mutate<T>(path: string, data: Record<string, unknown>, expectedRevision?: number, operationId = crypto.randomUUID()): Promise<T> {
    return request<T>(path, { method: "POST", headers: { "Content-Type": "application/json", "X-RepoWatch-Session": csrf }, body: JSON.stringify({ operation_id: operationId, expected_revision: expectedRevision, data }) });
}
export const initialAmounts: Amounts = { active_ms: 600000, model_calls: 30, tokens: 100000, repair_rounds: 5, recovery_rounds: 5 };
export const labels: Record<keyof Amounts, string> = { active_ms: "有效时长（毫秒）", model_calls: "模型请求", tokens: "Tokens", repair_rounds: "修复轮数", recovery_rounds: "恢复轮数" };
