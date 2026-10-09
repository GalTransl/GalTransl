import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement as h, useEffect, useState } from 'react';
import { act, create } from 'react-test-renderer';
import { MemoryRouter, Route, useLocation, useNavigate, useParams } from 'react-router-dom';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

const load = createTypeScriptLoader();
const { CachedRoutes, RECENT_PAGE_LIMIT } = load(new URL('../src/components/CachedRoutes.tsx', import.meta.url));
const { usePageActive, useRetainPage } = load(new URL('../src/components/PageActivity.tsx', import.meta.url));

function fixture() {
  let navigate, setAvailable;
  const mounts = new Map();
  const unmounts = [];
  function Page() {
    const location = useLocation();
    const { id } = useParams();
    const active = usePageActive();
    const [text, setText] = useState('');
    const [busy, setBusy] = useState(false);
    useRetainPage(busy);
    useEffect(() => {
      mounts.set(id, (mounts.get(id) || 0) + 1);
      return () => { unmounts.push(id); };
    }, []);
    return h('input', { id, active, location, value: text, onChange: setText, setBusy });
  }
  function App() {
    const location = useLocation();
    navigate = useNavigate();
    const [available, update] = useState(() => () => true);
    setAvailable = (fn) => update(() => fn);
    return h(CachedRoutes, { location, currentPath: location.pathname, isAvailable: available },
      h(Route, { path: '/page/:id', element: h(Page) }));
  }
  let renderer;
  act(() => { renderer = create(h(MemoryRouter, { initialEntries: ['/page/0'] }, h(App))); });
  return {
    renderer, mounts, unmounts,
    go: (path) => act(() => navigate(path)),
    restrict: (fn) => act(() => setAvailable(fn)),
    page: (id) => renderer.root.findAllByType('input').find((node) => node.props.id === String(id)),
    dispose: () => act(() => renderer.unmount()),
  };
}

test('ten recent routes preserve component state and frozen location/params; revisit updates LRU', () => {
  const f = fixture();
  assert.equal(RECENT_PAGE_LIMIT, 10);
  act(() => f.page(0).props.onChange('unfinished'));
  for (let i = 1; i < 10; i++) f.go(`/page/${i}`);
  assert.equal(f.page(0).props.value, 'unfinished');
  assert.equal(f.page(0).props.location.pathname, '/page/0');
  assert.equal(f.page(0).props.active, false);
  assert.equal(f.page(9).props.active, true);
  f.go('/page/0');
  assert.equal(f.mounts.get('0'), 1);
  assert.equal(f.page(0).props.value, 'unfinished');
  f.go('/page/10');
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

test('scroll positions survive a WebView resetting hidden containers, including nested scrollers', () => {
  const f = fixture();
  const container = () => f.renderer.root.findAllByProps({ className: 'app-layout__page' })
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
  act(() => f.page(0).props.setBusy(true));
  for (let i = 1; i <= 10; i++) f.go(`/page/${i}`);
  assert.ok(f.page(0));
  assert.equal(f.renderer.root.findAllByType('input').length, 11);
  act(() => f.page(0).props.setBusy(false));
  assert.equal(f.page(0), undefined);
  act(() => f.page(1).props.setBusy(true));
  f.restrict((path) => path !== '/page/1');
  assert.equal(f.page(1), undefined);
  f.go('/new-project');
  assert.equal(f.renderer.root.findAllByType('input').some((page) => page.props.active), false);
  f.dispose();
});
