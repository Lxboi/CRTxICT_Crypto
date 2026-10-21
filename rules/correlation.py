"""
rules/correlation.py

Recherche de corrélations entre les actifs suivis, pour identifier
dynamiquement des paires candidates au SMT (Smart Money Technique) —
en complément de l'appariement fixe configuré dans main.py
(DEFAULT_CORRELATED_PAIR). Utile en particulier une fois plusieurs
actifs de classes différentes suivis (crypto via Binance, ETF/Forex via
Polygon) : certaines corrélations pertinentes ne sont pas évidentes a
priori (ex: BTC et un ETF particulier), ce module permet de les
détecter plutôt que de compter uniquement sur un appariement manuel.

Ne prend aucune décision de trading : fournit une liste de paires
corrélées, à charge de main.py de décider comment les utiliser.

Implémenté en Python pur (pas de dépendance numpy/pandas) : le volume
de données pour un calcul de corrélation (quelques dizaines à
quelques centaines de points) ne justifie pas la dépendance.
"""

from __future__ import annotations

from dataclasses import dataclass

from models.market_data import CandleSeries

DEFAULT_CORRELATION_THRESHOLD = 0.7
DEFAULT_LOOKBACK_CANDLES = 30


@dataclass
class CorrelationResult:
    """Une paire d'actifs corrélés au-delà du seuil, sur une timeframe donnée."""

    symbol_a: str
    symbol_b: str
    correlation: float
    sample_size: int


def pearson_correlation(series_a: list[float], series_b: list[float]) -> float:
    """
    Coefficient de corrélation de Pearson entre deux séries alignées de
    même longueur (entre -1 et 1).

    Args:
        series_a: Première série de valeurs (ex: clôtures).
        series_b: Deuxième série, même longueur que series_a.

    Returns:
        Le coefficient de corrélation, ou 0.0 si l'une des séries est
        constante (écart-type nul, corrélation mathématiquement
        indéfinie — 0.0 est plus pratique qu'une exception pour un
        balayage automatique sur de nombreuses paires) ou si moins de
        2 points sont fournis.

    Raises:
        ValueError: Si les deux séries n'ont pas la même longueur.
    """
    n = len(series_a)
    if n != len(series_b):
        raise ValueError(
            f"Les deux séries doivent avoir la même longueur (reçu {n} et {len(series_b)})"
        )
    if n < 2:
        return 0.0

    mean_a = sum(series_a) / n
    mean_b = sum(series_b) / n

    covariance = sum((a - mean_a) * (b - mean_b) for a, b in zip(series_a, series_b))
    std_a = sum((a - mean_a) ** 2 for a in series_a) ** 0.5
    std_b = sum((b - mean_b) ** 2 for b in series_b) ** 0.5

    if std_a == 0 or std_b == 0:
        return 0.0

    return covariance / (std_a * std_b)


def _aligned_closes(
    series_a: CandleSeries, series_b: CandleSeries, lookback: int
) -> tuple[list[float], list[float]]:
    """
    Extrait les N derniers prix de clôture de deux CandleSeries,
    tronqués à la longueur commune la plus courte.

    Alignement simple par position (pas par horodatage exact) :
    suffisant quand les deux séries partagent la même granularité et
    une fenêtre de collecte comparable, ce qui est le cas ici (toutes
    deux collectées au même cycle). Une divergence de calendrier entre
    sources différentes (ex: Binance qui trade 24/7 vs Polygon sur les
    heures de marché US) peut légèrement désynchroniser l'alignement —
    acceptable pour une détection de tendance générale, mais à garder
    en tête si la précision devient critique.
    """
    closes_a = [c.close for c in series_a.candles[-lookback:]]
    closes_b = [c.close for c in series_b.candles[-lookback:]]
    common_length = min(len(closes_a), len(closes_b))
    return closes_a[-common_length:], closes_b[-common_length:]


def find_correlated_pairs(
    candles_by_symbol: dict[str, dict[str, CandleSeries]],
    timeframe: str = "daily",
    lookback: int = DEFAULT_LOOKBACK_CANDLES,
    threshold: float = DEFAULT_CORRELATION_THRESHOLD,
) -> list[CorrelationResult]:
    """
    Calcule la corrélation entre chaque paire d'actifs suivis (sur la
    timeframe donnée), et renvoie celles au-dessus du seuil, triées de
    la plus forte à la plus faible (en valeur absolue).

    Args:
        candles_by_symbol: Dict {symbole: {"daily": CandleSeries, ...}}
            — même structure que ce que main.py collecte pour chaque
            actif dans run_once().
        timeframe: Timeframe à comparer (défaut "daily").
        lookback: Nombre de bougies récentes à considérer.
        threshold: Seuil de corrélation absolue (|r|) au-delà duquel
            une paire est retenue. 0.7 est un seuil usuel pour une
            corrélation "forte" en analyse financière, mais reste un
            choix arbitraire — à ajuster selon vos observations.

    Returns:
        Liste de CorrelationResult, triée par |correlation| décroissante.
    """
    symbols = list(candles_by_symbol.keys())
    results: list[CorrelationResult] = []

    for i in range(len(symbols)):
        for j in range(i + 1, len(symbols)):
            symbol_a, symbol_b = symbols[i], symbols[j]

            series_a = candles_by_symbol[symbol_a].get(timeframe)
            series_b = candles_by_symbol[symbol_b].get(timeframe)
            if series_a is None or series_b is None:
                continue

            closes_a, closes_b = _aligned_closes(series_a, series_b, lookback)
            if len(closes_a) < 2:
                continue

            correlation = pearson_correlation(closes_a, closes_b)

            if abs(correlation) >= threshold:
                results.append(
                    CorrelationResult(
                        symbol_a=symbol_a,
                        symbol_b=symbol_b,
                        correlation=correlation,
                        sample_size=len(closes_a),
                    )
                )

    results.sort(key=lambda r: abs(r.correlation), reverse=True)
    return results


def build_correlated_pair_map(
    correlation_results: list[CorrelationResult],
) -> dict[str, str]:
    """
    Construit un mapping {symbole: paire_corrélée_la_plus_forte} à
    partir des résultats de find_correlated_pairs() — une seule paire
    corrélée retenue par symbole (la plus forte trouvée), pour rester
    compatible avec le mécanisme SMT actuel de NarrativeRule (une seule
    paire secondaire à la fois — voir MarketContext.secondary_candles).

    Si un symbole apparaît dans plusieurs paires corrélées au-dessus du
    seuil, seule la plus forte est retenue pour lui.
    """
    best_match: dict[str, tuple[str, float]] = {}

    for result in correlation_results:
        for symbol, other in (
            (result.symbol_a, result.symbol_b),
            (result.symbol_b, result.symbol_a),
        ):
            current_best = best_match.get(symbol)
            if current_best is None or abs(result.correlation) > abs(current_best[1]):
                best_match[symbol] = (other, result.correlation)

    return {symbol: other for symbol, (other, _) in best_match.items()}
