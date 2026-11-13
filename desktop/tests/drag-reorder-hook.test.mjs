import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement } from 'react';
import { act, create } from 'react-test-renderer';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

/* 没有 DOM：用最小的假 window / 元素驱动 useDragReorder，守护"按下即可拖动"的手势
   （以前要长按 350ms 才能拖，按下移动会直接取消）。 */
function setup(keys = ['a', 'b', 'c']) {
  const listeners = new Map();
  const on = (type, fn) => listeners.set(type, [...(listeners.get(type) || []), fn]);
  const off = (type, fn) => listeners.set(type, (listeners.get(type) || []).filter((item) => item !== fn));
  const fire = (type, event) => (listeners.get(type) || []).forEach((fn) => fn(event));
  const fakeWindow = {
    addEventListener: on, removeEventListener: off, setTimeout, clearTimeout,
    requestAnimationFrame: () => 1, cancelAnimationFrame() {}, getSelection: () => null,
  };
  const scroller = {
    scrollTop: 0, scrollLeft: 0, attributes: new Set(),
    setAttribute(name) { this.attributes.add(name); }, removeAttribute(name) { this.attributes.delete(name); },
    addEventListener() {}, removeEventListener() {},
    getBoundingClientRect: () => ({ top: 0, bottom: 1000, left: 0, right: 200 }),
  };
  const globals = {
    window: fakeWindow,
    document: { body: { style: { userSelect: '' } } },
    getComputedStyle: () => ({ flexDirection: 'column', rowGap: '0px', columnGap: '0px' }),
  };
  const { useDragReorder } = createTypeScriptLoader(globals)(new URL('../src/components/useDragReorder.ts', import.meta.url));
  const reorders = [];
  let api;
  function Probe() {
    api = useDragReorder({ keys, onReorder: (key, toIndex) => reorders.push([key, toIndex]), handle: '.handle' });
    return null;
  }
  let renderer;
  act(() => { renderer = create(createElement(Probe)); });
  // 每个分组 40px 高，纵向排列
  keys.forEach((key, index) => api.itemRef(key)({ getBoundingClientRect: () => ({ top: index * 40, height: 40, left: 0, width: 200 }) }));
  const handle = { closest: (selector) => (selector === '.handle' ? handle : null) };
  const press = (key, y) => act(() => api.onPointerDown({
    button: 0, isPrimary: true, pointerId: 1, clientX: 10, clientY: y, target: handle,
    currentTarget: { contains: () => true, parentElement: scroller },
  }, key));
  const move = (y) => act(() => fire('pointermove', { pointerId: 1, clientX: 10, clientY: y, preventDefault() {} }));
  const release = () => act(() => fire('pointerup', { pointerId: 1 }));
  return { api: () => api, reorders, scroller, press, move, release, dispose: () => act(() => renderer.unmount()) };
}

test('pressing a folder and moving starts dragging right away, and releasing reorders it', () => {
  const f = setup();
  f.press('a', 20);
  f.move(26);
  assert.equal(f.api().drag?.key, 'a');
  assert.ok(f.scroller.attributes.has('data-reordering'));
  f.move(110);
  assert.equal(f.api().drag.toIndex, 2);
  f.release();
  assert.deepEqual(f.reorders, [['a', 2]]);
  assert.equal(f.api().drag, null);
  assert.ok(!f.scroller.attributes.has('data-reordering'));
  f.dispose();
});

test('a press without movement stays a plain click and never reorders', () => {
  const f = setup();
  f.press('b', 60);
  f.move(62);
  assert.equal(f.api().drag, null);
  f.release();
  assert.deepEqual(f.reorders, []);
  f.dispose();
});
