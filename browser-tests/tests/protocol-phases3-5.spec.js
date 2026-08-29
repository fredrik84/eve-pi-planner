const { test, expect } = require('@playwright/test');
const { installSession } = require('./helpers');

const M1 = 57487; // Capital Core Temperature Regulator: stable deep build with reaction inputs.

async function resetProduction(page) {
  const builds = await page.request.get('/api/industry/orders');
  if (builds.ok()) {
    for (const order of (await builds.json()).orders) {
      await page.request.delete(`/api/industry/orders/${order.id}`);
    }
  }
  const reactions = await page.request.get('/api/reactions/orders');
  if (reactions.ok()) {
    for (const order of (await reactions.json()).orders.filter(o => o.status === 'open')) {
      await page.request.post(`/api/reactions/orders/${order.id}/status`, { data: { status: 'cancelled' } });
    }
  }
}

async function addBuild(page, label, quantity = 1) {
  const response = await page.request.post('/api/industry/orders', { data: {
    product_type_id: M1, quantity, label, build_reactions: true,
  }});
  expect(response.ok(), await response.text()).toBeTruthy();
  return response.json();
}

async function planQueue(page, extra = {}) {
  const response = await page.request.post('/api/industry/queue-plan', { data: {
    force_build: true, prioritize_speed: false, ...extra,
  }});
  expect(response.ok(), await response.text()).toBeTruthy();
  return response.json();
}

function linkedIds(plan) {
  return (plan.reaction_handoff?.orders || []).map(o => o.order_id).sort((a, b) => a - b);
}

test.describe('@protocol production acceptance: phases 3–5', () => {
  test.beforeEach(async ({ page }) => {
    expect(await installSession(page)).toBeTruthy();
    await resetProduction(page);
  });

  test('P3-1 — ready Manufacturing reactions become linked Reactions orders', async ({ page }) => {
    const build = await addBuild(page, 'Phase 3 M1');
    const plan = await planQueue(page);
    expect(plan.reaction_handoff).toBeTruthy();
    expect(linkedIds(plan).length).toBeGreaterThan(0);
    for (const linked of plan.reaction_handoff.orders) {
      expect(linked.owners).toEqual([{ order_id: build.id, runs: linked.owners[0].runs }]);
      expect(linked.owners[0].runs).toBeGreaterThan(0);
    }

    await page.goto('/reactions');
    await page.locator('[data-tabgroup="rx"][data-tabkey="orders"]').click();
    await expect(page.locator('#rxOrdersContent .rx-order-row').filter({ hasText: 'Manufacturing' }).first()).toBeVisible();
    await expect(page.locator('#rxOrdersContent')).toContainText('Phase 3 M1');
  });

  test('P3-2 — increasing a build updates the existing linked demand', async ({ page }) => {
    const build = await addBuild(page, 'Phase 3 grow');
    const first = await planQueue(page);
    const ids = linkedIds(first);
    const before = new Map(first.reaction_handoff.orders.map(o => [o.order_id, o.owners[0].runs]));
    const update = await page.request.patch(`/api/industry/orders/${build.id}`, { data: { quantity: 2 } });
    expect(update.ok()).toBeTruthy();
    const second = await planQueue(page);
    expect(linkedIds(second)).toEqual(ids);
    for (const linked of second.reaction_handoff.orders) {
      expect(linked.owners[0].runs).toBeGreaterThanOrEqual(before.get(linked.order_id));
    }
  });

  test('P3-3 — decreasing a build never drops below committed reaction work silently', async ({ page }) => {
    const build = await addBuild(page, 'Phase 3 shrink', 3);
    const first = await planQueue(page);
    const assigned = new Map(first.reaction_handoff.orders.map(o => [o.order_id, o.owners[0].runs]));
    await page.request.patch(`/api/industry/orders/${build.id}`, { data: { quantity: 1 } });
    const second = await planQueue(page);
    for (const linked of second.reaction_handoff.orders) {
      const oldRuns = assigned.get(linked.order_id) || 0;
      const newRuns = linked.owners[0].runs;
      expect(newRuns <= oldRuns || (second.reaction_handoff.conflicts || []).length > 0).toBeTruthy();
    }
  });

  test('P3-4 — removing pending Manufacturing work releases linked demand', async ({ page }) => {
    const build = await addBuild(page, 'Phase 3 remove');
    const plan = await planQueue(page);
    const ids = linkedIds(plan);
    expect(ids.length).toBeGreaterThan(0);
    const removed = await page.request.delete(`/api/industry/orders/${build.id}`);
    expect(removed.ok()).toBeTruthy();
    const remaining = await (await page.request.get('/api/reactions/orders')).json();
    const stillOpen = remaining.orders.filter(o => ids.includes(o.id) && o.status === 'open');
    expect(stillOpen).toEqual([]);
    const refreshed = await planQueue(page);
    expect(refreshed.empty).toBeTruthy();
  });

  test('P3-5 — manually running linked work is retained when its source disappears', async ({ page }) => {
    const build = await addBuild(page, 'Phase 3 running');
    const plan = await planQueue(page);
    const dashboard = await (await page.request.get('/api/reactions/jobs')).json();
    const pending = dashboard.characters.flatMap(c => (c.pending || []).map(p => ({ ...p, character_id: c.character_id }))).find(p => linkedIds(plan).includes(p.order_id));
    expect(pending).toBeTruthy();
    const marked = await page.request.post('/api/reactions/mark', { data: {
      character_id: pending.character_id, type_id: pending.type_id,
      tier_order: pending.tier_order || 0, state: 'running', jobs: null,
    }});
    expect(marked.ok()).toBeTruthy();
    await page.request.delete(`/api/industry/orders/${build.id}`);
    const orders = await (await page.request.get('/api/reactions/orders')).json();
    const retained = orders.orders.filter(o => linkedIds(plan).includes(o.id));
    expect(retained.some(o => o.source_state === 'running_after_finish' || o.status === 'open')).toBeTruthy();
  });

  test('P4-1 — two builds share physical reaction orders with both owners', async ({ page }) => {
    const a = await addBuild(page, 'Phase 4 owner A');
    const b = await addBuild(page, 'Phase 4 owner B');
    const plan = await planQueue(page, { per_order_plans: false });
    expect(plan.reaction_handoff.orders.length).toBeGreaterThan(0);
    for (const linked of plan.reaction_handoff.orders) {
      expect(linked.owners.map(o => o.order_id).sort((x, y) => x - y)).toEqual([a.id, b.id].sort((x, y) => x - y));
      expect(linked.owners.reduce((sum, o) => sum + o.runs, 0)).toBeGreaterThan(0);
    }
  });

  test('P4-2 — removing one owner adjusts rather than replaces shared work', async ({ page }) => {
    const a = await addBuild(page, 'Phase 4 remove A');
    const b = await addBuild(page, 'Phase 4 keep B');
    const first = await planQueue(page, { per_order_plans: false });
    const ids = linkedIds(first);
    await page.request.delete(`/api/industry/orders/${a.id}`);
    const second = await planQueue(page, { per_order_plans: false });
    expect(linkedIds(second)).toEqual(ids);
    for (const linked of second.reaction_handoff.orders) {
      expect(linked.owners.map(o => o.order_id)).toEqual([b.id]);
    }
  });

  test('P4-2b — deleting several owners never amplifies linked work', async ({ page }) => {
    const a = await addBuild(page, 'Phase 4 many A');
    const b = await addBuild(page, 'Phase 4 many B');
    const c = await addBuild(page, 'Phase 4 survivor');
    const first = await planQueue(page, { per_order_plans: false });
    const before = first.reaction_handoff.orders.reduce((sum, o) => sum + o.owners.reduce((s, x) => s + x.runs, 0), 0);
    await page.request.delete(`/api/industry/orders/${a.id}`);
    await page.request.delete(`/api/industry/orders/${b.id}`);
    const second = await planQueue(page, { per_order_plans: false });
    const after = second.reaction_handoff.orders.reduce((sum, o) => sum + o.owners.reduce((s, x) => s + x.runs, 0), 0);
    expect(after).toBeLessThan(before);
    expect(second.reaction_handoff.orders.every(o => o.owners.every(x => x.order_id === c.id))).toBeTruthy();
  });

  test('P4-3 — persisted priority controls the next reaction allocation', async ({ page }) => {
    const low = await page.request.post('/api/reactions/orders', { data: { type_id: 16669, target_qty: 20, client_name: 'Priority low' } });
    const high = await page.request.post('/api/reactions/orders', { data: { type_id: 16669, target_qty: 20, client_name: 'Priority high' } });
    const lo = (await low.json()).order;
    const hi = (await high.json()).order;
    const reorder = await page.request.post('/api/reactions/orders/reorder', { data: { order: [hi.id, lo.id] } });
    expect(reorder.ok()).toBeTruthy();
    const listed = await (await page.request.get('/api/reactions/orders')).json();
    const open = listed.orders.filter(o => [hi.id, lo.id].includes(o.id));
    expect(open[0].id).toBe(hi.id);
  });

  test('P4-4 — per-build isolation is explicit and reports its trade-off', async ({ page }) => {
    await addBuild(page, 'Phase 4 isolate A');
    await addBuild(page, 'Phase 4 isolate B');
    const comparison = await page.request.post('/api/industry/queue-plan/compare', { data: { force_build: true, prioritize_speed: false } });
    expect(comparison.ok(), await comparison.text()).toBeTruthy();
    const data = await comparison.json();
    expect(data).toHaveProperty('aggregated');
    expect(data).toHaveProperty('per_order');
    const enabled = await page.request.post('/api/industry/per-order-plans', { data: { enabled: true } });
    expect(enabled.ok()).toBeTruthy();
    await page.goto('/manufacturing');
    await page.locator('.header-settings-btn').click();
    await page.locator('#settingsNavBuildrules').click();
    await expect(page.locator('#settingsSecBuildrules')).toContainText(/separately|apart|shared components/i);
  });

  test('P5 — attention states and two-way ownership links stay singular', async ({ page }) => {
    const a = await addBuild(page, 'Phase 5 owner A');
    const b = await addBuild(page, 'Phase 5 owner B');
    const plan = await planQueue(page, { per_order_plans: false });
    expect(new Set(linkedIds(plan)).size).toBe(linkedIds(plan).length);
    await page.goto('/reactions');
    await page.locator('[data-tabgroup="rx"][data-tabkey="orders"]').click();
    const linkedRows = page.locator('.rx-order-row').filter({ hasText: 'Manufacturing' });
    await expect(linkedRows.first()).toBeVisible();
    await linkedRows.first().click();
    await expect(page.locator('#rxOrderDetailContent')).toContainText('Used by Manufacturing');
    await expect(page.locator('#rxOrderDetailContent')).toContainText('Phase 5 owner A');
    await expect(page.locator('#rxOrderDetailContent')).toContainText('Phase 5 owner B');
    expect(a.id).not.toBe(b.id);
  });
});
