#!/usr/bin/env node
"use strict";
/* Dashboard template dashboard-currency mutant-gate test.
 * Extracts the inline <script> from index.html and exercises the FX/DZD
 * conversion logic under a stubbed DOM.  Every assertion can fail: the
 * negative controls feed deliberately broken FX configs and require the
 * template to fail closed into EUR display.
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const INDEX = process.env.INDEX_HTML || path.join(__dirname, "..", "index.html");
const html = fs.readFileSync(INDEX, "utf8");
const scriptBody = (html.match(/<script>([\s\S]*?)<\/script>/) || [])[1];
if (!scriptBody) { console.error("FATAL: no <script> block found"); process.exit(2); }

function stubElement() {
  return {
    value: "", checked: false, textContent: "", innerHTML: "",
    disabled: false, style: {}, onclick: null, options: [], dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    addEventListener() {}, removeAttribute() {}, setAttribute() {},
  };
}

function makeSandbox(fxResponse) {
  const elements = new Map();
  const storage = (() => { const m = new Map(); return {
    getItem: k => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: k => m.delete(k),
  }; })();
  const sandbox = {
    console,
    setTimeout() {}, clearTimeout() {}, setInterval() {}, clearInterval() {},
    addEventListener() {}, requestAnimationFrame() {}, scrollTo() {},
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
    isNaN,
    parseFloat,
    localStorage: storage,
    sessionStorage: storage,
    fetch: async url => {
      const href = String(url || "");
      if (href.includes("display_currency.json")) {
        if (fxResponse && fxResponse.ok === false) return { ok: false, status: 404 };
        return { ok: true, json: async () => fxResponse || {} };
      }
      return { ok: false, status: 404 };
    },
  };
  sandbox.window = sandbox;
  sandbox.document = {
    getElementById: id => {
      if (!elements.has(id)) elements.set(id, stubElement());
      return elements.get(id);
    },
  };
  sandbox.seedPriceOptions = () => {
    const select = sandbox.document.getElementById("fp");
    if (!select.options.length) {
      for (const value of ["0-10000", "10000-15000", "15000-20000", "20000-30000", "30000-999999"]) {
        select.options.push({ value, textContent: "" });
      }
    }
  };
  const nf = new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 0 });
  sandbox.__FX = {
    EUR16500: nf.format(16500) + " €",
    DZD16500: nf.format(2531164) + " دج",
    BOUND0: nf.format(1.53) + " مليون دج",
    BOUND1: nf.format(2.3) + " مليون دج",
  };
  sandbox.__results = { passed: 0, failed: 0, errors: [] };
  sandbox.__check = (name, fn) => {
    try { fn(); sandbox.__results.passed++; console.log("PASS " + name); }
    catch (err) { sandbox.__results.failed++; sandbox.__results.errors.push(name + ": " + err.message); console.error("FAIL " + name + " -> " + err.message); }
  };
  vm.createContext(sandbox);
  vm.runInContext(scriptBody, sandbox, { filename: "dashboard-template.js" });
  return sandbox;
}

const VALID_FX = {
  semantic: { sell_rate: "153.40390", rate_scale: 100000, effective_date: "2026-08-13" },
};

const ALL_RESULTS = [];
async function scenario(name, fxResponse, body) {
  const sandbox = makeSandbox(fxResponse);
  sandbox.seedPriceOptions();
  try {
    await vm.runInContext("(async()=>{" + body + "})()", sandbox, { filename: name + ".js" });
  } finally {
    ALL_RESULTS.push({ name, results: sandbox.__results });
  }
  return sandbox;
}

(async () => {
  /* Baseline: without FX the template must keep EUR fallback display. */
  const base = await scenario("baseline", { ok: false }, `
    __check("EUR fallback before FX load", () => {
      if (FX !== null) throw new Error("FX unexpectedly set: " + JSON.stringify(FX));
    });
    await loadFx();
    __check("fmtMoney fallback is EUR", () => {
      if (fmtMoney(16500) !== __FX.EUR16500) throw new Error("got " + fmtMoney(16500));
    });
    __check("fxnote fallback message", () => {
      const text = document.getElementById("fxnote").textContent;
      if (text !== "تعذر جلب سعر الصرف الجمركي؛ تُعرض المبالغ باليورو مؤقتًا.") throw new Error("note: " + text);
    });
    __check("static filter labels carry no EUR", () => {
      for (const o of document.getElementById("fp").options) {
        if (o.textContent.includes("€")) throw new Error("EUR leaked into label: " + o.textContent);
      }
    });
  `);

  /* Valid sealed rate: prices must render in DZD. */
  const good = await scenario("valid", VALID_FX, `
    await loadFx();
    __check("FX loaded with valid config", () => {
      if (!FX || FX.sell !== 153.40390 || FX.scale !== 100000) throw new Error(JSON.stringify(FX));
    });
    __check("fmtDzd converts 16500 EUR", () => {
      if (fmtDzd(16500) !== __FX.DZD16500) throw new Error("got " + fmtDzd(16500));
    });
    __check("fmtMoney uses DZD when FX present", () => {
      if (fmtMoney(16500) !== __FX.DZD16500) throw new Error("got " + fmtMoney(16500));
    });
    __check("fxnote shows rate and date", () => {
      const text = document.getElementById("fxnote").textContent;
      if (!text.includes("دج") || !text.includes("2026-08-13")) throw new Error("note: " + text);
    });
    __check("filter labels updated to DZD ranges", () => {
      const labels = document.getElementById("fp").options.map(o => o.textContent);
      if (labels[0] !== "حتى " + __FX.BOUND0) throw new Error("first label: " + labels[0]);
      if (labels[1] !== __FX.BOUND0 + " — " + __FX.BOUND1) throw new Error("second label: " + labels[1]);
      if (labels.some(t => t.includes("€"))) throw new Error("EUR leaked after FX load");
    });
  `);

  /* Negative control A: zero rate must be rejected (fail closed to EUR). */
  await scenario("zero", { semantic: { sell_rate: "0.00000", rate_scale: 100000, effective_date: "2026-08-13" } }, `
    await loadFx();
    __check("zero sell rate is rejected", () => {
      if (FX !== null) throw new Error("FX accepted zero rate: " + JSON.stringify(FX));
    });
    __check("zero-rate fallback stays EUR", () => {
      if (fmtMoney(16500) !== __FX.EUR16500) throw new Error("got " + fmtMoney(16500));
    });
  `);

  /* Negative control B: non-numeric rate must be rejected. */
  await scenario("garbage", { semantic: { sell_rate: "abc", rate_scale: 100000, effective_date: "2026-08-13" } }, `
    await loadFx();
    __check("non-numeric sell rate is rejected", () => {
      if (FX !== null) throw new Error("FX accepted garbage");
    });
    __check("garbage-rate fallback stays EUR", () => {
      if (fmtMoney(16500) !== __FX.EUR16500) throw new Error("got " + fmtMoney(16500));
    });
  `);

  /* Negative control C: missing config (http 404) must fail closed. */
  await scenario("missing", { ok: false }, `
    await loadFx();
    __check("missing config is rejected", () => {
      if (FX !== null) throw new Error("FX accepted 404");
    });
    __check("404 fallback stays EUR", () => {
      if (fmtMoney(16500) !== __FX.EUR16500) throw new Error("got " + fmtMoney(16500));
    });
  `);

  const passedTotal = ALL_RESULTS.reduce((sum, s) => sum + s.results.passed, 0);
  const failedTotal = ALL_RESULTS.reduce((sum, s) => sum + s.results.failed, 0);
  const allErrors = ALL_RESULTS.flatMap(s => s.results.errors.map(e => s.name + ": " + e));
  console.log(`\nDASHBOARD_FX_TEST_RESULT passed=${passedTotal} failed=${failedTotal} scenarios=${ALL_RESULTS.length}`);
  if (allErrors.length) console.error(allErrors.join("\n"));
  process.exit(failedTotal ? 1 : 0);
})().catch(err => { console.error("HARNESS_ERROR", err); process.exit(2); });