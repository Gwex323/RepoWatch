import { test, expect, type Page } from '@playwright/test';

async function enter(page: Page) {
  await page.goto('/');
  await page.getByLabel('管理密码', { exact: true }).fill('qa-only-owner-password');
  await page.getByRole('button', { name: '进入工作空间' }).click();
  await page.getByRole('button', { name: '系统故障', exact: true }).click();
  await expect(page.getByRole('heading', { name: '系统故障', exact: true })).toBeVisible();
}

test('automatic recovery is opt-in, persists and can be disabled without changing tasks', async ({ page }) => {
  await enter(page);
  const toggle = page.getByRole('checkbox', { name: '自动恢复新发生的流程故障' });
  await expect(toggle).not.toBeChecked();
  const before = await (await page.request.get('/api/dashboard')).json();
  await toggle.check();
  await expect(toggle).toBeEnabled();
  await page.reload();
  await page.getByRole('button', { name: '系统故障', exact: true }).click();
  await expect(toggle).toBeChecked();
  await toggle.uncheck();
  await expect(toggle).toBeEnabled();
  await expect(toggle).not.toBeChecked();
  const after = await (await page.request.get('/api/dashboard')).json();
  expect(after).toEqual(before);
  expect((await (await page.request.get('/api/system/recovery')).json()).enabled).toBe(false);
});

test('fault ledger filters, diagnoses, closes with reason and links the original task', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await enter(page);
  await page.getByLabel('仓库', { exact: true }).selectOption({ label: 'owner/fault-fixture' });
  await expect(page.locator('.fault-row')).toHaveCount(1);
  await expect(page.locator('.fault-row')).toContainText('出现 2 次');
  await page.locator('.fault-row').click();
  await expect(page.getByLabel('故障详情')).toContainText('2 次待处理');
  await expect(page.getByText('本批调用未执行', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('调用数量超过上限', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('fixture-model', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('调用数量', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('允许上限', { exact: true }).first()).toBeVisible();
  await page.screenshot({ path: 'data/qa-78/faults-desktop.png', fullPage: true });
  await page.getByRole('button', { name: '关闭此出现记录', exact: true }).first().click();
  await page.getByLabel('关闭原因', { exact: true }).fill('该历史出现无需继续处理');
  await page.getByRole('button', { name: '确认关闭记录', exact: true }).click();
  await expect(page.getByLabel('故障详情')).toContainText('1 次待处理');
  await expect(page.getByText('该历史出现无需继续处理', { exact: true })).toBeVisible();
  await expect(page.getByLabel('故障详情')).toContainText('已关闭');
  await page.getByRole('button', { name: /查看来源任务/ }).first().click();
  await expect(page.getByRole('heading', { name: '项目分析', exact: true })).toBeVisible();
  await expect(page.getByLabel('分析记录', { exact: true })).toHaveValue('analysis_1111111111111111');
  await expect(page.getByRole('link', { name: '查看此任务的系统故障记录' })).toBeVisible();
  await page.getByRole('link', { name: '查看此任务的系统故障记录' }).click();
  await expect(page.getByLabel('来源任务', { exact: true })).toHaveValue('analysis_1111111111111111');
  await page.getByLabel('流程', { exact: true }).selectOption('publication');
  await expect(page.getByText('当前范围没有故障记录', { exact: true })).toBeVisible();
  expect(errors).toEqual([]);
});

test('fault ledger fits mobile width and preserves read-only phase boundaries', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  await page.getByLabel('管理密码', { exact: true }).fill('qa-only-owner-password');
  await page.getByRole('button', { name: '进入工作空间' }).click();
  await page.getByRole('button', { name: '打开导航', exact: true }).click();
  await page.getByRole('button', { name: '系统故障', exact: true }).click();
  await page.getByLabel('流程', { exact: true }).selectOption('service');
  await page.locator('.fault-row').first().click();
  await expect(page.getByLabel('故障详情')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  await page.screenshot({ path: 'data/qa-78/faults-mobile.png', fullPage: true });
  const before = await (await page.request.get('/api/dashboard')).json();
  await page.getByRole('button', { name: '补录一批历史故障', exact: true }).click();
  await expect(page.getByRole('button', { name: '补录一批历史故障', exact: true })).toBeEnabled();
  const after = await (await page.request.get('/api/dashboard')).json();
  expect(after).toEqual(before);
  const config = await (await page.request.get('/api/system')).json();
  expect(config.roles).toHaveLength(6);
  expect(config.system_faults.mode).toBe('record_only');
  expect(config.external_model_calls).toBe(false);
});

test('switching groups hides stale details and close controls while loading', async ({ page }) => {
  await enter(page);
  const groups = (await (await page.request.get('/api/system/faults')).json()).items as
    { id: string; flow: string }[];
  const service = groups.find(item => item.flow === 'service')!;
  await page.locator('.fault-row').filter({ hasText: 'analysis_scope_read_not_followed' })
    .filter({ hasText: '仓库分析' }).click();
  await expect(page.getByLabel('故障详情')).toContainText('fixture-model');
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  await page.route(`**/api/system/faults/${service.id}?*`, async route => {
    await gate;
    await route.continue();
  });
  try {
    await page.locator('.fault-row').filter({ hasText: 'worker_failed' }).click();
    await expect(page.getByLabel('故障详情')).not.toContainText('fixture-model');
    await expect(page.getByRole('button', { name: '关闭此出现记录', exact: true })).toHaveCount(0);
  } finally { release(); }
  await expect(page.getByLabel('故障详情')).toContainText('fixture_worker');
});
