import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

const load = createTypeScriptLoader();
const { summarizeTokenUsage, formatTokenCount, formatTokenPercent } = load(
  new URL('../src/lib/tokenUsage.ts', import.meta.url),
);

test('no usage yet (idle project, old backend) shows a placeholder instead of zeros', () => {
  for (const usage of [undefined, null]) {
    const summary = summarizeTokenUsage(usage);
    assert.equal(summary.hasData, false);
    assert.equal(summary.total, 0);
    assert.equal(summary.cached, null);
    assert.equal(summary.reasoning, null);
  }
});

test('rates use only the requests that reported that item as the denominator', () => {
  const summary = summarizeTokenUsage({
    requests: 3,
    reported_requests: 2,
    input_tokens: 1500,
    cached_tokens: 800,
    cache_reported_input_tokens: 1000,
    output_tokens: 350,
    reasoning_tokens: 100,
    reasoning_reported_output_tokens: 300,
  });
  assert.equal(summary.hasData, true);
  assert.equal(summary.unreportedRequests, 1);
  assert.equal(summary.total, 1850);
  assert.equal(summary.cacheRate, 0.8);
  assert.equal(formatTokenPercent(summary.reasoningRate), '33.3');
});

test('items the provider never reported stay unknown rather than becoming zero hits', () => {
  const summary = summarizeTokenUsage({
    requests: 2,
    reported_requests: 2,
    input_tokens: 1000,
    cached_tokens: null,
    cache_reported_input_tokens: 0,
    output_tokens: 100,
    reasoning_tokens: null,
    reasoning_reported_output_tokens: 0,
  });
  assert.equal(summary.cached, null);
  assert.equal(summary.cacheRate, null);
  assert.equal(summary.reasoning, null);
  assert.equal(summary.reasoningRate, null);
});

test('compact counts fit one ribbon cell and never round up into the next unit', () => {
  assert.equal(formatTokenCount(0), '0');
  assert.equal(formatTokenCount(999), '999');
  assert.equal(formatTokenCount(1000), '1.0K');
  assert.equal(formatTokenCount(12_345), '12.3K');
  assert.equal(formatTokenCount(999_949), '999.9K');
  assert.equal(formatTokenCount(999_950), '1.00M');
  assert.equal(formatTokenCount(4_560_000), '4.56M');
  assert.equal(formatTokenCount(2_500_000_000), '2.50B');
});
