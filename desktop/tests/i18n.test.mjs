import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { test } from 'node:test';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';
import { checkResources, validateCatalog } from '../scripts/check-i18n.mjs';

const src = (file) => new URL(`../src/${file}`, import.meta.url);
function setup(globals = {}) {
  const load = createTypeScriptLoader(globals);
  return { load, core: load(src('i18n/core.ts')) };
}
function storage(value) {
  const values = new Map(value == null ? [] : [['galtransl.ui.language', value]]);
  return { values, getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) };
}

test('first launch is Chinese regardless of browser language; persisted invalid preferences fall back', () => {
  const { core } = setup({ navigator: { language: 'en-US' } });
  for (const preference of [undefined, 'fr', 'en-US', '', 'zh-CN']) {
    const document = { documentElement: { lang: '' } };
    const client = core.createUiI18n({ storage: storage(preference), document });
    assert.equal(client.language(), 'zh-CN');
    assert.equal(document.documentElement.lang, 'zh-CN');
  }
  const client = core.createUiI18n({ storage: storage('en') });
  assert.equal(client.language(), 'en');
  assert.equal(client.translate('common:actions.save'), 'Save');
});

test('switching persists an independent UI preference, updates document language and subscriptions', async () => {
  const { core } = setup();
  const localStorage = storage();
  localStorage.values.set('translation.language', 'ja');
  const document = { documentElement: { lang: '' } };
  const client = core.createUiI18n({ storage: localStorage, document });
  const observed = [];
  const unsubscribe = client.subscribe(() => observed.push(client.language()));
  client.instance.addResource('en', 'common', 'actions.save', 'Save');
  await client.changeLanguage('en');
  assert.equal(client.translate('common:actions.save'), 'Save');
  assert.equal(localStorage.values.get(core.UI_LANGUAGE_STORAGE_KEY), 'en');
  assert.equal(localStorage.values.get('translation.language'), 'ja');
  assert.equal(document.documentElement.lang, 'en');
  await client.changeLanguage('zh-CN');
  assert.equal(client.translate('common:actions.save'), '保存');
  assert.deepEqual(observed, ['en', 'zh-CN']);
  unsubscribe();
  await client.changeLanguage('invalid');
  assert.equal(observed.length, 2);
});

test('blocked storage can still switch language for the current session', async () => {
  const { core } = setup();
  const blocked = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('quota'); } };
  const client = core.createUiI18n({ storage: blocked });
  assert.equal(client.language(), 'zh-CN');
  await client.changeLanguage('en');
  assert.equal(client.language(), 'en');
});

test('missing and empty English resources fall back; interpolation and plurals use named values', async () => {
  const { core } = setup();
  const client = core.createUiI18n();
  const key = 'settings:appearance.language';
  client.instance.addResource('en', 'settings', 'appearance.language', '');
  await client.changeLanguage('en');
  assert.equal(client.translate(key), '界面语言');
  client.instance.removeResourceBundle('en', 'settings');
  assert.equal(client.translate(key), '界面语言');
  assert.equal(client.translate('errors:profiles.notFound', { name: 'A&B <model>' }), 'Backend configuration not found: A&B <model>');
  const pluralKey = 'plugins:pluginSettingsEditor.pluginSettingsAdvanced_message_advancedSettingsItem';
  const leaf = pluralKey.split(':')[1];
  client.instance.addResource('en', 'plugins', `${leaf}_one`, '{{count}} advanced setting');
  client.instance.addResource('en', 'plugins', `${leaf}_other`, '{{count}} advanced settings');
  assert.equal(client.translate(pluralKey, { count: 1 }), '1 advanced setting');
  assert.equal(client.translate(pluralKey, { count: 3 }), '3 advanced settings');
});

test('saved message references resolve in the new language while raw backend text is preserved', async () => {
  const { core } = setup();
  const client = core.createUiI18n();
  const saved = core.message('errors:profiles.notFound', { name: 'test' });
  assert.equal(client.resolve(saved), '未找到后端配置：test');
  client.instance.addResource('en', 'errors', 'profiles.notFound', 'Missing profile: {{name}}');
  await client.changeLanguage('en');
  assert.equal(client.resolve(saved), 'Missing profile: test');
  assert.equal(client.resolve('模型返回的原始错误'), '模型返回的原始错误');
});

test('config option labels translate without changing the configured value', async () => {
  const { core, load } = setup();
  const { resolveConfigOption } = load(src('i18n/config.ts'));
  const labelKey = 'config:translationSettingsSection.option_zhCn';
  const option = { value: 'zh-cn', labelKey };
  assert.equal(resolveConfigOption(option).value, 'zh-cn');
  core.i18n.addResource('en', 'config', 'translationSettingsSection.option_zhCn', 'Simplified Chinese');
  await core.setUiLanguage('en');
  assert.equal(resolveConfigOption(option).label, 'Simplified Chinese');
  assert.equal(resolveConfigOption(option).value, 'zh-cn');
  assert.equal(resolveConfigOption('custom.md').value, 'custom.md');
});

test('builtin plugin options preserve numeric, empty and custom values; unknown metadata falls back', async () => {
  const { core, load } = setup();
  const { localizePlugin } = load(src('i18n/plugins.ts'));
  const plugin = { name: 'file_subtitle_srt_lrc_vtt', display_name: 'raw', description: 'raw', settings: { '上下双语1左右双语2': 1 }, settings_schema: {
    '上下双语1左右双语2': { label: 'raw', options: [{ value: 1, label: 'raw' }, { value: 2, label: 'raw' }, { value: true, label: 'custom boolean' }] },
    future: { label: 'future label', options: [{ value: '', label: 'empty custom' }] },
  } };
  core.i18n.addResource('en', 'plugins', 'builtin.file_subtitle_srt_lrc_vtt.settings.bilingualLayout.options.layout1', 'Vertical');
  await core.setUiLanguage('en');
  const result = localizePlugin(plugin);
  const options = result.settings_schema['上下双语1左右双语2'].options;
  assert.equal(options[0].label, 'Vertical');
  assert.equal(options[0].value, 1);
  assert.equal(options[2].value, true);
  assert.equal(options[2].label, 'custom boolean');
  assert.equal(result.settings, plugin.settings);
  assert.equal(result.settings_schema.future, plugin.settings_schema.future);
  assert.equal(JSON.stringify(plugin.settings), JSON.stringify(result.settings));
  const script = localizePlugin({ name: 'file_msgtool_script', display_name: 'raw', settings: {}, settings_schema: { script_type: { options: [{ value: '', label: 'raw' }] } } });
  assert.equal(script.settings_schema.script_type.options[0].value, '');
  assert.equal(script.settings_schema.script_type.options[0].label, 'Auto-detect (recommended)');
  const thirdParty = { ...plugin, name: 'custom.plugin-v2' };
  assert.equal(localizePlugin(thirdParty), thirdParty);
});

test('metadata created at module load and date formatting observe language changes', async () => {
  const { core, load } = setup();
  const { toolMeta } = load(src('pages/agent/toolMeta.ts'));
  const { formatTimestamp } = load(src('lib/format.ts'));
  const permissions = load(src('lib/permissionMode.ts'));
  const timestamp = '2026-10-03T02:00:00Z';
  const chineseDate = formatTimestamp(timestamp);
  const meta = toolMeta('read_input_file');
  assert.equal(meta.action, '读取原文');
  core.i18n.addResource('en', 'agent', 'tools.read_input_file.action', 'Read input');
  core.i18n.addResource('en', 'agent', 'permissionMode.ask_ask_text', 'Ask');
  await core.setUiLanguage('en');
  assert.equal(meta.action, 'Read input');
  assert.equal(permissions.PERMISSION_MODE_LABELS.ask, 'Ask');
  assert.notEqual(formatTimestamp(timestamp), chineseDate);
  assert.equal(toolMeta('third_party_tool').action, 'third_party_tool');
  await core.setUiLanguage('zh-CN');
  assert.equal(meta.action, '读取原文');
  assert.equal(formatTimestamp(timestamp), chineseDate);
});

test('resource checks reject unknown keys, invalid types, parameter and rich text mismatches', () => {
  const result = validateCatalog({ message: '你好 {{name}} <0>说明</0>', pending: '待翻译' }, { message: 'Hi {{wrong}}', pending: '', unknown: 'x', bad: 7 });
  assert.ok(result.errors.some((error) => error.includes('unknown')));
  assert.ok(result.errors.some((error) => error.includes('expected a string')));
  assert.ok(result.errors.some((error) => error.includes('interpolation')));
  assert.ok(result.errors.some((error) => error.includes('rich text')));
  assert.deepEqual(result.pending, ['pending']);
  assert.equal(validateCatalog({ missing: '中文' }, {}).missing.length, 1);
});

test('production English catalogs are complete and all resource references pass audit', () => {
  const directory = new URL('../src/i18n/locales/en/', import.meta.url);
  const filled = (object) => Object.values(object).every((value) => typeof value === 'string' ? value.trim() !== '' : filled(value));
  for (const file of readdirSync(directory)) assert.ok(filled(JSON.parse(readFileSync(new URL(file, directory), 'utf8'))), file);
  const { errors, report } = checkResources();
  assert.deepEqual(errors, []);
  assert.deepEqual(report.filter((item) => item.pending.length || item.missing.length), []);
});
