import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { Button } from '../../components/Button';
import { Icon } from '../../components/Icon';
import { getIgnoredUpdateVersion, setIgnoredUpdateVersion } from '../../lib/api';
import { RELEASE_LATEST_URL, openExternalUrl } from '../../lib/externalLink';
import { useConnection } from '../connection/ConnectionContext';

/**
 * 发现新版本时的提示弹窗。
 *
 * 两条出路：去发布页下载，或者「忽略本次更新」——忽略记的是版本号，
 * 所以同一个版本不再打扰，等更新的版本出现还会再提醒一次。右上角的 ×
 * （或点遮罩、按 Esc）只是这次先不弹，下次启动依然会提醒。
 */
export function UpdateNotice() {
  const { versionInfo } = useConnection();
  const [ignoredVersion, setIgnoredVersion] = useState(() => getIgnoredUpdateVersion());
  const [closedForNow, setClosedForNow] = useState(false);

  const currentVersion = versionInfo?.version ?? '';
  const latestVersion = versionInfo?.latest_version?.trim() ?? '';
  const visible = Boolean(
    versionInfo?.update_available &&
      latestVersion &&
      latestVersion !== ignoredVersion &&
      !closedForNow,
  );

  useEffect(() => {
    if (!visible) {
      return undefined;
    }
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setClosedForNow(true);
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [visible]);

  if (!visible) {
    return null;
  }

  const handleOpenDownload = () => {
    setClosedForNow(true);
    void openExternalUrl(RELEASE_LATEST_URL);
  };

  const handleIgnore = () => {
    setIgnoredUpdateVersion(latestVersion);
    setIgnoredVersion(latestVersion);
    setClosedForNow(true);
  };

  return createPortal(
    <div
      className="update-notice__overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="update-notice-title"
      onClick={() => setClosedForNow(true)}
    >
      <div className="update-notice" onClick={(event) => event.stopPropagation()}>
        <button
          type="button"
          className="update-notice__close"
          onClick={() => setClosedForNow(true)}
          title="稍后提醒"
          aria-label="稍后提醒"
        >
          <Icon name="close" size={13} />
        </button>

        <header className="update-notice__header">
          <span className="update-notice__badge" aria-hidden="true">
            <Icon name="sparkle" size={18} />
          </span>
          <div className="update-notice__heading">
            <h3 className="update-notice__title" id="update-notice-title">
              发现新版本
            </h3>
            <p className="update-notice__subtitle">GalTransl 有新版本可以下载了。</p>
          </div>
        </header>

        <dl className="update-notice__versions">
          <div className="update-notice__version-row">
            <dt>当前版本</dt>
            <dd>v{currentVersion || '—'}</dd>
          </div>
          <div className="update-notice__version-row update-notice__version-row--latest">
            <dt>最新版本</dt>
            <dd>v{latestVersion}</dd>
          </div>
        </dl>

        <p className="update-notice__hint">
          到发布页下载新版压缩包，解压后覆盖原目录即可；项目和翻译缓存都不受影响。
        </p>

        <div className="update-notice__actions">
          <Button type="button" variant="secondary" onClick={handleIgnore}>
            忽略本次更新
          </Button>
          <Button type="button" onClick={handleOpenDownload}>
            打开下载页面
          </Button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
