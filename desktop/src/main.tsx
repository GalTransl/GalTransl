import React from 'react';
import ReactDOM from 'react-dom/client';
import { App } from './app/App';
import { applyAnimationsPreference } from './lib/api';
import { UiI18nProvider } from './i18n';
import './styles.css';

// 「关闭所有动画效果」在首帧之前落到根元素上，避免启动时的页面转场动画闪一下。
applyAnimationsPreference();

ReactDOM.createRoot(document.getElementById('root') as HTMLElement).render(
  <React.StrictMode>
    <UiI18nProvider><App /></UiI18nProvider>
  </React.StrictMode>,
);
