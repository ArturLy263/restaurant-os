import { apiRequest } from "./client";
import type {
  CurrentUser,
  PreparationQueueItem,
  PreparationResponse,
  Station,
  TokenResponse,
} from "./types";

const BASE = "/api/v1";

export function login(email: string, password: string): Promise<TokenResponse> {
  return apiRequest<TokenResponse>(`${BASE}/auth/login`, {
    method: "POST",
    body: { email, password },
  });
}

export function getMe(token: string, signal?: AbortSignal): Promise<CurrentUser> {
  return apiRequest<CurrentUser>(`${BASE}/me`, { token, signal });
}

export function getStations(token: string, signal?: AbortSignal): Promise<Station[]> {
  return apiRequest<Station[]>(`${BASE}/stations`, { token, signal });
}

export function getStationQueue(
  token: string,
  stationId: string,
  signal?: AbortSignal,
): Promise<PreparationQueueItem[]> {
  return apiRequest<PreparationQueueItem[]>(
    `${BASE}/stations/${encodeURIComponent(stationId)}/preparations`,
    { token, signal },
  );
}

export type PreparationAction = "start" | "ready" | "serve";

export function updatePreparation(
  token: string,
  preparationId: string,
  action: PreparationAction,
): Promise<PreparationResponse> {
  return apiRequest<PreparationResponse>(
    `${BASE}/preparations/${encodeURIComponent(preparationId)}/${action}`,
    { method: "POST", token },
  );
}
