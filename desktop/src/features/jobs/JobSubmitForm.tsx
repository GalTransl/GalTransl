import { UiTrans, message as uiMessage, t as translate, useMessageState, useUiLanguage } from "../../i18n";
import { useEffect, useState } from 'react';
import { Button } from '../../components/Button';
import { CustomSelect } from '../../components/CustomSelect';
import { Panel } from '../../components/Panel';
import { InlineFeedback } from '../../components/page-state/InlineFeedback';
import type { SubmitJobPayload, TranslatorOption } from '../../lib/api';
import { useConnection } from '../connection/ConnectionContext';

type JobSubmitFormProps = {
  disabled: boolean;
  isSubmitting: boolean;
  onSubmit: (payload: SubmitJobPayload) => Promise<void>;
  submitError: string | null;
  translators: TranslatorOption[];
};

export function JobSubmitForm({ disabled, isSubmitting, onSubmit, submitError, translators }: JobSubmitFormProps) {
  useUiLanguage();
  const { backendUrl } = useConnection();
  const [projectDir, setProjectDir] = useState('');
  const [configFileName, setConfigFileName] = useState('config.yaml');
  const [translator, setTranslator] = useState('');
  const [localError, setLocalError] = useMessageState<string | null>(null);
  const activeError = localError ?? submitError;

  useEffect(() => {
    if (!translator && translators.length > 0) {
      setTranslator(translators[0].name);
    }
  }, [translator, translators]);

  return (
    <Panel
      title={translate("common:jobSubmitForm.jobSubmitForm_title_submitJob")}
      description={translate("common:jobSubmitForm.jobSubmitForm_description_projectDirectoryConfigFileTranslationJobSend")}
    >
      <form
        className="form-stack"
        onSubmit={async (event) => {
          event.preventDefault();

          const normalizedProjectDir = projectDir.trim();
          const normalizedConfig = configFileName.trim() || 'config.yaml';

          if (!normalizedProjectDir) {
            setLocalError(uiMessage("common:jobSubmitForm.formStack_setLocalError_enterProjectDirectory"));
            return;
          }

          if (!translator) {
            setLocalError(uiMessage("common:jobSubmitForm.formStack_setLocalError_chooseTranslation"));
            return;
          }

          setLocalError(null);
          await onSubmit({
            config_file_name: normalizedConfig,
            project_dir: normalizedProjectDir,
            translator,
          });
        }}
      >
        <label className="field">
          <span>{translate("common:jobSubmitForm.field_message_projectDirectory")}</span>
          <input
            autoComplete="off"
            disabled={disabled || isSubmitting}
            onChange={(event) => setProjectDir(event.target.value)}
            placeholder={translate("common:jobSubmitForm.field_placeholder_homeUserGalTranslSampleProject")}
            value={projectDir}
          />
        </label>

        <label className="field">
          <span>{translate("common:jobSubmitForm.field_message_configFile")}</span>
          <input
            autoComplete="off"
            disabled={disabled || isSubmitting}
            onChange={(event) => setConfigFileName(event.target.value)}
            value={configFileName}
          />
        </label>

        <label className="field">
          <span>{translate("common:jobSubmitForm.field_message_translation")}</span>
          <CustomSelect
            disabled={disabled || isSubmitting || translators.length === 0}
            onChange={(event) => setTranslator(event.target.value)}
            value={translator}
          >
            {translators.length === 0 ? <option value="">{translate("common:jobSubmitForm.field_message_empty")}</option> : null}
            {translators.map((item) => (
              <option key={item.name} value={item.name}>
                {item.name} · {item.description}
              </option>
            ))}
          </CustomSelect>
        </label>

        {activeError ? (
          <InlineFeedback tone="error" title={translate("common:jobSubmitForm.formStack_title_jobFailed")} description={activeError} />
        ) : (
          <InlineFeedback tone="info" title={translate("common:jobSubmitForm.formStack_title_connectionHint")}>
            {backendUrl ? <><UiTrans k="common:jobSubmitForm.formStack_message_currentBackendAddress00" values={{ backendUrl: backendUrl }} components={[<code />]} /></> : translate("common:jobSubmitForm.formStack_message_backendNotReconnect")}
          </InlineFeedback>
        )}

        <div className="form-actions">
          <Button disabled={disabled || isSubmitting} type="submit">
            {isSubmitting ? translate("common:jobSubmitForm.formActions_message_submit") : translate("common:jobSubmitForm.formActions_message_job")}
          </Button>
        </div>
      </form>
    </Panel>
  );
}
