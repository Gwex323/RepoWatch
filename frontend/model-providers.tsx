import { useState } from 'react';
import { Download, Loader2, Plus, PlugZap, Save, Trash2 } from 'lucide-react';

type Api = <T>(path: string, body?: unknown) => Promise<T>;
type Model = { model: string; reasoning_effort: string };
type Provider = { id: string; name: string; base_url: string; api: string;
  has_key: boolean; models: Model[]; version: number };
export type ProviderChoice = { provider_id: string; model: string };
export type RoleChoice = ProviderChoice | { direct: true };
export type ProviderCatalog = { version: number; enabled: boolean; legacy_available: boolean; legacy_model: string;
  providers: Provider[]; roles: Record<string, string>; routes: Record<string, RoleChoice | null>;
  migration_notes: string[] };
type Draft = { id?: string; name: string; base_url: string; api: string; api_key: string;
  has_key: boolean; models: Model[]; registry_version: number };

const blank = (): Draft => ({ name: '', base_url: '', api: 'chat_completions', api_key: '',
  has_key: false, models: [{ model: '', reasoning_effort: 'auto' }], registry_version: 0 });
export const encodeChoice = (choice?: RoleChoice | null) => !choice ? '' : 'direct' in choice
  ? 'direct' : JSON.stringify([choice.provider_id, choice.model]);
export const decodeChoice = (value: string): RoleChoice | null => {
  if (!value) return null;
  if (value === 'direct') return { direct: true };
  const [provider_id, model] = JSON.parse(value) as [string, string];
  return { provider_id, model };
};
export const providerErrors: Record<string, string> = {
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
  analysis_model_key_unavailable: '无法读取已保存的密钥，请重新填写 API Key。',
  analysis_model_invalid: '请检查 baseURL 和接口类型。',
  model_probe_key_invalid: 'API Key 格式不正确，请去除换行或非英文字符。',
  model_probe_model_required: '请先选择或填写测试模型。',
  model_probe_authentication: '供应商拒绝了 API Key，请检查密钥。',
  model_probe_forbidden: '供应商拒绝访问，请检查密钥和模型权限。',
  model_probe_rate_limited: '供应商限流或额度不足，请稍后重试。',
  model_probe_models_unsupported: '供应商不支持当前地址的模型列表接口，可手动填写模型并进行连接测试。',
  model_probe_not_found: '供应商未找到接口或模型，请检查 baseURL、接口类型和模型名称。',
  model_probe_request_rejected: '供应商拒绝了测试参数，请检查接口类型和模型名称。',
  model_probe_redirect: '供应商返回了跳转，请填写最终服务地址后重试。',
  model_probe_service_error: '供应商服务暂时异常，请稍后重试。',
  model_probe_unreachable: '无法连接供应商，请检查地址与网络。',
  model_probe_timeout: '供应商响应超时，请稍后重试。',
  model_probe_response_limit: '供应商响应过大，请手动填写模型。',
  model_probe_invalid_response: '供应商返回内容不符合所选接口格式，请检查服务地址和接口类型。',
  analysis_model_required: '请先选择默认供应商和模型，或保存原有默认配置。',
  model_agent_invalid: '智能体选择无效，请刷新配置。',
  secure_storage_unavailable: '本机系统凭据库不可用，密钥尚未保存。',
};
const importExample = JSON.stringify({ providers: [{ name: '我的供应商',
  base_url: 'https://api.example.com/v1', api: 'chat_completions', api_key: '',
  models: [{ model: 'my-model', reasoning_effort: 'auto' }] }] }, null, 2);

export function ModelProvidersSettings({ api, catalog, onChange, disabled = false }: {
  api: Api; catalog: ProviderCatalog | null; onChange: (value: ProviderCatalog) => void; disabled?: boolean;
}) {
  const [draft, setDraft] = useState<Draft | null>(null);
  const [importText, setImportText] = useState('');
  const [pending, setBusy] = useState(false);
  const busy = pending || disabled;
  const [notice, setNotice] = useState('');
  const [probeBusy, setProbeBusy] = useState<'models' | 'connection' | null>(null);
  const [probeNotice, setProbeNotice] = useState('');
  const [discovered, setDiscovered] = useState<string[]>([]);
  const [modelSearch, setModelSearch] = useState('');
  const [checkedModels, setCheckedModels] = useState<string[]>([]);
  const [testModel, setTestModel] = useState('');
  const openEditor = (value: Draft) => {
    setDraft(value); setNotice(''); setProbeNotice(''); setDiscovered([]);
    setCheckedModels([]); setModelSearch(''); setTestModel(value.models[0]?.model ?? '');
  };
  const updateEndpoint = (changes: Partial<Draft>) => {
    if (draft) setDraft({ ...draft, ...changes });
    setProbeNotice(''); setDiscovered([]); setCheckedModels([]);
  };
  const probe = async (kind: 'models' | 'connection') => {
    if (!draft) return;
    const targetModel = testModel.trim() || draft.models.find(m => m.model.trim())?.model || '';
    setBusy(true); setProbeBusy(kind); setProbeNotice('');
    try {
      const result = await api<{ models?: string[]; truncated?: boolean; connected?: boolean; latency_ms?: number }>(
        `/model-providers/${kind === 'models' ? 'discover' : 'test-connection'}`, {
          version: draft.registry_version, provider_id: draft.id, base_url: draft.base_url,
          api: draft.api, api_key: draft.api_key, model: kind === 'connection' ? targetModel : '',
        });
      if (kind === 'models') {
        if (!Array.isArray(result.models)) throw new Error('model_probe_invalid_response');
        setDiscovered(result.models ?? []); setCheckedModels([]); setModelSearch('');
        setProbeNotice(result.models?.length
          ? `已拉取 ${result.models.length} 个模型，请选择要添加的模型。${result.truncated ? '列表未全部返回，仍可手动填写其他模型。' : ''}`
          : '供应商未返回可用模型，可手动填写。');
      } else {
        if (!result.connected) throw new Error('model_probe_invalid_response');
        setProbeNotice(`连接成功：${targetModel}，已收到模型接口响应，耗时 ${result.latency_ms ?? 0} 毫秒。`);
      }
    } catch (error) {
      if (kind === 'models') { setDiscovered([]); setCheckedModels([]); }
      setProbeNotice(providerErrors[(error as Error).message] ?? '检测未完成，请检查配置后重试。');
    } finally { setBusy(false); setProbeBusy(null); }
  };
  const load = async () => {
    setBusy(true);
    try { onChange(await api<ProviderCatalog>('/model-providers')); }
    catch { setNotice('无法读取供应商配置，请刷新重试。'); }
    finally { setBusy(false); }
  };
  const save = async (path: string, body: unknown, success: string) => {
    setBusy(true); setNotice('');
    try {
      onChange(await api<ProviderCatalog>(path, body));
      setNotice(success);
      return true;
    } catch (error) {
      setNotice(providerErrors[(error as Error).message] ?? '操作未完成，请检查配置后重试。');
      return false;
    } finally { setBusy(false); }
  };
  return <section className="model-providers" aria-label="模型供应商">
    <div className="section-heading"><h2>模型供应商</h2>
      <button type="button" className="button secondary" disabled={busy}
        onClick={() => { setNotice(''); void load(); }}>刷新供应商</button></div>
    <p>集中维护供应商、API Key 和模型列表，再在下方为五个角色分别选择。支持新增、JSON 导入、模型拉取和连接测试。</p>
    {notice && <p role="status">{notice}</p>}
    {catalog && <>
      {catalog.migration_notes.map(note => <p className="diagnosis-meta" key={note}>{note}</p>)}
      <div className="provider-list">
        {catalog.providers.length === 0 && <p className="diagnosis-meta">尚未保存供应商，可新增或导入。</p>}
        {catalog.providers.map(provider => <article key={provider.id} className="provider-card">
          <div><strong>{provider.name}</strong><span className="tag neutral">
            {provider.has_key ? '密钥已保存' : '尚未保存密钥'}</span></div>
          <code>{provider.base_url}</code>
          <p>{provider.api === 'responses' ? 'Responses' : 'Chat Completions'} · {provider.models.map(m => m.model).join('、')}</p>
          <div className="execution-actions">
            <button type="button" className="button secondary" disabled={busy}
              onClick={() => { openEditor({ ...provider, api_key: '', registry_version: catalog.version,
                models: provider.models.map(m => ({ ...m })) }); }}>编辑 {provider.name}</button>
            <button type="button" className="button secondary" disabled={busy}
              onClick={() => void save(`/model-providers/${provider.id}/delete`,
                { version: catalog.version }, '供应商已删除。')}><Trash2 size={14} />删除 {provider.name}</button>
          </div>
        </article>)}
      </div>
      <button type="button" className="button secondary" disabled={busy || catalog.providers.length >= 25}
        onClick={() => openEditor({ ...blank(), registry_version: catalog.version })}><Plus size={15} />新增供应商</button>
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
            onChange={e => updateEndpoint({ api: e.target.value })}>
            <option value="chat_completions">Chat Completions</option><option value="responses">Responses</option>
          </select></label>
        </div>
        <label>供应商 baseURL<input required type="url" maxLength={500} placeholder="https://api.example.com/v1"
          value={draft.base_url} disabled={busy} onChange={e => updateEndpoint({ base_url: e.target.value })} /></label>
        <label>供应商 API Key<input type="password" autoComplete="new-password" maxLength={1024}
          placeholder={draft.has_key ? '已保存，留空保留；更换地址需重新填写' : '填写此供应商的 API Key'}
          value={draft.api_key} disabled={busy} onChange={e => updateEndpoint({ api_key: e.target.value })} /></label>
        <div className="provider-probe">
          <div className="execution-actions">
            <button type="button" className="button secondary" disabled={busy}
              onClick={() => void probe('models')}>{probeBusy === 'models'
                ? <Loader2 size={15} className="spin" /> : <Download size={15} />}拉取供应商模型</button>
            <button type="button" className="button secondary" disabled={busy}
              onClick={() => void probe('connection')}>{probeBusy === 'connection'
                ? <Loader2 size={15} className="spin" /> : <PlugZap size={15} />}连接测试</button>
          </div>
          <label>连接测试模型<input list="provider-test-models" maxLength={200} value={testModel}
            placeholder="填写模型名称，留空使用下方第一个模型" disabled={busy}
            onChange={e => { setTestModel(e.target.value); setProbeNotice(''); }} /></label>
          <datalist id="provider-test-models">{[...new Set([...draft.models.map(m => m.model), ...discovered])]
            .filter(Boolean).map(model => <option key={model} value={model} />)}</datalist>
          <p className="diagnosis-meta">连接测试会发起一次少量输出的模型请求，仅验证基础连接；工具调用与交接需在智能体运行时验证。检测不会保存配置。</p>
          {probeNotice && <p role="status">{probeNotice}</p>}
          {!!discovered.length && <div className="discovered-models">
            <label>筛选拉取的模型<input value={modelSearch} disabled={busy}
              onChange={e => setModelSearch(e.target.value)} /></label>
            <div className="discovered-model-list">{discovered.filter(model => model.toLowerCase().includes(modelSearch.toLowerCase()))
              .map(model => {
                const existing = draft.models.some(m => m.model.trim() === model);
                return <label className="checkbox-label" key={model}>
                  <input type="checkbox" aria-label={`选择模型 ${model}`} checked={existing || checkedModels.includes(model)}
                    disabled={busy || existing} onChange={e => setCheckedModels(e.target.checked
                      ? [...checkedModels, model] : checkedModels.filter(item => item !== model))} />
                  <span>{model}{existing ? '（已添加）' : ''}</span>
                </label>;
              })}</div>
            <button type="button" className="button secondary" disabled={busy || !checkedModels.length}
              onClick={() => {
                const models = draft.models.filter(m => m.model.trim());
                const additions = checkedModels.filter(model => !models.some(m => m.model.trim() === model));
                if (models.length + additions.length > 40) { setProbeNotice('每个供应商最多保存 40 个模型，请减少选择。'); return; }
                setDraft({ ...draft, models: [...models, ...additions.map(model => ({ model, reasoning_effort: 'auto' }))] });
                setCheckedModels([]); setProbeNotice('所选模型已加入编辑内容，保存供应商后生效。');
              }}>添加所选模型（{checkedModels.length}）</button>
          </div>}
        </div>
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
    </>}
  </section>;
}
