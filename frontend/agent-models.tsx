import { useEffect, useState } from 'react';
import { Code2, FolderGit2, RefreshCw, Save, Search, ShieldCheck, Terminal, Workflow } from 'lucide-react';
import { decodeChoice, encodeChoice, ModelProvidersSettings, providerErrors } from './model-providers';
import type { ProviderCatalog, RoleChoice } from './model-providers';

type Api = <T>(path: string, body?: unknown) => Promise<T>;

type ModelConfig = {
  model: string; base_url: string; api: string; has_key: boolean; configured: boolean; version: number;
  reasoning_effort: string; effective_reasoning_effort: string | null;
  output_protocol: string; finalization_mode: string;
};

export type AgentModel = ModelConfig & {
  role: string; name: string; description: string; flows: string[];
  provider_id: string | null; provider_name: string; provider_version: number;
  selection_choice: RoleChoice | null; selection_error?: string;
  direct_config?: ModelConfig;
};

export type AgentModels = {
  enabled: boolean; version: number; roles: AgentModel[];
  flows: Record<string, { ready: boolean; missing_roles: string[] }>;
};
type EscalationRoute = { provider_id: string; model: string } | null;
type EscalationModel = {
  route: EscalationRoute; provider_id: string; provider_name: string; model: string;
  has_key: boolean;
} | null;
type EscalationStatus = {
  enabled: boolean; version: number; monitor_route: EscalationRoute; repair_route: EscalationRoute;
  monitor: EscalationModel; repair: EscalationModel; ready: boolean; missing: string[];
};

const icons: Record<string, typeof FolderGit2> = {
  repository_analyst: FolderGit2, problem_investigator: Search,
  reproduction_assistant: Terminal, repair_agent: Code2, independent_reviewer: ShieldCheck,
};
const flowNames: Record<string, string> = { analysis: '仓库分析', diagnosis: '环境诊断', repair: '自动修复', coordination: '系统协调' };
const errors: Record<string, string> = {
  ...providerErrors,
  analysis_model_invalid: '请检查模型名称、服务地址、接口类型和推理强度。',
  analysis_model_changed: '配置已被更新，请刷新配置后重试。',
  analysis_model_key_required: '请为此角色填写 API Key。',
  analysis_model_new_key_required: '更换服务地址时，请重新填写此角色的 API Key。',
  analysis_model_role_invalid: '角色不存在，请刷新页面。',
  model_escalation_changed: '模型升级策略已被更新，请刷新后重试。',
  model_escalation_route_required: '请选择监控模型和修复模型。',
  model_provider_missing: '供应商已不存在，请刷新配置。',
  model_provider_model_missing: '模型已不存在，请重新选择。',
  secure_storage_unavailable: '本机系统凭据库不可用，密钥尚未保存。',
};

export function agentModelEvent(value: string) {
  try {
    const detail = JSON.parse(value) as { name?: string; role?: string; model?: string; provider_name?: string };
    return `${detail.name ?? detail.role ?? 'Agent'} · ${detail.provider_name ? detail.provider_name + ' / ' : ''}${detail.model ?? '未记录'}`;
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
        {role.configured ? `${role.provider_id ? role.provider_name + ' / ' : ''}${role.model}` : '选择模型'}
      </button>
      <span className={`tag ${role.configured && models.enabled ? 'green' : 'neutral'}`}>
        {!role.configured ? '未配置' : models.enabled ? '已配置' : '已配置 · 未启用'}
      </span>
    </article>;
  })}</div>;
}

function ProviderOptions({ catalog }: { catalog: ProviderCatalog }) {
  return <>{catalog.providers.map(provider => <optgroup key={provider.id} label={provider.name}>
    {provider.models.map(model => <option key={model.model} disabled={!provider.has_key}
      value={encodeChoice({ provider_id: provider.id, model: model.model })}>
      {provider.name} / {model.model}{provider.has_key ? '' : '（未保存密钥）'}
    </option>)}
  </optgroup>)}</>;
}

function DefaultModelForm({ catalog, disabled, onSelect }: {
  catalog: ProviderCatalog; disabled: boolean; onSelect: (role: string, choice: RoleChoice | null) => Promise<void>;
}) {
  const [choice, setChoice] = useState(encodeChoice(catalog.routes.default));
  return <form className="execution-config default-model-form" onSubmit={e => {
    e.preventDefault(); void onSelect('default', decodeChoice(choice));
  }}>
    <label>默认供应商与模型<select disabled={disabled} value={choice} onChange={e => setChoice(e.target.value)}>
      <option value="">使用各角色原有独立配置</option><ProviderOptions catalog={catalog} />
    </select></label>
    <button className="button secondary" disabled={disabled}><Save size={15} />保存默认选择</button>
    <p className="diagnosis-meta">未单独选择供应商的角色使用默认模型；选择“使用此角色独立配置”可保留手工配置。</p>
  </form>;
}

const escalationValue = (route: EscalationRoute) => route
  ? JSON.stringify([route.provider_id, route.model]) : '';

function ModelEscalationSettings({ api, catalog, disabled, onChange }: {
  api: Api; catalog: ProviderCatalog | null; disabled: boolean; onChange: () => void;
}) {
  const [policy, setPolicy] = useState<EscalationStatus | null>(null);
  const [monitor, setMonitor] = useState('');
  const [repair, setRepair] = useState('');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  useEffect(() => {
    let active = true;
    void api<EscalationStatus>('/model-escalation').then(value => {
      if (!active) return;
      setPolicy(value);
      setMonitor(escalationValue(value.monitor_route));
      setRepair(escalationValue(value.repair_route));
    }).catch(() => { if (active) setNotice('无法读取模型升级策略，请重试。'); });
    return () => { active = false; };
  }, [api]);
  if (!catalog) return <p className="diagnosis-meta">请先保存供应商，再配置模型升级策略。</p>;
  const options = <>{catalog.providers.map(provider => <optgroup key={provider.id} label={provider.name}>
    {provider.models.map(model => <option key={model.model} disabled={!provider.has_key}
      value={JSON.stringify([provider.id, model.model])}>
      {provider.name} / {model.model}{provider.has_key ? '' : '（未保存密钥）'}
    </option>)}</optgroup>)}</>;
  const save = () => {
    if (!policy) return;
    setBusy(true); setNotice('');
    void api<EscalationStatus>('/model-escalation', {
      version: policy.version, enabled: policy.enabled,
      monitor_route: monitor ? JSON.parse(monitor) : null,
      repair_route: repair ? JSON.parse(repair) : null,
    }).then(value => {
      setPolicy(value); setNotice('模型升级策略已保存；运行中的阶段继续使用原快照。'); onChange();
    }).catch(e => setNotice(errors[(e as Error).message] ?? '模型升级策略保存失败，请刷新后重试。'))
      .finally(() => setBusy(false));
  };
  return <section className="analysis-model-section model-escalation" aria-label="模型升级编排">
    <div className="section-heading"><h3><ShieldCheck size={17} />flash 监控 + v4pro 修复</h3>
      <span className={`tag ${policy?.ready && policy.enabled ? 'green' : 'neutral'}`}>
        {policy?.enabled ? (policy.ready ? '已启用' : '配置缺失') : '默认关闭'}
      </span></div>
    <p className="diagnosis-meta">策略启用后，协调、分析、调查、复现和独立审查使用监控模型；只有服务端确认需要修改代码时才使用修复模型，并在修复结束后回到监控模型。该策略不授予合并或部署权限。</p>
    {policy && <>
      <label className="checkbox-label"><input type="checkbox" checked={policy.enabled} disabled={busy || disabled}
        onChange={e => setPolicy({ ...policy, enabled: e.target.checked })} />启用模型升级策略</label>
      <div className="execution-config-row">
        <label>监控模型<select disabled={busy || disabled} value={monitor} onChange={e => setMonitor(e.target.value)}>
          <option value="">请选择 flash 监控模型</option>{options}</select></label>
        <label>修复模型<select disabled={busy || disabled} value={repair} onChange={e => setRepair(e.target.value)}>
          <option value="">请选择 v4pro 修复模型</option>{options}</select></label>
      </div>
      <div className="agent-model-readiness">{policy.monitor && <span className="tag neutral">监控：{policy.monitor.provider_name} / {policy.monitor.model}</span>}
        {policy.repair && <span className="tag neutral">修复：{policy.repair.provider_name} / {policy.repair.model}</span>}
        {policy.missing.map(item => <span className="tag neutral" key={item}>{errors[item] ?? item}</span>)}</div>
      <button className="button secondary" disabled={busy || disabled} onClick={save}><Save size={15} />保存升级策略</button>
    </>}
    {notice && <p role="status" className="diagnosis-meta">{notice}</p>}
  </section>;
}

function AgentModelForm({ value, catalog, disabled, onSave, onSelect }: {
  value: AgentModel; catalog: ProviderCatalog; disabled: boolean;
  onSave: (role: AgentModel, key: string) => Promise<void>;
  onSelect: (role: string, choice: RoleChoice | null) => Promise<void>;
}) {
  const direct = value.direct_config ?? value;
  const [draft, setDraft] = useState({ ...value, ...direct });
  const [choice, setChoice] = useState(encodeChoice(value.selection_choice));
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
    <form className="execution-config agent-model-selection" onSubmit={e => {
      e.preventDefault(); void onSelect(value.role, decodeChoice(choice));
    }}>
      <label>{value.name}供应商与模型<select disabled={disabled} value={choice}
        onChange={e => setChoice(e.target.value)}>
        <option value="">{value.role === 'system_coordinator' ? '未选择供应商（仅使用此角色独立配置）' : '使用默认模型（未设置时使用原有配置）'}</option>
        <option value="direct">使用此角色独立配置</option>
        <ProviderOptions catalog={catalog} />
      </select></label>
      <p className="diagnosis-meta">当前生效：{value.configured
        ? `${value.provider_name} / ${value.model}` : '尚未配置可用模型'}</p>
      {value.selection_error && <p role="alert" className="error-inline">
        {errors[value.selection_error] ?? '原选择已失效，请重新选择供应商与模型。'}
      </p>}
      <button className="button secondary" disabled={disabled}><Save size={15} />保存{value.name}选择</button>
    </form>
    <details>
      <summary>此角色独立配置（兼容已有手工设置）</summary>
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
          required={!direct.has_key || draft.base_url.trim().replace(/\/+$/, '') !== direct.base_url}
          disabled={disabled} placeholder={direct.has_key ? '已保存，留空保留此角色的密钥' : '填写此角色的密钥'}
          value={key} onChange={e => setKey(e.target.value)} /></label>
        {direct.configured && <p className="diagnosis-meta">
          已保存输出协议：{direct.output_protocol === 'json_object' ? 'JSON Object' : 'JSON Schema'}
          {direct.finalization_mode === 'deepseek_direct' && ' · DeepSeek 兼容模式，推理强度不生效'}
          {direct.finalization_mode === 'glm47_direct' && ' · GLM-4.7 直接报告模式'}
        </p>}
        <p className="diagnosis-meta">保存独立配置后，此角色将使用该配置；选择已保存的供应商无需在这里重复填写密钥。</p>
        <div className="execution-config-footer"><button className="button secondary" disabled={disabled}>
          <Save size={15} />保存{value.name}模型
        </button></div>
      </form>
    </details>
  </article>;
}

export function AgentModelSettings({ api, onChange }: { api: Api; onChange: () => void }) {
  const [section, setSection] = useState('roles');
  const [config, setConfig] = useState<AgentModels | null>(null);
  const [catalog, setCatalog] = useState<ProviderCatalog | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [failed, setFailed] = useState(false);
  const load = () => {
    setBusy(true); setNotice(''); setFailed(false);
    void Promise.all([api<AgentModels>('/agent-models'), api<ProviderCatalog>('/model-providers')])
      .then(([models, providers]) => { setConfig(models); setCatalog(providers); })
      .catch(() => { setNotice('无法读取角色模型配置，请重试。'); setFailed(true); })
      .finally(() => setBusy(false));
  };
  useEffect(() => {
    let active = true;
    void Promise.all([api<AgentModels>('/agent-models'), api<ProviderCatalog>('/model-providers')])
      .then(([models, providers]) => { if (active) { setConfig(models); setCatalog(providers); } })
      .catch(() => { if (active) { setNotice('无法读取角色模型配置，请重试。'); setFailed(true); } });
    return () => { active = false; };
  }, [api]);
  const catalogChanged = (value: ProviderCatalog) => {
    setCatalog(value);
    void api<AgentModels>('/agent-models').then(setConfig)
      .catch(() => { setNotice('供应商已保存，角色状态刷新失败，请点击刷新配置。'); setFailed(true); });
    onChange();
  };
  const select = async (role: string, choice: RoleChoice | null) => {
    if (!catalog) return;
    setBusy(true); setNotice(''); setFailed(false);
    try {
      const path = role === 'default' ? '/model-providers/default' : `/agent-models/${role}/selection`;
      catalogChanged(await api<ProviderCatalog>(path, { version: catalog.version, choice }));
      setNotice('供应商与模型选择已保存，对后续任务生效；运行中的任务保留原配置。');
    } catch (e) {
      setNotice(errors[(e as Error).message] ?? '模型选择保存失败，请刷新后重试。'); setFailed(true);
    } finally { setBusy(false); }
  };
  const save = async (role: AgentModel, key: string) => {
    setBusy(true); setNotice(''); setFailed(false);
    try {
      const saved = await api<AgentModels>(`/agent-models/${role.role}`, {
        version: role.version, model: role.model, base_url: role.base_url, api: role.api,
        api_key: key, reasoning_effort: role.reasoning_effort,
      });
      setConfig(saved); setNotice(`已保存${role.name}模型，同一角色在各流程中共用此配置。`);
      setCatalog(await api<ProviderCatalog>('/model-providers'));
      onChange();
    } catch (e) {
      setNotice(errors[(e as Error).message] ?? '保存未完成，请检查配置后重试。'); setFailed(true);
    } finally { setBusy(false); }
  };
  return <><nav className="section-nav secondary-nav" aria-label="模型配置步骤">{[['providers', '1. 连接供应商'], ['roles', '2. 分配模型']].map(([key, label]) => <button key={key} aria-current={section === key ? 'page' : undefined} className={section === key ? 'active' : ''} onClick={() => setSection(key)}>{label}</button>)}</nav>
  <div hidden={section !== 'providers'}><ModelProvidersSettings api={api} catalog={catalog} onChange={catalogChanged} disabled={busy} /></div>
  <section hidden={section !== 'roles'} className="analysis-model-section" aria-labelledby="agent-model-heading">
    <div className="section-heading"><h2 id="agent-model-heading"><Workflow size={18} />智能体模型配置</h2>
      <span className="section-meta">{config?.roles.length ?? 6} 个 Agent 角色 · 分别选择模型</span>
      <button className="button secondary small" disabled={busy} onClick={load}>
        <RefreshCw size={14} />刷新配置
      </button>
    </div>
    {catalog && <DefaultModelForm key={catalog.version} catalog={catalog} disabled={busy} onSelect={select} />}
    <ModelEscalationSettings api={api} catalog={catalog} disabled={busy} onChange={onChange} />
    {config && <>
      <div className="agent-model-switch"><label className="checkbox-label">
        <input type="checkbox" checked={config.enabled} disabled={busy} onChange={e => {
          setBusy(true); setNotice(''); setFailed(false);
          void api<AgentModels>('/agent-models/settings', { version: config.version, enabled: e.target.checked })
            .then(async saved => { setConfig(saved); setNotice(saved.enabled ? '已启用模型工作流，就绪的任务将继续。' : '模型工作流已停用，角色配置保留。'); setCatalog(await api<ProviderCatalog>('/model-providers')); onChange(); })
            .catch(e => { setNotice(errors[e.message] ?? '启用状态保存失败，请重试。'); setFailed(true); })
            .finally(() => setBusy(false));
        }} />启用模型工作流
      </label><span className="diagnosis-meta">自动修复还需单独启用「自动修复」分类中的开关。</span></div>
      <div className="agent-model-readiness">{Object.entries(config.flows).map(([flow, state]) =>
        <span className={`tag ${state.ready && config.enabled ? 'green' : 'neutral'}`} key={flow}>
          {flowNames[flow] ?? flow}：{!state.ready
            ? `待配置${state.missing_roles.map(role => config.roles.find(item => item.role === role)?.name ?? role).join('、')}`
            : config.enabled ? '模型就绪' : '已配置 · 未启用'}
        </span>)}</div>
    </>}
    {notice && <p role={failed ? 'alert' : 'status'} className={failed ? 'error-inline' : 'diagnosis-meta'}>{notice}</p>}
    {config && catalog && <div className="agent-model-grid">{config.roles.map(role =>
      <AgentModelForm key={`${role.role}:${role.version}:${catalog.version}`} value={role} catalog={catalog}
        disabled={busy} onSave={save} onSelect={select} />
    )}</div>}
  </section></>;
}
