"""
backtest/execution_costs.py

Modèle de coûts d'exécution réels (Phase 3 du plan "Backtest réaliste +
Monte Carlo") : frais taker (barèmes publics par défaut, ajustables) +
slippage forfaitaire en points de base, appliqués sur les DEUX jambes
d'un trade (entrée ET sortie) -- jamais une seule, un aller-retour
complet paie toujours les deux.

Les coûts sont exprimés directement en multiples du risque initial (R)
plutôt qu'en dollars : puisque chaque position est dimensionnée pour
risquer exactement `risk_pct`% du capital (voir
execution/position_sizing.py::compute_position_size), le risque en
dollars d'un trade est TOUJOURS égal à
risk_amount = capital * risk_pct/100 -- les frais/slippage en dollars,
une fois divisés par ce risk_amount, se simplifient et ne dépendent
donc PLUS ni du capital ni de la taille de position, seulement des PRIX
(entrée, sortie, distance au stop initial) et des taux configurés. Voir
compute_costs_in_r() pour la dérivation complète. Ceci permet de
calculer le coût réel d'un trade sans avoir à connaître le capital au
moment de son entrée -- seule l'évolution du capital lui-même (voir
backtest/realistic_backtest.py) a besoin de ce risk_amount explicite.

Barèmes par défaut (taker, en % du notionnel, PAR JAMBE) -- valeurs
publiques approximatives au 14/08/2026, À AJUSTER si les grilles
tarifaires réelles changent (voir CLAUDE.md, section Exchanges
supportés, pour le détail par venue) : Kraken Futures ~0.05%, Binance
USDⓈ-M ~0.04-0.05%, OKX X-Perps ~0.05%. IBKR facture une commission PAR
CONTRAT (structure radicalement différente d'un pourcentage du
notionnel) -- NON modélisée ici faute de données de contrat
(multiplicateur, devise de cotation) : un taux `--fee-pct` doit être
fourni explicitement pour tout backtest exécuté via IBKR (voir
run_backtest_intraday.py, qui avertit clairement si aucun taux n'est
disponible pour l'exchange configuré).
"""

from __future__ import annotations

# Taux taker par défaut, en % du notionnel, PAR JAMBE (pas aller-retour).
DEFAULT_FEE_RATES_PCT: dict[str, float] = {
    "kraken": 0.05,
    "binance": 0.05,
    "okx": 0.05,
}

# Slippage forfaitaire par défaut, en points de base (1 bps = 0.01%),
# appliqué au prix sur CHAQUE jambe (entrée et sortie) -- valeur
# prudente par défaut pour un ordre au marché sur un actif liquide
# (BTC/ETH/majors Forex) ; à ajuster (--slippage-bps) pour un actif
# moins liquide ou en cas de forte volatilité connue sur la période
# testée.
DEFAULT_SLIPPAGE_BPS = 5.0


def resolve_default_fee_pct(exchange: str | None) -> float | None:
    """
    Renvoie le taux par défaut pour cet exchange (voir
    DEFAULT_FEE_RATES_PCT), ou None si aucun taux public par défaut
    n'est disponible pour lui (cas IBKR -- commission par contrat, voir
    docstring module) -- l'appelant doit alors exiger un `--fee-pct`
    explicite plutôt que de deviner une valeur arbitraire.
    """
    if exchange is None:
        return None
    return DEFAULT_FEE_RATES_PCT.get(exchange.lower())


def compute_costs_in_r(
    entry_price: float,
    exit_price: float,
    initial_risk: float,
    fee_pct: float,
    slippage_bps: float,
) -> tuple[float, float]:
    """
    Renvoie (fees_paid_r, slippage_cost_r) -- coûts d'un aller-retour
    complet (entrée + sortie), exprimés en multiples du risque initial
    (R), toujours positifs (un coût, jamais un gain).

    Dérivation (voir docstring module) : pour une position dimensionnée
    à risk_amount = capital * risk_pct/100, quantity = risk_amount /
    initial_risk, donc :
        fees_$      = quantity * entry_price * (fee_pct/100)
                    + quantity * exit_price  * (fee_pct/100)
                    = risk_amount * (entry_price + exit_price) * (fee_pct/100) / initial_risk
        fees_R      = fees_$ / risk_amount
                    = (entry_price + exit_price) * (fee_pct/100) / initial_risk
    Même raisonnement pour le slippage (bps du prix, appliqué sur
    chaque jambe au lieu d'un taux de frais).

    initial_risk <= 0 (ne devrait jamais arriver -- un trade sans
    risque n'aurait jamais dû être exécuté, voir
    execution/position_sizing.py::compute_position_size, qui lève déjà
    ValueError dans ce cas en amont) renvoie (0.0, 0.0) par sécurité
    plutôt qu'une division par zéro.
    """
    if initial_risk <= 0:
        return 0.0, 0.0

    price_sum = entry_price + exit_price

    fee_rate = fee_pct / 100
    fees_r = price_sum * fee_rate / initial_risk

    slippage_rate = slippage_bps / 10_000
    slippage_r = price_sum * slippage_rate / initial_risk

    return fees_r, slippage_r
