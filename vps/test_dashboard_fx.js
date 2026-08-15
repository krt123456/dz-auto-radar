#!/usr/bin/env node
"use strict";

/* EUR-invariance mutant gate. The dashboard must ignore the optional FX
 * sidecar completely: valid, missing, and malformed responses all render the
 * same EUR amounts and no active DZD UI.
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const INDEX = process.env.INDEX_HTML || path.join(__dirname, "..", "index.html");
const html = fs.readFileSync(INDEX, "utf8");
const scriptBody = (html.match(/<script>([\s\S]*?)<\/script>/) || [])[1];
if (!scriptBody) {
  console.error("FATAL: no <script> block found");
  process.exit(2);
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
  id: "eur-only",
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
  u: "https://example.test/eur-only",
  ls: "2026-08-14T12:00:00Z",
  v: 1,
};

let assertions = 0;
function check(condition, message) {
  assertions++;
  if (!condition) throw new Error(message);
}

function evaluate(sandbox, source) {
  return vm.runInContext(source, sandbox, { filename: "dashboard-eur-exercise.js" });
}

try {
  const filter = (html.match(/<select class="f" id="fp"[\s\S]*?<\/select>/) || [""])[0];
  check(filter.length > 0, "price filter must exist");
  check((filter.match(/<option\b/g) || []).length === 6, "price filter must retain its five ranges");
  check(!filter.includes("دج"), "price filter must not expose DZD labels");
  check((filter.match(/€/g) || []).length === 5, "each bounded price range must show EUR");
  check(!scriptBody.includes("display_currency.json"), "dashboard script must not fetch the FX sidecar");
  check(!scriptBody.includes("loadFx"), "dashboard script must not retain an active FX loader");
  check(!html.includes('id="fxnote"'), "dashboard must not expose an FX status region");

  const scenarios = [
    ["valid", { semantic: { sell_rate: "153.40390", rate_scale: 100000, effective_date: "2026-08-13" } }],
    ["missing", { ok: false }],
    ["malformed", { semantic: { sell_rate: "not-a-rate", rate_scale: "broken" } }],
  ];
  const expected = new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 0 }).format(16_500) + " €";
  for (const [name, response] of scenarios) {
    const sandbox = makeSandbox(response);
    sandbox.__offer = offer;
    check(evaluate(sandbox, "fmtMoney(16500)") === expected, `${name}: money must render in EUR`);
    const cardHtml = evaluate(sandbox, "card(__offer)");
    check(cardHtml.includes("€"), `${name}: cards must display EUR`);
    check(!cardHtml.includes("دج"), `${name}: cards must not display DZD`);
    check(sandbox.__fxFetches === 0, `${name}: dashboard must not request the FX sidecar`);
    check(evaluate(sandbox, 'typeof FX') === "undefined", `${name}: active FX state must not exist`);
  }

  console.log(`DASHBOARD_EUR_TEST_PASS assertions=${assertions} scenarios=${scenarios.length}`);
} catch (error) {
  console.error(`DASHBOARD_EUR_TEST_FAIL assertions=${assertions}: ${error.message}`);
  process.exit(1);
}
