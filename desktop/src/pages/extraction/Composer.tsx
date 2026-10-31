import { useEffect, useId, useRef, useState, type RefObject } from 'react';
import { Icon } from '../../components/Icon';
import { usePageActive } from '../../components/PageActivity';
import { useComposerAutosize } from '../../components/useComposerAutosize';
import { ComposerSelect } from '../agent/ComposerSelect';
import { ContextMeter, SendIcon, StopIcon } from '../agent/composer';
import { extractionContextUsage } from './presentation';
import { x } from './labels';
import type { AgentEvent, Session } from './types';
import { ImageAttachments, ImageInputStatus, type useImageInput } from '../agent/images';
import { getBackendProfile } from '../../lib/api';
import { formatProfileLabel } from '../../lib/backendProfile';
import { PERMISSION_MODES } from '../../lib/permissionMode';
import { t } from '../../i18n';

export function ExtractionComposer({ draft, setDraft, textarea, session, running, busy, ready, profile, profileSelection = profile, defaultProfile, setProfile, models, mode, setMode,
  context, compacting, send, stop, chooseSource, configure, imageInput }: {
  draft: string; setDraft: (text: string) => void; textarea: RefObject<HTMLTextAreaElement>;
  session?: Session; running: boolean; busy: boolean; ready: boolean;
  profile: string; setProfile: (value: string) => void; models: string[]; mode: string; setMode: (value: string) => void;
  profileSelection?: string; defaultProfile?: string;
  context?: AgentEvent; compacting?: boolean; send: () => void; stop: () => void; chooseSource: () => void; configure: () => void;
  imageInput?: ReturnType<typeof useImageInput>;
}) {
  const usage = extractionContextUsage(context);
  const canSend = Boolean((draft.trim() || imageInput?.images.length) && !imageInput?.processing && !running && !busy && profile && ready);
  useComposerAutosize(textarea, draft);
  return <div {...imageInput?.drop} className={`agent-composer extract-composer${running ? ' is-running' : ''}${imageInput?.dragging ? ' is-image-drop' : ''}`}>
    {imageInput && <ImageAttachments images={imageInput.images} remove={imageInput.remove} />}
    <textarea ref={textarea} className="agent-composer__input" aria-label={x('composer')} placeholder={x('composer')}
      onPaste={imageInput?.onPaste}
      value={draft} rows={2} maxLength={60000} onChange={event => setDraft(event.target.value)} onKeyDown={event => {
        if (event.key !== 'Enter' || event.shiftKey || event.metaKey || event.ctrlKey || event.nativeEvent.isComposing || !canSend) return;
        event.preventDefault(); send();
      }} />
    {imageInput && <ImageInputStatus input={imageInput} />}
    <div className="agent-composer__toolbar">
      <div className="agent-composer__left">
        {session ? <span className="agent-composer__chip agent-composer__chip--static" title={session.game_dir}>
          <span className="agent-composer__chip-icon"><Icon name="folder" /></span><span className="agent-composer__chip-label">{session.game_dir.split(/[\\/]/).filter(Boolean).pop()}</span>
        </span> : <button type="button" className="agent-composer__chip" onClick={chooseSource}><span className="agent-composer__chip-icon"><Icon name="folder" /></span><span className="agent-composer__chip-label">{x('chooseSource')}</span></button>}
        <ComposerSelect icon="settings" label={x('model')} value={profileSelection} onChange={setProfile} disabled={running || busy}
          options={[
            ...(defaultProfile ? [{ value: '', label: t('extraction:followAgentDefault', { profile: formatProfileLabel(defaultProfile, getBackendProfile(defaultProfile)) }) }] : []),
            ...models.map(name => ({ value: name, label: formatProfileLabel(name, getBackendProfile(name)) })),
          ]} emptyLabel={x('selectModel')} action={{ label: x('configureModel'), onClick: configure }} />
        <ComposerSelect icon="shield" label={x('permissions')} value={mode} onChange={setMode} disabled={busy}
          triggerClassName={mode === 'auto' || mode === 'auto-quiet' ? 'agent-composer__chip--auto' : undefined}
          options={PERMISSION_MODES.map(value => ({ value,
            label: x(value === 'ask' ? 'ask' : value === 'auto' ? 'auto' : value === 'auto-quiet' ? 'autoQuiet' : 'acceptEdits'),
            hint: value === 'auto' ? x('autoHint') : value === 'auto-quiet' ? x('autoQuietHint') : undefined,
          }))} />
        <ExtractionQuickCommands setDraft={setDraft} textarea={textarea} />
      </div>
      <div className="agent-composer__right">
        {compacting ? <span className="extract-context-note" role="status" title={x('compacting')}><Icon name="repeat" /></span>
          : usage ? <ContextMeter usage={usage} /> : context ? <span className="extract-context-note" title={x('contextUnknown')} aria-label={x('contextUnknown')}><Icon name="help" /></span> : null}
        <button type="button" className="agent-composer__send" onClick={send} disabled={!canSend} aria-label={x('send')} title={x('sendHint')}><SendIcon /></button>
        {running && <button type="button" className="agent-composer__stop" onClick={stop} disabled={busy || session?.status === 'stopping'}
          aria-label={x(session?.status === 'stopping' ? 'stopping' : 'stop')} title={x(session?.status === 'stopping' ? 'stopping' : 'stop')}><StopIcon /></button>}
      </div>
    </div>
  </div>;
}

function ExtractionQuickCommands({ setDraft, textarea }: {
  setDraft: (text: string) => void; textarea: RefObject<HTMLTextAreaElement>;
}) {
  const active = usePageActive();
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const id = useId();
  useEffect(() => { if (!active) setOpen(false); }, [active]);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false); };
    document.addEventListener('pointerdown', outside);
    return () => document.removeEventListener('pointerdown', outside);
  }, [open]);
  const showMenu = () => {
    setOpen(true);
    requestAnimationFrame(() => root.current?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus());
  };
  return <div className="agent-profile-picker extract-quick-commands" ref={root}
    onMouseEnter={() => setOpen(true)} onMouseLeave={() => {
      if (!root.current?.contains(document.activeElement)) setOpen(false);
    }} onBlur={event => {
      if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOpen(false);
    }} onKeyDown={event => {
      if (event.key === 'Escape') {
        event.preventDefault(); event.stopPropagation(); setOpen(false); trigger.current?.focus(); return;
      }
      if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      if (!open) { showMenu(); return; }
      const items = Array.from(root.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') || []);
      const current = items.indexOf(document.activeElement as HTMLButtonElement);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1
        : (current + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
      items[next]?.focus();
    }}>
    <button type="button" ref={trigger} className={`agent-composer__chip${open ? ' is-open' : ''}`}
      aria-haspopup="menu" aria-expanded={open} aria-controls={open ? id : undefined} onClick={showMenu}>
      <span className="agent-composer__chip-icon"><Icon name="sparkle" /></span>
      <span className="agent-composer__chip-label">{x('quickCommands')}</span>
    </button>
    {open && <div className="agent-profile-menu" id={id} role="menu" aria-label={x('quickCommands')}>
      {([{ label: 'quickExtractLabel', prompt: 'quickExtract' }, { label: 'quickPatchLabel', prompt: 'quickPatch' }] as const)
        .map(command => <button type="button" className="agent-profile-menu__item" key={command.prompt} role="menuitem" onClick={() => {
          setDraft(x(command.prompt)); setOpen(false); textarea.current?.focus();
        }}>{x(command.label)}</button>)}
    </div>}
  </div>;
}
