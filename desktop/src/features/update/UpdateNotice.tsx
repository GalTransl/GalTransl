import { message, t as translate, useMessageState, useUiLanguage } from "../../i18n";
import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { Button } from '../../components/Button';
import { Icon } from '../../components/Icon';
import { canInstallApplicationUpdate, fetchApplicationUpdateStatus, getIgnoredUpdateVersion, installApplicationUpdate, setIgnoredUpdateVersion, startApplicationUpdate } from '../../lib/api';
import type { ApplicationUpdateStatus } from '../../lib/api';
import { normalizeError } from '../../lib/errors';
import { RELEASE_LATEST_URL, openExternalUrl } from '../../lib/externalLink';
import { useConnection } from '../connection/ConnectionContext';

/**
 * 发现新版本时的提示弹窗。
 *
 * 自动升级、去发布页下载，或者「忽略本次更新」——忽略记的是版本号，
 * 所以同一个版本不再打扰，等更新的版本出现还会再提醒一次。右上角的 ×
 * （或点遮罩、按 Esc）只是这次先不弹，下次启动依然会提醒。
 */
export function UpdateNotice() {
  useUiLanguage();
  const { versionInfo } = useConnection();
  const [ignoredVersion, setIgnoredVersion] = useState(() => getIgnoredUpdateVersion());
  const [closedForNow, setClosedForNow] = useState(false);
  const [preserveDict, setPreserveDict] = useState(true);
  const [update, setUpdate] = useState<ApplicationUpdateStatus | null>(null);
  const [starting, setStarting] = useState(false);
  const [installing, setInstalling] = useState(false);
  const [error, setError] = useMessageState<string | null>(null);
  const downloading = Boolean(update && ['downloading', 'verifying', 'extracting'].includes(update.phase));
  const busy = starting || installing || downloading;
  const automatic = canInstallApplicationUpdate();

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
      if (event.key === 'Escape' && !busy) {
        setClosedForNow(true);
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [visible, busy]);

  useEffect(() => {
    if (!visible || !automatic) return;
    let disposed = false;
    void fetchApplicationUpdateStatus().then(result => { if (!disposed) setUpdate(result); })
      .catch(value => { if (!disposed) setError(normalizeError(value, message('common:updateNotice.updateFailed'))); });
    return () => { disposed = true; };
  }, [visible, automatic, setError]);

  useEffect(() => {
    if (!downloading) return;
    let disposed = false;
    const timer = setTimeout(() => {
      void fetchApplicationUpdateStatus().then(result => { if (!disposed) setUpdate(result); })
        .catch(value => {
          if (disposed) return;
          setError(normalizeError(value, message('common:updateNotice.updateFailed')));
          setUpdate(previous => previous ? { ...previous, phase: 'failed' } : previous);
        });
    }, 500);
    return () => { disposed = true; clearTimeout(timer); };
  }, [downloading, update, setError]);

  useEffect(() => {
    if (update?.phase !== 'ready') return;
    setInstalling(true);
    void installApplicationUpdate(update.id)
      .catch(value => setError(normalizeError(value, message('common:updateNotice.updateFailed'))))
      .finally(() => setInstalling(false));
  }, [update?.phase, update?.id, setError]);

  const beginUpdate = async () => {
    setError(null);
    if (update?.phase === 'ready') {
      setInstalling(true);
      try { await installApplicationUpdate(update.id); }
      catch (value) { setError(normalizeError(value, message('common:updateNotice.updateFailed'))); }
      finally { setInstalling(false); }
      return;
    }
    setStarting(true);
    try { setUpdate(await startApplicationUpdate(preserveDict)); }
    catch (value) { setError(normalizeError(value, message('common:updateNotice.updateFailed'))); }
    finally { setStarting(false); }
  };

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
      onClick={() => { if (!busy) setClosedForNow(true); }}
    >
      <div className="update-notice" onClick={(event) => event.stopPropagation()}>
        <button
          type="button"
          className="update-notice__close"
          onClick={() => setClosedForNow(true)}
          disabled={busy}
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

        <label className="update-notice__dictionary">
          <input type="checkbox" checked={preserveDict} disabled={busy || update?.phase === 'ready'} onChange={event => setPreserveDict(event.target.checked)} />
          <span>{translate('common:updateNotice.preserveDict')}</span>
        </label>
        {!automatic && <p className="update-notice__hint">{translate('common:updateNotice.desktopOnly')}</p>}
        {automatic && update && !update.supported && <p className="update-notice__hint">{translate(update.reason === 'development' ? 'common:updateNotice.developmentUnavailable' : 'common:updateNotice.installationUnavailable')}</p>}
        {busy && <div className="update-notice__progress" role="status" aria-live="polite">
          <span>{translate(installing ? 'common:updateNotice.installing' : update?.phase === 'verifying' ? 'common:updateNotice.verifying' : update?.phase === 'extracting' ? 'common:updateNotice.extracting' : 'common:updateNotice.downloading')}</span>
          <progress aria-label={translate('common:updateNotice.downloadProgress')} max={update?.total || 1} value={downloading && update?.total ? update.downloaded : undefined} />
          {downloading && Boolean(update?.total) && <small>{Math.round((update?.downloaded || 0) / (update?.total || 1) * 100)}%</small>}
        </div>}
        {(error || update?.error) && <p className="update-notice__error" role="alert">{error || update?.error}</p>}

        <div className="update-notice__actions">
          <Button type="button" variant="secondary" disabled={busy} onClick={handleIgnore}>{translate("common:updateNotice.updateNoticeActions_message_update")}</Button>
          <Button type="button" variant="secondary" disabled={busy} onClick={handleOpenDownload}>{translate("common:updateNotice.updateNoticeActions_message_open")}</Button>
          <Button type="button" disabled={busy || !automatic || !update?.supported} onClick={() => void beginUpdate()}>{translate('common:updateNotice.autoUpdate')}</Button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
