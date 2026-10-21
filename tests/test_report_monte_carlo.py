"""tests/test_report_monte_carlo.py"""

from __future__ import annotations

import csv

from backtest.monte_carlo import MonteCarloResult, run_monte_carlo
from backtest.report import export_monte_carlo_csv, print_monte_carlo_summary


def _sample_result() -> MonteCarloResult:
    return run_monte_carlo(
        [1.0, -1.0, 0.0, 2.0, -1.0], n_trades=8, n_simulations=100, starting_capital=10_000.0,
        risk_pct=2.0, ruin_threshold_pct=100.0, seed=7,
    )


class TestPrintMonteCarloSummary:
    def test_does_not_raise_with_or_without_observed_r_cumulative(self, capsys):
        result = _sample_result()
        print_monte_carlo_summary(result, "BTC", observed_r_cumulative=None)
        print_monte_carlo_summary(result, "BTC", observed_r_cumulative=6.63)

        captured = capsys.readouterr()
        assert "MONTE CARLO" in captured.out
        assert "Probabilité de ruine" in captured.out
        assert "6.63" in captured.out or "+6.63" in captured.out


class TestExportMonteCarloCsv:
    def test_writes_one_row_per_simulation_with_expected_columns(self, tmp_path):
        result = _sample_result()
        output_path = tmp_path / "montecarlo.csv"

        export_monte_carlo_csv(result, output_path)

        with open(output_path, encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))

        assert len(rows) == result.n_simulations
        assert set(rows[0].keys()) == {"simulation", "final_capital", "return_pct", "max_drawdown_pct", "ruined"}
        assert rows[0]["simulation"] == "1"
        assert rows[-1]["simulation"] == str(result.n_simulations)

    def test_ruined_flag_matches_ruin_threshold(self, tmp_path):
        # Toutes les trajectoires perdent tout (-1R à 100% de risque) -> ruined=1 partout.
        result = run_monte_carlo(
            [-1.0], n_trades=1, n_simulations=5, starting_capital=1_000.0, risk_pct=100.0,
            ruin_threshold_pct=100.0, seed=1,
        )
        output_path = tmp_path / "montecarlo_ruin.csv"
        export_monte_carlo_csv(result, output_path)

        with open(output_path, encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))

        assert all(row["ruined"] == "1" for row in rows)
