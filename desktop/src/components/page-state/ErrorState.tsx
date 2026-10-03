import { t as translate, useUiLanguage } from "../../i18n";
import type { ReactNode } from 'react';
import { InlineFeedback } from './InlineFeedback';

type ErrorStateProps = {
  title?: string;
  description: ReactNode;
  action?: ReactNode;
  className?: string;
};

export function ErrorState({
  title = translate("common:errorState.errorState_message_loadFailed"),
  description,
  action,
  className,
}: ErrorStateProps) {
  useUiLanguage();
  return (
    <InlineFeedback
      tone="error"
      title={title}
      description={description}
      action={action}
      className={className}
    />
  );
}
