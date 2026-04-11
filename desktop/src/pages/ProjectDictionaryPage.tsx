import { useCallback, useEffect, useState } from 'react';
import { useOutletContext } from 'react-router-dom';
import { Panel } from '../components/Panel';
import {
  ApiError,
  type ProjectDictionaryResponse,
  type DictFileContent,
  fetchProjectDictionary,
} from '../lib/api';

type OutletContext = {
  projectDir: string;
  projectId: string;
  configFileName: string;
  onProjectDirChange: (dir: string) => void;
};

type DictTab = 'pre' | 'gpt' | 'post';

export function ProjectDictionaryPage() {
  const { projectDir, projectId, configFileName } = useOutletContext<OutletContext>();

  const [data, setData] = useState<ProjectDictionaryResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<DictTab>('pre');
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [searchTerm, setSearchTerm] = useState('');

  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    setLoading(true);
    fetchProjectDictionary(projectId, configFileName)
      .then((res) => {
        if (!cancelled) {
          setData(res);
          // Auto-select first file in active tab
          const files = getActiveFiles(res, activeTab);
          if (files.length > 0 && !selectedFile) {
            setSelectedFile(files[0]);
          }
        }
      })
      .catch((err) => {
        if (!cancelled) setError(getErrorMessage(err, '加载字典失败'));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [projectId, configFileName, activeTab]);

  const activeFiles = data ? getActiveFiles(data, activeTab) : [];
  const currentContent: DictFileContent | null = selectedFile && data?.dict_contents?.[selectedFile]
    ? data.dict_contents[selectedFile]
    : null;

  const filteredLines = currentContent?.lines.filter((line) => {
    if (!searchTerm) return true;
    return line.toLowerCase().includes(searchTerm.toLowerCase());
  }) || [];

  if (loading) {
    return (
      <div className="project-dictionary-page">
        <div className="project-dictionary-page__header"><h1>字典管理</h1></div>
        <div className="empty-state"><strong>加载中…</strong></div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="project-dictionary-page">
        <div className="project-dictionary-page__header"><h1>字典管理</h1></div>
        <div className="inline-alert inline-alert--error" role="alert">{error}</div>
      </div>
    );
  }

  return (
    <div className="project-dictionary-page">
      <div className="project-dictionary-page__header">
        <h1>字典管理</h1>
        <p>查看和管理翻译字典文件，字典直接影响翻译质量。</p>
      </div>

      <div className="project-dictionary-page__content">
        <div className="dict-tabs">
          {(['pre', 'gpt', 'post'] as DictTab[]).map((tab) => (
            <button
              key={tab}
              className={`dict-tab ${activeTab === tab ? 'dict-tab--active' : ''}`}
              onClick={() => { setActiveTab(tab); setSelectedFile(null); }}
            >
              {tab === 'pre' ? '译前字典' : tab === 'gpt' ? 'GPT字典' : '译后字典'}
              <span className="dict-tab__count">
                {tab === 'pre' ? data?.pre_dict_files.length : tab === 'gpt' ? data?.gpt_dict_files.length : data?.post_dict_files.length}
              </span>
            </button>
          ))}
        </div>

        <div className="dict-layout">
          <aside className="dict-layout__sidebar">
            <h3>字典文件</h3>
            {activeFiles.map((file) => {
              const content = data?.dict_contents?.[file];
              const isActive = selectedFile === file;
              return (
                <button
                  key={file}
                  className={`dict-file-item ${isActive ? 'dict-file-item--active' : ''}`}
                  onClick={() => setSelectedFile(file)}
                >
                  <span className="dict-file-item__name">{file}</span>
                  {content && (
                    <span className="dict-file-item__count">{content.count}条</span>
                  )}
                </button>
              );
            })}
          </aside>

          <div className="dict-layout__main">
            {currentContent ? (
              <Panel
                title={selectedFile || ''}
                description={`${currentContent.count} 条有效条目 · ${currentContent.path}`}
              >
                <div className="dict-toolbar">
                  <input
                    type="text"
                    placeholder="搜索字典条目…"
                    value={searchTerm}
                    onChange={(e) => setSearchTerm(e.target.value)}
                    className="dict-search"
                  />
                </div>
                <div className="dict-entry-list">
                  {filteredLines.map((line, i) => {
                    if (!line.trim() || line.startsWith('\\\\') || line.startsWith('//')) {
                      return (
                        <div key={i} className="dict-entry dict-entry--comment">
                          <span className="dict-entry__line">{line}</span>
                        </div>
                      );
                    }
                    const parts = line.split('\t');
                    return (
                      <div key={i} className="dict-entry">
                        <span className="dict-entry__search">{parts[0] || ''}</span>
                        <span className="dict-entry__arrow">→</span>
                        <span className="dict-entry__replace">{parts.slice(1).join(' → ')}</span>
                      </div>
                    );
                  })}
                </div>
              </Panel>
            ) : (
              <div className="empty-state">
                <strong>选择一个字典文件</strong>
                <span>从左侧选择字典文件查看内容。</span>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function getActiveFiles(data: ProjectDictionaryResponse, tab: DictTab): string[] {
  if (tab === 'pre') return data.pre_dict_files;
  if (tab === 'gpt') return data.gpt_dict_files;
  return data.post_dict_files;
}

function getErrorMessage(error: unknown, fallback: string) {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error && error.message.trim()) return error.message;
  return fallback;
}
