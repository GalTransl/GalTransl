import { useEffect, useRef, useState, type ClipboardEvent, type DragEvent, type RefObject } from 'react';
import { invoke, isTauri } from '@tauri-apps/api/core';
import { getCurrentWebviewWindow } from '@tauri-apps/api/webviewWindow';
import { getBackendBaseUrl } from '../../lib/api';
import { t, useUiLanguage } from '../../i18n';
import { usePageActive } from '../../components/PageActivity';

export type AgentImage = { id: string; name: string; mime_type?: string; size?: number; width?: number; height?: number; data_url?: string };
export type ImageScope = { sessionId: string; projectDir?: string; extraction?: boolean };
const MAX_BYTES = 2 * 1024 * 1024;
export const imageUploads = (images: AgentImage[]) => images.map(({ name, data_url }) => ({ name, data_url }));
export function imageUrl(image: AgentImage, scope?: ImageScope) {
  if (image.data_url?.startsWith('data:image/png;base64,') || image.data_url?.startsWith('data:image/jpeg;base64,')) return image.data_url;
  if (!scope || !/^[0-9a-f]{64}\.(png|jpg|gif|webp)$/.test(image.id)) return '';
  const query = new URLSearchParams({ session_id: scope.sessionId, image_id: image.id,
    ...(scope.extraction ? { agent: 'extraction' } : { project_dir: scope.projectDir || '' }) });
  return `${getBackendBaseUrl()}/api/agent/image?${query}`;
}

export async function prepareImage(file: File): Promise<AgentImage> {
  if (!/^image\/(png|jpeg|webp|gif|bmp)$/.test(file.type) || file.size > 20 * 1024 * 1024) throw new Error(t('agent:images.invalid'));
  const source = URL.createObjectURL(file);
  try {
    const image = new Image();
    image.src = source;
    await image.decode();
    if (!image.naturalWidth || !image.naturalHeight || image.naturalWidth * image.naturalHeight > 40_000_000) throw new Error(t('agent:images.invalid'));
    const scale = Math.min(1, 2048 / Math.max(image.naturalWidth, image.naturalHeight));
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
    canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
    const context = canvas.getContext('2d');
    if (!context) throw new Error(t('agent:images.invalid'));
    context.drawImage(image, 0, 0, canvas.width, canvas.height);
    let data_url = canvas.toDataURL('image/png');
    if ((data_url.length - data_url.indexOf(',') - 1) * .75 > MAX_BYTES) {
      context.globalCompositeOperation = 'destination-over';
      context.fillStyle = '#fff';
      context.fillRect(0, 0, canvas.width, canvas.height);
      for (const quality of [.9, .75, .6]) {
        data_url = canvas.toDataURL('image/jpeg', quality);
        if ((data_url.length - data_url.indexOf(',') - 1) * .75 <= MAX_BYTES) break;
      }
    }
    const size = Math.floor((data_url.length - data_url.indexOf(',') - 1) * .75);
    if (size > MAX_BYTES) throw new Error(t('agent:images.tooLarge'));
    return { id: crypto.randomUUID(), name: file.name || 'image.png', data_url, size, width: canvas.width, height: canvas.height };
  } finally { URL.revokeObjectURL(source); }
}

export function useImageInput(key: string, target: RefObject<HTMLTextAreaElement>, disabled = false) {
  const active = usePageActive();
  const drafts = useRef(new Map<string, AgentImage[]>());
  const current = useRef({ key, active, disabled }); current.current = { key, active, disabled };
  const [revision, refresh] = useState(0);
  const [processing, setProcessing] = useState(false);
  const [error, setError] = useState('');
  const [dragging, setDragging] = useState(false);
  const pending = useRef(false);
  const images = drafts.current.get(key) || [];
  useEffect(() => { setError(''); setDragging(false); }, [key, active]);
  const removeSent = (sent: AgentImage[]) => {
    const ids = new Set(sent.map(image => image.id));
    for (const [scope, images] of drafts.current) drafts.current.set(scope, images.filter(image => !ids.has(image.id)));
    refresh(value => value + 1);
  };
  const move = (to: string) => {
    const images = drafts.current.get(key);
    if (images?.length) { drafts.current.set(to, images); drafts.current.delete(key); refresh(value => value + 1); }
  };
  const add = async (files: File[]) => {
    const captured = current.current.key;
    if (!current.current.active || current.current.disabled || pending.current || !files.length) return;
    const previous = drafts.current.get(captured) || [];
    if (previous.length + files.length > 4) { setError(t('agent:images.limit')); return; }
    pending.current = true; setProcessing(true); setError('');
    try {
      const added: AgentImage[] = [];
      for (const file of files) added.push(await prepareImage(file));
      if (current.current.key !== captured || !current.current.active) return;
      const existing = drafts.current.get(captured) || [];
      if ([...existing, ...added].reduce((sum, image) => sum + (image.size || 0), 0) > 6 * 1024 * 1024) throw new Error(t('agent:images.tooLarge'));
      drafts.current.set(captured, [...existing, ...added]);
      // Images stay in RAM, never localStorage. Keep the number of retained draft scopes bounded.
      while (drafts.current.size > 8) {
        const oldest = [...drafts.current.keys()].find(scope => scope !== captured)!;
        drafts.current.delete(oldest);
      }
      refresh(value => value + 1);
    } catch (error) { if (current.current.key === captured) setError(error instanceof Error ? error.message : String(error)); }
    finally { pending.current = false; setProcessing(false); }
  };
  const addRef = useRef(add); addRef.current = add;
  useEffect(() => {
    if (!active || !isTauri()) return;
    let disposed = false;
    const listener = getCurrentWebviewWindow().onDragDropEvent(event => {
      if (disposed || current.current.disabled) return;
      const payload = event.payload;
      const bounds = target.current?.closest('.agent-composer')?.getBoundingClientRect();
      if (payload.type === 'leave') { setDragging(false); return; }
      const position = payload.position;
      const ratio = window.devicePixelRatio || 1;
      const within = bounds && position.x / ratio >= bounds.left && position.x / ratio <= bounds.right
        && position.y / ratio >= bounds.top && position.y / ratio <= bounds.bottom;
      setDragging(Boolean(within) && payload.type !== 'drop');
      if (payload.type !== 'drop' || !within) return;
      if (payload.paths.length > 4) { setError(t('agent:images.limit')); return; }
      const captured = current.current.key;
      void Promise.all(payload.paths.map(async path => {
        const bytes = await invoke<number[]>('read_image_file', { path });
        const extension = path.split('.').pop()?.toLowerCase();
        const mime = extension === 'jpg' || extension === 'jpeg' ? 'image/jpeg' : `image/${extension}`;
        return new File([new Uint8Array(bytes)], path.split(/[\\/]/).pop() || 'image', { type: mime });
      })).then(files => { if (!disposed && captured === current.current.key) return addRef.current(files); })
        .catch(error => { if (!disposed && captured === current.current.key) setError(String(error)); });
    });
    return () => { disposed = true; void listener.then(unlisten => unlisten()).catch(() => {}); };
  }, [active, target]);
  const onPaste = (event: ClipboardEvent) => {
    const files = Array.from(event.clipboardData.items).filter(item => item.kind === 'file' && item.type.startsWith('image/')).map(item => item.getAsFile()).filter((file): file is File => Boolean(file));
    if (files.length) { event.preventDefault(); void add(files); }
  };
  const onDragOver = (event: DragEvent) => { if (event.dataTransfer.types.includes('Files')) { event.preventDefault(); event.dataTransfer.dropEffect = 'copy'; setDragging(true); } };
  const onDrop = (event: DragEvent) => { event.preventDefault(); setDragging(false); void add(Array.from(event.dataTransfer.files)); };
  return { images, processing, error, dragging, move, removeSent, remove: (id: string) => removeSent(images.filter(image => image.id === id)),
    onPaste, drop: { onDragOver, onDrop, onDragLeave: () => setDragging(false) }, revision };
}

export function ImageAttachments({ images, scope, remove }: { images: AgentImage[]; scope?: ImageScope; remove?: (id: string) => void }) {
  useUiLanguage();
  const [opened, setOpened] = useState<AgentImage | null>(null);
  const close = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!opened) return;
    const previous = document.activeElement as HTMLElement | null;
    close.current?.focus();
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); setOpened(null); } };
    document.addEventListener('keydown', escape, true);
    return () => { document.removeEventListener('keydown', escape, true); previous?.focus(); };
  }, [opened]);
  if (!images.length) return null;
  return <><div className="agent-images">{images.map(image => <div className="agent-images__item" key={image.id}>
    <button type="button" className="agent-images__preview" title={image.name} aria-label={t('agent:images.preview', { name: image.name })} onClick={() => setOpened(image)}><img src={imageUrl(image, scope)} alt={image.name} loading="lazy" /></button>
    {remove && <button type="button" className="agent-images__remove" aria-label={t('agent:images.remove', { name: image.name })} onClick={() => remove(image.id)}>×</button>}
  </div>)}</div>{opened && <div className="agent-image-viewer" role="dialog" aria-modal="true" aria-label={opened.name} onClick={() => setOpened(null)}>
    <button type="button" ref={close} onClick={() => setOpened(null)} aria-label={t('agent:images.close')}>×</button><img src={imageUrl(opened, scope)} alt={opened.name} onClick={event => event.stopPropagation()} />
  </div>}</>;
}

export function ImageInputStatus({ input }: { input: Pick<ReturnType<typeof useImageInput>, 'processing' | 'error' | 'dragging'> }) {
  useUiLanguage();
  return <>{input.error && <div className="agent-images__error" role="alert">{input.error}</div>}
    {(input.processing || input.dragging) && <div className="agent-images__hint" role="status">{t(input.processing ? 'agent:images.processing' : 'agent:images.drop')}</div>}</>;
}
