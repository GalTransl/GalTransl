import React from 'react';
import ReactDOM from 'react-dom/client';
import { App } from './app/App';
import { applyAnimationsPreference, migrateLegacyStoredDefaults } from './lib/api';
import { installDocumentMotionPause } from './lib/documentMotionPause';
import { installNativeContextMenuBlock } from './lib/nativeContextMenu';
import { initializeProjectStorage } from './lib/projectStorage';
import { UiI18nProvider } from './i18n';
import { UiMotionProvider } from './components/UiMotion';
import './styles.css';

// 「关闭所有动画效果」在首帧之前落到根元素上，避免启动时的页面转场动画闪一下。
initializeProjectStorage();
// 要在读任何偏好之前：把旧版本存下的默认值清掉。
migrateLegacyStoredDefaults();
applyAnimationsPreference();
// 窗口不可见时停掉 CSS 动画，避免最小化期间继续占合成层。
installDocumentMotionPause();
// 浏览器自带的右键菜单只在输入框和选中文字上保留。
installNativeContextMenuBlock();

ReactDOM.createRoot(document.getElementById('root') as HTMLElement).render(
  <React.StrictMode>
    <UiI18nProvider><UiMotionProvider><App /></UiMotionProvider></UiI18nProvider>
  </React.StrictMode>,
);
