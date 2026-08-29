const { test, expect } = require('@playwright/test');
const { installSession, watchBrowser } = require('./helpers');

const FERROFLUID = 16669;

async function createOrder(page, overrides = {}) {
  const response = await page.request.post('/api/reactions/orders', {
    data: {
      type_id: FERROFLUID,
      target_qty: 20,
      client_name: `Protocol ${Date.now()}`,
      recurring_interval_days: null,
      ...overrides,
    },
  });
  expect(response.ok(), await response.text()).toBeTruthy();
  return response.json();
}

async function openOrder(page, orderId) {
  const expected = await (await page.request.get(`/api/reactions/orders/${orderId}`)).json();
  await page.goto('/reactions');
  await page.locator('[data-tabgroup="rx"][data-tabkey="orders"]').click();
  await expect(page.locator('#rxOrdersContent')).not.toContainText('Loading…');
  await page.locator('.rx-order-row').filter({ hasText: expected.order.client_name }).click();
  await expect(page.locator('#rxOrderDetailModal')).toBeVisible();
}

async function cancelOpenOrders(page) {
  const response = await page.request.get('/api/reactions/orders');
  if (!response.ok()) return;
  for (const order of (await response.json()).orders.filter(o => o.status === 'open')) {
    await page.request.post(`/api/reactions/orders/${order.id}/status`, { data: { status: 'cancelled' } });
  }
}

test.describe('@protocol production acceptance: phase 2', () => {
  test.beforeEach(async ({ page }) => {
    expect(await installSession(page)).toBeTruthy();
    await cancelOpenOrders(page);
  });

  test('P2-R1 — blank cadence saves as the seven-day default', async ({ page }) => {
    await page.goto('/manufacturing');
    await page.locator('.header-settings-btn').click();
    await page.locator('#settingsNavBuildrules').click();
    await expect(page.locator('#ir-joblen')).toBeVisible();
    await page.locator('#ir-joblen').fill('0');
    await page.getByRole('button', { name: 'Save build rules' }).click();
    await expect(page.getByText('Build rules saved')).toBeVisible();
    await page.locator('#settingsModal .pp-modal-close').click();
    await page.locator('.header-settings-btn').click();
    await page.locator('#settingsNavBuildrules').click();
    await expect(page.locator('#ir-joblen')).toHaveValue('7');
  });

  test('P2-R2 — a recurring order assigns its first cycle immediately', async ({ page }) => {
    const created = await createOrder(page, {
      client_name: 'Protocol recurring',
      recurring_interval_days: 7,
    });
    expect(created.order.recurring_interval_days).toBe(7);
    expect(created.order.assigned_runs).toBeGreaterThan(0);
    expect(created.order.recurring_next_at).toBeGreaterThan(Date.now() / 1000);
    await openOrder(page, created.order.id);
    await expect(page.locator('#rxOrderDetailContent')).toContainText(/Repeats every 7 days/i);
    await expect(page.locator('#rxOrderDetailContent')).toContainText(/Next cycle/i);
  });

  test('P2-R3 — completing a recurring cycle frees work but preserves recurrence', async ({ page }) => {
    const created = await createOrder(page, {
      client_name: 'Protocol complete cycle',
      recurring_interval_days: 7,
    });
    await openOrder(page, created.order.id);
    const completedResponse = page.waitForResponse((response) =>
      response.url().endsWith(`/api/reactions/orders/${created.order.id}/status`) &&
      response.request().method() === 'POST');
    await page.getByRole('button', { name: 'Complete this cycle' }).click();
    await page.getByRole('alertdialog').getByRole('button', { name: 'Confirm' }).click();
    const completed = await completedResponse;
    expect(completed.ok(), await completed.text()).toBeTruthy();
    const detail = await page.request.get(`/api/reactions/orders/${created.order.id}`);
    expect(detail.ok()).toBeTruthy();
    const report = await detail.json();
    expect(report.order.status).toBe('open');
    expect(report.order.recurring_interval_days).toBe(7);
    expect(report.order.assigned_runs).toBe(0);
    expect(report.order.recurring_next_at).toBeGreaterThan(Date.now() / 1000);
  });

  test('P2-R4 — insufficient capacity remains visible and actionable', async ({ page }) => {
    for (let i = 0; i < 11; i++) {
      await createOrder(page, { client_name: `Protocol slot holder ${i}` });
    }
    const created = await createOrder(page, {
      target_qty: 20,
      client_name: 'Protocol blocked capacity',
      recurring_interval_days: 7,
    });
    expect(created.order.assigned_runs).toBeLessThan(created.order.top_level_runs);
    expect(created.auto_assign_error || created.order.recurring_error).toMatch(/slot|capacity|assign/i);
    await openOrder(page, created.order.id);
    await expect(page.locator('#rxOrderDetailContent')).toContainText(/slot|capacity|waiting|assign/i);
    await expect(page.getByRole('button', { name: 'Skip this cycle' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Stop recurring' })).toBeVisible();
  });

  test('P2-R5 — Balanced and Fastest are explicit persisted production modes', async ({ page }) => {
    const balanced = await page.request.post('/api/industry/build-setup', { data: { pace: { mode: 'balanced' } } });
    expect(balanced.ok()).toBeTruthy();
    expect((await balanced.json()).account.pace.mode).toBe('balanced');
    const fastest = await page.request.post('/api/industry/build-setup', { data: { pace: { mode: 'fastest' } } });
    expect(fastest.ok()).toBeTruthy();
    expect((await fastest.json()).account.pace.mode).toBe('fastest');

    await page.goto('/manufacturing');
    await page.locator('.header-settings-btn').click();
    await page.locator('#settingsNavBuildrules').click();
    await expect(page.locator('input[name="ir-pace"][value="fastest"]')).toBeChecked();
    await expect(page.locator('#settingsSecBuildrules')).toContainText(/useful spare reaction slots/i);
    await page.locator('input[name="ir-pace"][value="balanced"]').check();
    await page.getByRole('button', { name: 'Save build rules' }).click();
  });

  test('P2-M1 — Manufacturing tab changes do not duplicate its toolbar', async ({ page }) => {
    const assertNoBrowserFailures = watchBrowser(page);
    await page.goto('/manufacturing');
    const stripsBefore = await page.locator('#tab-industry > .pp-tabstrip, #indStatusCard > .pp-tabstrip').count();
    await page.locator('[data-tabgroup="ind"][data-tabkey="blueprints"]').click();
    await page.locator('[data-tabgroup="ind"][data-tabkey="status"]').click();
    await page.reload();
    await expect(page.locator('#tab-industry')).toBeVisible();
    const stripsAfter = await page.locator('#tab-industry > .pp-tabstrip, #indStatusCard > .pp-tabstrip').count();
    expect(stripsAfter).toBe(stripsBefore);
    assertNoBrowserFailures();
  });
});
