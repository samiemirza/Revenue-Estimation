"""
visualize.py — Generate revenue estimation charts.

Produces 4 charts:
  1. Bar chart: R_base per store with [R_low, R_high] error bars (sorted by R_base)
  2. Scatter: R_flow vs R_inventory with y=x line (triangulation visual)
  3. Small multiples: Store tier vs revenue by store type and store size
  4. Band width as fraction of R_base, ordered by confidence_score

Run:
    python3 visualize.py --clean_dir ./clean_out --out_dir ./charts
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import numpy as np


def load_data(clean_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Load model results, store master, and per-visit results."""
    model = pd.read_csv(clean_dir / "model_results.csv")
    store = pd.read_csv(clean_dir / "store_master.csv")
    visits = pd.read_csv(clean_dir / "model_results_per_visit.csv")
    return model, store, visits


def chart_1_revenue_bars(model: pd.DataFrame, out_dir: Path) -> None:
    """
    Bar chart: R_base per store with [R_low, R_high] error bars.
    Sorted by R_base descending. The CEO headline chart.
    """
    df = model[["store_id", "revenue_flow_low", "revenue_flow_base", "revenue_flow_high"]].dropna()
    df = df.sort_values("revenue_flow_base", ascending=True)  # ascending for horizontal bars

    fig, ax = plt.subplots(figsize=(10, 6))

    y_pos = np.arange(len(df))

    # Calculate error bar lengths (asymmetric)
    err_low = df["revenue_flow_base"].values - df["revenue_flow_low"].values
    err_high = df["revenue_flow_high"].values - df["revenue_flow_base"].values

    # Create horizontal bar chart
    bars = ax.barh(
        y_pos,
        df["revenue_flow_base"].values / 1000,  # Convert to thousands
        xerr=[err_low / 1000, err_high / 1000],
        capsize=4,
        color="#2E86AB",
        edgecolor="#1B4F72",
        alpha=0.85,
        error_kw={"elinewidth": 1.5, "capthick": 1.5, "ecolor": "#E74C3C"}
    )

    ax.set_yticks(y_pos)
    ax.set_yticklabels(df["store_id"].values)
    ax.set_xlabel("Daily Revenue (PKR thousands)", fontsize=11)
    ax.set_title("Estimated Daily Revenue by Store\n(with uncertainty bands)", fontsize=13, fontweight="bold")

    # Add value labels
    for i, (bar, val) in enumerate(zip(bars, df["revenue_flow_base"].values)):
        ax.text(val / 1000 + 5, bar.get_y() + bar.get_height() / 2,
                f'{val/1000:.0f}k', va='center', fontsize=9, color="#333")

    ax.set_xlim(0, df["revenue_flow_high"].max() / 1000 * 1.15)
    ax.grid(axis="x", alpha=0.3, linestyle="--")

    plt.tight_layout()
    plt.savefig(out_dir / "1_revenue_bars.png", dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Saved: 1_revenue_bars.png")


def chart_2_triangulation_scatter(model: pd.DataFrame, out_dir: Path) -> None:
    """
    Scatter: R_flow vs R_inventory with y=x line.
    Shows the two independent estimation methods agreeing (or not).
    """
    df = model[["store_id", "revenue_flow_base", "revenue_inventory_est"]].dropna()

    fig, ax = plt.subplots(figsize=(8, 8))

    # Plot y=x reference line
    max_val = max(df["revenue_flow_base"].max(), df["revenue_inventory_est"].max())
    min_val = min(df["revenue_flow_base"].min(), df["revenue_inventory_est"].min())
    margin = (max_val - min_val) * 0.1
    line_range = [min_val - margin, max_val + margin]
    ax.plot(line_range, line_range, "k--", alpha=0.5, linewidth=1.5, label="Perfect agreement (y=x)")

    # Plot agreement bands (0.7x to 1.4x)
    ax.fill_between(
        line_range,
        [x * 0.7 for x in line_range],
        [x * 1.4 for x in line_range],
        alpha=0.15, color="green", label="Acceptable band (0.7x - 1.4x)"
    )

    # Scatter points
    ax.scatter(
        df["revenue_flow_base"] / 1000,
        df["revenue_inventory_est"] / 1000,
        s=120, c="#E74C3C", edgecolors="#922B21",
        alpha=0.8, zorder=5
    )

    # Label each point
    for _, row in df.iterrows():
        ax.annotate(
            row["store_id"],
            (row["revenue_flow_base"] / 1000, row["revenue_inventory_est"] / 1000),
            xytext=(5, 5), textcoords="offset points",
            fontsize=9, alpha=0.8
        )

    ax.set_xlabel("Flow Model Revenue (PKR thousands)", fontsize=11)
    ax.set_ylabel("Inventory Model Revenue (PKR thousands)", fontsize=11)
    ax.set_title("Triangulation: Flow vs Inventory Revenue Estimates", fontsize=13, fontweight="bold")

    ax.set_xlim(0, max_val / 1000 * 1.15)
    ax.set_ylim(0, max_val / 1000 * 1.15)
    ax.set_aspect("equal", adjustable="box")
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(alpha=0.3, linestyle="--")

    plt.tight_layout()
    plt.savefig(out_dir / "2_triangulation_scatter.png", dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Saved: 2_triangulation_scatter.png")


def chart_3_small_multiples(model: pd.DataFrame, store: pd.DataFrame, out_dir: Path) -> None:
    """
    Small multiples: store tier vs revenue by store type and store size.
    Shows whether the model produces sensible patterns across segments.
    """
    # Merge model results with store metadata
    df = model.merge(store[["store_id", "store_type", "store_size"]], on="store_id")

    fig, axes = plt.subplots(1, 2, figsize=(14, 6))

    # --- Panel A: By Store Type ---
    ax1 = axes[0]
    type_order = ["pan shop", "kiryana", "mini mart"]
    type_colors = {"pan shop": "#F39C12", "kiryana": "#27AE60", "mini mart": "#2E86AB"}

    for store_type in type_order:
        subset = df[df["store_type"] == store_type]
        if len(subset) > 0:
            y_pos = [type_order.index(store_type)] * len(subset)
            ax1.scatter(
                subset["revenue_flow_base"] / 1000,
                [p + np.random.uniform(-0.15, 0.15) for p in y_pos],  # jitter
                s=150, c=type_colors[store_type], edgecolors="#333",
                alpha=0.8, label=f"{store_type} (n={len(subset)})"
            )
            # Add median line
            median = subset["revenue_flow_base"].median() / 1000
            ax1.axvline(median, color=type_colors[store_type], linestyle="--", alpha=0.5, linewidth=2)

    ax1.set_yticks(range(len(type_order)))
    ax1.set_yticklabels([t.title() for t in type_order])
    ax1.set_xlabel("Daily Revenue (PKR thousands)", fontsize=11)
    ax1.set_title("Revenue by Store Type", fontsize=12, fontweight="bold")
    ax1.grid(axis="x", alpha=0.3, linestyle="--")
    ax1.set_xlim(0, df["revenue_flow_base"].max() / 1000 * 1.1)

    # --- Panel B: By Store Size ---
    ax2 = axes[1]
    size_order = ["Small", "Medium", "Large"]
    size_colors = {"Small": "#9B59B6", "Medium": "#3498DB", "Large": "#E74C3C"}

    for store_size in size_order:
        subset = df[df["store_size"] == store_size]
        if len(subset) > 0:
            y_pos = [size_order.index(store_size)] * len(subset)
            ax2.scatter(
                subset["revenue_flow_base"] / 1000,
                [p + np.random.uniform(-0.15, 0.15) for p in y_pos],  # jitter
                s=150, c=size_colors[store_size], edgecolors="#333",
                alpha=0.8, label=f"{store_size} (n={len(subset)})"
            )
            # Add median line
            median = subset["revenue_flow_base"].median() / 1000
            ax2.axvline(median, color=size_colors[store_size], linestyle="--", alpha=0.5, linewidth=2)

    ax2.set_yticks(range(len(size_order)))
    ax2.set_yticklabels(size_order)
    ax2.set_xlabel("Daily Revenue (PKR thousands)", fontsize=11)
    ax2.set_title("Revenue by Store Size", fontsize=12, fontweight="bold")
    ax2.grid(axis="x", alpha=0.3, linestyle="--")
    ax2.set_xlim(0, df["revenue_flow_base"].max() / 1000 * 1.1)

    fig.suptitle("Revenue Distribution by Store Segments", fontsize=14, fontweight="bold", y=1.02)
    plt.tight_layout()
    plt.savefig(out_dir / "3_small_multiples.png", dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Saved: 3_small_multiples.png")


def main(clean_dir: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Loading data from {clean_dir}...")
    model, store, visits = load_data(clean_dir)

    print(f"Generating charts...")
    chart_1_revenue_bars(model, out_dir)
    chart_2_triangulation_scatter(model, out_dir)
    chart_3_small_multiples(model, store, out_dir)

    print(f"\nAll charts saved to {out_dir}/")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean_dir", type=Path, default=Path("./clean_out"))
    ap.add_argument("--out_dir", type=Path, default=Path("./charts"))
    args = ap.parse_args()
    main(args.clean_dir, args.out_dir)
