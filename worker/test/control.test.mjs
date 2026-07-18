import test from "node:test";
import assert from "node:assert/strict";
import worker from "../src/index.js";
import {webcrypto} from "node:crypto";

globalThis.crypto ??= webcrypto;
globalThis.atob ??= value => Buffer.from(value, "base64").toString("binary");

class MemoryKV {
  constructor() { this.values = new Map(); }
  async get(key, type) {
    const value = this.values.get(key);
    if (value === undefined) return null;
    return type === "json" ? JSON.parse(value) : value;
  }
  async put(key, value) { this.values.set(key, String(value)); }
  async delete(key) { this.values.delete(key); }
}

async function deriveControlKey(pin) {
  const material = await crypto.subtle.importKey("raw", new TextEncoder().encode(pin), "PBKDF2", false, ["deriveBits"]);
  return new Uint8Array(await crypto.subtle.deriveBits({
    name: "PBKDF2", salt: new TextEncoder().encode("sonardeals-radar-control-v1"),
    iterations: 210000, hash: "SHA-256"
  }, material, 256));
}

async function signedRequest(env, mode, nonce = "00112233445566778899aabbccddeeff") {
  const stamp = String(Math.floor(Date.now() / 1000));
  const key = await crypto.subtle.importKey("raw", Buffer.from(env.RADAR_CONTROL_KEY_B64, "base64"), {name: "HMAC", hash: "SHA-256"}, false, ["sign"]);
  const canonical = `${stamp}\n${nonce}\nPOST\n/api/refresh\n${mode}`;
  const signature = Buffer.from(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(canonical))).toString("hex");
  return new Request("https://radar-control.sonardeals.com/api/refresh", {
    method: "POST",
    headers: {"Content-Type": "application/json", "Origin": env.ALLOWED_ORIGIN,
      "X-Radar-Time": stamp, "X-Radar-Nonce": nonce, "X-Radar-Signature": signature},
    body: JSON.stringify({mode})
  });
}

test("signed request queues, duplicate coalesces, VPS claims and completes", async () => {
  const raw = await deriveControlKey("12345678");
  const env = {
    RADAR_STATE: new MemoryKV(), ALLOWED_ORIGIN: "https://krt123456.github.io",
    RADAR_CONTROL_KEY_B64: Buffer.from(raw).toString("base64"),
    RADAR_INTERNAL_TOKEN: "internal-test-token",
    PUBLIC_SMART_COOLDOWN_SECONDS: "0", PUBLIC_FULL_COOLDOWN_SECONDS: "0"
  };
  let response = await worker.fetch(await signedRequest(env, "smart"), env);
  assert.equal(response.status, 202);
  const queued = await response.json();
  assert.equal(queued.status, "pending");

  response = await worker.fetch(await signedRequest(env, "smart", "11112222333344445555666677778888"), env);
  assert.equal(response.status, 202);
  assert.equal((await response.json()).coalesced, true);

  response = await worker.fetch(new Request("https://radar-control.sonardeals.com/internal/claim", {
    method: "POST", headers: {Authorization: "Bearer internal-test-token"}
  }), env);
  assert.equal(response.status, 200);
  const claim = await response.json();
  assert.equal(claim.job_id, queued.job_id);

  response = await worker.fetch(new Request("https://radar-control.sonardeals.com/internal/update", {
    method: "POST", headers: {Authorization: "Bearer internal-test-token", "Content-Type": "application/json"},
    body: JSON.stringify({job_id: queued.job_id, status: "ok", running: false, universe_unique_offers: 747239})
  }), env);
  assert.equal(response.status, 200);
  assert.equal((await response.json()).universe_unique_offers, 747239);
});

test("bad signature cannot start a refresh", async () => {
  const env = {RADAR_STATE: new MemoryKV(), ALLOWED_ORIGIN: "https://krt123456.github.io",
    RADAR_CONTROL_KEY_B64: Buffer.alloc(32).toString("base64"), RADAR_INTERNAL_TOKEN: "x"};
  const request = new Request("https://radar-control.sonardeals.com/api/refresh", {
    method: "POST", headers: {"Content-Type": "application/json", Origin: env.ALLOWED_ORIGIN,
      "X-Radar-Time": String(Math.floor(Date.now() / 1000)), "X-Radar-Nonce": "00112233445566778899aabbccddeeff",
      "X-Radar-Signature": "0".repeat(64)}, body: JSON.stringify({mode: "full"})
  });
  const response = await worker.fetch(request, env);
  assert.equal(response.status, 401);
});
