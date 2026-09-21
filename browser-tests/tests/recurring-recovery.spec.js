const { test, expect } = require('@playwright/test');

function fn(source, name) {
  const at = source.search(new RegExp(`(?:async )?function ${name}\\(`));
  if (at < 0) throw Error(`Missing ${name}`);
  return source.slice(at, source.indexOf('\n}', at) + 2);
}

test.beforeEach(async ({ page, request }) => {
  const sources = await Promise.all(['app', 'dashboard', 'reactions'].map(async name => {
    const response = await request.get(`/${name}.js`);
    expect(response.ok()).toBeTruthy();
    return response.text();
  }));
  const [app, dashboard, reactions] = sources;
  const routeAt = app.indexOf('// ── Routing:');
  const switchAt = app.indexOf('\nfunction switchTab(', routeAt);
  const routing = app.slice(routeAt, app.indexOf('\n}\n', switchAt) + 3);
  await page.route('**/recurrence-test', route => route.fulfill({ contentType: 'text/html', body: `
    <div id="tab-dashboard" class="tab-panel"><div id="alerts"></div></div>
    <div id="tab-reactions" class="tab-panel" style="display:none">
      <div id="rxOrderDetailModal" style="display:none"><div id="rxOrderDetailTitle">Order</div>
      <div id="rxOrderDetailContent"></div></div></div><div id="toast"></div>` }));
  await page.goto('/recurrence-test');
  await page.addScriptTag({ content: `
    let _rxOpenOrderId = null;
    const _esc = s => String(s).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
    const _rxManufacturingSourcesHtml = () => '', _rxOrderBarHtml = () => '', _rxOrderReportBody = () => '';
    const onReactionsTabOpen = () => {}, onDashboardTabOpen = () => {}, _rxLoadOrders = async () => {}, _loadReactionsDashboard = async () => {};
    const toast = message => document.getElementById('toast').textContent = message;
    window.requests = [];
    window.order = {id: 46, name: 'Reinforced Carbon Fiber', status: 'open', target_qty: 1000,
      top_level_runs: 1000, assigned_runs: 1000, recurring_interval_days: 7,
      recurring_error: 'The previous recurring cycles must advance before another weekly batch can fit.'};
    const api = async () => ({order: {...window.order}});
    const apiSend = async (method, url, body) => {
      window.requests.push({method, url, body});
      if (window.failRefresh) throw Error('ESI is unavailable. Try again later.');
      if (window.clearOnRefresh) window.order.recurring_error = null;
      return {};
    };
    ${routing}
    ${fn(reactions, '_rxOpenOrderLink')}
    ${fn(reactions, '_renderRxOrderDetail')}
    ${fn(reactions, '_rxRefreshRecurringOrder')}
    ${fn(dashboard, '_renderReactionAlerts')}
    switchTab('dashboard');
    document.getElementById('alerts').innerHTML = _renderReactionAlerts({reaction_alerts: [{
      kind: 'recurring_order_blocked', order_id: 46, location: window.order.name, message: window.order.recurring_error
    }]});
  ` });
});

test('recurrence warning opens visible order recovery controls', async ({ page }) => {
  await page.getByRole('link', { name: 'Review options' }).click();
  await expect(page).toHaveURL(/\/reactions\/order\/46$/);
  await expect(page.locator('#rxOrderDetailModal')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Refresh jobs and retry', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Skip this cycle', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Stop recurring', exact: true })).toBeVisible();
  await expect(page.locator('#rxOrderDetailContent')).toContainText('let them finish, then refresh again');
});

test('dashboard recovery refreshes once without issuing a second batch release', async ({ page }) => {
  await page.evaluate(() => { window.clearOnRefresh = true; });
  await page.getByRole('button', { name: 'Refresh jobs and retry' }).click();
  await expect(page.locator('#toast')).toContainText('Job status updated');
  expect(await page.evaluate(() => window.requests)).toEqual([{method: 'POST', url: '/api/reactions/jobs/refresh'}]);
});

test('unfinished work and refresh failures stay visible and allow retry', async ({ page }) => {
  const button = page.getByRole('button', { name: 'Refresh jobs and retry' });
  await button.click();
  await expect(page.getByRole('status')).toContainText('previous recurring cycles must advance');
  await page.evaluate(() => { window.failRefresh = true; });
  await button.click();
  await expect(page.getByRole('status')).toContainText('ESI is unavailable');
  await expect(button).toBeEnabled();
});
