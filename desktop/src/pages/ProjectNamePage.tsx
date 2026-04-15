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

  // AI translate popover state
  const [showAiPopover, setShowAiPopover] = useState(false);
  const [aiProfileNames, setAiProfileNames] = useState<string[]>([]);
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
        setNames(res.names);
        setSourceFile(res.source_file);
        setDirty(false);
      }
    } catch (err) {
      setError(normalizeError(err, '生成人名表失败'));
    } finally {
      setGenerating(false);
    }
  }, [projectId, projectDir, configFileName]);

  const handleSave = useCallback(async () => {
    if (!projectId) return;
    setSaving(true);
    setError(null);
    try {
      await saveNameTable(projectId, names);
      setDirty(false);
    } catch (err) {
      setError(normalizeError(err, '保存人名表失败'));
    } finally {
      setSaving(false);
    }
  }, [projectId, names]);

  // Open AI translate popover — load profiles & preselect default
  const handleOpenAiPopover = useCallback(() => {
    if (names.filter((n) => n.dst_name.trim() === '').length === 0) {
      setError('所有人名已翻译，无需AI翻译');
      return;
    }
    fetchBackendProfiles()
      .then((data) => {
        const profileKeys = Object.keys(data.profiles || {});
        setAiProfileNames(profileKeys);
        const defaultName = getSelectedBackendProfile(projectDir);
        setAiSelectedProfile(defaultName && profileKeys.includes(defaultName) ? defaultName : (profileKeys[0] || ''));
        setShowAiPopover(true);
      })
      .catch(() => {
        setError('加载后端配置失败');
      });
  }, [names, projectDir]);

  const handleAiTranslate = useCallback(async () => {
    if (!projectId) return;
    const untranslated = names.filter((n) => n.dst_name.trim() === '');
    if (untranslated.length === 0) {
      setError('所有人名已翻译，无需AI翻译');
      return;
    }
    setShowAiPopover(false);
    setAiTranslating(true);
    setError(null);
    let aborted = false;
    try {
      const url = getAiTranslateUrl(projectId);
      const response = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
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
      let filledCount = 0;
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
      setError(normalizeError(err, 'AI翻译人名失败'));
    } finally {
      setAiTranslating(false);
    }
  }, [projectId, names, aiSelectedProfile]);

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

  return (
    <div className="page name-page">
      <PageHeader title="人名翻译" description="管理项目的人名替换表，填入中文译名后可用于翻译工作台的 name 字段替换。" />

      <div className="name-page__toolbar">
        <div className="name-page__toolbar-left">
          <Button onClick={handleGenerate} disabled={generating}>
            {generating ? '提取中...' : '提取人名表'}
          </Button>
          <div className="name-page__ai-wrap" ref={aiPopoverRef}>
            <Button onClick={handleOpenAiPopover} disabled={aiTranslating || names.length === 0}>
              {aiTranslating ? 'AI翻译中...' : 'AI翻译人名'}
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
                      {aiProfileNames.map((name) => (
                        <option key={name} value={name}>{name}</option>
                      ))}
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
          <Button onClick={handleSave} disabled={!dirty || saving} variant="primary">
            {saving ? '保存中...' : '保存'}
          </Button>
        </div>
        <div className="name-page__toolbar-right">
          <input
            type="text"
            className="name-page__search"
            placeholder="搜索人名..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
          />
        </div>
      </div>

      {error && <InlineFeedback tone="error">{error}</InlineFeedback>}

      <Panel title="人名替换表">
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
              </tbody>
            </table>
          </div>
        )}

        <div className="name-page__footer">
          <Button onClick={handleAddRow}>+ 添加人名</Button>
        </div>
      </Panel>
    </div>
  );
}
