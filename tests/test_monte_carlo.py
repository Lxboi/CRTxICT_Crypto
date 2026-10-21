"""tests/test_monte_carlo.py"""

from __future__ import annotations

import pytest

from backtest.monte_carlo import (
    MonteCarloResult,
    extract_net_r_values,
    percentile,
    run_monte_carlo,
)


class TestPercentile:
    def test_median_of_odd_length(self):
        assert percentile([1.0, 2.0, 3.0], 50) == pytest.approx(2.0)

    def test_min_and_max(self):
        values = [5.0, 1.0, 3.0, 2.0, 4.0]
        assert percentile(values, 0) == pytest.approx(1.0)
        assert percentile(values, 100) == pytest.approx(5.0)

    def test_interpolates_between_two_values(self):
        # 4 valeurs triées [1,2,3,4] -- p50 doit interpoler entre l'index 1 (2) et l'index 2 (3).
        assert percentile([1.0, 2.0, 3.0, 4.0], 50) == pytest.approx(2.5)

    def test_single_value_returns_it_for_any_percentile(self):
        assert percentile([42.0], 5) == 42.0
        assert percentile([42.0], 95) == 42.0

    def test_unsorted_input_still_handled_correctly(self):
        assert percentile([3.0, 1.0, 2.0], 50) == pytest.approx(2.0)

    def test_empty_values_raises(self):
        with pytest.raises(ValueError):
            percentile([], 50)

    def test_out_of_range_pct_raises(self):
        with pytest.raises(ValueError):
            percentile([1.0, 2.0], 150)
        with pytest.raises(ValueError):
            percentile([1.0, 2.0], -1)


class TestExtractNetRValues:
    class _FakeOutcome:
        def __init__(self, rejected, net_r_multiple):
            self.rejected = rejected
            self.net_r_multiple = net_r_multiple

    def test_excludes_rejected_and_open_trades(self):
        outcomes = [
            self._FakeOutcome(rejected=True, net_r_multiple=None),  # rejeté par un garde-fou
            self._FakeOutcome(rejected=False, net_r_multiple=None),  # encore OPEN
            self._FakeOutcome(rejected=False, net_r_multiple=1.5),  # exécuté, clôturé
            self._FakeOutcome(rejected=False, net_r_multiple=-0.8),  # exécuté, clôturé
        ]
        assert extract_net_r_values(outcomes) == [1.5, -0.8]

    def test_empty_list_returns_empty(self):
        assert extract_net_r_values([]) == []


class TestRunMonteCarloValidation:
    def test_empty_net_r_values_raises(self):
        with pytest.raises(ValueError):
            run_monte_carlo([], n_simulations=10)

    def test_zero_or_negative_n_simulations_raises(self):
        with pytest.raises(ValueError):
            run_monte_carlo([1.0], n_simulations=0)
        with pytest.raises(ValueError):
            run_monte_carlo([1.0], n_simulations=-5)


class TestRunMonteCarloBasics:
    def test_default_n_trades_matches_input_length(self):
        result = run_monte_carlo([1.0, -1.0, 0.5], n_simulations=50, seed=1)
        assert result.n_trades_per_simulation == 3

    def test_explicit_n_trades_overrides_default(self):
        result = run_monte_carlo([1.0, -1.0], n_trades=10, n_simulations=50, seed=1)
        assert result.n_trades_per_simulation == 10

    def test_result_has_one_entry_per_simulation(self):
        result = run_monte_carlo([1.0, -1.0, 0.0], n_simulations=123, seed=1)
        assert len(result.final_capitals) == 123
        assert len(result.max_drawdowns_pct) == 123

    def test_deterministic_with_fixed_seed(self):
        result_a = run_monte_carlo([1.0, -1.0, 2.0, -0.5], n_trades=20, n_simulations=200, seed=42)
        result_b = run_monte_carlo([1.0, -1.0, 2.0, -0.5], n_trades=20, n_simulations=200, seed=42)
        assert result_a.final_capitals == result_b.final_capitals

    def test_different_seeds_produce_different_results(self):
        result_a = run_monte_carlo([1.0, -1.0, 2.0, -0.5], n_trades=20, n_simulations=200, seed=1)
        result_b = run_monte_carlo([1.0, -1.0, 2.0, -0.5], n_trades=20, n_simulations=200, seed=2)
        assert result_a.final_capitals != result_b.final_capitals


class TestAllWinningTrades:
    def test_all_positive_r_values_only_grow_capital_never_ruin(self):
        result = run_monte_carlo([1.0, 2.0, 0.5], n_trades=10, n_simulations=100, starting_capital=10_000.0, risk_pct=2.0, seed=1)
        assert all(c > 10_000.0 for c in result.final_capitals)
        assert result.ruin_count == 0
        assert result.probability_of_ruin == 0.0
        assert all(d == 0.0 for d in result.max_drawdowns_pct)  # jamais de recul si toujours gagnant


class TestRuinDetection:
    def test_catastrophic_losses_trigger_ruin_at_default_threshold(self):
        # risk_pct=100% -- une seule perte de -1R épuise déjà TOUT le capital risqué.
        result = run_monte_carlo(
            [-1.0], n_trades=1, n_simulations=10, starting_capital=10_000.0, risk_pct=100.0,
            ruin_threshold_pct=100.0, seed=1,
        )
        assert result.ruin_count == 10
        assert result.probability_of_ruin == 1.0
        assert all(c == pytest.approx(0.0) for c in result.final_capitals)

    def test_capital_never_goes_negative_even_on_extreme_loss(self):
        result = run_monte_carlo(
            [-100.0], n_trades=1, n_simulations=5, starting_capital=1_000.0, risk_pct=50.0, seed=1,
        )
        assert all(c >= 0.0 for c in result.final_capitals)

    def test_lower_ruin_threshold_flags_more_trajectories(self):
        # -0.5R répété -- pas assez pour ruiner à 100%, mais ruine bien à un seuil plus prudent (30%).
        strict = run_monte_carlo(
            [-0.5], n_trades=20, n_simulations=50, starting_capital=10_000.0, risk_pct=2.0,
            ruin_threshold_pct=100.0, seed=1,
        )
        lenient_threshold = run_monte_carlo(
            [-0.5], n_trades=20, n_simulations=50, starting_capital=10_000.0, risk_pct=2.0,
            ruin_threshold_pct=15.0, seed=1,
        )
        assert strict.ruin_count == 0
        assert lenient_threshold.ruin_count == 50  # -0.5R * 2% * 20 trades ~ -19% de drawdown, dépasse -15%


class TestConvergence:
    """
    Sanity check explicitement demandé par le plan initial (Phase 4) :
    la moyenne des trajectoires Monte Carlo doit converger vers
    l'espérance mathématique de la distribution bootstrap sous-jacente.

    Avec des tirages i.i.d. et une composition multiplicative
    indépendante par trade, E[capital_final] = capital_départ *
    E[1 + r*risk_pct/100]^n_trades (produit d'espérances indépendantes)
    -- calculable exactement sans simuler quoi que ce soit, ce qui
    donne une cible de comparaison fiable plutôt qu'un simple "à vue de
    nez". Un grand nombre de simulations (20000) et une graine fixe
    gardent ce test déterministe et reproductible.
    """

    def test_mean_final_capital_converges_to_theoretical_expectation(self):
        net_r_values = [1.0, -1.0, 0.0, 2.0, -1.05]
        n_trades = 6
        risk_pct = 2.0
        starting_capital = 10_000.0

        mean_factor = sum(1 + r * risk_pct / 100 for r in net_r_values) / len(net_r_values)
        theoretical_expected_capital = starting_capital * mean_factor**n_trades

        result = run_monte_carlo(
            net_r_values, n_trades=n_trades, n_simulations=20_000, starting_capital=starting_capital,
            risk_pct=risk_pct, seed=123,
        )
        mean_final_capital = sum(result.final_capitals) / len(result.final_capitals)

        assert mean_final_capital == pytest.approx(theoretical_expected_capital, rel=0.02)


class TestMonteCarloResultProbabilityOfRuin:
    def test_probability_of_ruin_property(self):
        result = MonteCarloResult(
            n_simulations=200, n_trades_per_simulation=10, starting_capital=10_000.0, risk_pct=2.0,
            ruin_threshold_pct=100.0, final_capitals=[10_000.0] * 200, max_drawdowns_pct=[0.0] * 200,
            ruin_count=42,
        )
        assert result.probability_of_ruin == pytest.approx(42 / 200)

    def test_zero_simulations_does_not_divide_by_zero(self):
        result = MonteCarloResult(
            n_simulations=0, n_trades_per_simulation=0, starting_capital=10_000.0, risk_pct=2.0,
            ruin_threshold_pct=100.0, final_capitals=[], max_drawdowns_pct=[], ruin_count=0,
        )
        assert result.probability_of_ruin == 0.0
