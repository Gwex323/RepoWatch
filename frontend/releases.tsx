import { useEffect, useState } from 'react';

type Api = <T>(path: string, body?: unknown) => Promise<T>;
type ReleaseState = {
  managed: boolean; draining: boolean;
  current: { id: string; commit: string; port: number } | null;
  supervisor: { state?: string; reason?: string } | null;
  events: { id: string; release_id: string; kind: string; reason: string | null; created: number }[];
  resumptions: { release_id: string; state: string; reason: string | null }[];
};
const states: Record<string, string> = {
  healthy: '健康', blocked: '受阻', waiting: '等待排空', deployed: '部署核验通过',
  merged: 'GitHub 已合并', restart: '监护重启', rollback: '代码回滚', failed: '发布失败',
  dispatching: '核对原任务', verifying_original: '验证原故障步骤',
};

export function ReleasesPanel({ api }: { api: Api }) {
  const [status, setStatus] = useState<ReleaseState | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    const load = () => { void api<ReleaseState>('/system/releases').then(value => {
      if (active) { setStatus(value); setError(''); }
    }).catch(e => { if (active) setError(e.message); }); };
    load();
    const timer = setInterval(load, 5000);
    return () => { active = false; clearInterval(timer); };
  }, [api]);
  return <section className="coordination-panel" aria-label="发布与服务恢复">
    <div className="section-heading"><h2>发布与服务恢复</h2>
      <span className="tag neutral">{status?.draining ? '发布排空中' : status?.managed ? '受管运行' : '非受管运行'}</span>
    </div>
    <dl className="fault-metadata">
      <div><dt>合并授权</dt><dd>默认关闭，逐个提交授权</dd></div>
      <div><dt>部署授权</dt><dd>默认关闭，逐份清单授权</dd></div>
      <div><dt>运行提交</dt><dd><code>{status?.current?.commit ?? '未绑定清单'}</code></dd></div>
      <div><dt>独立监护</dt><dd>{status?.supervisor?.state ? states[status.supervisor.state] ?? status.supervisor.state : '未确认'}</dd></div>
    </dl>
    {status?.supervisor?.reason && <p className="error-inline">{status.supervisor.reason}</p>}
    {error && <p className="error-inline" role="alert">{error}</p>}
    {status?.events.length === 0 && <p className="diagnosis-meta">暂无受管发布记录</p>}
    {status?.events.map(event => <div className="coordination-job" key={event.id}>
      <div className="section-heading"><strong>{states[event.kind] ?? event.kind}</strong>
        <span className="diagnosis-meta">{new Date(event.created * 1000).toLocaleString('zh-CN')}</span></div>
      <p className="diagnosis-meta"><code>{event.release_id.slice(0, 12)}</code>{event.reason ? ' · ' + event.reason : ''}</p>
    </div>)}
    {status?.resumptions.map(item => <p className="diagnosis-meta" key={item.release_id}>
      原任务：{states[item.state] ?? item.state}{item.reason ? ' · ' + item.reason : ''}
    </p>)}
  </section>;
}
