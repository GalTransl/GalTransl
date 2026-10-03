import { t as translate, useUiLanguage } from "../../i18n";
import { Button } from '../../components/Button';
import { Panel } from '../../components/Panel';
import { StatusBadge } from '../../components/StatusBadge';
import type { ConnectionPhase } from '../../lib/api';

type ConnectionStatusCardProps = {
  backendUrl: string;
  connectionMessage: string;
  connectionPhase: ConnectionPhase;
  isRefreshing: boolean;
  onRefresh: () => void;
  translatorCount: number;
};

export function ConnectionStatusCard({
  backendUrl,
  connectionMessage,
  connectionPhase,
  isRefreshing,
  onRefresh,
  translatorCount,
}: ConnectionStatusCardProps) {
  useUiLanguage();
  return (
    <Panel
      title={translate("common:connectionStatusCard.connectionStatusCard_title_backendConnection")}
      description={translate("common:connectionStatusCard.connectionStatusCard_description_checkPythonCurrentReadTranslationCount")}
      actions={
        <Button disabled={isRefreshing} onClick={onRefresh} variant="secondary">
          {isRefreshing ? translate("common:connectionStatusCard.connectionStatusCard_message_text") : translate("common:connectionStatusCard.connectionStatusCard_message_reconnect")}
        </Button>
      }
    >
      <div className="connection-card__status-row">
        <StatusBadge label={getPhaseLabel(connectionPhase)} tone={connectionPhase} />
        <span className="connection-card__url">{backendUrl || translate("common:connectionStatusCard.connectionCardUrl_message_wait")}</span>
      </div>

      <p className="connection-card__message">{connectionMessage}</p>

      <dl className="meta-grid">
        <div>
          <dt>{translate("common:connectionStatusCard.metaGrid_message_translation")}</dt>
          <dd>{translatorCount}</dd>
        </div>
        <div>
          <dt>{translate("common:connectionStatusCard.metaGrid_message_text")}</dt>
          <dd>{translate("common:connectionStatusCard.metaGrid_message_2Seconds")}</dd>
        </div>
      </dl>
    </Panel>
  );
}

function getPhaseLabel(phase: ConnectionPhase) {
  switch (phase) {
    case 'online':
      return translate("common:connectionStatusCard.getPhaseLabel_message_doneConnection");
    case 'offline':
      return translate("common:connectionStatusCard.getPhaseLabel_message_text");
    default:
      return translate("common:connectionStatusCard.getPhaseLabel_message_connection");
  }
}
