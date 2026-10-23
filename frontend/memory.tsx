import { useCallback, useEffect, useState } from 'react';
import { Archive, ArrowLeft, ArrowRight, BookOpen, History, Pencil, Plus, RefreshCw, RotateCcw, Save, X } from 'lucide-react';

export type MemoryRecord = {
  id: string; repo_id: string; category: string; title: string; content: string;
  revision: string; source: 'owner' | 'workflow';
  evidence: Record<string, unknown>; status: string; version: number; updated: number;
};
type MemoryHistory = { version: number; action: string; record: MemoryRecord };
type Api = <T>(path: string, body?: unknown) => Promise<T>;
type Repo = { id: string; name: string };
type Draft = { category: string; title: string; content: string; revision: string };
type MemoryMetrics = {
  with_memory: Record<string, number>;
  without_memory: Record<string, number>;
};
type MemoryAging = {
  id: string; title: string; category: string; revision: string;
  read_count: number; days_since_updated: number;
  days_since_last_read: number | null; stale: boolean;
};
const categories: Record<string, string> = {
  architecture: '项目结构', setup: '环境与启动', testing: '测试约定',
  bug: '已复现问题', fix: '修复经验', investigation: '未确认记录',
};
const blank = (revision: string): Draft => ({ category: 'architecture', title: '', content: '', revision });
const sourceName = (item: MemoryRecord) => item.source === 'owner' ? '手工知识 · 未验证' :
  item.category === 'fix' ? (item.evidence.merged ? '已合并修复经验' : '修复记录') :
    item.category === 'bug' ? '问题复现记录' : '调查记录';

export function MemoryView({ repositories, api }: {
  repositories: Repo[]; api: Api;
}) {
  const [selected, setSelected] = useState(repositories[0]?.id ?? '');
  return <section className="memory-page">
    {!repositories.length && <div className="empty"><BookOpen size={28} /><p>添加仓库后，即可保存项目知识。</p></div>}
    <div className="memory-scope">
      <label>所属仓库<select aria-label="所属仓库" value={selected} onChange={e => setSelected(e.target.value)}>
        {repositories.map(repo => <option key={repo.id} value={repo.id}>{repo.name}</option>)}
      </select></label>
    </div>
    {selected && <RepositoryMemory key={selected} repoId={selected} revision={''}
      api={api} />}
  </section>;
}

function RepositoryMemory({ repoId, revision, api }: {
  repoId: string; revision: string; api: Api;
}) {
  const [items, setItems] = useState<MemoryRecord[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [query, setQuery] = useState('');
  const [category, setCategory] = useState('');
  const [revisionFilter, setRevisionFilter] = useState('');
  const [status, setStatus] = useState('active');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [draft, setDraft] = useState<Draft | null>(null);
  const [editing, setEditing] = useState<MemoryRecord | null>(null);
  const [history, setHistory] = useState<{ id: string; items: MemoryHistory[] } | null>(null);
  const [reload, setReload] = useState(0);
  const [metrics, setMetrics] = useState<MemoryMetrics | null>(null);
  const [aging, setAging] = useState<MemoryAging[]>([]);
  const base = `/repositories/${repoId}/memories`;
  useEffect(() => {
    let active = true;
    setLoading(true); setError(''); setHistory(null);
    const params = new URLSearchParams({
      query, category, revision: revisionFilter, status, limit: '20', offset: String(offset),
    });
    void api<{ items: MemoryRecord[]; total: number }>(`${base}?${params}`)
      .then(result => { if (active) { setItems(result.items); setTotal(result.total); } })
      .catch(e => { if (active) { setItems([]); setError(e.message); } })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [base, query, category, revisionFilter, status, offset, api, reload]);
  useEffect(() => {
    let active = true;
    setMetrics(null);
    void api<MemoryMetrics>(`/repositories/${repoId}/memory-metrics?revision=${encodeURIComponent(revisionFilter)}`)
      .then(result => { if (active) setMetrics(result); })
      .catch(() => { if (active) setMetrics(null); });
    return () => { active = false; };
  }, [repoId, revisionFilter, api, reload]);
  useEffect(() => {
    let active = true;
    setAging([]);
    const params = new URLSearchParams({ revision: revisionFilter, limit: '5' });
    void api<{ items: MemoryAging[] }>(`/repositories/${repoId}/memory-aging?${params}`)
      .then(result => { if (active) setAging(result.items); })
      .catch(() => { if (active) setAging([]); });
    return () => { active = false; };
  }, [repoId, revisionFilter, api, reload]);
  const action = useCallback(async (fn: () => Promise<void>) => {
    setBusy(true); setError('');
    try { await fn(); } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }, []);
  return <>
    <div className="memory-toolbar">
      <div className="segmented" aria-label="记忆状态">
        {[['active', '有效记忆'], ['archived', '已归档']].map(([value, label]) =>
          <button key={value} className={status === value ? 'active' : ''} onClick={() => {
            setStatus(value); setOffset(0);
          }}>{label}</button>)}
      </div>
      <button className="icon-button" title="刷新记忆" disabled={loading || busy}
        onClick={() => setReload(v => v + 1)}><RefreshCw size={16} /></button>
      <button className="button secondary" disabled={busy} onClick={() => {
        setDraft(blank(revision)); setEditing(null);
      }}><Plus size={16} />添加知识</button>
    </div>
    <div className="memory-filters">
      <label>搜索记忆<input value={query} maxLength={200} onChange={e => { setQuery(e.target.value); setOffset(0); }} /></label>
      <label>记忆分类<select aria-label="记忆分类" value={category} onChange={e => { setCategory(e.target.value); setOffset(0); }}>
        <option value="">全部分类</option>
        {Object.entries(categories).map(([key, name]) => <option key={key} value={key}>{name}</option>)}
      </select></label>
      <label>筛选代码版本<input value={revisionFilter} maxLength={128}
        onChange={e => { setRevisionFilter(e.target.value); setOffset(0); }} /></label>
    </div>
    <details className="disclosure"><summary>知识使用情况与维护建议</summary>
    {metrics && <div className="memory-metrics" aria-label="记忆效果指标">
      <span>有记忆分析 {metrics.with_memory.analyses}</span>
      <span>无记忆分析 {metrics.without_memory.analyses}</span>
      <span>有记忆合并 {metrics.with_memory.merged_resolutions}</span>
      <span>无记忆合并 {metrics.without_memory.merged_resolutions}</span>
    </div>}
    {aging.length > 0 && <div className="memory-aging" aria-label="老化候选">
      {aging.map(item => <div className="memory-aging-row" key={item.id}>
        <strong>{item.title}</strong>
        <span>{item.read_count} 次引用</span>
        <span>{item.days_since_last_read === null ? '从未引用' : `${item.days_since_last_read} 天未引用`}</span>
      </div>)}
    </div>}
    </details>
    {error && <div className="error-inline" role="alert">{error}</div>}
    {draft && <form className="memory-editor" onSubmit={e => {
      e.preventDefault();
      void action(async () => {
        await api(editing ? `${base}/${editing.id}` : base,
          editing ? { ...draft, version: editing.version } : draft);
        setDraft(null); setEditing(null); setOffset(0); setReload(v => v + 1);
      });
    }}>
      <div className="section-heading"><h2>{editing ? '编辑知识' : '添加知识'}</h2>
        <button type="button" className="icon-button" title="取消编辑" disabled={busy}
          onClick={() => { setDraft(null); setEditing(null); }}><X size={17} /></button></div>
      <div className="memory-form-row">
        <label>知识分类<select aria-label="知识分类" value={draft.category} onChange={e => setDraft({ ...draft, category: e.target.value })}>
          {['architecture', 'setup', 'testing'].map(key => <option key={key} value={key}>{categories[key]}</option>)}
        </select></label>
        <label>适用代码版本<input required maxLength={128} value={draft.revision}
          onChange={e => setDraft({ ...draft, revision: e.target.value })} /></label>
      </div>
      <label>知识标题<input required maxLength={160} value={draft.title}
        onChange={e => setDraft({ ...draft, title: e.target.value })} /></label>
      <label>知识内容<textarea required maxLength={4000} rows={5} value={draft.content}
        onChange={e => setDraft({ ...draft, content: e.target.value })} /></label>
      <div className="memory-save"><span className="tag neutral">手工知识 · 未验证</span>
        <button className="button primary" disabled={busy}><Save size={15} />保存知识</button></div>
    </form>}
    {loading ? <div className="loading">正在载入记忆</div> : !items.length ?
      <div className="empty"><BookOpen size={28} /><p>暂无匹配的记忆</p></div> :
      <div className="content-card-grid">{items.map(item => <article className="memory-entry" key={item.id}>
        <div className="memory-entry-heading">
          <div><div className="memory-meta"><span className="tag neutral">{categories[item.category]}</span>
            <span>{sourceName(item)}</span><span>v{item.version}</span></div><h3>{item.title}</h3></div>
          <div className="memory-actions">
            <button className="icon-button" title="查看修改记录" disabled={busy} onClick={() => {
              if (history?.id === item.id) { setHistory(null); return; }
              void action(async () => setHistory({ id: item.id, items: await api<MemoryHistory[]>(`${base}/${item.id}/history`) }));
            }}><History size={16} /></button>
            {item.source === 'owner' && item.status === 'active' && <button className="icon-button"
              title="编辑知识" disabled={busy} onClick={() => {
                setEditing(item); setDraft({ category: item.category, title: item.title, content: item.content, revision: item.revision });
              }}><Pencil size={16} /></button>}
            {item.status === 'active' && <button className="icon-button" title="归档记忆" disabled={busy}
              onClick={() => void action(async () => {
                await api(`${base}/${item.id}/archive`, { version: item.version });
                setOffset(0); setReload(v => v + 1);
              })}><Archive size={16} /></button>}
            {item.status === 'archived' && <button className="icon-button" title="恢复记忆" disabled={busy}
              onClick={() => void action(async () => {
                await api(`${base}/${item.id}/restore`, { version: item.version });
                setOffset(0); setReload(v => v + 1);
              })}><RotateCcw size={16} /></button>}
          </div>
        </div>
        <p>{item.content}</p>
        <div className="memory-meta"><span>代码版本</span><code>{item.revision}</code>
          {typeof item.evidence.pr === 'string' && /^https:\/\/github\.com\//.test(item.evidence.pr) &&
            <a className="text-link" href={item.evidence.pr} target="_blank" rel="noreferrer">来源 PR<ArrowRight size={13} /></a>}</div>
        {!!Object.keys(item.evidence).length && <details className="memory-evidence"><summary>验证证据</summary>
          <pre>{JSON.stringify(item.evidence, null, 2)}</pre></details>}
        {history?.id === item.id && <div className="memory-history">
          {history.items.map(entry => <div key={entry.version}><strong>v{entry.version} · {
            ({ created: '创建', edited: '修改', archived: '归档', restored: '恢复' } as Record<string, string>)[entry.action]
          }</strong><span>{entry.record.title}</span><p>{entry.record.content}</p>
            <code>{entry.record.revision}</code></div>)}
        </div>}
      </article>)}</div>}
    <div className="memory-pagination"><span>{total} 条记忆</span>
      <button className="icon-button" title="上一页记忆" disabled={loading || offset === 0}
        onClick={() => setOffset(Math.max(0, offset - 20))}><ArrowLeft size={16} /></button>
      <span>{Math.floor(offset / 20) + 1}</span>
      <button className="icon-button" title="下一页记忆" disabled={loading || offset + 20 >= total}
        onClick={() => setOffset(offset + 20)}><ArrowRight size={16} /></button></div>
  </>;
}
