import { test, expect, type Page } from '@playwright/test';

const password = 'qa-only-owner-password';

async function enter(page: Page) {
  await page.goto('/');
  await expect(page.getByLabel('管理密码', { exact: true })).toBeVisible();
  await page.getByLabel('管理密码', { exact: true }).fill(password);
  if (await page.getByRole('heading', { name: '创建本地工作空间' }).isVisible()) {
    await page.getByLabel('确认密码', { exact: true }).fill(password);
    await page.getByRole('button', { name: '创建并进入' }).click();
  } else {
    await page.getByRole('button', { name: '进入工作空间' }).click();
  }
  await expect(page.getByRole('heading', { name: '工作台', exact: true })).toBeVisible();
}

test('repository schedule history: expand outcomes without overflow', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/schedules', route => route.fulfill({ json: [{
    repo_id: 'repo_history_qa', repository: 'owner / history-project', kind: 'github',
    enabled: true, local_time: '08:30', last_run_date: '2026-10-21',
    last_status: 'scheduled', last_detail: 'environment_history_qa',
  }] }));
  await page.route('**/api/repositories/repo_history_qa/schedule/history**', route =>
    route.fulfill({ json: [
      {
        run_date: '2026-10-21', status: 'scheduled', detail: '',
        environment_id: 'environment_history_qa', pipeline_id: null, created: 1761004800,
      },
      {
        run_date: '2026-10-20', status: 'failed', detail: 'github_active_account_required',
        environment_id: null, pipeline_id: null, created: 1760918400,
      },
      {
        run_date: '2026-10-19', status: 'skipped_active', detail: '',
        environment_id: null, pipeline_id: 'pipeline_history_qa', created: 1760832000,
      },
    ] }));
  await enter(page);
  await page.getByRole('button', { name: '仓库', exact: true }).click();
  const row = page.locator('.schedule-item').filter({ hasText: 'owner / history-project' });
  await expect(row).toBeVisible();
  await row.getByRole('button', { name: '查看巡检历史' }).click();
  const history = row.locator('.schedule-history');
  await expect(history).toContainText('已调度');
  await expect(history).toContainText('已创建运行环境 environment_history_qa');
  await expect(history).toContainText('调度失败');
  await expect(history).toContainText('需要激活 GitHub 账号');
  await expect(history).toContainText('活动任务中');
  await expect(history).toContainText('流水线 pipeline_history_qa');
  await page.screenshot({
    path: 'test-results/desktop-schedule-history.png', fullPage: true, animations: 'disabled',
  });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({
    path: 'test-results/mobile-schedule-history.png', fullPage: true, animations: 'disabled',
  });
  expect(errors).toEqual([]);
});
