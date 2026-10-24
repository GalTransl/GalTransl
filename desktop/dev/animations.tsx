// Dev-only preview: real components, local fixtures, no project writes or model requests.
import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import { BackendConfigEditor } from '../src/components/BackendConfigEditor';
import { ProxyConfigEditor } from '../src/components/ProxyConfigEditor';
import { PageActivityContext } from '../src/components/PageActivity';
import { Button } from '../src/components/Button';
import { UiMotionProvider } from '../src/components/UiMotion';
import { PluginSettingsEditor } from '../src/components/PluginSettingsEditor';
import { ToolRow } from '../src/pages/agent/rows/ToolRow';
import { SubagentList } from '../src/pages/agent/rows/SubagentList';
import { ProjectCachePage } from '../src/pages/ProjectCachePage';
import { UiI18nProvider } from '../src/i18n';
import { applyAnimationsPreference, getDisableAnimationsPreference, setDisableAnimationsPreference } from '../src/lib/api';
import '../src/styles.css';
import '../src/styles/pages/agent.css';

const demoPlugin = {
  name: 'animation-demo', display_name: '展开动画示例', version: '1', author: '', description: '', type: 'text', module: '',
  settings: { extra: { enabled: true } },
  settings_schema: { extra: { label: '高级 JSON 配置', advanced: true } },
};

const entries = Array.from({ length: 80 }, (_, index) => ({
  index: index + 1, pre_src: `春の風が吹いている。${index + 1}`, post_src: `春の風が吹いている。${index + 1}`,
  pre_dst: `春风轻轻吹过。${index + 1}`, proofread_dst: `春风轻轻拂过。${index + 1}`,
}));
window.fetch = async (input) => {
  const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, location.href);
  const path = url.pathname;
  let data: unknown;
  if (path.endsWith('/cache/search')) data = { results: [{ filename: '春风.json', ...entries[24], src: entries[24].post_src, dst: entries[24].pre_dst }], total: 1 };
  else if (path.endsWith('/cache')) data = { files: [{ name: '春风.json', is_file: true, size: 1024, entry_count: entries.length }], cache_dir: '' };
  else if (path.includes('/cache/')) data = { entries: structuredClone(entries), has_cache: true };
  else if (path.endsWith('/problems')) data = { problems: [], filter_keys: [] };
  else if (path.endsWith('/name-dict')) data = { name_dict: {} };
  else if (path.endsWith('/openai-models')) data = { models: ['model-a', 'model-b', 'model-c'] };
  else throw new Error('预览页仅使用演示数据。');
  return new Response(JSON.stringify(data), { headers: { 'Content-Type': 'application/json' } });
};
applyAnimationsPreference();

function Preview() {
  const [tab, setTab] = useState(0);
  const [disabled, setDisabled] = useState(getDisableAnimationsPreference);
  const [overrides, setOverrides] = useState<Record<string, unknown>>({});
  const [backend, setBackend] = useState<Record<string, unknown>>({ 'OpenAI-Compatible': { tokens: [
    { token: 'demo-0001', endpoint: 'https://example.com/v1', modelName: 'model-a', contextWindow: 128000 },
    { token: 'demo-0002', endpoint: 'https://example.com/v1', modelName: 'model-b', contextWindow: 200000 },
  ] } });
  const [proxy, setProxy] = useState<Record<string, unknown>>({ enableProxy: true, proxies: [
    { address: 'http://127.0.0.1:7890', username: '', password: '' },
    { address: 'http://127.0.0.1:8080', username: '', password: '' },
  ] });
  return <div style={{ maxWidth: 1180, margin: '0 auto', padding: '24px 28px', height: '100vh', display: 'flex', flexDirection: 'column', gap: 18 }}>
    <header style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 16 }}>
      <div><h1 style={{ margin: '0 0 6px', fontSize: 24 }}>动画预览</h1><p style={{ margin: 0, color: 'var(--color-text-muted)' }}>试试添加、删除、展开卡片和打开菜单。演示数据不会写入项目配置。</p></div>
      <label style={{ display: 'flex', gap: 8, whiteSpace: 'nowrap' }}><input type="checkbox" checked={disabled} onChange={event => {
        setDisabled(event.target.checked); setDisableAnimationsPreference(event.target.checked); applyAnimationsPreference();
      }} />关闭动画</label>
    </header>
    <nav style={{ display: 'flex', gap: 10 }}>
      {['令牌配置', '代理列表', '缓存搜索', '更多展开'].map((label, index) => <Button key={label} variant={tab === index ? 'primary' : 'secondary'} onClick={() => setTab(index)}>{label}</Button>)}
    </nav>
    <main style={{ flex: 1, minHeight: 0 }}>
      {[0, 1, 2, 3].map(index => <PageActivityContext.Provider key={index} value={tab === index}>
        <section hidden={tab !== index} style={{ display: tab === index ? 'block' : 'none', height: '100%', overflow: index === 2 ? 'hidden' : 'auto', padding: index === 2 ? 0 : 20, borderRadius: 20, background: 'var(--color-surface)', border: index === 2 ? 'none' : '1px solid var(--color-line)' }}>
          {index === 0 ? <BackendConfigEditor config={backend} onChange={setBackend} /> : index === 1 ? <ProxyConfigEditor proxyConfig={proxy} onChange={setProxy} />
            : index === 2 ? <ProjectCachePage ctx={{ projectId: 'animation-preview', projectDir: 'preview', configFileName: 'config.yaml' }} active={tab === 2} />
            : <>
              <PluginSettingsEditor plugin={demoPlugin} overrides={overrides} onChange={(_name, key, value) => setOverrides(current => ({ ...current, [key]: value }))} />
              <div style={{ marginTop: 24 }}>
                <ToolRow persistKey="animation-preview" item={{ kind: 'tool', id: 'demo-search', step: 1, name: 'search_transl_cache', arguments: { query: '春风', field: 'pre_dst' }, ok: true, result: { total: 1, results: ['春风轻轻吹过。'] } }} />
                <SubagentList runs={[{ id: 'demo-review', agent: 'proofreader', label: '校对', file: '春风.json', indexes: '1-3', brief: '检查译文是否自然。', status: 'done', steps: [{ kind: 'text', text: '已检查人物语气和用词。' }], report: '三句译文都符合上下文。' }]} />
              </div>
            </>}
        </section>
      </PageActivityContext.Provider>)}
    </main>
  </div>;
}

createRoot(document.getElementById('root')!).render(<UiI18nProvider><UiMotionProvider><MemoryRouter><Preview /></MemoryRouter></UiMotionProvider></UiI18nProvider>);
