import React from 'react';
import ReactDOM from 'react-dom/client';
import { App } from './app/App';
import { UiI18nProvider } from './i18n';
import './styles.css';

ReactDOM.createRoot(document.getElementById('root') as HTMLElement).render(
  <React.StrictMode>
    <UiI18nProvider><App /></UiI18nProvider>
  </React.StrictMode>,
);
