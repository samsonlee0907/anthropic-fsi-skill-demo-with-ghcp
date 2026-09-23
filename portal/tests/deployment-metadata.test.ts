import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { DeploymentMetadata } from '../app/components/DeploymentMetadata';

test('deployment labels display backend values without a hardcoded model', () => {
  const html = renderToStaticMarkup(createElement(DeploymentMetadata, {
    environmentName: 'fsi-comparison-env', modelDeploymentName: 'custom-model-deployment'
  }));
  assert.match(html, /fsi-comparison-env/);
  assert.match(html, /custom-model-deployment/);
  assert.match(html, /Model deployment/);
  assert.match(html, /not a model-readiness check/);
});

test('older and unconfigured backends show Not reported, never an inferred model', () => {
  for (const props of [{}, { environmentName: null, modelDeploymentName: null },
    { environmentName: ' ', modelDeploymentName: '' }]) {
    const html = renderToStaticMarkup(createElement(DeploymentMetadata, props));
    assert.equal(html.match(/Not reported/g)?.length, 2);
  }
});

test('deployment labels are rendered as text, not HTML', () => {
  const html = renderToStaticMarkup(createElement(DeploymentMetadata, {
    environmentName: '<script>example</script>', modelDeploymentName: 'model&name'
  }));
  assert.ok(!html.includes('<script>'));
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /model&amp;name/);
});
