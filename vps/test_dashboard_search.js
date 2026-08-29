#!/usr/bin/env node
"use strict";

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

function makeStorage(initial = {}) {
  const values = new Map(Object.entries(initial).map(([key, value]) => [key, String(value)]));
  return {
    getItem: key => (values.has(key) ? values.get(key) : null),
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key),
  };
}

function stubElement(id) {
  const attributes = new Map();
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
    setAttribute(name, value) { attributes.set(name, String(value)); },
    getAttribute(name) { return attributes.has(name) ? attributes.get(name) : null; },
  };
}

function makeSandbox({ local = {}, session = {} } = {}) {
  const elements = new Map();
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
    localStorage: makeStorage(local),
    sessionStorage: makeStorage(session),
    fetch: async () => ({ ok: false, status: 404 }),
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

function offer(id, title, model, country = "FR") {
  return {
    id,
    m: model,
    t: title,
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
    c: country,
    s: "Source A",
    u: `https://example.test/${id}`,
    ls: "2026-08-14T12:00:00Z",
    v: 1,
  };
}

const offers = [
  offer("mercedes", "Mercedes-Benz GLA 200", "gla_200"),
  offer("mercedes-de", "Mercedes-Benz C 200", "c_200", "DE"),
  offer("citroen", "Citroën C3 PureTech", "c3_puretech"),
  offer("skoda", "Škoda Octavia", "octavia_1.5-tsi"),
  offer("seat-fr-de", "Seat Ibiza FR LED", "ibiza", "DE"),
  offer("lexus-is-bg", "Lexus IS 300", "is_300", "BG"),
  offer("bmw-at-sk", "BMW 320d AT M-Sport", "320d", "SK"),
];
const payload = {
  schema_version: 2,
  algorithm: "schengen-observed-peer-value-v7-live-verified",
  unsupported_economics_published: 0,
  data_generated_at_utc: "2026-08-14T12:00:00Z",
  board_built_at_utc: "2026-08-14T12:00:00Z",
  validation: { generated_at: "2026-08-14T12:00:00Z" },
  connected_source_count: 3,
  connected_country_count: 2,
  verified_live_count: offers.length,
  offers,
};

let assertions = 0;
function check(condition, message) {
  assertions++;
  if (!condition) throw new Error(message);
}

function evaluate(sandbox, source) {
  return vm.runInContext(source, sandbox, { filename: "dashboard-search-exercise.js" });
}

function matchingIds(sandbox, query, country = "") {
  sandbox.document.getElementById("q").value = query;
  sandbox.document.getElementById("fc").value = country;
  return Array.from(evaluate(sandbox, "apply(); VIEW.map(offer=>offer.id)"));
}

try {
  const local = {
    "dzr-known-offers-v1": JSON.stringify([
      "mercedes", "mercedes-de", "citroen", "seat-fr-de", "lexus-is-bg", "bmw-at-sk",
    ]),
    "dzr-new-offers-v1": "[]",
    "dzr-visited-offers-v1": "[]",
  };
  const fresh = makeSandbox({ local });
  fresh.__payload = payload;
  evaluate(fresh, "boot(__payload)");

  check(
    evaluate(fresh, 'normalizeSearchText("  Mercedes—Benz_GLA+200  ")') === "mercedes benz gla 200",
    "normalization must decompose case and map punctuation, symbols, underscores, and whitespace",
  );
  check(
    JSON.stringify(matchingIds(fresh, "Mercedes Benz")) === JSON.stringify(["mercedes", "mercedes-de"]),
    "Mercedes Benz must match Mercedes-Benz",
  );
  check(
    JSON.stringify(matchingIds(fresh, "Citroen")) === JSON.stringify(["citroen"]),
    "Citroen must match Citroën",
  );
  check(
    JSON.stringify(matchingIds(fresh, "Skoda")) === JSON.stringify(["skoda"]),
    "Skoda must match Škoda",
  );
  check(
    JSON.stringify(matchingIds(fresh, "Mercedes France")) === JSON.stringify(["mercedes"]),
    "combined brand-country search must match the localized country document",
  );
  check(
    JSON.stringify(matchingIds(fresh, "Mercedes/France")) === JSON.stringify(["mercedes"]),
    "slash-separated brand-country search must normalize into AND-matched tokens",
  );
  check(
    JSON.stringify(matchingIds(fresh, "Mercedes GLA")) === JSON.stringify(["mercedes"]),
    "non-contiguous brand and model tokens must match the same offer",
  );
  check(
    JSON.stringify(matchingIds(fresh, "Mercedes", "FR")) === JSON.stringify(["mercedes"]),
    "text search and the explicit country filter must compose",
  );
  check(
    JSON.stringify(matchingIds(fresh, "Citroen France")) === JSON.stringify(["citroen"]),
    "diacritic folding and country aliases must compose",
  );
  check(
    JSON.stringify(matchingIds(fresh, "Skoda فرنسا")) === JSON.stringify(["skoda"]),
    "Arabic country labels must participate in combined search",
  );
  check(
    JSON.stringify(matchingIds(fresh, "FR")) === JSON.stringify(["seat-fr-de"]),
    "FR must remain a trim search rather than silently becoming France",
  );
  check(
    JSON.stringify(matchingIds(fresh, "IS")) === JSON.stringify(["lexus-is-bg"]),
    "IS must remain a model search rather than silently becoming Iceland",
  );
  check(
    JSON.stringify(matchingIds(fresh, "AT")) === JSON.stringify(["bmw-at-sk"]),
    "AT must remain a transmission search rather than silently becoming Austria",
  );
  fresh.document.getElementById("q").value = "";
  evaluate(fresh, "apply()");
  check(evaluate(fresh, "NEW_IDS.size") === 1, "fixture must contain one newly discovered offer");
  check(fresh.document.getElementById("fnew").checked === false, "fresh session must not auto-enable new-only");
  check(evaluate(fresh, "VIEW.length") === offers.length, "default view must not hide non-new offers");

  const restoredNavigation = {
    page: 1,
    scrollY: 0,
    values: { q: "", fc: "", fsrc: "", ff: "", fy: "", fp: "", fs: "ranked" },
    checks: { flive: true, fnew: true, funseen: false },
  };
  const restored = makeSandbox({
    local,
    session: { "dzr-navigation-v2": JSON.stringify(restoredNavigation) },
  });
  restored.__payload = payload;
  evaluate(restored, "boot(__payload)");
  check(restored.document.getElementById("fnew").checked === true, "restored new-only choice must remain checked");
  check(
    JSON.stringify(Array.from(evaluate(restored, "VIEW.map(offer=>offer.id)"))) === JSON.stringify(["skoda"]),
    "restored new-only choice must filter the view",
  );

  console.log(`DASHBOARD_SEARCH_TEST_PASS assertions=${assertions}`);
} catch (error) {
  console.error(`DASHBOARD_SEARCH_TEST_FAIL assertions=${assertions}: ${error.message}`);
  process.exit(1);
}
