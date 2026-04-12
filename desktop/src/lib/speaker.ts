/* ── Deterministic speaker color from name hash ── */

export function speakerHue(name: string): number {
  let h = 5381;
  for (let i = 0; i < name.length; i++) {
    h = ((h << 5) + h + name.charCodeAt(i)) | 0;
  }
  return ((h & 0x7fffffff) % 360);
}

export function speakerStyle(name: string): React.CSSProperties {
  const hue = speakerHue(name);
  return {
    background: `hsl(${hue}, 60%, 92%)`,
    color: `hsl(${hue}, 55%, 32%)`,
    borderColor: `hsl(${hue}, 50%, 78%)`,
  };
}
