const assert = require('node:assert/strict');
const vm = require('node:vm');
const { installBrowseGuard } = require('../browser-tests/tests/browse-guard');

(async () => {
  const calls = [];
  class XHR { open(...args) { calls.push(args); } }
  const context = { URL, Request, Set, Error, Promise, XMLHttpRequest: XHR,
    location: { href: 'https://example.test/manufacturing', origin: 'https://example.test' },
    navigator: {}, fetch: (...args) => { calls.push(args); return Promise.resolve('ok'); } };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(`(${installBrowseGuard.toString()})()`, context);
  assert.equal(await context.fetch('/api/industry/orders'), 'ok');
  for (const url of ['/api/industry/queue-plan', '/api/industry/refresh-stale', '/api/reactions/jobs/refresh']) {
    assert.equal(await context.fetch(url, { method: 'POST' }), 'ok');
  }
  for (const [url, method] of [
    ['/api/industry/orders', 'POST'], ['/api/industry/orders/1', 'DELETE'],
    ['/api/industry/settings', 'PATCH'], ['/api/reactions/assign', 'POST'],
    ['/api/reactions/jobs/refresh?force=1', 'POST'],
    ['https://other.test/api/industry/queue-plan', 'POST'],
  ]) await assert.rejects(context.fetch(url, { method }));
  await assert.rejects(context.fetch(new Request('https://example.test/api/me', { method: 'DELETE' })));
  assert.throws(() => new XHR().open('POST', '/api/industry/orders'));
  assert.equal(context.navigator.sendBeacon('/api/me', 'data'), false);
  assert.equal(calls.length, 4, 'blocked writes never reach the transport');
  assert.equal(context.__ppBlockedWrites.length, 9);
  console.log('Browse guard: reads/page-load POSTs allowed; edits, force refresh and beacons blocked');
})().catch(error => { console.error(error); process.exitCode = 1; });
