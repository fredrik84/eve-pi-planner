const { test, expect } = require('@playwright/test');
const { installSession, watchBrowser } = require('./helpers');

test.beforeEach(async ({ page }) => {
  await installSession(page);
});

test('the application shell loads without browser failures', async ({ page }) => {
  const assertNoBrowserFailures = watchBrowser(page);
  const response = await page.goto('/');
  expect(response?.ok()).toBeTruthy();
  await expect(page.locator('.app-layout')).toBeAttached();
  await expect(page.locator('#sidebar')).toBeAttached();
  await page.waitForLoadState('networkidle');
  assertNoBrowserFailures();
});

test('known SPA routes return the application instead of a server error', async ({ page }) => {
  for (const route of ['/', '/reactions', '/manufacturing']) {
    const response = await page.goto(route);
    expect(response?.status(), route).toBeLessThan(400);
    await expect(page.locator('.app-layout')).toBeAttached();
  }
});

test('the page does not overflow the mobile viewport', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'mobile', 'mobile-only check');
  await page.goto('/');
  const sizes = await page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    content: document.documentElement.scrollWidth,
  }));
  expect(sizes.content).toBeLessThanOrEqual(sizes.viewport + 1);
});

test('@authenticated production navigation is reachable', async ({ page }) => {
  test.skip(!process.env.PP_SESSION, 'Set PP_SESSION to run authenticated protocol checks');
  const assertNoBrowserFailures = watchBrowser(page);
  await page.goto('/reactions');
  await expect(page.locator('.tab[data-tab="reactions"]')).toBeVisible();
  await expect(page.locator('[data-tabgroup="rx"][data-tabkey="overview"]')).toBeVisible();
  await page.locator('[data-tabgroup="rx"][data-tabkey="orders"]').click();
  await expect(page).toHaveURL(/\/reactions\/orders$/);

  await page.goto('/manufacturing');
  await expect(page.locator('.tab[data-tab="industry"]')).toBeVisible();
  await expect(page.locator('[data-tabgroup="ind"][data-tabkey="status"]')).toBeVisible();
  assertNoBrowserFailures();
});
