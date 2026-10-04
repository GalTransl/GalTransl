import { t as translate, useUiLanguage } from "../i18n";
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  BACKEND_PROFILES_CHANGE_EVENT,
  DEFAULT_BACKEND_PROFILE_CHANGE_EVENT,
  getBackendProfileNames,
  getDefaultBackendProfile,
} from '../lib/api';
import { Button } from './Button';
import { Icon } from './Icon';

const DISMISSED_KEY = 'galtransl-onboarding-dismissed';
const FIRST_TRANSLATION_KEY = 'galtransl-onboarding-first-translation';
const FIRST_DICT_KEY = 'galtransl-onboarding-first-dict';

function readFlag(key: string): boolean {
  try {
    return localStorage.getItem(key) === '1';
  } catch {
    return false;
  }
}

function writeFlag(key: string) {
  try {
    localStorage.setItem(key, '1');
  } catch {
    // ignore storage errors
  }
}

type GettingStartedProps = {
  hasProject: boolean;
  hasCompletedJob: boolean;
  /** 用 GenDic 生成过 GPT 字典（任务列表里有过跑完的 GenDic 任务） */
  hasGeneratedDict: boolean;
  /** 最近打开的项目，用于「开始翻译」一步直接跳过去 */
  onOpenLatestProject?: () => void;
  /** 最近打开的项目 → 项目字典页，用「AI生成GPT字典」先生成一份术语表 */
  onOpenProjectDictionary?: () => void;
};

/** 首页「快速上手」清单：走完（或手动关闭）后不再显示。 */
export function GettingStarted({
  hasProject,
  hasCompletedJob,
  hasGeneratedDict,
  onOpenLatestProject,
  onOpenProjectDictionary }: GettingStartedProps) {
  useUiLanguage();
  const navigate = useNavigate();
  const [dismissed, setDismissed] = useState(() => readFlag(DISMISSED_KEY));
  const [hasProfile, setHasProfile] = useState(() => getBackendProfileNames().length > 0);
  const [hasDefault, setHasDefault] = useState(() => Boolean(getDefaultBackendProfile()));
  const [translatedOnce, setTranslatedOnce] = useState(() => readFlag(FIRST_TRANSLATION_KEY));
  const [dictGenerated, setDictGenerated] = useState(() => readFlag(FIRST_DICT_KEY));

  useEffect(() => {
    const sync = () => {
      setHasProfile(getBackendProfileNames().length > 0);
      setHasDefault(Boolean(getDefaultBackendProfile()));
    };
    window.addEventListener(BACKEND_PROFILES_CHANGE_EVENT, sync);
    window.addEventListener(DEFAULT_BACKEND_PROFILE_CHANGE_EVENT, sync);
    return () => {
      window.removeEventListener(BACKEND_PROFILES_CHANGE_EVENT, sync);
      window.removeEventListener(DEFAULT_BACKEND_PROFILE_CHANGE_EVENT, sync);
    };
  }, []);

  // 任务列表可能被清空，所以「翻译过一次」「生成过字典」单独记下来
  useEffect(() => {
    if (hasCompletedJob && !translatedOnce) {
      writeFlag(FIRST_TRANSLATION_KEY);
      setTranslatedOnce(true);
    }
  }, [hasCompletedJob, translatedOnce]);

  useEffect(() => {
    if (hasGeneratedDict && !dictGenerated) {
      writeFlag(FIRST_DICT_KEY);
      setDictGenerated(true);
    }
  }, [hasGeneratedDict, dictGenerated]);

  const modelReady = hasProfile && hasDefault;
  const steps = [
    {
      title: translate("common:gettingStarted.title_title_configTranslationModel"),
      description: hasProfile && !hasDefault
        ? translate("common:gettingStarted.description_message_doneModelConfigEmptyDefaultProjectBackend")
        : translate("common:gettingStarted.description_message_modelSettingsNewConfigAPIAddressModel"),
      done: modelReady,
      action: <Button variant={modelReady ? 'secondary' : 'primary'} onClick={() => navigate('/backend-profiles')}>{hasProfile ? translate("common:gettingStarted.action_message_settingsDefault") : translate("common:gettingStarted.action_message_config")}</Button>,
    },
    {
      title: translate("common:gettingStarted.title_title_newTranslationProject"),
      description: translate("common:gettingStarted.description_description_selectProjectImportExtractFileJsonSelect"),
      done: hasProject,
      action: <Button variant={hasProject || !modelReady ? 'secondary' : 'primary'} onClick={() => navigate('/new-project')}>{translate("common:gettingStarted.action_action_newProject")}</Button>,
    },
    {
      title: translate("common:gettingStarted.title_title_aIGPTDictionary"),
      description: translate("common:gettingStarted.description_description_projectProjectDictionaryAIGPTDictionaryGenDic"),
      // 已经开始翻译就算这步过去了：它只是「建议先做」，不该拖住引导
      done: dictGenerated || translatedOnce,
      action: (
        <Button
          variant={hasProject && modelReady && !dictGenerated ? 'primary' : 'secondary'}
          disabled={!hasProject || !onOpenProjectDictionary}
          onClick={onOpenProjectDictionary}
        >{translate("common:gettingStarted.action_message_dictionary")}</Button>
      ),
    },
    {
      title: translate("common:gettingStarted.title_title_startTranslation"),
      description: translate("common:gettingStarted.description_description_projectStartTranslationTranslationCompleteGtOutput"),
      done: translatedOnce,
      action: (
        <Button
          variant={hasProject && modelReady ? 'primary' : 'secondary'}
          disabled={!hasProject || !onOpenLatestProject}
          onClick={onOpenLatestProject}
        >{translate("common:gettingStarted.action_message_openProject")}</Button>
      ),
    },
  ];

  const doneCount = steps.filter((step) => step.done).length;
  if (dismissed || doneCount === steps.length) {
    return null;
  }
  const currentIndex = steps.findIndex((step) => !step.done);

  const handleDismiss = () => {
    writeFlag(DISMISSED_KEY);
    setDismissed(true);
  };

  return (
    <section className="home-onboarding" aria-label={translate("common:gettingStarted.homeOnboarding_ariaLabel_text")}>
      <div className="home-onboarding__header">
        <div>
          <h2><Icon name="sparkle" />{translate("common:gettingStarted.homeOnboardingHeader_h2_text")}</h2>
          <p>{translate("common:gettingStarted.homeOnboardingHeader_message_completeTranslationAgentLanguageAIComplete", { doneCount: doneCount, count: steps.length })}</p>
        </div>
        <button type="button" className="home-onboarding__dismiss" onClick={handleDismiss} title={translate("common:gettingStarted.homeOnboardingDismiss_title_text")} aria-label={translate("common:gettingStarted.homeOnboardingDismiss_ariaLabel_text")}>
          <Icon name="close" />
        </button>
      </div>
      <ol className="home-onboarding__steps">
        {steps.map((step, index) => (
          <li
            key={index}
            className={`home-onboarding__step${step.done ? ' is-done' : ''}${index === currentIndex ? ' is-current' : ''}`}
          >
            <span className="home-onboarding__badge" aria-hidden="true">
              {step.done ? <Icon name="check" /> : index + 1}
            </span>
            <div className="home-onboarding__body">
              <strong>{step.title}</strong>
              <span>{step.description}</span>
            </div>
            {step.done ? <span className="home-onboarding__done">{translate("common:gettingStarted.homeOnboardingSteps_message_doneComplete")}</span> : step.action}
          </li>
        ))}
      </ol>
    </section>
  );
}
