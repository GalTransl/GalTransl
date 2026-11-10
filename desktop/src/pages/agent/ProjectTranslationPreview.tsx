import '../../styles/pages/project-translation-preview.css';
import { createContext, lazy, Suspense, useCallback, useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { Icon } from '../../components/Icon';
import { PageActivityContext, usePageActive } from '../../components/PageActivity';
import type { ProjectPageContext } from '../../components/ProjectLayout';
import { RenderErrorBoundary } from '../../components/RenderErrorBoundary';
import { m, UI_TRANSITION } from '../../components/UiMotion';
import { t, useUiLanguage } from '../../i18n';
import { encodeProjectDir, readConfigFileName } from '../../lib/api';
import { basenamePath } from '../../lib/paths';
import { useUiAnimations } from '../../lib/useUiAnimations';
import { ProjectPreviewContext, ProjectPreviewScopeContext, type ProjectPreviewRequest } from '../../components/ProjectPreviewContext';

const ProjectTranslatePage = lazy(async () => {
  const page = await import('../ProjectTranslatePage');
  return { default: page.ProjectTranslatePage };
});
const ProjectDictionaryPage = lazy(async () => {
  const page = await import('../ProjectDictionaryPage');
  return { default: page.ProjectDictionaryPage };
});
const CommonDictionaryPage = lazy(async () => {
  const page = await import('../CommonDictionaryPage');
  return { default: page.CommonDictionaryPage };
});
const ProjectCachePage = lazy(async () => {
  const page = await import('../ProjectCachePage');
  return { default: page.ProjectCachePage };
});

export const ProjectTranslationPreviewContext = createContext<((directory: string, configFileName?: string) => void) | null>(null);

export function ProjectTranslationPreviewProvider({ children, projectDir = '', configFileName }: { children: ReactNode; projectDir?: string; configFileName?: string }) {
  const active = usePageActive();
  const [project, setProject] = useState<ProjectPageContext | null>(null);
  const [open, setOpen] = useState(false);
  const scope = useMemo(() => ({ projectDir, configFileName }), [projectDir, configFileName]);
  const [target, setTarget] = useState<ProjectPreviewRequest>({ page: 'translate', projectDir: '' });
  const showPage = useCallback((request: ProjectPreviewRequest) => {
    setProject({ projectDir: request.projectDir, projectId: encodeProjectDir(request.projectDir), configFileName: request.configFileName || readConfigFileName(request.projectDir) });
    setTarget({ ...request });
    setOpen(true);
  }, []);
  const show = useCallback((directory: string, file?: string) => showPage({ page: 'translate', projectDir: directory, configFileName: file }), [showPage]);
  const close = useCallback(() => setOpen(false), []);
  useEffect(() => {
    if (!active) setOpen(false);
  }, [active]);

  return <ProjectTranslationPreviewContext.Provider value={show}>
    <ProjectPreviewContext.Provider value={showPage}>
      <ProjectPreviewScopeContext.Provider value={scope}>{children}</ProjectPreviewScopeContext.Provider>
      {project && <ProjectTranslationPreview project={project} target={target} show={showPage} open={open && active} close={close} />}
    </ProjectPreviewContext.Provider>
  </ProjectTranslationPreviewContext.Provider>;
}

function ProjectTranslationPreview({ project, target, show, open, close }: { project: ProjectPageContext; target: ProjectPreviewRequest; show: (request: ProjectPreviewRequest) => void; open: boolean; close: () => void }) {
  useUiLanguage();
  const animated = useUiAnimations();
  const titleId = useId();
  const dialogRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    const previousFocus = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        close();
        return;
      }
      if (event.key !== 'Tab') return;
      const controls = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>('button, a[href], input, select, textarea, [tabindex="0"]') || [])
        .filter(element => !element.hasAttribute('disabled') && element.getClientRects().length > 0);
      const next = event.shiftKey ? controls[controls.length - 1] : controls[0];
      const edge = event.shiftKey ? controls[0] : controls[controls.length - 1];
      if (document.activeElement === edge || !dialogRef.current?.contains(document.activeElement)) {
        event.preventDefault();
        next?.focus();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      previousFocus?.focus();
    };
  }, [open, close]);

  // 不带 (project_dir) 前缀的 file_key 是通用字典：它不属于任何项目，要开通用字典管理页
  const commonTarget = target.page === 'dictionary' && !target.fileKey.startsWith('(project_dir)') ? target : null;
  const commonDictionary = commonTarget !== null;
  // 保留工作台实例：收回窗口不会中断提交、停止请求或连续重译。
  return createPortal(<m.div className="agent-translation-preview-backdrop"
    initial={false}
    animate={open ? { opacity: 1, visibility: 'visible' } : { opacity: 0, transitionEnd: { visibility: 'hidden' } }}
    transition={animated ? UI_TRANSITION : { duration: 0 }}
    style={{ pointerEvents: open ? 'auto' : 'none' }}
    aria-hidden={!open || undefined} {...(!open ? { inert: '' } : {})}
    onClick={event => { if (event.target === event.currentTarget) close(); }}>
    <m.section ref={dialogRef} className="agent-translation-preview" role="dialog" aria-modal={open || undefined} aria-labelledby={titleId}
      initial={animated ? { opacity: 0, scale: 0.92, y: 16 } : false}
      animate={open ? { opacity: 1, scale: 1, y: 0 } : { opacity: 0, scale: 0.92, y: 16 }}
      transition={animated ? UI_TRANSITION : { duration: 0 }}>
      <header className="agent-translation-preview__header">
        <h2 id={titleId}><Icon name={target.page === 'translate' ? 'play' : target.page === 'dictionary' ? 'book' : 'file-text'} />{t(target.page === 'translate' ? 'common:projectLayout.label_label_startTranslation' : target.page === 'dictionary' ? (commonDictionary ? 'common:sidebar.sidebarBottomNav_message_dictionary' : 'common:projectLayout.label_label_projectDictionary') : 'common:projectLayout.label_label_text')}
          {!commonDictionary && <span>{basenamePath(project.projectDir)}</span>}
        </h2>
        <button ref={closeRef} type="button" onClick={close} aria-label={t('common:actions.close')}><Icon name="close" /></button>
      </header>
      <div className="agent-translation-preview__viewport">
        <PageActivityContext.Provider value={open}>
          <RenderErrorBoundary key={`${project.projectDir}:${project.configFileName}`}>
            <Suspense fallback={<div className="inline-feedback">{t('common:actions.loading')}</div>}>
              {target.page === 'translate' && <ProjectTranslatePage ctx={project} />}
              {commonTarget && <CommonDictionaryPage active={open} focusTarget={commonTarget} />}
              {target.page === 'dictionary' && !commonDictionary && <ProjectDictionaryPage ctx={project} active={open} focusTarget={target}
                onOpenInCache={query => show({ ...project, page: 'cache', query })} />}
              {target.page === 'cache' && <ProjectCachePage ctx={project} active={open} focusTarget={target} />}
            </Suspense>
          </RenderErrorBoundary>
        </PageActivityContext.Provider>
      </div>
    </m.section>
  </m.div>, document.body);
}
