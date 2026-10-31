import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement as h } from 'react';
import { act, create } from 'react-test-renderer';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

test('scrolling near the top loads history once, preserves the visible message and pauses on failure or hiding', () => {
  const listeners = new Map();
  let active = true, loads = 0, anchorTop = 200;
  const viewport = {
    scrollHeight: 1200, clientHeight: 400, clientTop: 0, scrollTop: 0,
    getBoundingClientRect: () => ({ top: 0, bottom: 400 }),
    addEventListener(name, callback) {
      if (!listeners.has(name)) listeners.set(name, new Set());
      listeners.get(name).add(callback);
    },
    removeEventListener(name, callback) { listeners.get(name)?.delete(callback); },
    scrollTo({ top }) { this.scrollTop = top; },
  };
  const anchor = { isConnected: true, getBoundingClientRect: () => ({
    top: anchorTop - viewport.scrollTop, bottom: anchorTop - viewport.scrollTop + 100, height: 100,
  }) };
  const content = { querySelectorAll: selector => selector.includes('history-anchor') ? [anchor] : [] };
  const load = createTypeScriptLoader({
    ResizeObserver: class { observe() {} disconnect() {} },
    window: { matchMedia: () => ({ matches: true, addEventListener() {}, removeEventListener() {} }) },
    document: { hidden: false, documentElement: { dataset: {} }, addEventListener() {}, removeEventListener() {} },
  }, {
    '../../components/PageActivity': { usePageActive: () => active },
    '../agent/rows/AgentGroupView': { AgentGroupView: () => null },
  });
  const { Conversation } = load(new URL('../src/pages/extraction/Conversation.tsx', import.meta.url));
  let props = { sessionId: 'scroll-history', running: false, status: 'idle', pending: null,
    rows: [{ id: 'user-100', role: 'user', text: 'Visible message' }], older: true, beforeSeq: 100,
    loadingOlder: false, loadOlder: () => { loads += 1; } };
  let renderer;
  const update = changes => {
    props = { ...props, ...changes };
    act(() => renderer.update(h(Conversation, props)));
  };
  const scroll = top => act(() => {
    listeners.get('wheel')?.forEach(callback => callback({ deltaY: top - viewport.scrollTop }));
    viewport.scrollTop = top;
    listeners.get('scroll')?.forEach(callback => callback());
  });
  act(() => { renderer = create(h(Conversation, props), { createNodeMock: element =>
    element.props.className === 'agent-console__thread' ? viewport : element.props.className === 'agent-thread' ? content : null }); });
  assert.equal(viewport.scrollTop, 800);
  assert.equal(loads, 0);
  assert.equal(renderer.root.findByProps({ className: 'extract-older' }).type, 'div');
  scroll(200);
  assert.equal(loads, 0);
  scroll(100);
  assert.equal(loads, 1);
  scroll(90);
  assert.equal(loads, 1);
  update({ loadingOlder: true });
  assert.match(renderer.root.findByProps({ className: 'extract-older' }).children.join(''), /正在加载/);
  scroll(75);
  anchorTop += 300;
  viewport.scrollHeight += 300;
  update({ rows: [{ id: 'user-50', role: 'user', text: 'Earlier message' }, ...props.rows], beforeSeq: 50, loadingOlder: false });
  assert.equal(viewport.scrollTop, 375);
  assert.equal(loads, 1);
  scroll(0);
  assert.equal(loads, 2);
  update({ loadingOlder: true });
  update({ loadingOlder: false }); // Failed request retains its cursor and does not retry itself.
  assert.equal(loads, 2);
  scroll(20);
  assert.equal(loads, 3);
  update({ loadingOlder: true });
  active = false;
  update({});
  scroll(0);
  assert.equal(loads, 3);
  update({ loadingOlder: false });
  active = true;
  update({ older: false, beforeSeq: null });
  scroll(10);
  assert.equal(loads, 3);
  viewport.scrollHeight = 200;
  anchorTop = 20;
  update({ sessionId: 'short-history', older: true, beforeSeq: 20,
    rows: [{ id: 'user-20', role: 'user', text: 'Short collapsed history' }] });
  assert.equal(viewport.scrollTop, 0);
  assert.equal(loads, 3); // Opening a short conversation does not eagerly fetch every page.
  act(() => listeners.get('wheel')?.forEach(callback => callback({ deltaY: -120 })));
  assert.equal(loads, 4); // Upward input still loads when there is no scrollbar yet.
  act(() => renderer.unmount());
  assert.ok([...listeners.values()].every(callbacks => callbacks.size === 0));
});
