import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { consumeSseStream, type RunEvent } from '../app/lib/sse';
import { nextRunStatus, suggestedNewsQuery, webiqHeaders } from '../app/lib/run-state';
import { fetchArtifact } from '../app/lib/artifacts';

const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });

function stream(text: string, splitBytes = false) {
  const bytes = new TextEncoder().encode(text);
  const chunks = splitBytes ? Array.from(bytes, (byte) => new Uint8Array([byte])) : [bytes];
  globalThis.fetch = async () => new Response(new ReadableStream<Uint8Array>({
    start(controller) {
      chunks.forEach((chunk) => controller.enqueue(chunk));
      controller.close();
    }
  }), { headers: { 'Content-Type': 'text/event-stream' } });
}

test('SSE handles UTF-8, CR/LF chunk boundaries, comments and multiline data', async () => {
  for (const newline of ['\n', '\r\n', '\r']) {
    stream([
      ': keepalive', '', 'data: {"type":"delta",', 'data: "agent":"test","text":"caf\u00e9"}',
      '', 'data: {"type":"done"}', '', ''
    ].join(newline), true);
    const events: RunEvent[] = [];
    await consumeSseStream<RunEvent>('https://example.com/api/run', {}, (event) => events.push(event));
    assert.deepEqual(events, [{ type: 'delta', agent: 'test', text: 'caf\u00e9' }, { type: 'done' }]);
  }
});

test('premature EOF and incomplete terminal frame are errors', async () => {
  for (const body of ['', 'data: {"type":"status","stage":"working","scenario":"test"}\n\n', 'data: {"type":"done"}']) {
    stream(body);
    await assert.rejects(consumeSseStream('https://example.com', {}, () => {}), /before completion/);
  }
});

test('malformed events and HTTP error bodies never echo submitted secrets', async () => {
  stream('data: SECRET-API-KEY\n\n');
  await assert.rejects(consumeSseStream('https://example.com', {}, () => {}), (error: Error) =>
    /invalid event/.test(error.message) && !error.message.includes('SECRET'));
  globalThis.fetch = async () => new Response('SECRET-API-KEY', { status: 422 });
  await assert.rejects(consumeSseStream('https://example.com', {}, () => {}), (error: Error) =>
    error.message.includes('422') && !error.message.includes('SECRET'));
});

test('callback failure is not misreported as a JSON error', async () => {
  stream('data: {"type":"done"}\n\n');
  await assert.rejects(consumeSseStream('https://example.com', {}, () => {
    throw new Error('callback failed');
  }), /callback failed/);
});

test('error and partial outcomes survive done events without an outcome', () => {
  let status = nextRunStatus('running', { type: 'error', message: 'failed' });
  status = nextRunStatus(status, { type: 'done' });
  assert.equal(status, 'error');
  assert.equal(nextRunStatus('running', { type: 'done', outcome: 'error' }), 'error');
  assert.equal(nextRunStatus('partial', { type: 'done', outcome: 'complete' }), 'partial');
  assert.equal(nextRunStatus('running', { type: 'done' }), 'complete');
  assert.equal(nextRunStatus('running', {
    type: 'artifact', agent: 'test', id: 'id', filename: 'summary.xlsx', kind: 'summary'
  }), 'partial');
});

test('missing key sends no WebIQ header and HTTPS is required when configured', () => {
  assert.deepEqual(webiqHeaders('  ', 'http://localhost:8000', 'http://localhost'), {});
  assert.deepEqual(webiqHeaders('a-secret', 'https://api.example.com', 'https://portal.example.com'), {
    'X-WebIQ-Key': 'a-secret'
  });
  assert.throws(() => webiqHeaders('a-secret', 'http://api.example.com', 'https://portal.example.com'), /HTTPS/);
  assert.throws(() => webiqHeaders('a-secret\nanother', '', 'https://portal.example.com'), (error: Error) =>
    /invalid/.test(error.message) && !error.message.includes('a-secret'));
});

test('news query follows actual prompt ticker rather than a hardcoded company', () => {
  assert.equal(suggestedNewsQuery('Value NVIDIA Corporation (ticker NVDA).'), 'NVDA recent company news');
  assert.equal(suggestedNewsQuery('Pitch for Apple Inc. (ticker AAPL).'), 'AAPL recent company news');
  assert.equal(suggestedNewsQuery('A custom mandate without an explicit ticker'), '');
});

test('downloads surface retryable storage failure without raw response content', async () => {
  globalThis.fetch = async () => new Response('provider-secret', { status: 503 });
  await assert.rejects(fetchArtifact('https://api.example.com', '/api/artifacts/id'), (error: Error) =>
    /temporarily unavailable/.test(error.message) && !error.message.includes('provider-secret'));
});

test('download requests never carry WebIQ credentials', async () => {
  globalThis.fetch = async (input, init) => {
    assert.equal(input, 'https://api.example.com/api/artifacts/id');
    assert.deepEqual(init, { cache: 'no-store' });
    return new Response('PKfile');
  };
  assert.equal(await (await fetchArtifact('https://api.example.com', '/api/artifacts/id')).text(), 'PKfile');
  await assert.rejects(fetchArtifact('', 'https://other.example.com/file'), /invalid download address/);
});
