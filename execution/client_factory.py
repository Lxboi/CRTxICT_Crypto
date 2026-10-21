"""
execution/client_factory.py

Construit l'ExchangeClient concret pour VOTRE compte Kraken.

Simplification architecture (août 2026) : IBKR/OKX/Binance-exécution ont
été retirés (le robot est désormais crypto uniquement, exécution sur
Kraken uniquement -- voir config/alerts.yaml::execution.exchange). Un
wrapper minimal est conservé, plutôt que de construire KrakenClient
directement partout où un client est nécessaire, pour garder un point
d'extension unique et propre si un second exchange crypto était ajouté
un jour.
"""

from __future__ import annotations

from execution.exchange_client import ExchangeClient

_SUPPORTED_EXCHANGES = {"kraken"}


def build_client(exchange: str, api_key: str, api_secret: str) -> ExchangeClient:
    """
    Args:
        exchange: Nom de l'exchange -- seul "kraken" est supporté
            (insensible à la casse). "kraken" = Kraken FUTURES (voir
            execution/kraken_client.py, PAS Kraken Pro Spot/Marge --
            API et clés totalement différentes).
        api_key: Clé API Kraken en clair (voir .env :: KRAKEN_API_KEY).
        api_secret: Secret API Kraken en clair (voir .env :: KRAKEN_API_SECRET).

    Raises:
        ValueError: Si l'exchange demandé n'est pas "kraken" -- refus
            explicite plutôt que de deviner un comportement par défaut.
    """
    exchange_normalized = exchange.lower().strip()

    if exchange_normalized == "kraken":
        from execution.kraken_client import KrakenClient

        return KrakenClient(api_key, api_secret)

    raise ValueError(
        f"Exchange '{exchange}' non supporté -- seul 'kraken' est disponible depuis la "
        "simplification architecture (crypto/Kraken uniquement, voir config/alerts.yaml)."
    )
