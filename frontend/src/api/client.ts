import type { ApiErrorBody } from "./types";

// Codes the client produces itself, for failures without a backend body.
export const NETWORK_ERROR = "NETWORK_ERROR";
export const INVALID_RESPONSE = "INVALID_RESPONSE";

/** A failed API request, normalized from the backend error envelope. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | null;
  readonly detail: unknown;

  constructor(options: {
    status: number;
    code: string;
    message: string;
    requestId?: string | null;
    detail?: unknown;
  }) {
    super(options.message);
    this.name = "ApiError";
    this.status = options.status;
    this.code = options.code;
    this.requestId = options.requestId ?? null;
    this.detail = options.detail;
  }

  get isUnauthorized(): boolean {
    return this.status === 401;
  }

  get isForbidden(): boolean {
    return this.status === 403;
  }

  get isConflict(): boolean {
    return this.status === 409;
  }

  /** The request may or may not have reached the server. */
  get isNetworkError(): boolean {
    return this.code === NETWORK_ERROR;
  }
}

/** A new Idempotency-Key for one user action. */
export function newIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
}

export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError;
}

function isErrorBody(body: unknown): body is ApiErrorBody {
  if (typeof body !== "object" || body === null) return false;
  const error = (body as { error?: unknown }).error;
  return (
    typeof error === "object" &&
    error !== null &&
    typeof (error as { code?: unknown }).code === "string" &&
    typeof (error as { message?: unknown }).message === "string"
  );
}

// Used when a response has no envelope (e.g. a proxy error page).
const FALLBACK_MESSAGES: Record<number, string> = {
  401: "Your session has expired. Please sign in again.",
  403: "You do not have permission to do this.",
  404: "Not found.",
  409: "This was changed by someone else.",
  502: "The RestaurantOS server is not reachable.",
  503: "The RestaurantOS server is unavailable.",
  504: "The RestaurantOS server did not respond in time.",
};

async function readBody(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return undefined;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

async function toApiError(response: Response): Promise<ApiError> {
  const body = await readBody(response);
  const headerRequestId = response.headers.get("X-Request-ID");

  if (isErrorBody(body)) {
    return new ApiError({
      status: response.status,
      code: body.error.code,
      message: body.error.message,
      requestId: body.error.request_id ?? headerRequestId,
      detail: body.detail,
    });
  }

  return new ApiError({
    status: response.status,
    code: `HTTP_${response.status}`,
    message:
      FALLBACK_MESSAGES[response.status] ??
      `Request failed with status ${response.status}.`,
    requestId: headerRequestId,
    detail: body,
  });
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PATCH";
  body?: unknown;
  token?: string | null;
  signal?: AbortSignal;
  // Sent as the Idempotency-Key header: a retry with the same key replays
  // the first result instead of repeating the change.
  idempotencyKey?: string;
}

/**
 * Sends a request to the API (same origin; the dev server proxies /api)
 * and returns the parsed JSON body, or throws ApiError.
 */
export async function apiRequest<T>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };

  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  if (options.token) {
    headers.Authorization = `Bearer ${options.token}`;
  }
  if (options.idempotencyKey) {
    headers["Idempotency-Key"] = options.idempotencyKey;
  }

  let response: Response;
  try {
    response = await fetch(path, {
      method: options.method ?? "GET",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw error;
    }
    throw new ApiError({
      status: 0,
      code: NETWORK_ERROR,
      message: "Cannot reach the RestaurantOS server. Check the connection.",
    });
  }

  if (!response.ok) {
    throw await toApiError(response);
  }

  const body = await readBody(response);
  if (typeof body === "string") {
    throw new ApiError({
      status: response.status,
      code: INVALID_RESPONSE,
      message: "The server returned an unexpected response.",
      requestId: response.headers.get("X-Request-ID"),
    });
  }
  return body as T;
}
