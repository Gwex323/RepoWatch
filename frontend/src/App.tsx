import { useCallback, useEffect, useState, type FormEvent } from "react";
import { initialAmounts, initialize, labels, mutate, request, type Amounts, type Budget, type BusinessEvent, type Provider, type Reason, type Renewal, type State, type Task } from "./client";
import "./styles.css";
type Run = (action: () => Promise<unknown>, message?: string) => Promise<void>;
const statusNames: Record<string, string> = { queued: "排队", running: "执行中", waiting: "等待", blocked: "阻塞", ended: "结束", cancelled: "取消", pending: "待批准", stale: "需刷新", approved: "已批准", rejected: "已拒绝", completed: "通过", failed: "失败", available: "可用", exhausted: "已耗尽", revoked: "已撤销", configuration_required: "待配置" };
const textStatus = (value: string) => statusNames[value] ?? value;
const pretty = (value: unknown) => JSON.stringify(value, null, 2);
function Evidence({ value, title = "查看持久记录" }: {
    value: unknown;
    title?: string;
}) {
    return <details>
<summary>
{title}</summary>
<pre>
{pretty(value)}</pre>
</details>;
}
function AmountFields({ value, onChange }: {
    value: Amounts;
    onChange: (value: Amounts) => void;
}) {
    return <div className="amount-fields">
{(Object.keys(labels) as Array<keyof Amounts>).map(key => <label key={key}>
{labels[key]}<input type="number" min="0" max="9007199254740991" value={value[key]} onChange={event => onChange({ ...value, [key]: Number(event.target.value) })} required/>
</label>)}</div>;
}
function BudgetView({ budget }: {
    budget: Budget;
}) {
    return <>
<p className={budget.status === "exhausted" ? "danger" : "muted"}>预算：{textStatus(budget.status)}{budget.warning ? " · 接近上限" : ""} · 修订 {budget.revision}</p>
<table>
<thead>
<tr>
<th>维度</th>
<th>已用</th>
<th>预留／未知占用</th>
<th>总额</th>
</tr>
</thead>
<tbody>
{(Object.keys(labels) as Array<keyof Amounts>).map(key => <tr key={key}>
<td>
{labels[key]}</td>
<td>
{budget.used[key]}</td>
<td>
{budget.reserved[key]}</td>
<td>
{budget.limits[key] ?? "未配置"}</td>
</tr>)}</tbody>
</table>
</>;
}
function ProviderPanel({ state, run }: {
    state: State;
    run: Run;
}) {
    const [edit, setEdit] = useState<Provider | null>(null);
    const [name, setName] = useState("");
    const [url, setUrl] = useState("");
    const [api, setApi] = useState("chat_completions");
    const [key, setKey] = useState("");
    const [model, setModel] = useState("");
    const [effort, setEffort] = useState("");
    const [batch, setBatch] = useState("");
    const [models, setModels] = useState<Record<string, unknown>>({});
    const select = (provider: Provider) => { setEdit(provider); setName(provider.configuration.name); setUrl(provider.configuration.base_url); setApi(provider.configuration.api); setKey(""); setModel(provider.configuration.models[0]?.model ?? ""); setEffort(provider.configuration.models[0]?.reasoning_effort ?? ""); };
    const save = (event: FormEvent) => { event.preventDefault(); void run(async () => { const payload = { name, base_url: url, api, ...(key ? { api_key: key } : {}), models: [...(model ? [{ model, ...(effort ? { reasoning_effort: effort } : {}) }] : []), ...(edit?.configuration.models.slice(1) ?? [])] }; const result = await mutate(edit ? `/providers/${edit.id}/edit` : "/providers/import", { payload }, edit?.revision); setKey(""); setEdit(null); return result; }, "供应商配置已保存；测试需单独发起。"); };
    return <section>
<h2>供应商配置</h2>
<p className="muted">保存配置不会请求模型。模型列表不可用时，可以手填模型。</p>
<div className="cards">
{state.providers.map(provider => <article key={provider.id}>
<h3>
{provider.configuration.name}</h3>
<p>
{provider.configuration.api} · 配置 {provider.revision} · 凭据 {provider.credential_revision}</p>
<p>
{provider.configuration.base_url}</p>
<div className="actions">
<button onClick={() => select(provider)}>编辑</button>
<button onClick={() => void run(async () => { const result = await mutate(`/providers/${provider.revision_id}/models`, {}); setModels(value => ({ ...value, [provider.id]: result })); }, "模型列表读取结束。")}>显式读取模型列表</button>
</div>
{models[provider.id] !== undefined && <Evidence value={models[provider.id]} title="模型列表结果"/>}</article>)}</div>
    <form onSubmit={save}>
<h3>
{edit ? `编辑 ${edit.configuration.name}` : "新增供应商"}</h3>
<div className="fields">
<label>名称<input value={name} onChange={e => setName(e.target.value)} required/>
</label>
<label>API 地址<input type="url" value={url} onChange={e => setUrl(e.target.value)} required/>
</label>
<label>接口<select value={api} onChange={e => setApi(e.target.value)}>
<option value="chat_completions">Chat Completions</option>
<option value="responses">Responses</option>
</select>
</label>
<label>密钥{edit ? "（留空沿用原修订）" : ""}<input type="password" autoComplete="off" value={key} onChange={e => setKey(e.target.value)} required={!edit}/>
</label>
<label>首个模型（可手填，其余保留）<input value={model} onChange={e => setModel(e.target.value)}/>
</label>
<label>Reasoning effort（可选）<input value={effort} onChange={e => setEffort(e.target.value)}/>
</label>
</div>
<button type="submit">保存配置</button>
{edit && <button type="button" onClick={() => { setEdit(null); setKey(""); }}>取消编辑</button>}</form>
    <form onSubmit={event => { event.preventDefault(); void run(async () => { const result = await mutate("/providers/import", { payload: batch }); setBatch(""); return result; }, "整批导入已完成。"); }}>
<h3>JSON 整批新增</h3>
<p className="muted">支持单对象、数组或 providers 包装；任何冲突整批拒绝。</p>
<label>选择 JSON 文件<input type="file" accept=".json,application/json" onChange={event => { const file = event.target.files?.[0]; if (file)
        void run(async () => { if (file.size > 2 * 1024 * 1024)
            throw new Error("文件超过 2 MiB。"); setBatch(await file.text()); }); }}/>
</label>
<label>JSON 文本<textarea value={batch} onChange={event => setBatch(event.target.value)} required spellCheck={false}/>
</label>
<button type="submit">整批新增</button>
</form>
  </section>;
}
function RolePanel({ state, run }: {
    state: State;
    run: Run;
}) {
    const [binding, setBinding] = useState("processor");
    const [role, setRole] = useState("processor");
    const [providerId, setProviderId] = useState(state.providers[0]?.id ?? "");
    const [model, setModel] = useState("");
    const [effort, setEffort] = useState("");
    return <section>
<h2>角色与模型</h2>
<p className="muted">处理、独审和子任务可分别绑定模型；新运行使用新配置，旧运行保留原快照。连接成功不会自动判定角色就绪。</p>
<div className="cards">
{state.roles.map(item => <article key={item.binding_key}>
<h3>
{item.binding_key}</h3>
<p>
{item.definition.selection.model} · 修订 {item.revision}</p>
<button onClick={() => { setBinding(item.binding_key); setRole(item.definition.role); setProviderId(item.definition.provider_id); setModel(item.definition.selection.model); setEffort(item.definition.selection.reasoning_effort ?? ""); }}>编辑绑定</button>
</article>)}</div>
<form onSubmit={event => { event.preventDefault(); void run(() => mutate(`/roles/${binding}`, { definition: { role, provider_id: providerId, selection: { model, reasoning_effort: effort || null } } }, state.roles.find(item => item.binding_key === binding)?.revision ?? 0), "角色绑定已保存，需核对对应能力。"); }}>
<div className="fields">
<label>绑定标识<input value={binding} onChange={e => setBinding(e.target.value)} list="binding-keys" required/>
<datalist id="binding-keys">
<option value="processor"/>
<option value="reviewer"/>
<option value="subtask.investigator"/>
</datalist>
</label>
<label>角色<select value={role} onChange={e => setRole(e.target.value)}>
<option value="processor">处理</option>
<option value="reviewer">独审</option>
</select>
</label>
<label>供应商<select value={providerId} onChange={e => setProviderId(e.target.value)} required>
<option value="">请选择</option>
{state.providers.map(item => <option key={item.id} value={item.id}>
{item.configuration.name}</option>)}</select>
</label>
<label>模型<input value={model} onChange={e => setModel(e.target.value)} required/>
</label>
<label>Reasoning effort<input value={effort} onChange={e => setEffort(e.target.value)}/>
</label>
</div>
<button type="submit">保存角色绑定</button>
</form>
<Evidence value={state.capabilities} title="能力核验范围与有效期"/>
</section>;
}
function DiagnosticPanel({ state, run }: {
    state: State;
    run: Run;
}) {
    const [kind, setKind] = useState("provider_connection");
    const [provider, setProvider] = useState(state.providers[0]?.revision_id ?? "");
    const [model, setModel] = useState("");
    const [role, setRole] = useState("processor");
    const [effort, setEffort] = useState("");
    const [duration, setDuration] = useState(120);
    const [calls, setCalls] = useState(4);
    const [tokens, setTokens] = useState(100000);
    return <section>
<h2>显式诊断</h2>
<p className="muted">诊断有独立有限预算；旧执行核实停止后独占运行。测试结束仍保留原暂停与业务阻塞。</p>
<form onSubmit={event => { event.preventDefault(); void run(() => mutate("/diagnostics", { kind, parameters: kind === "storage_probe" ? {} : { provider_revision_id: provider, model, ...(kind === "provider_capability" ? { role, ...(effort ? { reasoning_effort: effort } : {}) } : {}) }, duration, limits: { active_ms: duration * 1000, model_calls: calls, tokens, repair_rounds: 1, recovery_rounds: 1 } }, state.control.revision), "诊断已登记，等待安全执行位。"); }}>
<div className="fields">
<label>测试类型<select value={kind} onChange={e => setKind(e.target.value)}>
<option value="provider_connection">模型连接</option>
<option value="provider_capability">角色能力</option>
<option value="storage_probe">存储核验</option>
</select>
</label>
{kind !== "storage_probe" && <>
<label>供应商修订<select value={provider} onChange={e => setProvider(e.target.value)} required>
<option value="">请选择</option>
{state.providers.map(item => <option key={item.id} value={item.revision_id}>
{item.configuration.name} · {item.revision}</option>)}</select>
</label>
<label>测试模型<input value={model} onChange={e => setModel(e.target.value)} required/>
</label>
{kind === "provider_capability" && <>
<label>角色<select value={role} onChange={e => setRole(e.target.value)}>
<option value="processor">处理</option>
<option value="reviewer">独审</option>
</select>
</label>
<label>Reasoning effort<input value={effort} onChange={e => setEffort(e.target.value)}/>
</label>
</>}</>}<label>期限（秒）<input type="number" min="1" max="300" value={duration} onChange={e => setDuration(Number(e.target.value))} required/>
</label>
<label>最多模型请求<input type="number" min="1" max="10" value={calls} onChange={e => setCalls(Number(e.target.value))} required/>
</label>
<label>最多 Tokens<input type="number" min="1" max="100000" value={tokens} onChange={e => setTokens(Number(e.target.value))} required/>
</label>
</div>
<button type="submit">明确发起诊断</button>
</form>
<div className="cards">
{state.diagnostics.map(item => <article key={item.task_id}>
<h3>
{item.kind}</h3>
<p>
{textStatus(item.state)}</p>
<Evidence value={item.result} title="核验结果"/>
</article>)}</div>
</section>;
}
function ReasonForm({ reason, run }: {
    reason: Reason;
    run: Run;
}) {
    const [explanation, setExplanation] = useState("");
    const [steps, setSteps] = useState("");
    const [success, setSuccess] = useState("");
    const [retry, setRetry] = useState(false);
    return <article>
<h3>
{reason.code}</h3>
<p>修订 {reason.revision} · {reason.task_id ?? "全局"}</p>
<Evidence value={reason.details} title="阻塞依据"/>
<form onSubmit={event => { event.preventDefault(); void run(() => mutate(`/reasons/${reason.id}/resolve`, { explanation, ...(retry ? { retry_plan: { steps: steps.split("\n").filter(Boolean), success_condition: success }, retry_limits: { max_attempts: 1, max_steps: 10, max_active_ms: 120000 } } : {}) }, reason.revision), "原因处理已保存；恢复仍需明确操作。"); }}>
<label>实质处理依据<textarea value={explanation} onChange={e => setExplanation(e.target.value)} required/>
</label>
<label className="checkbox">
<input type="checkbox" checked={retry} onChange={e => setRetry(e.target.checked)}/>授权一次有界重试</label>
{retry && <>
<label>具体步骤（每行一步）<textarea value={steps} onChange={e => setSteps(e.target.value)} required/>
</label>
<label>可观察的成功条件<input value={success} onChange={e => setSuccess(e.target.value)} required/>
</label>
<p className="muted">本次最多 1 次尝试、10 个工具步骤、120 秒有效执行。</p>
</>}<button type="submit">提交处理结果</button>
</form>
</article>;
}
function RenewalForm({ renewal, run }: {
    renewal: Renewal;
    run: Run;
}) {
    const [increase, setIncrease] = useState<Amounts>({ active_ms: 60000, model_calls: 5, tokens: 10000, repair_rounds: 1, recovery_rounds: 1 });
    const [reason, setReason] = useState("");
    return <article>
<h3>有限续期 · {textStatus(renewal.state)}</h3>
<Evidence value={renewal.report} title="累计用量、真实进展与剩余计划"/>
<form onSubmit={event => { event.preventDefault(); void run(() => mutate(`/budget/renewals/${renewal.id}/decide`, { decision: "approve", increase, reason }, renewal.revision), "有限追加已批准；其他阻塞保持。"); }}>
<AmountFields value={increase} onChange={setIncrease}/>
<label>批准／修改依据<textarea value={reason} onChange={e => setReason(e.target.value)} required/>
</label>
<div className="actions">
<button disabled={renewal.state !== "pending"} type="submit">批准此有限额度</button>
<button type="button" onClick={() => void run(() => mutate(`/budget/renewals/${renewal.id}/refresh`, { reason: reason || "重新核对最新输入与账本" }, renewal.revision), "申请已刷新，请核对新修订。")}>刷新申请</button>
{["reject", "cancel", "scope_confirmation"].map(decision => <button type="button" key={decision} disabled={renewal.state !== "pending" || !reason} onClick={() => void run(() => mutate(`/budget/renewals/${renewal.id}/decide`, { decision, reason }, renewal.revision))}>
{decision === "reject" ? "拒绝追加" : decision === "cancel" ? "取消任务" : "要求调整方案"}</button>)}</div>
</form>
</article>;
}
function TaskDetail({ task, state, run, onUpdated }: {
    task: Task;
    state: State;
    run: Run;
    onUpdated: () => Promise<void>;
}) {
    const [information, setInformation] = useState("");
    const [configuration, setConfiguration] = useState(pretty(task.configuration));
    const [amounts, setAmounts] = useState(initialAmounts);
    const [reason, setReason] = useState("");
    const [manualPriority, setManualPriority] = useState(0);
    const [reports, setReports] = useState<Record<string, unknown>>({});
    const operate = (action: string, payload?: Record<string, unknown>) => run(async () => { const result = await mutate(`/tasks/${task.id}/operate`, { action, payload }, task.revision); await onUpdated(); return result; });
    const [basis, setBasis] = useState<{basis_sha256: string; ledger: Budget; owner_task_id: string} | null>(null);
    const [basisRefresh, setBasisRefresh] = useState(0);
    useEffect(() => {
      let active = true;
      setBasis(null);
      void request<{basis_sha256: string; ledger: Budget; owner_task_id: string}>(`/tasks/${task.id}/budget/adjustment-basis`).then(value => { if(active) setBasis(value); }).catch(() => {});
      return () => { active = false; };
    }, [task.id, task.revision, task.budget.revision, basisRefresh]);
    const adjust = (event: FormEvent) => {
      event.preventDefault();
      void run(async () => {
        if (task.budget.status === "configuration_required") return mutate(`/tasks/${task.budget_owner_task_id}/budget/initial`, {limits:amounts, reason}, task.budget.revision);
        if (!basis || basis.ledger.revision !== task.budget.revision) throw new Error("预算依据尚未更新，请刷新后核对。");
        return mutate(`/tasks/${basis.owner_task_id}/budget/adjust`, {increase:amounts, basis_sha256:basis.basis_sha256, reason}, basis.ledger.revision);
      }, "有限预算调整已保存；暂停和其他阻塞不会自动解除。");
    };
    return <section className="task-detail">
<h2>任务详情</h2>
<p className="identifier">
{task.id}</p>
<p>
<span className="badge">
{textStatus(task.state)}</span> {task.mode} · 修订 {task.revision}</p>
<p>前置条件／结果：{task.wait_reason ?? task.outcome ?? "以当前执行权、预算和证据门槛为准"}</p>
<div className="actions">
{["select", "request_triage", "cancel", "exclude", "reenable"].map(action => <button key={action} onClick={() => void operate(action)}>
{({ select: "明确选择", request_triage: "重新分诊", cancel: "取消任务", exclude: "排除", reenable: "明确重新启用" })[action]}</button>)}</div>
<Evidence value={task.input} title="当前输入与补充材料"/>
<form onSubmit={event => { event.preventDefault(); void operate("supply_information", { information }); }}>
<label>补充信息<textarea value={information} onChange={e => setInformation(e.target.value)} required/>
</label>
<button type="submit">保存补充材料</button>
</form>
<form onSubmit={event => { event.preventDefault(); void run(async () => { const payload = JSON.parse(configuration); await mutate(`/tasks/${task.id}/operate`, {action:"update_configuration", payload}, task.revision); await onUpdated(); }); }}>
<label>任务策略配置<textarea value={configuration} onChange={e => setConfiguration(e.target.value)} spellCheck={false} required/>
</label>
<button type="submit">保存任务配置</button>
</form>
<form onSubmit={event => { event.preventDefault(); void operate("set_priority", { manual: manualPriority }); }}>
<label>人工优先级<input type="number" min="-1000" max="1000" value={manualPriority} onChange={e => setManualPriority(Number(e.target.value))}/>
</label>
<button type="submit">保存优先级</button>
</form>
<h3>累计预算</h3>
<BudgetView budget={task.budget}/>
<form onSubmit={adjust}>
<h3>
{task.budget.status === "configuration_required" ? "初始有限授权" : "人工有限加额"}</h3>
<p className="muted">修改以下额度后明确提交；不会清零已用额度、未知占用或检测历史。</p>
{task.budget.status !== "configuration_required" && <button type="button" onClick={() => setBasisRefresh(value => value + 1)}>重新核对加额依据</button>}
<AmountFields value={amounts} onChange={setAmounts}/>
<label>依据与处理方案<textarea value={reason} onChange={e => setReason(e.target.value)} required/>
</label>
<button type="submit">确认有限预算</button>
</form>
<Evidence value={task.progress} title="经核验的真实进展"/>
<Evidence value={task.progress_windows} title="无进展窗口与检测阈值"/>
<Evidence value={task.attempts} title="指纹和尝试记录"/>
{task.runs?.map(item => <article key={item.id}>
<h3>运行 · {textStatus(item.state)}</h3>
<p className="identifier">
{item.id}</p>
<Evidence value={item.snapshot} title="固化配置与输入"/>
<button onClick={() => void run(async () => { const result = await mutate(`/runs/${item.id}/report`, {}); setReports(value => ({ ...value, [item.id]: result })); }, "运行核对报告已生成。")}>重新核对旧运行</button>
{reports[item.id] !== undefined && <RecoveryForm report={reports[item.id] as Record<string, unknown>} run={run}/>}</article>)}{state.renewals.filter(item => item.owner_task_id === task.budget_owner_task_id && ["pending", "stale"].includes(item.state)).map(item => <RenewalForm key={`${item.id}:${item.revision}`} renewal={item} run={run}/>)}</section>;
}
function RecoveryForm({ report, run }: {
    report: Record<string, unknown>;
    run: Run;
}) {
    const [explanation, setExplanation] = useState("");
    return <>
<Evidence value={report} title="核对事实与安全结论"/>
<form onSubmit={event => { event.preventDefault(); void run(() => mutate(`/recovery/${String(report.id)}/abandon`, { basis_sha256: report.basis_sha256, explanation }), "旧运行已按报告核对放弃，仍保持暂停。"); }}>
<label>核对依据<textarea value={explanation} onChange={e => setExplanation(e.target.value)} required/>
</label>
<button type="submit" disabled={!(report.facts as Record<string, unknown>)?.safe_to_abandon}>明确放弃已核对的旧执行</button>
</form>
</>;
}
export default function App() {
    const [state, setState] = useState<State | null>(null);
    const [tab, setTab] = useState("tasks");
    const [selected, setSelected] = useState<Task | null>(null);
    const [events, setEvents] = useState<BusinessEvent[]>([]);
    const [error, setError] = useState("");
    const [message, setMessage] = useState("");
    const [busy, setBusy] = useState(false);
    const refresh = useCallback(async () => { setState(await request<State>("/state")); }, []);
    const refreshSelected = useCallback(async () => {
      if (selected) {
        const id = selected.id;
        const detail = await request<Task>(`/tasks/${id}`);
        setSelected(current => current?.id === id ? detail : current);
      }
    }, [selected?.id]);
    useEffect(() => { if(selected) void refreshSelected().catch(failure => setError(String(failure.message))); }, [state, selected?.id]);
    const run: Run = async (action, notice = "操作已保存。") => { setBusy(true); setError(""); setMessage(""); try {
        await action();
        await refresh();
        await refreshSelected();
        setMessage(notice);
    }
    catch (failure) {
        setError(failure instanceof Error ? failure.message : "操作失败");
    }
    finally {
        setBusy(false);
    } };
    useEffect(() => {
        let active = true;
        let source: EventSource | undefined;
        let refreshTimer: ReturnType<typeof setTimeout> | undefined;
        void initialize().then(async () => {
            if (!active) return;
            const stored = Number(sessionStorage.getItem("repowatch-event-cursor") ?? 0);
            let cursor = Number.isSafeInteger(stored) && stored >= 0 ? stored : 0;
            await refresh();
            if (!active) return;
            source = new EventSource(`/api/v1/events/stream?after=${cursor}`);
            source.addEventListener("business", event => {
                if (!active) return;
                const item = JSON.parse((event as MessageEvent).data) as BusinessEvent;
                if (item.sequence <= cursor) return;
                cursor = item.sequence;
                sessionStorage.setItem("repowatch-event-cursor", String(cursor));
                setEvents(value => [...value, item].slice(-100));
                if (refreshTimer) clearTimeout(refreshTimer);
                refreshTimer = setTimeout(() => {
                    void refresh().catch(failure => { if (active) setError(String(failure.message)); });
                }, 100);
            });
        }).catch(failure => { if (active) setError(String(failure.message)); });
        return () => { active = false; source?.close(); if (refreshTimer) clearTimeout(refreshTimer); };
    }, [refresh]);
    return <div className="app">
<header>
<div>
<p className="eyebrow">本地受控工作台</p>
<h1>RepoWatch</h1>
</div>
<div className="actions">
<span className={`badge ${state?.control.paused ? "paused" : "ready"}`}>
{state ? state.control.paused ? "常规队列已暂停" : "常规队列已放行" : "正在连接"}</span>
<button disabled={busy || !state} onClick={() => void run(refresh, "已读取最新状态，未启动任务。")}>刷新状态</button>
{state && <>
<button disabled={busy} onClick={() => void run(() => mutate("/control/pause", {}, state.control.revision), "暂停请求已保存，等待安全停止。")}>全局暂停</button>
<button disabled={busy || !state.control.paused} onClick={() => void run(() => mutate("/control/resume", {}, state.control.revision), "已通过恢复门槛并明确恢复。")}>明确恢复</button>
</>}</div>
</header>
<nav aria-label="工作台导航">
{[["tasks", "任务"], ["items", `待人工事项 ${state ? state.reasons.length + state.renewals.filter(item => ["pending", "stale"].includes(item.state)).length : ""}`], ["providers", "供应商"], ["roles", "角色模型"], ["diagnostics", "诊断"], ["repositories", "仓库"], ["events", "业务事件"]].map(([key, label]) => <button className={tab === key ? "active" : ""} key={key} onClick={() => setTab(key)}>
{label}</button>)}</nav>
<div aria-live="polite">
{error && <p className="notice error" role="alert">
{error}</p>}{message && <p className="notice success" role="status">
{message}</p>}{busy && <p className="muted">正在保存并核对结果…</p>}</div>
{state && <main aria-busy={busy}>
{tab === "tasks" && <div className="task-layout">
<section>
<h2>任务队列</h2>
{state.tasks.length === 0 && <p className="empty">暂无任务。可先配置供应商与角色，再显式发起诊断。</p>}{state.tasks.map(task => <button className="task-row" key={task.id} onClick={() => void run(async () => setSelected(await request<Task>(`/tasks/${task.id}`)), "任务详情已读取。")}>
<span>
<strong>
{task.mode}</strong>
<small>
{task.wait_reason ?? task.outcome ?? task.id}</small>
</span>
<span className="badge">
{textStatus(task.state)}</span>
</button>)}</section>
{selected && <TaskDetail key={selected.id} task={selected} state={state} run={run} onUpdated={refreshSelected}/>}</div>}{tab === "providers" && <ProviderPanel state={state} run={run}/>}{tab === "roles" && <RolePanel state={state} run={run}/>}{tab === "diagnostics" && <DiagnosticPanel state={state} run={run}/>}{tab === "items" && <section>
<h2>待人工事项</h2>
<p className="muted">先查看证据、补充材料或调整配置；处理原因与明确恢复是两次操作。</p>
{state.reasons.map(reason => <ReasonForm key={`${reason.id}:${reason.revision}`} reason={reason} run={run}/>)}{state.renewals.filter(item => ["pending", "stale"].includes(item.state)).map(item => <RenewalForm key={`${item.id}:${item.revision}`} renewal={item} run={run}/>)}{state.tasks.filter(item => ["waiting", "blocked"].includes(item.state)).map(task => <article key={task.id}>
<h3>
{task.mode} · {textStatus(task.state)}</h3>
<p>
{task.wait_reason ?? task.outcome}</p>
<button onClick={() => { setTab("tasks"); void run(async () => setSelected(await request<Task>(`/tasks/${task.id}`))); }}>查看证据并处理任务</button>
</article>)}{state.reasons.length === 0 && state.renewals.every(item => !["pending", "stale"].includes(item.state)) && <p className="empty">当前没有未解决的全局阻塞或预算申请。</p>}</section>}{tab === "repositories" && <section>
<h2>仓库</h2>
{state.repositories.map(repo => <article key={repo.id}>
<h3>
{repo.github_id}</h3>
<p>
{repo.paused ? "仓库已暂停" : "仓库未暂停"}</p>
<button onClick={() => void run(() => mutate(`/control/${repo.paused ? "resume" : "pause"}`, { repository_id: repo.id }, repo.revision))}>
{repo.paused ? "明确恢复此仓库" : "暂停此仓库"}</button>
</article>)}{state.repositories.length === 0 && <p className="empty">尚未导入仓库。系统诊断可在未导入仓库时进行。</p>}</section>}{tab === "events" && <section>
<h2>业务事件</h2>
<p className="muted">按持久游标继续读取，重新打开页面不会启动任务。</p>
{events.slice().reverse().map(event => <article key={event.sequence}>
<h3>#{event.sequence} · {event.kind}</h3>
<Evidence value={event.payload} title="事件内容"/>
</article>)}</section>}</main>}<footer>本地单用户控制 · 所有执行以持久记录和当前授权为准</footer>
</div>;
}
