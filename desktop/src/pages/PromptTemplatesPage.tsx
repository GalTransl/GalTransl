import { UiTrans, message as uiMessage, t as translate, useMessageState, useUiLanguage } from "../i18n";
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
  const uiLanguage = useUiLanguage();
  const navigate = useNavigate();
  const [templates, setTemplates] = useState<PromptTemplateInfo[]>([]);
  const [selectedName, setSelectedName] = useState('');
  const [systemPromptValue, setSystemPromptValue] = useState('');
  const [userPromptValue, setUserPromptValue] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useMessageState<string | null>(null);
  const [feedback, setFeedback] = useMessageState<string | null>(null);

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
          setError(normalizeError(err, uiMessage("settings:promptTemplatesPage.promptTemplatesPage_normalizeError_loadDefaultPromptFailed")));
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
      setFeedback(uiMessage("settings:promptTemplatesPage.handleSave_setFeedback_saveSuccessJobSystemUserPrompt"));
    } catch (err) {
      setError(normalizeError(err, uiMessage("settings:promptTemplatesPage.handleSave_normalizeError_saveDefaultPromptFailed")));
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
      setFeedback(uiMessage("settings:promptTemplatesPage.handleReset_setFeedback_doneResetDefaultPrompt"));
    } catch (err) {
      setError(normalizeError(err, uiMessage("settings:promptTemplatesPage.handleReset_normalizeError_resetDefaultPromptFailed")));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="prompt-templates-page">
      <PageHeader
        className="prompt-templates-page__header"
        title={translate("settings:promptTemplatesPage.promptTemplatesPage_title_defaultPrompt")}
        description={translate("settings:promptTemplatesPage.promptTemplatesPage_description_translationDefaultPromptSaveTranslationJobAuto")}
      />

      <div className="prompt-templates-page__content">
        <section className="panel">
          <header className="panel__header">
            <div>
              <h2>{translate("settings:promptTemplatesPage.panelHeader_message_edit")}</h2>
              <p>{translate("settings:promptTemplatesPage.panelHeader_message_changeCountTranslationSystemPromptUserPrompt")}</p>
            </div>
          </header>

          {loading ? (
            <LoadingState title={translate("common:actions.loading")} description={translate("settings:promptTemplatesPage.panel_description_pendingCurrentEditTranslationPrompt")} />
          ) : error ? (
            <ErrorState title={translate("settings:promptTemplatesPage.panel_title_loadFailed")} description={error} />
          ) : templates.length === 0 ? (
            <EmptyState title={translate("settings:promptTemplatesPage.panel_title_emptyEdit")} description={translate("settings:promptTemplatesPage.panel_description_currentBackendNotBackEditTranslationPrompt")} />
          ) : (
            <>
              <label className="settings-number-row">
                <span className="settings-number-row__label">{translate("settings:promptTemplatesPage.settingsNumberRow_message_translation")}</span>
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
                      {saving ? translate("common:actions.saving") : translate("settings:promptTemplatesPage.buttonButtonPrimary_message_saveChange")}
                    </button>
                    <button
                      type="button"
                      className="button button--secondary"
                      disabled={saving || (!selectedTemplate.system_overridden && !selectedTemplate.user_overridden)}
                      onClick={() => {
                        void handleReset();
                      }}
                    >{translate("settings:promptTemplatesPage.promptTemplatesPageActions_message_resetDefaultPrompt")}</button>
                    <button
                      type="button"
                      className="button"
                      disabled={saving}
                      onClick={() => {
                        navigate('/settings');
                      }}
                    >{translate("settings:promptTemplatesPage.promptTemplatesPageActions_message_backSettings")}</button>
                  </div>

                  <label className="prompt-templates-page__editor-label">{translate("settings:promptTemplatesPage.promptTemplatesPageEditorWrap_message_systemPrompt")}</label>
                  <textarea
                    className="prompt-templates-page__editor prompt-templates-page__editor--system"
                    value={systemPromptValue}
                    onChange={(event) => {
                      setSystemPromptValue(event.target.value);
                      setFeedback(null);
                    }}
                  />

                  <label className="prompt-templates-page__editor-label">{translate("settings:promptTemplatesPage.promptTemplatesPageEditorWrap_message_userPrompt")}</label>
                  <textarea
                    className="prompt-templates-page__editor"
                    value={userPromptValue}
                    onChange={(event) => {
                      setUserPromptValue(event.target.value);
                      setFeedback(null);
                    }}
                  />

                  <div className="prompt-templates-page__placeholder-help">
                    <div className="prompt-templates-page__placeholder-help-title">{translate("settings:promptTemplatesPage.promptTemplatesPagePlaceholderHelp_message_description")}</div>
                    <ul>
                      <li><UiTrans k="settings:promptTemplatesPage.promptTemplatesPagePlaceholderHelp_message_0SourceLang0SourceLanguageName" components={[<code />]} /></li>
                      <li><UiTrans k="settings:promptTemplatesPage.promptTemplatesPagePlaceholderHelp_message_0TargetLang0TargetLanguageName" components={[<code />]} /></li>
                      <li><UiTrans k="settings:promptTemplatesPage.promptTemplatesPagePlaceholderHelp_message_0TranslationGuideline0CurrentTranslationGuideline" components={[<code />]} /></li>
                      <li><UiTrans k="settings:promptTemplatesPage.promptTemplatesPagePlaceholderHelp_message_0Glossary0Prompt" components={[<code />]} /></li>
                      <li><UiTrans k="settings:promptTemplatesPage.promptTemplatesPagePlaceholderHelp_message_0Input0TranslationSource" components={[<code />]} /></li>
                      <li><UiTrans k="settings:promptTemplatesPage.promptTemplatesPagePlaceholderHelp_message_0HistoryResult0ContextTranslationNone" components={[<code />]} /></li>
                    </ul>
                  </div>

                  <div className="prompt-templates-page__meta">
                    <span>
                      {selectedTemplate.system_overridden ? translate("settings:promptTemplatesPage.promptTemplatesPageMeta_message_systemDoneDefaultValue") : translate("settings:promptTemplatesPage.promptTemplatesPageMeta_message_systemDefaultValue")}
                    </span>
                    <span>
                      {selectedTemplate.user_overridden ? translate("settings:promptTemplatesPage.promptTemplatesPageMeta_message_userDoneDefaultValue") : translate("settings:promptTemplatesPage.promptTemplatesPageMeta_message_userDefaultValue")}
                    </span>
                    <span>{hasUnsavedChanges ? translate("settings:promptTemplatesPage.promptTemplatesPageMeta_message_notSaveChange") : translate("settings:promptTemplatesPage.promptTemplatesPageMeta_message_doneSave")}</span>
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
