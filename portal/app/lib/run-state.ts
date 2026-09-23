import type { RunEvent } from './sse';

export type OverallStatus = 'idle' | 'running' | 'complete' | 'partial' | 'error';

export function nextRunStatus(current: OverallStatus, event: RunEvent): OverallStatus {
  if (current === 'error' || event.type === 'error') return 'error';
  if (event.type === 'done') {
    if (event.outcome === 'error') return 'error';
    return current === 'partial' || event.outcome === 'partial' ? 'partial' : 'complete';
  }
  if (event.type === 'warning' ||
      (event.type === 'enrichment' && event.status !== 'ready') ||
      (event.type === 'artifact' && (!event.id || event.kind === 'summary'))) {
    return 'partial';
  }
  if (event.type === 'status' && current !== 'partial') return 'running';
  return current;
}

export function webiqHeaders(key: string, apiBase: string, pageUrl: string): Record<string, string> {
  const value = key.trim();
  if (!value) return {};
  if (new URL(`${apiBase}/api/run`, pageUrl).protocol !== 'https:') {
    throw new Error('WebIQ requires an HTTPS backend. Clear the key to run without WebIQ.');
  }
  if (value.length > 4096 || !/^[\x21-\x7e]+$/.test(value)) {
    throw new Error('The WebIQ key format is invalid. Clear or replace the key.');
  }
  return { 'X-WebIQ-Key': value };
}

export function suggestedNewsQuery(prompt: string): string {
  const ticker = prompt.match(/\bticker\s*[:=]?\s+([a-z][a-z0-9.-]{0,14})\b/i)?.[1];
  return ticker ? `${ticker.toUpperCase()} recent company news` : '';
}
