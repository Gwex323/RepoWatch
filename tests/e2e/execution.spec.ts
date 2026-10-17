import { test, expect } from '@playwright/test';

test('repository execution: download, configure, setup, test, start, stop and mobile layout', async ({ page }) => {
  test.setTimeout(90_000);
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  const env = {
    id: 'env_fixture', revision: 'a'.repeat(40), version: 1, state: 'downloaded', installed: false,
    busy: false, url: null as string | null, error: null as string | null, created: Date.now() / 1000,
    expires: null as number | null, test_result: null as { exit_code: number; revision: string } | null,
    config: { language: 'python', install: 'python -m venv /workspace/home/venv', test: 'python -m unittest',
      start: 'python app.py', port: 8000, install_network: false, detected: true },
    preparation: null as string | null,
    reused_environment_id: null as string | null,
    jobs: [] as { id: string; action: string; state: string; log: string; error: string | null; cancel: number }[],
  };
  let downloaded = false;
  let reusedDownload = false;
  const reuseAlias = { ...env, id: 'env_reuse_alias', state: 'reused',
    reused_environment_id: env.id, preparation: 'same_version' };
  await page.route('**/api/execution/readiness', route => route.fulfill({ json: { ready: true, reason: '' } }));
  await page.route('**/api/environments/env_fixture', route => route.fulfill({ json: env }));
  await page.route('**/api/environments/env_reuse_alias', route => route.fulfill({ json: reuseAlias }));
  await page.route('**/api/**/environments', async route => {
    if (route.request().method() === 'POST') {
      downloaded = true;
      if (reusedDownload) {
        await route.fulfill({ status: 202, json: reuseAlias });
        return;
      }
      env.jobs.unshift({ id: 'download', action: 'download', state: 'completed', log: '', error: null, cancel: 0 });
      await route.fulfill({ status: 202, json: env });
    } else await route.fulfill({ json: downloaded ? reusedDownload ? [reuseAlias, env] : [env] : [] });
  });
  await page.route('**/api/environments/env_fixture/**', async route => {
    const action = new URL(route.request().url()).pathname.split('/').pop()!;
    if (action === 'analyses' || action === 'repairs') {
      await route.fulfill({ json: [] }); return;
    }
    const body = route.request().postDataJSON();
    expect(body.version).toBe(env.version);
    env.version++;
    if (action === 'configuration') {
      env.config = { ...body, detected: true }; env.installed = false;
    } else {
      if (action === 'setup') { env.state = 'ready'; env.installed = true; }
      if (action === 'test') env.test_result = { exit_code: 0, revision: env.revision };
      if (action === 'start') {
        env.state = 'running'; env.url = 'http://127.0.0.2:49152'; env.expires = Date.now() / 1000 + 1800;
      }
      if (action === 'stop') { env.state = 'stopped'; env.url = null; env.expires = null; }
      env.jobs.unshift({ id: String(env.version), action, state: 'completed', log: 'fixture output',
        error: null, cancel: 0 });
    }
    await route.fulfill({ json: env });
  });
  await page.goto('/');
  await page.getByLabel('管理密码', { exact: true }).fill('qa-only-owner-password');
  if (await page.getByRole('heading', { name: '创建本地工作空间' }).isVisible()) {
    await page.getByLabel('确认密码', { exact: true }).fill('qa-only-owner-password');
    await page.getByRole('button', { name: '创建并进入' }).click();
  } else await page.getByRole('button', { name: '进入工作空间' }).click();
  await page.getByRole('button', { name: '添加仓库', exact: true }).click();
  await page.getByLabel('GitHub 仓库链接').fill('https://github.com/fixture/execution');
  await page.getByRole('dialog').getByRole('button', { name: '添加仓库', exact: true }).click();
  await page.locator('.repository-row').filter({ hasText: 'fixture/execution' })
    .getByRole('button', { name: '管理运行环境' }).click();
  const panel = page.locator('.execution-panel');
  await expect(panel.getByText('Docker 已就绪')).toBeVisible();
  await panel.getByRole('button', { name: '下载最新版本' }).click();
  await expect(panel.getByText('源码已下载', { exact: true })).toBeVisible();
  await expect(panel.getByRole('button', { name: '启动项目', exact: true })).toBeDisabled();
  await panel.getByRole('tab', { name: '运行配置' }).click();
  await panel.getByLabel('测试 / CLI 命令').fill('python -m unittest discover -s tests');
  await panel.getByRole('checkbox', { name: '安装时允许联网' }).check();
  await panel.getByRole('button', { name: '保存配置' }).click();
  await expect.poll(() => env.config.install_network).toBe(true);
  await panel.getByRole('button', { name: '安装环境', exact: true }).click();
  await expect(panel.getByText('环境已就绪', { exact: true })).toBeVisible();
  await panel.getByRole('button', { name: '执行测试', exact: true }).click();
  await expect(panel.getByText('最近测试：通过')).toBeVisible();
  await panel.getByRole('button', { name: '启动项目', exact: true }).click();
  await expect(panel.getByRole('link', { name: '打开项目' })).toHaveAttribute('href', 'http://127.0.0.2:49152');
  await expect(panel.getByLabel('启动命令')).toBeDisabled();
  await page.evaluate(() => scrollTo(0, 0));
  await page.screenshot({ path: 'test-results/desktop-execution.png', fullPage: true, animations: 'disabled' });
  await panel.getByRole('button', { name: '停止项目或任务' }).click();
  await expect(panel.getByText('已停止', { exact: true })).toBeVisible();
  await expect(panel.getByRole('link', { name: '打开项目' })).toHaveCount(0);
  env.preparation = 'dependencies_reused';
  await expect(panel.getByRole('status').filter({ hasText: '依赖未变化' })).toBeVisible();
  await page.screenshot({ path: 'test-results/execution-dependency-reuse-desktop.png', fullPage: true });
  reusedDownload = true;
  await panel.getByRole('button', { name: '下载最新版本' }).click();
  await expect(panel.getByRole('status').filter({ hasText: '源码版本未变化' })).toBeVisible();
  await expect(panel.getByLabel('环境版本')).toHaveValue('env_fixture');
  await page.reload();
  await page.getByRole('button', { name: '仓库', exact: true }).click();
  await page.locator('.repository-row').filter({ hasText: 'fixture/execution' })
    .getByRole('button', { name: '管理运行环境' }).click();
  await expect(panel.getByText('已停止', { exact: true })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.locator('.sidebar').evaluate(el => el.getBoundingClientRect().right)).toBeLessThanOrEqual(0);
  await panel.getByRole('tab', { name: '运行配置' }).click();
  await page.evaluate(() => scrollTo(0, 0));
  await page.screenshot({ path: 'test-results/mobile-execution.png', fullPage: true, animations: 'disabled' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(errors).toEqual([]);
});

test('installation diagnosis hides stale failures after recovery but keeps current test failures visible', async ({ page }) => {
  test.setTimeout(90_000);
  const browserErrors: string[] = [];
  page.on('pageerror', error => browserErrors.push(error.message));
  const env = {
    id: 'env_diagnosis', revision: 'b'.repeat(40), version: 5, state: 'setup_failed',
    installed: false, busy: false, url: null, error: 'setup_failed' as string | null,
    created: Date.now() / 1000, expires: null,
    test_result: null as { exit_code: number; revision: string } | null,
    config: { language: 'python', install: 'python -m pip install -r requirements.txt',
      test: '', start: '', port: 8000, install_network: false, detected: true },
    jobs: [{ id: 'job_failed', action: 'setup', state: 'failed', log: "Failed to resolve 'pypi.org'",
      error: 'setup_failed', cancel: 0 }],
    diagnosis: {
      job_id: 'job_failed', state: 'complete', current: true, applicable: true,
      report: {
        source: 'local_rules', code: 'install_network_disabled', confidence: 'likely',
        summary: '安装命令需要下载依赖，但本次安装关闭了联网，依赖源无法访问。',
        evidence: ["NameResolutionError: Failed to resolve 'pypi.org'", '本次执行配置：install_network=false'],
        steps: ['开启“安装时允许联网”，保存后重新安装。', '若开启后仍失败，再检查 Docker 的 DNS、代理和依赖源连通性。'],
        patch: { install_network: true } as { install_network?: boolean }, handoffs: [], model_status: 'not_needed',
      },
    },
  };
  let applied = false;
  await page.route('**/api/execution/readiness', route => route.fulfill({
    json: { ready: true, reason: '', diagnosis_model: { enabled: false, model: null } },
  }));
  await page.route('**/api/**/environments', route => route.fulfill({ json: [env] }));
  await page.route('**/api/environments/env_diagnosis', route => route.fulfill({ json: env }));
  await page.route('**/api/environments/env_diagnosis/diagnoses/job_failed/apply', async route => {
    expect(route.request().postDataJSON()).toEqual({ version: 5 });
    applied = true; env.version++; env.state = 'installing'; env.busy = true;
    env.config.install_network = true; env.error = null;
    env.diagnosis.current = false; env.diagnosis.applicable = false;
    await route.fulfill({ status: 202, json: env });
  });
  await page.goto('/');
  await page.getByLabel('管理密码', { exact: true }).fill('qa-only-owner-password');
  if (await page.getByRole('heading', { name: '创建本地工作空间' }).isVisible()) {
    await page.getByLabel('确认密码', { exact: true }).fill('qa-only-owner-password');
    await page.getByRole('button', { name: '创建并进入' }).click();
  } else await page.getByRole('button', { name: '进入工作空间' }).click();
  await page.getByRole('button', { name: '添加仓库', exact: true }).click();
  await page.getByLabel('GitHub 仓库链接').fill('https://github.com/fixture/diagnosis');
  await page.getByRole('dialog').getByRole('button', { name: '添加仓库', exact: true }).click();
  await page.locator('.repository-row').filter({ hasText: 'fixture/diagnosis' })
    .getByRole('button', { name: '管理运行环境' }).click();
  const panel = page.locator('.execution-panel');
  const diagnosis = page.getByLabel('失败原因与建议');
  await expect(diagnosis.getByText('本地规则诊断')).toBeVisible();
  await expect(diagnosis.getByText('本次执行配置：install_network=false')).toBeVisible();
  await expect(panel.getByText('模型诊断：未配置')).toBeVisible();
  expect(applied).toBe(false);
  await panel.screenshot({ path: 'test-results/desktop-diagnosis.png', animations: 'disabled' });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.locator('.sidebar').evaluate(el => el.getBoundingClientRect().right)).toBeLessThanOrEqual(0);
  await panel.screenshot({ path: 'test-results/mobile-diagnosis.png', animations: 'disabled' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await diagnosis.getByRole('button', { name: '应用建议并重新安装' }).click();
  await expect.poll(() => applied).toBe(true);
  await expect(diagnosis).toHaveCount(0);
  const history = panel.getByLabel('历史失败诊断', { exact: true });
  await expect(history).toBeVisible();
  expect(await history.evaluate(el => (el as HTMLDetailsElement).open)).toBe(false);
  await expect(history.getByText(env.diagnosis.report.summary)).not.toBeVisible();
  await expect(panel.getByText('安装中', { exact: true })).toBeVisible();
  await history.locator('summary').click();
  await expect(history.getByText('历史记录 · 仅供回看')).toBeVisible();
  await expect(history.getByRole('button', { name: '应用建议并重新安装' })).toHaveCount(0);
  await expect(history.getByText('建议尚未经过重试验证', { exact: false })).toHaveCount(0);
  await history.locator('summary').click();
  env.version++; env.state = 'ready'; env.busy = false; env.installed = true;
  env.test_result = { exit_code: 0, revision: env.revision };
  // A late old model result must not resurrect a foreground error or spinner.
  env.diagnosis.state = 'running';
  await expect(panel.getByText('依赖已准备', { exact: true })).toBeVisible();
  await expect(panel.getByText('最近测试：通过', { exact: true })).toBeVisible();
  await expect(diagnosis).toHaveCount(0);
  await expect(history.locator('.diagnosis-progress')).toHaveCount(0);
  expect(await history.evaluate(el => (el as HTMLDetailsElement).open)).toBe(false);
  for (const state of ['running', 'stopped']) {
    env.state = state; env.version++;
    await expect(panel.locator('.execution-facts .tag')).toHaveText(state === 'running' ? '项目运行中' : '已停止');
    await expect(diagnosis).toHaveCount(0);
  }
  await panel.screenshot({ path: 'test-results/mobile-diagnosis-recovered.png', animations: 'disabled' });
  // Installed dependencies do NOT mean a later real test failure can be hidden.
  env.state = 'test_failed'; env.error = 'test_failed'; env.version++;
  env.test_result = { exit_code: 1, revision: env.revision };
  env.diagnosis.job_id = 'job_test_failed'; env.diagnosis.current = true;
  env.diagnosis.state = 'complete'; env.diagnosis.applicable = false;
  env.diagnosis.report.summary = '当前测试失败，与早期依赖安装失败无关。';
  env.diagnosis.report.patch = {};
  await expect(diagnosis).toBeVisible();
  await expect(diagnosis.getByText('当前测试失败，与早期依赖安装失败无关。')).toBeVisible();
  await expect(panel.getByText('依赖已准备', { exact: true })).toBeVisible();
  await expect(history).toHaveCount(0);
  expect(browserErrors).toEqual([]);
});
