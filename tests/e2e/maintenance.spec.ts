import { test, expect } from '@playwright/test';

test('maintenance scope and default-off controls fit desktop and mobile', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/');
  await page.getByLabel('管理密码', { exact: true }).fill('qa-only-owner-password');
  await page.getByRole('button', { name: '进入工作空间' }).click();
  await page.getByRole('button', { name: '系统故障', exact: true }).click();
  const panel = page.getByRole('region', { name: '自身代码维护', exact: true });
  await expect(panel).toContainText('Gwex323/RepoWatch');
  await expect(panel).toContainText('仅所有者合并；不自动部署');
  await expect(panel).toContainText('暂无维护任务');
  const toggle = panel.getByRole('checkbox', { name: '授权维护新发生的内部缺陷' });
  await expect(toggle).not.toBeChecked();
  await expect(toggle).toBeDisabled();
  await page.screenshot({ path: 'data/qa-81/maintenance-desktop.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(panel).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  await page.screenshot({ path: 'data/qa-81/maintenance-mobile.png', fullPage: true, animations: 'disabled' });
  expect(errors).toEqual([]);
});
