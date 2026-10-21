"""
backtest/monte_carlo.py

Phase 4 du plan "Backtest réaliste + Monte Carlo" : ré-échantillonnage
bootstrap (AVEC remise) de la série de net_r_multiple produite par le
backtest RÉALISTE (Phase 3, voir backtest/realistic_backtest.py) --
méthode standard pour estimer la distribution des résultats possibles à
partir d'un historique de trades fini, sans supposer une distribution
paramétrique (normale, etc.) des rendements.

Principe : le backtest réaliste ne donne QU'UNE SEULE séquence de
trades, dans l'ordre chronologique où ils se sont réellement produits
-- mais si l'edge du bot est réel, l'ORDRE exact dans lequel ces trades
sont arrivés est en grande partie une question de chance (les 2 gros
gagnants du run BTC réel du 14/08 auraient très bien pu arriver
n'importe où dans la séquence, ou ne pas arriver du tout sur une autre
période). Le bootstrap simule des MILLIERS de "vies alternatives"
possibles en ré-échantillonnant, avec remise, dans le SAC des
net_r_multiple déjà observés (même distribution empirique, ordre et
répétitions aléatoires) -- sans jamais inventer de valeurs de R non
observées dans le backtest réel.

Capital composé trade après trade : capital_(t+1) = capital_t * (1 +
net_r_t * risk_pct/100) -- EXACTEMENT la même formule que
backtest/realistic_backtest.py::run_realistic_backtest (voir la
dérivation dans backtest/execution_costs.py) -- indispensable pour que
la distribution Monte Carlo soit cohérente avec le backtest réaliste
d'origine (voir tests/test_monte_carlo.py::TestConvergence pour la
vérification statistique).

Le capital est plancher à 0 pendant chaque trajectoire simulée (jamais
négatif) -- cas dégénéré qui ne devrait jamais se produire avec un
risk_pct/SL réalistes (il faudrait une perte nette de -100/risk_pct R
sur un SEUL trade, ex: -50R avec risk_pct=2%, pour franchir zéro), mais
protège le calcul du drawdown/de la ruine contre un outlier extrême du
sac de trades sans produire un capital négatif absurde.
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass
class MonteCarloResult:
    """Résultat agrégé d'une simulation Monte Carlo bootstrap sur N trajectoires."""

    n_simulations: int
    n_trades_per_simulation: int
    starting_capital: float
    risk_pct: float
    ruin_threshold_pct: float

    final_capitals: list[float]  # une valeur par trajectoire simulée, dans l'ordre
    max_drawdowns_pct: list[float]  # idem, drawdown max DE CETTE trajectoire
    ruin_count: int  # nombre de trajectoires ayant touché le seuil de ruine à un moment donné

    @property
    def probability_of_ruin(self) -> float:
        return self.ruin_count / self.n_simulations if self.n_simulations else 0.0


def extract_net_r_values(outcomes: list) -> list[float]:
    """
    Extrait la série de net_r_multiple des trades RÉELLEMENT exécutés
    ET CLÔTURÉS d'une liste de backtest.realistic_backtest.
    RealisticTradeOutcome -- jamais un trade rejeté par un garde-fou
    (`rejected=True`), ni encore OPEN (`net_r_multiple is None`). C'est
    cette série qui alimente le bootstrap ci-dessous.
    """
    return [o.net_r_multiple for o in outcomes if not o.rejected and o.net_r_multiple is not None]


def percentile(values: list[float], pct: float) -> float:
    """
    Percentile `pct` (0-100) par interpolation linéaire entre les deux
    valeurs encadrantes (méthode standard, équivalente au comportement
    par défaut de numpy.percentile) -- réimplémentée ici en pur Python
    pour éviter d'ajouter numpy comme dépendance (voir requirements.txt,
    qui documente explicitement son absence volontaire du projet) pour
    ce seul calcul.
    """
    if not values:
        raise ValueError("values est vide -- aucun percentile calculable")
    if not (0 <= pct <= 100):
        raise ValueError(f"pct doit être dans [0, 100], reçu : {pct}")

    sorted_values = sorted(values)
    if len(sorted_values) == 1:
        return sorted_values[0]

    rank = (pct / 100) * (len(sorted_values) - 1)
    lower_idx = int(rank)
    upper_idx = min(lower_idx + 1, len(sorted_values) - 1)
    fraction = rank - lower_idx
    return sorted_values[lower_idx] + (sorted_values[upper_idx] - sorted_values[lower_idx]) * fraction


def _simulate_one_path(
    net_r_values: list[float],
    n_trades: int,
    starting_capital: float,
    risk_pct: float,
    ruin_threshold_pct: float,
    rng: random.Random,
) -> tuple[float, float, bool]:
    """
    Simule UNE trajectoire bootstrap : tire `n_trades` valeurs AVEC
    remise dans `net_r_values`, compose le capital trade après trade.

    Returns:
        (capital_final, drawdown_maximum_pct, ruined) -- `ruined` est
        vrai si le capital a touché le seuil de ruine à N'IMPORTE QUEL
        moment de la trajectoire (pas seulement à la fin -- un chemin
        peut passer sous le seuil puis se rétablir, ça reste une ruine
        au sens pratique du terme).
    """
    capital = starting_capital
    peak = starting_capital
    max_drawdown_pct = 0.0
    ruin_level = starting_capital * (1 - ruin_threshold_pct / 100)
    ruined = False

    for _ in range(n_trades):
        r = rng.choice(net_r_values)
        capital = max(0.0, capital * (1 + r * risk_pct / 100))

        peak = max(peak, capital)
        if peak > 0:
            drawdown_pct = (peak - capital) / peak * 100
            max_drawdown_pct = max(max_drawdown_pct, drawdown_pct)

        if capital <= ruin_level:
            ruined = True

    return capital, max_drawdown_pct, ruined


def run_monte_carlo(
    net_r_values: list[float],
    n_trades: int | None = None,
    n_simulations: int = 2000,
    starting_capital: float = 10_000.0,
    risk_pct: float = 2.0,
    ruin_threshold_pct: float = 100.0,
    seed: int | None = None,
) -> MonteCarloResult:
    """
    Ré-échantillonne `net_r_values` en `n_simulations` trajectoires
    bootstrap indépendantes, chacune composant un capital simulé de
    `n_trades` trades tirés AVEC remise.

    Args:
        net_r_values: Série des net_r_multiple observés dans le
            backtest RÉALISTE (Phase 3, voir extract_net_r_values) --
            le "sac" dans lequel le bootstrap pioche à chaque trade
            simulé. DOIT correspondre aux MÊMES risk_pct/coûts que ceux
            utilisés pour produire cette série (voir docstring de
            run_realistic_backtest) -- le rôle de `risk_pct` ci-dessous
            est UNIQUEMENT de rejouer la composition du capital, pas de
            recalculer les coûts, qui sont déjà inclus dans chaque
            net_r_multiple fourni.
        n_trades: Nombre de trades par trajectoire simulée -- défaut :
            len(net_r_values) (reproduit un horizon de la même longueur
            que l'historique observé). Augmenter pour projeter sur un
            horizon plus long (ex: 3x pour ~3 ans si net_r_values
            couvre ~1 an), toujours en piochant dans le même sac de
            résultats déjà observés -- jamais une extrapolation vers de
            nouvelles valeurs de R non vues dans le backtest réel.
        ruin_threshold_pct: % de perte du capital de départ considéré
            comme "ruine" (défaut 100 = capital tombé à 0 exactement --
            définition stricte). Réduire (ex: 50) pour une définition
            plus prudente ("j'arrête de trader si je perds la moitié de
            mon capital, même si je ne suis pas totalement ruiné").
        seed: Graine du générateur pseudo-aléatoire -- fixer pour un
            résultat reproductible (tests, comparaison avant/après un
            changement de config), laisser None pour un tirage réel
            (résultat différent à chaque appel).

    Raises:
        ValueError: Si net_r_values est vide, ou si n_simulations <= 0.
    """
    if not net_r_values:
        raise ValueError("net_r_values est vide -- aucun trade net à ré-échantillonner (backtest réaliste vide ?)")
    if n_simulations <= 0:
        raise ValueError(f"n_simulations doit être positif, reçu : {n_simulations}")

    effective_n_trades = n_trades if n_trades is not None else len(net_r_values)
    rng = random.Random(seed)

    final_capitals: list[float] = []
    max_drawdowns_pct: list[float] = []
    ruin_count = 0

    for _ in range(n_simulations):
        final_capital, max_drawdown_pct, ruined = _simulate_one_path(
            net_r_values, effective_n_trades, starting_capital, risk_pct, ruin_threshold_pct, rng
        )
        final_capitals.append(final_capital)
        max_drawdowns_pct.append(max_drawdown_pct)
        if ruined:
            ruin_count += 1

    return MonteCarloResult(
        n_simulations=n_simulations,
        n_trades_per_simulation=effective_n_trades,
        starting_capital=starting_capital,
        risk_pct=risk_pct,
        ruin_threshold_pct=ruin_threshold_pct,
        final_capitals=final_capitals,
        max_drawdowns_pct=max_drawdowns_pct,
        ruin_count=ruin_count,
    )
