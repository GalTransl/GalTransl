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
  return (
    <Panel
      title="后端连接"
      description="检查本机 Python 服务是否可用，并展示当前可读取到的翻译模板数量。"
      actions={
        <Button disabled={isRefreshing} onClick={onRefresh} variant="secondary">
          {isRefreshing ? '刷新中…' : '重新连接'}
        </Button>
      }
    >
      <div className="connection-card__status-row">
        <StatusBadge label={getPhaseLabel(connectionPhase)} tone={connectionPhase} />
        <span className="connection-card__url">{backendUrl}</span>
      </div>

      <p className="connection-card__message">{connectionMessage}</p>

      <dl className="meta-grid">
        <div>
          <dt>翻译模板数</dt>
          <dd>{translatorCount}</dd>
        </div>
        <div>
          <dt>轮询频率</dt>
          <dd>每 2 秒</dd>
        </div>
      </dl>
    </Panel>
  );
}

function getPhaseLabel(phase: ConnectionPhase) {
  switch (phase) {
    case 'online':
      return '已连接';
    case 'offline':
      return '离线';
    default:
      return '连接中';
  }
}
