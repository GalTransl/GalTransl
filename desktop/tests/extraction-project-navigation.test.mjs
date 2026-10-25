import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createElement as h } from 'react';
import { act, create } from 'react-test-renderer';
import { createTypeScriptLoader } from './helpers/load-typescript.mjs';

for (const config of [null, 'config.yaml', 'config.yml']) {
  test(`workspace opens ${config || 'the new project wizard'} without changing existing project settings`, async () => {
    const navigated = [], opened = [], history = [], requests = [];
    const directory = 'C:\\Games\\Example\\Extracted';
    const session = { id: 'fixture', game_dir: 'C:\\Games\\Example', workspace: directory, status: 'idle' };
    const load = createTypeScriptLoader({ window: { innerWidth: 1600 } }, {
      '../styles/pages/agent.css': {}, '../styles/pages/extraction.css': {},
      'react-router-dom': { useNavigate: () => (...args) => navigated.push(args) },
      '../lib/api': {
        addOpenProject: (...args) => opened.push(args), encodeProjectDir: () => 'encoded-project',
        setSelectedBackendProfile: () => assert.fail('Opening an existing project must preserve its model selection'),
      },
      './HomePage': { addProjectToHistory: (...args) => history.push(args) },
      './agent/drafts': { useMessageDraft: () => ['', () => {}] },
      './agent/images': { useImageInput: () => ({ images: [] }) },
      './agent/composer': { StatusPill: () => null },
      './extraction/useModelProfile': { useModelProfile: () => ({ models: ['fixture'], profile: 'agent-profile' }) },
      './extraction/useExtraction': { useExtraction: () => ({
        selected: session.id, view: { session, rows: [] }, sessions: { total: 0, sessions: [] }, environment: { ready: true },
        mutate: async (...args) => { requests.push(args); return { project_dir: directory, config_file_name: config }; },
      }) },
      './extraction/SessionFolders': { SessionFolders: () => null },
      './extraction/Composer': { ExtractionComposer: () => null },
      './extraction/Workspace': { Workspace: props => h('button', { onClick: props.importProject, 'aria-label': 'open-project' }) },
    });
    const { ExtractionPage } = load(new URL('../src/pages/ExtractionPage.tsx', import.meta.url));
    let renderer;
    act(() => { renderer = create(h(ExtractionPage)); });
    await act(async () => { renderer.root.findByProps({ 'aria-label': 'open-project' }).props.onClick(); });
    assert.equal(requests.length, 1);
    assert.equal(requests[0][0], 'import');
    if (config) {
      assert.deepEqual(opened, [[directory, config]]);
      assert.deepEqual(history, [[directory, config]]);
      assert.deepEqual(navigated, [['/project/encoded-project/translate']]);
    } else {
      assert.equal(opened.length, 0);
      assert.equal(history.length, 0);
      assert.equal(navigated[0][0], '/new-project');
      assert.equal(navigated[0][1].state.newProjectWizardDirectory, directory);
    }
    act(() => renderer.unmount());
  });
}
