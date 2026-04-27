import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { CustomSelect } from '../components/CustomSelect';
import { PageHeader } from '../components/PageHeader';
import { EmptyState, ErrorState, LoadingState } from '../components/page-state';
import {
  type PromptTemplateInfo,
  fetchPromptTemplates,
  getPromptTemplateOverride,
  setPromptTemplateOverride,
  deletePromptTemplateOverride,
} from '../lib/api';
import { normalizeError } from '../lib/errors';

export function PromptTemplatesPage() {
  const navigate = useNavigate();
  const [templates, setTemplates] = useState<PromptTemplateInfo[]>([]);
  const [selectedName, setSelectedName] = useState('');
  const [systemPromptValue, setSystemPromptValue] = useState('');
  const [userPromptValue, setUserPromptValue] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<string | null>(null);

  const selectedTemplate = useMemo(
    () => templates.find((item) => item.name === selectedName) ?? null,
    [templates, selectedName],
  );

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setFeedback(null);
    fetchPromptTemplates()
      .then((data) => {
        if (cancelled) {
          return;
        }
        const defaultTemplates = data.templates || [];
        const nextTemplates = defaultTemplates.map((tpl) => {
          const override = getPromptTemplateOverride(tpl.name);
          return {
            ...tpl,
            system_prompt: override?.system_prompt ?? tpl.system_prompt,
            user_prompt: override?.user_prompt ?? tpl.user_prompt,
            system_overridden: override?.system_prompt != null,
            user_overridden: override?.user_prompt != null,
            overridden: override?.system_prompt != null || override?.user_prompt != null,
          };
        });
        setTemplates(nextTemplates);
        if (nextTemplates.length === 0) {
          setSelectedName('');
          setSystemPromptValue('');
          setUserPromptValue('');
          return;
        }
        setSelectedName((current) => {
          const fallback = nextTemplates[0].name;
          const keepCurrent = nextTemplates.some((item) => item.name === current);
          const nextName = keepCurrent ? current : fallback;
          const nextTemplate = nextTemplates.find((item) => item.name === nextName) || nextTemplates[0];
          setSystemPromptValue(nextTemplate.system_prompt);
          setUserPromptValue(nextTemplate.user_prompt);
          return nextName;
        });
      })
      .catch((err) => {
        if (!cancelled) {
          setError(normalizeError(err, '加载默认提示词失败'));
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, []);

  const hasUnsavedChanges = selectedTemplate !== null
    && (systemPromptValue !== selectedTemplate.system_prompt || userPromptValue !== selectedTemplate.user_prompt);

  const handleSelectTemplate = (name: string) => {
    setSelectedName(name);
    const nextTemplate = templates.find((item) => item.name === name);
    setSystemPromptValue(nextTemplate?.system_prompt || '');
    setUserPromptValue(nextTemplate?.user_prompt || '');
    setFeedback(null);
    setError(null);
  };

  const handleSave = async () => {
    if (!selectedTemplate) {
      return;
    }
    setSaving(true);
    setError(null);
    setFeedback(null);
    try {
      const override: { system_prompt?: string; user_prompt?: string } = {};
      if (systemPromptValue !== selectedTemplate.default_system_prompt) {
        override.system_prompt = systemPromptValue;
      }
      if (userPromptValue !== selectedTemplate.default_user_prompt) {
        override.user_prompt = userPromptValue;
      }
      if (Object.keys(override).length === 0) {
        deletePromptTemplateOverride(selectedTemplate.name);
      } else {
        setPromptTemplateOverride(selectedTemplate.name, override);
      }
      setTemplates((prev) =>
        prev.map((tpl) =>
          tpl.name === selectedTemplate.name
            ? {
                ...tpl,
                system_prompt: systemPromptValue,
                user_prompt: userPromptValue,
                system_overridden: override.system_prompt != null,
                user_overridden: override.user_prompt != null,
                overridden: override.system_prompt != null || override.user_prompt != null,
              }
            : tpl,
        ),
      );
      setFeedback('保存成功，后续该模板新任务会使用新的 system/user 提示词。');
    } catch (err) {
      setError(normalizeError(err, '保存默认提示词失败'));
    } finally {
      setSaving(false);
    }
  };

  const handleReset = async () => {
    if (!selectedTemplate) {
      return;
    }
    setSaving(true);
    setError(null);
    setFeedback(null);
    try {
      deletePromptTemplateOverride(selectedTemplate.name);
      setTemplates((prev) =>
        prev.map((tpl) =>
          tpl.name === selectedTemplate.name
            ? {
                ...tpl,
                system_prompt: tpl.default_system_prompt,
                user_prompt: tpl.default_user_prompt,
                system_overridden: false,
                user_overridden: false,
                overridden: false,
              }
            : tpl,
        ),
      );
      setSystemPromptValue(selectedTemplate.default_system_prompt);
      setUserPromptValue(selectedTemplate.default_user_prompt);
      setFeedback('已重置为内置默认提示词。');
    } catch (err) {
      setError(normalizeError(err, '重置默认提示词失败'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="prompt-templates-page">
      <PageHeader
        className="prompt-templates-page__header"
        title="默认提示词"
        description="按翻译模板维护默认提示词。保存后，对应模板的新翻译任务会自动使用更新后的 system/user 提示词。"
      />

      <div className="prompt-templates-page__content">
        <section className="panel">
          <header className="panel__header">
            <div>
              <h2>模板编辑器</h2>
              <p>可独立修改每个翻译模板的 system prompt 与 user prompt，并支持一键恢复内置默认。</p>
            </div>
          </header>

          {loading ? (
            <LoadingState title="加载中…" description="正在获取当前可编辑的翻译模板提示词。" />
          ) : error ? (
            <ErrorState title="加载失败" description={error} />
          ) : templates.length === 0 ? (
            <EmptyState title="暂无可编辑模板" description="当前后端未返回可编辑的翻译模板提示词。" />
          ) : (
            <>
              <label className="settings-number-row">
                <span className="settings-number-row__label">翻译模板</span>
                <div className="settings-number-row__control prompt-templates-page__select">
                  <CustomSelect
                    value={selectedName}
                    onChange={(event) => {
                      handleSelectTemplate(event.target.value);
                    }}
                  >
                    {templates.map((template) => (
                      <option key={template.name} value={template.name}>
                        {template.name} · {template.description}
                      </option>
                    ))}
                  </CustomSelect>
                </div>
              </label>

              {selectedTemplate ? (
                <div className="prompt-templates-page__editor-wrap">
                  <div className="prompt-templates-page__actions">
                    <button
                      type="button"
                      className="button button--primary"
                      disabled={saving || !hasUnsavedChanges}
                      onClick={() => {
                        void handleSave();
                      }}
                    >
                      {saving ? '保存中…' : '保存修改'}
                    </button>
                    <button
                      type="button"
                      className="button button--secondary"
                      disabled={saving || (!selectedTemplate.system_overridden && !selectedTemplate.user_overridden)}
                      onClick={() => {
                        void handleReset();
                      }}
                    >
                      重置为默认提示词
                    </button>
                    <button
                      type="button"
                      className="button"
                      disabled={saving}
                      onClick={() => {
                        navigate('/settings');
                      }}
                    >
                      返回设置
                    </button>
                  </div>

                  <label className="prompt-templates-page__editor-label">System Prompt</label>
                  <textarea
                    className="prompt-templates-page__editor prompt-templates-page__editor--system"
                    value={systemPromptValue}
                    onChange={(event) => {
                      setSystemPromptValue(event.target.value);
                      setFeedback(null);
                    }}
                  />

                  <label className="prompt-templates-page__editor-label">User Prompt</label>
                  <textarea
                    className="prompt-templates-page__editor"
                    value={userPromptValue}
                    onChange={(event) => {
                      setUserPromptValue(event.target.value);
                      setFeedback(null);
                    }}
                  />

                  <div className="prompt-templates-page__placeholder-help">
                    <div className="prompt-templates-page__placeholder-help-title">占位符说明</div>
                    <ul>
                      <li><code>[SourceLang]</code>：源语言名称。</li>
                      <li><code>[TargetLang]</code>：目标语言名称。</li>
                      <li><code>[translation_guideline]</code>：当前翻译规范内容。</li>
                      <li><code>[Glossary]</code>：本批次术语表提示词。</li>
                      <li><code>[Input]</code>：本批次待翻译原文内容。</li>
                      <li><code>[history_result]</code>：上下文历史翻译结果（无则为 None）。</li>
                    </ul>
                  </div>

                  <div className="prompt-templates-page__meta">
                    <span>
                      {selectedTemplate.system_overridden ? 'System：已覆盖默认值' : 'System：使用内置默认值'}
                    </span>
                    <span>
                      {selectedTemplate.user_overridden ? 'User：已覆盖默认值' : 'User：使用内置默认值'}
                    </span>
                    <span>{hasUnsavedChanges ? '有未保存修改' : '内容已保存'}</span>
                  </div>
                </div>
              ) : null}

              {feedback ? <div className="settings-toggle-row__desc">{feedback}</div> : null}
            </>
          )}
        </section>
      </div>
    </div>
  );
}
