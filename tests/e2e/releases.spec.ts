import { test, expect } from '@playwright/test';

test('release status stays explicit and fits desktop and mobile', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/');
  await page.getByLabel('管理密码', { exact: true }).fill('qa-only-owner-password');
  await page.getByRole('button', { name: '进入工作空间' }).click();
  await page.getByRole('button', { name: '系统故障', exact: true }).click();
  const panel = page.getByRole('region', { name: '发布与服务恢复', exact: true });
  await expect(panel).toContainText('非受管运行');
  await expect(panel).toContainText('默认关闭，逐份清单授权');
  await expect(panel).toContainText('暂无受管发布记录');
  await page.screenshot({ path: 'data/qa-82/releases-desktop.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(panel).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  await page.screenshot({ path: 'data/qa-82/releases-mobile.png', fullPage: true, animations: 'disabled' });
  expect(errors).toEqual([]);
});
