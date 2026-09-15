import { useCallback, useEffect, useState } from 'react';
import { BackendConfigEditor } from '../components/BackendConfigEditor';
import { Button } from '../components/Button';
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
  const [profiles, setProfiles] = useState<ProfileEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
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
      setError(normalizeError(err, '加载后端配置失败'));
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
      setError('配置名称不能为空');
      return;
    }
    if (profiles.some((p) => p.name === name)) {
      setError(`配置「${name}」已存在`);
      return;
    }
    setCreating(true);
    setError(null);
    try {
      const newConfig = JSON.parse(JSON.stringify(DEFAULT_BACKEND_CONFIG));
      await createBackendProfile(name, newConfig);
      setSaveSuccess(true);
      setShowNewDialog(false);
      setNewProfileName('');
      await loadProfiles();
      // Immediately open the edit dialog for the new profile
      setEditingName(name);
      setEditingConfig(newConfig);
      setIsEditing(true);
    } catch (err) {
      setError(normalizeError(err, '创建配置失败'));
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
      setError('配置名称不能为空');
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
      setError(normalizeError(err, '保存配置失败'));
    } finally {
      setSaving(false);
    }
  }, [editingName, editingConfig, loadProfiles]);

  const handleDelete = useCallback(async (name: string) => {
    if (!confirm(`确定要删除配置「${name}」吗？`)) return;
    try {
      await deleteBackendProfile(name);
      // If we're editing this profile, close the editor
      if (editingName === name) {
        handleCancel();
      }
      void loadProfiles();
    } catch (err) {
      setError(normalizeError(err, '删除配置失败'));
    }
  }, [editingName, handleCancel, loadProfiles]);

  return (
    <div className="backend-profiles-page">
      <PageHeader
        className="backend-profiles-page__header"
        title="🤖 模型设置"
        description="管理全局翻译后端配置，可在项目中直接选用，避免每个项目都重复配置。"
        status={
          <>
            {error && <InlineFeedback tone="error" title="操作失败" description={error} />}
            {saveSuccess && <InlineFeedback className="inline-alert--floating" tone="success" title="配置已保存" description="新的后端配置已写入，可在项目中直接选用。" onDismiss={() => setSaveSuccess(false)} />}
          </>
        }
      />

      <div className="backend-profiles-page__content">
        <Panel
          className="backend-profiles-page__list-panel"
          title="配置列表"
          description="已创建的全局翻译后端配置。"
          actions={(
            <Button onClick={openNewDialog}>
              + 新建配置
            </Button>
          )}
        >
          {loading ? (
            <LoadingState title="加载配置列表中…" description="正在读取全局翻译后端配置。" />
          ) : profiles.length === 0 ? (
            <EmptyState
              title="暂无配置"
              description="点击右上角「新建配置」按钮创建一个翻译后端配置。"
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
                          <span className="profile-card__badge">翻译器默认</span>
                        )}
                        {agentDefaultProfile === entry.name && (
                          <span className="profile-card__badge profile-card__badge--agent">Agent 默认</span>
                        )}
                      </div>
                      <div className="profile-card__meta">Base URL：{baseUrl}</div>
                      <div className="profile-card__meta">模型：{modelName}</div>
                    </div>
                    <div className="profile-card__actions">
                      {defaultProfile !== entry.name ? (
                        <Button
                          variant="secondary"
                          onClick={() => {
                            setDefaultBackendProfile(entry.name);
                            setDefaultProfileState(entry.name);
                          }}
                        >
                          设为翻译器默认
                        </Button>
                      ) : (
                        <Button
                          variant="secondary"
                          onClick={() => {
                            setDefaultBackendProfile('');
                            setDefaultProfileState('');
                          }}
                        >
                          取消翻译器默认
                        </Button>
                      )}
                      {agentDefaultProfile !== entry.name ? (
                        <Button
                          variant="secondary"
                          onClick={() => {
                            setAgentDefaultBackendProfile(entry.name);
                            setAgentDefaultState(entry.name);
                          }}
                        >
                          设为 Agent 默认
                        </Button>
                      ) : (
                        <Button
                          variant="secondary"
                          onClick={() => {
                            setAgentDefaultBackendProfile('');
                            setAgentDefaultState('');
                          }}
                        >
                          取消 Agent 默认
                        </Button>
                      )}
                      <Button
                        variant="secondary"
                        onClick={() => handleEdit(entry)}
                      >
                        编辑
                      </Button>
                      <Button
                        variant="secondary"
                        onClick={() => void handleDelete(entry.name)}
                      >
                        删除
                      </Button>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
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
              >
                编辑配置 - {editingName}
              </h3>
              <p className="backend-profiles-page__dialog-subtitle">
                配置翻译后端参数，与项目配置中的翻译后端设置一致。
              </p>
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
              <Button variant="secondary" onClick={handleCancel} disabled={saving}>
                取消
              </Button>
              <Button
                onClick={() => void handleSave()}
                disabled={saving || !editingName.trim()}
              >
                {saving ? '保存中…' : '保存配置'}
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
            >
              新建后端配置
            </h3>
            <label className="field">
              <span>配置名称</span>
              <input
                type="text"
                value={newProfileName}
                onChange={(e) => setNewProfileName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') { e.preventDefault(); void handleCreate(); }
                  else if (e.key === 'Escape') { e.preventDefault(); closeNewDialog(); }
                }}
                placeholder="例如：gpt5"
                autoFocus
                disabled={creating}
              />
              <span className="field__hint">配置名称创建后不可修改，可在列表中点击「编辑」填写具体参数。</span>
            </label>
            {error && <InlineFeedback tone="error" description={error} />}
            <div className="form-actions">
              <Button
                onClick={() => void handleCreate()}
                disabled={creating || !newProfileName.trim()}
              >
                {creating ? '创建中…' : '创建'}
              </Button>
              <Button variant="secondary" onClick={closeNewDialog} disabled={creating}>
                取消
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
