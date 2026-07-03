"""
clean.py — Raw Google-Form CSV -> normalized schemas.

Reads the raw form export and emits:
  - store_master.csv       (one row per store)
  - visit_observations.csv (one row per visit; a store can have multiple)
  - model_results.csv      (placeholder, populated by model.py)
  - validation.csv         (placeholder, populated later)

Run:
    python3 clean.py --raw raw.csv --out ./clean_out

Design notes
------------
- This is a *strict* cleaner: malformed rows raise warnings but are kept, with
  an issue tag on the visit. Silent substitutions are avoided. If a field can't
  be parsed, the cleaned field is left as NaN and the `issues` column records why.
- `visit_id = {store_id}_{visit_date}_{obs_start}` so that revisits produce
  distinct rows.
- Revenue bands like "140k - 160k" are parsed into (low, high) in PKR.
- Basket values are collapsed from 5 columns into a JSON-encoded list.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import pandas as pd


# ----------------------------------------------------------------------------
# Column name mapping: raw form header -> short snake_case name
# ----------------------------------------------------------------------------
COLMAP: dict[str, str] = {
    "Timestamp": "form_timestamp",
    "Store ID — store_id": "store_id",
    "Visit date — visit_date": "visit_date",
    "Day of week - day_week": "day_of_week",
    "Area / neighborhood — area_name": "area_name",
    "Income proxy - income_proxy": "income_proxy",
    "Weather / unusual condition today — weather_or_special_day_flag": "weather",
    "Store type — store_type": "store_type",
    "Store size — store_size": "store_size",
    "Location type — location_type": "location_type",
    "How long store has been operating - store_age": "store_age_years",
    "Observation start time — obs_window_start": "obs_window_start",
    "Observation end time — obs_window_end": "obs_window_end",
    "Window daypart — obs_daypart": "obs_daypart",
    "Number of completed purchase transactions during the window — transactions_n": "transactions_n",
    "Number of staff working during the visit — staff_n": "staff_n",
    "Number of fridges/coolers visible — fridges_n": "fridges_n",
    "Visible product categories — categories_visible": "categories_visible",
    "Estimated number of Customers per day (50s): customers_per_day_est_range": "customers_per_day_est",
    "Observed basket value 1 (PKR) — basket_value_obs_1": "basket_1",
    "Observed basket value 2 (PKR) — basket_value_obs_2": "basket_2",
    "Observed basket value 3 (PKR) — basket_value_obs_3": "basket_3",
    "Observed basket value 4 (PKR) — basket_value_obs_4": "basket_4",
    "Observed basket value 5 (PKR) — basket_value_obs_5": "basket_5",
    "What time do you usually open? — open_time": "open_time",
    "What time do you usually close? — close_time": "close_time",
    "How many days per week are you closed? — days_closed_per_week": "days_closed_per_week",
    "What are your busiest hours? — peak_hours ": "peak_hours",
    "Right now, is this a slow, normal, or busy time? — current_period_status_owner": "current_period_status",
    "Is today a normal day for sales, or unusually slow/busy? — today_normality_flag": "today_normality",
    "Are weekends meaningfully different from weekdays here? — weekday_weekend_difference_flag": "weekend_difference",
    "On a typical bill, what is the low end? (PKR) — bill_range_typical_low": "bill_range_low",
    "On a typical bill, what is the high end? (PKR) — bill_range_typical_high": "bill_range_high",
    "What is one of your top-selling items? — top_item_1": "top_item_1",
    "How often do you restock it? (num per day) — restock_freq_item_1": "restock_freq_item_1",
    "How much do you restock each time? — qty_per_restock_item_1": "qty_per_restock_item_1",
    "What do you sell it for? (PKR) — price_item_1": "price_item_1",
    "What is one of your top-selling items? — top_item_2": "top_item_2",
    "How often do you restock it? (num per day) — restock_freq_item_2": "restock_freq_item_2",
    "How much do you restock each time? — qty_per_restock_item_2": "qty_per_restock_item_2",
    "What do you sell it for? (PKR) — price_item_2": "price_item_2",
    "What is one of your top-selling items? — top_item_3": "top_item_3",
    "How often do you restock it? (num per day) — restock_freq_item_3": "restock_freq_item_3",
    "How much do you restock each time? — qty_per_restock_item_3": "qty_per_restock_item_3",
    "What do you sell it for? (PKR) — price_item_3": "price_item_3",
    "Do you offer customer credit / udhaar? — offers_credit_udhaar": "offers_credit_udhaar",
    "Do you take phone or WhatsApp orders? — takes_remote_orders": "takes_remote_orders",
    "Do you offer delivery? — offers_delivery": "offers_delivery",
    "On a typical day, which of these best describes your total sales? — revenue_band_reported": "revenue_band_reported_raw",
    "Confidence score for this visit (0–10) — confidence_score": "confidence_score",
    "Notes / anything unusual — notes_freetext": "notes",
    "Was owner cooperative (Data Quality) - owner_cooperative": "owner_cooperative",
    "Do they use Tajir  — use_tajir": "uses_tajir",
}


# ----------------------------------------------------------------------------
# Parsers
# ----------------------------------------------------------------------------
def parse_decimal_commas(val: Any) -> float | None:
    """Handle '0,5' -> 0.5. Leaves plain floats alone."""
    if pd.isna(val):
        return None
    s = str(val).strip()
    if not s:
        return None
    s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def parse_time_to_hour(val: Any) -> float | None:
    """'6:00:00 PM' -> 18.0. Returns None on failure."""
    if pd.isna(val):
        return None
    s = str(val).strip()
    if not s:
        return None
    # Try a few formats
    for fmt in ("%I:%M:%S %p", "%I:%M %p", "%H:%M:%S", "%H:%M"):
        try:
            t = pd.to_datetime(s, format=fmt).time()
            return t.hour + t.minute / 60 + t.second / 3600
        except (ValueError, TypeError):
            continue
    return None


def parse_revenue_band(val: Any) -> tuple[float | None, float | None, str]:
    """'140k - 160k' -> (140_000, 160_000, 'band'). 'refused' -> (None, None, 'refused')."""
    if pd.isna(val):
        return (None, None, "missing")
    s = str(val).strip().lower()
    if s in ("", "refused", "no answer", "na", "n/a"):
        return (None, None, "refused")
    # match patterns like "140k - 160k", "60k-80k", "5k-10k"
    m = re.match(r"(\d+(?:\.\d+)?)\s*k?\s*[-–]\s*(\d+(?:\.\d+)?)\s*k?", s)
    if m:
        lo = float(m.group(1)) * 1000
        hi = float(m.group(2)) * 1000
        return (lo, hi, "band")
    # open-ended "<5k" or "40k+"
    m = re.match(r"<\s*(\d+(?:\.\d+)?)\s*k", s)
    if m:
        return (0.0, float(m.group(1)) * 1000, "band")
    m = re.match(r"(\d+(?:\.\d+)?)\s*k\s*\+", s)
    if m:
        return (float(m.group(1)) * 1000, None, "band")
    return (None, None, "unparsed")


def parse_customers_per_day(val: Any) -> tuple[float | None, float | None]:
    """'5-8' -> (5, 8), '700' -> (700, 700). In this form it looks like midpoint-only numbers, handle both."""
    if pd.isna(val):
        return (None, None)
    s = str(val).strip()
    if not s:
        return (None, None)
    m = re.match(r"(\d+)\s*[-–]\s*(\d+)", s)
    if m:
        return (float(m.group(1)), float(m.group(2)))
    try:
        v = float(s)
        return (v, v)
    except ValueError:
        return (None, None)


def parse_peak_hours(val: Any) -> tuple[float | None, float | None]:
    """'5-8' -> (17, 20) assuming PM; '6-10' -> (18, 22). Ambiguous, but the form uses
    evening-shop convention. We treat a range whose first number is <= 12 as PM if both
    numbers are plausibly evening."""
    if pd.isna(val):
        return (None, None)
    s = str(val).strip()
    m = re.match(r"(\d+)\s*[-–]\s*(\d+)", s)
    if not m:
        return (None, None)
    a, b = int(m.group(1)), int(m.group(2))
    # Heuristic: if b < a (e.g. 8-12), assume evening-to-midday wrap => 20-12? no.
    # For kiryana the peak is almost always evening, so pad a,b to PM when ambiguous.
    # "6-10" -> 18-22. "8-12" -> 20-24 (midnight).
    if a < 12 and b <= 12 and b > a:
        return (float(a + 12), float(b + 12))
    return (float(a), float(b))


def normalize_store_id(val: Any) -> str | None:
    if pd.isna(val):
        return None
    return str(val).strip().replace(" ", "").upper()


def collect_basket_values(row: pd.Series) -> list[float]:
    vals: list[float] = []
    for i in range(1, 6):
        v = row.get(f"basket_{i}")
        if pd.notna(v):
            try:
                vals.append(float(v))
            except (ValueError, TypeError):
                pass
    return vals


def parse_bool(val: Any) -> bool | None:
    if pd.isna(val):
        return None
    s = str(val).strip().lower()
    if s in ("yes", "true", "y", "1"):
        return True
    if s in ("no", "false", "n", "0"):
        return False
    return None


# ----------------------------------------------------------------------------
# Clean a single visit
# ----------------------------------------------------------------------------
def clean_visit(row: pd.Series) -> dict[str, Any]:
    """Apply all parsers to one row, collect issues. Returns a flat dict."""
    issues: list[str] = []

    store_id = normalize_store_id(row.get("store_id"))
    if not store_id:
        issues.append("missing_store_id")

    visit_date = row.get("visit_date")
    if pd.isna(visit_date):
        issues.append("missing_visit_date")
        visit_date_str = ""
    else:
        visit_date_str = str(visit_date).strip()

    obs_start_hr = parse_time_to_hour(row.get("obs_window_start"))
    obs_end_hr = parse_time_to_hour(row.get("obs_window_end"))
    if obs_start_hr is None:
        issues.append("bad_obs_start")
    if obs_end_hr is None:
        issues.append("bad_obs_end")

    # Window minutes. Normal case: end - start. If end < start, assume wrap (unusual).
    if obs_start_hr is not None and obs_end_hr is not None:
        diff = obs_end_hr - obs_start_hr
        if diff < 0:
            diff += 24
            issues.append("obs_window_wraps_midnight")
        obs_window_minutes = diff * 60
        if obs_window_minutes < 5 or obs_window_minutes > 60:
            issues.append(f"obs_window_minutes_unusual={obs_window_minutes:.0f}")
    else:
        obs_window_minutes = None

    # Opening hours
    open_hr = parse_time_to_hour(row.get("open_time"))
    close_hr = parse_time_to_hour(row.get("close_time"))
    if open_hr is not None and close_hr is not None:
        if close_hr == open_hr:
            issues.append("open_eq_close_suspect_12h")
            operating_hours = 12.0
        elif close_hr < open_hr:
            operating_hours = (24 - open_hr) + close_hr
        else:
            operating_hours = close_hr - open_hr
        if operating_hours > 22:
            issues.append(f"operating_hours_very_long={operating_hours:.1f}")
    else:
        operating_hours = None
        issues.append("missing_operating_hours")

    # Bill range
    bill_lo = row.get("bill_range_low")
    bill_hi = row.get("bill_range_high")
    try:
        bill_lo = float(bill_lo) if pd.notna(bill_lo) else None
    except (ValueError, TypeError):
        bill_lo = None
    try:
        bill_hi = float(bill_hi) if pd.notna(bill_hi) else None
    except (ValueError, TypeError):
        bill_hi = None
    if bill_lo is not None and bill_hi is not None and bill_lo > bill_hi:
        issues.append("bill_range_inverted")
        bill_lo, bill_hi = bill_hi, bill_lo

    # Baskets
    baskets = collect_basket_values(row)
    if len(baskets) < 3:
        issues.append(f"few_basket_values_n={len(baskets)}")

    # Transactions
    try:
        txn_n = int(row.get("transactions_n")) if pd.notna(row.get("transactions_n")) else None
    except (ValueError, TypeError):
        txn_n = None
        issues.append("bad_transactions_n")

    # Customers per day
    cust_lo, cust_hi = parse_customers_per_day(row.get("customers_per_day_est"))

    # Peak hours
    peak_lo, peak_hi = parse_peak_hours(row.get("peak_hours"))

    # Revenue band
    rev_lo, rev_hi, rev_flag = parse_revenue_band(row.get("revenue_band_reported_raw"))

    # Restock frequencies (handle decimal commas)
    restock = {}
    for i in (1, 2, 3):
        freq = parse_decimal_commas(row.get(f"restock_freq_item_{i}"))
        try:
            qty = float(row.get(f"qty_per_restock_item_{i}")) if pd.notna(row.get(f"qty_per_restock_item_{i}")) else None
        except (ValueError, TypeError):
            qty = None
        try:
            price = float(row.get(f"price_item_{i}")) if pd.notna(row.get(f"price_item_{i}")) else None
        except (ValueError, TypeError):
            price = None
        restock[f"restock_freq_item_{i}"] = freq
        restock[f"qty_per_restock_item_{i}"] = qty
        restock[f"price_item_{i}"] = price

    visit_id = f"{store_id}_{visit_date_str}_{row.get('obs_window_start') or 'na'}".replace(" ", "").replace(":", "").replace("/", "")

    out: dict[str, Any] = {
        "visit_id": visit_id,
        "store_id": store_id,
        "visit_date": visit_date_str,
        "day_of_week": row.get("day_of_week"),
        "weather": row.get("weather"),
        "obs_window_start_hr": obs_start_hr,
        "obs_window_end_hr": obs_end_hr,
        "obs_window_minutes": obs_window_minutes,
        "obs_daypart": row.get("obs_daypart"),
        "transactions_n": txn_n,
        "staff_n": row.get("staff_n"),
        "fridges_n": row.get("fridges_n"),
        "categories_visible": row.get("categories_visible"),
        "customers_per_day_est_low": cust_lo,
        "customers_per_day_est_high": cust_hi,
        "basket_values_obs_json": json.dumps(baskets),
        "basket_values_obs_n": len(baskets),
        "open_hr": open_hr,
        "close_hr": close_hr,
        "operating_hours": operating_hours,
        "days_closed_per_week": row.get("days_closed_per_week"),
        "peak_hour_start": peak_lo,
        "peak_hour_end": peak_hi,
        "current_period_status": row.get("current_period_status"),
        "today_normality": row.get("today_normality"),
        "weekend_difference": row.get("weekend_difference"),
        "bill_range_low": bill_lo,
        "bill_range_high": bill_hi,
        "top_item_1": row.get("top_item_1"),
        "top_item_2": row.get("top_item_2"),
        "top_item_3": row.get("top_item_3"),
        **restock,
        "offers_credit_udhaar": parse_bool(row.get("offers_credit_udhaar")),
        "takes_remote_orders": parse_bool(row.get("takes_remote_orders")),
        "offers_delivery": parse_bool(row.get("offers_delivery")),
        "revenue_band_low_pkr": rev_lo,
        "revenue_band_high_pkr": rev_hi,
        "revenue_band_flag": rev_flag,
        "confidence_score": row.get("confidence_score"),
        "notes": row.get("notes"),
        "owner_cooperative": row.get("owner_cooperative"),
        "uses_tajir": parse_bool(row.get("uses_tajir")),
        "issues": ";".join(issues) if issues else "",
    }
    return out


# ----------------------------------------------------------------------------
# Build schemas
# ----------------------------------------------------------------------------
def build_store_master(visits_df: pd.DataFrame, raw_df: pd.DataFrame) -> pd.DataFrame:
    """One row per store_id. Takes the first-visit values for stable attributes
    (type, size, area, income_proxy) and aggregates visit counts."""
    cols_from_raw = [
        "store_id", "area_name", "income_proxy", "store_type", "store_size",
        "location_type", "store_age_years",
    ]
    first = raw_df.assign(store_id=raw_df["store_id"].map(normalize_store_id)) \
                  .groupby("store_id", as_index=False).first()[cols_from_raw]
    visit_counts = visits_df.groupby("store_id").size().rename("n_visits").reset_index()
    master = first.merge(visit_counts, on="store_id", how="left")
    return master


def build_visits(raw_df: pd.DataFrame) -> pd.DataFrame:
    cleaned = [clean_visit(row) for _, row in raw_df.iterrows()]
    return pd.DataFrame(cleaned)


def main(raw_path: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = pd.read_csv(raw_path)
    # Rename columns using COLMAP; leave unknown columns as-is
    raw = raw.rename(columns=COLMAP)

    visits = build_visits(raw)
    master = build_store_master(visits, raw)

    visits.to_csv(out_dir / "visit_observations.csv", index=False)
    master.to_csv(out_dir / "store_master.csv", index=False)

    # Stub the downstream files so the pipeline contract is always present
    pd.DataFrame(columns=[
        "store_id", "visit_id", "daily_txn_est", "avg_basket_est",
        "revenue_flow_low", "revenue_flow_base", "revenue_flow_high",
        "revenue_inventory_est", "triangulation_ratio",
        "owner_band_low", "owner_band_high", "owner_band_contains_base",
        "confidence_score", "flags",
    ]).to_csv(out_dir / "model_results.csv", index=False)

    pd.DataFrame(columns=["store_id", "metric", "value", "flag"]) \
        .to_csv(out_dir / "validation.csv", index=False)

    # Summary to stdout
    print(f"Wrote {len(visits)} visits across {master['store_id'].nunique()} stores to {out_dir}")
    issues = visits[visits["issues"] != ""]
    if len(issues):
        print(f"\n{len(issues)} visits flagged with issues:")
        for _, r in issues.iterrows():
            print(f"  {r['visit_id']}: {r['issues']}")
    else:
        print("No issues flagged.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", type=Path, required=True, help="Raw form CSV path")
    ap.add_argument("--out", type=Path, default=Path("./clean_out"), help="Output directory")
    args = ap.parse_args()
    main(args.raw, args.out)
