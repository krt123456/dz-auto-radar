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
    hidden: false,
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
    Event,
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
  vm.runInContext(scriptBody, sandbox, { filename: "dashboard-auction-exercise.js" });
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
];

const lane = (end, bid, priority, seen, id) => ({
  id,
  source: "zoll-auktion",
  source_key: "zoll-auktion",
  registry_key: "zoll-auktion",
  registry_priority: priority,
  url: `https://www.zoll-auktion.de/lot/${id}`,
  title: `Auction ${id}`,
  model: "golf",
  country: "DE",
  year: 2018,
  mileage: 100000,
  fuel: "petrol",
  seller: "public",
  current_bid_eur: bid,
  canonical_end_utc: end,
  ends_soon: priority === 1,
  first_seen_at: null,
  last_seen_at: seen,
  access_sale_note: "note",
  evidence: "mgr-e325f6c9",
});

const BASE = {
  schema_version: 2,
  algorithm: "schengen-observed-peer-value-v7-live-verified",
  unsupported_economics_published: 0,
  data_generated_at_utc: "2026-08-14T14:00:00Z",
  board_built_at_utc: "2026-08-14T14:00:00Z",
  generation_id: "gen-test-1234",
  validation: {
    generated_at: "2026-08-14T14:00:00Z",
    counts: { verified: offers.length, dead: 0, unknown: 0 },
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
  return vm.runInContext(source, sandbox, { filename: "dashboard-auction-exercise.js" });
}

function bootWith(sandbox, payload) {
  sandbox.__payload = payload;
  evaluate(sandbox, "boot(__payload)");
}

try {
  const wrapHtml = (html.match(/<label class="f check" id="auctionToggleWrap"[\s\S]*?<\/label>/) || [""])[0];
  check(wrapHtml.includes('id="fauction"'), "auction toggle checkbox must exist");
  check(wrapHtml.includes("hidden"), "auction toggle must start hidden");
  check(/[\u0600-\u06ff]/.test(wrapHtml), "auction toggle must have an Arabic label");

  const local = {
    "dzr-known-offers-v1": JSON.stringify(offers.map(row => row.id)),
    "dzr-new-offers-v1": "[]",
    "dzr-visited-offers-v1": "[]",
  };

  // --- absent lane: toggle stays hidden, everything else unchanged ----------
  const noLane = makeSandbox({ local });
  bootWith(noLane, BASE);
  check(noLane.document.getElementById("auctionToggleWrap").hidden === true, "toggle must stay hidden when the lane is absent");
  check(noLane.document.getElementById("fauction").checked === false, "toggle must default to unchecked");
  check(evaluate(noLane, "AUCTION_LANE === null"), "no lane state when the lane is absent");
  evaluate(noLane, "$('fs').value='price'; apply();");
  check(evaluate(noLane, "VIEW.map(o=>o.id)").join(",") === "tie-b,rank-z", "regular lane behavior must be unchanged without the lane");

  // --- valid lane: toggle visible, auction-only view, end-time hiding --------
  const nowIso = new Date().toISOString();
  const futureIso = new Date(Date.now() + 3600_000).toISOString();
  const pastIso = new Date(Date.now() - 3600_000).toISOString();
  const rows = [
    { ...lane(futureIso, 4000, 1, "2026-08-14T14:00:00Z", "a-1"),
      ouedkniss_reference: { average_dzd: 5_100_000, sample_count: 3,
        observed_at_utc: nowIso, source: "Ouedkniss" } },
    lane(futureIso, 9000, 2, "2026-08-14T14:00:00Z", "a-2"),
    lane(pastIso, 7000, 3, "2026-08-14T14:00:00Z", "a-ended"),
  ];
  const payload = {
    ...BASE,
    auction_lane: {
      schema_version: 1,
      lane: "auction",
      registry_digest: "registry-digest-abcdef0123456789",
      generated_at_utc: "2026-08-14T14:00:00Z",
      bound_generation_id: BASE.generation_id,
      bound_data_generated_at_utc: BASE.data_generated_at_utc,
      lane_count: rows.length,
      rows,
    },
  };

  const withLane = makeSandbox({ local });
  bootWith(withLane, payload);
  check(withLane.document.getElementById("auctionToggleWrap").hidden === false, "toggle must be visible when the lane is present");
  check(withLane.document.getElementById("fauction").checked === false, "toggle must default to unchecked when the lane is present");
  check(evaluate(withLane, "AUCTION_LANE.length") === 3, "lane rows must be loaded into state");
  evaluate(withLane, "populateFilterOptions(true)");
  const sourceOptions = withLane.document.getElementById("fsrc").innerHTML;
  for (const source of [
    "Zoll-Auktion", "Les Enchères du Domaine", "Justiz-Auktion", "BOE Subastas",
    "Kronofogden Auktionstorget", "Portale delle Vendite Pubbliche", "Fin Shop",
    "Domeinen Roerende Zaken", "e-Leilões", "Licytacje Komornicze",
  ]) {
    check(sourceOptions.includes(source), `${source} must always appear in the auction source filter`);
  }

  // unchecked: regular offers only
  evaluate(withLane, "apply();");
  check(evaluate(withLane, "VIEW.every(o=>typeof o.sv === 'number')"), "unchecked toggle shows regular offers only");

  // checked: auction-only, ended lots hidden immediately, ending-soon sort
  withLane.document.getElementById("fauction").checked = true;
  withLane.document.getElementById("fs").value = "auction_soon";
  evaluate(withLane, "apply();");
  const auctionIds = evaluate(withLane, "VIEW.map(o=>o.id)");
  check(auctionIds.includes("a-ended") === false, "ended lot must be hidden immediately", );
  check(auctionIds.join(",") === "a-1,a-2", "auction view must show only live lots, ending soon first");

  // lowest current EUR bid
  withLane.document.getElementById("fs").value = "auction_bid";
  evaluate(withLane, "apply();");
  check(evaluate(withLane, "VIEW.map(o=>o.id)").join(",") === "a-1,a-2", "lowest-bid sort must order by current bid");

  // newest vetted source (registry priority asc)
  withLane.document.getElementById("fs").value = "auction_vetted";
  evaluate(withLane, "apply();");
  check(evaluate(withLane, "VIEW.map(o=>o.id)").join(",") === "a-1,a-2", "vetted-source sort must order by registry priority");

  // auction search narrows the lane
  withLane.document.getElementById("q").value = "a-2";
  withLane.document.getElementById("fs").value = "auction_soon";
  evaluate(withLane, "apply();");
  check(evaluate(withLane, "VIEW.map(o=>o.id)").join(",") === "a-2", "auction search must filter lane rows");
  withLane.document.getElementById("q").value = "";

  // normal filters must also apply inside the auction lane
  withLane.document.getElementById("fsrc").value = "missing-source";
  evaluate(withLane, "apply();");
  check(evaluate(withLane, "VIEW.length") === 0, "auction source filter must filter lane rows");
  withLane.document.getElementById("fsrc").value = "";

  // auction card must not claim ROI/profit and must show the bid and end
  evaluate(withLane, "apply();");
  const firstCard = evaluate(withLane, "cardForTest=auctionCard(VIEW[0]); cardForTest");
  check(firstCard.includes("المزايدة الحالية") && !/roi|profit|ربح|عائد/i.test(firstCard), "auction card must show bid without profit claims");
  check(firstCard.includes("واد كنيس") && firstCard.includes("إعلانات مشابهة"), "auction card must show a validated Ouedkniss average");

  // cross-lane isolation: toggle off restores regular lane
  withLane.document.getElementById("fauction").checked = false;
  withLane.document.getElementById("fs").value = "price";
  evaluate(withLane, "apply();");
  check(evaluate(withLane, "VIEW.map(o=>o.id)").join(",") === "tie-b,rank-z", "unchecking must restore the regular lane");

  // --- negative controls: malformed lanes must fail closed ------------------
  const mutants = [
    ["wrong lane key", { ...payload.auction_lane, lane: "regular" }],
    ["wrong schema version", { ...payload.auction_lane, schema_version: 2 }],
    ["count mismatch", { ...payload.auction_lane, lane_count: rows.length + 1 }],
    ["unbound generation", { ...payload.auction_lane, bound_generation_id: "other-gen" }],
    ["bad bid", { ...payload.auction_lane, rows: [{ ...rows[0], current_bid_eur: 0 }] }],
    ["missing url", { ...payload.auction_lane, rows: [{ ...rows[0], url: "" }] }],
    ["bad registry digest", { ...payload.auction_lane, registry_digest: "" }],
    ["bad Ouedkniss sample", { ...payload.auction_lane, lane_count: 1, rows: [{ ...rows[0],
      ouedkniss_reference: { average_dzd: 5_100_000, sample_count: 1,
        observed_at_utc: nowIso, source: "Ouedkniss" } }] }],
  ];
  for (const [name, laneMutant] of mutants) {
    const bad = makeSandbox({ local });
    bad.__payload = { ...payload, auction_lane: laneMutant };
    let threw = false;
    try {
      evaluate(bad, "boot(__payload)");
    } catch (error) {
      threw = error && error.message === "contract";
    }
    check(threw, `${name}: malformed lane must fail closed with contract error`);
  }

  console.log(`DASHBOARD_AUCTION_TEST_PASS assertions=${assertions}`);
} catch (error) {
  console.error(`DASHBOARD_AUCTION_TEST_FAIL assertions=${assertions}: ${error.message}`);
  process.exit(1);
}
