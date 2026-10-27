import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { isApiError, type ApiError } from "../api/client";
import { getStationQueue, getStations, updatePreparation } from "../api/endpoints";
import type { CurrentUser, PreparationQueueItem, Station } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { QueueBoard } from "./QueueBoard";
import { StationPicker } from "./StationPicker";
import { nextAction } from "./queue";

const STATION_KEY = "restaurantos.stationId";
// How often elapsed times are recomputed (not a refetch).
const CLOCK_INTERVAL_MS = 30_000;

type Load<T> =
  | { status: "loading" }
  | { status: "loaded"; data: T }
  | { status: "error"; error: ApiError | Error };

interface Notice {
  kind: "info" | "error";
  text: string;
}

export function KdsScreen({ token, user }: { token: string; user: CurrentUser }) {
  const { logout, handleAuthError } = useAuth();
  const [stations, setStations] = useState<Load<Station[]>>({ status: "loading" });
  const [stationsAttempt, setStationsAttempt] = useState(0);
  const [stationId, setStationId] = useState<string | null>(readStoredStation);

  useEffect(() => {
    const controller = new AbortController();
    setStations({ status: "loading" });
    getStations(token, controller.signal).then(
      (data) => setStations({ status: "loaded", data }),
      (error: Error) => {
        if (controller.signal.aborted || handleAuthError(error)) return;
        setStations({ status: "error", error });
      },
    );
    return () => controller.abort();
  }, [token, handleAuthError, stationsAttempt]);

  const stationList = stations.status === "loaded" ? stations.data : null;
  const station = stationList?.find((candidate) => candidate.id === stationId) ?? null;

  // A single visible station needs no choice; a remembered station that is
  // no longer visible is forgotten.
  useEffect(() => {
    if (!stationList) return;
    if (stationList.length === 1 && stationId !== stationList[0].id) {
      selectStation(stationList[0].id);
    } else if (stationId && !stationList.some((candidate) => candidate.id === stationId)) {
      selectStation(null);
    }
  }, [stationList, stationId]);

  function selectStation(id: string | null) {
    storeStation(id);
    setStationId(id);
  }

  return (
    <div className="app">
      <header className="topbar">
        <div className="topbar__brand">
          <span className="topbar__logo">RestaurantOS</span>
          {station && <span className="topbar__station">{station.name}</span>}
        </div>
        <div className="topbar__actions">
          {station && stationList && stationList.length > 1 && (
            <button type="button" className="button button--ghost" onClick={() => selectStation(null)}>
              Change station
            </button>
          )}
          <span className="topbar__user">
            {user.name}
            {user.role && <span className="tag">{user.role.name}</span>}
          </span>
          <button type="button" className="button button--ghost" onClick={() => logout()}>
            Sign out
          </button>
        </div>
      </header>

      {stations.status === "loading" && <LoadingState text="Loading stations…" />}
      {stations.status === "error" && (
        <ErrorState
          title={isApiError(stations.error) && stations.error.isForbidden ? "No access to station queues" : "Stations could not be loaded"}
          error={stations.error}
          onRetry={isApiError(stations.error) && stations.error.isForbidden ? undefined : () => setStationsAttempt((n) => n + 1)}
        />
      )}
      {stationList && !station && (
        <StationPicker stations={stationList} onSelect={(picked) => selectStation(picked.id)} />
      )}
      {station && (
        <StationQueue
          key={station.id}
          token={token}
          station={station}
          permissions={user.permissions}
          onLeave={() => selectStation(null)}
        />
      )}
    </div>
  );
}

interface StationQueueProps {
  token: string;
  station: Station;
  permissions: string[];
  onLeave(): void;
}

function StationQueue({ token, station, permissions, onLeave }: StationQueueProps) {
  const { handleAuthError } = useAuth();
  const [queue, setQueue] = useState<Load<PreparationQueueItem[]>>({ status: "loading" });
  const [refreshing, setRefreshing] = useState(false);
  const [pendingIds, setPendingIds] = useState<ReadonlySet<string>>(new Set());
  const [notice, setNotice] = useState<Notice | null>(null);
  const [now, setNow] = useState(() => new Date());
  const permissionSet = useMemo(() => new Set(permissions), [permissions]);
  const latestLoad = useRef(0);

  const loadQueue = useCallback(async () => {
    const attempt = ++latestLoad.current;
    setRefreshing(true);
    try {
      const data = await getStationQueue(token, station.id);
      if (attempt !== latestLoad.current) return;
      setQueue({ status: "loaded", data });
      setNow(new Date());
    } catch (error) {
      if (attempt !== latestLoad.current || handleAuthError(error)) return;
      // Keep showing the last queue if there is one; say it is stale.
      setQueue((current) =>
        current.status === "loaded" ? current : { status: "error", error: error as Error },
      );
      setNotice({ kind: "error", text: `Queue could not be refreshed: ${messageOf(error)}` });
    } finally {
      if (attempt === latestLoad.current) setRefreshing(false);
    }
  }, [token, station.id, handleAuthError]);

  useEffect(() => {
    void loadQueue();
    return () => {
      // Results of a request for an unmounted station are ignored.
      latestLoad.current++;
    };
  }, [loadQueue]);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), CLOCK_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, []);

  async function handleAction(item: PreparationQueueItem) {
    const { action, label } = nextAction(item.status);
    setPendingIds((current) => new Set(current).add(item.id));
    setNotice(null);
    try {
      await updatePreparation(token, item.id, action);
    } catch (error) {
      if (handleAuthError(error)) return;
      setNotice({ kind: "error", text: actionErrorMessage(error, label, item) });
    } finally {
      setPendingIds((current) => {
        const next = new Set(current);
        next.delete(item.id);
        return next;
      });
    }
    // Refetch after success and after a failure, which usually means the
    // item changed elsewhere (409) or left this station's queue (404).
    await loadQueue();
  }

  if (queue.status === "loading") {
    return <LoadingState text={`Loading ${station.name} queue…`} />;
  }

  if (queue.status === "error") {
    const forbidden = isApiError(queue.error) && queue.error.isForbidden;
    return (
      <ErrorState
        title={forbidden ? `No access to ${station.name}` : "Queue could not be loaded"}
        error={queue.error}
        onRetry={forbidden ? undefined : () => void loadQueue()}
        secondary={{ label: "Choose another station", onClick: onLeave }}
      />
    );
  }

  return (
    <main className="queue">
      <div className="queue__toolbar">
        <h1 className="queue__title">
          {station.name}
          {!station.is_active && <span className="tag tag--muted">Inactive</span>}
        </h1>
        <span className="queue__summary">
          {queue.data.length} open {queue.data.length === 1 ? "item" : "items"}
        </span>
        <button
          type="button"
          className="button button--secondary"
          onClick={() => void loadQueue()}
          disabled={refreshing}
        >
          {refreshing ? "Refreshing…" : "Refresh"}
        </button>
      </div>

      {notice && (
        <div className={`banner banner--${notice.kind}`} role={notice.kind === "error" ? "alert" : "status"}>
          <span>{notice.text}</span>
          <button type="button" className="banner__close" aria-label="Dismiss" onClick={() => setNotice(null)}>
            ×
          </button>
        </div>
      )}

      {queue.data.length === 0 ? (
        <section className="state">
          <h2 className="state__title">All caught up</h2>
          <p className="state__text">No open preparations for {station.name}.</p>
        </section>
      ) : (
        <QueueBoard
          items={queue.data}
          now={now}
          permissions={permissionSet}
          pendingIds={pendingIds}
          onAction={(item) => void handleAction(item)}
        />
      )}
    </main>
  );
}

function actionErrorMessage(error: unknown, label: string, item: PreparationQueueItem): string {
  const what = `${item.quantity}× ${item.product_name}`;
  if (!isApiError(error)) return `${label} failed for ${what}.`;
  if (error.isConflict) {
    return `${what} was already updated or cancelled elsewhere. The queue has been refreshed.`;
  }
  if (error.isForbidden) {
    return `You are not allowed to ${label.toLowerCase()} ${what}: ${error.message}`;
  }
  if (error.status === 404) {
    return `${what} is no longer in this queue.`;
  }
  return `${label} failed for ${what}: ${error.message}`;
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : "Unknown error";
}

function LoadingState({ text }: { text: string }) {
  return (
    <section className="state" aria-busy="true">
      <div className="spinner" aria-hidden="true" />
      <p className="state__text">{text}</p>
    </section>
  );
}

interface ErrorStateProps {
  title: string;
  error: Error;
  onRetry?: () => void;
  secondary?: { label: string; onClick(): void };
}

function ErrorState({ title, error, onRetry, secondary }: ErrorStateProps) {
  const requestId = isApiError(error) ? error.requestId : null;
  return (
    <section className="state state--error" role="alert">
      <h2 className="state__title">{title}</h2>
      <p className="state__text">{error.message}</p>
      {requestId && <p className="state__hint">Request ID: {requestId}</p>}
      <div className="state__actions">
        {onRetry && (
          <button type="button" className="button button--primary" onClick={onRetry}>
            Try again
          </button>
        )}
        {secondary && (
          <button type="button" className="button button--secondary" onClick={secondary.onClick}>
            {secondary.label}
          </button>
        )}
      </div>
    </section>
  );
}

function readStoredStation(): string | null {
  try {
    return window.sessionStorage.getItem(STATION_KEY);
  } catch {
    return null;
  }
}

function storeStation(id: string | null): void {
  try {
    if (id) window.sessionStorage.setItem(STATION_KEY, id);
    else window.sessionStorage.removeItem(STATION_KEY);
  } catch {
    // Not remembered across reloads.
  }
}
