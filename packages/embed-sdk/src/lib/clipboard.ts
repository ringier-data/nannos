/**
 * Clipboard API first; a host served over plain http (a dev box, say) has
 * none, so fall back to a throwaway textarea + `execCommand` rather than
 * nothing. Resolves to whether the text made it.
 */
export async function writeClipboard(text: string): Promise<boolean> {
  if (typeof window === 'undefined' || !text) return false;
  if (navigator?.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // fall through to the legacy path
    }
  }
  const area = document.createElement('textarea');
  area.value = text;
  area.setAttribute('readonly', '');
  area.style.cssText = 'position:fixed;top:-1000px;opacity:0';
  document.body.appendChild(area);
  area.select();
  let ok = false;
  try {
    ok = document.execCommand('copy');
  } catch {
    ok = false;
  }
  area.remove();
  return ok;
}
