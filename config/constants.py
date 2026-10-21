"""
config/constants.py

Constantes partagées entre plusieurs modules du projet — centralisées
ici pour éviter qu'une même valeur (fuseau horaire, ordre des
timeframes, heures de session) soit dupliquée à plusieurs endroits et
risque de diverger silencieusement si l'une des copies est modifiée
sans les autres.

Ne contient QUE des constantes de code (jamais destinées à changer sans
une intervention délibérée). Les paramètres ajustables sans toucher au
code (seuils, fenêtres, etc.) restent dans config/alerts.yaml.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

# --- Fuseau horaire de référence ---
# Toute la logique temporelle du projet (sessions, KillZones, timing des
# CRT) raisonne en heure de New York, quel que soit le fuseau dans lequel
# les données sont stockées (UTC).
NY_TZ = ZoneInfo("America/New_York")

# --- Actif suivi ---
ASSET_SYMBOL = "BTC"
COINGECKO_ASSET_ID = "bitcoin"  # identifiant utilisé par l'API CoinGecko, différent du symbole

# --- Ordre des timeframes HTF, du plus haut au plus bas ---
# Utilisé par les règles qui itèrent sur plusieurs timeframes (CRT, DOL)
# pour parcourir MarketContext.candles dans un ordre cohérent.
TIMEFRAME_ORDER: tuple[str, ...] = ("monthly", "weekly", "daily")

# --- Fenêtres de session / KillZone (heure NY, début inclus, fin exclue) ---
# Valeurs partagées entre purge_rule.py (KillZones pour un CRT Daily) et
# narrative_ltf_rule.py (fenêtres de session pour les Intraday Profiles) :
# les deux raisonnent sur les mêmes créneaux Londres/New York.
LONDON_SESSION_HOURS_NY: tuple[int, int] = (2, 5)
NEW_YORK_SESSION_HOURS_NY: tuple[int, int] = (7, 10)
LONDON_CLOSE_SESSION_HOURS_NY: tuple[int, int] = (10, 12)
ASIA_SESSION_HOURS_NY: tuple[int, int] = (18, 24)
