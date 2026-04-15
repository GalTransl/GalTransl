import type { ReactNode } from 'react';
import { CustomSelect } from '../../components/CustomSelect';

export type FieldValueType = 'number' | 'text' | 'select' | 'textarea';

export interface ConfigFieldDef {
  key: string;
  label: string;
  description: string;
  type: FieldValueType;
  options?: string[];
  placeholder?: string;
  rows?: number;
}

interface ConfigFieldRowProps {
  field: ConfigFieldDef;
  value: unknown;
  onChange: (path: string, value: string) => void;
  pathPrefix: string;
  /** Optional visual emphasis level */
  tier?: 'primary' | 'advanced';
}

export function ConfigFieldRow({ field, value, onChange, pathPrefix, tier }: ConfigFieldRowProps) {
  const fieldId = `${pathPrefix}-${field.key.replace(/\./g, '-')}`;
  const displayValue = value == null ? '' : String(value);
  const fullPath = `${pathPrefix}.${field.key}`;

  const inputElement =
    field.type === 'select' ? (
      <CustomSelect
        id={fieldId}
        value={displayValue}
        onChange={(e) => onChange(fullPath, e.target.value)}
      >
        {field.options?.map((opt) => (
          <option key={opt} value={opt}>{opt}</option>
        ))}
      </CustomSelect>
    ) : field.type === 'textarea' ? (
      <textarea
        id={fieldId}
        rows={field.rows ?? 4}
        value={displayValue}
        placeholder={field.placeholder}
        onChange={(e) => onChange(fullPath, e.target.value)}
      />
    ) : (
      <input
        id={fieldId}
        type={field.type}
        value={displayValue}
        placeholder={field.placeholder}
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
      <label htmlFor={fieldId} className="config-field-row__label">{field.label}</label>
      <div className="config-field-row__input">{inputElement}</div>
      {field.description ? (
        <span className="config-field-row__hint">{field.description}</span>
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
  return (
    <div className={['config-field-group', tier === 'advanced' ? 'config-field-group--advanced' : ''].filter(Boolean).join(' ')}>
      <div className="config-field-group__title">{title}</div>
      <div className="config-field-group__fields">
        {children}
      </div>
    </div>
  );
}
