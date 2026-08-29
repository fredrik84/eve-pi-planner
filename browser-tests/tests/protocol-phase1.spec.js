const { test, expect } = require('@playwright/test');
const { installSession, watchBrowser } = require('./helpers');

test.describe('@protocol production acceptance: settings and phase 1', () => {
  test.beforeEach(async ({ page }) => {
    expect(await installSession(page), 'PP_SESSION is required for protocol tests').toBeTruthy();
  });

  test('P1-R0 — reaction pipeline cards remain compact and usable', async ({ page }) => {
    const created = await page.request.post('/api/reactions/orders', { data: {
      type_id: 16669, target_qty: 200, client_name: 'Protocol compact cards',
    }});
    expect(created.ok()).toBeTruthy();
    await page.goto('/reactions');
    const cards = page.locator('#rxOverviewPanel .ind-pipe-card');
    await expect(cards.first()).toBeVisible();
    const layout = await page.locator('#rxOverviewPanel').evaluate(el => ({
      clientWidth: el.clientWidth, scrollWidth: el.scrollWidth,
    }));
    expect(layout.scrollWidth).toBeLessThanOrEqual(layout.clientWidth + 1);
    await expect(cards.first()).toHaveAttribute('title', /job|runs|installed/i);
    const keys = page.locator('#rxOverviewPanel .rx-order-slot-tag');
    await expect(keys.first()).toBeVisible();
  });

  test('S1 — inventory, structures, markets, and build rules are distinct settings jobs', async ({ page }) => {
    const assertNoBrowserFailures = watchBrowser(page);
    await page.goto('/manufacturing');
    await page.locator('.header-settings-btn').click();
    await expect(page.locator('#settingsModal')).toBeVisible();

    await page.locator('#settingsNavBlueprints').click();
    await expect(page.locator('#settingsSecBlueprints')).toBeVisible();
    await expect(page.locator('#settingsSecBlueprints')).toContainText(/Blueprints|formulas/i);
    await expect(page.locator('#settingsSecBlueprints')).toContainText(/ESI|inventory/i);
    await expect(page.locator('#settingsSecBlueprints')).toContainText(/paste/i);

    await page.locator('#settingsNavMarkets').click();
    await expect(page.locator('#settingsSecMarkets')).toBeVisible();
    await expect(page.locator('#settingsSecMarkets')).toContainText(/Market prices/i);
    await expect(page.locator('#settingsSecMarkets')).toContainText(/Production facilities/i);
    await expect(page.locator('#settingsSecMarkets')).toContainText(/Operating costs/i);

    await page.locator('#settingsNavBuildrules').click();
    await expect(page.locator('#settingsSecBuildrules')).toBeVisible();
    await expect(page.locator('#settingsSecBuildrules')).toContainText(/Build rules/i);
    await expect(page.locator('#settingsSecBuildrules')).toContainText(/reaction|production/i);
    assertNoBrowserFailures();
  });

  test('P1-R1 — Reactions opens on Overview with value and capacity first', async ({ page }) => {
    const assertNoBrowserFailures = watchBrowser(page);
    await page.goto('/reactions');
    const overview = page.locator('[data-tabgroup="rx"][data-tabkey="overview"]');
    await expect(overview).toHaveClass(/active/);
    await expect(page.locator('#rxOverviewPanel')).toBeVisible();
    await expect(page.locator('#rxOverviewPanel .pp-card').first()).toContainText('Plan value & capacity');
    await expect(page.locator('#rxMetricsContent')).not.toContainText('Loading…');
    assertNoBrowserFailures();
  });

  test('P1-R2 — creating a reaction order returns to Overview', async ({ page }) => {
    await page.goto('/reactions');
    const orders = page.locator('[data-tabgroup="rx"][data-tabkey="orders"]');
    await expect(orders).toBeVisible();
    await orders.click();
    await expect(orders).toHaveClass(/active/);
    await expect(page.locator('#rxOrdersPanel')).toBeVisible();
    await page.getByRole('button', { name: '+ New order' }).click();
    await expect(page.locator('#rxNewOrderModal')).toBeVisible();
    await expect(page.locator('#rxOrderCreateStatus')).not.toContainText('Loading the product list…', { timeout: 30_000 });
    await page.locator('#rxOrderProduct').fill('Ferrofluid');
    await expect(page.locator('#rxOrderProductDropdown .rx-man-product-row').first()).toBeVisible();
    await page.locator('#rxOrderProductDropdown .rx-man-product-row').first().click();
    await page.locator('#rxOrderQty').fill('20');
    await page.locator('#rxOrderClient').fill('Protocol R');
    await page.locator('#rxOrderReviewBtn').click();
    await expect(page.locator('#rxOrderCreateBtn')).toBeVisible();
    await expect(page.locator('#rxOrderReview')).toContainText(/Material cost|Cost to produce/i);
    await page.locator('#rxNewOrderModal .pp-modal-box').evaluate(el => { el.scrollTop = el.scrollHeight; });
    await page.locator('#rxOrderCreateBtn').click();
    await expect(page.locator('#rxNewOrderModal')).toBeHidden();
    await expect(page.locator('[data-tabgroup="rx"][data-tabkey="overview"]')).toHaveClass(/active/);
    await expect(page.locator('#rxOverviewPanel')).toContainText('Protocol R');
  });

  test('P1-R3 — order review material cost equals its stock-netted material rows', async ({ page }) => {
    await page.goto('/reactions');
    await page.locator('[data-tabgroup="rx"][data-tabkey="orders"]').click();
    await page.getByRole('button', { name: '+ New order' }).click();
    await expect(page.locator('#rxOrderCreateStatus')).not.toContainText('Loading the product list…', { timeout: 30_000 });
    await page.locator('#rxOrderProduct').fill('Ferrofluid');
    await page.locator('#rxOrderProductDropdown .rx-man-product-row').first().click();
    await page.locator('#rxOrderQty').fill('20');
    const previewResponse = page.waitForResponse(r => r.url().includes('/api/reactions/orders/preview') && r.request().method() === 'POST');
    await page.locator('#rxOrderReviewBtn').click();
    const report = await (await previewResponse).json();
    await expect(page.locator('#rxOrderReview')).toContainText('Cost to produce');
    const rowTotal = report.materials.reduce((sum, row) => sum + row.unit_cost * row.quantity, 0);
    expect(report.cost.material_cost).toBeCloseTo(rowTotal, 2);
    expect(report.cost.total_cost).toBeGreaterThanOrEqual(report.cost.material_cost);
    await expect(page.locator('#rxOrderReview details.rx-order-materials summary')).toContainText(/lines?.*[KMBT]/i);
  });

  test('P1-M1 — Manufacturing Overview opens normally and presents its primary action', async ({ page }) => {
    const assertNoBrowserFailures = watchBrowser(page);
    const response = await page.goto('/manufacturing');
    expect(response?.status()).toBe(200);
    await expect(page.locator('.tab[data-tab="industry"]')).toHaveClass(/active/);
    await expect(page.locator('#tab-industry')).toBeVisible();
    await expect(page.locator('#tab-industry')).toContainText(/Add manufacturing work|Your build/i);
    assertNoBrowserFailures();
  });

  test('P1-M2 — adding Manufacturing work returns to Overview', async ({ page }) => {
    await page.goto('/manufacturing');
    await page.getByRole('button', { name: /Add manufacturing work/i }).click();
    await expect(page.locator('#indPlanModal')).toBeVisible();
    await page.locator('#indSearch').fill('Hobgoblin I');
    await expect(page.locator('#indSearchResults .ind-search-row').first()).toBeVisible();
    await page.locator('#indSearchResults .ind-search-row').first().click();
    await page.locator('#indQty').fill('2');
    await page.locator('#indLabel').fill('Protocol M1');
    await page.locator('#indPlanBtn').click();
    await expect(page.locator('#indResult')).toContainText(/Net cost|Total cost/i, { timeout: 30_000 });
    await page.locator('#indQueueBtn').click();
    await expect(page.locator('#indPlanModal')).toBeHidden({ timeout: 30_000 });
    await expect(page.locator('#tab-industry')).toContainText('Protocol M1');
    await expect(page.locator('[data-tabgroup="ind"][data-tabkey="status"]')).toHaveClass(/active/);
  });

  test('P1-M3 — Manufacturing controls preserve the plan and margin arithmetic', async ({ page }) => {
    await page.goto('/manufacturing');
    await page.getByRole('button', { name: /Add manufacturing work/i }).click();
    await page.locator('#indSearch').fill('Hobgoblin I');
    await page.locator('#indSearchResults .ind-search-row').first().click();
    await expect(page.locator('#indMarginal')).toBeVisible();
    await expect(page.locator('#indMargin')).toBeVisible();

    await page.evaluate(() => openSettingsModal('buildrules'));
    await expect(page.locator('#settingsModal')).toBeVisible();
    const layers = await page.evaluate(() => ({
      rules: Number(getComputedStyle(document.querySelector('#settingsModal')).zIndex),
      planner: Number(getComputedStyle(document.querySelector('#indPlanModal')).zIndex),
    }));
    expect(layers.rules).toBeGreaterThanOrEqual(layers.planner);
    await page.locator('#settingsModal .pp-modal-close').click();
    await expect(page.locator('#indPlanModal')).toBeVisible();
    await expect(page.locator('#indSearch')).toHaveValue('Hobgoblin I');

    await page.locator('#indPlanBtn').click();
    await expect(page.locator('#indResult')).toContainText(/Net cost|Total cost/i, { timeout: 30_000 });
    await page.locator('#indMargin').fill('0');
    await page.locator('#indMargin').dispatchEvent('input');
    const zero = await page.evaluate(() => ({
      net: _indLastPlan.metrics.net_cost ?? _indLastPlan.metrics.total_cost,
      quote: _indPriceOf(_indLastPlan.metrics, 0),
      text: document.querySelector('#indMarginLive').textContent,
    }));
    expect(zero.quote).toBeCloseTo(zero.net, 6);
    await page.locator('#indMargin').fill('10');
    await page.locator('#indMargin').dispatchEvent('input');
    const ten = await page.evaluate(() => ({
      net: _indLastPlan.metrics.net_cost ?? _indLastPlan.metrics.total_cost,
      quote: _indPriceOf(_indLastPlan.metrics, 10),
      text: document.querySelector('#indMarginLive').textContent,
    }));
    expect(ten.quote).toBeCloseTo(ten.net * 1.1, 6);
    expect(ten.text).not.toBe(zero.text);
  });
});
