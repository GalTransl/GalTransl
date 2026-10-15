import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement as h, useEffect, useState } from 'react';
import { act, create } from 'react-test-renderer';
import { MemoryRouter, Route, useLocation, useNavigate, useParams } from 'react-router-dom';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

const load = createTypeScriptLoader();
const { CachedRoutes, RECENT_PAGE_LIMIT } = load(new URL('../src/components/CachedRoutes.tsx', import.meta.url));
const { usePageActive, useRetainPage } = load(new URL('../src/components/PageActivity.tsx', import.meta.url));

function fixture(limit = RECENT_PAGE_LIMIT) {
  let navigate, setAvailable, setLeavingPath;
  const mounts = new Map();
  const renders = new Map();
  const unmounts = [];
  function Page() {
    const location = useLocation();
    const { id } = useParams();
    const pageNavigate = useNavigate();
    renders.set(id, (renders.get(id) || 0) + 1);
    const active = usePageActive();
    const [text, setText] = useState('');
    const [busy, setBusy] = useState(false);
    useRetainPage(busy);
    useEffect(() => {
      mounts.set(id, (mounts.get(id) || 0) + 1);
      return () => { unmounts.push(id); };
    }, []);
    return h('input', { id, active, location, value: text, onChange: setText, setBusy, navigate: pageNavigate });
  }
  const routes = h(Route, { path: '/page/:id', element: h(Page) });
  function App() {
    const location = useLocation();
    navigate = useNavigate();
    const [available, update] = useState(() => () => true);
    const [leavingPath, updateLeaving] = useState(null);
    setLeavingPath = updateLeaving;
    setAvailable = (fn) => update(() => fn);
    return h(CachedRoutes, { location, currentPath: location.pathname, leavingPath, limit, isAvailable: available }, routes);
  }
  let renderer;
  act(() => { renderer = create(h(MemoryRouter, { initialEntries: ['/page/0'] }, h(App))); });
  return {
    renderer, mounts, unmounts, renders,
    go: (path, options) => act(() => navigate(path, options)),
    transition: (from, to) => act(() => { setLeavingPath(from); navigate(to); }),
    finishTransition: () => act(() => setLeavingPath(null)),
    restrict: (fn) => act(() => setAvailable(fn)),
    page: (id) => renderer.root.findAllByType('input').find((node) => node.props.id === String(id)),
    dispose: () => act(() => renderer.unmount()),
  };
}

test('unrelated navigation does not render hidden cached routes', () => {
  const f = fixture();
  f.go('/page/1');
  const hiddenRenders = f.renders.get('0');
  f.go('/page/2');
  assert.equal(f.renders.get('0'), hiddenRenders);
  act(() => f.page(2).props.setBusy(true));
  assert.equal(f.renders.get('0'), hiddenRenders);
  f.go('/page/0');
  assert.equal(f.page(0).props.active, true);
  assert.equal(f.mounts.get('0'), 1);
  f.dispose();
});

test('navigation inside a cached page reaches the outer router and keeps local URL updates', () => {
  const f = fixture();
  act(() => f.page(0).props.navigate('/page/1'));
  assert.equal(f.page(1).props.active, true);
  act(() => f.page(1).props.navigate('?section=settings#row'));
  assert.equal(f.page(1).props.location.pathname, '/page/1');
  assert.equal(f.page(1).props.location.search, '?section=settings');
  assert.equal(f.page(1).props.location.hash, '#row');
  assert.equal(f.page(0).props.location.search, '');
  assert.equal(f.mounts.get('1'), 1);
  f.dispose();
});

test('recent routes preserve component state and frozen location/params; revisit updates LRU', () => {
  const f = fixture();
  assert.equal(RECENT_PAGE_LIMIT, 5);
  act(() => f.page(0).props.onChange('unfinished'));
  for (let i = 1; i < RECENT_PAGE_LIMIT; i++) f.go(`/page/${i}`);
  assert.equal(f.page(0).props.value, 'unfinished');
  assert.equal(f.page(0).props.location.pathname, '/page/0');
  assert.equal(f.page(0).props.active, false);
  assert.equal(f.page(RECENT_PAGE_LIMIT - 1).props.active, true);
  // 回访把 0 号页刷成最近使用：再开新页时被驱逐的应该是 1 号页
  f.go('/page/0');
  assert.equal(f.mounts.get('0'), 1);
  assert.equal(f.page(0).props.value, 'unfinished');
  f.go(`/page/${RECENT_PAGE_LIMIT}`);
  assert.equal(f.page(1), undefined);
  assert.ok(f.page(0));
  assert.deepEqual(f.unmounts, ['1']);
  f.dispose();
});

test('query/hash navigation updates the existing page; back navigation restores its URL', () => {
  const f = fixture();
  act(() => f.page(0).props.onChange('draft'));
  f.go('/page/0?q=search#row');
  assert.equal(f.page(0).props.location.search, '?q=search');
  assert.equal(f.page(0).props.location.hash, '#row');
  assert.equal(f.page(0).props.value, 'draft');
  assert.equal(f.mounts.get('0'), 1);
  f.go(-1);
  assert.equal(f.page(0).props.location.search, '');
  assert.equal(f.mounts.get('0'), 1);
  f.dispose();
});

test('same-URL navigation updates route state and history key without remounting', () => {
  const f = fixture();
  const initialKey = f.page(0).props.location.key;
  f.go('/page/0', { state: { source: 'new-navigation' } });
  assert.equal(f.page(0).props.location.state.source, 'new-navigation');
  assert.notEqual(f.page(0).props.location.key, initialKey);
  assert.equal(f.mounts.get('0'), 1);
  f.go(-1);
  assert.equal(f.page(0).props.location.state, null);
  assert.equal(f.page(0).props.location.key, initialKey);
  f.dispose();
});

test('outgoing pages remain visible but inert until a cross-fade finishes, even with a one-page cache', () => {
  const f = fixture(1);
  f.transition('/page/0', '/page/1');
  const page = id => f.renderer.root.findAllByType('div').find(node =>
    String(node.props.className || '').split(' ').includes('app-layout__page')
      && node.findAllByType('input').some(input => input.props.id === id));
  assert.equal(page('0').props.hidden, false);
  assert.equal(page('0').props.inert, '');
  assert.equal(page('0').props['aria-hidden'], true);
  assert.equal(f.page(0).props.active, false);
  assert.equal(page('1').props.hidden, false);
  assert.equal(page('1').props.inert, undefined);
  assert.equal(f.page(1).props.active, true);
  f.finishTransition();
  assert.equal(f.page(0), undefined);
  assert.ok(f.page(1));
  f.dispose();
});

test('scroll positions survive a WebView resetting hidden containers, including nested scrollers', () => {
  const f = fixture();
  // 页面容器现在还带交叉淡化的状态类（app-layout__page--entering/--leaving），按 class 列表匹配
  const container = () => f.renderer.root
    .findAll(node => String(node.props?.className || '').split(' ').includes('app-layout__page'))
    .find(node => node.findAllByType('input').some(input => input.props.id === '0'));
  const outer = { isConnected: true, scrollTop: 1200, scrollLeft: 0, hasAttribute: () => false };
  const inner = { isConnected: true, scrollTop: 150, scrollLeft: 30, hasAttribute: () => false };
  container().props.onScrollCapture({ target: outer });
  container().props.onScrollCapture({ target: inner });
  f.go('/page/1');
  outer.scrollTop = inner.scrollTop = inner.scrollLeft = 0;
  container().props.onScrollCapture({ target: outer });
  f.go('/page/0');
  assert.equal(outer.scrollTop, 1200);
  assert.equal(inner.scrollTop, 150);
  assert.equal(inner.scrollLeft, 30);
  f.dispose();
});

test('unfinished work survives eviction then releases; closed pages release even when retained', () => {
  const f = fixture();
  // 0 号页有未完成工作 → retain：跌出最近窗口后仍然挂着
  act(() => f.page(0).props.setBusy(true));
  for (let i = 1; i <= RECENT_PAGE_LIMIT; i++) f.go(`/page/${i}`);
  assert.ok(f.page(0));
  assert.equal(f.renderer.root.findAllByType('input').length, RECENT_PAGE_LIMIT + 1);
  // 释放后立刻被驱逐
  act(() => f.page(0).props.setBusy(false));
  assert.equal(f.page(0), undefined);

  // 先 retain 窗口内的页，再把它挤出窗口：同样保留
  const retained = RECENT_PAGE_LIMIT;
  act(() => f.page(retained).props.setBusy(true));
  for (let i = RECENT_PAGE_LIMIT + 1; i <= RECENT_PAGE_LIMIT * 2; i++) f.go(`/page/${i}`);
  assert.ok(f.page(retained));

  // isAvailable 变 false（项目被关掉）时，即使被 retain 也要释放
  f.restrict((path) => path !== `/page/${retained}`);
  assert.equal(f.page(retained), undefined);

  f.go('/new-project');
  assert.equal(f.renderer.root.findAllByType('input').some((page) => page.props.active), false);
  f.dispose();
});

test('retained pages outside the recent window remain mounted until released', () => {
  const f = fixture();
  // Retention protects unfinished work even when many pages are open.
  const busyCount = RECENT_PAGE_LIMIT + 2;
  for (let i = 0; i < busyCount; i++) {
    if (i > 0) f.go(`/page/${i}`);
    act(() => f.page(i).props.setBusy(true));
  }
  const last = busyCount + RECENT_PAGE_LIMIT;
  for (let i = busyCount; i <= last; i++) f.go(`/page/${i}`);
  assert.equal(f.renderer.root.findAllByType('input').length, busyCount + RECENT_PAGE_LIMIT);
  assert.ok(f.page(0));
  assert.ok(f.page(busyCount - 1));
  f.dispose();
});
