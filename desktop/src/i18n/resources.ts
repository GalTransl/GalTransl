import commonZh from "./locales/zh-CN/common.json";
import commonEn from "./locales/en/common.json";
import settingsZh from "./locales/zh-CN/settings.json";
import settingsEn from "./locales/en/settings.json";
import projectsZh from "./locales/zh-CN/projects.json";
import projectsEn from "./locales/en/projects.json";
import agentZh from "./locales/zh-CN/agent.json";
import agentEn from "./locales/en/agent.json";
import configZh from "./locales/zh-CN/config.json";
import configEn from "./locales/en/config.json";
import pluginsZh from "./locales/zh-CN/plugins.json";
import pluginsEn from "./locales/en/plugins.json";
import errorsZh from "./locales/zh-CN/errors.json";
import errorsEn from "./locales/en/errors.json";

export const resources = {
  "zh-CN": { common: commonZh, settings: settingsZh, projects: projectsZh, agent: agentZh, config: configZh, plugins: pluginsZh, errors: errorsZh },
  en: { common: commonEn, settings: settingsEn, projects: projectsEn, agent: agentEn, config: configEn, plugins: pluginsEn, errors: errorsEn },
};

type LeafPaths<T> = { [K in keyof T & string]: T[K] extends string ? K : T[K] extends object ? `${K}.${LeafPaths<T[K]>}` : never }[keyof T & string];
export type TranslationKey = { [N in keyof typeof resources["zh-CN"]]: `${N}:${LeafPaths<typeof resources["zh-CN"][N]>}` }[keyof typeof resources["zh-CN"]];
