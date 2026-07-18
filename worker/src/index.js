const STATE_KEY = "radar:state:v1";
const QUEUE_KEY = "radar:queue:v1";
const MAX_CLOCK_SKEW_SECONDS = 120;
const PUBLIC_STATE_FIELDS = new Set([
  "status", "running", "phase", "message", "mode", "job_id",
  "requested_at", "started_at", "completed_at", "updated_at",
  "universe_unique_offers", "qualified_universe_offers",
  "ranked_offer_count", "published_offer_count", "verified_live_count",
  "connected_country_count", "connected_source_count", "generation_id",
  "error_code"
]);

function nowIso() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
}

function corsHeaders(env, request) {
  const origin = request.headers.get("Origin") || "";
  const allowed = origin === env.ALLOWED_ORIGIN ? origin : env.ALLOWED_ORIGIN;
  return {
    "Access-Control-Allow-Origin": allowed,
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-Radar-Time, X-Radar-Nonce, X-Radar-Signature",
    "Access-Control-Max-Age": "600",
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff"
  };
}

function responseJson(payload, status, env, request, extra = {}) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      ...corsHeaders(env, request),
      ...extra
    }
  });
}

function safePublicState(raw) {
  const state = {};
  if (!raw || typeof raw !== "object") return {status: "idle", running: false};
  for (const [key, value] of Object.entries(raw)) {
    if (PUBLIC_STATE_FIELDS.has(key)) state[key] = value;
  }
  if (typeof state.running !== "boolean") {
    state.running = ["pending", "queued", "running"].includes(state.status);
  }
  return state;
}

async function readJson(request, maxBytes = 8192) {
  const length = Number(request.headers.get("Content-Length") || "0");
  if (!Number.isFinite(length) || length < 0 || length > maxBytes) {
    throw new Error("invalid_body_length");
  }
  const text = await request.text();
  if (text.length > maxBytes) throw new Error("body_too_large");
  const value = text ? JSON.parse(text) : {};
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("body_must_be_object");
  }
  return value;
}

function hexToBytes(value) {
  if (!/^[a-f0-9]+$/i.test(value) || value.length % 2) return null;
  const bytes = new Uint8Array(value.length / 2);
  for (let i = 0; i < bytes.length; i++) bytes[i] = parseInt(value.slice(i * 2, i * 2 + 2), 16);
  return bytes;
}

function timingSafeEqual(a, b) {
  if (!a || !b || a.length !== b.length) return false;
  let difference = 0;
  for (let i = 0; i < a.length; i++) difference |= a[i] ^ b[i];
  return difference === 0;
}

function base64ToBytes(value) {
  const raw = atob(value);
  return Uint8Array.from(raw, character => character.charCodeAt(0));
}

async function verifyPublicSignature(request, env, mode) {
  const origin = request.headers.get("Origin") || "";
  if (origin !== env.ALLOWED_ORIGIN) return {ok: false, error: "origin_not_allowed"};
  const stampText = request.headers.get("X-Radar-Time") || "";
  const nonce = (request.headers.get("X-Radar-Nonce") || "").toLowerCase();
  const signature = (request.headers.get("X-Radar-Signature") || "").toLowerCase();
  const stamp = Number(stampText);
  const now = Math.floor(Date.now() / 1000);
  if (!Number.isInteger(stamp) || Math.abs(now - stamp) > MAX_CLOCK_SKEW_SECONDS) {
    return {ok: false, error: "expired_signature"};
  }
  if (!/^[a-f0-9]{32}$/.test(nonce) || !/^[a-f0-9]{64}$/.test(signature)) {
    return {ok: false, error: "invalid_signature_shape"};
  }
  const replayKey = `radar:nonce:${nonce}`;
  if (await env.RADAR_STATE.get(replayKey)) return {ok: false, error: "replayed_signature"};
  let keyBytes;
  try {
    keyBytes = base64ToBytes(env.RADAR_CONTROL_KEY_B64 || "");
  } catch (_) {
    return {ok: false, error: "control_key_unavailable"};
  }
  if (keyBytes.length !== 32) return {ok: false, error: "control_key_unavailable"};
  const key = await crypto.subtle.importKey("raw", keyBytes, {name: "HMAC", hash: "SHA-256"}, false, ["sign"]);
  const canonical = `${stampText}\n${nonce}\nPOST\n/api/refresh\n${mode}`;
  const expected = new Uint8Array(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(canonical)));
  if (!timingSafeEqual(expected, hexToBytes(signature))) return {ok: false, error: "invalid_signature"};
  await env.RADAR_STATE.put(replayKey, "1", {expirationTtl: 300});
  return {ok: true};
}

function internalAuthorized(request, env) {
  const header = request.headers.get("Authorization") || "";
  const provided = header.startsWith("Bearer ") ? header.slice(7) : "";
  const expected = env.RADAR_INTERNAL_TOKEN || "";
  if (!provided || !expected || provided.length !== expected.length) return false;
  let difference = 0;
  for (let i = 0; i < provided.length; i++) difference |= provided.charCodeAt(i) ^ expected.charCodeAt(i);
  return difference === 0;
}

async function getState(env) {
  return await env.RADAR_STATE.get(STATE_KEY, "json") || {status: "idle", running: false};
}

async function handleRefresh(request, env) {
  let body;
  try {
    body = await readJson(request, 1024);
  } catch (error) {
    return responseJson({ok: false, error: String(error.message || error)}, 400, env, request);
  }
  const mode = body.mode === "full" ? "full" : body.mode === "smart" ? "smart" : "";
  if (!mode) return responseJson({ok: false, error: "unsupported_mode"}, 400, env, request);
  const verification = await verifyPublicSignature(request, env, mode);
  if (!verification.ok) return responseJson({ok: false, error: verification.error}, 401, env, request);

  const state = await getState(env);
  if (["pending", "queued", "running"].includes(state.status) || state.running) {
    return responseJson({ok: true, coalesced: true, ...safePublicState(state)}, 202, env, request);
  }
  const completed = Date.parse(state.completed_at || "");
  const cooldown = Number(mode === "full" ? env.PUBLIC_FULL_COOLDOWN_SECONDS : env.PUBLIC_SMART_COOLDOWN_SECONDS) || 600;
  if (Number.isFinite(completed)) {
    const remaining = Math.ceil(cooldown - (Date.now() - completed) / 1000);
    if (remaining > 0) {
      return responseJson({ok: false, error: "cooldown", retry_after_seconds: remaining, ...safePublicState(state)}, 429, env, request, {"Retry-After": String(remaining)});
    }
  }

  const requestedAt = nowIso();
  const job = {job_id: crypto.randomUUID(), mode, requested_at: requestedAt};
  const nextState = {
    status: "pending", running: true, phase: "queued", mode,
    job_id: job.job_id, requested_at: requestedAt, updated_at: requestedAt,
    message: mode === "full" ? "تم وضع التحديث الشامل في طابور الـVPS" : "تم وضع التحديث الذكي في طابور الـVPS"
  };
  await env.RADAR_STATE.put(QUEUE_KEY, JSON.stringify(job), {expirationTtl: 21600});
  await env.RADAR_STATE.put(STATE_KEY, JSON.stringify(nextState));
  return responseJson({ok: true, ...safePublicState(nextState)}, 202, env, request);
}

async function handleClaim(request, env) {
  if (!internalAuthorized(request, env)) return responseJson({ok: false, error: "unauthorized"}, 401, env, request);
  const job = await env.RADAR_STATE.get(QUEUE_KEY, "json");
  if (!job) return new Response(null, {status: 204, headers: corsHeaders(env, request)});
  const started = nowIso();
  const state = {
    status: "running", running: true, phase: "starting", mode: job.mode,
    job_id: job.job_id, requested_at: job.requested_at, started_at: started,
    updated_at: started, message: "استلم الـVPS المهمة وبدأ التنفيذ"
  };
  await env.RADAR_STATE.put(STATE_KEY, JSON.stringify(state));
  await env.RADAR_STATE.delete(QUEUE_KEY);
  return responseJson({ok: true, ...job}, 200, env, request);
}

async function handleInternalUpdate(request, env) {
  if (!internalAuthorized(request, env)) return responseJson({ok: false, error: "unauthorized"}, 401, env, request);
  let body;
  try {
    body = await readJson(request, 8192);
  } catch (error) {
    return responseJson({ok: false, error: String(error.message || error)}, 400, env, request);
  }
  const previous = await getState(env);
  if (body.job_id && previous.job_id && body.job_id !== previous.job_id) {
    return responseJson({ok: false, error: "job_mismatch", current_job_id: previous.job_id}, 409, env, request);
  }
  const next = {...previous};
  for (const key of PUBLIC_STATE_FIELDS) {
    if (Object.prototype.hasOwnProperty.call(body, key)) next[key] = body[key];
  }
  next.updated_at = nowIso();
  if (["ok", "failed", "skipped"].includes(next.status)) next.running = false;
  await env.RADAR_STATE.put(STATE_KEY, JSON.stringify(next));
  return responseJson({ok: true, ...safePublicState(next)}, 200, env, request);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") return new Response(null, {status: 204, headers: corsHeaders(env, request)});
    if (request.method === "GET" && (url.pathname === "/api/status" || url.pathname === "/health")) {
      const state = safePublicState(await getState(env));
      return responseJson(url.pathname === "/health" ? {ok: true, ...state} : state, 200, env, request);
    }
    if (request.method === "POST" && url.pathname === "/api/refresh") return handleRefresh(request, env);
    if (request.method === "POST" && url.pathname === "/internal/claim") return handleClaim(request, env);
    if (request.method === "POST" && url.pathname === "/internal/update") return handleInternalUpdate(request, env);
    return responseJson({ok: false, error: "not_found"}, 404, env, request);
  }
};
