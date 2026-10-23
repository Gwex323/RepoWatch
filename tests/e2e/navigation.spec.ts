import { test, expect } from '@playwright/test';

for (const width of [1440, 390]) test(`task navigation and retained forms at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 1000 });
  const errors: string[] = [];
  page.on('pageerror', e => errors.push(e.message));
  const repo = { id: 'repo_ui', name: 'example/project', kind: 'github', language: 'Python', url: 'https://github.com/example/project' };
  const env = { id: 'env_ui', state: 'ready', version: 1, revision: 'a'.repeat(40), created: 1,
    installed: true, busy: false, jobs: [], config: { language: 'python', port: 8000, install: '', test: 'pytest', start: '', detected: true, run_mode: 'test_only' } };
  const models = { enabled: true, version: 1, roles: [['repository_analyst','仓库分析'],['problem_investigator','问题调查'],['reproduction_assistant','复现辅助'],['repair_agent','代码修复'],['independent_reviewer','独立审查']].map(([role,name]) => ({role,name,description:'根据当前仓库证据执行任务',flows:[],model:'',api:'chat_completions',base_url:'https://example.com/v1',reasoning_effort:'auto',version:1,configured:false})), flows: {} };
  const analysis = { id: 'analysis_ui', status: 'failed', version: 1, revision: env.revision, created: 1,
    current: true, error: 'analysis_provider_timeout_exhausted', report: null, findings: [], events: [],
    report_recovery: { available: true } };
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    const values: Record<string, unknown> = {
      '/api/session': { authenticated: true, setup_required: false, csrf: 'test' },
      '/api/dashboard': { repositories: [repo], findings: [], events: [], counts: { waiting: 0, active: 0, published_prs: 0 } },
      '/api/system': { roles: [], agent_models: models, repository_analysis: { enabled: true }, automatic_repair: { enabled: false }, schedule: {} },
      '/api/analysis-notifications': [], '/api/schedules': [], '/api/agent-models': models,
      '/api/model-providers': { version: 1, providers: [], routes: {}, roles: {}, migration_notes: [], default: null },
      '/api/github/accounts': { accounts: [], version: 1 }, '/api/repair-settings': { enabled: false, version: 1 },
      '/api/repositories/repo_ui/environments': [env], '/api/environments/env_ui': env,
      '/api/execution/readiness': { ready: true }, '/api/environments/env_ui/analyses': [analysis],
      '/api/environments/env_ui/repairs': [],
      '/api/repositories/repo_ui/issues': [{ id: 'issue_ui', number: 2, title: '需要分析的问题', state: 'open' }],
      '/api/repositories/repo_ui/issues/issue_ui/analysis': { id: analysis.id, status: 'queued' },
      '/api/repositories/repo_ui/schedule': { enabled: false, local_time: '09:00', last_status: 'idle', version: 1 },
      '/api/repositories/repo_ui/memories': { items: [], total: 0 },
      '/api/repositories/repo_ui/memory-metrics': { with_memory: {}, without_memory: {} },
      '/api/repositories/repo_ui/memory-aging': { items: [] },
    };
    await route.fulfill({ json: values[path] ?? [] });
  });
  const nav = async (name: string) => {
    if (width < 800) await page.getByTitle('打开导航').click();
    await page.locator('.sidebar').getByRole('button', { name, exact: true }).click();
  };
  const fits = async () => expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.goto('/');
  await expect(page.getByLabel('下一步')).toBeVisible();
  await fits();
  await page.screenshot({ path: `test-results/home-${width}.png`, fullPage: true });
  await nav('设置');
  await page.getByRole('button', { name: '1. 连接供应商' }).click();
  await page.getByText('导入供应商 JSON', { exact: true }).click();
  const importBox = page.locator('.provider-import textarea');
  await importBox.fill('{"draft":"保留输入"}');
  await page.getByRole('navigation', { name: '设置分类' }).getByRole('button', { name: 'GitHub 账号' }).click();
  await expect(page.locator('.github-settings')).toBeVisible();
  await expect(page.locator('.model-providers')).toBeHidden();
  await page.getByRole('button', { name: '模型与供应商', exact: true }).click();
  await expect(importBox).toHaveValue('{"draft":"保留输入"}');
  await page.getByRole('button', { name: '2. 分配模型' }).click();
  await expect(page.getByLabel('启用模型工作流')).toBeVisible();
  await expect(page.locator('.agent-model-card')).toHaveCount(5);
  await fits();
  await page.screenshot({ path: `test-results/settings-${width}.png`, fullPage: true });
  await nav('仓库');
  await page.getByTitle('管理运行环境').click();
  await expect(page.getByLabel('GitHub Issues')).toBeVisible();
  await expect(page.getByLabel('仓库自动分析')).toBeHidden();
  await page.getByLabel('GitHub Issues').getByRole('button', { name: '分析', exact: true }).click();
  await expect(page.getByRole('button', { name: '从已保存证据恢复报告' })).toBeVisible();
  await expect(page.getByLabel('GitHub Issues')).toBeHidden();
  await page.getByRole('navigation', { name: '仓库任务分区' }).getByRole('button', { name: '运行环境' }).click();
  await page.getByRole('tab', { name: '运行配置' }).click();
  await page.getByLabel('测试 / CLI 命令').fill('pytest -q');
  await page.getByRole('button', { name: '定时巡检', exact: true }).click();
  await expect(page.getByLabel('启用每日巡检')).toBeVisible();
  await page.getByRole('navigation', { name: '仓库任务分区' }).getByRole('button', { name: '运行环境' }).click();
  await expect(page.getByLabel('测试 / CLI 命令')).toHaveValue('pytest -q');
  await fits();
  await page.screenshot({ path: `test-results/repository-${width}.png`, fullPage: true });
  await page.getByRole('button', { name: '返回仓库列表', exact: false }).click();
  await expect(page.getByTitle('管理运行环境')).toBeVisible();
  for (const name of ['待办与问题', '项目知识', '运行记录']) { await nav(name); await expect(page.getByRole('heading', { name, exact: false })).toBeVisible(); await fits(); }
  expect(errors).toEqual([]);
});
