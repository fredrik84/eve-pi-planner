const { test, expect } = require('@playwright/test');

function fn(source, name) {
  const at = source.search(new RegExp(`(?:async )?function ${name}\\(`));
  if (at < 0) throw Error(`Missing ${name}`);
  return source.slice(at, source.indexOf('\n}', at) + 2);
}

test.beforeEach(async ({ page, request }) => {
  const sources = await Promise.all(['app', 'dashboard', 'reactions', 'utils'].map(async name => {
    const response = await request.get(`/${name}.js`);
    expect(response.ok()).toBeTruthy();
    return response.text();
  }));
  const [app, dashboard, reactions, utils] = sources;
  const routeAt = app.indexOf('// ── Routing:');
  const switchAt = app.indexOf('\nfunction switchTab(', routeAt);
  const routing = app.slice(routeAt, app.indexOf('\n}\n', switchAt) + 3);
  await page.route('**/recurrence-test', route => route.fulfill({ contentType: 'text/html', body: `
    <div id="tab-dashboard" class="tab-panel"><div id="alerts"></div></div>
    <div id="tab-reactions" class="tab-panel" style="display:none">
      <div id="rxOverviewPanel" data-tabpanel="rx" data-tabkey="overview" style="display:none">Current jobs</div>
      <div data-tabpanel="rx" data-tabkey="orders">Orders</div>
      <div id="rxOrderDetailModal" style="display:none"><div id="rxOrderDetailTitle">Order</div>
      <div id="rxOrderDetailContent"></div></div></div><div id="toast"></div>` }));
  await page.goto('/recurrence-test');
  await page.addScriptTag({ content: `
    let _rxOpenOrderId = null;
    const _esc = s => String(s).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
    const _rxManufacturingSourcesHtml = () => '', _rxOrderBarHtml = () => '', _rxOrderReportBody = () => '';
    const onReactionsTabOpen = () => {}, onDashboardTabOpen = () => renderAlert(), _rxLoadOrders = async () => {}, _loadReactionsDashboard = async () => {};
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
      if (window.nextError) window.order.recurring_error = window.nextError;
      return {};
    };
    ${routing}
    ${fn(utils, 'ppSelectTab')}
    ${fn(reactions, '_rxCloseOrderDetail')}
    ${fn(reactions, '_rxViewCurrentJobs')}
    ${fn(reactions, '_rxRecurringRecoveryHtml')}
    ${fn(reactions, '_rxOpenOrderLink')}
    ${fn(reactions, '_renderRxOrderDetail')}
    ${fn(reactions, '_rxRefreshRecurringOrder')}
    ${fn(dashboard, '_renderReactionAlerts')}
    switchTab('dashboard');
    window.renderAlert = () => {
      document.getElementById('alerts').innerHTML = _renderReactionAlerts({reaction_alerts: window.order.recurring_error ? [{
        kind: 'recurring_order_blocked', order_id: 46, location: window.order.name, message: window.order.recurring_error
      }] : []});
    };
    renderAlert();
  ` });
});

test('recurrence warning opens visible order recovery controls', async ({ page }) => {
  await page.getByRole('link', { name: 'Review options' }).click();
  await expect(page).toHaveURL(/\/reactions\/order\/46$/);
  await expect(page.locator('#rxOrderDetailModal')).toBeVisible();
  await expect(page.getByRole('button', { name: 'View current jobs', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Skip this cycle', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Stop recurring', exact: true })).toBeVisible();
  await expect(page.locator('#rxOrderDetailContent')).toContainText('Keep recurrence enabled');
});

test('dashboard recovery refreshes once without issuing a second batch release', async ({ page }) => {
  await page.evaluate(() => {
    window.order.recurring_error = 'Refresh reaction jobs before assigning: capacity is stale.';
    window.clearOnRefresh = true;
    renderAlert();
  });
  await page.getByRole('button', { name: 'Refresh jobs and retry' }).click();
  await expect(page.locator('#toast')).toContainText('Job status updated');
  expect(await page.evaluate(() => window.requests)).toEqual([{method: 'POST', url: '/api/reactions/jobs/refresh'}]);
});

test('backlog leads to current jobs without retrying or changing recurrence', async ({ page }) => {
  await expect(page.getByRole('button', { name: 'Refresh jobs and retry' })).toHaveCount(0);
  await page.getByRole('link', { name: 'Review options' }).click();
  await page.getByRole('button', { name: 'View current jobs' }).click();
  await expect(page.locator('#rxOrderDetailModal')).toBeHidden();
  await expect(page.locator('#rxOverviewPanel')).toBeVisible();
  await expect(page).toHaveURL(/\/reactions$/);
  expect(await page.evaluate(() => window.requests)).toEqual([]);
  expect(await page.evaluate(() => window.order.recurring_interval_days)).toBe(7);
});

test('a refreshed backlog replaces retry with instructions and current jobs', async ({ page }) => {
  await page.evaluate(() => {
    window.nextError = window.order.recurring_error;
    window.order.recurring_error = 'Refresh reaction jobs before assigning: capacity is stale.';
    renderAlert();
  });
  await page.getByRole('button', { name: 'Refresh jobs and retry' }).click();
  await expect(page.getByRole('button', { name: 'View current jobs' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Refresh jobs and retry' })).toHaveCount(0);
  await expect(page.locator('#alerts')).toContainText('Let running jobs finish');
});

test('refresh failures stay visible and allow retry', async ({ page }) => {
  await page.evaluate(() => {
    window.order.recurring_error = 'Refresh reaction jobs before assigning: capacity is stale.';
    window.failRefresh = true;
    renderAlert();
  });
  const button = page.getByRole('button', { name: 'Refresh jobs and retry' });
  await button.click();
  await expect(page.getByRole('status')).toContainText('ESI is unavailable');
  await expect(button).toBeEnabled();
});

test('an unfinished first stage also points to existing work', async ({ page }) => {
  await page.evaluate(() => {
    window.order.recurring_error = 'Waiting for Stage 1 of the current cycle to finish.';
    renderAlert();
  });
  await expect(page.getByRole('button', { name: 'View current jobs' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Refresh jobs and retry' })).toHaveCount(0);
});
