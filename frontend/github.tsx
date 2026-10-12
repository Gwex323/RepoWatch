import { useCallback, useEffect, useState } from 'react';
import { Check, ExternalLink, Github, KeyRound, Loader2, Plus, RefreshCw, Search, ShieldCheck, Unplug, X } from 'lucide-react';

type Api = <T>(path: string, body?: unknown) => Promise<T>;
type Repo = { id: string; name: string; kind: string };
type Account = {
  id: number; login: string; state: string; scopes: string[] | null;
  checked_at: number; version: number; active: boolean;
};
type Connections = {
  accounts: Account[]; active_account_id: number | null; secure_storage: string;
};
type Access = { account_id: number; repository: string; visible: boolean; private: boolean; write_access: string };
const errors: Record<string, string> = {
  github_invalid_input: '请检查用户名和访问令牌格式。',
  github_account_mismatch: '令牌对应的 GitHub 账号与所选用户名不一致，连接未更改。',
  github_invalid_token: '访问令牌无效、已过期或已撤销，请更新令牌。',
  github_unreachable: '暂时无法连接 GitHub，请检查网络后重试。',
  github_unavailable: 'GitHub 暂时不可用，请稍后重试。',
  github_denied_or_rate_limited: 'GitHub 拒绝了请求或触发访问频率限制，请检查权限或稍后重试。',
  github_repository_unavailable: '仓库不存在，或当前令牌无法访问此仓库。',
  github_repository_required: '请先选择已登记的 GitHub 仓库。',
  github_invalid_identity: 'GitHub 未返回有效的个人账号身份。',
  github_invalid_response: 'GitHub 返回的数据无法验证，请稍后重试。',
  github_credential_missing: '本机无法解密该账号凭据，请重新连接。',
  secure_storage_unavailable: '系统凭据库不可用，暂时不能保存或使用令牌。',
  'GitHub account changed. Refresh and try again.': '账号配置已变化，已刷新列表，请重试。',
  'GitHub account not found.': '该账号已被解绑，已刷新列表。',
  'Owner login is required.': '本地登录已过期，请重新登录。',
};
const stateName: Record<string, string> = {
  verified: '身份已验证', invalid: '授权已失效', credential_missing: '需要重新连接',
};
const checkedTime = (value: number) => new Date(value * 1000).toLocaleString('zh-CN', {
  timeZone: 'Asia/Shanghai', hour12: false,
});

export function GitHubAccounts({ api, repositories }: { api: Api; repositories: Repo[] }) {
  const [data, setData] = useState<Connections | null>(null);
  const [editing, setEditing] = useState(false);
  const [username, setUsername] = useState('');
  const [token, setToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [removing, setRemoving] = useState<number | null>(null);
  const [repoId, setRepoId] = useState('');
  const [access, setAccess] = useState<Access | null>(null);
  const refresh = useCallback(async () => setData(await api<Connections>('/github/accounts')), [api]);
  useEffect(() => {
    void refresh().catch(e => setError(errors[e.message] ?? '无法读取账号配置，请重试。'));
  }, [refresh]);
  const perform = async (work: () => Promise<void>) => {
    setBusy(true); setError(''); setMessage('');
    try { await work(); }
    catch (e) {
      setError(errors[(e as Error).message] ?? '操作未完成，请刷新后重试。');
      await refresh().catch(() => {});
    } finally { setBusy(false); }
  };
  const active = data?.accounts.find(account => account.active);
  const githubRepos = repositories.filter(repo => repo.kind === 'github');
  return <section className="github-settings" aria-labelledby="github-title" aria-busy={busy}>
    <div className="section-heading">
      <h2 id="github-title"><Github size={18} />GitHub 账号</h2>
      <div className="github-actions">
        <button className="icon-button" title="刷新 GitHub 账号" disabled={busy}
          onClick={() => void perform(refresh)}><RefreshCw size={16} /></button>
        <button className="button secondary" disabled={busy || data?.secure_storage === 'unavailable'}
          onClick={() => { setEditing(true); setUsername(''); setToken(''); setRemoving(null); setError(''); }}>
          <Plus size={16} />连接账号</button>
      </div>
    </div>
    {error && <div className="error-inline" role="alert">{error}</div>}
    {message && <p className="github-message" role="status"><Check size={15} />{message}</p>}
    {data?.secure_storage === 'unavailable' &&
      <p className="error-inline">系统凭据库不可用，账号连接已暂停。</p>}
    {!data ? <div className="loading"><Loader2 size={18} className="spin" />正在读取账号</div> : <>
      <div className="github-summary">
        <span><ShieldCheck size={14} />{data.secure_storage === 'available' ? '系统凭据库可用' : '凭据库不可用'}</span>
        <span>当前账号：{active?.login ?? '未选择'}</span><span>Issue / PR 发布：未启用</span>
        {busy && <span role="status"><Loader2 size={14} className="spin" />正在处理</span>}
      </div>
      {!data.accounts.length && !editing && <div className="empty"><Github size={28} /><p>尚未连接 GitHub 账号</p></div>}
      {data.accounts.map(account => <article className="github-account" key={account.id}>
        <div className="github-account-row">
          <label className="github-account-choice">
            <input type="radio" name="github-active" checked={account.active}
              aria-label={`使用账号 ${account.login}`} disabled={busy || account.state !== 'verified' || data.secure_storage !== 'available'}
              onChange={() => void perform(async () => {
                setAccess(null);
                setData(await api<Connections>(`/github/accounts/${account.id}/activate`, { version: account.version }));
                setMessage(`当前账号已切换为 ${account.login}`);
              })} />
            <Github size={20} /><strong>{account.login}</strong>
          </label>
          <span className={`tag ${account.state === 'verified' ? 'green' : 'neutral'}`}>{stateName[account.state] ?? account.state}</span>
          {account.active && <span className="tag neutral">当前账号</span>}
          <div className="github-actions">
            <a className="icon-button" title={`打开 ${account.login} 的 GitHub`}
              href={`https://github.com/${encodeURIComponent(account.login)}`} target="_blank" rel="noreferrer"><ExternalLink size={16} /></a>
            <button className="icon-button" title={`校验 ${account.login}`} disabled={busy}
              onClick={() => void perform(async () => {
                setAccess(null);
                setData(await api<Connections>(`/github/accounts/${account.id}/verify`, { version: account.version }));
                setMessage(`${account.login} 身份校验通过`);
              })}><RefreshCw size={16} /></button>
            <button className="icon-button" title={`更新 ${account.login} 的令牌`} disabled={busy || data.secure_storage !== 'available'}
              onClick={() => { setUsername(account.login); setToken(''); setEditing(true); setError(''); }}>
              <KeyRound size={16} /></button>
            <button className="icon-button" title={`解绑 ${account.login}`} disabled={busy}
              onClick={() => setRemoving(account.id)}><Unplug size={16} /></button>
          </div>
        </div>
        <div className="github-account-meta">
          <span>最近验证：{checkedTime(account.checked_at)}</span>
          <span>令牌权限范围：{account.scopes === null ? 'GitHub 未返回' : account.scopes.join(', ') || '无附加范围'}</span>
        </div>
        {removing === account.id && <div className="github-disconnect">
          <span>解绑 {account.login}？</span>
          <button className="button secondary" disabled={busy} onClick={() => setRemoving(null)}><X size={14} />取消</button>
          <button className="button secondary" disabled={busy} onClick={() => void perform(async () => {
            setData(await api<Connections>(`/github/accounts/${account.id}/disconnect`, { version: account.version }));
            setAccess(null); setRemoving(null); setMessage(`${account.login} 已解绑`);
          })}><Unplug size={14} />确认解绑</button>
        </div>}
      </article>)}
      {editing && <form className="github-form" aria-label="连接 GitHub 账号" onSubmit={e => {
        e.preventDefault();
        const submittedToken = token;
        setToken('');
        void perform(async () => {
          setData(await api<Connections>('/github/accounts', { username: username.trim(), token: submittedToken }));
          setEditing(false); setAccess(null); setMessage('账号已连接并设为当前账号');
        });
      }}>
        <div className="github-form-fields">
          <label>GitHub 用户名<input autoFocus value={username} required maxLength={39}
            autoComplete="off" spellCheck={false} pattern="[A-Za-z0-9][A-Za-z0-9-]*"
            disabled={busy} onChange={e => setUsername(e.target.value)} /></label>
          <label>访问令牌（PAT）<input type="password" value={token} required minLength={20} maxLength={512}
            autoComplete="off" spellCheck={false} disabled={busy} onChange={e => setToken(e.target.value)} /></label>
        </div>
        <div className="github-form-footer">
          <a className="text-link" href="https://github.com/settings/tokens" target="_blank" rel="noreferrer">
            <ExternalLink size={14} />管理 GitHub 访问令牌</a>
          <div className="github-actions">
            <button type="button" className="button secondary" disabled={busy}
              onClick={() => { setEditing(false); setToken(''); }}><X size={15} />取消</button>
            <button className="button primary" disabled={busy || data.secure_storage !== 'available'}>
              {busy ? <Loader2 className="spin" size={15} /> : <KeyRound size={15} />}验证并连接</button>
          </div>
        </div>
      </form>}
      {active && <div className="github-access">
        <h3>仓库访问检查</h3>
        <div className="github-access-controls">
          <label>已登记仓库<select value={repoId} disabled={busy} aria-label="检查访问的仓库"
            onChange={e => { setRepoId(e.target.value); setAccess(null); }}>
            <option value="">选择仓库</option>
            {githubRepos.map(repo => <option value={repo.id} key={repo.id}>{repo.name}</option>)}
          </select></label>
          <button className="button secondary" disabled={busy || !repoId || active.state !== 'verified'}
            onClick={() => void perform(async () => {
              setAccess(null);
              setAccess(await api<Access>(`/github/accounts/${active.id}/repository-access`, {
                version: active.version, repo_id: repoId,
              }));
            })}><Search size={15} />检查访问</button>
        </div>
        {access && <div className="github-access-result" role="status">
          <Check size={15} /><strong>{access.repository}</strong>
          <span>{access.private ? '私有仓库' : '公开仓库'} · 可读取</span><span>写入权限：未核验</span>
        </div>}
      </div>}
    </>}
  </section>;
}
