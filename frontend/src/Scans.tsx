import { useState } from "react";
import { mutate, request } from "./client";

type Entry = { path: string; disposition: string; reason?: string; module: string; oid: string };
type Batch = {
    id: string; task_id: string; state: string; evidence_valid: boolean; current_ready: boolean;
    version: { target_sha: string; import_epoch: number; scan_policy_revision: number };
    manifest: { entries: Entry[]; units: Array<{ id: string; path: string; start: number; end: number }> };
    coverage: Array<{ unit_id: string; input_artifact_id: string; result_artifact_id: string }>;
    checks: Array<{ id: string; argv: string[]; basis: string }>;
    baselines: Array<{ check_id: string; outcome: string; evidence: { log_artifact_id: string; preparation_report?: { reason: string; gaps: Array<{ path: string; reason: string }>; steps: string[][] } | null } }>;
    attempts?: Array<{ unit_id: string; outcome: string; created_at: number }>;
    candidates: Array<{ id: string; revision: number; state: string; finding: { title: string; basis: string; path: string; sha: string; start: number; end: number }; request_task_id: string | null }>;
};

export function Scans({ repositoryId, run, openTask }: { repositoryId: string; run: (action: () => Promise<unknown>, message?: string) => Promise<void>; openTask: (id: string) => void }) {
    const [batches, setBatches] = useState<Batch[]>([]);
    const load = async () => setBatches((await request<{ batches: Batch[] }>(`/repositories/${repositoryId}/scans`)).batches);
    return <details><summary>扫描清单、覆盖与本地候选</summary>
        <button onClick={() => void run(load, "已读取扫描证据。")}>读取／刷新扫描</button>
        <p>候选保留为本地线索。选择后建立 Issue 查重／创建请求，身份和门禁确认后才能修复。</p>
        {batches.map(batch => <article key={batch.id}>
            <h4>{batch.state === "completed" && batch.evidence_valid ? "覆盖已核验" : "扫描未完成或证据已失效"} · 导入 {batch.version.import_epoch}</h4>
            <p>{batch.current_ready ? "当前仓库扫描就绪" : "此批次不构成当前仓库就绪，不能据此启动修复或发布"}</p>
            <p>SHA {batch.version.target_sha} · 策略 {batch.version.scan_policy_revision} · 覆盖 {batch.coverage.length}/{batch.manifest.units.length} · 补扫尝试 {batch.attempts?.length ?? 0}</p>
            <p>任务 {batch.task_id}：缺环境时可在任务详情补充说明、修改环境配置、申请有界重试或取消；恢复须重新核验。</p>
            <button onClick={() => openTask(batch.task_id)}>打开扫描任务：环境配置／补充材料／重试与取消</button>
            <details><summary>全部 Git 树与排除依据</summary><ul>{batch.manifest.entries.map(entry => <li key={entry.path}>{entry.path} · {entry.module} · {entry.disposition} · {entry.reason ?? entry.oid}</li>)}</ul></details>
            <details><summary>分块覆盖与缺口</summary><ul>{batch.manifest.units.map(unit => {
                const proof = batch.coverage.find(row => row.unit_id === unit.id);
                return <li key={unit.id}>{unit.path} [{unit.start}, {unit.end}) · {proof ? `输入 ${proof.input_artifact_id} ／结果 ${proof.result_artifact_id}` : "缺少有效审查结果"}</li>;
            })}</ul></details>
            {(batch.manifest.entries.some(entry => entry.disposition === "gap") || batch.coverage.length < batch.manifest.units.length) && <p className="danger">当前扫描未完成：存在物化缺口或无有效证据的分块。可在任务页补充环境、申请有界重试或取消。</p>}
            <details><summary>检查基线</summary>{!batch.checks.length && <p>未识别到适用的声明检查；依据为此批次固定清单。</p>}
                <ul>{batch.checks.map(check => {
                    const baseline = batch.baselines.find(row => row.check_id === check.id);
                    return <li key={check.id}>{check.argv.join(" ")} · {check.basis} · {baseline?.outcome ?? "未执行"} · {baseline?.evidence.log_artifact_id ?? "日志缺口"}{baseline?.evidence.preparation_report && <pre>{JSON.stringify(baseline.evidence.preparation_report, null, 2)}</pre>}</li>;
                })}</ul></details>
            {batch.candidates.map(candidate => <article key={candidate.id}>
                <h4>{candidate.finding.title} · {candidate.state}</h4>
                <p>{candidate.finding.path} [{candidate.finding.start}, {candidate.finding.end}) @ {candidate.finding.sha}</p><p>{candidate.finding.basis}</p>
                <button disabled={candidate.state !== "local"} onClick={() => void run(async () => { await mutate(`/scan-candidates/${candidate.id}/decide`, { action: "select" }, candidate.revision); await load(); }, "已保存 Issue 查重／创建请求。")}>选择处理</button>
                <button disabled={candidate.state === "excluded"} onClick={() => void run(async () => { await mutate(`/scan-candidates/${candidate.id}/decide`, { action: "exclude" }, candidate.revision); await load(); }, "已持久排除此候选。")}>排除候选</button>
                {candidate.request_task_id && <p>处理请求：{candidate.request_task_id}</p>}
            </article>)}
        </article>)}
    </details>;
}
