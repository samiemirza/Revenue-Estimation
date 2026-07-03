"""
model.py — Pure estimator for daily revenue.

One function, `estimate(visit, constants) -> dict`. Takes a single cleaned visit
row (a dict or pandas Series) and returns the flow model's low/base/high scenarios
plus the inventory cross-check and a triangulation ratio.

Follows Step 5 of the plan:
  R_base = daily_txn * avg_basket
    daily_txn = (txn_in_window / window_frac_of_day) / daypart_factor
    avg_basket = 0.6 * B_observed + 0.4 * midpoint(bill_range)  (blend if both)

Inventory cross-check:
  R_inv = sum(restock_freq * qty * price) / top_sku_share

Constants live in CONSTANTS at the top. All sensitivity perturbations go through
this dict. Nothing in the estimator is magical.

Run:
    python3 model.py --clean_dir ./clean_out
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd


# ----------------------------------------------------------------------------
# Constants — all model assumptions live here. Sensitivity analysis perturbs this.
# ----------------------------------------------------------------------------
CONSTANTS: dict[str, Any] = {
    # Daypart factor: how busy is THIS window vs daily average?
    # >1.0 = window is busier than avg (so observed rate over-represents the day)
    # <1.0 = window is quieter (observed rate under-represents the day)
    # Daily avg rate = observed window rate / daypart_factor
    "daypart_factor": {
        "Morning": 1.0,     # 6-10 AM: steady
        "Midday": 0.9,      # 10-14: slight dip
        "Afternoon": 0.7,   # 14-17: quiet
        "Evening": 1.4,     # 17-21: peak
        "Night": 0.7,       # 21+: slow
        "Late": 0.7,
    },
    # How the flow model's low/high scenarios are bounded relative to base
    "txn_scenario_swing": 0.25,   # ±25% on T
    "basket_scenario_swing": 0.20,  # ±20% on B when one source missing
    # Weights when both observed and asked basket values exist
    "basket_weight_observed": 0.6,
    "basket_weight_asked": 0.4,
    # Inventory cross-check: top 3 SKUs cover this fraction of revenue
    "top_sku_share": 0.60,
    # Defaults for fallbacks
    "default_basket_by_type": {
        "pan shop": 80.0,
        "kiryana": 200.0,
        "mini mart": 350.0,
        "mart": 350.0,
    },
    # Sanity bounds — results outside these get flagged, not rejected
    "sanity": {
        "daily_txn_min": 20, "daily_txn_max": 2000,
        "basket_min": 20, "basket_max": 1500,
        "revenue_min": 1000, "revenue_max": 500_000,
    },
    # Triangulation agreement band
    "triangulation_agreement": (0.7, 1.4),
}


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def _get(visit: Any, key: str, default: Any = None) -> Any:
    """Works with dict or pandas Series."""
    try:
        v = visit.get(key, default) if hasattr(visit, "get") else visit[key]
    except (KeyError, IndexError):
        return default
    if pd.isna(v) if not isinstance(v, (list, dict, str)) else False:
        return default
    return v


def _parse_baskets(v: Any) -> list[float]:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return []
    if isinstance(v, list):
        return [float(x) for x in v]
    if isinstance(v, str) and v.strip():
        try:
            return [float(x) for x in json.loads(v)]
        except (json.JSONDecodeError, ValueError):
            return []
    return []


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


# ----------------------------------------------------------------------------
# Core estimator
# ----------------------------------------------------------------------------
def estimate(visit: Any, constants: dict[str, Any] = CONSTANTS) -> dict[str, Any]:
    """Estimate daily revenue for one visit.

    Returns a dict with model outputs and a `flags` list of warnings (not errors).
    Missing critical inputs cause graceful degradation with flags, not exceptions.
    """
    flags: list[str] = []

    # --- 1. Transaction rate in window ---
    txn_n = _get(visit, "transactions_n")
    window_min = _get(visit, "obs_window_minutes")

    if txn_n is None or window_min is None or window_min <= 0:
        flags.append("no_observation_window")
        rate_per_hr = None
    else:
        rate_per_hr = float(txn_n) * (60.0 / float(window_min))

    # --- 2. Daypart adjustment to daily average rate ---
    daypart = _get(visit, "obs_daypart")
    daypart_factors = constants["daypart_factor"]
    if daypart in daypart_factors:
        f = daypart_factors[daypart]
    else:
        f = 1.0
        flags.append(f"unknown_daypart={daypart}")

    if rate_per_hr is not None:
        # window rate / f = daily-average rate
        daily_avg_rate = rate_per_hr / f
    else:
        daily_avg_rate = None

    # --- 3. Operating hours ---
    op_hours = _get(visit, "operating_hours")
    if op_hours is None or op_hours <= 0:
        flags.append("missing_operating_hours")
        # Fall back to a generous 12
        op_hours = 12.0
    if op_hours > 20:
        flags.append(f"operating_hours_very_long={op_hours:.1f}")

    # --- 4. Daily transactions ---
    if daily_avg_rate is not None:
        daily_txn = daily_avg_rate * float(op_hours)
    else:
        # Fallback: owner's customer-per-day estimate, haircut for overclaim
        cust_lo = _get(visit, "customers_per_day_est_low")
        cust_hi = _get(visit, "customers_per_day_est_high")
        if cust_lo is not None and cust_hi is not None:
            daily_txn = ((cust_lo + cust_hi) / 2) * 0.75
            flags.append("daily_txn_from_owner_estimate")
        else:
            daily_txn = None
            flags.append("daily_txn_unavailable")

    # Sanity bound
    sn = constants["sanity"]
    if daily_txn is not None and not (sn["daily_txn_min"] <= daily_txn <= sn["daily_txn_max"]):
        flags.append(f"daily_txn_out_of_bounds={daily_txn:.0f}")

    # --- 5. Basket size ---
    baskets = _parse_baskets(_get(visit, "basket_values_obs_json"))
    B_obs = _mean(baskets) if len(baskets) >= 3 else None

    bill_lo = _get(visit, "bill_range_low")
    bill_hi = _get(visit, "bill_range_high")
    B_asked = None
    if bill_lo is not None and bill_hi is not None:
        B_asked = (float(bill_lo) + float(bill_hi)) / 2

    w_obs = constants["basket_weight_observed"]
    w_ask = constants["basket_weight_asked"]

    if B_obs is not None and B_asked is not None:
        B_avg = w_obs * B_obs + w_ask * B_asked
        basket_source = "blended"
    elif B_obs is not None:
        B_avg = B_obs
        basket_source = "observed_only"
        flags.append("basket_observed_only")
    elif B_asked is not None:
        B_avg = B_asked
        basket_source = "asked_only"
        flags.append("basket_asked_only")
    else:
        # Default by store type
        store_type = (_get(visit, "store_type") or "").lower()
        default_b = constants["default_basket_by_type"]
        B_avg = default_b.get(store_type, 200.0)
        basket_source = "default_by_type"
        flags.append(f"basket_default_used_type={store_type}")

    if not (sn["basket_min"] <= B_avg <= sn["basket_max"]):
        flags.append(f"basket_out_of_bounds={B_avg:.0f}")

    # --- 6. Base revenue (flow model) ---
    if daily_txn is not None:
        R_base = daily_txn * B_avg
    else:
        R_base = None

    # --- 7. Scenario band ---
    t_swing = constants["txn_scenario_swing"]
    b_swing = constants["basket_scenario_swing"]

    if daily_txn is not None:
        daily_txn_low = daily_txn * (1 - t_swing)
        daily_txn_high = daily_txn * (1 + t_swing)
    else:
        daily_txn_low = daily_txn_high = None

    # Basket bounds: use observed + asked extremes when available
    if B_obs is not None and B_asked is not None and bill_lo is not None and bill_hi is not None:
        B_low = min(B_obs, float(bill_lo))
        B_high = max(B_obs, float(bill_hi))
    else:
        B_low = B_avg * (1 - b_swing)
        B_high = B_avg * (1 + b_swing)

    if R_base is not None:
        R_low = daily_txn_low * B_low
        R_high = daily_txn_high * B_high
    else:
        R_low = R_high = None

    if R_base is not None and not (sn["revenue_min"] <= R_base <= sn["revenue_max"]):
        flags.append(f"revenue_out_of_bounds={R_base:.0f}")

    # --- 8. Inventory cross-check ---
    R_top = 0.0
    items_used = 0
    for i in (1, 2, 3):
        freq = _get(visit, f"restock_freq_item_{i}")
        qty = _get(visit, f"qty_per_restock_item_{i}")
        price = _get(visit, f"price_item_{i}")
        if freq is not None and qty is not None and price is not None:
            try:
                R_top += float(freq) * float(qty) * float(price)
                items_used += 1
            except (ValueError, TypeError):
                pass

    if items_used >= 2:
        R_inv = R_top / constants["top_sku_share"]
    else:
        R_inv = None
        flags.append(f"inventory_model_skipped_items_used={items_used}")

    # --- 9. Triangulation ---
    if R_base is not None and R_inv is not None and R_base > 0:
        ratio = R_inv / R_base
        lo, hi = constants["triangulation_agreement"]
        if not (lo <= ratio <= hi):
            flags.append(f"triangulation_disagree_ratio={ratio:.2f}")
    else:
        ratio = None

    # --- 10. Owner band coverage ---
    owner_lo = _get(visit, "revenue_band_low_pkr")
    owner_hi = _get(visit, "revenue_band_high_pkr")
    if R_base is not None and owner_lo is not None and owner_hi is not None:
        owner_contains_base = bool(owner_lo <= R_base <= owner_hi)
    else:
        owner_contains_base = None

    return {
        "visit_id": _get(visit, "visit_id"),
        "store_id": _get(visit, "store_id"),
        # Intermediate
        "rate_per_hr_window": rate_per_hr,
        "daypart_factor_used": f,
        "operating_hours_used": op_hours,
        "daily_txn_est": daily_txn,
        "basket_source": basket_source,
        "B_obs": B_obs,
        "B_asked": B_asked,
        "avg_basket_est": B_avg,
        # Flow outputs
        "revenue_flow_low": R_low,
        "revenue_flow_base": R_base,
        "revenue_flow_high": R_high,
        # Inventory + triangulation
        "revenue_inventory_est": R_inv,
        "inventory_items_used": items_used,
        "triangulation_ratio": ratio,
        # Owner
        "owner_band_low": owner_lo,
        "owner_band_high": owner_hi,
        "owner_band_contains_base": owner_contains_base,
        # QC
        "confidence_score": _get(visit, "confidence_score"),
        "flags": ";".join(flags),
    }


# ----------------------------------------------------------------------------
# Store-level aggregation (multiple visits per store get averaged)
# ----------------------------------------------------------------------------
def aggregate_store_level(visit_results: pd.DataFrame) -> pd.DataFrame:
    """Collapse multiple visits into one store-level row.

    For stores with multiple visits, the daily-revenue estimates should agree
    after daypart correction. We take the mean of R_base/R_low/R_high and
    record the per-visit spread so V3 (two-window consistency) can use it.
    """
    agg_cols = [
        "daily_txn_est", "avg_basket_est",
        "revenue_flow_low", "revenue_flow_base", "revenue_flow_high",
        "revenue_inventory_est", "triangulation_ratio",
        "owner_band_low", "owner_band_high",
    ]
    rows = []
    for store_id, g in visit_results.groupby("store_id"):
        row: dict[str, Any] = {"store_id": store_id, "n_visits": len(g)}
        for c in agg_cols:
            row[c] = g[c].mean(skipna=True)
        # two-window gap as a % of mean (V3 input)
        if len(g) >= 2 and g["revenue_flow_base"].notna().sum() >= 2:
            vals = g["revenue_flow_base"].dropna()
            row["two_window_gap_pct"] = (vals.max() - vals.min()) / vals.mean() * 100
        else:
            row["two_window_gap_pct"] = None
        # any visit flagged?
        all_flags = ";".join(f for f in g["flags"] if f)
        row["flags"] = all_flags
        row["owner_band_contains_base"] = None
        if pd.notna(row["revenue_flow_base"]) and pd.notna(row["owner_band_low"]) and pd.notna(row["owner_band_high"]):
            row["owner_band_contains_base"] = row["owner_band_low"] <= row["revenue_flow_base"] <= row["owner_band_high"]
        rows.append(row)
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------
# Runner
# ----------------------------------------------------------------------------
def main(clean_dir: Path) -> None:
    visits = pd.read_csv(clean_dir / "visit_observations.csv")
    results = pd.DataFrame([estimate(row) for _, row in visits.iterrows()])
    results.to_csv(clean_dir / "model_results_per_visit.csv", index=False)
    store_results = aggregate_store_level(results)
    store_results.to_csv(clean_dir / "model_results.csv", index=False)

    print(f"Estimated {len(results)} visits -> {len(store_results)} stores\n")
    cols = ["store_id", "n_visits", "daily_txn_est", "avg_basket_est",
            "revenue_flow_low", "revenue_flow_base", "revenue_flow_high",
            "revenue_inventory_est", "triangulation_ratio",
            "owner_band_contains_base", "two_window_gap_pct"]
    with pd.option_context("display.max_columns", None, "display.width", 200,
                            "display.float_format", "{:,.0f}".format):
        print(store_results[cols].to_string(index=False))

    flagged = results[results["flags"] != ""]
    if len(flagged):
        print(f"\n{len(flagged)} visits have flags:")
        for _, r in flagged.iterrows():
            print(f"  {r['visit_id']}: {r['flags']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean_dir", type=Path, default=Path("./clean_out"))
    args = ap.parse_args()
    main(args.clean_dir)
