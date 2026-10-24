import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement as h, useState } from 'react';
import { act, create } from 'react-test-renderer';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

test('small-list animations follow the app switch, cached-page activity, visibility and disposal', () => {
  const listeners = new Map(), controllers = [];
  let disabled = false;
  const events = {
    addEventListener(type, callback) {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(callback);
    },
    removeEventListener(type, callback) { listeners.get(type)?.delete(callback); },
  };
  const document = { ...events, hidden: false };
  const load = createTypeScriptLoader({ document, window: events }, {
    './api': { DISABLE_ANIMATIONS_CHANGE_EVENT: 'animations', getDisableAnimationsPreference: () => disabled },
    '@formkit/auto-animate': { __esModule: true, default: (element, options) => {
      const controller = { element, options, disabled: 0, destroyed: 0,
        disable() { this.disabled++; }, destroy() { this.destroyed++; } };
      controllers.push(controller);
      return controller;
    } },
  });
  const { AnimatedList } = load(new URL('../src/components/AnimatedList.tsx', import.meta.url));
  const { PageActivityContext } = load(new URL('../src/components/PageActivity.tsx', import.meta.url));
  const node = {};
  let renderer;
  const view = (active = true, count = 3) => h(PageActivityContext.Provider, { value: active },
    h(AnimatedList, null, Array.from({ length: count }, (_, id) => h('div', { key: id }, String(id)))));
  const dispatch = type => act(() => { for (const callback of listeners.get(type) ?? []) callback(); });
  act(() => { renderer = create(view(), { createNodeMock: () => node }); });
  assert.equal(controllers.length, 1);
  assert.equal(controllers[0].element, node);
  assert.equal(controllers[0].options.duration, 200);
  act(() => renderer.update(view(false)));
  assert.equal(controllers[0].disabled, 1);
  assert.equal(controllers[0].destroyed, 1);
  act(() => renderer.update(view()));
  assert.equal(controllers.length, 2);
  document.hidden = true;
  dispatch('visibilitychange');
  assert.equal(controllers[1].destroyed, 1);
  document.hidden = false;
  dispatch('visibilitychange');
  assert.equal(controllers.length, 3);
  disabled = true;
  dispatch('animations');
  assert.equal(controllers[2].destroyed, 1);
  disabled = false;
  dispatch('animations');
  assert.equal(controllers.length, 4);
  act(() => renderer.update(view(true, 41)));
  assert.equal(controllers[3].destroyed, 1);
  act(() => renderer.update(view(true, 40)));
  assert.equal(controllers.length, 5);
  act(() => renderer.unmount());
  assert.equal(controllers[4].destroyed, 1);
  assert.equal([...listeners.values()].reduce((sum, set) => sum + set.size, 0), 0);
});

test('stable row keys survive edits, deletion of a middle row, reordering and duplicate values', () => {
  const load = createTypeScriptLoader();
  const { useStableListKeys } = load(new URL('../src/lib/useStableListKeys.ts', import.meta.url));
  let renderer, keys;
  function Probe({ items }) {
    keys = useStableListKeys(items);
    return null;
  }
  const a = { query: 'same' }, b = { query: 'same' }, c = { query: 'last' };
  act(() => { renderer = create(h(Probe, { items: [a, b, c] })); });
  const initial = [...keys];
  assert.equal(new Set(initial).size, 3);
  const editedB = { query: 'edited' };
  act(() => renderer.update(h(Probe, { items: [a, editedB, c] })));
  assert.deepEqual(keys, initial);
  act(() => renderer.update(h(Probe, { items: [a, c] })));
  assert.deepEqual(keys, [initial[0], initial[2]]);
  act(() => renderer.update(h(Probe, { items: [c, a] })));
  assert.deepEqual(keys, [initial[2], initial[0]]);
  act(() => renderer.update(h(Probe, { items: [c, a, { query: 'new' }] })));
  assert.deepEqual(keys.slice(0, 2), [initial[2], initial[0]]);
  assert.ok(!initial.includes(keys[2]));
  act(() => renderer.unmount());
});

test('a surviving row keeps its input state when an earlier row is removed', () => {
  const load = createTypeScriptLoader();
  const { useStableListKeys } = load(new URL('../src/lib/useStableListKeys.ts', import.meta.url));
  let renderer;
  function Row({ item }) {
    const [draft, setDraft] = useState(item.text);
    return h('input', { value: draft, onChange: event => setDraft(event.target.value) });
  }
  function List({ items }) {
    const keys = useStableListKeys(items);
    return items.map((item, index) => h(Row, { key: keys[index], item }));
  }
  const first = { text: 'one' }, second = { text: 'two' };
  act(() => { renderer = create(h(List, { items: [first, second] })); });
  act(() => renderer.root.findAllByType('input')[1].props.onChange({ target: { value: 'unsaved draft' } }));
  act(() => renderer.update(h(List, { items: [second] })));
  assert.equal(renderer.root.findByType('input').props.value, 'unsaved draft');
  act(() => renderer.unmount());
});

test('pausing collapse animations preserves an open form draft', () => {
  let enabled = true;
  const load = createTypeScriptLoader({}, {
    '../lib/useUiAnimations': { useUiAnimations: () => enabled },
  });
  const { AnimatedCollapse, UiMotionProvider } = load(new URL('../src/components/UiMotion.tsx', import.meta.url));
  function Draft() {
    const [value, setValue] = useState('initial');
    return h('input', { value, onChange: event => setValue(event.target.value) });
  }
  const view = () => h(UiMotionProvider, null, h(AnimatedCollapse, { open: true }, h(Draft)));
  let renderer;
  act(() => { renderer = create(view()); });
  act(() => renderer.root.findByType('input').props.onChange({ target: { value: 'unsaved' } }));
  enabled = false;
  act(() => renderer.update(view()));
  assert.equal(renderer.root.findByType('input').props.value, 'unsaved');
  enabled = true;
  act(() => renderer.update(view()));
  assert.equal(renderer.root.findByType('input').props.value, 'unsaved');
  act(() => renderer.unmount());
});

test('advanced disclosures retain invalid form drafts through close and reopen', () => {
  const load = createTypeScriptLoader({}, {
    '../lib/useUiAnimations': { useUiAnimations: () => false },
  });
  const { AnimatedDetails } = load(new URL('../src/components/AnimatedDetails.tsx', import.meta.url));
  function Draft() {
    const [value, setValue] = useState('{}');
    return h('textarea', { value, onChange: event => setValue(event.target.value) });
  }
  let renderer;
  act(() => { renderer = create(h(AnimatedDetails, { summary: 'Advanced' }, h(Draft))); });
  const button = () => renderer.root.findByType('button');
  const body = () => renderer.root.findAll(node => typeof node.type === 'string' && node.props.id === button().props['aria-controls'])[0];
  assert.equal(button().props['aria-expanded'], false);
  assert.equal(body().props['aria-hidden'], true);
  assert.equal(body().props.inert, '');
  act(() => button().props.onClick());
  act(() => renderer.root.findByType('textarea').props.onChange({ target: { value: '{unfinished' } }));
  act(() => button().props.onClick());
  act(() => button().props.onClick());
  assert.equal(button().props['aria-expanded'], true);
  assert.equal(body().props['aria-hidden'], undefined);
  assert.equal(body().props.inert, undefined);
  assert.equal(renderer.root.findByType('textarea').props.value, '{unfinished');
  act(() => renderer.unmount());
});

test('closed tool disclosures do not construct or mount their contents', () => {
  const load = createTypeScriptLoader({}, {
    '../lib/useUiAnimations': { useUiAnimations: () => false },
  });
  const { AnimatedCollapse } = load(new URL('../src/components/UiMotion.tsx', import.meta.url));
  let constructed = 0, renderer;
  const children = () => { constructed++; return h('div', null, 'tool details'); };
  act(() => { renderer = create(h(AnimatedCollapse, { open: false }, children)); });
  assert.equal(constructed, 0);
  act(() => renderer.update(h(AnimatedCollapse, { open: true }, children)));
  assert.ok(constructed > 0);
  assert.equal(renderer.root.findAllByType('div').at(-1).children[0], 'tool details');
  act(() => renderer.unmount());
});
