import { useSyncExternalStore } from "react";

/**
 * How the dashboard picks its colours: follow the system, or stay light or
 * dark. A per-viewer convenience kept in the browser; nothing else depends on
 * it, so a browser that refuses storage simply follows the system.
 */
export type ThemePreference = "system" | "light" | "dark";
export const themes: ThemePreference[] = ["system", "light", "dark"];
const KEY = "theme";

export function parseTheme(raw: unknown): ThemePreference {
  return raw === "light" || raw === "dark" ? raw : "system";
}

function read(): ThemePreference {
  try {
    return parseTheme(localStorage.getItem(KEY));
  } catch {
    return "system";
  }
}

/** The system setting needs no script: the stylesheet follows it unless an
 * explicit choice is set on the document. */
export function applyTheme(preference: ThemePreference) {
  const root = document.documentElement;
  if (preference === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", preference);
}

const listeners = new Set<() => void>();
let current: ThemePreference | undefined;

export function initTheme() {
  current = read();
  applyTheme(current);
  window.addEventListener("storage", (event) => {
    if (event.key !== KEY && event.key !== null) return;
    current = read();
    applyTheme(current);
    for (const notify of listeners) notify();
  });
}

export function setTheme(preference: ThemePreference) {
  current = preference;
  applyTheme(preference);
  try {
    if (preference === "system") localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, preference);
  } catch {
    /* The choice still applies until the page is closed. */
  }
  for (const notify of listeners) notify();
}

export function useTheme(): [
  ThemePreference,
  (value: ThemePreference) => void,
] {
  const preference = useSyncExternalStore(
    (notify) => {
      listeners.add(notify);
      return () => listeners.delete(notify);
    },
    () => (current ??= read()),
  );
  return [preference, setTheme];
}
