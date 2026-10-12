import { test, expect } from '@playwright/test';

test('GitHub account settings: connect, switch, failures, access check and disconnect', async ({ page }) => {
  test.setTimeout(90_000);
  type Account = {
    id: number; login: string; state: string; scopes: string[] | null;
    checked_at: number; version: number; active: boolean;
  };
  let accounts: Account[] = [];
  let activeId: number | null = null;
  let expireNext = false;
  const snapshot = () => ({
    accounts: accounts.map(a => ({ ...a, active: a.id === activeId })),
    active_account_id: activeId, secure_storage: 'available', github_publication: false,
  });
  const browserErrors: string[] = [];
  page.on('pageerror', error => browserErrors.push(error.message));
  // UI behavior uses synthetic accounts; backend tests exercise the real adapter with MockTransport.
  await page.route('**/api/github/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() === 'GET') {
      await route.fulfill({ json: snapshot() }); return;
    }
    const body = route.request().postDataJSON();
    if (path === '/api/github/accounts') {
      if (body.username === 'wrong-user') {
        await route.fulfill({ status: 409, json: { detail: 'github_account_mismatch' } }); return;
      }
      const id = body.username === 'alice' ? 101 : 202;
      const previous = accounts.find(a => a.id === id);
      accounts = accounts.filter(a => a.id !== id);
      accounts.push({
        id, login: body.username, state: 'verified', scopes: id === 101 ? ['repo'] : null,
        checked_at: Date.now() / 1000, version: (previous?.version ?? 0) + 1, active: true,
      });
      activeId = id;
    } else {
      const id = Number(path.split('/')[4]);
      const account = accounts.find(a => a.id === id)!;
      expect(body.version).toBe(account.version);
      if (path.endsWith('/repository-access')) {
        await route.fulfill({ json: {
          account_id: id, repository: 'alice/project', private: true,
          visible: true, write_access: 'not_verified',
        } }); return;
      }
      if (path.endsWith('/disconnect')) {
        accounts = accounts.filter(a => a.id !== id);
        if (activeId === id) activeId = null;
      } else if (expireNext) {
        expireNext = false; account.state = 'invalid'; account.version++;
        await route.fulfill({ status: 401, json: { detail: 'github_invalid_token' } }); return;
      } else {
        account.version++;
        if (path.endsWith('/activate')) activeId = id;
      }
    }
    await route.fulfill({ json: snapshot() });
  });
  await page.goto('/');
  await page.getByLabel('管理密码', { exact: true }).fill('qa-only-owner-password');
  if (await page.getByRole('heading', { name: '创建本地工作空间' }).isVisible()) {
    await page.getByLabel('确认密码', { exact: true }).fill('qa-only-owner-password');
    await page.getByRole('button', { name: '创建并进入' }).click();
  } else {
    await page.getByRole('button', { name: '进入工作空间' }).click();
  }
  await expect(page.getByRole('heading', { name: '工作台', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '添加仓库', exact: true }).click();
  await page.getByLabel('GitHub 仓库链接').fill('https://github.com/alice/project');
  await page.getByRole('dialog').getByRole('button', { name: '添加仓库', exact: true }).click();
  await page.getByRole('button', { name: '运行配置', exact: true }).click();
  const panel = page.locator('.github-settings');
  await expect(panel.getByText('尚未连接 GitHub 账号')).toBeVisible();
  await panel.getByRole('button', { name: '连接账号', exact: true }).click();
  await page.getByLabel('GitHub 用户名', { exact: true }).fill('wrong-user');
  await page.getByLabel('访问令牌（PAT）').fill('test_token_not_a_real_credential');
  await page.getByRole('button', { name: '验证并连接', exact: true }).click();
  await expect(panel.getByRole('alert')).toContainText('账号与所选用户名不一致');
  await expect(page.getByLabel('访问令牌（PAT）')).toHaveValue('');
  await page.getByLabel('GitHub 用户名', { exact: true }).fill('alice');
  await page.getByLabel('访问令牌（PAT）').fill('test_token_not_a_real_credential');
  await page.getByRole('button', { name: '验证并连接', exact: true }).click();
  await expect(panel.getByRole('radio', { name: '使用账号 alice' })).toBeChecked();
  await panel.getByRole('button', { name: '连接账号', exact: true }).click();
  await page.getByLabel('GitHub 用户名', { exact: true }).fill('bob');
  await page.getByLabel('访问令牌（PAT）').fill('test_token_not_a_real_credential');
  await page.getByRole('button', { name: '验证并连接', exact: true }).click();
  await expect(panel.getByRole('radio', { name: '使用账号 bob' })).toBeChecked();
  await expect(panel.getByText('令牌权限范围：GitHub 未返回')).toBeVisible();
  await panel.getByRole('radio', { name: '使用账号 alice' }).click();
  await expect(panel.getByRole('radio', { name: '使用账号 alice' })).toBeChecked();
  await panel.getByLabel('检查访问的仓库').selectOption({ label: 'alice/project' });
  await panel.getByRole('button', { name: '检查访问', exact: true }).click();
  await expect(panel.getByText('私有仓库 · 可读取')).toBeVisible();
  await expect(panel.getByText('写入权限：未核验')).toBeVisible();
  await page.screenshot({ path: 'test-results/desktop-github-accounts.png', fullPage: true, animations: 'disabled' });
  expireNext = true;
  await panel.getByRole('button', { name: '校验 alice', exact: true }).click();
  await expect(panel.getByText('授权已失效', { exact: true })).toBeVisible();
  await expect(panel.getByRole('radio', { name: '使用账号 alice' })).toBeDisabled();
  await expect(panel.getByText('私有仓库 · 可读取')).toHaveCount(0);
  await panel.getByRole('button', { name: '更新 alice 的令牌', exact: true }).click();
  await expect(page.getByLabel('GitHub 用户名', { exact: true })).toHaveValue('alice');
  await page.getByLabel('访问令牌（PAT）').fill('test_token_not_a_real_credential');
  await page.getByRole('button', { name: '验证并连接', exact: true }).click();
  await expect(panel.getByRole('radio', { name: '使用账号 alice' })).toBeEnabled();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.locator('.sidebar').evaluate(el =>
    el.getBoundingClientRect().right)).toBeLessThanOrEqual(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: 'test-results/mobile-github-accounts.png', fullPage: true, animations: 'disabled' });
  await panel.getByRole('button', { name: '解绑 alice', exact: true }).click();
  await panel.getByRole('button', { name: '确认解绑', exact: true }).click();
  await expect(panel.getByText('当前账号：未选择', { exact: true })).toBeVisible();
  await expect(panel.getByRole('radio', { name: '使用账号 alice' })).toHaveCount(0);
  await expect(panel.getByRole('radio', { name: '使用账号 bob' })).not.toBeChecked();
  expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain('test_token');
  expect(await page.locator('body').innerText()).not.toContain('test_token');
  expect(browserErrors).toEqual([]);
});
