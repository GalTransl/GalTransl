import { useCallback, useEffect, useRef, useState } from 'react';
import { CustomSelect } from '../components/CustomSelect';
import { PageHeader } from '../components/PageHeader';
import { ConnectionStatusCard } from '../features/connection/ConnectionStatusCard';
import { EmptyState, ErrorState, LoadingState } from '../components/page-state';
import { useConnection } from '../features/connection/ConnectionContext';
import {
  CUSTOM_BACKGROUND_OPACITY_MAX,
  CUSTOM_BACKGROUND_OPACITY_MIN,
  CUSTOM_BACKGROUND_SURFACE_OPACITY_MAX,
  CUSTOM_BACKGROUND_SURFACE_OPACITY_MIN,
  type PluginInfo,
  type ThemeMode,
  clearCustomBackgroundPreference,
  fetchPlugins,
  getCustomBackgroundPreference,
  getHideBackendConsolePreference,
  getHomeHistoryRetentionLimit,
  getHomeJobRetentionLimit,
  getThemeModePreference,
  HOME_LIST_LIMIT_MAX,
  HOME_LIST_LIMIT_MIN,
  setCustomBackgroundPreference,
  setHideBackendConsolePreference,
  setHomeHistoryRetentionLimit,
  setHomeJobRetentionLimit,
  setThemeModePreference,
} from '../lib/api';
import { normalizeError } from '../lib/errors';


function PluginListSection() {
  const [plugins, setPlugins] = useState<PluginInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [typeFilter, setTypeFilter] = useState<string>('');

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchPlugins()
      .then((res) => {
        if (!cancelled) setPlugins(res);
      })
      .catch((err) => {
        if (!cancelled) setError(normalizeError(err, '加载插件列表失败'));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, []);

  const filePlugins = plugins.filter((p) => p.type === 'file');
  const textPlugins = plugins.filter((p) => p.type === 'text');
  const filteredPlugins = typeFilter === 'file' ? filePlugins : typeFilter === 'text' ? textPlugins : plugins;

  return (
    <section className="panel">
      <header className="panel__header">
        <div>
          <h2>插件清单</h2>
          <p>查看当前可用的翻译插件。</p>
        </div>
      </header>

      {loading ? (
        <LoadingState title="加载插件中…" description="正在读取当前可用的文件插件与文本插件。" />
      ) : error ? (
        <ErrorState title="加载插件列表失败" description={error} />
      ) : (
        <>
          <div className="plugin-tabs">
            <button
              className={`plugin-tab ${typeFilter === '' ? 'plugin-tab--active' : ''}`}
              onClick={() => setTypeFilter('')}
            >
              全部 ({plugins.length})
            </button>
            <button
              className={`plugin-tab ${typeFilter === 'file' ? 'plugin-tab--active' : ''}`}
              onClick={() => setTypeFilter('file')}
            >
              文件插件 ({filePlugins.length})
            </button>
            <button
              className={`plugin-tab ${typeFilter === 'text' ? 'plugin-tab--active' : ''}`}
              onClick={() => setTypeFilter('text')}
            >
              文本插件 ({textPlugins.length})
            </button>
          </div>

          <div className="plugin-list">
            {filteredPlugins.length === 0 ? (
              <EmptyState
                title={typeFilter ? '当前筛选下没有插件' : '暂无插件'}
                description={typeFilter ? '试试切换到其他插件类型，或检查后端插件目录。' : '后端暂未返回任何插件信息。'}
              />
            ) : filteredPlugins.map((plugin) => (
              <div key={plugin.name} className="plugin-card">
                <div className="plugin-card__header">
                  <span className="plugin-card__name">{plugin.display_name}</span>
                  <span className="plugin-card__version">v{plugin.version}</span>
                  <span className={`plugin-card__type plugin-card__type--${plugin.type}`}>
                    {plugin.type === 'file' ? '文件' : '文本'}
                  </span>
                </div>
                <div className="plugin-card__meta">
                  {plugin.author && <span>作者: {plugin.author}</span>}
                  <span>模块: {plugin.module}</span>
                </div>
                {plugin.description && (
                  <p className="plugin-card__desc">{plugin.description}</p>
                )}
                {Object.keys(plugin.settings).length > 0 && (
                  <div className="plugin-card__settings">
                    <h4>设置项</h4>
                    {Object.entries(plugin.settings).map(([key, value]) => (
                      <div key={key} className="plugin-setting-item">
                        <span className="plugin-setting-item__key">{key}:</span>
                        <span className="plugin-setting-item__value">{String(value)}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        </>
      )}
    </section>
  );
}


export function SettingsPage() {
  const {
    backendUrl,
    connectionPhase,
    connectionMessage,
    loadingInitialData,
    refreshingJobs,
    loadInitialData,
    translators } = useConnection();

  const [homeHistoryLimitInput, setHomeHistoryLimitInput] = useState(() => String(getHomeHistoryRetentionLimit()));
  const [homeJobLimitInput, setHomeJobLimitInput] = useState(() => String(getHomeJobRetentionLimit()));
  const [themeMode, setThemeMode] = useState<ThemeMode>(() => getThemeModePreference());
  const [hideBackendConsole, setHideBackendConsole] = useState(() => getHideBackendConsolePreference());
  const [customBackgroundImageDataUrl, setCustomBackgroundImageDataUrl] = useState(
    () => getCustomBackgroundPreference().imageDataUrl,
  );
  const [customBackgroundImageName, setCustomBackgroundImageName] = useState(
    () => getCustomBackgroundPreference().imageName,
  );
  const [customBackgroundOpacityInput, setCustomBackgroundOpacityInput] = useState(
    () => String(getCustomBackgroundPreference().opacity),
  );
  const [customBackgroundSurfaceOpacityInput, setCustomBackgroundSurfaceOpacityInput] = useState(
    () => String(getCustomBackgroundPreference().surfaceOpacity),
  );
  const [customBackgroundError, setCustomBackgroundError] = useState<string | null>(null);
  const [customBackgroundBusy, setCustomBackgroundBusy] = useState(false);
  const customBackgroundInputRef = useRef<HTMLInputElement | null>(null);

  const applyHomeHistoryLimit = useCallback((rawValue: string) => {
    const next = setHomeHistoryRetentionLimit(rawValue.trim() === '' ? Number.NaN : Number(rawValue));
    setHomeHistoryLimitInput(String(next));
  }, []);

  const applyHomeJobLimit = useCallback((rawValue: string) => {
    const next = setHomeJobRetentionLimit(rawValue.trim() === '' ? Number.NaN : Number(rawValue));
    setHomeJobLimitInput(String(next));
  }, []);

  const applyThemeMode = useCallback((mode: ThemeMode) => {
    const next = setThemeModePreference(mode);
    setThemeMode(next);
  }, []);

  const applyHideBackendConsole = useCallback((enabled: boolean) => {
    const next = setHideBackendConsolePreference(enabled);
    setHideBackendConsole(next);
  }, []);

  const applyCustomBackgroundOpacity = useCallback((rawValue: string) => {
    const current = getCustomBackgroundPreference();
    try {
      const next = setCustomBackgroundPreference({
        imageDataUrl: current.imageDataUrl,
        imageName: current.imageName,
        opacity: rawValue.trim() === '' ? Number.NaN : Number(rawValue),
        surfaceOpacity: current.surfaceOpacity,
      });
      setCustomBackgroundOpacityInput(String(next.opacity));
    } catch (err) {
      setCustomBackgroundError(normalizeError(err, '保存背景设置失败'));
    }
  }, []);

  const applyCustomBackgroundSurfaceOpacity = useCallback((rawValue: string) => {
    const current = getCustomBackgroundPreference();
    try {
      const next = setCustomBackgroundPreference({
        imageDataUrl: current.imageDataUrl,
        imageName: current.imageName,
        opacity: current.opacity,
        surfaceOpacity: rawValue.trim() === '' ? Number.NaN : Number(rawValue),
      });
      setCustomBackgroundSurfaceOpacityInput(String(next.surfaceOpacity));
    } catch (err) {
      setCustomBackgroundError(normalizeError(err, '保存背景设置失败'));
    }
  }, []);

  const handleCustomBackgroundFileChange = useCallback(async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) {
      return;
    }
    if (!file.type.startsWith('image/')) {
      setCustomBackgroundError('请选择图片文件。');
      return;
    }

    setCustomBackgroundBusy(true);
    setCustomBackgroundError(null);
    try {
      const dataUrl = await compressImageToDataUrl(file);
      const current = getCustomBackgroundPreference();
      const next = setCustomBackgroundPreference({
        imageDataUrl: dataUrl,
        imageName: file.name,
        opacity: current.opacity,
        surfaceOpacity: current.surfaceOpacity,
      });
      setCustomBackgroundImageDataUrl(next.imageDataUrl);
      setCustomBackgroundImageName(next.imageName);
      setCustomBackgroundOpacityInput(String(next.opacity));
      setCustomBackgroundSurfaceOpacityInput(String(next.surfaceOpacity));
    } catch (err) {
      const message = normalizeError(err, '保存背景失败');
      // 典型原因：localStorage 配额溢出。提醒用户换更小的图。
      const isQuota = err instanceof DOMException && (err.name === 'QuotaExceededError' || err.code === 22);
      setCustomBackgroundError(
        isQuota ? '图片过大，浏览器本地存储空间不足。请选择更小或更低分辨率的图片。' : message,
      );
    } finally {
      setCustomBackgroundBusy(false);
    }
  }, []);

  const clearCustomBackground = useCallback(() => {
    const next = clearCustomBackgroundPreference();
    setCustomBackgroundImageDataUrl(next.imageDataUrl);
    setCustomBackgroundImageName(next.imageName);
    setCustomBackgroundOpacityInput(String(next.opacity));
    setCustomBackgroundSurfaceOpacityInput(String(next.surfaceOpacity));
    setCustomBackgroundError(null);
  }, []);

  const triggerCustomBackgroundPicker = useCallback(() => {
    customBackgroundInputRef.current?.click();
  }, []);

  return (
    <div className="settings-page">
      <PageHeader className="settings-page__header" title="设置" description="管理应用配置和后端连接。" />

      <div className="settings-page__content">
        <section className="panel">
          <header className="panel__header">
            <div>
              <h2>外观</h2>
              <p>设置界面主题风格，以及半透明的全局自定义背景。</p>
            </div>
          </header>

          <label className="settings-number-row">
            <span className="settings-number-row__label">主题模式</span>
            <div className="settings-number-row__control">
              <CustomSelect
                value={themeMode}
                onChange={(event) => {
                  applyThemeMode(event.target.value as ThemeMode);
                }}
              >
                <option value="light">浅色</option>
                <option value="dark">深色</option>
                <option value="system">跟随系统</option>
              </CustomSelect>
            </div>
          </label>

          <label className="settings-toggle-row">
            <span className="settings-toggle-row__label">隐藏服务端控制台</span>
            <div className="settings-toggle-row__control">
              <input
                type="checkbox"
                checked={hideBackendConsole}
                onChange={(event) => {
                  applyHideBackendConsole(event.target.checked);
                }}
              />
            </div>
          </label>

          <label className="settings-number-row">
            <span className="settings-number-row__label">自定义背景</span>
            <div className="settings-number-row__control settings-background-control">
              <input
                ref={customBackgroundInputRef}
                className="settings-background-control__file-input"
                type="file"
                accept="image/*"
                onChange={handleCustomBackgroundFileChange}
              />
              <button
                type="button"
                className="settings-background-control__pick"
                onClick={triggerCustomBackgroundPicker}
                disabled={customBackgroundBusy}
              >
                {customBackgroundBusy ? '处理中…' : customBackgroundImageDataUrl ? '更换图片' : '选择图片'}
              </button>
              <span
                className={`settings-background-control__filename${customBackgroundImageName ? '' : ' settings-background-control__filename--empty'}`}
                title={customBackgroundImageName || '尚未选择图片'}
              >
                {customBackgroundImageName || '尚未选择图片'}
              </span>
              <button
                type="button"
                className="settings-background-control__clear"
                onClick={clearCustomBackground}
                disabled={!customBackgroundImageDataUrl || customBackgroundBusy}
                aria-label="清除自定义背景"
              >
                清除
              </button>
            </div>
          </label>

          {customBackgroundError && (
            <div className="settings-background-control__error" role="alert">
              {customBackgroundError}
            </div>
          )}

          <label className="settings-number-row">
            <span className="settings-number-row__label">背景透明度</span>
            <div className="settings-number-row__control settings-opacity-control">
              <input
                type="range"
                min={CUSTOM_BACKGROUND_OPACITY_MIN}
                max={CUSTOM_BACKGROUND_OPACITY_MAX}
                value={customBackgroundOpacityInput}
                onChange={(event) => {
                  setCustomBackgroundOpacityInput(event.target.value);
                  applyCustomBackgroundOpacity(event.target.value);
                }}
              />
              <input
                type="number"
                min={CUSTOM_BACKGROUND_OPACITY_MIN}
                max={CUSTOM_BACKGROUND_OPACITY_MAX}
                value={customBackgroundOpacityInput}
                onChange={(event) => {
                  setCustomBackgroundOpacityInput(event.target.value);
                }}
                onBlur={() => {
                  applyCustomBackgroundOpacity(customBackgroundOpacityInput);
                }}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') {
                    event.currentTarget.blur();
                  }
                }}
              />
            </div>
          </label>

          <label className="settings-number-row">
            <span className="settings-number-row__label">容器不透明度</span>
            <div className="settings-number-row__control settings-opacity-control">
              <input
                type="range"
                min={CUSTOM_BACKGROUND_SURFACE_OPACITY_MIN}
                max={CUSTOM_BACKGROUND_SURFACE_OPACITY_MAX}
                value={customBackgroundSurfaceOpacityInput}
                onChange={(event) => {
                  setCustomBackgroundSurfaceOpacityInput(event.target.value);
                  applyCustomBackgroundSurfaceOpacity(event.target.value);
                }}
              />
              <input
                type="number"
                min={CUSTOM_BACKGROUND_SURFACE_OPACITY_MIN}
                max={CUSTOM_BACKGROUND_SURFACE_OPACITY_MAX}
                value={customBackgroundSurfaceOpacityInput}
                onChange={(event) => {
                  setCustomBackgroundSurfaceOpacityInput(event.target.value);
                }}
                onBlur={() => {
                  applyCustomBackgroundSurfaceOpacity(customBackgroundSurfaceOpacityInput);
                }}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') {
                    event.currentTarget.blur();
                  }
                }}
              />
            </div>
          </label>

          <div className="settings-toggle-row__desc">
            {customBackgroundImageDataUrl ? '已启用自定义背景。' : '未设置自定义背景。'}
            主题、背景和容器透底设置会即时生效，并在下次打开应用时保持。
            自动拉起服务端时会按“隐藏服务端控制台”选项决定是否显示命令行窗口。
          </div>
        </section>

        <section className="panel">
          <header className="panel__header">
            <div>
              <h2>首页记忆保留</h2>
              <p>控制首页历史项目与翻译任务列表保留条数。</p>
            </div>
          </header>

          <label className="settings-number-row">
            <span className="settings-number-row__label">历史项目保留条数</span>
            <div className="settings-number-row__control">
              <input
                type="number"
                min={HOME_LIST_LIMIT_MIN}
                max={HOME_LIST_LIMIT_MAX}
                value={homeHistoryLimitInput}
                onChange={(event) => {
                  setHomeHistoryLimitInput(event.target.value);
                }}
                onBlur={() => {
                  applyHomeHistoryLimit(homeHistoryLimitInput);
                }}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') {
                    event.currentTarget.blur();
                  }
                }}
              />
            </div>
          </label>

          <label className="settings-number-row">
            <span className="settings-number-row__label">翻译任务保留条数</span>
            <div className="settings-number-row__control">
              <input
                type="number"
                min={HOME_LIST_LIMIT_MIN}
                max={HOME_LIST_LIMIT_MAX}
                value={homeJobLimitInput}
                onChange={(event) => {
                  setHomeJobLimitInput(event.target.value);
                }}
                onBlur={() => {
                  applyHomeJobLimit(homeJobLimitInput);
                }}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') {
                    event.currentTarget.blur();
                  }
                }}
              />
            </div>
          </label>

          <div className="settings-toggle-row__desc">
            取值范围 {HOME_LIST_LIMIT_MIN}-{HOME_LIST_LIMIT_MAX}。超出范围会自动修正。
          </div>
        </section>

        <ConnectionStatusCard
          backendUrl={backendUrl}
          connectionMessage={connectionMessage}
          connectionPhase={connectionPhase}
          isRefreshing={loadingInitialData || refreshingJobs}
          onRefresh={() => {
            void loadInitialData();
          }}
          translatorCount={translators.length}
        />

        <PluginListSection />
      </div>
    </div>
  );
}

// ── 壁纸图像压缩 ──
// 将用户选择的图片通过 canvas 重绘为较小的 JPEG data URL，再写入 localStorage。
// 这里的根因：原实现把原始文件直接 base64 编码存入 localStorage，大图极易触发 QuotaExceededError，
// 之前该错误还被静默吞掉，导致重启后加载到的仍是上一次成功保存的旧壁纸。
const CUSTOM_BACKGROUND_MAX_EDGE = 1920;
const CUSTOM_BACKGROUND_JPEG_QUALITY = 0.82;

async function compressImageToDataUrl(file: File): Promise<string> {
  const objectUrl = URL.createObjectURL(file);
  try {
    const img = await new Promise<HTMLImageElement>((resolve, reject) => {
      const image = new Image();
      image.onload = () => resolve(image);
      image.onerror = () => reject(new Error('无法读取图片文件。'));
      image.src = objectUrl;
    });

    const scale = Math.min(1, CUSTOM_BACKGROUND_MAX_EDGE / Math.max(img.naturalWidth, img.naturalHeight));
    const targetWidth = Math.max(1, Math.round(img.naturalWidth * scale));
    const targetHeight = Math.max(1, Math.round(img.naturalHeight * scale));

    const canvas = document.createElement('canvas');
    canvas.width = targetWidth;
    canvas.height = targetHeight;
    const ctx = canvas.getContext('2d');
    if (!ctx) {
      throw new Error('当前环境不支持 canvas 压缩。');
    }
    ctx.drawImage(img, 0, 0, targetWidth, targetHeight);

    // 半透明 PNG 会很大，统一转 JPEG；若原图带透明通道则用黑色填充，视觉影响可忽略。
    return canvas.toDataURL('image/jpeg', CUSTOM_BACKGROUND_JPEG_QUALITY);
  } finally {
    URL.revokeObjectURL(objectUrl);
  }
}
