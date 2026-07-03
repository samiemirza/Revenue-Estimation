"""
sensitivity.py — Sensitivity analysis for the daily-revenue model.

Imports estimate() and CONSTANTS from model.py and reruns the estimator with
six perturbed copies of the constants dict. No model formulas live here — this
script only mutates inputs and aggregates outputs.

Run:
    python3 sensitivity.py --clean_dir ./clean_out --out_dir ./sensitivity_out
"""

from __future__ import annotations

import argparse
import copy
import json
import math
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from model import CONSTANTS, estimate


# ----------------------------------------------------------------------------
# Perturbations: each is a function that takes a deep-copied constants dict
# and mutates it in place. Returned dict is the perturbed constants.
# ----------------------------------------------------------------------------
def _scale_daypart(c: dict[str, Any], scale: float) -> dict[str, Any]:
    for k, f in c["daypart_factor"].items():
        c["daypart_factor"][k] = 1.0 + (f - 1.0) * scale
    return c


def _set_top_sku(c: dict[str, Any], v: float) -> dict[str, Any]:
    c["top_sku_share"] = v
    return c


def _set_basket_weights(c: dict[str, Any], w_obs: float, w_ask: float) -> dict[str, Any]:
    c["basket_weight_observed"] = w_obs
    c["basket_weight_asked"] = w_ask
    return c


# (name, mutator, affects_R_base, affects_R_inv)
PERTURBATIONS: list[tuple[str, Callable[[dict], dict], bool, bool]] = [
    ("daypart_compressed",            lambda c: _scale_daypart(c, 0.8),       True,  False),
    ("daypart_steepened",             lambda c: _scale_daypart(c, 1.2),       True,  False),
    ("top_sku_share_0.50",            lambda c: _set_top_sku(c, 0.50),        False, True),
    ("top_sku_share_0.70",            lambda c: _set_top_sku(c, 0.70),        False, True),
    ("basket_weight_owner_heavy",     lambda c: _set_basket_weights(c, 0.4, 0.6), True, False),
    ("basket_weight_observation_heavy", lambda c: _set_basket_weights(c, 0.8, 0.2), True, False),
]


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def _swing_pct(baseline: float | None, perturbed: float | None) -> float | None:
    if baseline is None or perturbed is None:
        return None
    if baseline == 0 or pd.isna(baseline) or pd.isna(perturbed):
        return None
    return (perturbed - baseline) / baseline * 100.0


def _agrees(ratio: float | None, band: tuple[float, float]) -> bool | None:
    if ratio is None or pd.isna(ratio):
        return None
    lo, hi = band
    return bool(lo <= ratio <= hi)


def _check_finite(value: float | None, label: str, visit_id: str) -> None:
    if value is None:
        return
    if math.isinf(value) or math.isnan(value):
        raise ValueError(f"Non-finite {label} for visit {visit_id}: {value}")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main(clean_dir: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    visits = pd.read_csv(clean_dir / "visit_observations.csv")
    n_total = len(visits)

    # Baseline pass
    baseline = [estimate(row, CONSTANTS) for _, row in visits.iterrows()]
    n_baseline_ok = sum(1 for r in baseline if r["revenue_flow_base"] is not None)

    band = CONSTANTS["triangulation_agreement"]

    long_rows: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []
    perturbation_counts: dict[str, int] = {}

    for pname, mutate, affects_base, affects_inv in PERTURBATIONS:
        c_perturbed = mutate(copy.deepcopy(CONSTANTS))
        perturbed = [estimate(row, c_perturbed) for _, row in visits.iterrows()]

        n_computable_base = 0
        n_computable_inv = 0
        base_swings: list[float] = []
        inv_swings: list[float] = []
        n_flips = 0
        max_base_swing = 0.0
        max_base_visit = ""

        for b, p in zip(baseline, perturbed):
            vid = b["visit_id"]

            R_base_b = b["revenue_flow_base"]
            R_base_p = p["revenue_flow_base"]
            R_inv_b = b["revenue_inventory_est"]
            R_inv_p = p["revenue_inventory_est"]
            tri_b = b["triangulation_ratio"]
            tri_p = p["triangulation_ratio"]

            base_swing = _swing_pct(R_base_b, R_base_p) if affects_base else None
            inv_swing = _swing_pct(R_inv_b, R_inv_p) if affects_inv else None

            _check_finite(base_swing, "R_base swing", vid)
            _check_finite(inv_swing, "R_inv swing", vid)

            agree_b = _agrees(tri_b, band)
            agree_p = _agrees(tri_p, band)
            flipped = bool(agree_b is not None and agree_p is not None and agree_b != agree_p)
            if flipped:
                n_flips += 1

            if base_swing is not None:
                n_computable_base += 1
                base_swings.append(base_swing)
                if abs(base_swing) > abs(max_base_swing):
                    max_base_swing = base_swing
                    max_base_visit = vid
            if inv_swing is not None:
                n_computable_inv += 1
                inv_swings.append(inv_swing)

            long_rows.append({
                "visit_id": vid,
                "store_id": b["store_id"],
                "perturbation": pname,
                "R_base_baseline": R_base_b,
                "R_base_perturbed": R_base_p,
                "R_base_swing_pct": base_swing,
                "R_inv_baseline": R_inv_b,
                "R_inv_perturbed": R_inv_p,
                "R_inv_swing_pct": inv_swing,
                "triangulation_ratio_baseline": tri_b,
                "triangulation_ratio_perturbed": tri_p,
                "triangulation_status_flipped": flipped,
            })

        perturbation_counts[pname] = (
            n_computable_base if affects_base else n_computable_inv
        )

        median_abs_base = (
            float(pd.Series([abs(x) for x in base_swings]).median()) if base_swings else None
        )
        max_abs_base = max((abs(x) for x in base_swings), default=None)
        median_abs_inv = (
            float(pd.Series([abs(x) for x in inv_swings]).median()) if inv_swings else None
        )
        max_abs_inv = max((abs(x) for x in inv_swings), default=None)

        summary_rows.append({
            "perturbation": pname,
            "n_visits": n_total,
            "median_abs_R_base_swing_pct": median_abs_base,
            "max_abs_R_base_swing_pct": max_abs_base,
            "max_swing_visit_id": max_base_visit if affects_base else "",
            "median_abs_R_inv_swing_pct": median_abs_inv,
            "max_abs_R_inv_swing_pct": max_abs_inv,
            "n_triangulation_flips": n_flips,
        })

    long_df = pd.DataFrame(long_rows)
    summary_df = pd.DataFrame(summary_rows)
    long_df.to_csv(out_dir / "per_visit.csv", index=False)
    summary_df.to_csv(out_dir / "summary.csv", index=False)

    # ------------------------------------------------------------------
    # Stdout report
    # ------------------------------------------------------------------
    print(f"Baseline computed for {n_baseline_ok}/{n_total} visits")
    print("\nVisits with computable swings per perturbation:")
    for pname, _, affects_base, affects_inv in PERTURBATIONS:
        which = "R_base" if affects_base else "R_inv"
        print(f"  {pname:35s}  {perturbation_counts[pname]}/{n_total}  ({which})")

    # Note about default_by_type visits + weight perturbations
    default_visits = [b["visit_id"] for b in baseline if b["basket_source"] == "default_by_type"]
    if default_visits:
        print(
            f"\nNote: {len(default_visits)} visit(s) used basket_source=default_by_type "
            f"({', '.join(default_visits)}). Basket-weight perturbations have 0% effect on these by construction."
        )

    print()
    header = (
        f"{'Perturbation':35s} | {'Median |ΔR_base|':16s} | {'Max |ΔR_base|':28s} | "
        f"{'Median |ΔR_inv|':15s} | Triangulation flips"
    )
    print(header)
    print("-" * len(header))

    def fmt_pct(x: float | None) -> str:
        return "—" if x is None else f"{x:6.1f}%"

    for row in summary_rows:
        max_b = row["max_abs_R_base_swing_pct"]
        max_b_str = "—" if max_b is None else f"{max_b:5.1f}% ({row['max_swing_visit_id']})"
        print(
            f"{row['perturbation']:35s} | "
            f"{fmt_pct(row['median_abs_R_base_swing_pct']):>16s} | "
            f"{max_b_str:28s} | "
            f"{fmt_pct(row['median_abs_R_inv_swing_pct']):>15s} | "
            f"{row['n_triangulation_flips']:>6d}"
        )

    print(f"\nWrote {len(long_df)} rows to {out_dir / 'per_visit.csv'}")
    print(f"Wrote {len(summary_df)} rows to {out_dir / 'summary.csv'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean_dir", type=Path, default=Path("./clean_out"))
    ap.add_argument("--out_dir", type=Path, default=Path("./sensitivity_out"))
    args = ap.parse_args()
    main(args.clean_dir, args.out_dir)
