import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement } from 'react';
import * as React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

const load = createTypeScriptLoader({}, {
  '../../lib/api': { fetchProblemTypes: async () => [] },
});
const { ProblemAnalyzeSection, readProblemList, problemPluginOverrides } = load(new URL('../src/pages/project-config/ProblemAnalyzeSection.tsx', import.meta.url));
const common = { name: 'problem_common', module: 'problem_common', type: 'problem',
  display_name: 'Common checks', settings: {}, description: '', version: '', author: '' };
const render = (config, problemPlugins = [common]) => renderToStaticMarkup(createElement(ProblemAnalyzeSection, {
  config, problemPlugins, onProblemPluginsChange() {}, onPluginSettingChange() {},
  onProblemListChange() {}, onDirty() {},
}));

test('legacy config enables common checks and an explicit empty list disables them', () => {
  const legacy = render({});
  assert.match(legacy, /type="checkbox" checked=""/);
  assert.match(legacy, /problem-analyze-section/);
  const disabled = render({ plugin: { problemPlugins: [] } });
  assert.doesNotMatch(disabled, /checked=""/);
  assert.match(disabled, /problem-analyze-section/);
});

test('local problem plugins display overrides using the module key', () => {
  const custom = { ...common, name: 'problem_local', module: 'problem_local', project_local: true,
    settings: { limit: 100 }, settings_schema: { limit: { label: 'Limit' } } };
  const html = render({ plugin: { problemPlugins: ['(project_dir)problem_local'],
    problem_local: { limit: 25 } } }, [custom]);
  assert.match(html, /\(project_dir\)problem_local/);
  assert.match(html, /value="25"/);
  assert.match(html, /problem-analyze-section/);
});

test('configured unavailable plugins remain selected and visible', () => {
  const html = render({ plugin: { problemPlugins: ['problem_missing'] } });
  assert.match(html, /problem_missing/);
  assert.equal((html.match(/checked=""/g) || []).length, 1);
});

test('selection reads declared defaults from every plugin and respects explicit overrides', () => {
  const types = [
    { name: 'common check', description: '', default_enabled: true },
    { name: 'custom check', description: '', default_enabled: true },
    { name: 'optional check', description: '', default_enabled: false },
  ];
  const selected = (config) => Array.from(readProblemList(config, types));
  assert.deepEqual(selected({}), ['common check', 'custom check']);
  assert.deepEqual(selected({ problemAnalyze: { problemList: null } }), ['common check', 'custom check']);
  assert.deepEqual(selected({ problemAnalyze: { problemList: [] } }), []);
  assert.deepEqual(selected({ problemAnalyze: { problemList: ['optional check'] } }), ['optional check']);
  assert.deepEqual(selected({ problemAnalyze: { GPT35: ['legacy check'] } }), ['legacy check']);
  assert.deepEqual(selected({ problemAnalyze: { problemList: [], GPT35: ['legacy check'] } }), []);
});

test('threshold renders once as a plugin setting with legacy fallback and override precedence', () => {
  const plugin = { ...common, settings: { avgSentenceLengthThreshold: 17 },
    settings_schema: { avgSentenceLengthThreshold: { min: 1, step: 1,
      legacy_path: 'problemAnalyze.avgSentenceLengthThreshold' } } };
  const legacy = { problemAnalyze: { avgSentenceLengthThreshold: 8 } };
  assert.equal(problemPluginOverrides(legacy, plugin).avgSentenceLengthThreshold, 8);
  const html = render(legacy, [plugin]);
  assert.match(html, /value="8"/);
  assert.equal((html.match(/type="number"/g) || []).length, 1);
  assert.doesNotMatch(html, /problem-analyze-section__threshold/);
  const overridden = { ...legacy, plugin: { problem_common: { avgSentenceLengthThreshold: 25 } } };
  assert.equal(problemPluginOverrides(overridden, plugin).avgSentenceLengthThreshold, 25);
  assert.match(render(overridden, [plugin]), /value="25"/);
  assert.match(render({}, [plugin]), /value="17"/);
});

function loadedSection(types, config, callbacks = {}) {
  const loader = createTypeScriptLoader({}, {
    react: { ...React, useState: () => [types, () => {}], useMemo: (fn) => fn(), useEffect() {} },
    '../../i18n': {
      useUiLanguage: () => 'en', useMessageState: () => [null, () => {}],
      t: (key, params) => params?.count !== undefined ? `Enabled ${params.count} / ${params.value}` : key,
    },
    '../../lib/api': { fetchProblemTypes: async () => types },
  });
  const { ProblemAnalyzeSection: Section } = loader(new URL('../src/pages/project-config/ProblemAnalyzeSection.tsx', import.meta.url));
  return Section({ config, problemPlugins: [common], onProblemPluginsChange() {},
    onPluginSettingChange() {}, onProblemListChange() {}, onDirty() {}, ...callbacks });
}

function descendants(element) {
  if (!element || typeof element !== 'object') return [];
  if (Array.isArray(element)) return element.flatMap(descendants);
  return [element, ...descendants(element.props?.children)];
}

test('disabled providers keep all types visible with disabled controls and accurate effective count', () => {
  const types = [
    { name: 'shared', default_enabled: true, plugins: ['problem_common', '(project_dir)custom'] },
    { name: 'inactive', default_enabled: true, plugins: ['problem_inactive'] },
    { name: 'optional', default_enabled: false, plugins: ['problem_common'] },
  ];
  const section = loadedSection(types, { plugin: { problemPlugins: ['problem_common'] } });
  const commonGroup = descendants(section).find((item) => item.props?.['data-plugin-name'] === 'problem_common');
  const controls = descendants(commonGroup).filter((item) => item.props?.className === 'problem-analyze-section__checkbox');
  assert.deepEqual(controls.map((item) => [item.props.checked, item.props.disabled]), [
    [true, false], [false, false],
  ]);
  const inactiveGroup = descendants(section).find((item) => item.props?.['data-plugin-name'] === 'problem_inactive');
  const inactive = descendants(inactiveGroup).find((item) => item.props?.className === 'problem-analyze-section__checkbox');
  assert.equal(inactive.props.checked, true);
  assert.equal(inactive.props.disabled, true);
  const html = renderToStaticMarkup(section);
  assert.match(html, /Enabled 1 \/ 3/);
  assert.match(html, /providerDisabled/);
  assert.match(html, /sourcePlugins/);
  assert.match(html, /inactive/);
  const disabled = loadedSection(types, { plugin: { problemPlugins: [] } });
  assert.match(renderToStaticMarkup(disabled), /Enabled 0 \/ 3/);
  assert.ok(descendants(disabled).filter((item) => item.props?.className === 'problem-analyze-section__checkbox')
    .every((item) => item.props.disabled));
});

test('checks and settings render inside their provider and shared checks stay synchronized', () => {
  const custom = { ...common, name: 'local', module: 'local', project_local: true };
  const commonWithSettings = { ...common, settings: { threshold: 17 } };
  const types = [
    { name: 'common only', default_enabled: false, plugins: ['problem_common'] },
    { name: 'local only', default_enabled: false, plugins: ['(project_dir)local'] },
    { name: 'shared', default_enabled: false, plugins: ['problem_common', '(project_dir)local'] },
  ];
  let config = { plugin: { problemPlugins: ['problem_common', '(project_dir)local'] },
    problemAnalyze: { problemList: ['missing check'] } };
  const callbacks = { problemPlugins: [commonWithSettings, custom],
    onProblemListChange: (names) => { config = { ...config, problemAnalyze: { problemList: Array.from(names) } }; } };
  const section = loadedSection(types, config, callbacks);
  const groups = descendants(section).filter((item) => item.props?.['data-plugin-name']);
  const rowNames = (group) => descendants(group)
    .filter((item) => item.props?.className === 'problem-analyze-section__name')
    .map((item) => item.props.children);
  assert.deepEqual(rowNames(groups[0]), ['common only', 'shared']);
  assert.deepEqual(rowNames(groups[1]), ['local only', 'shared']);
  assert.ok(descendants(groups[0]).some((item) => item.type?.name === 'PluginSettingsEditor'));
  const lists = descendants(section).filter((item) => item.type === 'ul' && item.props.className === 'problem-analyze-section__list');
  assert.equal(lists.length, 1);
  assert.deepEqual(rowNames(lists[0]), ['missing check']);
  const commonControls = descendants(groups[0]).filter((item) => item.props?.className === 'problem-analyze-section__checkbox');
  commonControls[1].props.onChange({ target: { checked: true } });
  assert.deepEqual(config.problemAnalyze.problemList, ['shared', 'missing check']);
  const updated = loadedSection(types, config, callbacks);
  const sharedRows = descendants(updated).filter((item) => item.type === 'li' && item.key === 'shared');
  assert.equal(sharedRows.length, 2);
  assert.ok(sharedRows.every((row) => descendants(row).find((item) => item.type === 'input').props.checked));
  assert.match(renderToStaticMarkup(updated), /Enabled 1 \/ 3/);
});

test('select all respects provider state and restore defaults emits null with dirty notification', () => {
  const types = [
    { name: 'active', default_enabled: false, plugins: ['problem_common'] },
    { name: 'inactive', default_enabled: false, plugins: ['problem_inactive'] },
  ];
  const changes = [];
  let dirty = 0;
  const section = loadedSection(types, { problemAnalyze: { problemList: [] } }, {
    onProblemListChange: (value) => changes.push(value), onDirty: () => dirty++,
  });
  const buttons = descendants(section).filter((item) => item.type === 'button');
  buttons.find((item) => item.props.children === 'common:actions.selectAll').props.onClick();
  assert.deepEqual(Array.from(changes[0]), ['active']);
  buttons.find((item) => item.props.children.endsWith('.restorePluginDefaults')).props.onClick();
  assert.equal(changes[1], null);
  assert.equal(dirty, 2);
});
