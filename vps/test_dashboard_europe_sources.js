#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const INDEX = process.env.INDEX_HTML || path.join(__dirname, "..", "index.html");
const ARTIFACT_PATH =
  process.env.EUROPE_SOURCES_JSON || path.join(__dirname, "..", "europe_car_ads_sources.json");

const html = fs.readFileSync(INDEX, "utf8");
const scriptBody = (html.match(/<script>([\s\S]*?)<\/script>/) || [])[1];
if (!scriptBody) {
  console.error("FATAL: no <script> block found");
  process.exit(2);
}
const artifact = JSON.parse(fs.readFileSync(ARTIFACT_PATH, "utf8"));

let assertions = 0;
function check(condition, message) {
  assertions += 1;
  if (!condition) throw new Error(message);
}

function stubElement() {
  const attributes = new Map();
  return {
    value: "",
    checked: false,
    textContent: "",
    innerHTML: "",
    disabled: false,
    hidden: false,
    style: {},
    dataset: {},
    classList: {
      add() {},
      remove() {},
      toggle() {},
      contains() {
        return false;
      },
    },
    addEventListener() {},
    setAttribute(name, value) {
      attributes.set(name, String(value));
    },
    getAttribute(name) {
      return attributes.has(name) ? attributes.get(name) : null;
    },
    replaceChildren() {
      this.innerHTML = "";
    },
  };
}

function makeSandbox(fetchImpl) {
  const elements = new Map();
  const sandbox = {
    console,
    setTimeout() {},
    clearTimeout() {},
    setInterval() {},
    clearInterval() {},
    addEventListener() {},
    removeEventListener() {},
    requestAnimationFrame() {},
    scrollTo() {},
    location: { reload() {}, href: "https://krt123456.github.io/dz-auto-radar/" },
    TextEncoder,
    TextDecoder,
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
    RegExp,
    isNaN,
    parseFloat,
    parseInt,
    atob: value => Buffer.from(value, "base64").toString("binary"),
    btoa: value => Buffer.from(value, "binary").toString("base64"),
    localStorage: {
      getItem: () => null,
      setItem() {},
      removeItem() {},
    },
    sessionStorage: {
      getItem: () => null,
      setItem() {},
      removeItem() {},
    },
    navigator: { userAgent: "contract-test", language: "en" },
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    fetch: fetchImpl,
    document: {
      hidden: false,
      getElementById(id) {
        if (!elements.has(id)) elements.set(id, stubElement());
        return elements.get(id);
      },
      addEventListener() {},
      querySelector: () => null,
      querySelectorAll: () => [],
      createElement: () => stubElement(),
      body: stubElement(),
      documentElement: stubElement(),
    },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(scriptBody, sandbox, { filename: "dashboard-template.js" });
  return { sandbox, elements };
}

function sourcesFetch(payload) {
  return async url =>
    String(url).includes("europe_car_ads_sources.json")
      ? { ok: true, status: 200, json: async () => payload, text: async () => JSON.stringify(payload) }
      : { ok: false, status: 404 };
}

const settle = () => new Promise(resolve => setTimeout(resolve, 80));

async function main() {
  {
    const { elements } = makeSandbox(sourcesFetch(JSON.parse(JSON.stringify(artifact))));
    await settle();
    const totals = elements.get("euTotals").textContent;
    check(/موقعًا في 52 سوقًا/.test(totals), `totals line must state 52 markets: ${totals}`);
    check(totals.includes(String(artifact.summary.sites)), "totals must carry the site count");
    const grid = elements.get("euGrid").innerHTML;
    check((grid.match(/<details class="eu-country"/g) || []).length === 52, "every market must render");
    const siteRows = (grid.match(/<li class="eu-site"/g) || []).length;
    check(siteRows >= artifact.summary.sites, "every published site must render a row");
    const legend = elements.get("euLegend").innerHTML;
    for (const label of ["موثّق", "محمي بحاجز", "خارج نطاق الرصد"]) {
      check(legend.includes(label), `legend missing status label ${label}`);
    }
    const note = elements.get("euNote").innerHTML;
    check(note.includes("آخر فحص"), "probe timestamp must surface in the note");
    check(artifact.summary.excluded === 0 || note.includes("تعذّر التحقق"), "excluded hosts must be disclosed");
    check(note.includes("فشل النطاق") || artifact.summary.excluded === 0, "exclusion reasons must be shown in Arabic");
  }

  {
    const tampered = JSON.parse(JSON.stringify(artifact));
    const key = Object.keys(tampered.sites).find(k => tampered.sites[k].status === "verified");
    check(!!key, "artifact must publish a verified site to tamper with");
    tampered.sites[key].evidence.http_status = 403;
    const { elements } = makeSandbox(sourcesFetch(tampered));
    await settle();
    check(elements.get("euGrid").innerHTML === "", "tampered artifact must fail closed");
    check(
      elements.get("euTotals").textContent.includes("تعذّر تحميل"),
      "tampered artifact must show the failure line",
    );
  }

  {
    const { elements } = makeSandbox(async () => ({ ok: false, status: 404 }));
    await settle();
    check(elements.get("euGrid").innerHTML === "", "missing artifact must not render");
    check(
      elements.get("euNote").innerHTML.includes("europe_car_ads_sources.json"),
      "failure note must link the raw data file",
    );
  }

  console.log(`DASHBOARD_EUROPE_SOURCES_TEST_PASS assertions=${assertions}`);
}

main().catch(error => {
  console.error(`DASHBOARD_EUROPE_SOURCES_TEST_FAIL assertions=${assertions}: ${error.message}`);
  process.exit(1);
});
