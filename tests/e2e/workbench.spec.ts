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

test('desktop: real SDK interruption, manual evidence, resumption and drafts', async ({ page }) => {
  const browserErrors: string[] = [];
  page.on('pageerror', error => browserErrors.push(error.message));
  await enter(page);
  await expect(page.locator('.repository-table').getByText('samples / page-window', { exact: true })).toBeVisible();
  await page.screenshot({ path: 'test-results/desktop-overview.png', fullPage: true, animations: 'disabled' });
  await page.getByRole('button', { name: '运行样例', exact: true }).first().click();
  await expect(page.locator('.finding-detail .status')).toHaveText('待人工复现');
  const confirm = page.getByRole('button', { name: '确认已复现并继续' });
  await expect(confirm).toBeDisabled();
  await page.getByRole('button', { name: '执行复现' }).click();
  await expect(page.getByText('观察到预期与实际结果不一致')).toBeVisible();
  await page.getByRole('checkbox').check();
  await page.getByLabel('复现备注').fill('Browser QA: personally checked the controlled fixture.');
  await page.screenshot({ path: 'test-results/desktop-reproduction.png', fullPage: true, animations: 'disabled' });
  await confirm.click();
  await expect(page.locator('.finding-detail .status')).toHaveText('样例已完成');
  await page.getByRole('tab', { name: '交接记录' }).click();
  await expect(page.locator('.finding-detail .event-icon.handoff')).toHaveCount(5);
  await page.screenshot({ path: 'test-results/desktop-handoffs.png', fullPage: true, animations: 'disabled' });
  await page.getByRole('tab', { name: '本地草稿' }).click();
  await expect(page.getByText('5 项样例断言通过')).toBeVisible();
  await expect(page.getByText('未发布至 GitHub')).toHaveCount(2);
  await expect(page.getByRole('button', { name: '合并', exact: true })).toHaveCount(0);
  expect(browserErrors).toEqual([]);
});

test('mobile: negative result, repository registration and no overflow', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await enter(page);
  await page.getByRole('button', { name: '运行样例', exact: true }).first().click();
  await expect(page.locator('.finding-detail .status')).toHaveText('待人工复现');
  await page.getByLabel('页码', { exact: true }).fill('1');
  await page.getByRole('button', { name: '执行复现' }).click();
  await expect(page.getByText('本次输入未复现问题')).toBeVisible();
  await expect(page.getByRole('button', { name: '确认已复现并继续' })).toBeDisabled();
  await page.screenshot({ path: 'test-results/mobile-reproduction.png', fullPage: true, animations: 'disabled' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.getByRole('button', { name: '未能复现', exact: true }).click();
  await expect(page.locator('.finding-detail .status')).toHaveText('未能复现');
  await page.getByRole('tab', { name: '本地草稿' }).click();
  await expect(page.getByText('暂无草稿')).toBeVisible();
  await page.getByRole('button', { name: '添加仓库', exact: true }).click();
  await page.getByLabel('GitHub 仓库链接').fill('https://github.com/openai/openai-agents-python');
  await page.getByRole('dialog').getByRole('button', { name: '添加仓库', exact: true }).click();
  await expect(page.getByText('openai/openai-agents-python', { exact: true })).toBeVisible();
  await expect(page.locator('.repository-row').filter({ hasText: 'openai/openai-agents-python' })
    .getByRole('button', { name: '管理运行环境' })).toBeEnabled();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.getByRole('button', { name: '打开导航' }).click();
  await page.getByRole('button', { name: '运行配置', exact: true }).click();
  await expect(page.getByText('6 个已注册角色')).toBeVisible();
  await page.screenshot({ path: 'test-results/mobile-settings.png', fullPage: true, animations: 'disabled' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test('shared memory: owner knowledge, scoped references, edits and archive', async ({ page }) => {
  test.setTimeout(90_000);
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await enter(page);
  await page.getByRole('button', { name: '运行样例', exact: true }).first().click();
  await expect(page.locator('.finding-detail .status')).toHaveText('待人工复现');
  const revision = await page.locator('.detail-repo code').getAttribute('title');
  await page.getByRole('button', { name: '未能复现', exact: true }).click();
  await expect(page.locator('.finding-detail .status')).toHaveText('未能复现');
  await page.getByRole('button', { name: '仓库记忆', exact: true }).click();
  await page.getByRole('button', { name: '添加知识', exact: true }).click();
  await page.getByLabel('知识分类', { exact: true }).selectOption('testing');
  await expect(page.getByLabel('适用代码版本', { exact: true })).toHaveValue(revision!);
  const title = `QA shared memory ${Date.now()}`;
  const content = 'Use the pagination boundary cases; treat this as reference data only.';
  await page.getByLabel('知识标题').fill(title);
  await page.getByLabel('知识内容').fill(content);
  await page.getByRole('button', { name: '保存知识', exact: true }).click();
  await page.getByLabel('搜索记忆').fill(title);
  await expect(page.locator('.memory-entry')).toHaveCount(1);
  await expect(page.getByText('手工知识 · 未验证', { exact: true })).toBeVisible();
  await page.getByLabel('筛选代码版本').fill('another-revision');
  await expect(page.getByText('暂无匹配的记忆')).toBeVisible();
  await page.getByLabel('筛选代码版本').fill(revision!);
  await expect(page.locator('.memory-entry')).toHaveCount(1);
  await page.screenshot({ path: 'test-results/desktop-memory.png', fullPage: true, animations: 'disabled' });
  await page.getByRole('button', { name: '添加仓库', exact: true }).click();
  await page.getByLabel('GitHub 仓库链接').fill('https://github.com/memory-isolation/project');
  await page.getByRole('dialog').getByRole('button', { name: '添加仓库', exact: true }).click();
  await page.getByRole('button', { name: '仓库记忆', exact: true }).click();
  await page.getByLabel('所属仓库').selectOption({ label: 'memory-isolation/project' });
  await expect(page.getByText('暂无匹配的记忆')).toBeVisible();
  await expect(page.getByRole('heading', { name: title })).toHaveCount(0);
  await page.getByLabel('所属仓库').selectOption('repo_fixture');
  await page.getByLabel('搜索记忆').fill(title);
  await expect(page.getByRole('heading', { name: title })).toBeVisible();
  await page.getByRole('button', { name: '运行样例', exact: true }).first().click();
  await expect(page.locator('.finding-detail .status')).toHaveText('待人工复现');
  await expect(page.getByRole('button', { name: '确认已复现并继续' })).toBeDisabled();
  await page.getByRole('button', { name: '执行复现' }).click();
  await page.getByRole('checkbox').check();
  await page.getByRole('button', { name: '确认已复现并继续' }).click();
  await expect(page.locator('.finding-detail .status')).toHaveText('样例已完成');
  await page.getByRole('tab', { name: '记忆引用' }).click();
  await expect(page.getByRole('heading', { name: title })).toHaveCount(6);
  await page.getByRole('button', { name: '仓库记忆', exact: true }).click();
  await page.getByLabel('搜索记忆').fill(title);
  await page.getByRole('button', { name: '编辑知识', exact: true }).click();
  await page.getByLabel('知识内容').fill('Updated ' + content);
  await page.getByRole('button', { name: '保存知识', exact: true }).click();
  await expect(page.locator('.memory-entry p').first()).toHaveText('Updated ' + content);
  await page.getByRole('button', { name: '查看修改记录' }).click();
  await expect(page.locator('.memory-history').getByText(content, { exact: true })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.locator('.sidebar').evaluate(el =>
    el.getBoundingClientRect().right)).toBeLessThanOrEqual(0);
  await page.screenshot({ path: 'test-results/mobile-memory.png', fullPage: true, animations: 'disabled' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.getByRole('button', { name: '归档记忆' }).click();
  await expect(page.getByText('暂无匹配的记忆')).toBeVisible();
  await page.getByRole('button', { name: '已归档', exact: true }).click();
  await expect(page.getByRole('heading', { name: title })).toBeVisible();
  await page.getByRole('button', { name: '恢复记忆' }).click();
  await expect(page.getByText('暂无匹配的记忆')).toBeVisible();
  await page.getByRole('button', { name: '有效记忆', exact: true }).click();
  await expect(page.getByRole('heading', { name: title })).toBeVisible();
  expect(errors).toEqual([]);
});

test('existing Issue sample: automatic validation without human confirmation', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await enter(page);
  await page.getByRole('button', { name: '已有 Issue 样例', exact: true }).click();
  await expect(page.locator('.finding-detail .status')).toHaveText('样例已完成');
  await expect(page.getByText('自动验证通过', { exact: true })).toBeVisible();
  await expect(page.getByText('fixture://page-window/issues/1', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '确认已复现并继续' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '执行复现', exact: true })).toHaveCount(0);
  await page.screenshot({ path: 'test-results/desktop-existing-issue.png', fullPage: true, animations: 'disabled' });
  await page.getByRole('tab', { name: '本地草稿' }).click();
  await expect(page.locator('.artifact')).toHaveCount(3);
  await expect(page.getByText('5 项样例断言通过')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Issue 草稿', exact: true })).toHaveCount(0);
  await page.getByRole('tab', { name: '自动验证', exact: true }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.locator('.sidebar').evaluate(el =>
    el.getBoundingClientRect().right)).toBeLessThanOrEqual(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: 'test-results/mobile-existing-issue.png', fullPage: true, animations: 'disabled' });
  expect(errors).toEqual([]);
});


test('sample-free workspace keeps real repositories and hides all sample launchers', async ({ page }) => {
  await page.route('**/api/dashboard', route => route.fulfill({ json: {
    repositories: [{ id: 'repo_real_only', name: 'owner / real-project',
      url: 'https://github.com/owner/real-project', kind: 'github', language: 'Python' }],
    runs: [], events: [],
  } }));
  await page.route('**/api/analysis-notifications', route => route.fulfill({ json: [{
    repo_id: 'repo_real_only', repository: 'owner / real-project', status: 'completed', waiting: 0,
    repair_published: 2,
  }] }));
  await enter(page);
  await expect(page.getByRole('button', { name: '运行样例', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '已有 Issue 样例', exact: true })).toHaveCount(0);
  await expect(page.getByText('含本地受控样例', { exact: true })).toHaveCount(0);
  await expect(page.getByText('已完成样例', { exact: true })).toHaveCount(0);
  await expect(page.locator('.metric.done strong')).toHaveText('2');
  await expect(page.locator('.repository-table').getByText('owner / real-project', { exact: true })).toBeVisible();
  await page.screenshot({ path: 'test-results/sample-free-desktop.png', fullPage: true, animations: 'disabled' });
  await page.getByRole('button', { name: '运行配置', exact: true }).click();
  await expect(page.getByText('样例模型', { exact: true })).toHaveCount(0);
  await expect(page.getByText('自动验证（仅样例）', { exact: true })).toHaveCount(0);
  await expect(page.locator('.roles-list')).not.toContainText('样例');
  await page.getByRole('button', { name: '仓库', exact: true }).click();
  await expect(page.locator('.repo-page .repository-row').getByText('owner / real-project', { exact: true })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.locator('.sidebar').evaluate(el => el.getBoundingClientRect().right)).toBeLessThanOrEqual(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: 'test-results/sample-free-mobile.png', fullPage: true, animations: 'disabled' });
});
