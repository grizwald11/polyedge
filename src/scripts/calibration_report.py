"""Standalone calibration report — run anytime to check prediction accuracy.

Usage:
    python -m src.scripts.calibration_report
    python -m src.scripts.calibration_report --db data/markets.db
"""

from __future__ import annotations

import argparse
import sys

from src.analysis.calibration_analyzer import CalibrationAnalyzer
from src.storage.database import Database


def format_report(analyzer: CalibrationAnalyzer) -> str:
    """Generate a formatted calibration report string."""
    report = analyzer.generate_report()
    lines = []

    lines.append("=" * 60)
    lines.append("  POLYEDGE CALIBRATION REPORT")
    lines.append("=" * 60)
    lines.append("")

    # Overall stats
    lines.append("OVERALL STATS")
    lines.append("-" * 40)
    if report.total_resolved == 0:
        lines.append("  No resolved predictions yet.")
        lines.append(f"  Unresolved predictions: {report.total_unresolved}")
        lines.append("")
        return "\n".join(lines)

    lines.append(f"  Brier score:    {report.overall_brier:.4f}  (0=perfect, 0.25=random)")
    lines.append(f"  Win rate:       {report.overall_win_rate:.1%}")
    lines.append(f"  Resolved:       {report.total_resolved}")
    lines.append(f"  Unresolved:     {report.total_unresolved}")
    lines.append(f"  Total:          {report.total_resolved + report.total_unresolved}")
    lines.append("")

    # Quality assessment
    if report.overall_brier is not None:
        if report.overall_brier < 0.10:
            quality = "Excellent"
        elif report.overall_brier < 0.15:
            quality = "Good"
        elif report.overall_brier < 0.20:
            quality = "Decent"
        elif report.overall_brier < 0.25:
            quality = "Below random — needs work"
        else:
            quality = "Poor — worse than random guessing"
        lines.append(f"  Quality:        {quality}")
        lines.append("")

    # Calibration curve
    if report.calibration_curve:
        lines.append("CALIBRATION CURVE")
        lines.append("-" * 40)
        lines.append(f"  {'Bucket':<12s} {'Predicted':>10s} {'Actual':>10s} {'Count':>6s}  {'Delta':>8s}")
        for b in report.calibration_curve:
            if b.count > 0 and b.actual_avg is not None:
                delta = b.actual_avg - b.predicted_avg
                delta_str = f"{delta:+.1%}"
                lines.append(
                    f"  {b.bin_label:<12s} {b.predicted_avg:>10.1%} {b.actual_avg:>10.1%} {b.count:>6d}  {delta_str:>8s}"
                )
            else:
                lines.append(f"  {b.bin_label:<12s} {'—':>10s} {'—':>10s} {b.count:>6d}  {'—':>8s}")
        lines.append("")

    # Per-category breakdown
    if report.category_stats:
        lines.append("CATEGORY BREAKDOWN")
        lines.append("-" * 40)
        lines.append(f"  {'Category':<15s} {'Brier':>8s} {'Count':>6s} {'Bias':>10s}")
        for cs in sorted(report.category_stats, key=lambda x: x.brier_score):
            if cs.bias > 0.02:
                bias_str = f"under {cs.bias:+.1%}"
            elif cs.bias < -0.02:
                bias_str = f"over {cs.bias:+.1%}"
            else:
                bias_str = "neutral"
            lines.append(
                f"  {cs.category:<15s} {cs.brier_score:>8.4f} {cs.count:>6d} {bias_str:>10s}"
            )
        lines.append("")

        if report.best_category:
            lines.append(f"  Best category:  {report.best_category}")
        if report.worst_category and report.worst_category != report.best_category:
            lines.append(f"  Worst category: {report.worst_category}")
        lines.append("")

    # Recommended adjustments
    adjustments = analyzer.get_category_adjustments()
    if adjustments:
        lines.append("RECOMMENDED ADJUSTMENTS")
        lines.append("-" * 40)
        for cat, adj in sorted(adjustments.items(), key=lambda x: abs(x[1]), reverse=True):
            direction = "underestimates" if adj > 0 else "overestimates"
            lines.append(f"  {cat}: Claude {direction} by {abs(adj):.1%} (apply {adj:+.3f})")
        lines.append("")
    else:
        lines.append("RECOMMENDED ADJUSTMENTS")
        lines.append("-" * 40)
        lines.append("  No significant biases detected (or insufficient data).")
        lines.append("")

    lines.append("=" * 60)
    return "\n".join(lines)


def main():
    """Generate and display a calibration accuracy report from the database."""
    parser = argparse.ArgumentParser(description="PolyEdge Calibration Report")
    parser.add_argument("--db", default="data/markets.db", help="Database path")
    args = parser.parse_args()

    db = Database(db_path=args.db, wal_mode=True)
    analyzer = CalibrationAnalyzer(db)

    import logging
    logging.basicConfig(level=logging.INFO)
    report = format_report(analyzer)
    logging.getLogger(__name__).info(report)
    # Also print to stdout for CLI usage
    sys.stdout.write(report + "\n")


if __name__ == "__main__":
    main()
