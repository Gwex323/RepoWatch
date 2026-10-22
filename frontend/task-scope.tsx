import { useEffect, useState } from 'react';
import { Check, ListChecks, Plus, RefreshCw } from 'lucide-react';

type Api = <T>(path: string, body?: unknown) => Promise<T>;
type EvidenceKind = 'regression' | 'integration' | 'client';
type ScopeEvidence = {
  id: string; kind: EvidenceKind; source: 'repair' | 'owner'; reference: string;
  passed: number; valid: boolean; created: number; patch_digest: string | null;
  details: { note?: string; observation?: string; rationale?: string; regression_paths?: string[] };
};
type ScopeItem = {
  key: string; title: string; expected: string; paths: string[];
  required_evidence: EvidenceKind[]; status: string; missing_evidence: EvidenceKind[];
  conclusion: { rationale: string } | null; evidence: ScopeEvidence[];
  findings: { id: string; analysis_id: string; status: string }[];
};
export type TaskScope = {
  id: string; version: number; analysis_id: string; revision: string;
  reviewed: boolean; stale: boolean; complete: boolean; combination_pending: boolean;
  total: number; done: number; counts: Record<string, number>; items: ScopeItem[];
  execution_limits: { key: string; path: string | null; reasons: string[]; count?: number }[];
  pending_limits: { key: string; path: string | null; reasons: string[] }[];
  original: { title?: string; body?: string; request?: string; clipped: boolean };
  review: { rationale: string; exclusions: {
    key: string; outcome: 'required' | 'unrelated' | 'unknown'; rationale: string;
    required_keys: string[];
  }[] } | null;
};
type RepairChoice = {
  id: string; analysis_id: string; revision: string;
  context: { finding: { title: string }; task_scope?: { version: number } };
  patch: unknown; patch_digest: string | null;
};
const states: Record<string, string> = {
  investigating: '待调查', confirmed: '已确认问题', awaiting_verification: '待验证',
  verified_fixed: '已验证修复', verified_no_defect: '已核验无缺陷', blocked: '受阻',
};
const kinds: Record<EvidenceKind, string> = {
  regression: '回归验证', integration: '集成验证', client: '真实客户端／平台验证',
};
const errors: Record<string, string> = {
  scope_changed: '范围或版本已变化，请刷新；新证据必须对应当前范围。',
  scope_observation_required: '请填写核验说明、实际观察结果，并确认本人完成核验。',
  scope_requirement_immutable: '已有验收条件不能删除或降低，请添加新的补充项。',
  scope_path_invalid: '源码路径不在当前分析快照中，请填写仓库内的实际文件路径。',
  scope_limit: '已达到本任务的验收项数量上限，请先检查重复项。',
  scope_evidence_invalid: '核验对象与当前任务或补丁不一致，请刷新后重新选择。',
  scope_mapping_invalid: '验收项与当前任务不一致，请刷新。',
  analysis_changed: '分析状态已变化，请刷新后继续。',
  analysis_busy: '同环境有其他分析或修复正在执行，请完成后再审查范围。',
  analysis_stale: '源码或执行配置已变化，请从当前环境开始新的分析。',
};
const errorText = (error: unknown) => error instanceof Error
  ? errors[error.message] ?? '操作未完成，请刷新并检查任务状态。' : '操作未完成。';

function ObservationForm({ item, scope, repairs, api, refresh }: {
  item: ScopeItem; scope: TaskScope; repairs: RepairChoice[]; api: Api; refresh: () => Promise<void>;
}) {
  const [kind, setKind] = useState<EvidenceKind>(item.required_evidence[0] ?? 'regression');
  const [repair, setRepair] = useState('');
  const [repairDigest, setRepairDigest] = useState<string | null>(null);
  const [passed, setPassed] = useState(true);
  const [note, setNote] = useState('');
  const [observation, setObservation] = useState('');
  const [verified, setVerified] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const candidateChanged = !!repair && !repairs.some(task => task.id === repair
    && task.patch_digest === repairDigest);
  return <details><summary>补充本人核验记录</summary>
    <p className="diagnosis-meta">记录来源标记为本人观察。核验对象必须与实际操作的版本或候选补丁一致；失败结果也应保留。</p>
    <form className="analysis-feedback" onSubmit={event => {
      event.preventDefault(); setBusy(true); setNotice('');
      void api(`/task-scopes/${scope.id}/items/${item.key}/observations`, {
        version: scope.version, kind, passed, note, observation,
        personally_verified: verified, repair_id: repair || null, repair_patch_digest: repairDigest,
      }).then(async () => { setVerified(false); await refresh(); setNotice('核验记录已保存。'); })
        .catch(error => setNotice(errorText(error))).finally(() => setBusy(false));
    }}>
      <label>核验对象<select value={repair} onChange={event => {
        setRepair(event.target.value);
        setRepairDigest(repairs.find(task => task.id === event.target.value)?.patch_digest ?? null);
        setVerified(false);
      }} disabled={busy}>
        <option value="">原始版本 {scope.revision.slice(0, 12)}（用于核验无缺陷）</option>
        {repairs.map(task => <option key={task.id} value={task.id}>
          候选修复：{task.context.finding.title} · {task.id.slice(-8)}</option>)}
      </select></label>
      <label>验证类型<select value={kind} onChange={event => setKind(event.target.value as EvidenceKind)} disabled={busy}>
        {item.required_evidence.map(value => <option key={value} value={value}>{kinds[value]}</option>)}
      </select></label>
      <label>结果<select value={passed ? 'passed' : 'failed'} disabled={busy}
        onChange={event => setPassed(event.target.value === 'passed')}>
        <option value="passed">符合预期</option><option value="failed">未通过</option>
      </select></label>
      <label>环境与核验说明<textarea required maxLength={2000} value={note} rows={2}
        onChange={event => setNote(event.target.value)} disabled={busy} /></label>
      <label>实际步骤、观察与结果（请勿填写密钥）<textarea required maxLength={6000}
        value={observation} rows={3} onChange={event => setObservation(event.target.value)} disabled={busy} /></label>
      <label className="checkbox-label"><input type="checkbox" checked={verified}
        onChange={event => setVerified(event.target.checked)} disabled={busy} />我已在选定版本／补丁上亲自完成此项核验</label>
      {candidateChanged && <p role="status">所选候选补丁已变化，请重新选择并完成核验。</p>}
      <button className="button secondary" disabled={busy || !verified || candidateChanged}><Check size={15} />保存核验记录</button>
      {notice && <p role="status">{notice}</p>}
    </form>
  </details>;
}

export function TaskScopePanel({ scope, analysisId, analysisVersion, analysisStatus, current, envId, api, refresh }: {
  scope: TaskScope; analysisId: string; analysisVersion: number; analysisStatus: string;
  current: boolean; envId: string; api: Api; refresh: () => Promise<void>;
}) {
  const [repairs, setRepairs] = useState<RepairChoice[]>([]);
  const [title, setTitle] = useState('');
  const [expected, setExpected] = useState('');
  const [paths, setPaths] = useState('');
  const [requiredKinds, setRequiredKinds] = useState<EvidenceKind[]>(['regression']);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  useEffect(() => {
    let active = true;
    const load = () => { void api<RepairChoice[]>(`/environments/${envId}/repairs`).then(tasks => {
      if (active) setRepairs(tasks.filter(task => task.analysis_id === analysisId && task.patch
        && task.revision === scope.revision && task.context.task_scope?.version === scope.version));
    }).catch(() => { if (active) setRepairs([]); }); };
    load(); const timer = setInterval(load, 5000);
    return () => { active = false; clearInterval(timer); };
  }, [api, envId, analysisId, scope.revision, scope.version, scope.done]);
  const canReview = current && scope.analysis_id === analysisId
    && ['completed', 'failed', 'interrupted'].includes(analysisStatus);
  return <section className="task-scope" aria-label="任务范围与验收">
    <div className="section-heading"><h4><ListChecks size={17} />任务范围与验收</h4>
      <span className="tag neutral">{scope.done} / {scope.total} 项已核验</span></div>
    <p>{scope.complete ? '当前已登记范围的必需项均已通过同一版本或组合补丁的核验。'
      : scope.combination_pending ? '各项已有单独验证；组合验收仍待验证，不能据此关闭原 Issue。'
        : scope.reviewed ? '整体任务尚未完成；部分修复可以提交，未完成项继续保留。'
          : '完整性未核验：原始任务范围尚未通过当前版本的独立审查。'}</p>
    {scope.stale && <p role="status">原问题或执行依据已变化，请继续范围审查；执行依据过期时需开始新分析。</p>}
    {scope.original.clipped && <p role="status">原始问题内容存在截断，不能自动判定整体完成。</p>}
    {!!scope.pending_limits.length && <p role="status">尚有 {scope.pending_limits.length} 项跳过或排除的验证未核清关联，不能自动判定整体完成。</p>}
    {!!scope.execution_limits.length && <details><summary>跳过与排除的验证范围</summary>
      <ul>{scope.execution_limits.map(limit => {
        const classification = scope.review?.exclusions.find(item => item.key === limit.key);
        return <li key={limit.key}>
        {limit.path ?? `被跳过的测试（${limit.count ?? 0} 项）`} · {limit.reasons.map(reason => ({
          windows_native: '依赖 Windows 原生能力', gui: '依赖桌面交互', skipped: '未执行',
          unknown_dependency: '依赖未确认', dynamic_import: '动态依赖未确认',
        } as Record<string, string>)[reason] ?? reason).join('、')}
        {' · '}{classification?.outcome === 'required' ? '关联必需验收'
          : classification?.outcome === 'unrelated' ? '已审查为与当前任务无关' : '关联尚未核清'}
        {classification && <p>{classification.rationale}</p>}
        {classification?.required_keys.length ? <p>关联验收项：{classification.required_keys.map(key =>
          scope.items.find(item => item.key === key)?.title ?? '待核对').join('、')}</p> : null}
        </li>;
      })}</ul>
    </details>}
    <details><summary>原始问题与范围审查</summary>
      <p>{scope.original.title ?? scope.original.request}</p>
      {scope.original.body && <pre>{scope.original.body}</pre>}
      <p>{scope.review?.rationale ?? '尚无独立范围审查记录。'}</p>
    </details>
    {scope.total === 0 && <p>尚未拆解验收项；未登记不代表没有待办。</p>}
    {scope.items.map(item => <article className="analysis-finding" key={`${scope.version}-${item.key}`}>
      <div className="section-heading"><h4>{item.title}</h4>
        <span className="tag neutral">{states[item.status] ?? '待调查'}</span></div>
      <p>{item.expected}</p>
      <p className="diagnosis-meta">涉及：{item.paths.join('、')} · 必需：{item.required_evidence.map(kind => kinds[kind]).join('、')}</p>
      {item.conclusion && <p>{item.conclusion.rationale}</p>}
      {!!item.missing_evidence.length && <p>待补证据：{item.missing_evidence.map(kind => kinds[kind]).join('、')}</p>}
      {!!item.evidence.length && <details><summary>证据与核验历史（最近 {Math.min(12, item.evidence.length)} 条）</summary>
        {item.evidence.slice(-12).map(evidence => <div key={evidence.id}>
          <p>{kinds[evidence.kind]} · {evidence.source === 'owner' ? '本人观察' : '系统补丁验证及独立审查'}
            {' · '}{!evidence.passed ? '未通过' : evidence.valid ? '计入当前验收' : '未计入当前验收'}
            {' · '}{new Date(evidence.created * 1000).toLocaleString('zh-CN')}</p>
          <p>{evidence.details.rationale ?? evidence.details.note}</p>
          {evidence.details.observation && <pre>{evidence.details.observation}</pre>}
          {evidence.details.regression_paths && <p>{evidence.details.regression_paths.join('、')}</p>}
        </div>)}
      </details>}
      {scope.reviewed && current && !scope.stale && <ObservationForm item={item} scope={scope}
        repairs={repairs} api={api} refresh={refresh} />}
    </article>)}
    {canReview && <>
      <button className="button secondary" disabled={busy} onClick={() => {
        setBusy(true); setNotice('');
        void api(`/analyses/${analysisId}/scope-review`, { version: analysisVersion })
          .then(refresh).catch(error => setNotice(errorText(error))).finally(() => setBusy(false));
      }}><RefreshCw size={15} />继续分析与范围审查</button>
      <p className="diagnosis-meta">使用已启用的模型工作流，保留既有发现和验收条件；旧证据需按新范围重新核验。</p>
      <details><summary>补充遗漏的验收项</summary>
        <p>新增条件会使范围重新待审。已有条件和证据历史会保留。</p>
        <form className="analysis-feedback" onSubmit={event => {
          event.preventDefault(); setBusy(true); setNotice('');
          void api(`/analyses/${analysisId}/scope/requirements`, {
            version: scope.version, items: [{ key: `owner_${Date.now().toString(36)}`,
              title, expected, paths: paths.split(/[\n,，]/).map(value => value.trim()).filter(Boolean),
              required_evidence: requiredKinds }],
          }).then(async () => { setTitle(''); setExpected(''); setPaths(''); await refresh(); })
            .catch(error => setNotice(errorText(error))).finally(() => setBusy(false));
        }}>
          <label>验收项标题<input required maxLength={200} value={title} disabled={busy}
            onChange={event => setTitle(event.target.value)} /></label>
          <label>预期行为<textarea required maxLength={1600} rows={3} value={expected} disabled={busy}
            onChange={event => setExpected(event.target.value)} /></label>
          <label>涉及源码路径（每行一个）<textarea required rows={2} value={paths} disabled={busy}
            onChange={event => setPaths(event.target.value)} /></label>
          <fieldset><legend>必要验证</legend>{(Object.keys(kinds) as EvidenceKind[]).map(kind =>
            <label className="checkbox-label" key={kind}><input type="checkbox" disabled={busy}
              checked={requiredKinds.includes(kind)} onChange={event => setRequiredKinds(previous =>
                event.target.checked ? [...previous, kind] : previous.filter(value => value !== kind))} />{kinds[kind]}</label>)}</fieldset>
          <button className="button secondary" disabled={busy || !requiredKinds.length}><Plus size={15} />添加验收项</button>
        </form>
      </details>
    </>}
    {notice && <p role="status">{notice}</p>}
  </section>;
}
