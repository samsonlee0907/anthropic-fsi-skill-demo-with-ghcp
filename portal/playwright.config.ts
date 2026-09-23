import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  timeout: 45000,
  use: { baseURL: 'http://127.0.0.1:3217', browserName: 'chromium', screenshot: 'off', video: 'off', trace: 'off' },
  webServer: {
    command: 'npm run dev -- --hostname 127.0.0.1 --port 3217',
    url: 'http://127.0.0.1:3217',
    reuseExistingServer: false,
    timeout: 360000,
    stdout: 'pipe',
    stderr: 'pipe',
    env: { NEXT_PUBLIC_API_BASE_URL: 'https://api.example.test', NEXT_TELEMETRY_DISABLED: '1' }
  }
});
