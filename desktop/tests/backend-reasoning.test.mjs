import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement as h } from 'react';
import { act, create } from 'react-test-renderer';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

function fixture(readOnly = false) {
  const load = createTypeScriptLoader();
  const { BackendConfigEditor } = load(new URL('../src/components/BackendConfigEditor.tsx', import.meta.url));
  const { CustomSelect } = load(new URL('../src/components/CustomSelect.tsx', import.meta.url));
  let config = { 'OpenAI-Compatible': { tokens: [{ token: 'sk-test', modelName: 'custom-alias', endpoint: '' }] } };
  let renderer;
  function render() {
    renderer.update(h(BackendConfigEditor, { config, onChange, readOnly }));
  }
  function onChange(next) { config = next; render(); }
  act(() => { renderer = create(h(BackendConfigEditor, { config, onChange, readOnly })); });
  const select = (label) => renderer.root.findAllByType(CustomSelect).find((node) =>
    node.parent.props.children[0].props.children === label);
  return {
    value: () => JSON.parse(JSON.stringify(config['OpenAI-Compatible'])),
    select,
    change(label, value) { act(() => select(label).props.onChange({ target: { value } })); },
    dispose() { act(() => renderer.unmount()); },
  };
}

test('reasoning effort can return to provider defaults or inheritance', () => {
  const f = fixture();
  try {
    for (const label of ['思考开关', '思考类型', '思考开关（全局）', '思考类型（全局）']) {
      assert.equal(f.select(label), undefined);
    }
    assert.equal(f.select('思考等级（全局）').props.value, '');
    assert.equal(f.select('思考等级').props.value, '');
    f.change('思考等级（全局）', 'high');
    assert.equal(f.value().reasoningEffort, 'high');

    f.change('思考等级', 'none');
    assert.equal(f.value().tokens[0].reasoningEffort, 'none');
    assert.equal(f.value().reasoningEffort, 'high');
    assert.equal(f.value().tokens[0].modelName, 'custom-alias');

    for (const label of ['思考等级', '思考等级（全局）']) {
      f.change(label, '');
    }
    assert.equal(Object.hasOwn(f.value(), 'reasoningEffort'), false);
    assert.equal(Object.hasOwn(f.value().tokens[0], 'reasoningEffort'), false);
  } finally { f.dispose(); }
});

test('read-only profiles disable all reasoning controls and ignore edits', () => {
  const f = fixture(true);
  try {
    for (const label of ['思考等级', '思考等级（全局）']) {
      assert.equal(f.select(label).props.disabled, true);
      f.change(label, 'high');
    }
    assert.deepEqual(f.value(), { tokens: [{ token: 'sk-test', modelName: 'custom-alias', endpoint: '' }] });
  } finally { f.dispose(); }
});
