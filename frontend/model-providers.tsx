import { useEffect, useState } from 'react';
import { Download, Plus, Save, Trash2 } from 'lucide-react';

type Api = <T>(path: string, body?: unknown) => Promise<T>;
type Model = { model: string; reasoning_effort: string };
type Provider = { id: string; name: string; base_url: string; api: string;
  has_key: boolean; models: Model[]; version: number };
type Choice = { provider_id: string; model: string };
type Catalog = { version: number; enabled: boolean; legacy_available: boolean; legacy_model: string;
  providers: Provider[]; roles: Record<string, string>; routes: Record<string, Choice | null> };
type Draft = { id?: string; name: string; base_url: string; api: string; api_key: string;
  has_key: boolean; models: Model[]; registry_version: number };

const blank = (): Draft => ({ name: '', base_url: '', api: 'chat_completions', api_key: '',
  has_key: false, models: [{ model: '', reasoning_effort: 'auto' }], registry_version: 0 });
const encode = (choice?: Choice | null) => choice ? JSON.stringify([choice.provider_id, choice.model]) : '';
const decode = (value: string): Choice | null => {
  if (!value) return null;
  const [provider_id, model] = JSON.parse(value) as [string, string];
  return { provider_id, model };
};
const errors: Record<string, string> = {
  model_invalid_input: '配置或导入字段不符合要求，请对照示例检查名称、地址、模型与接口类型。',
  model_provider_invalid: '请检查供应商地址、模型名称和推理强度，模型不可重复。',
  model_provider_name_exists: '供应商名称已存在，请使用其他名称。',
  model_provider_in_use: '该供应商或模型仍在使用，请先修改默认或智能体选择。',
  model_providers_changed: '配置已变化，请刷新配置后重试。',
  model_provider_missing: '供应商已不存在，请刷新配置。',
  model_provider_model_missing: '模型已不存在，请重新选择。',
  model_provider_limit: '最多保存 25 个供应商，每个最多 40 个模型。',
  analysis_model_new_key_required: '更换 baseURL 时请填写对应的新 API Key。',
  analysis_model_key_required: '所选供应商尚未保存 API Key。',
  analysis_model_required: '请先选择默认供应商和模型，或保存原有默认配置。',
  model_agent_invalid: '智能体选择无效，请刷新配置。',
  secure_storage_unavailable: '本机系统凭据库不可用，密钥尚未保存。',
};
const importExample = JSON.stringify({ providers: [{ name: '我的供应商',
  base_url: 'https://api.example.com/v1', api: 'chat_completions', api_key: '',
  models: [{ model: 'my-model', reasoning_effort: 'auto' }] }] }, null, 2);

export function selectedModelLabel(value: string, labels: Record<string, string>) {
  try {
    const selected = JSON.parse(value);
    return `${labels[selected.role] ?? selected.role} · ${selected.provider_name} / ${selected.model}`;
  } catch { return '已选择本次模型'; }
}

export function ModelProvidersSettings({ api, legacyVersion }: { api: Api; legacyVersion?: number }) {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [importText, setImportText] = useState('');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const load = () => api<Catalog>('/model-providers').then(setCatalog);
  useEffect(() => {
    let active = true;
    void api<Catalog>('/model-providers').then(value => { if (active) setCatalog(value); })
      .catch(() => { if (active) setNotice('无法读取供应商配置，请刷新重试。'); });
    return () => { active = false; };
  }, [api, legacyVersion]);
  const save = async (path: string, body: unknown, success: string) => {
    setBusy(true); setNotice('');
    try {
      setCatalog(await api<Catalog>(path, body));
      setNotice(success);
      return true;
    } catch (error) {
      setNotice(errors[(error as Error).message] ?? '操作未完成，请检查配置后重试。');
      return false;
    } finally { setBusy(false); }
  };
  const options = () => catalog?.providers.map(p => <optgroup key={p.id} label={p.name}>
    {p.models.map(m => <option key={m.model} value={encode({ provider_id: p.id, model: m.model })}
      disabled={!p.has_key}>{p.name} / {m.model}{!p.has_key ? '（未保存密钥）' : ''}</option>)}
  </optgroup>);
  const endpoint = (choice?: Choice | null) => {
    const selected = choice ?? catalog?.routes.default;
    return selected ? catalog?.providers.find(p => p.id === selected.provider_id)?.base_url
      : catalog?.legacy_available ? `原有默认配置 / ${catalog.legacy_model}` : '尚未选择默认模型';
  };
  const selection = (key: string, label: string) => <label key={key} className="agent-model-choice">
    <span>{label}</span>
    <select aria-label={label} value={encode(catalog?.routes[key])} disabled={busy}
      onChange={e => catalog && setCatalog({ ...catalog,
        routes: { ...catalog.routes, [key]: decode(e.target.value) } })}>
      <option value="">{key === 'default' ? (catalog?.legacy_available
        ? `使用原有默认配置 / ${catalog.legacy_model}` : '请选择默认模型') : '使用默认模型'}</option>
      {options()}
    </select>
    <small>{endpoint(catalog?.routes[key])}</small>
  </label>;
  return <section className="model-providers" aria-label="供应商与智能体模型">
    <div className="section-heading"><h2>供应商与智能体模型</h2>
      <button type="button" className="button secondary" disabled={busy}
        onClick={() => { setNotice(''); void load().catch(() => setNotice('刷新配置失败')); }}>刷新配置</button></div>
    <p>保存多个模型服务，并按智能体的工作内容选择模型。未单独选择的智能体使用默认模型。</p>
    {notice && <p role="status">{notice}</p>}
    {catalog && <>
      <div className="provider-list">
        {catalog.providers.length === 0 && <p className="diagnosis-meta">尚未保存供应商，可新增或导入。</p>}
        {catalog.providers.map(provider => <article key={provider.id} className="provider-card">
          <div><strong>{provider.name}</strong><span className="tag neutral">
            {provider.has_key ? '密钥已保存' : '尚未保存密钥'}</span></div>
          <code>{provider.base_url}</code>
          <p>{provider.api === 'responses' ? 'Responses' : 'Chat Completions'} · {provider.models.map(m => m.model).join('、')}</p>
          <div className="execution-actions">
            <button type="button" className="button secondary" disabled={busy}
              onClick={() => { setNotice(''); setDraft({ ...provider, api_key: '', registry_version: catalog.version,
                models: provider.models.map(m => ({ ...m })) }); }}>编辑 {provider.name}</button>
            <button type="button" className="button secondary" disabled={busy}
              onClick={() => void save(`/model-providers/${provider.id}/delete`,
                { version: catalog.version }, '供应商已删除。')}><Trash2 size={14} />删除 {provider.name}</button>
          </div>
        </article>)}
      </div>
      <button type="button" className="button secondary" disabled={busy || catalog.providers.length >= 25}
        onClick={() => { setNotice(''); setDraft({ ...blank(), registry_version: catalog.version }); }}><Plus size={15} />新增供应商</button>
      {draft && <form className="execution-config provider-editor" aria-label="供应商编辑" onSubmit={e => {
        e.preventDefault();
        void save(draft.id ? `/model-providers/${draft.id}` : '/model-providers', {
          version: draft.registry_version, name: draft.name, base_url: draft.base_url,
          api: draft.api, api_key: draft.api_key, models: draft.models,
        }, '供应商已保存，密钥不会回显。').then(ok => { if (ok) setDraft(null); });
      }}>
        <div className="execution-config-row">
          <label>供应商名称<input required maxLength={80} value={draft.name} disabled={busy}
            onChange={e => setDraft({ ...draft, name: e.target.value })} /></label>
          <label>供应商接口<select value={draft.api} disabled={busy}
            onChange={e => setDraft({ ...draft, api: e.target.value })}>
            <option value="chat_completions">Chat Completions</option><option value="responses">Responses</option>
          </select></label>
        </div>
        <label>供应商 baseURL<input required type="url" maxLength={500} placeholder="https://api.example.com/v1"
          value={draft.base_url} disabled={busy} onChange={e => setDraft({ ...draft, base_url: e.target.value })} /></label>
        <label>供应商 API Key<input type="password" autoComplete="new-password" maxLength={1024}
          placeholder={draft.has_key ? '已保存，留空保留；更换地址需重新填写' : '填写此供应商的 API Key'}
          value={draft.api_key} disabled={busy} onChange={e => setDraft({ ...draft, api_key: e.target.value })} /></label>
        <fieldset><legend>供应商模型</legend>
          {draft.models.map((model, index) => <div className="provider-model-row" key={index}>
            <label>模型名称 {index + 1}<input required maxLength={200} value={model.model} disabled={busy}
              onChange={e => setDraft({ ...draft, models: draft.models.map((m, i) => i === index
                ? { ...m, model: e.target.value } : m) })} /></label>
            <label>推理强度 {index + 1}<select value={model.reasoning_effort} disabled={busy}
              onChange={e => setDraft({ ...draft, models: draft.models.map((m, i) => i === index
                ? { ...m, reasoning_effort: e.target.value } : m) })}>
              <option value="auto">自动适配</option><option value="default">服务默认</option>
              <option value="low">低</option><option value="medium" disabled={model.model.toLowerCase() === 'glm-5.3'}>中</option>
              <option value="high">高</option><option value="max">最高</option>
            </select></label>
            <button type="button" className="button secondary" aria-label={`移除模型 ${index + 1}`}
              disabled={busy || draft.models.length === 1}
              onClick={() => setDraft({ ...draft, models: draft.models.filter((_, i) => i !== index) })}><Trash2 size={15} /></button>
          </div>)}
          <button type="button" className="button secondary" disabled={busy || draft.models.length >= 40}
            onClick={() => setDraft({ ...draft, models: [...draft.models, { model: '', reasoning_effort: 'auto' }] })}><Plus size={15} />添加模型</button>
        </fieldset>
        <div className="execution-actions"><button className="button secondary" disabled={busy}><Save size={15} />保存供应商</button>
          <button type="button" className="button secondary" disabled={busy} onClick={() => setDraft(null)}>取消编辑</button></div>
      </form>}
      <details className="provider-import"><summary>导入供应商 JSON</summary>
        <p>支持单个供应商、供应商数组或下方格式。导入仅新增，不覆盖同名配置；整批验证后一起保存。</p>
        <pre>{importExample}</pre>
        <label>读取 JSON 文件<input type="file" accept=".json,application/json" disabled={busy} onChange={e => {
          const file = e.target.files?.[0];
          if (file) {
            if (file.size > 256000) { setNotice('导入文件不能超过 250 KB。'); return; }
            void file.text().then(setImportText).catch(() => setNotice('无法读取导入文件。'));
          }
          e.target.value = '';
        }} /></label>
        <label>供应商 JSON<textarea rows={7} maxLength={256000} value={importText} disabled={busy}
          autoComplete="off" spellCheck={false} onChange={e => setImportText(e.target.value)} /></label>
        <button type="button" className="button secondary" disabled={busy || !importText.trim()} onClick={() => {
          try {
            const value: unknown = JSON.parse(importText);
            const providers = Array.isArray(value) ? value : value && typeof value === 'object' && 'providers' in value
              ? value.providers : [value];
            void save('/model-providers/import', { version: catalog.version, providers }, '供应商已导入。')
              .then(ok => { if (ok) setImportText(''); });
          } catch { setNotice('JSON 格式不正确，请检查后重试。'); }
        }}><Download size={15} />导入供应商</button>
      </details>
      <form className="execution-config agent-model-form" aria-label="智能体模型选择" onSubmit={e => {
        e.preventDefault(); void save('/agent-models', { version: catalog.version,
          enabled: catalog.enabled, routes: catalog.routes }, '智能体模型选择已保存，下次任务生效。');
      }}>
        <h3>智能体模型选择</h3>
        {selection('default', '默认供应商与模型')}
        {['analysis', 'repair', 'diagnosis'].map(group => <fieldset key={group}>
          <legend>{{ analysis: '仓库分析', repair: '自动修复', diagnosis: '运行诊断' }[group]}</legend>
          <div className="agent-model-grid">{Object.entries(catalog.roles)
            .filter(([key]) => key.startsWith(group + '.')).map(([key, name]) => selection(key, name))}</div>
        </fieldset>)}
        <label className="checkbox-label"><input type="checkbox" checked={catalog.enabled} disabled={busy}
          onChange={e => setCatalog({ ...catalog, enabled: e.target.checked })} />启用智能体模型调用</label>
        <button className="button secondary" disabled={busy}><Save size={15} />保存智能体选择</button>
        <p className="diagnosis-meta">配置适用于后续任务，运行中的任务保留启动时的模型选择。保存不会发起模型请求，自动修复仍需单独启用。</p>
      </form>
    </>}
  </section>;
}
