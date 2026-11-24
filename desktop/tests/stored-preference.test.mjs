import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

// 回归：偏好以前不管是不是默认值都写进 localStorage，之后调整默认值时，
// 只要用户碰过一次设置页，就永远停在当时的默认值上。

function fixture(initial = []) {
  const store = new Map(initial);
  const load = createTypeScriptLoader({
    localStorage: {
      getItem: (key) => (store.has(key) ? store.get(key) : null),
      setItem: (key, value) => { store.set(key, String(value)); },
      removeItem: (key) => { store.delete(key); },
    },
    window: { dispatchEvent: () => true },
    CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options?.detail; } },
  }, {
    '@tauri-apps/api/core': { invoke: () => Promise.resolve({}) },
  });
  return { load, store };
}

test('preferences are stored only while they differ from the default', () => {
  const { load, store } = fixture();
  const api = load(new URL('../src/lib/api.ts', import.meta.url));
  const cases = [
    [api.setThemeModePreference, 'dark', 'system'],
    [api.setThemePalettePreference, 'sakura', 'default'],
    [api.setCacheBrowserFontSizePreference, 18, api.CACHE_BROWSER_FONT_SIZE_DEFAULT],
    [api.setHomeHistoryRetentionLimit, 50, api.HOME_HISTORY_LIMIT_DEFAULT],
    [api.setHomeJobRetentionLimit, 50, api.HOME_JOB_LIMIT_DEFAULT],
    [api.setDisableAnimationsPreference, true, api.DISABLE_ANIMATIONS_DEFAULT],
  ];
  for (const [set, changed, defaultValue] of cases) {
    set(changed);
    assert.equal(store.size, 1, `${set.name} stores a changed value`);
    set(defaultValue);
    assert.equal(store.size, 0, `${set.name} drops the entry at its default`);
  }
});

test('agent permission mode is stored only while it is not the default', () => {
  const store = new Map();
  const storage = {
    getItem: (key) => (store.has(key) ? store.get(key) : null),
    setItem: (key, value) => { store.set(key, String(value)); },
    removeItem: (key) => { store.delete(key); },
  };
  const permission = createTypeScriptLoader({ localStorage: storage, window: { localStorage: storage } })(
    new URL('../src/lib/permissionMode.ts', import.meta.url));
  permission.savePermissionMode('auto');
  assert.equal(permission.loadPermissionMode(), 'auto');
  permission.savePermissionMode('ask');
  assert.equal(store.size, 0);
  assert.equal(permission.loadPermissionMode(), 'ask');
});

test('values 8.5.3 stored only because they were its defaults are cleared once at startup', () => {
  const { load, store } = fixture([
    ['galtransl-theme-mode', 'system'],
    ['galtransl-theme-palette', 'sakura'],
    ['galtransl-cache-browser-font-size', '14'],
    ['galtransl-home-history-limit', '50'],
    ['galtransl.agent.permissionMode', 'ask'],
  ]);
  const api = load(new URL('../src/lib/api.ts', import.meta.url));
  api.migrateLegacyStoredDefaults();
  assert.deepEqual(Object.fromEntries(store), {
    'galtransl-theme-palette': 'sakura',
    'galtransl-home-history-limit': '50',
    'galtransl-stored-defaults-migrated': '1',
  });
  // Runs once: a value the user picks afterwards stays even if it equals an old default.
  store.set('galtransl-cache-browser-font-size', '14');
  api.migrateLegacyStoredDefaults();
  assert.equal(store.get('galtransl-cache-browser-font-size'), '14');
});
