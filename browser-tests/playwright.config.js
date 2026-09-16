const { defineConfig, devices } = require('@playwright/test');

const baseURL = process.env.BASE_URL || 'http://web:8000';

module.exports = defineConfig({
  testDir: './tests',
  outputDir: './artifacts/results',
  timeout: 30_000,
  expect: { timeout: 8_000 },
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  workers: 1,
  reporter: [
    ['list'],
    ['html', { outputFolder: './artifacts/report', open: 'never' }],
    ['json', { outputFile: './artifacts/results.json' }],
  ],
  use: {
    baseURL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  projects: [
    {
      name: 'desktop',
      grepInvert: /@protocol|@latency/,
      use: { ...devices['Desktop Chrome'] },
    },
    {
      name: 'mobile',
      grepInvert: /@protocol|@latency/,
      use: { ...devices['Pixel 7'] },
    },
    {
      name: 'latency',
      grep: /@latency/,
      use: { ...devices['Desktop Chrome'], trace: 'off', screenshot: 'off', video: 'off' },
    },
    {
      name: 'protocol',
      grep: /@protocol/,
      use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 1000 } },
    },
  ],
});
