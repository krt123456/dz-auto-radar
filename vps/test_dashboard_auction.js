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
  const listeners = new Map();
  const attributes = new Map();
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
    addEventListener(type, handler) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(handler);
    },
    dispatchEvent(event) {
      for (const handler of listeners.get(event.type) || []) handler.call(this, event);
      return true;
    },
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

const REGISTRY = JSON.stringify({
  registry_sha256_source: "dashboard-auction-test",
  sources: [
    ["zoll-auktion", ["zoll-auktion.de"]],
    ["encheres-du-domaine", ["encheres-domaine.gouv.fr"]],
    ["justiz-auktion", ["justiz-auktion.de"]],
    ["boe-subastas", ["subastas.boe.es"]],
    ["kronofogden", ["auktion.kronofogden.se"]],
    ["pvp-giustizia", ["pvp.giustizia.it"]],
    ["finshop", ["finshop.belgium.be"]],
    ["onlineveilingmeester", ["onlineveilingmeester.nl"]],
    ["e-leiloes", ["e-leiloes.pt"]],
    ["licytacje-komornik", ["licytacje.komornik.pl"]],
    ["nabidka-majetku", ["nabidkamajetku.gov.cz"]],
    ["vebeg", ["vebeg.de"]],
    ["auto1", ["auto1.com"], "blocked"],
    ["copart-es", ["copart.es"]],
  ].map(([key, domains, publicationStatus], index) => ({
    key, domains, priority: index + 1, publication_status: publicationStatus || "migration", name: key,
  })),
});

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
  check(html.includes('id="eligibleAuctionsBtn"') && html.includes('id="allAuctionsBtn"'),
    "auction section must expose strict-eligible and all-official controls");
  check(html.includes('id="fBid"') && html.includes('id="fTerms"'),
    "auction section must expose optional bid-visibility and no-reserve filters");
  check(html.includes("كل المزادات الرسمية"), "all-official control must have an Arabic label");

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
      registry_digest: REGISTRY,
      generated_at_utc: "2026-08-14T14:00:00Z",
      bound_generation_id: BASE.generation_id,
      bound_data_generated_at_utc: BASE.data_generated_at_utc,
      lane_count: rows.length,
      rows,
    },
  };

  const withLane = makeSandbox({ local });
  withLane.document.getElementById("fy").value = "2025";
  withLane.document.getElementById("fp").value = "0-10000";
  bootWith(withLane, payload);
  check(withLane.document.getElementById("auctionToggleWrap").hidden === false, "toggle must be visible when the lane is present");
  check(withLane.document.getElementById("fauction").checked === true, "auction lane must be selected by default when present");
  check(withLane.document.getElementById("auctionModeBtn").hidden === false, "separate auction tab must be visible when the lane is present");
  check(withLane.document.getElementById("auctionModeBtn").classList.contains("active"), "separate auction tab must be active by default");
  check(withLane.document.getElementById("fy").value === "" && withLane.document.getElementById("fp").value === "", "entering the auction section must clear regular-lane filters");
  check(evaluate(withLane, "AUCTION_LANE.length") === 3, "lane rows must be loaded into state");
  evaluate(withLane, "populateFilterOptions(true)");
  const sourceOptions = withLane.document.getElementById("fsrc").innerHTML;
  for (const source of [
    "Zoll-Auktion", "Les Enchères du Domaine", "Justiz-Auktion", "BOE Subastas",
    "Kronofogden Auktionstorget", "Portale delle Vendite Pubbliche", "Fin Shop",
    "Domeinen Roerende Zaken", "e-Leilões", "Licytacje Komornicze", "Nabídka majetku ÚZSVM",
    "VEBEG Federal Surplus",
  ]) {
    check(sourceOptions.includes(source), `${source} must always appear in the auction source filter`);
  }

  // unchecked: regular offers only
  withLane.document.getElementById("fauction").checked = false;
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
  check(firstCard.includes("Auction a-1") && firstCard.includes("https://www.zoll-auktion.de/lot/a-1"),
    "auction card must preserve the canonical title and source URL");

  const scheduledNoReserveCard = evaluate(withLane, `auctionCard({
    id:"copart-demo", source:"copart-es", source_key:"copart-es", registry_key:"copart-es", registry_priority:1,
    url:"https://www.copart.es/lot/12345678", title:"Copart demo", model:"Demo", country:"ES",
    year:2024, mileage:12000, fuel:"petrol", seller:"Copart", price_eur:5300,
    price_currency:"EUR", price_amount:5300, price_kind:"current_bid", price_label:"current bid",
    bid_visibility:"public", eligibility_status:"review_required", eligibility_reason:"review", access_sale_note:"note",
    last_seen_at:${JSON.stringify(nowIso)}, canonical_end_utc:null, sale_event_utc:${JSON.stringify(futureIso)}, no_reserve:true
  })`);
  const scheduledCountdown = evaluate(withLane, `countdownText(Date.parse(${JSON.stringify(futureIso)}))`);
  check(scheduledNoReserveCard.includes("No Reserve") && scheduledNoReserveCard.includes(scheduledCountdown),
    "scheduled Copart session must show its no-reserve state and live days/hours/minutes countdown");

  // The useful auction refinements are opt-in: they must not hide rows unless
  // the visitor actively chooses one. A Copart sale event is also a valid
  // time reference for the date filter and the soonest-first ordering.
  const scheduledFarIso = new Date(Date.now() + 10 * 24 * 3600_000).toISOString();
  const copartScheduled = {
    ...lane(null, 5300, 14, nowIso, "a-copart-session"),
    source: "copart-es", source_key: "copart-es", registry_key: "copart-es",
    registry_priority: 14, url: "https://www.copart.es/lot/12345678", country: "ES",
    canonical_end_utc: null, sale_event_utc: scheduledFarIso, no_reserve: true,
    price_kind: "current_bid", price_amount: 5300, price_eur: 5300, price_currency: "EUR",
  };
  const startingBid = {
    ...lane(futureIso, 4500, 1, nowIso, "a-starting-bid"),
    price_kind: "starting_bid", price_amount: 4500, price_eur: 4500, price_currency: "EUR",
  };
  evaluate(withLane, `AUCTION_SCOPE="eligible"; AUCTION_LANE=[...AUCTION_LANE,${JSON.stringify(copartScheduled)},${JSON.stringify(startingBid)}];`);
  withLane.document.getElementById("fDays").value = "";
  withLane.document.getElementById("fBid").value = "";
  withLane.document.getElementById("fTerms").value = "";
  evaluate(withLane, "apply();");
  check(evaluate(withLane, "VIEW.map(o=>o.id)").includes("a-copart-session") &&
    evaluate(withLane, "VIEW.map(o=>o.id)").includes("a-starting-bid"),
    "optional refinements must default to all rows");
  withLane.document.getElementById("fDays").value = "1";
  evaluate(withLane, "apply();");
  check(!evaluate(withLane, "VIEW.map(o=>o.id)").includes("a-copart-session"),
    "a far scheduled Copart session must obey the selected time window");
  withLane.document.getElementById("fDays").value = "";
  withLane.document.getElementById("fTerms").value = "no_reserve";
  evaluate(withLane, "apply();");
  check(evaluate(withLane, "VIEW.map(o=>o.id)").join(",") === "a-copart-session",
    "no-reserve filter must only act after it is explicitly selected");
  withLane.document.getElementById("fTerms").value = "";
  withLane.document.getElementById("fBid").value = "current_bid";
  evaluate(withLane, "apply();");
  check(!evaluate(withLane, "VIEW.map(o=>o.id)").includes("a-starting-bid") &&
    evaluate(withLane, "VIEW.map(o=>o.id)").includes("a-copart-session"),
    "current-bid filter must remain opt-in and retain declared current bids");
  withLane.document.getElementById("fBid").value = "";

  // broad monitored rows stay visibly distinct from strict eligible rows
  const broadRows = [
    {
      id: "pvp-4620200", source: "pvp-giustizia", source_key: "pvp-giustizia",
      registry_key: "pvp-giustizia", registry_priority: 6,
      url: "https://pvp.giustizia.it/pvp/it/dettaglio_annuncio.page?id=4620200",
      title: "MG ZS 2024", model: "MG ZS", country: "IT", year: 2024,
      mileage_km: 12000, fuel: "petrol", seller: "court sale manager",
      price_amount: 13800, price_currency: "EUR", price_eur: null,
      price_kind: "base_price", price_label: "Prezzo base",
      bid_visibility: "hidden_on_pvp", registration_date: "2024-03-12",
      canonical_end_utc: futureIso, last_seen_at: nowIso,
      eligibility_status: "conditional",
      eligibility_reason: "Current bid and foreign bidder access require review.",
      access_sale_note: "Registration with the sale manager applies.", evidence: "official-pvp",
    },
    {
      id: "boe-hidden", source: "boe-subastas", source_key: "boe-subastas",
      registry_key: "boe-subastas", registry_priority: 4,
      url: "https://subastas.boe.es/detalleSubasta.php?idSub=BOE-SUB-1",
      title: "Vehículo con puja oculta", model: "", country: "ES", year: null,
      mileage: null, fuel: "", seller: "state",
      price_amount: null, price_currency: "EUR", price_eur: null,
      price_kind: "hidden", price_label: "Con puja",
      bid_visibility: "login_required", registration_date: "",
      canonical_end_utc: futureIso, last_seen_at: nowIso,
      eligibility_status: "not_eligible",
      eligibility_reason: "Remote bidder identity requirements are not met.",
      access_sale_note: "DNI/NIE and bank requirements apply.", evidence: "official-boe",
    },
    {
      id: "auto1-adapter", source: "auto1", source_key: "auto1",
      registry_key: "auto1", registry_priority: 13,
      url: "https://www.auto1.com/offer/adapter-lot", title: "Configured AUTO1 feed",
      model: "Configured AUTO1 feed", country: "DE", year: 2025, mileage: 12000,
      fuel: "petrol", seller: "configured feed", price_amount: 12345,
      price_currency: "EUR", price_eur: null, price_kind: "unknown",
      price_label: "configured source feed", bid_visibility: "source feed",
      canonical_end_utc: futureIso, last_seen_at: nowIso,
      eligibility_status: "review_required",
      eligibility_reason: "Configured adapter feed requires offer review.",
      access_sale_note: "Configured source feed.", evidence: "source-adapter:auto1",
      adapter_authorized: true,
    },
  ];
  const broadPayload = {
    ...payload,
    auction_lane: {
      ...payload.auction_lane,
      monitored_schema_version: 1,
      monitored_generated_at_utc: nowIso,
      monitored_count: broadRows.length,
      monitored_source_reports: [],
      monitored_rows: broadRows,
    },
  };
  const broad = makeSandbox({ local });
  bootWith(broad, broadPayload);
  check(evaluate(broad, "AUCTION_SCOPE") === "all", "all official auctions must be the default auction scope");
  check(evaluate(broad, "AUCTION_MONITORED_LANE.length") === rows.length + broadRows.length,
    "all-official scope must merge broad monitoring with strict rows");
  evaluate(broad, "apply();");
  check(evaluate(broad, "VIEW.some(row=>row.id==='pvp-4620200')"), "broad PVP row must be visible in all-official scope");
  check(evaluate(broad, "VIEW.some(row=>row.id==='auto1-adapter')"),
    "configured adapter row must be visible even when its registry source is otherwise blocked");

  // A broad public watch may be newer than data.enc.  It must remain usable
  // after its own registry and rows validate, even when the encrypted payload
  // has no embedded auction lane from that newer dashboard generation.
  const independentWatch = {
    schema_version: 1,
    lane: "official_auction_watch",
    registry_digest: REGISTRY,
    generated_at_utc: nowIso,
    row_count: 1,
    source_reports: {},
    rows: [broadRows[0]],
  };
  const independent = makeSandbox({ local });
  independent.__payload = { ...BASE, auction_lane: null };
  independent.__watch = independentWatch;
  evaluate(independent,
    "const __independentRows=validateOfficialAuctionWatch(__watch,{registry_digest:'older-dashboard'}); boot(__payload,__independentRows);");
  check(evaluate(independent, "AUCTION_LANE !== null && AUCTION_LANE.length===0"),
    "a valid independent watch must enable the auction section without an embedded lane");
  check(evaluate(independent, "VIEW.map(row=>row.id).join(',')") === "pvp-4620200",
    "a newer independently published watch must not disappear from the dashboard");

  broad.__unmarkedAdapter = { ...broadRows.find(row => row.id === "auto1-adapter"), adapter_authorized: false };
  let unmarkedRejected = false;
  try {
    evaluate(broad, "validateMonitoredAuctionRow(__unmarkedAdapter)");
  } catch (error) {
    unmarkedRejected = error && error.message === "contract";
  }
  check(unmarkedRejected, "a blocked source must still require the generated adapter marker");
  const broadCard = evaluate(broad, "auctionCard(VIEW.find(row=>row.id==='pvp-4620200'))");
  check(broadCard.includes("MG ZS 2024") && broadCard.includes("Current bid and foreign bidder access require review.") &&
    !broadCard.includes("المزايدة الحالية الظاهرة"), "base price and conditional eligibility must be labelled honestly");
  check(evaluate(broad, "!VIEW.some(row=>row.id==='boe-hidden')"),
    "unknown-fuel monitored rows must not leak through the accepted petrol/hybrid policy");
  broad.document.getElementById("eligibleAuctionsBtn").dispatchEvent(new Event("click"));
  check(evaluate(broad, "VIEW.every(row=>!['pvp-4620200','boe-hidden'].includes(row.id))"),
    "strict-eligible scope must exclude broad-only monitoring rows");
  broad.document.getElementById("allAuctionsBtn").dispatchEvent(new Event("click"));
  check(evaluate(broad, "AUCTION_SCOPE") === "all" &&
    evaluate(broad, "VIEW.some(row=>row.id==='pvp-4620200')"),
    "all-official scope must restore monitored rows");

  // Fuel, year and price boundaries must match the accepted petrol/hybrid policy.
  const filterRows = [
    { ...lane(futureIso, 9_999, 1, nowIso, "f-petrol-low"), fuel: "Benzine", year: 2020 },
    { ...lane(futureIso, 10_000, 2, nowIso, "f-hybrid-low"), fuel: "PHEV", year: 2021 },
    { ...lane(futureIso, 15_000, 3, nowIso, "f-petrol-high"), fuel: "Essence", year: 2022 },
    { ...lane(futureIso, 5_000, 4, nowIso, "f-diesel-low"), fuel: "Diesel", year: 2020 },
    { ...lane(futureIso, 7_000, 5, nowIso, "f-electric-low"), fuel: "Electric", year: 2020 },
  ];
  const filterPayload = {
    ...BASE,
    auction_lane: { ...payload.auction_lane, lane_count: filterRows.length, rows: filterRows },
  };
  const fuelFilters = makeSandbox({ local });
  bootWith(fuelFilters, filterPayload);
  check(fuelFilters.document.getElementById("ff").value === "petrol_or_hybrid", "auction fuel policy must default to petrol or hybrid");
  check(!/diesel|electric/i.test(fuelFilters.document.getElementById("ff").innerHTML), "auction fuel selector must not offer unaccepted fuels");
  check(evaluate(fuelFilters, "VIEW.map(row=>row.id).join(',')") === "f-petrol-low,f-hybrid-low,f-petrol-high",
    "default auction fuel policy must exclude diesel and electric rows while retaining localized petrol and hybrid labels");

  fuelFilters.document.getElementById("ff").value = "petrol";
  evaluate(fuelFilters, "apply();");
  check(evaluate(fuelFilters, "VIEW.map(row=>row.id).join(',')") === "f-petrol-low,f-petrol-high",
    "petrol-only filter must retain exactly petrol rows");
  fuelFilters.document.getElementById("ff").value = "hybrid";
  evaluate(fuelFilters, "apply();");
  check(evaluate(fuelFilters, "VIEW.map(row=>row.id).join(',')") === "f-hybrid-low",
    "hybrid-only filter must retain exactly hybrid rows");

  fuelFilters.document.getElementById("ff").value = "petrol_or_hybrid";
  fuelFilters.document.getElementById("fy").value = "2021";
  fuelFilters.document.getElementById("fy2").value = "2021";
  evaluate(fuelFilters, "apply();");
  check(evaluate(fuelFilters, "VIEW.map(row=>row.id).join(',')") === "f-hybrid-low",
    "inclusive year range must neither lose nor include adjacent model years");

  fuelFilters.document.getElementById("fy").value = "";
  fuelFilters.document.getElementById("fy2").value = "";
  fuelFilters.document.getElementById("fp").value = "0-10000";
  evaluate(fuelFilters, "apply();");
  check(evaluate(fuelFilters, "VIEW.map(row=>row.id).join(',')") === "f-petrol-low",
    "price range below 10,000 EUR must exclude the 10,000 EUR boundary and every unaccepted fuel");
  fuelFilters.document.getElementById("fp").value = "10000-15000";
  evaluate(fuelFilters, "apply();");
  check(evaluate(fuelFilters, "VIEW.map(row=>row.id).join(',')") === "f-hybrid-low",
    "price range boundaries must be exact and must not include the next range");

  const stalePayload = {
    ...broadPayload,
    auction_lane: { ...broadPayload.auction_lane, monitored_generated_at_utc: "2020-01-01T00:00:00Z" },
  };
  const stale = makeSandbox({ local });
  bootWith(stale, stalePayload);
  check(evaluate(stale, "AUCTION_WATCH_STATE") === "invalid" &&
    evaluate(stale, "AUCTION_MONITORED_LANE.length") === rows.length,
    "stale broad watch must fail safely while strict rows remain available");

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
