// A scripted stand-in for the FastAPI backend: the HTTP endpoints the
// display uses and the station event WebSocket (a minimal RFC 6455 server:
// text, close and ping frames). The test decides when the server answers
// `subscribed`, sends events or closes, so nothing depends on timing.

import { createHash } from "node:crypto";
import { createServer } from "node:http";

const WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";

function base64url(value) {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

// JWT-shaped, unsigned: the client only reads `exp`.
export const TOKEN = `${base64url({ alg: "HS256", typ: "JWT" })}.${base64url({
  sub: "e2e-user",
  exp: Math.floor(Date.now() / 1000) + 3600,
})}.e2e-signature`;

export const KITCHEN = { id: "5b0a3c1e-0000-4000-8000-0000000000e1", name: "Kitchen", type: "KITCHEN", is_active: true };
export const BAR = { id: "5b0a3c1e-0000-4000-8000-0000000000e2", name: "Bar", type: "BAR", is_active: true };

const USER = {
  id: "9d6f0d8e-0000-4000-8000-0000000000e1",
  name: "E2E Manager",
  email: "e2e@example.com",
  restaurant_id: "1f2e3d4c-0000-4000-8000-0000000000e1",
  role: { id: "aa000000-0000-4000-8000-0000000000e1", name: "MANAGER" },
  permissions: ["preparations:serve", "preparations:update", "preparations:view"],
  stations: null,
};

// Naive UTC, as the backend serializes it.
function ago(minutes) {
  return new Date(Date.now() - minutes * 60_000).toISOString().replace("Z", "");
}

function item(n, fields) {
  const id = String(n).padStart(12, "0");
  return {
    id: `0e2e0000-0000-4000-8000-${id}`,
    order_item_id: `1e2e0000-0000-4000-8000-${id}`,
    station_id: KITCHEN.id,
    status: "PENDING",
    created_at: ago(3),
    started_at: null,
    ready_at: null,
    order_id: `2e2e0000-0000-4000-8000-${id}`,
    table_number: n,
    table_name: null,
    product_name: `Dish ${n}`,
    quantity: 1,
    modifiers: [],
    order_status: "SENT",
    bill_status: null,
    ...fields,
  };
}

export function initialKitchenQueue() {
  return [
    item(4, { product_name: "Pasta", table_name: "Window" }),
    item(7, {
      product_name: "Soup of the day",
      status: "IN_PROGRESS",
      created_at: ago(14),
      started_at: ago(10),
      quantity: 2,
      modifiers: [{ modifier_id: "3e2e0000-0000-4000-8000-000000000001", name: "No croutons" }],
    }),
    // A fully paid order, 848 h old: the widest ticket header.
    item(3, {
      product_name: "Burger",
      status: "READY",
      created_at: ago(848 * 60 + 34),
      started_at: ago(848 * 60),
      ready_at: ago(847 * 60),
      order_status: "CLOSED",
      bill_status: "PAID",
    }),
  ];
}

class Connection {
  constructor(socket, stationId, url) {
    this.socket = socket;
    this.stationId = stationId;
    this.url = url;
    this.received = [];
    this.closeCode = null; // set when the client closes
    this.ended = false;
    this.closeSent = false;
    this.buffer = Buffer.alloc(0);
    socket.on("data", (chunk) => this.#read(chunk));
    socket.on("close", () => (this.ended = true));
    socket.on("error", () => (this.ended = true));
  }

  get authMessage() {
    return this.received[0] ?? null;
  }

  send(message) {
    this.#frame(0x1, Buffer.from(JSON.stringify(message)));
  }

  close(code, reason = "") {
    if (this.closeSent) return;
    this.closeSent = true;
    const payload = Buffer.alloc(2 + Buffer.byteLength(reason));
    payload.writeUInt16BE(code, 0);
    payload.write(reason, 2);
    this.#frame(0x8, payload);
    setTimeout(() => this.socket.end(), 50);
  }

  /** A dropped connection: no close frame (the browser reports 1006). */
  drop() {
    this.socket.destroy();
  }

  #frame(opcode, payload) {
    if (this.ended) return;
    let header;
    if (payload.length < 126) {
      header = Buffer.from([0x80 | opcode, payload.length]);
    } else {
      header = Buffer.alloc(4);
      header[0] = 0x80 | opcode;
      header[1] = 126;
      header.writeUInt16BE(payload.length, 2);
    }
    this.socket.write(Buffer.concat([header, payload]));
  }

  #read(chunk) {
    this.buffer = Buffer.concat([this.buffer, chunk]);
    for (;;) {
      if (this.buffer.length < 2) return;
      const opcode = this.buffer[0] & 0x0f;
      const masked = (this.buffer[1] & 0x80) !== 0;
      let length = this.buffer[1] & 0x7f;
      let offset = 2;
      if (length === 126) {
        if (this.buffer.length < 4) return;
        length = this.buffer.readUInt16BE(2);
        offset = 4;
      } else if (length === 127) {
        if (this.buffer.length < 10) return;
        length = Number(this.buffer.readBigUInt64BE(2));
        offset = 10;
      }
      const mask = masked ? this.buffer.subarray(offset, offset + 4) : null;
      if (masked) offset += 4;
      if (this.buffer.length < offset + length) return;
      const payload = Buffer.from(this.buffer.subarray(offset, offset + length));
      if (mask) for (let i = 0; i < payload.length; i++) payload[i] ^= mask[i % 4];
      this.buffer = this.buffer.subarray(offset + length);

      if (opcode === 0x1) {
        this.received.push(JSON.parse(payload.toString("utf8")));
      } else if (opcode === 0x8) {
        this.closeCode = payload.length >= 2 ? payload.readUInt16BE(0) : 1005;
        // Echo only a close the client started; one close frame per side.
        if (!this.closeSent) {
          this.closeSent = true;
          this.#frame(0x8, payload.subarray(0, 2));
        }
        this.socket.end();
      } else if (opcode === 0x9) {
        this.#frame(0xa, payload);
      }
    }
  }
}

export async function startMockApi() {
  const state = {
    queues: { [KITCHEN.id]: initialKitchenQueue(), [BAR.id]: [] },
    queueRequests: { [KITCHEN.id]: 0, [BAR.id]: 0 },
    logins: [],
    actionRequests: [],
    connections: [],
  };

  const server = createServer((req, res) => {
    const reply = (status, body) => {
      res.writeHead(status, { "Content-Type": "application/json" });
      res.end(JSON.stringify(body));
    };
    const authorized = req.headers.authorization === `Bearer ${TOKEN}`;
    const url = new URL(req.url, "http://mock");
    let body = "";
    req.on("data", (chunk) => (body += chunk));
    req.on("end", () => {
      if (req.method === "POST" && url.pathname === "/api/v1/auth/login") {
        state.logins.push(JSON.parse(body));
        return reply(200, { access_token: TOKEN, token_type: "bearer" });
      }
      if (!authorized) {
        return reply(401, { detail: "x", error: { code: "NOT_AUTHENTICATED", message: "Not authenticated", request_id: "e2e" } });
      }
      if (req.method === "GET" && url.pathname === "/api/v1/me") return reply(200, USER);
      if (req.method === "GET" && url.pathname === "/api/v1/stations") return reply(200, [KITCHEN, BAR]);
      const queue = url.pathname.match(/^\/api\/v1\/stations\/([^/]+)\/preparations$/);
      if (req.method === "GET" && queue && state.queues[queue[1]]) {
        state.queueRequests[queue[1]] += 1;
        return reply(200, state.queues[queue[1]]);
      }
      if (req.method === "POST" && url.pathname.startsWith("/api/v1/preparations/")) {
        // The test never clicks an action; record it if the app ever does.
        state.actionRequests.push(url.pathname);
      }
      return reply(404, { detail: "x", error: { code: "NOT_FOUND", message: "Not found", request_id: "e2e" } });
    });
  });

  server.on("upgrade", (req, socket) => {
    const match = req.url.match(/^\/api\/v1\/stations\/([^/?]+)\/events$/);
    const key = req.headers["sec-websocket-key"];
    if (!match || !key || req.headers.upgrade?.toLowerCase() !== "websocket") {
      socket.destroy();
      return;
    }
    const accept = createHash("sha1").update(key + WS_GUID).digest("base64");
    socket.write(
      ["HTTP/1.1 101 Switching Protocols", "Upgrade: websocket", "Connection: Upgrade", `Sec-WebSocket-Accept: ${accept}`, "", ""].join("\r\n"),
    );
    state.connections.push(new Connection(socket, decodeURIComponent(match[1]), req.url));
  });

  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  return {
    state,
    port: server.address().port,
    close: () =>
      new Promise((resolve) => {
        for (const connection of state.connections) connection.socket.destroy();
        server.close(resolve);
      }),
  };
}
