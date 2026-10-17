import { test, expect } from '@playwright/test';

test('automatic pipeline: download consent, missing start, preserved tests, access, analysis and cancel', async ({ page }) => {
  test.setTimeout(90_000);
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  const env = {
    id: 'env_pipeline', revision: 'c'.repeat(40), version: 1, state: 'downloaded',
    installed: false, busy: false, url: null as string | null, error: null,
    created: Date.now() / 1000, expires: null as number | null,
    test_result: null as { exit_code: number; revision: string; command: string } | null,
    config: { language: 'python', install: 'python -m venv /workspace/home/venv',
      test: 'python -m pytest', start: '', port: 8000, install_network: false,
      detected: true, run_mode: 'service' },
    jobs: [] as { id: string; action: string; state: string; log: string; error: null; cancel: number }[],
    pipeline: null as { id: string; state: string; stage: string; error: string | null; analysis_id: string | null } | null,
    access_result: null as { status_code: number; path: string } | null,
  };
  let downloaded = false;
  let downloads = 0;
  let stops = 0;
  let configurations = 0;
  await page.route('**/api/execution/readiness', route => route.fulfill({ json: { ready: true, reason: '' } }));
  await page.route('**/api/environments/env_pipeline', route => route.fulfill({ json: env }));
  await page.route('**/api/**/environments', async route => {
    if (route.request().method() === 'POST') {
      expect(route.request().postDataJSON()).toEqual({ auto_run: true });
      downloaded = true; downloads++;
      env.version++; env.state = 'downloading'; env.busy = true;
      env.pipeline = { id: `pipeline_${downloads}`, state: 'running', stage: 'download', error: null, analysis_id: null };
      await route.fulfill({ status: 202, json: env });
    } else await route.fulfill({ json: downloaded ? [env] : [] });
  });
  await page.route('**/api/environments/env_pipeline/**', async route => {
    const action = new URL(route.request().url()).pathname.split('/').pop()!;
    if (action === 'analyses' || action === 'repairs') {
      await route.fulfill({ json: [] }); return;
    }
    const body = route.request().postDataJSON();
    expect(body.version).toBe(env.version);
    env.version++;
    if (action === 'configuration') {
      configurations++;
      if (configurations === 2) expect(body.continue_pipeline).toBe(true);
      const previousResult = env.test_result;
      env.config = { ...body, detected: true };
      expect(env.installed).toBe(true);
      expect(previousResult?.exit_code).toBe(0);
      env.pipeline!.state = 'running'; env.pipeline!.stage = 'start';
      env.pipeline!.error = null; env.busy = true;
    } else if (action === 'stop') {
      stops++; env.pipeline!.state = 'cancelled'; env.pipeline!.error = 'execution_cancelled';
      env.busy = false; env.state = 'stopped'; env.url = null;
    } else throw new Error(`Unexpected manual step ${action}`);
    await route.fulfill({ json: env });
  });
  await page.goto('/');
  await page.getByLabel('管理密码', { exact: true }).fill('qa-only-owner-password');
  if (await page.getByRole('heading', { name: '创建本地工作空间' }).isVisible()) {
    await page.getByLabel('确认密码', { exact: true }).fill('qa-only-owner-password');
    await page.getByRole('button', { name: '创建并进入' }).click();
  } else await page.getByRole('button', { name: '进入工作空间' }).click();
  await page.getByRole('button', { name: '添加仓库', exact: true }).click();
  await page.getByLabel('GitHub 仓库链接').fill('https://github.com/fixture/pipeline');
  await page.getByRole('dialog').getByRole('button', { name: '添加仓库', exact: true }).click();
  await page.locator('.repository-row').filter({ hasText: 'fixture/pipeline' })
    .getByRole('button', { name: '管理运行环境' }).click();
  const panel = page.locator('.execution-panel');
  await expect(panel.getByRole('checkbox', { name: '下载后自动准备环境、测试、启动并分析' })).toBeChecked();
  await panel.getByRole('button', { name: '下载最新版本', exact: true }).click();
  const flow = panel.getByLabel('自动执行流水线');
  await expect(flow).toContainText('当前步骤：下载源码');
  await expect(panel.getByRole('button', { name: '下载最新版本', exact: true })).toBeDisabled();
  env.pipeline!.stage = 'setup'; env.state = 'installing';
  await expect(flow).toContainText('当前步骤：准备环境／复用依赖');
  env.pipeline!.stage = 'test'; env.installed = true; env.state = 'testing';
  await expect(flow).toContainText('当前步骤：自动测试');
  env.test_result = { exit_code: 0, revision: env.revision, command: env.config.test };
  env.state = 'ready'; env.busy = false; env.pipeline!.stage = 'start';
  env.pipeline!.state = 'waiting_configuration'; env.pipeline!.error = 'pipeline_start_command_required';
  await expect(flow).toContainText('等待补齐配置');
  await expect(panel.getByText('最近测试：通过')).toBeVisible();
  await flow.getByRole('button', { name: '补齐运行配置' }).click();
  await expect(panel.getByLabel('启动命令')).toBeEnabled();
  await expect(panel.getByText('仅修改启动命令或端口会保留安装和测试结果；修改测试命令只清除测试结果。')).toBeVisible();
  await panel.getByLabel('启动命令').fill('DEMO_MODE=true python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000');
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.locator('.sidebar').evaluate(el => el.getBoundingClientRect().right))
    .toBeLessThanOrEqual(0);
  await page.screenshot({ path: 'test-results/pipeline-missing-start-mobile.png', fullPage: true, animations: 'disabled' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await panel.getByRole('button', { name: '保存配置', exact: true }).click();
  await expect(flow).toContainText('当前步骤：启动项目');
  expect(configurations).toBe(1);
  await expect(panel.getByText('最近测试：通过')).toBeVisible();
  env.pipeline!.stage = 'access';
  await expect(flow).toContainText('当前步骤：检查访问地址');
  env.state = 'running'; env.busy = false; env.url = 'http://127.0.0.2:49152';
  env.expires = Date.now() / 1000 + 1800;
  env.access_result = { status_code: 200, path: '/' };
  env.pipeline!.stage = 'analysis'; env.pipeline!.state = 'waiting_model';
  await expect(flow).toContainText('等待模型配置');
  await expect(panel.getByText('访问地址已检查：HTTP 200')).toBeVisible();
  await expect(panel.getByRole('link', { name: '打开项目' })).toHaveAttribute('href', env.url);
  await expect(flow.getByText('请启用仓库分析模型，流水线将自动继续。')).toBeVisible();
  await page.screenshot({ path: 'test-results/pipeline-analysis-mobile.png', fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await panel.getByRole('button', { name: '停止项目或任务' }).click();
  await expect(flow).toContainText('流水线已取消');
  await expect(panel.getByRole('link', { name: '打开项目' })).toHaveCount(0);
  expect(stops).toBe(1);
  await panel.getByRole('tab', { name: '运行配置' }).click();
  await expect(panel.getByRole('button', { name: '保存并继续流水线', exact: true })).toBeVisible();
  await panel.getByRole('button', { name: '保存并继续流水线', exact: true }).click();
  await expect(flow).toContainText('当前步骤：启动项目');
  await expect(panel.getByText('最近测试：通过')).toBeVisible();
  expect(configurations).toBe(2);
  await panel.getByRole('button', { name: '停止项目或任务' }).click();
  await expect(flow).toContainText('流水线已取消');
  expect(stops).toBe(2);
  env.pipeline!.stage = 'analysis'; env.pipeline!.state = 'failed';
  env.pipeline!.error = 'analysis_source_read_not_followed';
  await expect(flow).toContainText('模型返回的源码读取请求未通过校验');
  await expect(flow).toContainText('无需重新安装');
  await expect(flow).not.toContainText('操作未完成，请刷新状态后重试');
  await expect(panel.getByText('最近测试：通过')).toBeVisible();
  expect(configurations).toBe(2);

  expect(errors).toEqual([]);
});
