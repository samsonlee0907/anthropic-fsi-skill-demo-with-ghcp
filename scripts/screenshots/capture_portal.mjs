// capture_portal.mjs — Playwright capture of the FSI demo portal for the README.
//
// Drives the LIVE portal exactly like a user: loads the scenario gallery, then for
// each requested scenario clicks the card, fills the default (Microsoft) prompt,
// runs the workflow, waits for the real artifact download button to appear, and
// screenshots the completed run. It also downloads each produced artifact so the
// companion Office-render step (capture_office.ps1) can turn the .xlsx / .pptx into
// images. A manifest.json records everything produced.
//
// This talks to the deployed portal + API only — no secrets, no Azure SDK. The
// portal already has the API base URL baked into its build, so simply navigating
// and clicking exercises the whole hosted-agent path end to end.
//
// Env:
//   PORTAL_URL       (required) e.g. https://ca-portal-<env>.<region>.azurecontainerapps.io
//   OUT_DIR          output dir for PNGs + manifest (default: ../../docs/images)
//   ARTIFACT_DIR     where to save downloaded .xlsx/.pptx (default: <OUT_DIR>/_artifacts)
//   SCENARIOS        comma list of scenario keys to run (default: equity-research,ib-pitch)
//   RUN              "false" to only capture the landing gallery (default: true)
//   RUN_TIMEOUT_MS   per-scenario completion timeout (default: 600000 = 10 min)
//   HEADED           "true" to watch the browser (default: headless)

import { chromium } from 'playwright';
import { mkdir, writeFile } from 'node:fs/promises';
import { createWriteStream } from 'node:fs';
import { Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const PORTAL_URL = process.env.PORTAL_URL;
if (!PORTAL_URL) {
  console.error('ERROR: PORTAL_URL env var is required.');
  process.exit(2);
}
const OUT_DIR = process.env.OUT_DIR
  ? path.resolve(process.env.OUT_DIR)
  : path.resolve(__dirname, '..', '..', 'docs', 'images');
const ARTIFACT_DIR = process.env.ARTIFACT_DIR
  ? path.resolve(process.env.ARTIFACT_DIR)
  : path.join(OUT_DIR, '_artifacts');
const SCENARIOS = (process.env.SCENARIOS ?? 'equity-research,ib-pitch')
  .split(',')
  .map((s) => s.trim())
  .filter(Boolean);
const RUN = (process.env.RUN ?? 'true').toLowerCase() !== 'false';
const RUN_TIMEOUT_MS = Number(process.env.RUN_TIMEOUT_MS ?? 600000);
const HEADED = (process.env.HEADED ?? 'false').toLowerCase() === 'true';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  await mkdir(OUT_DIR, { recursive: true });
  await mkdir(ARTIFACT_DIR, { recursive: true });

  const manifest = { portalUrl: PORTAL_URL, capturedAt: new Date().toISOString(), images: [], artifacts: [] };

  const browser = await chromium.launch({ headless: !HEADED });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1024 },
    deviceScaleFactor: 2 // retina-crisp PNGs for the README
  });
  const page = await context.newPage();

  console.log(`[portal] opening ${PORTAL_URL}`);
  await page.goto(PORTAL_URL, { waitUntil: 'networkidle', timeout: 60000 });
  // Scenario gallery is populated from GET /api/scenarios.
  await page.waitForSelector('button.scenarioCard', { timeout: 60000 });
  await sleep(1200); // let chips/skeletons settle

  const landing = path.join(OUT_DIR, 'portal-landing.png');
  await page.screenshot({ path: landing, fullPage: true });
  manifest.images.push({ name: 'portal-landing.png', kind: 'portal', caption: 'Scenario gallery — one hosted agent per FSI workflow.' });
  console.log(`[portal] saved ${landing}`);

  if (RUN) {
    for (const key of SCENARIOS) {
      try {
        await runScenario(page, key, manifest);
      } catch (err) {
        console.error(`[portal] scenario '${key}' failed: ${err?.message ?? err}`);
      }
    }
  }

  await writeFile(path.join(OUT_DIR, 'manifest.json'), JSON.stringify(manifest, null, 2));
  await browser.close();
  console.log(`[portal] done — ${manifest.images.length} image(s), ${manifest.artifacts.length} artifact(s).`);
}

async function runScenario(page, key, manifest) {
  console.log(`[portal] === scenario ${key} ===`);
  // Select the card whose data flows from the scenario key. Cards render the title;
  // match on the key's known title, else fall back to nth card.
  const card = page.locator('button.scenarioCard', { hasText: titleHint(key) }).first();
  if ((await card.count()) === 0) {
    throw new Error(`no scenario card matched '${key}'`);
  }
  await card.click();
  await sleep(600);

  // Fill the default (Microsoft) prompt via the preset button, then run.
  const preset = page.locator('button.presetButton').first();
  if ((await preset.count()) > 0) {
    await preset.click();
    await sleep(300);
  }
  await page.locator('button.primaryButton').first().click();
  console.log(`[portal] run submitted for ${key}; waiting up to ${Math.round(RUN_TIMEOUT_MS / 1000)}s`);

  // Completion = a real artifact download button appears (a.artifactChip), or the
  // agent card reaches the 'complete' state. Poll so we can also fail fast on error.
  const deadline = Date.now() + RUN_TIMEOUT_MS;
  let completed = false;
  while (Date.now() < deadline) {
    const chips = await page.locator('a.artifactChip').count();
    const complete = await page.locator('article.agentCard.complete').count();
    const errored = await page.locator('article.agentCard.error').count();
    if (chips > 0 || complete > 0) {
      completed = true;
      break;
    }
    if (errored > 0) throw new Error('agent card entered error state');
    await sleep(4000);
  }
  if (!completed) throw new Error('timed out waiting for completion');

  await sleep(1500); // let the final markdown + chips paint
  // Scroll the completed agent card into view and screenshot the full page.
  await page.locator('article.agentCard').first().scrollIntoViewIfNeeded();
  await sleep(500);
  const runImg = path.join(OUT_DIR, `portal-run-${key}.png`);
  await page.screenshot({ path: runImg, fullPage: true });
  manifest.images.push({ name: `portal-run-${key}.png`, kind: 'portal', scenario: key, caption: `${titleHint(key)} — live activity feed, narrative, and artifact download button.` });
  console.log(`[portal] saved ${runImg}`);

  // Download every artifact this run produced.
  const chipLocs = page.locator('a.artifactChip');
  const n = await chipLocs.count();
  for (let i = 0; i < n; i++) {
    const href = await chipLocs.nth(i).getAttribute('href');
    let filename = (await chipLocs.nth(i).innerText()).trim().replace(/^▣\s*/, '').trim();
    if (!href) continue;
    const url = new URL(href, PORTAL_URL).toString();
    if (!filename) filename = `artifact-${key}-${i}`;
    const dest = path.join(ARTIFACT_DIR, filename);
    try {
      const resp = await page.request.get(url, { timeout: 120000 });
      if (!resp.ok()) throw new Error(`HTTP ${resp.status()}`);
      const buf = await resp.body();
      await pipeline(Readable.from(buf), createWriteStream(dest));
      manifest.artifacts.push({ file: path.relative(OUT_DIR, dest).split(path.sep).join('/'), filename, scenario: key });
      console.log(`[portal] downloaded artifact ${filename} (${buf.length} bytes)`);
    } catch (err) {
      console.error(`[portal] artifact download failed for ${filename}: ${err?.message ?? err}`);
    }
  }
}

// Human-readable hint used to match the scenario card by its visible title.
function titleHint(key) {
  switch (key) {
    case 'equity-research':
      return 'Equity Research';
    case 'ib-pitch':
      return 'Investment Banking';
    case 'pe-lbo':
      return 'LBO';
    default:
      return key;
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
