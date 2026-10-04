import { t as translate, useMessageState, useUiLanguage } from "../../i18n";
import { useEffect, useMemo, useState } from 'react';
import { Panel } from '../../components/Panel';
import { PluginSettingsEditor } from '../../components/PluginSettingsEditor';
import { localizePlugin } from '../../i18n/plugins';
import { fetchProblemTypes, type ProblemTypeInfo, type PluginInfo } from '../../lib/api';

interface ProblemAnalyzeSectionProps {
  config: Record<string, unknown> | null;
  projectId?: string;
  problemPlugins: PluginInfo[];
  onProblemPluginsChange: (plugins: string[]) => void;
  onPluginSettingChange: (pluginName: string, key: string, value: unknown) => void;
  onProblemListChange: (lines: string[] | null) => void;
  onDirty: () => void;
}

export function readProblemList(config: Record<string, unknown> | null, problemTypes: ProblemTypeInfo[] | null): string[] {
  const pa = (config?.problemAnalyze as Record<string, unknown>) || {};
  const raw = pa.problemList ?? pa.GPT35;
  if (Array.isArray(raw)) {
    return raw.map((x) => String(x ?? '').trim()).filter(Boolean);
  }
  if (typeof raw === 'string') {
    return raw.split(/\r?\n/).map((x) => x.trim()).filter(Boolean);
  }
  return (problemTypes ?? []).filter((item) => item.default_enabled).map((item) => item.name);
}

export function problemPluginOverrides(config: Record<string, unknown> | null, plugin: PluginInfo): Record<string, unknown> {
  const legacy: Record<string, unknown> = {};
  for (const [key, field] of Object.entries(plugin.settings_schema ?? {})) {
    if (!field.legacy_path) continue;
    let node: unknown = config;
    for (const part of field.legacy_path.split('.')) {
      node = node && typeof node === 'object' ? (node as Record<string, unknown>)[part] : undefined;
    }
    if (node !== undefined) legacy[key] = node;
  }
  const configured = (config?.plugin as Record<string, unknown>) ?? {};
  return { ...legacy, ...((configured[plugin.module] as Record<string, unknown>) ?? {}) };
}

export function isProblemTypeAvailable(item: ProblemTypeInfo, enabledPlugins: string[]): boolean {
  return item.plugins === undefined || item.plugins.some((name) => enabledPlugins.includes(name));
}

export function ProblemAnalyzeSection({ config, projectId, problemPlugins, onProblemPluginsChange, onPluginSettingChange, onProblemListChange, onDirty }: ProblemAnalyzeSectionProps) {
  const uiLanguage = useUiLanguage();
  const [problemTypes, setProblemTypes] = useState<ProblemTypeInfo[] | null>(null);
  const [loadError, setLoadError] = useMessageState<string | null>(null);
  const pluginConfig = (config?.plugin as Record<string, unknown>) || {};
  const enabledPlugins = Array.isArray(pluginConfig.problemPlugins)
    ? pluginConfig.problemPlugins as string[]
    : Object.prototype.hasOwnProperty.call(pluginConfig, 'problemPlugins') ? [] : ['problem_common'];
  const togglePlugin = (name: string) => {
    onProblemPluginsChange(enabledPlugins.includes(name)
      ? enabledPlugins.filter((item) => item !== name)
      : [...enabledPlugins, name]);
  };

  useEffect(() => {
    let cancelled = false;
    setProblemTypes(null);
    setLoadError(null);
    fetchProblemTypes(projectId)
      .then((list) => {
        if (!cancelled) {
          setProblemTypes(list);
          setLoadError(null);
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setLoadError(err instanceof Error ? err.message : String(err));
        }
      });
    return () => { cancelled = true; };
  }, [projectId]);

  const selected = useMemo(() => readProblemList(config, problemTypes), [config, problemTypes]);
  const selectedSet = useMemo(() => new Set(selected), [selected]);
  const availableTypes = (problemTypes ?? []).filter((item) => isProblemTypeAvailable(item, enabledPlugins));
  const enabledTypeCount = availableTypes.filter((item) => selectedSet.has(item.name)).length;

  // Keep entries the user already has in config even if backend doesn't list them
  // (e.g. future types or custom strings); render them at the bottom.
  const extras = useMemo(() => {
    if (!problemTypes) return [] as string[];
    const known = new Set(problemTypes.map((t) => t.name));
    return selected.filter((name) => !known.has(name));
  }, [problemTypes, selected]);

  const commit = (nextSet: Set<string>) => {
    // Preserve the original order from backend, then append unknown extras.
    const ordered: string[] = [];
    if (problemTypes) {
      for (const t of problemTypes) {
        if (nextSet.has(t.name)) ordered.push(t.name);
      }
    }
    for (const name of extras) {
      if (nextSet.has(name)) ordered.push(name);
    }
    onProblemListChange(ordered);
    onDirty();
  };

  const toggle = (name: string, checked: boolean) => {
    const next = new Set(selectedSet);
    if (checked) next.add(name);
    else next.delete(name);
    commit(next);
  };

  const selectAll = () => {
    if (!problemTypes) return;
    const next = new Set<string>([...selectedSet, ...availableTypes.map((t) => t.name)]);
    commit(next);
  };

  const clearAll = () => {
    commit(new Set());
  };

  const restoreDefaults = () => {
    onProblemListChange(null);
    onDirty();
  };

  const pluginsByName = new Map(problemPlugins.map((plugin) => [
    `${plugin.project_local ? '(project_dir)' : ''}${plugin.name}`, plugin,
  ]));
  const pluginNames = [...new Set([
    ...pluginsByName.keys(), ...enabledPlugins,
    ...(problemTypes ?? []).flatMap((item) => item.plugins ?? []),
  ])];
  const unassignedTypes = (problemTypes ?? []).filter((item) => !item.plugins?.length);

  const renderType = (item: ProblemTypeInfo, available: boolean) => {
    const checked = selectedSet.has(item.name);
    const sources = (item.plugins ?? []).map((name) => {
      const plugin = pluginsByName.get(name);
      return plugin ? localizePlugin(plugin).display_name : name;
    });
    return (
      <li key={item.name}
        className={`problem-analyze-section__item${checked ? ' problem-analyze-section__item--checked' : ''}${available ? '' : ' problem-analyze-section__item--disabled'}`}>
        <label className="problem-analyze-section__label">
          <input type="checkbox" className="problem-analyze-section__checkbox"
            checked={checked} disabled={!available}
            onChange={(e) => toggle(item.name, e.target.checked)} />
          <span className="problem-analyze-section__item-body">
            <span className="problem-analyze-section__name">{item.name}</span>
            {item.description && <span className="problem-analyze-section__desc">{item.description}</span>}
            {sources.length > 1 && <span className="problem-analyze-section__desc">
              {translate('config:problemAnalyzeSection.sourcePlugins', { names: sources.join(', ') })}
            </span>}
            {!available && <span className="problem-analyze-section__desc">
              {translate('config:problemAnalyzeSection.providerDisabled')}
            </span>}
          </span>
        </label>
      </li>
    );
  };

  return (
    <Panel
      title={translate("config:problemAnalyzeSection.problemAnalyzeSection_title_problem")}
      description={translate("config:problemAnalyzeSection.problemAnalyzeSection_description_selectEnableTranslationProblemDetectItemTranslation")}
    >
      <div className="problem-analyze-section plugin-section">
        <div className="plugin-section__title">{translate('plugins:pluginsPage.pluginCardHeader_message_problem')}</div>
        <div className="problem-analyze-section__toolbar">
          <span className="problem-analyze-section__count">{translate("config:problemAnalyzeSection.problemAnalyzeSectionToolbar_message_doneEnable", { count: enabledTypeCount, value: problemTypes?.length ?? 0 })}</span>
          <div className="problem-analyze-section__toolbar-actions">
            <button type="button" className="problem-analyze-section__btn"
              onClick={selectAll} disabled={availableTypes.length === 0}>
              {translate("common:actions.selectAll")}
            </button>
            <button type="button" className="problem-analyze-section__btn"
              onClick={clearAll} disabled={!problemTypes || selectedSet.size === 0}>
              {translate("common:actions.clearAll")}
            </button>
            <button type="button" className="problem-analyze-section__btn"
              onClick={restoreDefaults} disabled={!problemTypes}>
              {translate('config:problemAnalyzeSection.restorePluginDefaults')}
            </button>
          </div>
        </div>
        {loadError && (
          <div className="problem-analyze-section__error">{translate("config:problemAnalyzeSection.problemAnalyzeSection_message_loadBackendProblemItemFailed", { loadError: loadError })}</div>
        )}

        {problemTypes === null && !loadError ? (
          <div className="problem-analyze-section__loading">{translate("config:problemAnalyzeSection.problemAnalyzeSection_message_pendingLoadBackendProblemItem")}</div>
        ) : null}
        <div className="plugin-check-list">
          {pluginNames.map((name) => {
            const plugin = pluginsByName.get(name);
            const enabled = enabledPlugins.includes(name);
            const types = (problemTypes ?? []).filter((item) => item.plugins?.includes(name));
            return <div key={name} className="plugin-check-item" data-plugin-name={name}>
              <label className="plugin-check-item__header">
                <input type="checkbox" checked={enabled} onChange={() => togglePlugin(name)} />
                <span className="plugin-check-item__name">{plugin ? localizePlugin(plugin).display_name : name}</span>
                {plugin && <span className="plugin-check-item__module">({name})</span>}
              </label>
              {plugin && enabled && Object.keys(plugin.settings).length > 0 && <div className="plugin-check-item__settings">
                <PluginSettingsEditor plugin={plugin}
                  overrides={problemPluginOverrides(config, plugin)}
                  onChange={(_name, key, value) => onPluginSettingChange(plugin.module, key, value)} />
              </div>}
              {types.length > 0 && <ul className="problem-analyze-section__list plugin-check-item__problems">
                {types.map((item) => renderType(item, enabled))}
              </ul>}
            </div>;
          })}
        </div>
        {(unassignedTypes.length > 0 || extras.length > 0) && (
            <ul className="problem-analyze-section__list">
              {unassignedTypes.map((item) => renderType(item, isProblemTypeAvailable(item, enabledPlugins)))}
              {extras.map((name) => (
                <li
                  key={`extra-${name}`}
                  className="problem-analyze-section__item problem-analyze-section__item--checked problem-analyze-section__item--extra"
                >
                  <label className="problem-analyze-section__label">
                    <input
                      type="checkbox"
                      className="problem-analyze-section__checkbox"
                      checked
                      onChange={(e) => toggle(name, e.target.checked)}
                    />
                    <span className="problem-analyze-section__item-body">
                      <span className="problem-analyze-section__name">{name}</span>
                      <span className="problem-analyze-section__desc problem-analyze-section__desc--warn">{translate("config:problemAnalyzeSection.problemAnalyzeSectionItemBody_message_currentBackendNotProblemItemCancelConfig")}</span>
                    </span>
                  </label>
                </li>
              ))}
            </ul>
        )}
      </div>
    </Panel>
  );
}
