import { t as translate, useUiLanguage } from "../../i18n";
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
  useUiLanguage();
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
          title={translate("common:updateNotice.updateNoticeClose_title_text")}
          aria-label={translate("common:updateNotice.updateNoticeClose_ariaLabel_text")}
        >
          <Icon name="close" size={13} />
        </button>

        <header className="update-notice__header">
          <span className="update-notice__badge" aria-hidden="true">
            <Icon name="sparkle" size={18} />
          </span>
          <div className="update-notice__heading">
            <h3 className="update-notice__title" id="update-notice-title">{translate("common:updateNotice.updateNoticeHeading_message_version")}</h3>
            <p className="update-notice__subtitle">{translate("common:updateNotice.updateNoticeHeading_message_galTranslVersion")}</p>
          </div>
        </header>

        <dl className="update-notice__versions">
          <div className="update-notice__version-row">
            <dt>{translate("common:updateNotice.updateNoticeVersionRow_message_currentVersion")}</dt>
            <dd>{translate("common:updateNotice.updateNoticeVersionRow_message_v", { value: currentVersion || '—' })}</dd>
          </div>
          <div className="update-notice__version-row update-notice__version-row--latest">
            <dt>{translate("common:updateNotice.updateNoticeVersionRowUpdateNoticeVersionRowLatest_message_version")}</dt>
            <dd>{translate("common:updateNotice.updateNoticeVersionRowUpdateNoticeVersionRowLatest_message_v", { latestVersion: latestVersion })}</dd>
          </div>
        </dl>

        <p className="update-notice__hint">{translate("common:updateNotice.updateNotice_message_directoryProjectTranslationCache")}</p>

        <div className="update-notice__actions">
          <Button type="button" variant="secondary" onClick={handleIgnore}>{translate("common:updateNotice.updateNoticeActions_message_update")}</Button>
          <Button type="button" onClick={handleOpenDownload}>{translate("common:updateNotice.updateNoticeActions_message_open")}</Button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
