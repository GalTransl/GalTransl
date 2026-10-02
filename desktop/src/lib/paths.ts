const WINDOWS_DRIVE_RE = /^[A-Za-z]:[\\/]/;
const WINDOWS_UNC_RE = /^\\\\/;

export function detectPathSeparator(value: string): '/' | '\\' {
  if (WINDOWS_UNC_RE.test(value)) return '\\';
  if (WINDOWS_DRIVE_RE.test(value)) return value.includes('\\') ? '\\' : '/';
  return '/';
}

export function joinPath(base: string, ...parts: string[]): string {
  if (!base) return '';
  const separator = detectPathSeparator(base);
  let result = base;
  for (const part of parts) {
    const cleanPart = String(part || '').replace(/^[\\/]+/, '').replace(/[\\/]+$/, '');
    if (!cleanPart) continue;
    result = /[\\/]$/.test(result) ? `${result}${cleanPart}` : `${result}${separator}${cleanPart}`;
  }
  return result;
}

export function dirnamePath(value: string): string {
  if (!value) return '';
  const normalized = value.replace(/[\\/]+$/, '');
  if (!normalized) return value[0] || '';
  const slashIndex = Math.max(normalized.lastIndexOf('/'), normalized.lastIndexOf('\\'));
  if (slashIndex < 0) return '';
  if (slashIndex === 0) return normalized.slice(0, 1);
  if (/^[A-Za-z]:$/.test(normalized.slice(0, slashIndex))) {
    return `${normalized.slice(0, slashIndex)}${detectPathSeparator(normalized)}`;
  }
  return normalized.slice(0, slashIndex);
}

export function basenamePath(value: string): string {
  const normalized = value.replace(/[\\/]+$/, '');
  if (!normalized) return value;
  const slashIndex = Math.max(normalized.lastIndexOf('/'), normalized.lastIndexOf('\\'));
  return slashIndex >= 0 ? normalized.slice(slashIndex + 1) : normalized;
}

export function normalizeFileUriPath(raw: string): string {
  const line = raw.trim();
  if (!line.startsWith('file://')) {
    try {
      return decodeURIComponent(line);
    } catch {
      return line;
    }
  }

  try {
    const url = new URL(line);
    let decoded = decodeURIComponent(url.pathname || '');
    if (url.hostname && url.hostname !== 'localhost') {
      return `//${url.hostname}${decoded}`;
    }
    if (/^\/[A-Za-z]:/.test(decoded)) {
      decoded = decoded.slice(1);
    }
    return decoded;
  } catch {
    return line;
  }
}

export function isAbsolutePath(value: string): boolean {
  return value.startsWith('/') || WINDOWS_DRIVE_RE.test(value) || WINDOWS_UNC_RE.test(value);
}
