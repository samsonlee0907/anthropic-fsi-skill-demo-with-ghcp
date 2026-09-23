import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { SourceCitation } from '../app/components/SourceCitation';

test('source update and crawl dates are never labeled publication dates', () => {
  const html = renderToStaticMarkup(createElement(SourceCitation, { source: {
    id: 'webiq-1', title: 'Synthetic news', url: 'https://example.com/news',
    published_at: null, updated_at: '2026-09-16', crawled_at: '2026-09-17'
  } }));
  assert.match(html, /Publication date unavailable/);
  assert.match(html, /Updated: 2026-09-16/);
  assert.match(html, /Crawled: 2026-09-17/);
  assert.ok(!html.includes('Published:'));
  assert.match(html, /href="https:\/\/example.com\/news"/);
  assert.match(html, /target="_blank"/);
});

test('source without timestamps does not invent a date', () => {
  const html = renderToStaticMarkup(createElement(SourceCitation, { source: {
    id: 'webiq-1', title: 'Synthetic news', url: 'https://example.com/news', published_at: null
  } }));
  assert.match(html, /Publication date unavailable/);
  assert.ok(!html.includes('Updated:'));
  assert.ok(!html.includes('Crawled:'));
});
