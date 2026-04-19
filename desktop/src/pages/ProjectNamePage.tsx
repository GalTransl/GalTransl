import { useCallback, useEffect, useRef, useState } from 'react';
import type { ProjectPageContext } from '../components/ProjectLayout';
import { Button } from '../components/Button';
import { CustomSelect } from '../components/CustomSelect';
import { PageHeader } from '../components/PageHeader';
import { Panel } from '../components/Panel';
import { EmptyState, InlineFeedback, LoadingState } from '../components/page-state';
import {
  type NameEntry,
  type Job,
  fetchNameTable,
  submitJob,
  fetchJob,
  saveNameTable,
  getAiTranslateUrl,
  fetchBackendProfiles,
  getSelectedBackendProfile,
  DEFAULT_BACKEND_PROFILE_CHANGE_EVENT,
  fetchProjectConfig,
  updateProjectConfig,
  fetchProjectDictionaryManager,
} from '../lib/api';
import { normalizeError } from '../lib/errors';

const JOB_POLL_INTERVAL_MS = 1500;

export function ProjectNamePage({ ctx }: { ctx: ProjectPageContext }) {
  const { projectId, projectDir, configFileName } = ctx;

  const [names, setNames] = useState<NameEntry[]>([]);
  const [sourceFile, setSourceFile] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState(false);
  const [saving, setSaving] = useState(false);
  const [aiTranslating, setAiTranslating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const searchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const autoSaveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const namesRef = useRef<NameEntry[]>([]);
  const aiTranslateAbortRef = useRef<AbortController | null>(null);

  // GPT dict integration state
  const [useGptDictForName, setUseGptDictForName] = useState(false);
  const [gptDictNameMap, setGptDictNameMap] = useState<Map<string, string>>(new Map());
  const [gptToggleBusy, setGptToggleBusy] = useState(false);

  // AI translate popover state
  const [showAiPopover, setShowAiPopover] = useState(false);
  const [aiProfileNames, setAiProfileNames] = useState<string[]>([]);
  const [aiProfileModelMap, setAiProfileModelMap] = useState<Record<string, string>>({});
  const [aiSelectedProfile, setAiSelectedProfile] = useState('');
  const aiPopoverRef = useRef<HTMLDivElement>(null);

  // Debounced search
  useEffect(() => {
    if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    searchTimerRef.current = setTimeout(() => setDebouncedSearch(searchQuery), 300);
    return () => { if (searchTimerRef.current) clearTimeout(searchTimerRef.current); };
  }, [searchQuery]);

  // Close popover on outside click
  useEffect(() => {
    if (!showAiPopover) return;
    const handler = (e: MouseEvent) => {
      if (aiPopoverRef.current && !aiPopoverRef.current.contains(e.target as Node)) {
        setShowAiPopover(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [showAiPopover]);

  // React to global default backend profile changes
  useEffect(() => {
    const handler = () => {
      if (projectDir) {
        const newName = getSelectedBackendProfile(projectDir);
        setAiSelectedProfile((prev) => {
          // Only update if the project has no explicit selection (prev follows default)
          // or always update to keep in sync with the global default
          return newName !== prev ? newName : prev;
        });
      }
    };
    window.addEventListener(DEFAULT_BACKEND_PROFILE_CHANGE_EVENT, handler);
    return () => window.removeEventListener(DEFAULT_BACKEND_PROFILE_CHANGE_EVENT, handler);
  }, [projectDir]);

  const loadData = useCallback(async () => {
    if (!projectId) return;
    setLoading(true);
    setError(null);
    try {
      const res = await fetchNameTable(projectId);
      setNames(res.names);
      setSourceFile(res.source_file);
      setDirty(false);
    } catch (err) {
      setError(normalizeError(err, '加载人名表失败'));
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void loadData();
  }, [loadData]);

  // Cleanup poll timer on unmount
  useEffect(() => {
    return () => { if (pollTimerRef.current) clearTimeout(pollTimerRef.current); };
  }, []);

  // Cleanup auto-save timer on unmount
  useEffect(() => {
    return () => { if (autoSaveTimerRef.current) clearTimeout(autoSaveTimerRef.current); };
  }, []);

  useEffect(() => {
    return () => { aiTranslateAbortRef.current?.abort(); };
  }, []);

  // Parse GPT dictionary lines into a search_word -> replace_word Map.
  // Mirrors the parsing logic in GalTransl/Dictionary.py CGptDict.load_dic.
  const parseGptDictLines = useCallback((allLines: string[]): Map<string, string> => {
    const map = new Map<string, string>();
    for (const raw of allLines) {
      if (!raw || raw.startsWith('\n')) continue;
      // Skip comment lines
      const trimmedLeft = raw.replace(/^\s+/, '');
      if (trimmedLeft.startsWith('//') || trimmedLeft.startsWith('\\\\')) continue;
      // Tolerate 4-space as tab, and "src->dst #note" form
      let line = raw.replace(/    /g, '\t');
      if (line.includes('->')) {
        line = line.replace('->', '\t').replace('#', '\t');
      }
      const parts = line.replace(/[\r\n]+$/, '').split('\t');
      if (parts.length < 2) continue;
      const src = parts[0].trim();
      const dst = parts[1].trim();
      if (!src || !dst) continue;
      if (!map.has(src)) map.set(src, dst);
    }
    return map;
  }, []);

  // Load project GPT dictionaries (project-scoped files) and build name map.
  const loadProjectGptDictMap = useCallback(async (): Promise<Map<string, string>> => {
    if (!projectId) return new Map();
    const res = await fetchProjectDictionaryManager(projectId, configFileName || 'config.yaml');
    const lines: string[] = [];
    for (const fileKey of res.gpt_dict_files) {
      const content = res.dict_contents[fileKey];
      if (content && Array.isArray(content.lines)) {
        lines.push(...content.lines);
      }
    }
    return parseGptDictLines(lines);
  }, [projectId, configFileName, parseGptDictLines]);

  // Overlay GPT dict translations onto empty dst_name entries.
  const overlayGptDictOntoNames = useCallback(
    (list: NameEntry[], dictMap: Map<string, string>): { next: NameEntry[]; changed: number } => {
      let changed = 0;
      const next = list.map((entry) => {
        if (entry.dst_name.trim() !== '') return entry;
        const src = entry.src_name.trim();
        if (!src) return entry;
        const mapped = dictMap.get(src);
        if (!mapped) return entry;
        changed++;
        return { ...entry, dst_name: mapped };
      });
      return { next, changed };
    },
    [],
  );

  // Load initial useGPTDictInName toggle state from project config,
  // and preload the dict map if already enabled (so extraction/generate also overlays).
  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    (async () => {
      try {
        const res = await fetchProjectConfig(projectId, configFileName || 'config.yaml');
        const dictCfg = (res.config?.dictionary ?? {}) as Record<string, unknown>;
        const enabled = Boolean(dictCfg.useGPTDictInName);
        if (cancelled) return;
        setUseGptDictForName(enabled);
        if (enabled) {
          const map = await loadProjectGptDictMap();
          if (cancelled) return;
          setGptDictNameMap(map);
        }
      } catch {
        // Non-critical: leave toggle off if config can't be read
      }
    })();
    return () => { cancelled = true; };
  }, [projectId, configFileName, loadProjectGptDictMap]);

  // Toggle handler: persists config flag and applies/removes overlay.
  const handleToggleGptDictForName = useCallback(async (nextEnabled: boolean) => {
    if (!projectId || gptToggleBusy) return;
    setGptToggleBusy(true);
    setError(null);
    try {
      // 1) Read current config, set dictionary.useGPTDictInName, write back.
      const current = await fetchProjectConfig(projectId, configFileName || 'config.yaml');
      const newConfig = { ...(current.config as Record<string, unknown>) };
      const dictCfg = { ...((newConfig.dictionary as Record<string, unknown>) ?? {}) };
      dictCfg.useGPTDictInName = nextEnabled;
      newConfig.dictionary = dictCfg;
      await updateProjectConfig(projectId, {
        config: newConfig,
        config_file_name: configFileName || 'config.yaml',
      });
      setUseGptDictForName(nextEnabled);

      // 2) If turning on, load project GPT dict and overlay onto empty dst_name.
      if (nextEnabled) {
        const map = await loadProjectGptDictMap();
        setGptDictNameMap(map);
        if (map.size > 0 && namesRef.current.length > 0) {
          const { next, changed } = overlayGptDictOntoNames(namesRef.current, map);
          if (changed > 0) {
            setNames(next);
            setDirty(true);
          }
        }
      } else {
        setGptDictNameMap(new Map());
      }
    } catch (err) {
      setError(normalizeError(err, '切换 GPT 字典用于人名失败'));
    } finally {
      setGptToggleBusy(false);
    }
  }, [projectId, configFileName, gptToggleBusy, loadProjectGptDictMap, overlayGptDictOntoNames]);

  const handleGenerate = useCallback(async () => {
    if (!projectId || !projectDir) return;
    setGenerating(true);
    setError(null);
    try {
      const job = await submitJob({
        project_dir: projectDir,
        config_file_name: configFileName || 'config.yaml',
        translator: 'dump-name',
      });

      const pollJob = async (jobId: string): Promise<Job> => {
        const j = await fetchJob(jobId);
        if (j.status === 'pending' || j.status === 'running') {
          await new Promise<void>((resolve) => {
            pollTimerRef.current = setTimeout(resolve, JOB_POLL_INTERVAL_MS);
          });
          return pollJob(jobId);
        }
        return j;
      };

      const finished = await pollJob(job.job_id);

      if (finished.status === 'failed') {
        setError(`生成人名表失败: ${finished.error || '未知错误'}`);
      } else if (finished.status === 'cancelled') {
        setError('生成人名表已被取消');
      } else {
        const res = await fetchNameTable(projectId);
        let nextNames = res.names;
        let nextDirty = false;
        // If GPT-dict-for-name is enabled, overlay newly extracted names with GPT dict mappings.
        if (useGptDictForName && gptDictNameMap.size > 0) {
          const { next, changed } = overlayGptDictOntoNames(nextNames, gptDictNameMap);
          if (changed > 0) {
            nextNames = next;
            nextDirty = true;
          }
        }
        setNames(nextNames);
        setSourceFile(res.source_file);
        setDirty(nextDirty);
      }
    } catch (err) {
      setError(normalizeError(err, '生成人名表失败'));
    } finally {
      setGenerating(false);
    }
  }, [projectId, projectDir, configFileName, useGptDictForName, gptDictNameMap, overlayGptDictOntoNames]);

  // Keep a ref in sync with names for safe access in async save handlers
  useEffect(() => {
    namesRef.current = names;
  }, [names]);

  const handleSave = useCallback(async () => {
    if (!projectId) return;
    if (autoSaveTimerRef.current) {
      clearTimeout(autoSaveTimerRef.current);
      autoSaveTimerRef.current = null;
    }
    const snapshot = namesRef.current;
    setSaving(true);
    setError(null);
    try {
      await saveNameTable(projectId, snapshot);
      // Only clear dirty when no edits happened during the save
      if (namesRef.current === snapshot) {
        setDirty(false);
      }
    } catch (err) {
      setError(normalizeError(err, '保存人名表失败'));
    } finally {
      setSaving(false);
    }
  }, [projectId]);

  // Auto-save when dirty: debounce 1s after the last change
  useEffect(() => {
    if (!dirty || saving) return;
    if (autoSaveTimerRef.current) clearTimeout(autoSaveTimerRef.current);
    autoSaveTimerRef.current = setTimeout(() => {
      void handleSave();
    }, 1000);
    return () => {
      if (autoSaveTimerRef.current) {
        clearTimeout(autoSaveTimerRef.current);
        autoSaveTimerRef.current = null;
      }
    };
  }, [dirty, saving, names, handleSave]);

  // Open AI translate popover — load profiles & preselect default
  const handleOpenAiPopover = useCallback(() => {
    if (names.filter((n) => n.dst_name.trim() === '').length === 0) {
      setError('所有人名已翻译，无需AI翻译');
      return;
    }
    fetchBackendProfiles()
      .then((data) => {
        const profiles = data.profiles || {};
        const profileKeys = Object.keys(profiles);
        setAiProfileNames(profileKeys);
        // Extract model name for each profile
        const modelMap: Record<string, string> = {};
        for (const [name, cfg] of Object.entries(profiles)) {
          const c = cfg as Record<string, unknown>;
          const oai = typeof c['OpenAI-Compatible'] === 'object' && c['OpenAI-Compatible'] !== null
            ? c['OpenAI-Compatible'] as Record<string, unknown> : null;
          const tokens = Array.isArray(oai?.tokens) ? oai!.tokens : [];
          const firstToken = tokens.length > 0 && typeof tokens[0] === 'object' && tokens[0] !== null
            ? tokens[0] as Record<string, unknown> : null;
          const modelName = (firstToken?.modelName as string) || '';
          modelMap[name] = modelName;
        }
        setAiProfileModelMap(modelMap);
        const defaultName = getSelectedBackendProfile(projectDir);
        setAiSelectedProfile(defaultName && profileKeys.includes(defaultName) ? defaultName : (profileKeys[0] || ''));
        setShowAiPopover(true);
      })
      .catch(() => {
        setError('加载后端配置失败');
      });
  }, [names, projectDir]);

  const handleAiTranslate = useCallback(async () => {
    if (!projectId || aiTranslating) return;
    const untranslated = names.filter((n) => n.dst_name.trim() === '');
    if (untranslated.length === 0) {
      setError('所有人名已翻译，无需AI翻译');
      return;
    }
    setShowAiPopover(false);
    setAiTranslating(true);
    setError(null);
    const abortController = new AbortController();
    aiTranslateAbortRef.current = abortController;
    let aborted = false;
    let filledCount = 0;
    try {
      const url = getAiTranslateUrl(projectId);
      const response = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: abortController.signal,
        body: JSON.stringify({ names: untranslated, backend_profile: aiSelectedProfile }),
      });

      if (!response.ok) {
        const errData = await response.json().catch(() => ({}));
        throw new Error(errData.error || `请求失败：${response.status}`);
      }

      const reader = response.body?.getReader();
      if (!reader) throw new Error('无法读取流式响应');

      const decoder = new TextDecoder();
      let sseBuf = '';
      const remaining = new Map(untranslated.map((n) => [n.src_name, true]));

      while (!aborted) {
        const { done, value } = await reader.read();
        if (done) break;

        sseBuf += decoder.decode(value, { stream: true });

        const parts = sseBuf.split('\n\n');
        sseBuf = parts.pop() || '';

        for (const part of parts) {
          if (!part.trim()) continue;
          let eventType = '';
          let eventData = '';
          for (const line of part.split('\n')) {
            if (line.startsWith('event: ')) eventType = line.slice(7).trim();
            else if (line.startsWith('data: ')) eventData = line.slice(6);
          }
          if (!eventData) continue;

          try {
            const data = JSON.parse(eventData);
            if (eventType === 'name') {
              const src = data.src_name as string;
              const dst = data.dst_name as string;
              if (src && dst) {
                setNames((prev) =>
                  prev.map((entry) =>
                    entry.src_name === src && entry.dst_name.trim() === ''
                      ? { ...entry, dst_name: dst }
                      : entry
                  )
                );
                filledCount++;
                remaining.delete(src);
                if (remaining.size === 0) {
                  aborted = true;
                }
              }
            } else if (eventType === 'error') {
              setError(data.error || 'AI翻译人名失败');
            } else if (eventType === 'done') {
              aborted = true;
            }
          } catch {
            // Skip unparseable events
          }
        }
      }

      if (filledCount > 0) setDirty(true);
      if (filledCount === 0) {
        setError('AI未能返回任何翻译结果');
      }
    } catch (err) {
      if ((err instanceof DOMException && err.name === 'AbortError') || ((err as Error | null)?.name === 'AbortError')) {
        if (filledCount > 0) setDirty(true);
        setError('AI翻译人名已取消');
      } else {
        setError(normalizeError(err, 'AI翻译人名失败'));
      }
    } finally {
      if (aiTranslateAbortRef.current === abortController) {
        aiTranslateAbortRef.current = null;
      }
      setAiTranslating(false);
    }
  }, [projectId, names, aiSelectedProfile, aiTranslating]);

  const handleCancelAiTranslate = useCallback(() => {
    aiTranslateAbortRef.current?.abort();
  }, []);

  const handleDstNameChange = useCallback((index: number, value: string) => {
    setNames((prev) => {
      const next = [...prev];
      next[index] = { ...next[index], dst_name: value };
      return next;
    });
    setDirty(true);
  }, []);

  const handleDeleteRow = useCallback((index: number) => {
    setNames((prev) => prev.filter((_, i) => i !== index));
    setDirty(true);
  }, []);

  const handleAddRow = useCallback(() => {
    setNames((prev) => [...prev, { src_name: '', dst_name: '', count: 0 }]);
    setDirty(true);
  }, []);

  const handlePaste = useCallback((e: React.ClipboardEvent, field: 'src_name' | 'dst_name', startIndex: number) => {
    const text = e.clipboardData.getData('text/plain');
    if (text.includes('\n') || text.includes('\t')) {
      e.preventDefault();
      const lines = text.split('\n').filter((l) => l.trim() !== '');
      const newNames = [...names];
      for (let i = 0; i < lines.length; i++) {
        const parts = lines[i].split('\t');
        const targetIndex = startIndex + i;
        if (targetIndex >= newNames.length) {
          newNames.push({ src_name: '', dst_name: '', count: 0 });
        }
        if (field === 'src_name') {
          newNames[targetIndex] = { ...newNames[targetIndex], src_name: (parts[0] || '').trim() };
          if (parts.length > 1) {
            newNames[targetIndex] = { ...newNames[targetIndex], dst_name: (parts[1] || '').trim() };
          }
        } else {
          newNames[targetIndex] = { ...newNames[targetIndex], dst_name: (parts[0] || '').trim() };
        }
      }
      setNames(newNames);
      setDirty(true);
    }
  }, [names]);

  // Filter by search
  const filteredNames = debouncedSearch
    ? names.filter((n) =>
        n.src_name.toLowerCase().includes(debouncedSearch.toLowerCase()) ||
        n.dst_name.toLowerCase().includes(debouncedSearch.toLowerCase())
      )
    : names;

  const translatedCount = names.filter((n) => n.dst_name.trim() !== '').length;

  if (loading) return <LoadingState />;

  const saveLabel = saving
    ? '保存中…'
    : dirty
      ? '待保存'
      : '已保存';

  const panelActions = (
    <div className="name-page__panel-actions">
      <input
        type="text"
        className="name-page__search"
        placeholder="搜索人名..."
        value={searchQuery}
        onChange={(e) => setSearchQuery(e.target.value)}
      />
      <label
        className="name-page__gpt-toggle"
        title="打开后：自动设置项目配置中的「字典用在name字段(GPT)」，并用项目GPT字典中的条目覆盖未翻译的人名（AI翻译时会自动跳过这些人名）"
      >
        <span className="toggle-switch">
          <input
            type="checkbox"
            checked={useGptDictForName}
            disabled={gptToggleBusy}
            onChange={(e) => { void handleToggleGptDictForName(e.target.checked); }}
          />
          <span className="toggle-switch__slider" />
        </span>
        <span className="name-page__gpt-toggle-label">GPT字典用于人名</span>
      </label>
      <div className="name-page__panel-actions-group">
      <Button onClick={handleGenerate} disabled={generating} variant="secondary">
        {generating ? '提取中...' : '提取人名表'}
      </Button>
      <div className="name-page__ai-wrap" ref={aiPopoverRef}>
        <Button
          onClick={aiTranslating ? handleCancelAiTranslate : handleOpenAiPopover}
          disabled={!aiTranslating && names.length === 0}
          variant={aiTranslating ? 'secondary' : 'primary'}
          title={aiTranslating ? '点击取消当前人名翻译' : undefined}
        >
          {aiTranslating ? '翻译中，点击取消' : 'AI翻译人名'}
        </Button>
        {showAiPopover && (
          <div className="name-page__ai-popover">
            <div className="name-page__ai-popover-title">选择翻译后端</div>
            {aiProfileNames.length === 0 ? (
              <div className="name-page__ai-popover-empty">
                未找到后端配置，请先在「后端配置」页添加 OpenAI 兼容接口
              </div>
            ) : (
              <>
                <CustomSelect
                  className="name-page__ai-popover-select"
                  value={aiSelectedProfile}
                  onChange={(e) => setAiSelectedProfile(e.target.value)}
                >
                  {(() => {
                    const def = getSelectedBackendProfile(projectDir);
                    const sorted = def && aiProfileNames.includes(def)
                      ? [def, ...aiProfileNames.filter((n) => n !== def)]
                      : aiProfileNames;
                    return sorted.map((name) => {
                      const model = aiProfileModelMap[name];
                      const suffix = name === def ? '（默认）' : '';
                      const label = model ? `${name} - ${model}${suffix}` : `${name}${suffix}`;
                      return <option key={name} value={name}>{label}</option>;
                    });
                  })()}
                </CustomSelect>
                <Button
                  variant="primary"
                  onClick={handleAiTranslate}
                  disabled={!aiSelectedProfile}
                >
                  开始翻译
                </Button>
              </>
            )}
          </div>
        )}
      </div>
      <Button
        onClick={handleSave}
        disabled={!dirty || saving}
        variant="primary"
        className={`name-page__save-btn name-page__save-btn--${saving ? 'saving' : dirty ? 'dirty' : 'saved'}`}
        title={dirty ? '立即保存（编辑 1 秒后会自动保存）' : '已自动保存'}
      >
        {saveLabel}
      </Button>
      </div>
    </div>
  );

  return (
    <div className="page name-page">
      <PageHeader title="人名翻译" description="用于翻译输入文件中的“name”字段，是直接替换模式。注意正文中的人名应使用“GPT字典”让模型翻译。" />

      {error ? (
        <InlineFeedback
          className="inline-alert--floating"
          tone="error"
          title="操作失败"
          description={error}
          onDismiss={() => setError(null)}
        />
      ) : null}

      <Panel title="人名替换表" actions={panelActions}>
        <div className="name-page__stats">
          <span className="name-page__stat">
            共 {names.length} 个人名
          </span>
          <span className="name-page__stat">
            已翻译 {translatedCount} / {names.length}
          </span>
          {sourceFile && (
            <span className="name-page__stat">
              来源: {sourceFile}
            </span>
          )}
        </div>

        {names.length === 0 && !generating ? (
          <EmptyState
            title="尚未生成人名表"
            description="点击「提取人名表」从当前项目的输入文件中提取所有人名。"
          />
        ) : (
          <div className="name-page__table-wrap">
            <table className="name-page__table">
              <thead>
                <tr>
                  <th className="name-page__th name-page__th--index">#</th>
                  <th className="name-page__th name-page__th--jp">原名</th>
                  <th className="name-page__th name-page__th--cn">译名</th>
                  <th className="name-page__th name-page__th--count">次数</th>
                  <th className="name-page__th name-page__th--actions" />
                </tr>
              </thead>
              <tbody>
                {filteredNames.map((entry, i) => {
                  const originalIndex = names.indexOf(entry);
                  const hasTranslation = entry.dst_name.trim() !== '';
                  return (
                    <tr key={originalIndex} className={`name-page__row${hasTranslation ? ' name-page__row--translated' : ''}`}>
                      <td className="name-page__td name-page__td--index">{originalIndex + 1}</td>
                      <td className="name-page__td name-page__td--jp">
                        <input
                          type="text"
                          className="name-page__input"
                          value={entry.src_name}
                          onChange={(e) => {
                            const newNames = [...names];
                            newNames[originalIndex] = { ...newNames[originalIndex], src_name: e.target.value };
                            setNames(newNames);
                            setDirty(true);
                          }}
                          onPaste={(e) => handlePaste(e, 'src_name', originalIndex)}
                        />
                      </td>
                      <td className="name-page__td name-page__td--cn">
                        <input
                          type="text"
                          className="name-page__input"
                          value={entry.dst_name}
                          placeholder={entry.src_name ? `输入 ${entry.src_name} 的译名...` : ''}
                          onChange={(e) => handleDstNameChange(originalIndex, e.target.value)}
                          onPaste={(e) => handlePaste(e, 'dst_name', originalIndex)}
                        />
                      </td>
                      <td className="name-page__td name-page__td--count">{entry.count}</td>
                      <td className="name-page__td name-page__td--actions">
                        <button
                          type="button"
                          className="name-page__delete-btn"
                          onClick={() => handleDeleteRow(originalIndex)}
                          title="删除此行"
                        >
                          ✕
                        </button>
                      </td>
                    </tr>
                  );
                })}
                <tr className="name-page__row name-page__row--add">
                  <td className="name-page__td name-page__td--add" colSpan={5}>
                    <button
                      type="button"
                      className="name-page__add-btn"
                      onClick={handleAddRow}
                      title="添加人名"
                      aria-label="添加人名"
                    >
                      +
                    </button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}
