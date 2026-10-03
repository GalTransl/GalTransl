import { t as translate, useUiLanguage } from "../../i18n";
import type { ReactNode } from 'react';
import { resolveConfigOption, type ConfigOption } from '../../i18n/config';
import type { TranslationKey } from '../../i18n/core';
import { CustomSelect } from '../../components/CustomSelect';

export type FieldValueType = 'number' | 'text' | 'select' | 'textarea' | 'list';

export interface ConfigFieldDef {
  key: string;
  labelKey: TranslationKey;
  descriptionKey: TranslationKey;
  type: FieldValueType;
  options?: ConfigOption[];
  placeholder?: string;
  placeholderKey?: TranslationKey;
  rows?: number;
}

interface ConfigFieldRowProps {
  field: ConfigFieldDef;
  value: unknown;
  onChange: (path: string, value: string) => void;
  onListChange?: (path: string, value: string[]) => void;
  pathPrefix: string;
  /** Optional visual emphasis level */
  tier?: 'primary' | 'advanced';
}

export function ConfigFieldRow({ field, value, onChange, onListChange, pathPrefix, tier }: ConfigFieldRowProps) {
  const uiLanguage = useUiLanguage();
  const fieldId = `${pathPrefix}-${field.key.replace(/\./g, '-')}`;
  const displayValue = value == null ? '' : String(value);
  const fullPath = `${pathPrefix}.${field.key}`;
  const placeholder = field.placeholderKey ? translate(field.placeholderKey) : field.placeholder;
  const description = translate(field.descriptionKey);

  const inputElement =
    field.type === 'select' ? (
      <CustomSelect
        id={fieldId}
        value={displayValue}
        onChange={(e) => onChange(fullPath, e.target.value)}
      >
        {field.options?.map((opt) => {
          const option = resolveConfigOption(opt);
          return <option key={option.value} value={option.value}>{option.label}</option>;
        })}
      </CustomSelect>
    ) : field.type === 'textarea' ? (
      <textarea
        id={fieldId}
        rows={field.rows ?? 4}
        value={displayValue}
        placeholder={placeholder}
        onChange={(e) => onChange(fullPath, e.target.value)}
      />
    ) : field.type === 'list' ? (
      <textarea
        id={fieldId}
        rows={field.rows ?? 4}
        value={Array.isArray(value) ? value.join('\n') : (value == null ? '' : String(value))}
        placeholder={placeholder || translate("common:actions.oneEntryPerLine")}
        onChange={(e) => {
          if (onListChange) {
            const lines = e.target.value.split('\n').filter((l: string) => l.trim());
            onListChange(fullPath, lines);
          } else {
            onChange(fullPath, e.target.value);
          }
        }}
      />
    ) : (
      <input
        id={fieldId}
        type={field.type}
        value={displayValue}
        placeholder={placeholder}
        onChange={(e) => onChange(fullPath, e.target.value)}
      />
    );

  return (
    <div
      className={[
        'config-field-row',
        tier === 'advanced' ? 'config-field-row--advanced' : '',
        tier === 'primary' ? 'config-field-row--primary' : '',
      ].filter(Boolean).join(' ')}
    >
      <label htmlFor={fieldId} className="config-field-row__label">{translate(field.labelKey)}</label>
      <div className="config-field-row__input">{inputElement}</div>
      {description ? (
        <span className="config-field-row__hint">{description}</span>
      ) : null}
    </div>
  );
}

interface ConfigFieldGroupProps {
  title: string;
  children: ReactNode;
  tier?: 'primary' | 'advanced';
  collapsible?: boolean;
  defaultCollapsed?: boolean;
}

export function ConfigFieldGroup({ title, children, tier }: ConfigFieldGroupProps) {
  useUiLanguage();
  return (
    <div className={['config-field-group', tier === 'advanced' ? 'config-field-group--advanced' : ''].filter(Boolean).join(' ')}>
      <div className="config-field-group__title">{title}</div>
      <div className="config-field-group__fields">
        {children}
      </div>
    </div>
  );
}
