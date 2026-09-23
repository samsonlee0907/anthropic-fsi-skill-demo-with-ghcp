import { expect, test, type Page, type Request, type Route } from '@playwright/test';

const API = 'https://api.example.test';

const scenarios = [
  { key: 'equity', title: 'Equity Research & Valuation', tagline: 'DCF + comps', toolbox: 'tb-equity-research',
    skills: ['fsi-dcf-model', 'fsi-comps-analysis'], default_prompt: 'Value Microsoft (MSFT).', filename: 'MSFT_DCF.xlsx' },
  { key: 'ib-pitch', title: 'IB Pitch / Deal Prep', tagline: 'Pitch deck', toolbox: 'tb-ib-pitch',
    skills: ['fsi-pptx-author'], default_prompt: 'Build a pitch deck for Microsoft (MSFT).', filename: 'MSFT_Pitch.pptx' },
  { key: 'pe-lbo', title: 'PE LBO Screening', tagline: 'LBO screen', toolbox: 'tb-pe-lbo',
    skills: ['fsi-lbo-model'], default_prompt: 'Screen an LBO of Microsoft (MSFT).', filename: 'MSFT_LBO.xlsx' }
];

type RunCapture = { headers: Record<string, string>; body: Record<string, unknown> };

function sse(events: object[]): string {
  return events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join('');
}

function cors(_route: Route, extra: Record<string, string> = {}) {
  return {
    'access-control-allow-origin': '*',
    'access-control-allow-headers': 'content-type, x-webiq-key, accept',
    'access-control-allow-methods': 'GET, POST, OPTIONS',
    ...extra
  };
}

async function mockApi(page: Page, options: { enrichmentFails?: boolean } = {}) {
  const runs: RunCapture[] = [];
  const otherRequests: Request[] = [];

  await page.route(`${API}/**`, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === 'OPTIONS') {
      return route.fulfill({ status: 204, headers: cors(route) });
    }
    if (url.pathname === '/api/scenarios') {
      return route.fulfill({ headers: cors(route), json: { scenarios: scenarios.map(({ filename, ...rest }) => rest) } });
    }
    if (url.pathname === '/api/toolboxes') {
      return route.fulfill({ headers: cors(route), json: { toolboxes: scenarios.map((s) => ({ name: s.toolbox, description: s.title, tools: ['web', 'sec-edgar'] })) } });
    }
    if (url.pathname === '/api/health') {
      return route.fulfill({ headers: cors(route), json: { status: 'ok', environment_name: 'fsi-test', model_deployment_name: 'gpt-6-astra' } });
    }
    if (url.pathname === '/api/run') {
      const body = request.postDataJSON() as Record<string, unknown>;
      runs.push({ headers: request.headers(), body });
      const scenario = scenarios.find((s) => s.key === body.scenario) ?? scenarios[0];
      const agent = `fsi-${scenario.key}`;
      const usingWebiq = Boolean(request.headers()['x-webiq-key']);
      const events: object[] = [{ type: 'status', stage: 'start', scenario: scenario.key, title: scenario.title, toolbox: scenario.toolbox }];
      if (usingWebiq && options.enrichmentFails) {
        // Mirrors api/app/webiq.py: a failed search stops before any agent run starts.
        const message = 'WebIQ rejected the API key. Replace it or run without WebIQ.';
        return route.fulfill({ headers: cors(route, { 'content-type': 'text/event-stream' }), body: sse([
          { type: 'status', stage: 'webiq_search', scenario: scenario.key },
          { type: 'enrichment', provider: 'webiq', status: 'failed', message, sources: [] },
          { type: 'error', message },
          { type: 'done', outcome: 'error' }
        ]) });
      }
      if (usingWebiq) {
        events.push({ type: 'enrichment', provider: 'webiq', status: 'ready', message: 'WebIQ returned 1 source.', sources: [
            { id: 's1', title: 'Safe source', url: 'https://news.example.test/a', published_at: null },
            { id: 's2', title: 'Unsafe source', url: 'javascript:alert(1)', published_at: null }
        ] });
      }
      events.push(
        { type: 'agent_start', agent, role: 'scenario', label: scenario.title },
        { type: 'final', agent, text: `## ${scenario.title}\nDemo narrative.` },
        { type: 'artifact', agent, id: `art${scenario.key.replace('-', '')}`, filename: scenario.filename, url: `/api/artifacts/art${scenario.key.replace('-', '')}`, kind: 'generated' },
        { type: 'agent_end', agent },
        { type: 'done' }
      );
      return route.fulfill({ headers: cors(route, { 'content-type': 'text/event-stream' }), body: sse(events) });
    }
    if (url.pathname.startsWith('/api/artifacts/')) {
      otherRequests.push(request);
      return route.fulfill({ headers: cors(route, { 'content-type': 'application/octet-stream' }), body: Buffer.from('PK\u0003\u0004demo') });
    }
    otherRequests.push(request);
    return route.fulfill({ status: 404, headers: cors(route), body: '' });
  });

  return { runs, otherRequests };
}

async function openScenario(page: Page, title: string) {
  await page.getByRole('button', { name: new RegExp(title.replace(/[/&]/g, '.')) }).first().click();
  await expect(page.locator('#workflow-prompt')).toBeVisible();
}

async function runAndDownload(page: Page, filename: string) {
  await page.getByRole('button', { name: /Run scenario workflow|Start a new run/ }).click();
  await expect(page.locator('.runBadge')).toHaveText('Complete');
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('button', { name: `Download: ${filename}` }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe(filename);
}

test('each scenario reaches Complete and downloads its artifact without WebIQ', async ({ page }) => {
  const api = await mockApi(page);
  await page.goto('/');
  await expect(page.getByText('gpt-6-astra')).toBeVisible();

  for (const scenario of scenarios) {
    await openScenario(page, scenario.title);
    await runAndDownload(page, scenario.filename);
  }

  expect(api.runs).toHaveLength(3);
  for (const run of api.runs) {
    expect(run.headers['x-webiq-key']).toBeUndefined();
    expect(run.body.webiq_query).toBeUndefined();
  }
});

test('a WebIQ key alone never searches; an explicit query sends the key only as a header', async ({ page }) => {
  const api = await mockApi(page);
  await page.goto('/');
  await openScenario(page, scenarios[0].title);

  await page.getByText('Optional WebIQ news enrichment').click();
  await page.locator('#webiq-key').fill('secret-test-key');
  await expect(page.locator('#webiq-key')).toHaveAttribute('type', 'password');
  await runAndDownload(page, scenarios[0].filename);
  expect(api.runs[0].headers['x-webiq-key']).toBeUndefined();
  expect(api.runs[0].body.webiq_query).toBeUndefined();

  await page.locator('#webiq-query').fill('Microsoft MSFT recent news');
  await runAndDownload(page, scenarios[0].filename);
  expect(api.runs[1].headers['x-webiq-key']).toBe('secret-test-key');
  expect(api.runs[1].body.webiq_query).toBe('Microsoft MSFT recent news');
  expect(JSON.stringify(api.runs[1].body)).not.toContain('secret-test-key');

  await page.getByText(/WebIQ sources retrieved/).click();
  await expect(page.getByRole('link', { name: /Safe source/ })).toHaveAttribute('href', 'https://news.example.test/a');
  await expect(page.getByRole('link', { name: /Unsafe source/ })).toHaveCount(0);

  for (const request of api.otherRequests) {
    expect(request.headers()['x-webiq-key']).toBeUndefined();
    expect(request.url()).not.toContain('secret-test-key');
  }
  const stored = await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage }, cookie: document.cookie, url: location.href }));
  expect(stored).not.toContain('secret-test-key');
});

test('a failed WebIQ search starts no agent run and can be rerun without WebIQ', async ({ page }) => {
  const api = await mockApi(page, { enrichmentFails: true });
  await page.goto('/');
  await openScenario(page, scenarios[1].title);

  await page.getByText('Optional WebIQ news enrichment').click();
  await page.locator('#webiq-key').fill('bad-key');
  await page.locator('#webiq-query').fill('Microsoft MSFT news');
  await page.getByRole('button', { name: 'Run scenario workflow' }).click();
  await expect(page.locator('.runBadge')).toHaveText('Attention needed');
  await expect(page.getByRole('alert').filter({ hasText: 'WebIQ rejected the API key' })).toBeVisible();
  expect(api.runs[0].headers['x-webiq-key']).toBe('bad-key');

  await page.getByRole('button', { name: 'Clear key and run without WebIQ' }).click();
  await expect(page.locator('.runBadge')).toHaveText('Complete');
  expect(api.runs).toHaveLength(2);
  expect(api.runs[1].headers['x-webiq-key']).toBeUndefined();
  expect(api.runs[1].body.webiq_query).toBeUndefined();
  await expect(page.locator('#webiq-key')).toHaveValue('');
});
