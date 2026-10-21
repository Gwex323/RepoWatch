import { test, expect } from '@playwright/test';

test('saved providers, JSON import and independent agent selections persist without key disclosure', async ({ page }) => {
  test.setTimeout(120_000);
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/');
  await page.getByLabel('管理密码', { exact: true }).fill('qa-only-owner-password');
  if (await page.getByRole('heading', { name: '创建本地工作空间' }).isVisible()) {
    await page.getByLabel('确认密码', { exact: true }).fill('qa-only-owner-password');
    await page.getByRole('button', { name: '创建并进入' }).click();
  } else await page.getByRole('button', { name: '进入工作空间' }).click();
  await page.getByRole('button', { name: '运行配置', exact: true }).click();
  const panel = page.getByRole('region', { name: '供应商与智能体模型' });
  await panel.getByRole('button', { name: '新增供应商' }).click();
  await panel.getByLabel('供应商名称', { exact: true }).fill('模型服务甲');
  await panel.getByLabel('供应商 baseURL').fill('https://first.example/v1');
  await panel.getByLabel('供应商 API Key').fill('browser-only-first-secret');
  await panel.getByLabel('模型名称 1', { exact: true }).fill('general-model');
  await panel.getByRole('button', { name: '添加模型' }).click();
  await panel.getByLabel('模型名称 2', { exact: true }).fill('code-model');
  await panel.getByLabel('推理强度 2').selectOption('high');
  await panel.getByRole('button', { name: '保存供应商', exact: true }).click();
  await expect(panel.getByRole('status')).toContainText('供应商已保存');
  await expect(panel.getByText('密钥已保存', { exact: true })).toHaveCount(1);

  await panel.getByText('导入供应商 JSON', { exact: true }).click();
  await panel.getByLabel('供应商 JSON', { exact: true }).fill(JSON.stringify({ providers: [{
    name: '模型服务乙', base_url: 'https://second.example/v1', api: 'responses',
    api_key: 'browser-only-second-secret', models: [{ model: 'reasoning-model', reasoning_effort: 'high' }],
  }] }));
  await panel.getByRole('button', { name: '导入供应商', exact: true }).click();
  await expect(panel.getByRole('status')).toContainText('供应商已导入');
  await expect(panel.getByLabel('供应商 JSON', { exact: true })).toHaveValue('');
  const response = await page.request.get('/api/model-providers');
  const saved = await response.json();
  expect(JSON.stringify(saved)).not.toContain('browser-only-');
  const first = saved.providers.find((p: { name: string }) => p.name === '模型服务甲').id;
  const second = saved.providers.find((p: { name: string }) => p.name === '模型服务乙').id;
  const general = JSON.stringify([first, 'general-model']);
  const code = JSON.stringify([first, 'code-model']);
  const reasoning = JSON.stringify([second, 'reasoning-model']);
  await panel.getByLabel('默认供应商与模型', { exact: true }).selectOption(general);
  await panel.getByLabel('仓库分析 · 仓库分析员', { exact: true }).selectOption(reasoning);
  await panel.getByLabel('自动修复 · 代码修复员', { exact: true }).selectOption(code);
  await panel.getByLabel('自动修复 · 独立审查员', { exact: true }).selectOption(reasoning);
  await panel.getByLabel('运行诊断 · 原因分析员', { exact: true }).selectOption(reasoning);
  await panel.getByLabel('启用智能体模型调用', { exact: true }).check();
  await panel.getByRole('button', { name: '保存智能体选择' }).click();
  await expect(panel.getByRole('status')).toContainText('下次任务生效');
  await panel.getByText('导入供应商 JSON', { exact: true }).click();
  await panel.screenshot({ path: 'test-results/model-providers-desktop.png' });

  await page.reload();
  await page.getByRole('button', { name: '运行配置', exact: true }).click();
  await expect(panel.getByLabel('默认供应商与模型', { exact: true })).toHaveValue(general);
  await expect(panel.getByLabel('仓库分析 · 仓库分析员', { exact: true })).toHaveValue(reasoning);
  await expect(panel.getByLabel('自动修复 · 代码修复员', { exact: true })).toHaveValue(code);
  await expect(panel.getByLabel('仓库分析 · 问题调查员', { exact: true })).toHaveValue('');
  await expect(panel.getByLabel('运行诊断 · 原因分析员', { exact: true })).toHaveValue(reasoning);
  await panel.getByRole('button', { name: '删除 模型服务甲', exact: true }).click();
  await expect(panel.getByRole('status')).toContainText('仍在使用');
  await panel.getByRole('button', { name: '编辑 模型服务甲', exact: true }).click();
  await expect(panel.getByLabel('供应商 API Key')).toHaveValue('');
  await panel.getByLabel('供应商名称', { exact: true }).fill('我的编程供应商');
  await panel.getByLabel('供应商 baseURL').fill('https://updated.example/v1');
  await panel.getByRole('button', { name: '保存供应商', exact: true }).click();
  await expect(panel.getByRole('status')).toContainText('对应的新 API Key');
  await panel.getByLabel('供应商 API Key').fill('browser-only-rotated-secret');
  await panel.getByRole('button', { name: '保存供应商', exact: true }).click();
  await expect(panel.getByRole('status')).toContainText('供应商已保存');
  await expect(panel.getByLabel('默认供应商与模型', { exact: true })).toHaveValue(general);
  expect(await page.content()).not.toContain('browser-only-');

  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.locator('.sidebar').evaluate(el => el.getBoundingClientRect().right)).toBeLessThanOrEqual(1);
  await panel.getByLabel('自动修复 · 代码修复员', { exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: 'test-results/model-providers-mobile.png' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  await expect(panel.getByLabel('自动修复 · 代码修复员', { exact: true })).toBeVisible();
  expect(errors).toEqual([]);
});
