import { test, expect } from '@playwright/test';

test('desktop project: detect, confirm partial offline scope and never launch GUI or HTTP preview', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  const commands: string[] = [];
  page.on('request', request => {
    if (request.method() === 'POST' && request.url().includes('/api/environments/env_desktop/')) {
      commands.push(new URL(request.url()).pathname.split('/env_desktop/')[1]);
    }
  });
  const uncovered = ['Windows 桌面窗口与交互', 'Windows 原生接口、DPAPI 及真实客户端集成'];
  const profile = { kind: 'python_desktop', runtime_stdlib_only: true, complete: false,
    test_command: 'PYTHONPATH=/workspace/project/tests:/workspace/project /workspace/home/venv/bin/python -m unittest -v test_core',
    selected: [{ path: 'tests/test_core.py', module: 'test_core' }],
    excluded: [{ path: 'tests/test_ui.py', reasons: ['gui'] },
      { path: 'tests/test_windows.py', reasons: ['windows_native'] }], uncovered };
  const scope = { ...profile, kind: 'desktop_offline', total: 3, skipped: 1, executed: 2,
    selection: 'generated' };
  const env = { id: 'env_desktop', revision: 'd'.repeat(40), version: 1,
    state: 'config_required', installed: false, busy: false, url: null, expires: null,
    error: null, created: 1, test_result: null as unknown,
    config: { language: 'python', install: '', test: '', start: '', port: 8000,
      install_network: false, detected: false, run_mode: 'service',
      project_profile: undefined as typeof profile | undefined, desktop_approved: false },
    pipeline: { id: 'pipeline_desktop', state: 'waiting_configuration', stage: 'setup',
      error: 'execution_config_required' as string | null }, jobs: [] as unknown[] };
  let confirmed = false;
  await page.route('**/api/dashboard', route => route.fulfill({ json: {
    repositories: [{ id: 'repo_desktop', name: 'fixture/desktop', kind: 'github',
      url: 'https://github.com/fixture/desktop', language: 'Python' }], runs: [], events: [],
  } }));
  await page.route('**/api/execution/readiness', route => route.fulfill({ json: { ready: true, reason: '' } }));
  await page.route('**/api/repositories/repo_desktop/environments', route => route.fulfill({ json: [env] }));
  await page.route('**/api/environments/env_desktop', route => route.fulfill({ json: env }));
  await page.route('**/api/environments/env_desktop/repairs', route => route.fulfill({ json: [] }));
  await page.route('**/api/environments/env_desktop/analyses', route => route.fulfill({ json: confirmed ? [{
    id: 'analysis_desktop', status: 'completed', current: true, version: 1, revision: env.revision,
    error: null, created: 1, findings: [], events: [],
    report: { summary: '模拟离线测试与源码分析结果。', limitations: ['未运行桌面窗口。'],
      files_read: ['app.py', 'core.py'], execution_scope: scope },
  }] : [] }));
  await page.route('**/api/environments/env_desktop/configuration/detect', async route => {
    expect(route.request().postDataJSON()).toEqual({ version: 1 });
    Object.assign(env.config, { detected: true, run_mode: 'test_only', project_profile: profile,
      install: 'python -m venv /workspace/home/venv', test: profile.test_command });
    env.version++;
    env.pipeline.error = 'desktop_confirmation_required';
    await route.fulfill({ json: env });
  });
  await page.route('**/api/environments/env_desktop/configuration', async route => {
    const body = route.request().postDataJSON();
    expect(body.desktop_offline_confirmed).toBe(true);
    expect(body.run_mode).toBe('test_only');
    expect(body.install_network).toBe(false);
    expect(body.start).toBe('');
    expect(body).not.toHaveProperty('project_profile');
    confirmed = true; env.version++; env.installed = true; env.state = 'ready';
    env.config.desktop_approved = true; env.test_result = { exit_code: 0, revision: env.revision,
      desktop_scope: scope };
    Object.assign(env.pipeline, { state: 'completed', stage: 'analysis', error: null });
    await route.fulfill({ json: env });
  });
  await page.goto('/');
  await page.getByLabel('管理密码', { exact: true }).fill('qa-only-owner-password');
  if (await page.getByRole('heading', { name: '创建本地工作空间' }).isVisible()) {
    await page.getByLabel('确认密码', { exact: true }).fill('qa-only-owner-password');
    await page.getByRole('button', { name: '创建并进入' }).click();
  } else await page.getByRole('button', { name: '进入工作空间' }).click();
  await page.locator('.repository-row').filter({ hasText: 'fixture/desktop' })
    .getByRole('button', { name: '管理运行环境' }).click();
  const panel = page.locator('.execution-panel');
  await panel.getByRole('tab', { name: '运行配置', exact: true }).click();
  await panel.getByRole('button', { name: '识别桌面测试配置', exact: true }).click();
  const coverage = panel.getByLabel('桌面项目验证范围');
  await expect(coverage).toContainText('Windows 桌面窗口与交互');
  await expect(coverage).toContainText('离线测试通过不代表整个桌面程序验收通过');
  await expect(panel.getByLabel('项目运行类型')).toBeDisabled();
  await expect(panel.getByLabel('项目运行类型')).toHaveValue('test_only');
  const confirm = panel.getByRole('button', { name: '确认离线范围并继续', exact: true });
  await expect(confirm).toBeDisabled();
  expect(commands).toEqual(['configuration/detect']);
  await panel.getByLabel('我确认仅运行容器离线测试，Windows 桌面界面及专属功能仍未覆盖').check();
  await confirm.click();
  await expect(coverage).toContainText('实际执行 2 项，跳过 1 项');
  await expect(panel.getByLabel('自动执行流水线')).toContainText('离线测试与源码分析已完成');
  await expect(panel.getByLabel('仓库自动分析')).toContainText('不能据此认定整个 Windows 桌面程序验收通过');
  await expect(panel.getByRole('link', { name: '打开项目', exact: true })).toHaveCount(0);
  await expect(panel.getByRole('button', { name: '启动项目', exact: true })).toBeDisabled();
  expect(commands).toEqual(['configuration/detect', 'configuration']);
  await page.screenshot({ path: 'test-results/desktop-offline-desktop.png', fullPage: true, animations: 'disabled' });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.locator('.sidebar').evaluate(el => el.getBoundingClientRect().right)).toBeLessThanOrEqual(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await panel.screenshot({ path: 'test-results/desktop-offline-mobile.png', animations: 'disabled' });
  expect(errors).toEqual([]);
});
