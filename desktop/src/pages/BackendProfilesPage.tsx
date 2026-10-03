import { message as uiMessage, t as translate, useMessageState, useUiLanguage } from "../i18n";
import { useCallback, useEffect, useState } from 'react';
import { BackendConfigEditor } from '../components/BackendConfigEditor';
import { Button } from '../components/Button';
import { CustomSelect } from '../components/CustomSelect';
import { Icon } from '../components/Icon';
import { PageHeader } from '../components/PageHeader';
import { Panel } from '../components/Panel';
import { EmptyState, InlineFeedback, LoadingState } from '../components/page-state';
import { ProxyConfigEditor } from '../components/ProxyConfigEditor';
import {
  AGENT_DEFAULT_BACKEND_PROFILE_CHANGE_EVENT,
  DEFAULT_BACKEND_PROFILE_CHANGE_EVENT,
  createBackendProfile,
  deleteBackendProfile,
  fetchBackendProfiles,
  getAgentDefaultBackendProfile,
  getDefaultBackendProfile,
  setAgentDefaultBackendProfile,
  setDefaultBackendProfile } from '../lib/api';
import { normalizeError } from '../lib/errors';
import { getProfileMeta } from '../lib/backendProfile';

type ProfileEntry = {
  name: string;
  config: Record<string, unknown>;
};

const DEFAULT_BACKEND_CONFIG: Record<string, unknown> = {};


export function BackendProfilesPage() {
  useUiLanguage();
  const [profiles, setProfiles] = useState<ProfileEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useMessageState<string | null>(null);
  const [defaultProfile, setDefaultProfileState] = useState(getDefaultBackendProfile());
  const [agentDefaultProfile, setAgentDefaultState] = useState(getAgentDefaultBackendProfile());

  // Editor state
  const [editingName, setEditingName] = useState('');
  const [editingConfig, setEditingConfig] = useState<Record<string, unknown>>(DEFAULT_BACKEND_CONFIG);
  const [isEditing, setIsEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveSuccess, setSaveSuccess] = useState(false);

  // New-profile dialog state
  const [showNewDialog, setShowNewDialog] = useState(false);
  const [newProfileName, setNewProfileName] = useState('');
  const [creating, setCreating] = useState(false);

  const loadProfiles = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchBackendProfiles();
      const entries: ProfileEntry[] = Object.entries(data.profiles || {}).map(
        ([name, config]) => ({ name, config: config as Record<string, unknown> })
      );
      setProfiles(entries);
      setDefaultProfileState(getDefaultBackendProfile());
      setAgentDefaultState(getAgentDefaultBackendProfile());
    } catch (err) {
      setError(normalizeError(err, uiMessage("settings:backendProfilesPage.loadProfiles_normalizeError_loadBackendConfigFailed")));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadProfiles();
  }, [loadProfiles]);

  // 默认标签可能从别处改动（将来留口子），监听两个事件保持 badge 实时同步
  useEffect(() => {
    const onTranslatorDefault = (e: Event) => setDefaultProfileState((e as CustomEvent<string>).detail || '');
    const onAgentDefault = (e: Event) => setAgentDefaultState((e as CustomEvent<string>).detail || '');
    window.addEventListener(DEFAULT_BACKEND_PROFILE_CHANGE_EVENT, onTranslatorDefault as EventListener);
    window.addEventListener(AGENT_DEFAULT_BACKEND_PROFILE_CHANGE_EVENT, onAgentDefault as EventListener);
    return () => {
      window.removeEventListener(DEFAULT_BACKEND_PROFILE_CHANGE_EVENT, onTranslatorDefault as EventListener);
      window.removeEventListener(AGENT_DEFAULT_BACKEND_PROFILE_CHANGE_EVENT, onAgentDefault as EventListener);
    };
  }, []);

  const openNewDialog = useCallback(() => {
    setNewProfileName('');
    setShowNewDialog(true);
    setError(null);
    setSaveSuccess(false);
  }, []);

  const closeNewDialog = useCallback(() => {
    if (creating) return;
    setShowNewDialog(false);
    setNewProfileName('');
  }, [creating]);

  const handleCreate = useCallback(async () => {
    const name = newProfileName.trim();
    if (!name) {
      setError(uiMessage("settings:backendProfilesPage.handleCreate_setError_configNameRequired"));
      return;
    }
    if (profiles.some((p) => p.name === name)) {
      setError(uiMessage("settings:backendProfilesPage.handleCreate_setError_configDone", { name: name }));
      return;
    }
    setCreating(true);
    setError(null);
    try {
      const newConfig = JSON.parse(JSON.stringify(DEFAULT_BACKEND_CONFIG));
      await createBackendProfile(name, newConfig);
      // 还没有默认配置时（通常是第一次使用），新建的配置直接当默认，
      // 否则「跟随全局默认」的项目仍然是「未配置后端」。
      if (!getDefaultBackendProfile()) {
        setDefaultBackendProfile(name);
        setDefaultProfileState(name);
      }
      if (!getAgentDefaultBackendProfile()) {
        setAgentDefaultBackendProfile(name);
        setAgentDefaultState(name);
      }
      setSaveSuccess(true);
      setShowNewDialog(false);
      setNewProfileName('');
      await loadProfiles();
      // Immediately open the edit dialog for the new profile
      setEditingName(name);
      setEditingConfig(newConfig);
      setIsEditing(true);
    } catch (err) {
      setError(normalizeError(err, uiMessage("settings:backendProfilesPage.handleCreate_normalizeError_createConfigFailed")));
    } finally {
      setCreating(false);
    }
  }, [newProfileName, profiles, loadProfiles]);

  const handleEdit = useCallback((entry: ProfileEntry) => {
    setIsEditing(true);
    setEditingName(entry.name);
    setEditingConfig(JSON.parse(JSON.stringify(entry.config)));
    setSaveSuccess(false);
    setError(null);
  }, []);

  const handleCancel = useCallback(() => {
    setIsEditing(false);
    setEditingName('');
    setEditingConfig(DEFAULT_BACKEND_CONFIG);
    setSaveSuccess(false);
    setError(null);
  }, []);

  const handleSave = useCallback(async () => {
    const name = editingName.trim();
    if (!name) {
      setError(uiMessage("settings:backendProfilesPage.handleSave_setError_configNameRequired"));
      return;
    }
    setSaving(true);
    setError(null);
    setSaveSuccess(false);
    try {
      await createBackendProfile(name, editingConfig);
      setSaveSuccess(true);
      setIsEditing(false);
      void loadProfiles();
    } catch (err) {
      setError(normalizeError(err, uiMessage("settings:backendProfilesPage.handleSave_normalizeError_saveConfigFailed")));
    } finally {
      setSaving(false);
    }
  }, [editingName, editingConfig, loadProfiles]);

  const handleDelete = useCallback(async (name: string) => {
    if (!confirm(translate("settings:backendProfilesPage.handleDelete_confirm_deleteConfig", { name: name }))) return;
    try {
      await deleteBackendProfile(name);
      // If we're editing this profile, close the editor
      if (editingName === name) {
        handleCancel();
      }
      void loadProfiles();
    } catch (err) {
      setError(normalizeError(err, uiMessage("settings:backendProfilesPage.handleDelete_normalizeError_deleteConfigFailed")));
    }
  }, [editingName, handleCancel, loadProfiles]);

  return (
    <div className="backend-profiles-page">
      <PageHeader
        className="backend-profiles-page__header"
        title={<><Icon name="bot" />{translate("settings:backendProfilesPage.backendProfilesPage_title_modelSettings")}</>}
        description={translate("settings:backendProfilesPage.backendProfilesPage_description_translationBackendConfigProjectCountProjectConfig")}
        status={
          <>
            {error && <InlineFeedback tone="error" title={translate("settings:backendProfilesPage.backendProfilesPage_title_failed")} description={error} />}
            {saveSuccess && <InlineFeedback className="inline-alert--floating" tone="success" title={translate("settings:backendProfilesPage.backendProfilesPage_title_configDoneSave")} description={translate("settings:backendProfilesPage.backendProfilesPage_description_backendConfigDoneProject")} onDismiss={() => setSaveSuccess(false)} />}
          </>
        }
      />

      <div className="backend-profiles-page__content">
        <Panel
          className="backend-profiles-page__list-panel"
          title={translate("settings:backendProfilesPage.backendProfilesPageListPanel_title_config")}
          description={translate("settings:backendProfilesPage.backendProfilesPageListPanel_description_doneCreateTranslationBackendConfig")}
          actions={(
            <Button onClick={openNewDialog}>{translate("settings:backendProfilesPage.backendProfilesPageListPanel_actions_newConfig")}</Button>
          )}
        >
          <div className="default-selectors">
            <label className="field">
              <span>{translate("settings:backendProfilesPage.field_message_translationDefault")}</span>
              <CustomSelect
                value={defaultProfile}
                onChange={(e) => {
                  setDefaultBackendProfile(e.target.value);
                  setDefaultProfileState(e.target.value);
                }}
              >
                {profiles.map((entry) => (
                  <option key={entry.name} value={entry.name}>{entry.name}</option>
                ))}
              </CustomSelect>
            </label>

            <label className="field">
              <span>{translate("settings:backendProfilesPage.field_message_agentDefault")}</span>
              <CustomSelect
                value={agentDefaultProfile}
                onChange={(e) => {
                  setAgentDefaultBackendProfile(e.target.value);
                  setAgentDefaultState(e.target.value);
                }}
              >
                {profiles.map((entry) => (
                  <option key={entry.name} value={entry.name}>{entry.name}</option>
                ))}
              </CustomSelect>
            </label>
          </div>
          <div className="backend-profiles-page__divider" />

          <div className="backend-profiles-page__list-scroll">
            {loading ? (
              <LoadingState title={translate("settings:backendProfilesPage.backendProfilesPageListScroll_title_loadConfig")} description={translate("settings:backendProfilesPage.backendProfilesPageListScroll_description_pendingReadTranslationBackendConfig")} />
            ) : profiles.length === 0 ? (
              <EmptyState
                title={translate("settings:backendProfilesPage.backendProfilesPageListScroll_title_emptyConfig")}
                description={translate("settings:backendProfilesPage.backendProfilesPageListScroll_description_translationCountModelConfigNewAPIAddress")}
                action={<Button onClick={openNewDialog}><Icon name="file-plus" />{translate("settings:backendProfilesPage.backendProfilesPageListScroll_button_newCountConfig")}</Button>}
              />
            ) : (
              <div className="profile-list">
                {profiles.map((entry) => {
                  const { baseUrl, modelName } = getProfileMeta(entry.config);

                  return (
                    <div key={entry.name} className="profile-card">
                      <div className="profile-card__info">
                        <div className="profile-card__name">
                          {entry.name}
                          {defaultProfile === entry.name && (
                            <span className="profile-card__badge">{translate("settings:backendProfilesPage.profileCardName_message_translationDefault")}</span>
                          )}
                          {agentDefaultProfile === entry.name && (
                            <span className="profile-card__badge profile-card__badge--agent">{translate("settings:backendProfilesPage.profileCardName_message_agentDefault")}</span>
                          )}
                        </div>
                        <div className="profile-card__meta">{translate("settings:backendProfilesPage.profileCardInfo_message_baseURL", { baseUrl: baseUrl })}</div>
                        <div className="profile-card__meta">{translate("settings:backendProfilesPage.profileCardInfo_message_model", { modelName: modelName })}</div>
                      </div>
                      <div className="profile-card__actions">
                        <Button
                          variant="secondary"
                          onClick={() => handleEdit(entry)}
                        >{translate("common:actions.edit")}</Button>
                        <Button
                          variant="secondary"
                          onClick={() => void handleDelete(entry.name)}
                        >{translate("common:actions.delete")}</Button>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </Panel>

      </div>

      {isEditing && (
        <div
          className="backend-profiles-page__dialog-overlay"
          role="dialog"
          aria-modal="true"
          aria-labelledby="edit-profile-dialog-title"
        >
          <div
            className="backend-profiles-page__dialog backend-profiles-page__dialog--wide"
            onClick={(e) => e.stopPropagation()}
          >
            <header className="backend-profiles-page__dialog-header">
              <h3
                id="edit-profile-dialog-title"
                className="backend-profiles-page__dialog-title"
              >{translate("settings:backendProfilesPage.backendProfilesPageDialogHeader_message_editConfig", { editingName: editingName })}</h3>
              <p className="backend-profiles-page__dialog-subtitle">{translate("settings:backendProfilesPage.backendProfilesPageDialogHeader_message_configTranslationBackendProjectConfigTranslationBackend")}</p>
            </header>

            <div className="backend-profiles-page__dialog-body">
              <div className="config-form">
                <BackendConfigEditor
                  config={editingConfig}
                  onChange={setEditingConfig}
                />

                <ProxyConfigEditor
                  proxyConfig={(editingConfig.proxy as Record<string, unknown>) || {}}
                  onChange={(newProxy) => {
                    setEditingConfig((prev) => ({ ...prev, proxy: newProxy }));
                    setSaveSuccess(false);
                  }}
                />
              </div>
              {error && <InlineFeedback tone="error" description={error} />}
            </div>

            <div className="form-actions">
              <Button variant="secondary" onClick={handleCancel} disabled={saving}>{translate("common:actions.cancel")}</Button>
              <Button
                onClick={() => void handleSave()}
                disabled={saving || !editingName.trim()}
              >
                {saving ? translate("common:actions.saving") : translate("settings:backendProfilesPage.formActions_message_saveConfig")}
              </Button>
            </div>
          </div>
        </div>
      )}

      {showNewDialog && (
        <div
          className="backend-profiles-page__dialog-overlay"
          role="dialog"
          aria-modal="true"
          aria-labelledby="new-profile-dialog-title"
          onClick={closeNewDialog}
        >
          <div
            className="backend-profiles-page__dialog"
            onClick={(e) => e.stopPropagation()}
          >
            <h3
              id="new-profile-dialog-title"
              className="backend-profiles-page__dialog-title"
            >{translate("settings:backendProfilesPage.backendProfilesPageDialog_message_newBackendConfig")}</h3>
            <label className="field">
              <span>{translate("settings:backendProfilesPage.field_message_configName")}</span>
              <input
                type="text"
                value={newProfileName}
                onChange={(e) => setNewProfileName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') { e.preventDefault(); void handleCreate(); }
                  else if (e.key === 'Escape') { e.preventDefault(); closeNewDialog(); }
                }}
                placeholder={translate("settings:backendProfilesPage.field_placeholder_gpt5")}
                autoFocus
                disabled={creating}
              />
              <span className="field__hint">{translate("settings:backendProfilesPage.field_message_configNameCreateChangeEdit")}</span>
            </label>
            {error && <InlineFeedback tone="error" description={error} />}
            <div className="form-actions">
              <Button
                onClick={() => void handleCreate()}
                disabled={creating || !newProfileName.trim()}
              >
                {creating ? translate("settings:backendProfilesPage.formActions_message_create") : translate("settings:backendProfilesPage.formActions_message_createVariant2")}
              </Button>
              <Button variant="secondary" onClick={closeNewDialog} disabled={creating}>{translate("common:actions.cancel")}</Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
