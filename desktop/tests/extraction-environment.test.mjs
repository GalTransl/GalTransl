import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement as h } from 'react';
import { act, create } from 'react-test-renderer';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

const initial = {
  ready: false, problems: ['skill_missing'], python_version: 'Python 3.11',
  settings: { skill_dir: '/app/skills/missing', python_path: '' },
  skill: { installed: false, current_commit: '', latest_commit: '', update_available: false },
};
const installed = {
  ...initial, ready: true, problems: [], settings: { skill_dir: '/data/skills/version', python_path: '' },
  skill: { ...initial.skill, installed: true, current_commit: 'b'.repeat(40) },
};

function mount(api, environment = initial) {
  const saved = [], calls = [];
  const load = createTypeScriptLoader({}, {
    '../../i18n': { useUiLanguage: () => 'zh-CN' },
    '@tauri-apps/plugin-dialog': { open: async () => null },
    './useDirectoryDrop': {},
    './labels': { x: key => key },
    './api': { extractionApi: (action, params, body) => { calls.push({ action, body }); return api(action, body); } },
  });
  const { EnvironmentDialog } = load(new URL('../src/pages/extraction/Dialogs.tsx', import.meta.url));
  let closed = false, renderer;
  act(() => { renderer = create(h(EnvironmentDialog, { initial: environment, saved: value => saved.push(value), close: () => { closed = true; } })); });
  return { renderer, saved, calls, closed: () => closed,
    button: key => renderer.root.findAllByType('button').find(node => node.children.includes(key)) };
}

test('runtime skill actions are explicit and downloads activate without losing pending Python edits', async () => {
  const dialog = mount(async (action, body) => {
    if (action === 'skill-check') return { ...initial.skill, latest_commit: 'b'.repeat(40), update_available: true };
    if (action === 'skill-install') return installed;
    if (action === 'settings') return { ...installed, settings: body };
    throw Error(`Unexpected action: ${action}`);
  });
  assert.equal(dialog.calls.length, 0);
  assert.ok(dialog.button('skillDownload'));
  act(() => dialog.renderer.root.findAllByType('input')[1].props.onChange({ target: { value: '/custom/python' } }));
  await act(async () => dialog.button('skillCheck').props.onClick());
  assert.equal(dialog.calls[0].action, 'skill-check');
  assert.equal(dialog.calls[0].body.skill_dir, initial.settings.skill_dir);
  assert.ok(dialog.renderer.root.findAllByType('p').some(node => node.children.includes('skillUpdateAvailable')));
  assert.equal(dialog.saved.length, 0);
  await act(async () => dialog.button('skillDownload').props.onClick());
  assert.deepEqual(dialog.saved, [installed]);
  assert.equal(dialog.closed(), false);
  assert.equal(dialog.renderer.root.findAllByType('input')[0].props.value, installed.settings.skill_dir);
  assert.equal(dialog.renderer.root.findAllByType('input')[1].props.value, '/custom/python');
  assert.ok(dialog.renderer.root.findAllByType('p').some(node => node.children.includes('skillUpToDate')));
  assert.equal(dialog.renderer.root.findAllByType('p').some(node => node.children.includes('missingSkill')), false);
  assert.equal(dialog.button('skillUpdate').props.disabled, true);
  act(() => dialog.renderer.root.findAllByType('input')[0].props.onChange({ target: { value: '/custom/skill' } }));
  assert.equal(dialog.renderer.root.findAllByType('code').length, 0);
  await act(async () => dialog.renderer.root.findByType('form').props.onSubmit({ preventDefault() {} }));
  assert.equal(dialog.calls.at(-1).body.skill_dir, '/custom/skill');
  assert.equal(dialog.calls.at(-1).body.python_path, '/custom/python');
  assert.equal(dialog.closed(), true);
  act(() => dialog.renderer.unmount());
});

test('checks and downloads show errors, release disabled controls, and permit retry', async () => {
  let finish;
  const dialog = mount(() => new Promise((resolve, reject) => { finish = { resolve, reject }; }), installed);
  act(() => dialog.button('skillCheck').props.onClick());
  assert.equal(dialog.renderer.root.findByType('fieldset').props.disabled, true);
  assert.ok(dialog.button('skillChecking'));
  await act(async () => finish.reject(Error('Network unavailable')));
  assert.equal(dialog.renderer.root.findByProps({ role: 'alert' }).children.join(''), 'Network unavailable');
  assert.equal(dialog.renderer.root.findByType('fieldset').props.disabled, false);
  act(() => dialog.button('skillUpdate').props.onClick());
  assert.ok(dialog.button('skillInstalling'));
  await act(async () => finish.reject(Error('Download failed')));
  assert.equal(dialog.renderer.root.findByProps({ role: 'alert' }).children.join(''), 'Download failed');
  assert.equal(dialog.renderer.root.findAllByType('input')[0].props.value, installed.settings.skill_dir);
  assert.equal(dialog.saved.length, 0);
  act(() => dialog.button('skillUpdate').props.onClick());
  await act(async () => finish.resolve(installed));
  assert.equal(dialog.renderer.root.findAllByProps({ role: 'alert' }).length, 0);
  assert.deepEqual(dialog.saved, [installed]);
  act(() => dialog.renderer.unmount());
});
