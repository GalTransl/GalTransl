import { useCallback, useEffect, useState } from 'react';
import { BackendConfigEditor } from '../components/BackendConfigEditor';
import { Button } from '../components/Button';
import { PageHeader } from '../components/PageHeader';
import { Panel } from '../components/Panel';
import { EmptyState, InlineFeedback, LoadingState } from '../components/page-state';
import { ProxyConfigEditor } from '../components/ProxyConfigEditor';
import {
  createBackendProfile,
  deleteBackendProfile,
  fetchBackendProfiles,
  getDefaultBackendProfile,
  setDefaultBackendProfile } from '../lib/api';
import { normalizeError } from '../lib/errors';

type ProfileEntry = {
  name: string;
  config: Record<string, unknown>;
};

const DEFAULT_BACKEND_CONFIG: Record<string, unknown> = {};
const MISSING_PROFILE_META = '—';

function getRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function getFirstArrayRecord(value: unknown): Record<string, unknown> | null {
  if (!Array.isArray(value) || value.length === 0) return null;
  return getRecord(value[0]);
}

function getFirstArrayString(value: unknown): string | null {
  if (!Array.isArray(value) || value.length === 0) return null;
  return getNonEmptyString(value[0]);
}

function getNonEmptyString(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

function getProfileMeta(config: Record<string, unknown>) {
  const openAiCompatible = getRecord(config['OpenAI-Compatible']);
  const firstOpenAiToken = getFirstArrayRecord(openAiCompatible?.tokens);
  const sakuraLlm = getRecord(config.SakuraLLM);
  const firstSakuraEndpoint = getFirstArrayString(sakuraLlm?.endpoints);

  const baseUrl =
    getNonEmptyString(firstOpenAiToken?.endpoint) ??
    firstSakuraEndpoint ??
    MISSING_PROFILE_META;

  const modelName =
    getNonEmptyString(firstOpenAiToken?.modelName) ??
    getNonEmptyString(sakuraLlm?.rewriteModelName) ??
    MISSING_PROFILE_META;

  return { baseUrl, modelName };
}


export function BackendProfilesPage() {
  const [profiles, setProfiles] = useState<ProfileEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [defaultProfile, setDefaultProfileState] = useState(getDefaultBackendProfile());

  // Editor state
  const [editingName, setEditingName] = useState('');
  const [editingConfig, setEditingConfig] = useState<Record<string, unknown>>(DEFAULT_BACKEND_CONFIG);
  const [isEditing, setIsEditing] = useState(false);
  const [isNew, setIsNew] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveSuccess, setSaveSuccess] = useState(false);

  const loadProfiles = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchBackendProfiles();
      const entries: ProfileEntry[] = Object.entries(data.profiles || {}).map(
        ([name, config]) => ({ name, config: config as Record<string, unknown> })
      );
      setProfiles(entries);
    } catch (err) {
      setError(normalizeError(err, '加载后端配置失败'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadProfiles();
  }, [loadProfiles]);

  const handleNew = useCallback(() => {
    setIsEditing(true);
    setIsNew(true);
    setEditingName('');
    setEditingConfig(JSON.parse(JSON.stringify(DEFAULT_BACKEND_CONFIG)));
    setSaveSuccess(false);
    setError(null);
  }, []);

  const handleEdit = useCallback((entry: ProfileEntry) => {
    setIsEditing(true);
    setIsNew(false);
    setEditingName(entry.name);
    setEditingConfig(JSON.parse(JSON.stringify(entry.config)));
    setSaveSuccess(false);
    setError(null);
  }, []);

  const handleCancel = useCallback(() => {
    setIsEditing(false);
    setIsNew(false);
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
      setIsNew(false);
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
        title="🤖 翻译后端配置"
        description="管理全局翻译后端配置，可在项目中直接选用，避免每个项目都重复配置。"
        status={
          <>
            {error && <InlineFeedback tone="error" title="操作失败" description={error} />}
            {saveSuccess && <InlineFeedback tone="success" title="配置已保存" description="新的后端配置已写入，可在项目中直接选用。" />}
          </>
        }
      />

      <div className="backend-profiles-page__content">
        <Panel title="配置列表" description="已创建的全局翻译后端配置。">
          {loading ? (
            <LoadingState title="加载配置列表中…" description="正在读取全局翻译后端配置。" />
          ) : profiles.length === 0 ? (
            <EmptyState
              title="暂无配置"
              description="点击下方「新建配置」按钮创建一个翻译后端配置。"
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
                          <span className="profile-card__badge">默认</span>
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
                          设为默认
                        </Button>
                      ) : (
                        <Button
                          variant="secondary"
                          onClick={() => {
                            setDefaultBackendProfile('');
                            setDefaultProfileState('');
                          }}
                        >
                          取消默认
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

          <div className="form-actions" style={{ marginTop: '16px' }}>
            <Button onClick={handleNew} disabled={isEditing}>
              + 新建配置
            </Button>
          </div>
        </Panel>

        {isEditing && (
          <Panel
            title={isNew ? '新建配置' : `编辑配置 - ${editingName}`}
            description="配置翻译后端参数，与项目配置中的翻译后端设置一致。"
          >
            <div className="config-form">
              <label className="field">
                <span>配置名称</span>
                <input
                  type="text"
                  value={editingName}
                  onChange={(e) => setEditingName(e.target.value)}
                  placeholder="例如：gpt5"
                  disabled={!isNew}
                />
                <span className="field__hint">配置名称创建后不可修改</span>
              </label>

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

            <div className="form-actions" style={{ marginTop: '16px' }}>
              <Button
                onClick={() => void handleSave()}
                disabled={saving || !editingName.trim()}
              >
                {saving ? '保存中…' : '保存配置'}
              </Button>
              <Button variant="secondary" onClick={handleCancel}>
                取消
              </Button>
            </div>
          </Panel>
        )}
      </div>
    </div>
  );
}
