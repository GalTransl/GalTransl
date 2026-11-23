import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement as h, useState } from 'react';
import { act, create } from 'react-test-renderer';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

const status = {
  supported: true, reason: '', phase: 'idle', id: '', version: '', downloaded: 0, total: 0, error: '',
};

function mount(options = {}) {
  const calls = [], timers = new Map(), listeners = new Map();
  let sequence = 0;
  const load = createTypeScriptLoader({
    document: { body: {}, addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: name => listeners.delete(name) },
    setTimeout: fn => { timers.set(++sequence, fn); return sequence; },
    clearTimeout: id => timers.delete(id),
  }, {
    '../../i18n': { t: key => key, message: key => key, useUiLanguage: () => 'zh-CN', useMessageState: useState },
    'react-dom': { createPortal: element => element },
    '../../components/Icon': { Icon: () => h('span') },
    '../../lib/errors': { normalizeError: error => error.message },
    '../connection/ConnectionContext': { useConnection: () => ({ versionInfo: { version: '1.0.0', latest_version: '2.0.0', update_available: true, ...options.versionInfo } }) },
    '../../lib/externalLink': { RELEASE_LATEST_URL: 'release', openExternalUrl: async () => calls.push('download-page') },
    '../../lib/api': {
      getIgnoredUpdateVersion: () => '', setIgnoredUpdateVersion: version => calls.push(['ignore', version]),
      canInstallApplicationUpdate: () => options.native !== false,
      fetchApplicationUpdateStatus: async () => { calls.push('status'); return options.poll?.() ?? status; },
      startApplicationUpdate: async preserve => { calls.push(['start', preserve]); return options.start?.() ?? { ...status, phase: 'downloading', id: 'a'.repeat(32), downloaded: 10, total: 100 }; },
      installApplicationUpdate: async id => { calls.push(['install', id]); return options.install?.(); },
    },
  });
  const { UpdateNotice } = load(new URL('../src/features/update/UpdateNotice.tsx', import.meta.url));
  let renderer;
  const render = async () => { await act(async () => { renderer = create(h(UpdateNotice)); }); };
  return { render, calls, timers, listeners, renderer: () => renderer,
    button: key => renderer.root.findAllByType('button').find(node => node.children.includes('common:updateNotice.' + key)),
    tick: async () => {
      const [id, fn] = timers.entries().next().value;
      timers.delete(id);
      await act(async () => fn());
    } };
}

test('automatic update passes the dictionary option, displays progress, and installs only when ready', async () => {
  let result = status;
  const dialog = mount({ poll: () => result });
  await dialog.render();
  assert.equal(dialog.renderer().root.findByType('input').props.checked, true);
  assert.equal(dialog.button('autoUpdate').props.disabled, false);
  assert.equal(dialog.calls.some(call => Array.isArray(call) && call[0] === 'start'), false);
  act(() => dialog.renderer().root.findByType('input').props.onChange({ target: { checked: false } }));
  await act(async () => dialog.button('autoUpdate').props.onClick());
  assert.deepEqual(dialog.calls.find(call => Array.isArray(call) && call[0] === 'start'), ['start', false]);
  assert.equal(dialog.button('autoUpdate').props.disabled, true);
  assert.equal(dialog.renderer().root.findByType('progress').props.value, 10);
  assert.equal(dialog.calls.some(call => Array.isArray(call) && call[0] === 'install'), false);
  act(() => dialog.listeners.get('keydown')({ key: 'Escape' }));
  assert.equal(dialog.renderer().root.findAllByProps({ role: 'dialog' }).length, 1);
  result = { ...status, phase: 'ready', id: 'a'.repeat(32), downloaded: 100, total: 100 };
  await dialog.tick();
  assert.deepEqual(dialog.calls.filter(call => Array.isArray(call) && call[0] === 'install'), [['install', 'a'.repeat(32)]]);
  assert.equal(dialog.timers.size, 0);
  act(() => dialog.renderer().unmount());
});

test('a failed download remains visible and can retry with dictionaries preserved', async () => {
  let result = status;
  const dialog = mount({ poll: () => result });
  await dialog.render();
  await act(async () => dialog.button('autoUpdate').props.onClick());
  result = { ...status, phase: 'failed', error: 'Checksum mismatch' };
  await dialog.tick();
  assert.equal(dialog.renderer().root.findByProps({ role: 'alert' }).children.join(''), 'Checksum mismatch');
  assert.equal(dialog.button('autoUpdate').props.disabled, false);
  assert.equal(dialog.calls.some(call => Array.isArray(call) && call[0] === 'install'), false);
  await act(async () => dialog.button('autoUpdate').props.onClick());
  assert.deepEqual(dialog.calls.filter(call => Array.isArray(call) && call[0] === 'start'), [['start', true], ['start', true]]);
  act(() => dialog.renderer().unmount());
  assert.equal(dialog.timers.size, 0);
});

test('a native handoff failure can retry the prepared installer without another download', async () => {
  let result = status, failure = true;
  const dialog = mount({ poll: () => result, install: () => { if (failure) throw Error('Installer unavailable'); } });
  await dialog.render();
  await act(async () => dialog.button('autoUpdate').props.onClick());
  result = { ...status, phase: 'ready', id: 'a'.repeat(32) };
  await dialog.tick();
  assert.equal(dialog.renderer().root.findByProps({ role: 'alert' }).children.join(''), 'Installer unavailable');
  failure = false;
  await act(async () => dialog.button('autoUpdate').props.onClick());
  assert.equal(dialog.calls.filter(call => Array.isArray(call) && call[0] === 'start').length, 1);
  assert.equal(dialog.calls.filter(call => Array.isArray(call) && call[0] === 'install').length, 2);
  act(() => dialog.renderer().unmount());
});

test('browser mode retains the download-page action without starting an installer', async () => {
  const dialog = mount({ native: false });
  await dialog.render();
  assert.equal(dialog.button('autoUpdate').props.disabled, true);
  assert.equal(dialog.calls.length, 0);
  await act(async () => dialog.button('updateNoticeActions_message_open').props.onClick());
  assert.deepEqual(dialog.calls, ['download-page']);
  assert.equal(dialog.renderer().toJSON(), null);
  act(() => dialog.renderer().unmount());
});

test('the notice shows the release summary under the versions and omits it when the release has none', async () => {
  // 概述来自 release 正文；旧后端不返回该字段、自动生成的说明没有概述，这两种情况都不留空段落。
  const summary = '本次更新调整了默认批次大小，工作台新增 Token 用量统计。';
  const dialog = mount({ versionInfo: { release_summary: `  ${summary}\n` } });
  await dialog.render();
  assert.equal(dialog.renderer().root.findByProps({ className: 'update-notice__summary' }).children.join(''), summary);
  act(() => dialog.renderer().unmount());
  for (const versionInfo of [{}, { release_summary: '' }]) {
    const plain = mount({ versionInfo });
    await plain.render();
    assert.equal(plain.renderer().root.findAllByProps({ className: 'update-notice__summary' }).length, 0);
    act(() => plain.renderer().unmount());
  }
});
