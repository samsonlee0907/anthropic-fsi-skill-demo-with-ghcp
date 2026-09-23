// Returns a navigable http(s) URL for an untrusted source link, or null when it should not be linked.
export function safeSourceUrl(value: string | null | undefined): string | null {
  if (!value || value.length > 2048 || /[\u0000-\u0020\u007f]/.test(value)) return null;
  try {
    const url = new URL(value);
    if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) return null;
    if (!url.hostname.includes('.') || /^(localhost|127\.|0\.|10\.|192\.168\.|169\.254\.|172\.(1[6-9]|2\d|3[01])\.)/i.test(url.hostname)) return null;
    return url.href;
  } catch {
    return null;
  }
}
