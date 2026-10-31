import { Icon } from '../../components/Icon';
import { t, useUiLanguage } from '../../i18n';
import type { QueuedMessage } from '../../lib/api';
import { ImageAttachments, type ImageScope } from './images';

export function MessageQueue({ queued, editing, onEditingChange, onSave, onSend, onDelete, scope, busy = false }: {
  queued: QueuedMessage[]; editing: { id: string; text: string } | null;
  onEditingChange: (editing: { id: string; text: string } | null) => void;
  onSave: () => void; onSend: (id: string) => void; onDelete: (id: string) => void;
  scope: ImageScope; busy?: boolean;
}) {
  useUiLanguage();
  if (!queued.length) return null;
  return <div className="agent-queue"><div className="agent-queue__list">
    {queued.map(item => <div className="agent-queue__item" key={item.id}>
      {editing?.id === item.id ? <>
        <input className="agent-queue__input" value={editing.text} autoFocus maxLength={60000} disabled={busy}
          onChange={event => onEditingChange({ id: item.id, text: event.target.value })}
          onKeyDown={event => {
            if (event.key === 'Escape') { onEditingChange(null); return; }
            if (event.key !== 'Enter' || event.nativeEvent.isComposing || busy) return;
            event.preventDefault(); onSave();
          }} />
        <button type="button" className="agent-queue__act is-primary" disabled={busy || (!editing.text.trim() && !item.images?.length)} onClick={onSave}>{t('common:actions.save')}</button>
        <button type="button" className="agent-queue__act" disabled={busy} onClick={() => onEditingChange(null)}>{t('common:actions.cancel')}</button>
      </> : <>
        <span className="agent-queue__text" title={item.text}>{item.text}</span>
        {item.images?.length ? <ImageAttachments images={item.images} scope={scope} /> : null}
        <button type="button" className="agent-queue__act is-primary" disabled={busy} title={t('agent:agentPage.agentQueueActIsPrimary_title_agentSendEntry')} onClick={() => onSend(item.id)}>
          <span className="agent-queue__act-icon"><Icon name="send-now" /></span>{t('agent:agentPage.agentQueueActIsPrimary_button_text')}
        </button>
        <button type="button" className="agent-queue__act" disabled={busy} title={t('agent:agentPage.agentQueueAct_title_editEntry')} aria-label={t('agent:agentPage.agentQueueAct_ariaLabel_editEntry')} onClick={() => onEditingChange({ id: item.id, text: item.text })}><Icon name="pencil" /></button>
        <button type="button" className="agent-queue__act" disabled={busy} title={t('agent:agentPage.agentQueueAct_title_deleteEntry')} aria-label={t('agent:agentPage.agentQueueAct_ariaLabel_deleteEntry')} onClick={() => onDelete(item.id)}><Icon name="trash" /></button>
      </>}
    </div>)}
  </div></div>;
}
