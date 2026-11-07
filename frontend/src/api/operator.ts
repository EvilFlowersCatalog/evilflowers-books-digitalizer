/**
 * Operator identity: a free-text name kept per browser and sent as `X-Operator`.
 *
 * There is no authentication (VPN only); the name is recorded on notes, reviews and
 * region edits. Storage access is wrapped because private windows can throw.
 */

const KEY = "studio.operator";
type Listener = (name: string) => void;
const listeners = new Set<Listener>();

export function getOperator(): string {
  try {
    return globalThis.localStorage?.getItem(KEY) ?? "";
  } catch {
    return "";
  }
}

export function setOperator(name: string): void {
  const clean = name.trim();
  try {
    if (clean) globalThis.localStorage?.setItem(KEY, clean);
    else globalThis.localStorage?.removeItem(KEY);
  } catch {
    /* storage unavailable: the name lives for this tab only via listeners */
  }
  listeners.forEach((fn) => fn(clean));
}

export function onOperatorChange(fn: Listener): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/**
 * Header value for a name. `fetch` rejects header values outside ISO-8859-1, and
 * Slovak names (č, š, ž, ľ …) are not Latin-1, so the value is percent-encoded;
 * the API decodes it with `urllib.parse.unquote` (a no-op for plain ASCII names).
 */
export function operatorHeader(name: string): string {
  return encodeURIComponent(name.trim());
}
