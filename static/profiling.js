// Initial browser navigation only. SPA/API work is measured by server request profiles.
const _initialProfilePage = currentTab();
let _pageProfilePending = false;
let _pageProfileSent = false;

function _pageProfilingAllowed() {
  return _features.page_profiling?.state === 'public' && navigator.doNotTrack !== '1'
    && !navigator.globalPrivacyControl && !navigator.webdriver
    && typeof navigator.sendBeacon === 'function';
}

function profileInitialPageLoad() {
  if (_pageProfileSent || _pageProfilePending || !_initialProfilePage || !_pageProfilingAllowed()) return;
  const send = () => {
    _pageProfilePending = false;
    if (_pageProfileSent || !_pageProfilingAllowed()) return;
    const nav = performance.getEntriesByType('navigation')[0];
    if (!nav || nav.loadEventEnd <= 0 || performance.now() - nav.loadEventEnd > 60000) return;
    const payload = { page: _initialProfilePage, load_ms: nav.loadEventEnd - nav.startTime,
      dom_ms: nav.domContentLoadedEventEnd - nav.startTime,
      ttfb_ms: nav.responseStart - nav.requestStart };
    if (Object.values(payload).some(value => typeof value === 'number' && (!Number.isFinite(value) || value < 0 || value > 300000))) return;
    try {
      _pageProfileSent = navigator.sendBeacon('/api/telemetry/page-load',
        new Blob([JSON.stringify(payload)], { type: 'application/json' }));
    } catch (e) { /* Optional telemetry never changes the page or retries navigation. */ }
  };
  _pageProfilePending = true;
  if (document.readyState === 'complete') setTimeout(send, 0);
  else window.addEventListener('load', () => setTimeout(send, 0), { once: true });
}
