#!/usr/bin/env node
// verify.js — runs the JS estimator (copied verbatim from the HTML) against
// model_results_per_visit.csv to confirm parity.

const fs = require("fs");
const path = require("path");

// --- BEGIN: copy of estimate() from revenue_estimator.html (no DOM deps) ---
const CONSTANTS = {
  daypart_factor: {
    Morning: 1.0, Midday: 0.9, Afternoon: 0.7,
    Evening: 1.4, Night: 0.7, Late: 0.7,
  },
  txn_scenario_swing: 0.25,
  basket_scenario_swing: 0.20,
  basket_weight_observed: 0.6,
  basket_weight_asked: 0.4,
  top_sku_share: 0.60,
  default_basket_by_type: {
    "pan shop": 80.0,
    "kiryana": 200.0,
    "mini mart": 350.0,
    "mart": 350.0,
  },
  sanity: {
    daily_txn_min: 20, daily_txn_max: 2000,
    basket_min: 20, basket_max: 1500,
    revenue_min: 1000, revenue_max: 500000,
  },
  triangulation_agreement: [0.7, 1.4],
};

function estimate(visit, C) {
  C = C || CONSTANTS;
  const flags = [];
  const txn_n = visit.transactions_n;
  const window_min = visit.obs_window_minutes;
  let rate_per_hr = null;
  if (txn_n == null || window_min == null || window_min <= 0) {
    flags.push("no_observation_window");
  } else {
    rate_per_hr = Number(txn_n) * (60.0 / Number(window_min));
  }
  const daypart = visit.obs_daypart;
  const dpf = C.daypart_factor;
  let f;
  if (daypart in dpf) f = dpf[daypart];
  else { f = 1.0; flags.push("unknown_daypart=" + daypart); }
  const daily_avg_rate = (rate_per_hr != null) ? rate_per_hr / f : null;

  let op_hours = visit.operating_hours;
  if (op_hours == null || op_hours <= 0) { flags.push("missing_operating_hours"); op_hours = 12.0; }
  if (op_hours > 20) flags.push("operating_hours_very_long=" + Number(op_hours).toFixed(1));

  let daily_txn;
  if (daily_avg_rate != null) daily_txn = daily_avg_rate * Number(op_hours);
  else {
    const cust_lo = visit.customers_per_day_est_low;
    const cust_hi = visit.customers_per_day_est_high;
    if (cust_lo != null && cust_hi != null) {
      daily_txn = ((cust_lo + cust_hi) / 2) * 0.75;
      flags.push("daily_txn_from_owner_estimate");
    } else { daily_txn = null; flags.push("daily_txn_unavailable"); }
  }
  const sn = C.sanity;
  if (daily_txn != null && !(sn.daily_txn_min <= daily_txn && daily_txn <= sn.daily_txn_max))
    flags.push("daily_txn_out_of_bounds=" + Math.round(daily_txn));

  const baskets = (visit.basket_values_obs || []).filter(x => x != null && !isNaN(x)).map(Number);
  const B_obs = (baskets.length >= 3) ? (baskets.reduce((a,b)=>a+b, 0) / baskets.length) : null;

  const bill_lo = visit.bill_range_low;
  const bill_hi = visit.bill_range_high;
  let B_asked = null;
  if (bill_lo != null && bill_hi != null) B_asked = (Number(bill_lo) + Number(bill_hi)) / 2;

  const w_obs = C.basket_weight_observed;
  const w_ask = C.basket_weight_asked;
  let B_avg, basket_source;
  if (B_obs != null && B_asked != null) { B_avg = w_obs * B_obs + w_ask * B_asked; basket_source = "blended"; }
  else if (B_obs != null) { B_avg = B_obs; basket_source = "observed_only"; flags.push("basket_observed_only"); }
  else if (B_asked != null) { B_avg = B_asked; basket_source = "asked_only"; flags.push("basket_asked_only"); }
  else {
    const st = (visit.store_type || "").toLowerCase();
    const def = C.default_basket_by_type;
    B_avg = (st in def) ? def[st] : 200.0;
    basket_source = "default_by_type";
    flags.push("basket_default_used_type=" + st);
  }
  if (!(sn.basket_min <= B_avg && B_avg <= sn.basket_max)) flags.push("basket_out_of_bounds=" + Math.round(B_avg));

  const R_base = (daily_txn != null) ? daily_txn * B_avg : null;
  const t_swing = C.txn_scenario_swing, b_swing = C.basket_scenario_swing;
  const daily_txn_low  = (daily_txn != null) ? daily_txn * (1 - t_swing) : null;
  const daily_txn_high = (daily_txn != null) ? daily_txn * (1 + t_swing) : null;

  let B_low, B_high;
  if (B_obs != null && B_asked != null && bill_lo != null && bill_hi != null) {
    B_low = Math.min(B_obs, Number(bill_lo));
    B_high = Math.max(B_obs, Number(bill_hi));
  } else {
    B_low = B_avg * (1 - b_swing);
    B_high = B_avg * (1 + b_swing);
  }
  const R_low  = (R_base != null) ? daily_txn_low  * B_low  : null;
  const R_high = (R_base != null) ? daily_txn_high * B_high : null;

  let R_top = 0.0, items_used = 0;
  for (let i = 1; i <= 3; i++) {
    const fr = visit["restock_freq_item_" + i];
    const qt = visit["qty_per_restock_item_" + i];
    const pr = visit["price_item_" + i];
    if (fr != null && qt != null && pr != null) {
      R_top += Number(fr) * Number(qt) * Number(pr);
      items_used += 1;
    }
  }
  const R_inv = (items_used >= 2) ? R_top / C.top_sku_share : null;

  let ratio = null;
  if (R_base != null && R_inv != null && R_base > 0) {
    ratio = R_inv / R_base;
    const [lo, hi] = C.triangulation_agreement;
    if (!(lo <= ratio && ratio <= hi)) flags.push("triangulation_disagree_ratio=" + ratio.toFixed(2));
  }

  return {
    daily_txn_est: daily_txn,
    avg_basket_est: B_avg,
    revenue_flow_low: R_low,
    revenue_flow_base: R_base,
    revenue_flow_high: R_high,
    revenue_inventory_est: R_inv,
    triangulation_ratio: ratio,
    flags: flags,
  };
}
// --- END copy ---

// Minimal CSV parser that handles quoted fields with commas
function parseCSV(text) {
  const rows = [];
  let row = [], cell = "", inQ = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (inQ) {
      if (c === '"') {
        if (text[i + 1] === '"') { cell += '"'; i++; }
        else inQ = false;
      } else cell += c;
    } else {
      if (c === '"') inQ = true;
      else if (c === ",") { row.push(cell); cell = ""; }
      else if (c === "\n") { row.push(cell); rows.push(row); row = []; cell = ""; }
      else if (c === "\r") { /* skip */ }
      else cell += c;
    }
  }
  if (cell.length || row.length) { row.push(cell); rows.push(row); }
  return rows;
}

function loadCSV(p) {
  const txt = fs.readFileSync(p, "utf8");
  const rows = parseCSV(txt).filter(r => r.length > 1 && r.some(c => c !== ""));
  const headers = rows[0];
  return rows.slice(1).map(r => {
    const o = {};
    headers.forEach((h, i) => o[h] = r[i] === undefined ? "" : r[i]);
    return o;
  });
}

const num = v => (v === "" || v === null || v === undefined) ? null : Number(v);

function visitFromCleanedRow(r) {
  // basket_values_obs_json is a JSON list like "[1000.0, 550.0, ...]"
  let baskets = [];
  try { baskets = JSON.parse(r.basket_values_obs_json || "[]"); } catch (_) { baskets = []; }
  return {
    visit_id: r.visit_id,
    store_id: r.store_id,
    store_type: r.store_type, // not in cleaned row but model doesn't need it when basket_source = blended
    obs_window_minutes: num(r.obs_window_minutes),
    obs_daypart: r.obs_daypart,
    transactions_n: num(r.transactions_n),
    operating_hours: num(r.operating_hours),
    basket_values_obs: baskets,
    bill_range_low: num(r.bill_range_low),
    bill_range_high: num(r.bill_range_high),
    customers_per_day_est_low: num(r.customers_per_day_est_low),
    customers_per_day_est_high: num(r.customers_per_day_est_high),
    restock_freq_item_1: num(r.restock_freq_item_1),
    qty_per_restock_item_1: num(r.qty_per_restock_item_1),
    price_item_1: num(r.price_item_1),
    restock_freq_item_2: num(r.restock_freq_item_2),
    qty_per_restock_item_2: num(r.qty_per_restock_item_2),
    price_item_2: num(r.price_item_2),
    restock_freq_item_3: num(r.restock_freq_item_3),
    qty_per_restock_item_3: num(r.qty_per_restock_item_3),
    price_item_3: num(r.price_item_3),
  };
}

const visits = loadCSV(path.join(__dirname, "clean_out", "visit_observations.csv"));
const expected = loadCSV(path.join(__dirname, "clean_out", "model_results_per_visit.csv"));
const expBy = Object.fromEntries(expected.map(r => [r.visit_id, r]));

const TARGETS = [
  "MART03_4242026_60000PM",
  "KIR02_4232026_80000PM",
  "KIR03_4222026_30000PM",
  "PAN02_4242026_93000AM",
  "MART01_4222026_60000PM",
];

const COLS = ["revenue_flow_base", "revenue_flow_low", "revenue_flow_high", "revenue_inventory_est"];

function pad(s, n) { s = String(s); return s + " ".repeat(Math.max(0, n - s.length)); }
function rpad(s, n) { s = String(s); return " ".repeat(Math.max(0, n - s.length)) + s; }

console.log("\n=== JS estimate() vs model_results_per_visit.csv ===\n");
console.log(pad("visit_id", 30) + pad("metric", 22) + rpad("expected", 14) + rpad("actual", 14) + rpad("Δ", 12) + "  ok");
console.log("-".repeat(94));

let allOk = true;
for (const vid of TARGETS) {
  const cleaned = visits.find(v => v.visit_id === vid);
  if (!cleaned) { console.log(`MISSING cleaned visit: ${vid}`); allOk = false; continue; }
  const exp = expBy[vid];
  const v = visitFromCleanedRow(cleaned);
  const got = estimate(v);
  for (const c of COLS) {
    const e = Number(exp[c]);
    const g = got[c];
    const delta = g - e;
    const ok = Math.abs(delta) <= 1.0;
    if (!ok) allOk = false;
    console.log(
      pad(vid, 30) + pad(c, 22) +
      rpad(e.toFixed(2), 14) + rpad((g == null ? "null" : g.toFixed(2)), 14) +
      rpad(delta.toFixed(4), 12) + "  " + (ok ? "✓" : "✗")
    );
  }
  console.log("");
}
console.log(allOk ? "ALL ROWS WITHIN ±1 PKR ✓" : "SOME ROWS FAILED ✗");
process.exit(allOk ? 0 : 1);
