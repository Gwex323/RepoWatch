import { test, expect } from '@playwright/test';

test('provider editor discovers models without a name and tests one selected model on explicit clicks', async ({ page }) => {
  test.setTimeout(90_000);
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  let discoverCalls = 0;
  let testCalls = 0;
  let releaseDiscovery: (() => void) | undefined;
  await page.route('**/api/model-providers/discover', async route => {
    const body = route.request().postDataJSON();
    discoverCalls++;
    if (discoverCalls === 1) {
      expect(body.api_key).toBe('browser-probe-key');
      expect(body.model).toBe('');
      await new Promise<void>(resolve => { releaseDiscovery = resolve; });
      await route.fulfill({ json: { models: ['discovered-model', 'manual-model', 'other-model'], truncated: false } });
    } else {
      expect(body.provider_id).toBeTruthy();
      expect(body.api_key).toBe('');
      await route.fulfill({ status: 409, json: { detail: 'model_probe_models_unsupported' } });
    }
  });
  await page.route('**/api/model-providers/test-connection', async route => {
    const body = route.request().postDataJSON();
    expect(body.model).toBe('discovered-model');
    expect(body.api_key).toBe('browser-probe-key');
    testCalls++;
    await route.fulfill(testCalls === 1
      ? { status: 409, json: { detail: 'model_probe_authentication' } }
      : { json: { connected: true, latency_ms: 23 } });
  });
  await page.goto('/');
  await page.getByLabel('管理密码', { exact: true }).fill('qa-only-owner-password');
  if (await page.getByRole('heading', { name: '创建本地工作空间' }).isVisible()) {
    await page.getByLabel('确认密码', { exact: true }).fill('qa-only-owner-password');
    await page.getByRole('button', { name: '创建并进入' }).click();
  } else await page.getByRole('button', { name: '进入工作空间' }).click();
  await page.getByRole('button', { name: '运行配置', exact: true }).click();
  const panel = page.getByRole('region', { name: '供应商与智能体模型' });
  await panel.getByRole('button', { name: '新增供应商' }).click();
  const editor = panel.getByRole('form', { name: '供应商编辑' });
  await editor.getByLabel('供应商 baseURL').fill('https://first.example/v1');
  await editor.getByLabel('供应商 API Key').fill('browser-probe-key');
  await editor.getByLabel('模型名称 1', { exact: true }).fill('manual-model');
  await editor.getByLabel('推理强度 1').selectOption('high');
  await expect(editor.getByLabel('供应商名称', { exact: true })).toHaveValue('');
  await editor.getByRole('button', { name: '拉取供应商模型' }).click();
  await expect.poll(() => discoverCalls).toBe(1);
  await expect(editor.getByLabel('供应商 baseURL')).toBeDisabled();
  releaseDiscovery!();
  await expect(editor.getByRole('status')).toContainText('已拉取 3 个模型');
  await expect(editor.getByLabel('选择模型 manual-model', { exact: true })).toBeChecked();
  await expect(editor.getByLabel('选择模型 manual-model', { exact: true })).toBeDisabled();
  await editor.getByLabel('筛选拉取的模型').fill('discovered');
  await editor.getByLabel('选择模型 discovered-model', { exact: true }).check();
  await editor.getByRole('button', { name: '添加所选模型（1）', exact: true }).click();
  await expect(editor.getByLabel('模型名称 1', { exact: true })).toHaveValue('manual-model');
  await expect(editor.getByLabel('推理强度 1')).toHaveValue('high');
  await expect(editor.getByLabel('模型名称 2', { exact: true })).toHaveValue('discovered-model');
  await editor.getByLabel('连接测试模型', { exact: true }).fill('discovered-model');
  await editor.getByRole('button', { name: '连接测试', exact: true }).click();
  await expect(editor.getByRole('status')).toContainText('供应商拒绝了 API Key');
  expect(testCalls).toBe(1);
  await editor.getByRole('button', { name: '连接测试', exact: true }).click();
  await expect(editor.getByRole('status')).toContainText('连接成功');
  expect(testCalls).toBe(2);
  await editor.getByLabel('供应商 baseURL').fill('https://other.example/v1');
  await expect(editor.getByRole('status')).toHaveCount(0);
  await expect(editor.getByLabel('筛选拉取的模型')).toHaveCount(0);
  await editor.getByLabel('供应商 baseURL').fill('https://first.example/v1');
  await editor.getByLabel('供应商名称', { exact: true }).fill('连接检测供应商');
  await editor.getByRole('button', { name: '保存供应商', exact: true }).click();
  await expect(editor).toHaveCount(0);
  await panel.getByRole('button', { name: '编辑 连接检测供应商', exact: true }).click();
  await expect(editor.getByLabel('供应商 API Key')).toHaveValue('');
  await editor.getByRole('button', { name: '拉取供应商模型' }).click();
  await expect(editor.getByRole('status')).toContainText('可手动填写模型并进行连接测试');
  await expect(editor.getByLabel('模型名称 1', { exact: true })).toHaveValue('manual-model');
  await expect(editor.getByLabel('推理强度 1')).toHaveValue('high');
  expect(discoverCalls).toBe(2);
  await editor.screenshot({ path: 'test-results/provider-probe-desktop.png' });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.locator('.sidebar').evaluate(el => el.getBoundingClientRect().right)).toBeLessThanOrEqual(1);
  await editor.getByRole('button', { name: '连接测试', exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: 'test-results/provider-probe-mobile.png' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  expect(errors).toEqual([]);
});
