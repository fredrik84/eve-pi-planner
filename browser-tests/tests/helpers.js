const { expect } = require('@playwright/test');

async function installSession(page) {
  const token = process.env.PP_SESSION;
  if (!token) return false;
  const url = new URL(process.env.BASE_URL || 'http://web:8000');
  await page.context().addCookies([{
    name: 'pp_session',
    value: token,
    domain: url.hostname,
    path: '/',
    httpOnly: true,
    sameSite: 'Lax',
    secure: url.protocol === 'https:',
  }]);
  return true;
}

function watchBrowser(page) {
  const failures = [];
  page.on('pageerror', error => failures.push(`pageerror: ${error.message}`));
  page.on('console', message => {
    if (message.type() === 'error') failures.push(`console: ${message.text()}`);
  });
  page.on('requestfailed', request => {
    const failure = request.failure();
    failures.push(`request: ${request.url()} (${failure?.errorText || 'failed'})`);
  });
  return () => expect(failures, failures.join('\n')).toEqual([]);
}

module.exports = { installSession, watchBrowser };
