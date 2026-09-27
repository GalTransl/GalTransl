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
      title: '配置翻译模型',
      description: hasProfile && !hasDefault
        ? '已有模型配置，但还没有设为全局默认，新项目会找不到后端。'
        : '在「模型设置」中新建配置，填写 API 地址、密钥与模型名，并设为全局默认。',
      done: modelReady,
      action: <Button variant={modelReady ? 'secondary' : 'primary'} onClick={() => navigate('/backend-profiles')}>{hasProfile ? '去设置默认' : '去配置'}</Button>,
    },
    {
      title: '新建翻译项目',
      description: '跟随向导选择项目位置，导入从游戏中提取出的脚本文件（json 等），并选择翻译规范。',
      done: hasProject,
      action: <Button variant={hasProject || !modelReady ? 'secondary' : 'primary'} onClick={() => navigate('/new-project')}>新建项目</Button>,
    },
    {
      title: '先用 AI 生成 GPT 字典',
      description: '在项目的「项目字典」里点「AI生成GPT字典」：GenDic 读原文提取人名、地名与专有名词并统一译名，正式翻译时术语才前后一致（跳过也能翻译，但译名容易漂）。',
      // 已经开始翻译就算这步过去了：它只是「建议先做」，不该拖住引导
      done: dictGenerated || translatedOnce,
      action: (
        <Button
          variant={hasProject && modelReady && !dictGenerated ? 'primary' : 'secondary'}
          disabled={!hasProject || !onOpenProjectDictionary}
          onClick={onOpenProjectDictionary}
        >
          去生成字典
        </Button>
      ),
    },
    {
      title: '开始第一次翻译',
      description: '在项目的「翻译工作台」点击开始翻译，完成后在 gt_output 文件夹取回译文。',
      done: translatedOnce,
      action: (
        <Button
          variant={hasProject && modelReady ? 'primary' : 'secondary'}
          disabled={!hasProject || !onOpenLatestProject}
          onClick={onOpenLatestProject}
        >
          打开最近项目
        </Button>
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
    <section className="home-onboarding" aria-label="快速上手">
      <div className="home-onboarding__header">
        <div>
          <h2><Icon name="sparkle" /> 快速上手</h2>
          <p>第一次使用？按下面几步即可完成一次翻译（{doneCount}/{steps.length}）。也可以进入「Agent 模式」，用自然语言让 AI 帮你完成这些操作。</p>
        </div>
        <button type="button" className="home-onboarding__dismiss" onClick={handleDismiss} title="不再显示" aria-label="不再显示快速上手">
          <Icon name="close" />
        </button>
      </div>
      <ol className="home-onboarding__steps">
        {steps.map((step, index) => (
          <li
            key={step.title}
            className={`home-onboarding__step${step.done ? ' is-done' : ''}${index === currentIndex ? ' is-current' : ''}`}
          >
            <span className="home-onboarding__badge" aria-hidden="true">
              {step.done ? <Icon name="check" /> : index + 1}
            </span>
            <div className="home-onboarding__body">
              <strong>{step.title}</strong>
              <span>{step.description}</span>
            </div>
            {step.done ? <span className="home-onboarding__done">已完成</span> : step.action}
          </li>
        ))}
      </ol>
    </section>
  );
}
