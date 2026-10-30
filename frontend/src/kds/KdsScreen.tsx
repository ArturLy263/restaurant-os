import { useEffect, useMemo, useState, type ReactNode } from "react";

import { isApiError } from "../api/client";
import { getStations, updatePreparation } from "../api/endpoints";
import type { CurrentUser, PreparationQueueItem, Station } from "../api/types";
import { SESSION_EXPIRED_NOTICE, useAuth } from "../auth/AuthContext";
import { ErrorState, LoadingState } from "../ui/States";
import { QueueBoard } from "./QueueBoard";
import { StationPicker } from "./StationPicker";
import { nextAction } from "./queue";
import { useStationLiveQueue, type Load, type LiveStatus } from "./stationLive";

const STATION_KEY = "restaurantos.stationId";
// How often elapsed times are recomputed (not a refetch).
const CLOCK_INTERVAL_MS = 30_000;

interface Notice {
  kind: "info" | "error";
  text: string;
}

interface KdsScreenProps {
  token: string;
  user: CurrentUser;
  // Switches to another screen (e.g. the POS), shown in the top bar.
  nav?: ReactNode;
}

export function KdsScreen({ token, user, nav }: KdsScreenProps) {
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
          {nav}
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
  const { handleAuthError, logout } = useAuth();
  const [pendingIds, setPendingIds] = useState<ReadonlySet<string>>(new Set());
  const [notice, setNotice] = useState<Notice | null>(null);
  const [now, setNow] = useState(() => new Date());
  const permissionSet = useMemo(() => new Set(permissions), [permissions]);

  const { snapshot, reload } = useStationLiveQueue({
    stationId: station.id,
    token,
    onLoadError: (error) => {
      if (handleAuthError(error)) return;
      // The last queue stays on screen; say it is stale.
      setNotice({ kind: "error", text: `Queue could not be refreshed: ${messageOf(error)}` });
    },
    onAuthExpired: () => logout(SESSION_EXPIRED_NOTICE),
  });
  const { queue, refreshing, live } = snapshot;

  // Elapsed times restart from a fresh clock whenever the queue changes.
  useEffect(() => {
    setNow(new Date());
  }, [queue]);

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
    await reload();
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
        onRetry={forbidden ? undefined : () => void reload()}
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
        <LiveIndicator status={live.status} reason={live.reason} />
        <button
          type="button"
          className="button button--secondary"
          onClick={() => void reload()}
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

const LIVE_LABELS: Record<LiveStatus, string> = {
  connecting: "Connecting…",
  connected: "Connected",
  live: "Live",
  reconnecting: "Reconnecting…",
  offline: "Live updates off",
};

const LIVE_HINTS: Record<LiveStatus, string> = {
  connecting: "Connecting to live updates; use Refresh meanwhile.",
  connected:
    "Connected to the station's live updates, but none has arrived on this connection yet, " +
    "so delivery is not confirmed. If the queue does not change as orders do, use Refresh.",
  live: "Receiving live updates; the queue changes as orders do.",
  reconnecting: "Live updates were interrupted; reconnecting. Use Refresh meanwhile.",
  offline: "Live updates are off; use Refresh.",
};

function LiveIndicator({ status, reason }: { status: LiveStatus; reason: string | null }) {
  const hint = reason ? `${reason} ${LIVE_HINTS[status]}` : LIVE_HINTS[status];
  return (
    <span className={`live live--${status}`} role="status" title={hint}>
      <span className="live__dot" aria-hidden="true" />
      {LIVE_LABELS[status]}
      <span className="visually-hidden">. {hint}</span>
    </span>
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
