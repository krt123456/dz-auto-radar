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
  const classes = new Set();
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
      add(...names) { names.forEach(name => classes.add(name)); },
      remove(...names) { names.forEach(name => classes.delete(name)); },
      toggle(name, force) {
        if (force === true) { classes.add(name); return true; }
        if (force === false) { classes.delete(name); return false; }
        if (classes.has(name)) { classes.delete(name); return false; }
        classes.add(name); return true;
      },
      contains(name) { return classes.has(name); },
    },
    addEventListener() {},
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

function roundRatioHalfEven(numerator, denominator) {
  const quotient = Math.floor(numerator / denominator);
  const remainder = numerator - quotient * denominator;
  if (remainder * 2 < denominator) return quotient;
  if (remainder * 2 > denominator) return quotient + 1;
  return quotient + (quotient % 2);
}

function offer(id, price, saving, observedAt, km) {
  const q1 = price + saving;
  const median = q1 + 5_000;
  return {
    id,
    m: "gla_200",
    t: `Offer ${id}`,
    p: price,
    q1,
    mp: median,
    sv: saving,
    sp: roundRatioHalfEven(10_000 * saving, q1) / 100,
    dp: roundRatioHalfEven(10_000 * (median - price), median) / 100,
    pn: 20,
    ps: 3,
    pc: 2,
    y: 2025,
    km,
    f: "petrol",
    c: "FR",
    s: "Source A",
    u: `https://example.test/${id}`,
    ls: observedAt,
    v: 1,
  };
}

const offers = [
  offer("rank-z", 12_000, 4_000, "2026-08-14T12:00:00Z", 30_000),
  offer("tie-b", 9_000, 2_500, "2026-08-14T13:00:00Z", 12_000),
  offer("tie-a", 9_000, 2_500, "2026-08-14T13:00:00Z", 8_000),
  offer("saving-top", 15_000, 7_000, "2026-08-14T14:00:00Z", 20_000),
];
const payload = {
  schema_version: 2,
  algorithm: "schengen-observed-peer-value-v7-live-verified",
  unsupported_economics_published: 0,
  data_generated_at_utc: "2026-08-14T14:00:00Z",
  board_built_at_utc: "2026-08-14T14:00:00Z",
  validation: {
    generated_at: "2026-08-14T14:00:00Z",
    counts: { verified: offers.length, dead: 3, unknown: 3 },
  },
  connected_source_count: 3,
  connected_country_count: 2,
  universe_unique_offers: 100,
  eligible_observed_rows: 20,
  ranked_candidate_rows: 10,
  qualified_universe_offers: offers.length,
  verified_live_count: offers.length,
  offers,
};

let assertions = 0;
function check(condition, message) {
  assertions++;
  if (!condition) throw new Error(message);
}

function evaluate(sandbox, source) {
  return vm.runInContext(source, sandbox, { filename: "dashboard-sort-exercise.js" });
}

function viewIds(sandbox, sort) {
  sandbox.document.getElementById("fs").value = sort;
  return Array.from(evaluate(sandbox, "apply(); VIEW.map(offer=>offer.id)"));
}

function expectOrder(actual, expected, message) {
  check(JSON.stringify(actual) === JSON.stringify(expected), `${message}: ${actual.join(",")}`);
}

try {
  const sortSelect = (html.match(/<select class="f" id="fs"[\s\S]*?<\/select>/) || [""])[0];
  const controls = [...sortSelect.matchAll(/<option value="([^"]+)">([^<]+)<\/option>/g)]
    .map(match => ({ value: match[1], label: match[2] }));
  check(controls.length === 5, `expected exactly five sort controls, got ${controls.length}`);
  check(
    JSON.stringify(controls.map(control => control.value)) === JSON.stringify(["ranked", "price", "saving", "recent", "km"]),
    "sort controls must be ranked, price, saving, recent, km",
  );
  check(controls.every(control => /[\u0600-\u06ff]/.test(control.label)), "every sort control must have an Arabic label");
  check(!controls.some(control => /roi|profit|ربح|عائد/i.test(control.label)), "sort controls must not claim ROI or profit");
  check(controls.find(control => control.value === "saving").label.includes("فرق مرصود"), "saving must remain an observed difference");
  check(html.includes('id="sortHelp"'), "sort controls must include a concise explanation");
  check(html.includes('id="coverageTruth"'), "dashboard must include a coverage-truth alert");
  check(sortSelect.includes('aria-describedby="sortHelp"'), "sort control must be linked to its explanation");

  check(evaluate(makeSandbox(), 'autoscoutCollectionUrl("https://www.autoscout24.com/lst/toyota/123456")'), "AutoScout collection URLs must fail the display gate");
  check(!evaluate(makeSandbox(), 'autoscoutCollectionUrl("https://www.autoscout24.it/annunci/car-503f6455-b5a5-48af-bcfa-8a08c1dd87c7")'), "AutoScout detail URLs must remain displayable");
  check(evaluate(makeSandbox(), 'legacyDamagedOffer({t:"Citroën C3 hybride endommagé",m:"c3"})'), "accented French damaged offers must fail the display gate");
  check(!evaluate(makeSandbox(), 'legacyDamagedOffer({t:"Peugeot avec protection anti-endommagement",m:"208"})'), "related non-damaged wording must remain displayable");

  const local = {
    "dzr-known-offers-v1": JSON.stringify(offers.map(row => row.id)),
    "dzr-new-offers-v1": "[]",
    "dzr-visited-offers-v1": "[]",
  };
  const sandbox = makeSandbox({ local });
  sandbox.__payload = payload;
  evaluate(sandbox, "boot(__payload)");

  check(
    sandbox.document.getElementById("stats").innerHTML.includes("مرشحون مرتبون بالقيمة المرصودة"),
    "stats must distinguish the ranked candidate pool from published offers",
  );
  const statsHtml = sandbox.document.getElementById("stats").innerHTML;
  check(statsHtml.includes('data-stat="ranked" data-value="10"'), "stats must show exactly 10 ranked candidates");
  check(statsHtml.includes('data-stat="validated-verified" data-value="4"'), "stats must show exactly 4 validation successes");
  check(statsHtml.includes('data-stat="dead" data-value="3"'), "stats must show exactly 3 dead links");
  check(statsHtml.includes('data-stat="unknown" data-value="3"'), "stats must show exactly 3 unknown links");
  check(statsHtml.includes('data-stat="published-verified" data-value="4"'), "stats must show exactly 4 published verified links");
  check(
    sandbox.document.getElementById("stats").innerHTML.includes("روابط غير محسومة مخفية"),
    "stats must disclose unresolved hidden links",
  );
  check(
    sandbox.document.getElementById("coverageTruth").textContent.includes("ليست إثباتًا للأفضل من كامل المخزون"),
    "coverage alert must constrain the best-value claim",
  );
  check(
    sandbox.document.getElementById("coverageNote").textContent.includes("الأفضل بين الروابط المثبتة"),
    "coverage note must constrain ranking to verified links",
  );
  check(
    sandbox.document.getElementById("sortHelp").textContent.includes("الروابط المتحقق منها"),
    "default sort explanation must constrain ranking to verified links",
  );

  const noUnknown = makeSandbox({ local });
  noUnknown.__payload = {
    ...payload,
    validation: { ...payload.validation, counts: { verified: offers.length, dead: 0, unknown: 0 } },
  };
  evaluate(noUnknown, "boot(__payload)");
  check(noUnknown.document.getElementById("coverageTruth").classList.contains("hidden"), "zero unknown links must hide coverage alert");
  check(noUnknown.document.getElementById("coverageTruth").textContent === "", "zero unknown links must clear coverage alert text");

  expectOrder(viewIds(sandbox, "ranked"), ["rank-z", "tie-b", "tie-a", "saving-top"], "ranked order");
  expectOrder(viewIds(sandbox, "price"), ["tie-b", "tie-a", "rank-z", "saving-top"], "price order with payload-rank tie");
  expectOrder(viewIds(sandbox, "saving"), ["saving-top", "rank-z", "tie-b", "tie-a"], "saving order with payload-rank tie");
  expectOrder(viewIds(sandbox, "recent"), ["saving-top", "tie-b", "tie-a", "rank-z"], "recent order with payload-rank tie");
  expectOrder(viewIds(sandbox, "km"), ["tie-a", "tie-b", "saving-top", "rank-z"], "odometer order");
  check(
    sandbox.document.getElementById("sortHelp").textContent.includes("عداد السيارة"),
    "km: explanation must state what is sorted",
  );

  evaluate(sandbox, 'ORIGINAL_RANK.delete("tie-a"); ORIGINAL_RANK.delete("tie-b")');
  expectOrder(viewIds(sandbox, "price"), ["tie-a", "tie-b", "rank-z", "saving-top"], "lexical ID final tie-break");

  for (const legacy of ["discount", "median", "year", "unknown"] ) {
    const navigation = {
      page: 1,
      scrollY: 0,
      values: { q: "", fc: "", fsrc: "", ff: "", fy: "", fp: "", fs: legacy },
      checks: { flive: true, fnew: false, funseen: false },
    };
    const restored = makeSandbox({
      local,
      session: { "dzr-navigation-v2": JSON.stringify(navigation) },
    });
    restored.__payload = payload;
    evaluate(restored, "boot(__payload)");
    check(restored.document.getElementById("fs").value === "ranked", `${legacy}: legacy sort must fall back to ranked`);
    expectOrder(
      Array.from(evaluate(restored, "VIEW.map(offer=>offer.id)")),
      ["rank-z", "tie-b", "tie-a", "saving-top"],
      `${legacy}: legacy fallback order`,
    );
  }

  console.log(`DASHBOARD_SORT_TEST_PASS assertions=${assertions}`);
} catch (error) {
  console.error(`DASHBOARD_SORT_TEST_FAIL assertions=${assertions}: ${error.message}`);
  process.exit(1);
}
