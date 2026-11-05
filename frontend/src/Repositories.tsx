import { useState, type FormEvent } from "react";
import { mutate, type State } from "./client";
import { Scans } from "./Scans";

type Preflight = { preflight_id: string; identity: { github_id: string; maintained: boolean; target_sha: string; execution_login: string; target_branch: string; publication: { owner: string; name: string } } };

export function Repositories({ state, run, openTask }: { state: State; run: (action: () => Promise<unknown>, message?: string) => Promise<void>; openTask: (id: string) => void }) {
    const [owner, setOwner] = useState("");
    const [name, setName] = useState("");
    const [target, setTarget] = useState("");
    const [path, setPath] = useState("direct");
    const [forkOwner, setForkOwner] = useState("");
    const [forkName, setForkName] = useState("");
    const [reference, setReference] = useState("");
    const [preflight, setPreflight] = useState<Preflight | null>(null);
    const [mode, setMode] = useState("");
    const [allowed, setAllowed] = useState("");
    const [excluded, setExcluded] = useState("");
    const [severity, setSeverity] = useState("{}");
    const [claim, setClaim] = useState(false);
    const [template, setTemplate] = useState("RepoWatch 将按已确认范围处理此 Issue。");
    const [scanPolicy, setScanPolicy] = useState('{"chunk_bytes":16000,"exclusions":{}}');
    const split = (value: string) => value.split(",").map(label => label.trim()).filter(Boolean);
    const inspect = (event: FormEvent) => {
        event.preventDefault();
        setPreflight(null);
        void run(async () => {
            const result = await mutate<Preflight>("/repositories/preflight", {
                owner, name, target_branch: target || null, contribution_path: path,
                fork_owner: path === "fork" ? forkOwner : null,
                fork_name: path === "fork" ? forkName : null,
                credential_reference: reference || null,
            });
            setPreflight(result);
            const existing = state.repositories.find(repository => repository.github_id === result.identity.github_id);
            const policy = existing?.policy;
            setScanPolicy(JSON.stringify(existing?.scan_policy ?? { chunk_bytes: 16000, exclusions: {} }));
            setMode(policy?.mode ?? (result.identity.maintained ? "all_bugs" : "labels"));
            setAllowed(policy?.allowed_labels.join(",") ?? "");
            setExcluded(policy?.excluded_labels.join(",") ?? "");
            setSeverity(JSON.stringify(policy?.severity_labels ?? {}));
            setClaim(policy?.claim_comment ?? false);
            setTemplate(policy?.claim_template ?? "RepoWatch 将按已确认范围处理此 Issue。");
        }, "贡献路径预检完成，请核对身份、目标及策略。");
    };
    const save = (event: FormEvent) => {
        event.preventDefault();
        if (!preflight) return;
        void run(async () => {
            const existing = state.repositories.find(repository => repository.github_id === preflight.identity.github_id);
            const result = await mutate("/repositories/import", { preflight_id: preflight.preflight_id, policy: {
                mode, allowed_labels: split(allowed), excluded_labels: split(excluded),
                severity_labels: JSON.parse(severity), claim_comment: claim, claim_template: template,
            }, scan_policy: JSON.parse(scanPolicy) }, existing?.revision ?? 0);
            setPreflight(null);
            return result;
        }, "仓库配置已保存并登记完整扫描；请在任务页配置有限预算。");
    };
    return <section><h2>仓库登记与配置</h2>
        <p>每次导入都登记新的完整扫描。目标变化后已有任务及 PR 保留原目标，等待明确处理。</p>
        <form onSubmit={inspect} onChange={() => setPreflight(null)}>
            <div className="fields">
                <label>上游所有者<input value={owner} onChange={event => setOwner(event.target.value)} required /></label>
                <label>仓库名称<input value={name} onChange={event => setName(event.target.value)} required /></label>
                <label>目标分支（空白使用默认分支）<input value={target} onChange={event => setTarget(event.target.value)} /></label>
                <label>贡献路径<select value={path} onChange={event => setPath(event.target.value)}><option value="direct">直接分支</option><option value="fork">自己的 Fork</option></select></label>
                {path === "fork" && <><label>Fork 所有者<input value={forkOwner} onChange={event => setForkOwner(event.target.value)} required /></label><label>Fork 仓库名<input value={forkName} onChange={event => setForkName(event.target.value)} required /></label></>}
                <label>宿主 GitHub 凭据引用<input value={reference} onChange={event => setReference(event.target.value)} required /></label>
            </div><button>读取身份并预检路径</button>
        </form>
        {preflight && <form onSubmit={save}><h3>策略与导入确认</h3>
            <p>执行身份：{preflight.identity.execution_login} · 目标：{preflight.identity.target_branch} @ {preflight.identity.target_sha}</p>
            <p>发布仓库：{preflight.identity.publication.owner}/{preflight.identity.publication.name} · 权限在实际交付时再次核验</p>
            <div className="fields">
                <label>准入模式<select value={mode} onChange={event => setMode(event.target.value)}><option value="manual">手动选择</option><option value="labels">标签准入</option><option value="all_bugs">全部合格 Bug</option></select></label>
                <label>允许标签（逗号分隔）<input value={allowed} onChange={event => setAllowed(event.target.value)} /></label>
                <label>排除标签（逗号分隔）<input value={excluded} onChange={event => setExcluded(event.target.value)} /></label>
                <label>严重程度映射（JSON，0—4）<textarea value={severity} onChange={event => setSeverity(event.target.value)} required /></label>
                <label><input type="checkbox" checked={claim} onChange={event => setClaim(event.target.checked)} />启用认领评论</label>
                <label>认领模板<textarea value={template} onChange={event => setTemplate(event.target.value)} required={claim} /></label>
                <label>扫描策略（JSON：分块字节数、排除路径与依据）<textarea value={scanPolicy} onChange={event => setScanPolicy(event.target.value)} required /></label>
            </div>
            {mode === "labels" && !split(allowed).length && <p className="danger">允许标签待配置；不会自动放宽准入。</p>}
            <p>评论时点：准入、Issue／PR 门禁及扫描均通过，即将实际修复时。默认不修改 assignee。</p>
            {claim && <blockquote>{template}</blockquote>}
            <button>保存配置并导入完整扫描</button>
        </form>}
        {state.repositories.map(repository => <article key={repository.id}><h3>{repository.github_owner}/{repository.github_name || repository.github_id}</h3>
            <p>修订 {repository.revision} · 导入代次 {repository.import_epoch} · 目标 {repository.target_branch || "未配置"}</p>
            <p>扫描就绪：{repository.scan_ready_sha || "未就绪"} · {repository.policy?.mode ?? "待配置"}</p>
            <Scans repositoryId={repository.id} run={run} openTask={openTask} />
            <button onClick={() => { setOwner(repository.github_owner ?? ""); setName(repository.github_name ?? ""); setTarget(repository.target_branch ?? ""); setPath(repository.contribution_path ?? "direct"); setReference(repository.credential_reference ?? ""); setForkOwner(repository.publication?.owner ?? ""); setForkName(repository.publication?.name ?? ""); setPreflight(null); }}>载入并重新预检</button>
            <button onClick={() => void run(() => mutate(`/control/${repository.paused ? "resume" : "pause"}`, { repository_id: repository.id }, repository.revision))}>{repository.paused ? "显式恢复仓库" : "暂停仓库"}</button>
        </article>)}
    </section>;
}
