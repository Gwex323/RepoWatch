import { useEffect, useState } from 'react';
import { Code2, FolderGit2, RefreshCw, Save, Search, ShieldCheck, Terminal, Workflow } from 'lucide-react';

type Api = <T>(path: string, body?: unknown) => Promise<T>;

export type AgentModel = {
  role: string; name: string; description: string; flows: string[];
  model: string; base_url: string; api: string; has_key: boolean; configured: boolean; version: number;
  reasoning_effort: string; effective_reasoning_effort: string | null;
  output_protocol: string; finalization_mode: string;
};

export type AgentModels = {
  enabled: boolean; version: number; roles: AgentModel[];
  flows: Record<string, { ready: boolean; missing_roles: string[] }>;
};

const icons: Record<string, typeof FolderGit2> = {
  repository_analyst: FolderGit2, problem_investigator: Search,
  reproduction_assistant: Terminal, repair_agent: Code2, independent_reviewer: ShieldCheck,
};
const flowNames: Record<string, string> = { analysis: '仓库分析', diagnosis: '环境诊断', repair: '自动修复' };
const errors: Record<string, string> = {
  analysis_model_invalid: '请检查模型名称、服务地址、接口类型和推理强度。',
  analysis_model_changed: '配置已被更新，请刷新配置后重试。',
  analysis_model_key_required: '请为此角色填写 API Key。',
  analysis_model_new_key_required: '更换服务地址时，请重新填写此角色的 API Key。',
  analysis_model_role_invalid: '角色不存在，请刷新页面。',
  secure_storage_unavailable: '本机系统凭据库不可用，密钥尚未保存。',
};

export function agentModelEvent(value: string) {
  try {
    const detail = JSON.parse(value) as { name?: string; role?: string; model?: string };
    return `${detail.name ?? detail.role ?? 'Agent'} · 使用模型 ${detail.model ?? '未记录'}`;
  } catch { return '角色模型配置已记录'; }
}

export function AgentRoleCards({ models, onConfigure }: {
  models?: AgentModels; onConfigure: () => void;
}) {
  if (!models) return <p className="diagnosis-meta">正在读取角色配置…</p>;
  return <div className="agent-role-grid">{models.roles.map(role => {
    const Icon = icons[role.role] ?? Workflow;
    return <article className="agent-role-card" key={role.role}>
      <div className="agent-role-title"><Icon size={18} /><strong>{role.name}</strong></div>
      <p>{role.description}</p>
      <small>{role.flows.join(' · ')}</small>
      <button className="text-link agent-role-model" onClick={onConfigure}>
        {role.configured ? role.model : '选择模型'}
      </button>
      <span className={`tag ${role.configured && models.enabled ? 'green' : 'neutral'}`}>
        {!role.configured ? '未配置' : models.enabled ? '已配置' : '已配置 · 未启用'}
      </span>
    </article>;
  })}</div>;
}

function AgentModelForm({ value, disabled, onSave }: {
  value: AgentModel; disabled: boolean;
  onSave: (role: AgentModel, key: string) => Promise<void>;
}) {
  const [draft, setDraft] = useState(value);
  const [key, setKey] = useState('');
  const Icon = icons[value.role] ?? Workflow;
  return <article className="agent-model-card">
    <div className="agent-role-title"><Icon size={19} /><h3>{value.name}</h3>
      <span className={`tag ${value.configured ? 'green' : 'neutral'}`}>
        {value.configured ? '已配置' : '未配置'}
      </span>
    </div>
    <code className="agent-role-id">{value.role}</code>
    <p className="diagnosis-meta">{value.description}</p>
    <p className="agent-model-flows">{value.flows.join(' · ')}</p>
    <details open={!value.configured}>
      <summary>{value.configured ? `当前模型：${value.model} · 修改配置` : '配置此角色的模型'}</summary>
      <form className="execution-config" onSubmit={e => {
        e.preventDefault();
        void onSave(draft, key).finally(() => setKey(''));
      }}>
        <label>模型名称<input required maxLength={200} disabled={disabled} value={draft.model}
          onChange={e => setDraft({ ...draft, model: e.target.value })} /></label>
        <div className="execution-config-row">
          <label>模型接口<select value={draft.api} disabled={disabled}
            onChange={e => setDraft({ ...draft, api: e.target.value })}>
            <option value="responses">Responses</option><option value="chat_completions">Chat Completions</option>
          </select></label>
          <label>推理强度<select value={draft.reasoning_effort} disabled={disabled}
            onChange={e => setDraft({ ...draft, reasoning_effort: e.target.value })}>
            <option value="auto">自动（按模型适配）</option><option value="default">模型服务默认</option>
            <option value="low">低</option>
            <option value="medium" disabled={draft.model.toLowerCase() === 'glm-5.3'}>中</option>
            <option value="high">高</option><option value="max">最高</option>
          </select></label>
        </div>
        <label>模型服务地址<input required type="url" maxLength={500} disabled={disabled} value={draft.base_url}
          onChange={e => setDraft({ ...draft, base_url: e.target.value })} /></label>
        <label>API Key<input type="password" autoComplete="new-password" maxLength={1024}
          required={!value.has_key || draft.base_url.trim().replace(/\/+$/, '') !== value.base_url}
          disabled={disabled} placeholder={value.has_key ? '已保存，留空保留此角色的密钥' : '填写此角色的密钥'}
          value={key} onChange={e => setKey(e.target.value)} /></label>
        {value.configured && <p className="diagnosis-meta">
          已保存输出协议：{value.output_protocol === 'json_object' ? 'JSON Object' : 'JSON Schema'}
          {value.finalization_mode === 'deepseek_direct' && ' · DeepSeek 兼容模式，推理强度不生效'}
          {value.finalization_mode === 'glm47_direct' && ' · GLM-4.7 直接报告模式'}
        </p>}
        <div className="execution-config-footer"><button className="button secondary" disabled={disabled}>
          <Save size={15} />保存{value.name}模型
        </button></div>
      </form>
    </details>
  </article>;
}

export function AgentModelSettings({ api, onChange }: { api: Api; onChange: () => void }) {
  const [config, setConfig] = useState<AgentModels | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [failed, setFailed] = useState(false);
  const load = () => {
    setBusy(true); setNotice(''); setFailed(false);
    void api<AgentModels>('/agent-models').then(setConfig)
      .catch(() => { setNotice('无法读取角色模型配置，请重试。'); setFailed(true); })
      .finally(() => setBusy(false));
  };
  useEffect(() => {
    let active = true;
    void api<AgentModels>('/agent-models').then(value => { if (active) setConfig(value); })
      .catch(() => { if (active) { setNotice('无法读取角色模型配置，请重试。'); setFailed(true); } });
    return () => { active = false; };
  }, [api]);
  const save = async (role: AgentModel, key: string) => {
    setBusy(true); setNotice(''); setFailed(false);
    try {
      const saved = await api<AgentModels>(`/agent-models/${role.role}`, {
        version: role.version, model: role.model, base_url: role.base_url, api: role.api,
        api_key: key, reasoning_effort: role.reasoning_effort,
      });
      setConfig(saved); setNotice(`已保存${role.name}模型，同一角色在各流程中共用此配置。`);
      onChange();
    } catch (e) {
      setNotice(errors[(e as Error).message] ?? '保存未完成，请检查配置后重试。'); setFailed(true);
    } finally { setBusy(false); }
  };
  return <section className="analysis-model-section" aria-labelledby="agent-model-heading">
    <div className="section-heading"><h2 id="agent-model-heading"><Workflow size={18} />智能体模型配置</h2>
      <span className="section-meta">{config?.roles.length ?? 5} 个 Agent 角色 · 分别选择模型</span>
      <button className="button secondary small" disabled={busy} onClick={load}>
        <RefreshCw size={14} />刷新配置
      </button>
    </div>
    <p className="diagnosis-meta">每个角色可独立配置模型与供应商，也可以填写相同模型。同名角色在分析、诊断和修复中共用配置。保存后在实际调用时验证服务兼容性。</p>
    {config && <>
      <div className="agent-model-switch"><label className="checkbox-label">
        <input type="checkbox" checked={config.enabled} disabled={busy} onChange={e => {
          setBusy(true); setNotice(''); setFailed(false);
          void api<AgentModels>('/agent-models/settings', { version: config.version, enabled: e.target.checked })
            .then(saved => { setConfig(saved); setNotice(saved.enabled ? '已启用模型工作流，就绪的任务将继续。' : '模型工作流已停用，角色配置保留。'); onChange(); })
            .catch(e => { setNotice(errors[e.message] ?? '启用状态保存失败，请重试。'); setFailed(true); })
            .finally(() => setBusy(false));
        }} />启用模型工作流
      </label><span className="diagnosis-meta">自动修复还需单独启用下方的修复开关。</span></div>
      <div className="agent-model-readiness">{Object.entries(config.flows).map(([flow, state]) =>
        <span className={`tag ${state.ready && config.enabled ? 'green' : 'neutral'}`} key={flow}>
          {flowNames[flow] ?? flow}：{!state.ready
            ? `待配置${state.missing_roles.map(role => config.roles.find(item => item.role === role)?.name ?? role).join('、')}`
            : config.enabled ? '模型就绪' : '已配置 · 未启用'}
        </span>)}</div>
    </>}
    {notice && <p role={failed ? 'alert' : 'status'} className={failed ? 'error-inline' : 'diagnosis-meta'}>{notice}</p>}
    {config && <div className="agent-model-grid">{config.roles.map(role =>
      <AgentModelForm key={`${role.role}:${role.version}`} value={role} disabled={busy} onSave={save} />
    )}</div>}
  </section>;
}
