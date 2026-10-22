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

test('workspace supports real repositories, memory and registered roles', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await enter(page);
  await expect(page.getByRole('button', { name: '运行样例', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '已有 Issue 样例', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: '添加仓库', exact: true }).click();
  await page.getByLabel('GitHub 仓库链接').fill('https://github.com/owner/workbench');
  await page.getByRole('dialog').getByRole('button', { name: '添加仓库', exact: true }).click();
  await page.getByRole('button', { name: '仓库记忆', exact: true }).click();
  await page.getByLabel('所属仓库').selectOption({ label: 'owner/workbench' });
  await page.getByRole('button', { name: '添加知识', exact: true }).click();
  await page.getByLabel('知识标题', { exact: true }).fill('Real repository test command');
  await page.getByLabel('知识内容', { exact: true }).fill('python -m pytest');
  await page.getByLabel('适用代码版本', { exact: true }).fill('a'.repeat(40));
  await page.getByRole('button', { name: '保存知识', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Real repository test command', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '运行配置', exact: true }).click();
  await expect(page.locator('.roles-list > div')).toHaveCount(5);
  await expect(page.locator('.roles-list')).not.toContainText('pr_author');
  await expect(page.locator('.roles-list')).not.toContainText('样例');
  expect(errors).toEqual([]);
});

test('mobile workspace has no horizontal overflow', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await enter(page);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  await page.getByRole('button', { name: '打开导航', exact: true }).click();
  await page.getByRole('button', { name: '问题与复现', exact: true }).click();
  await expect(page.getByRole('heading', { name: '问题与复现', exact: false })).toBeVisible();
  await expect(page.getByText('暂无匹配的问题')).toBeVisible();
});

test('real finding opens its original environment when a newer version exists', async ({ page }) => {
  const repo = { id: 'repo_real', name: 'owner/versions', kind: 'github',
    url: 'https://github.com/owner/versions', language: 'Python' };
  const oldEnv = {
    id: 'env_old', revision: 'b'.repeat(40), version: 1, state: 'downloaded', installed: false,
    busy: false, url: null, error: null, created: 1, expires: null, test_result: null, jobs: [],
    config: { language: 'python', install: '', test: 'pytest', start: 'python app.py',
      port: 8000, install_network: false, detected: true },
  };
  const newerEnv = { ...oldEnv, id: 'env_new', revision: 'c'.repeat(40), created: 2 };
  await page.route('**/api/dashboard', route => route.fulfill({ json: {
    repositories: [repo], findings: [{ id: 'finding_real', repo_id: repo.id,
      repository: repo.name, analysis_id: 'analysis_real', env_id: oldEnv.id,
      title: 'Original version defect', severity: 'P1', status: 'waiting_reproduction',
      revision: oldEnv.revision, repair_status: null, created: 1, updated: 1 }], events: [],
    counts: { waiting: 1, active: 0, published_prs: 0 },
  } }));
  await page.route('**/api/repositories/repo_real/environments', route =>
    route.fulfill({ json: [newerEnv, oldEnv] }));
  await page.route('**/api/environments/env_old', route => route.fulfill({ json: oldEnv }));
  await page.route('**/api/environments/env_old/analyses', route => route.fulfill({ json: [] }));
  await enter(page);
  await page.getByRole('button', { name: '问题与复现', exact: true }).click();
  await page.getByRole('button', { name: /Original version defect/ }).click();
  await expect(page.getByLabel('环境版本')).toHaveValue(oldEnv.id);
});
