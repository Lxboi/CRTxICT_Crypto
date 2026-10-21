"""
config/settings.py

Point unique de lecture des variables d'environnement (clés API,
tokens) utilisées par le projet. Plutôt que d'appeler os.getenv() dans
chaque module qui a besoin d'une clé, chaque appelant passe par
get_settings() — un seul endroit pour voir d'un coup d'œil quelles
clés existent et lesquelles sont réellement configurées.

Les valeurs sont lues à CHAQUE appel de get_settings() (pas mises en
cache au chargement du module), pour que l'ordre d'import n'ait pas
d'importance vis-à-vis de load_dotenv() dans main.py — peu coûteux
(de simples appels à os.getenv()), donc pas besoin d'optimiser.

Ne remplace pas config/alerts.yaml (paramètres de règles, ajustables
sans toucher au code) : ce module concerne exclusivement les secrets
et réglages d'environnement.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """
    Snapshot des variables d'environnement au moment de l'appel. Tous
    les champs sont optionnels (None si absents) : c'est à chaque
    appelant de décider s'il dégrade gracieusement ou s'il doit lever
    une erreur.
    """

    telegram_bot_token: str | None
    telegram_chat_id: str | None
    sosovalue_api_key: str | None
    polygon_api_key: str | None
    kraken_api_key: str | None
    kraken_api_secret: str | None

    @classmethod
    def from_env(cls) -> "Settings":
        # .strip() sur les identifiants d'exchange spécifiquement : un
        # espace ou retour à la ligne parasite copié donne exactement
        # une erreur "clé invalide" -- la clé semble correcte à l'œil
        # nu, mais ne correspond plus une fois envoyée. Pas de STRIP
        # sur les autres clés (Telegram/Polygon/SoSoValue) : leurs API
        # sont moins strictes typiquement, mais ça ne coûte rien de
        # garder cette protection ciblée sur l'exchange pour l'instant.
        kraken_api_key = os.getenv("KRAKEN_API_KEY")
        kraken_api_secret = os.getenv("KRAKEN_API_SECRET")
        return cls(
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
            sosovalue_api_key=os.getenv("SOSOVALUE_API_KEY"),
            polygon_api_key=os.getenv("POLYGON_API_KEY"),
            kraken_api_key=kraken_api_key.strip() if kraken_api_key else kraken_api_key,
            kraken_api_secret=kraken_api_secret.strip() if kraken_api_secret else kraken_api_secret,
        )


def get_settings() -> Settings:
    """Point d'entrée principal : lit l'environnement et renvoie un Settings à jour."""
    return Settings.from_env()
