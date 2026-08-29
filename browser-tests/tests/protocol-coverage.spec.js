const fs = require('node:fs');
const path = require('node:path');
const { test, expect } = require('@playwright/test');

test('@protocol acceptance result sheet has an automated test for every documented case', async () => {
  const protocol = fs.readFileSync(
    path.join(__dirname, '..', 'docs', 'production-phases-test-protocol.md'),
    'utf8',
  );
  const resultSheet = protocol.match(/## Result sheet[\s\S]*?(?=\n## |$)/)?.[0] || '';
  const expectedIds = [...resultSheet.matchAll(/^\| (S1|P\d-[RM]?\d+b?) \|/gm)].map((m) => m[1]);

  const testSources = fs.readdirSync(__dirname)
    .filter((name) => name.startsWith('protocol-') && name.endsWith('.spec.js') && name !== path.basename(__filename))
    .map((name) => fs.readFileSync(path.join(__dirname, name), 'utf8'))
    .join('\n');

  expect(expectedIds, 'the documentation result sheet should contain acceptance cases').not.toEqual([]);
  for (const id of expectedIds) {
    expect(testSources, `${id} is documented but has no automated Playwright case`).toMatch(
      new RegExp(`test\\(['\"]${id}(?:\\s|:)`),
    );
  }
});
