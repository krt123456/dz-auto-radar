#!/usr/bin/env node
"use strict";

/* Sealed FX display and fail-closed fallback gate. */
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { createHash, webcrypto } = require("crypto");

const INDEX = process.env.INDEX_HTML || path.join(__dirname, "..", "index.html");
const html = fs.readFileSync(INDEX, "utf8");
const scriptBody = (html.match(/<script>([\s\S]*?)<\/script>/) || [])[1];
if (!scriptBody) {
  console.error("FATAL: no <script> block found");
  process.exit(2);
}

function canonicalJson(value) {
  if (value === null || typeof value === "boolean" || typeof value === "string") return JSON.stringify(value);
  if (typeof value === "number" && Number.isFinite(value)) return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  }
  throw new Error("invalid fixture");
}

function digest(value) {
  return createHash("sha256").update(canonicalJson(value), "utf8").digest("hex");
}

function utcDay(timestamp) {
  return new Date(timestamp).toISOString().slice(0, 10);
}

function buildFx({ ageHours = 1 } = {}) {
  const captured = new Date(Date.now() - ageHours * 60 * 60 * 1000);
  const semantic = {
    base_currency: "EUR",
    display_currency: "DZD",
    effective_date: utcDay(captured),
    buy_rate: "153.31430",
    sell_rate: "153.34490",
    rate_scale: 100000,
    sell_rate_scaled: 15334490,
  };
  const core = {
    schema_version: 1,
    captured_at_utc: captured.toISOString().replace(/\.\d{3}Z$/, "Z"),
    source: {
      name: "ALCES External Portal",
      url: "https://alces.douane.gov.dz/api/public/com/main/selectFxrtList",
      rate_kind: "external_trade_sell",
    },
    semantic,
    semantic_sha256: digest(semantic),
    read_count: 2,
    evidence: [1, 2].map(number => ({
      replica: "41.111.157.9",
      evidence_file: `evidence/run/read-0${number}.json`,
      bytes: 1900 + number,
      raw_sha256: String(number).repeat(64),
    })),
  };
  return { ...core, seal: { algorithm: "sha256", value: digest(core) } };
}

function makeStorage() {
  const values = new Map();
  return {
    getItem: key => (values.has(key) ? values.get(key) : null),
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key),
  };
}

function stubElement(id) {
  return {
    value: "",
    checked: id === "flive",
    textContent: "",
    innerHTML: "",
    disabled: false,
    style: {},
    onclick: null,
    dataset: {},
    classList: {
      add() {},
      remove() {},
      toggle() {},
      contains() { return false; },
    },
    addEventListener() {},
  };
}

function makeSandbox(fxResponse) {
  const elements = new Map();
  const storage = makeStorage();
  const sandbox = {
    console,
    setTimeout() {},
    clearTimeout() {},
    setInterval() {},
    clearInterval() {},
    addEventListener() {},
    requestAnimationFrame() {},
    scrollTo() {},
    location: { reload() {} },
    TextEncoder,
    Uint8Array,
    crypto: webcrypto,
    Intl,
    Date,
    Math,
    Number,
    String,
    JSON,
    Object,
    Array,
    Promise,
    URL,
    isNaN,
    parseFloat,
    localStorage: storage,
    sessionStorage: storage,
    __fxFetches: 0,
    fetch: async url => {
      const href = String(url || "");
      if (href.includes("display_currency.json")) {
        sandbox.__fxFetches++;
        if (fxResponse && fxResponse.ok === false) return { ok: false, status: 404 };
        return { ok: true, json: async () => fxResponse || {} };
      }
      return { ok: false, status: 404 };
    },
  };
  sandbox.window = sandbox;
  sandbox.document = {
    getElementById: id => {
      if (!elements.has(id)) elements.set(id, stubElement(id));
      return elements.get(id);
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(scriptBody, sandbox, { filename: "dashboard-template.js" });
  return sandbox;
}

const offer = {
  id: "dual-currency",
  m: "gla_200",
  t: "Mercedes-Benz GLA 200",
  p: 10_000,
  q1: 15_000,
  mp: 20_000,
  sv: 5_000,
  sp: 33.33,
  dp: 50,
  pn: 20,
  ps: 3,
  pc: 2,
  y: 2025,
  km: 10_000,
  f: "petrol",
  c: "FR",
  s: "Source A",
  u: "https://example.test/dual-currency",
  ls: "2026-08-14T12:00:00Z",
  v: 1,
};

let assertions = 0;
function check(condition, message) {
  assertions++;
  if (!condition) throw new Error(message);
}

function evaluate(sandbox, source) {
  return vm.runInContext(source, sandbox, { filename: "dashboard-fx-exercise.js" });
}

(async () => {
  try {
    const filter = (html.match(/<select class="f" id="fp"[\s\S]*?<\/select>/) || [""])[0];
    check(filter.length > 0, "price filter must exist");
    check((filter.match(/<option\b/g) || []).length === 6, "price filter must retain its five EUR ranges");
    check((filter.match(/€/g) || []).length === 5, "each bounded price range must show EUR");
    check(scriptBody.includes("display_currency.json"), "dashboard must request the sealed FX sidecar");
    check(scriptBody.includes("validateFxConfig"), "dashboard must validate the sealed FX sidecar");
    check(html.includes('id="fxnote"'), "dashboard must expose FX source and fallback status");

    const valid = buildFx();
    const validSandbox = makeSandbox(valid);
    await evaluate(validSandbox, "FX_LOAD_PROMISE");
    validSandbox.__offer = offer;
    check(validSandbox.__fxFetches === 1, "valid: sidecar must be fetched once");
    check(evaluate(validSandbox, 'FX_STATE') === "ready", "valid: state must be ready");
    const expectedDzd = new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 0 }).format(Math.round(16_500 * 153.34490));
    const formatted = evaluate(validSandbox, "fmtMoney(16500)");
    check(formatted.includes("16"), "valid: EUR amount must remain primary");
    check(formatted.includes("€"), "valid: EUR code must remain visible");
    check(formatted.includes(expectedDzd) && formatted.includes("دج"), "valid: DZD conversion must be visible");
    const cardHtml = evaluate(validSandbox, "card(__offer)");
    check(cardHtml.includes("سعر شراء السيارة المعلن (قبل الشحن والجمارك)"), "card must label the observed price directly");
    check(cardHtml.includes("€") && cardHtml.includes("دج"), "card must show EUR and DZD for known amounts");
    check(cardHtml.includes("لم يُطبّق") && cardHtml.includes("لا تثبت أهلية الخصم"), "card must not invent a VAT deduction");
    check(cardHtml.includes("ليست تكلفة فعلية"), "estimator must be explicitly non-actual");
    check(evaluate(validSandbox, 'planningCost(10000,"1000","","500","0")') === null, "estimator must refuse an incomplete total");
    check(evaluate(validSandbox, 'planningCost(10000,"1000","750","500","0")') === 12250, "estimator must sum explicit user inputs");
    const fxNote = validSandbox.document.getElementById("fxnote").textContent;
    check(fxNote.includes("ALCES") && fxNote.includes(valid.semantic.effective_date), "valid: status must name source and effective date");
    check(fxNote.includes("لل عرض") === false, "valid: status wording must not contain malformed Arabic");

    const actual = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "display_currency.json"), "utf8"));
    validSandbox.__actualFx = actual;
    const validationTime = Date.parse(actual.captured_at_utc) + 60 * 60 * 1000;
    validSandbox.__validationTime = validationTime;
    const actualResult = await evaluate(validSandbox, "validateFxConfig(__actualFx,__validationTime)");
    check(actualResult.rateScaled === actual.semantic.sell_rate_scaled, "committed sidecar seal must validate");

    const tampered = buildFx();
    tampered.semantic.sell_rate = "199.99999";
    const scenarios = [
      ["missing", { ok: false }, "تعذر التحقق"],
      ["tampered", tampered, "تعذر التحقق"],
      ["stale", buildFx({ ageHours: 80 }), "قديم"],
    ];
    for (const [name, response, statusText] of scenarios) {
      const sandbox = makeSandbox(response);
      await evaluate(sandbox, "FX_LOAD_PROMISE");
      sandbox.__offer = offer;
      check(sandbox.__fxFetches === 1, `${name}: sidecar must be fetched once`);
      check(evaluate(sandbox, "FX") === null, `${name}: invalid FX must be discarded`);
      check(evaluate(sandbox, "FX_STATE") === "fallback", `${name}: state must fail closed`);
      const eurOnly = evaluate(sandbox, "fmtMoney(16500)");
      check(eurOnly.includes("€") && !eurOnly.includes("دج"), `${name}: money must fall back to EUR only`);
      const fallbackCard = evaluate(sandbox, "card(__offer)");
      check(fallbackCard.includes("€") && !fallbackCard.includes("دج"), `${name}: card must not fabricate DZD`);
      const note = sandbox.document.getElementById("fxnote").textContent;
      check(note.includes(statusText) && note.includes("اليورو وحده"), `${name}: fallback reason must be explicit`);
    }

    console.log(`DASHBOARD_FX_TEST_PASS assertions=${assertions} fallback_scenarios=${scenarios.length}`);
  } catch (error) {
    console.error(`DASHBOARD_FX_TEST_FAIL assertions=${assertions}: ${error.stack || error.message}`);
    process.exit(1);
  }
})();
