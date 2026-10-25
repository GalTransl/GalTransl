import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement as h } from 'react';
import { act, create } from 'react-test-renderer';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

const load = createTypeScriptLoader({ URLSearchParams });
const { projectDirectoryPrefill, splitWizardReferences, wizardDirectoryFromState } = load(new URL('../src/lib/newProjectWizard.ts', import.meta.url));

test('wizard references accept native and JSON-escaped absolute directory paths without evaluating them', () => {
  for (const directory of ['C:\\exports\\游戏', 'C:\\new\\test', 'C:/exports/游戏/', '\\\\server\\share\\游戏', '/home/user/游戏/']) {
    for (const argument of [JSON.stringify(directory), `"${directory}"`]) {
      const segments = splitWizardReferences(`Ready\n@new_project_wizard(${argument})\nContinue`);
      assert.equal(segments[0].text, 'Ready\n');
      assert.equal(segments[1].kind, 'wizard');
      assert.equal(segments[1].directory, directory);
      assert.equal(segments[2].text, '\nContinue');
    }
  }
  assert.deepEqual({ ...projectDirectoryPrefill('C:\\exports\\游戏\\') }, { parentDir: 'C:\\exports', projectName: '游戏' });
  assert.deepEqual({ ...projectDirectoryPrefill('/游戏') }, { parentDir: '/', projectName: '游戏' });
  for (const directory of ['', 'relative/game', '/', 'C:\\', '/home/..', '/home/CON', '/home/line\nbreak']) {
    assert.equal(projectDirectoryPrefill(directory), null);
  }
  for (const text of ['@new_project_wizard("/home/game"', '@new_project_wizard("relative")', '@new_project_wizard(alert("/game"))']) {
    assert.equal(splitWizardReferences(text).every(segment => segment.kind === 'text'), true);
  }
  assert.equal(splitWizardReferences('@new_project_wizard("/a") @new_project_wizard("/b")').filter(segment => segment.kind === 'wizard').length, 2);
  assert.equal(wizardDirectoryFromState({ newProjectWizardDirectory: '/games/游戏' }), '/games/游戏');
  assert.equal(wizardDirectoryFromState({ newProjectWizardDirectory: {} }), undefined);
});

function LocationProbe() { const location = useLocation(); return h('location', { path: location.pathname, state: location.state }); }

test('replayed extraction replies render a project button that passes the directory to the wizard', () => {
  const { AgentGroupView } = load(new URL('../src/pages/agent/rows/AgentGroupView.tsx', import.meta.url));
  const { extractionTimeline } = load(new URL('../src/pages/extraction/presentation.ts', import.meta.url));
  const { reduceEvents } = load(new URL('../src/pages/extraction/transcript.ts', import.meta.url));
  const text = '提取完成。\n@new_project_wizard("C:\\\\exports\\\\游戏")\n请继续配置翻译。';
  const rows = reduceEvents([], [
    { seq: 1, type: 'user_message', text: 'Extract' },
    { seq: 2, type: 'assistant_message', messageId: 'answer', text, stopReason: 'stop' },
  ]);
  const group = extractionTimeline(rows, false, null)[1];
  let renderer;
  const view = extraction => h(MemoryRouter, { initialEntries: ['/extraction'] },
    h(AgentGroupView, { group, isLive: false, projectDir: '', persistKey: 'ref', newProjectWizardRefs: extraction, imageScope: { sessionId: 'session', extraction } }), h(LocationProbe));
  act(() => { renderer = create(view(true)); });
  const button = renderer.root.findByProps({ className: 'extract-project-wizard-ref' });
  assert.equal(button.findByProps({ className: 'extract-project-wizard-ref__label' }).children.join(''), '创建翻译项目');
  assert.equal(button.findByProps({ className: 'extract-project-wizard-ref__path' }).children.join(''), 'C:\\exports\\游戏');
  assert.equal(renderer.root.findByType('location').props.path, '/extraction');
  act(() => button.props.onClick());
  assert.equal(renderer.root.findByType('location').props.path, '/new-project');
  assert.equal(renderer.root.findByType('location').props.state.newProjectWizardDirectory, 'C:\\exports\\游戏');
  act(() => renderer.unmount());
  act(() => { renderer = create(view(false)); });
  assert.equal(renderer.root.findAllByProps({ className: 'extract-project-wizard-ref' }).length, 0);
  act(() => renderer.unmount());
});
