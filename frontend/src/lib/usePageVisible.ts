import { useSyncExternalStore } from "react";

function subscribe(listener: () => void): () => void {
  document.addEventListener("visibilitychange", listener);
  return () => document.removeEventListener("visibilitychange", listener);
}

function isVisible(): boolean {
  return document.visibilityState !== "hidden";
}

/** Whether the page is visible (false while the tab is hidden or minimised). */
export function usePageVisible(): boolean {
  return useSyncExternalStore(subscribe, isVisible, () => true);
}
