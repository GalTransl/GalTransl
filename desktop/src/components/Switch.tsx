/* 布尔开关：替代「是 / 否」「开启 / 关闭」下拉框。
 *
 * 根节点刻意用 <span> 而不是 <label>：调用方通常已经把它包在
 * <label className="field"> 里（点整行文字也能切换），再嵌一层 label
 * 就是非法的嵌套 label。input 仍是 label 的后代，隐式关联照常生效。
 * 样式见 styles/components/switch.css。 */
export function Switch({
  id,
  checked,
  onChange,
  disabled = false,
  ariaLabel,
}: {
  /** 配合外部 <label htmlFor> 使用；开关自己不被 label 包裹时也能点文字 */
  id?: string;
  checked: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
  /** 不在 <label> 里时才需要显式给无障碍名称 */
  ariaLabel?: string;
}) {
  return (
    <span className={`toggle-switch${disabled ? ' toggle-switch--disabled' : ''}`}>
      <input
        id={id}
        type="checkbox"
        role="switch"
        checked={checked}
        disabled={disabled}
        aria-label={ariaLabel}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span className="toggle-switch__slider" aria-hidden="true" />
    </span>
  );
}
