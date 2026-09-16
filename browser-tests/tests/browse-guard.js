// Test-runner protection, NOT a server-side read-only permission or authentication bypass.
// Normal page refresh/reconciliation remains allowed; explicit user edits do not.
function installBrowseGuard() {
  window.__ppBlockedWrites = [];
  const allowedPosts = new Set([
    '/api/reactions/jobs/refresh',
    '/api/industry/refresh-stale',
    '/api/industry/queue-plan',
  ]);
  function permitted(method, input) {
    method = String(method).toUpperCase();
    if (method === 'GET' || method === 'HEAD') return true;
    const url = new URL(input, location.href);
    const allowed = method === 'POST' && url.origin === location.origin
      && allowedPosts.has(url.pathname) && !url.search;
    if (!allowed) window.__ppBlockedWrites.push('unexpected-write-blocked');
    return allowed;
  }
  const fetch = window.fetch;
  window.fetch = function(input, init) {
    const request = input instanceof Request;
    if (!permitted(init?.method || (request ? input.method : 'GET'), request ? input.url : input)) {
      return Promise.reject(new Error('Browse-only benchmark blocked a write'));
    }
    return fetch.call(this, input, init);
  };
  const open = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url, ...args) {
    if (!permitted(method, url)) throw new Error('Browse-only benchmark blocked a write');
    return open.call(this, method, url, ...args);
  };
  navigator.sendBeacon = () => {
    window.__ppBlockedWrites.push('beacon-blocked');
    return false;
  };
}

module.exports = { installBrowseGuard };
