"""
rules/entry_rule.py

Détecte les points d'entrée fins, en complément du DOL/CRT/Purge déjà
gérés par les autres règles :

1. Entrée "bougie de distribution" (bougie 3) : sur un CRT confirmé au
   sens strict (ancre -> confirmation immédiate, sans containment
   prolongé), la bougie suivant directement la confirmation est
   l'entrée la plus sûre selon la stratégie documentée. Une entrée
   "quasi au marché" (bougie 2) est aussi proposée quand la bougie de
   confirmation vient tout juste de clore et que la bougie 3 n'est pas
   encore formée — un compromis rapidité/précision.

2. Entrée "Turtle Soup" (TS) : repère le True Day Open (minuit heure
   NY), les liquidity pools de la période précédente (High/Low) et de
   la session asiatique, attend qu'un de ces niveaux soit balayé
   (sweep) avec rejet (clôture de l'autre côté) en confluence avec un
   Key Level de la bonne direction, puis évalue 4 modèles de
   confirmation.

IMPORTANT — ne calcule AUCUN stop-loss ni take-profit : cette règle ne
fait que signaler des points d'entrée et leur contexte. Le
dimensionnement du risque (SL) est un développement séparé, prévu plus
tard.

HYPOTHÈSE DOCUMENTÉE à valider avec l'utilisateur : la confluence KL
"dans le sens opposé de l'open daily" est interprétée ici comme :
- TS bullish (renversement haussier attendu) -> le prix doit être
  ACTUELLEMENT EN DESSOUS du True Day Open au moment du sweep (le
  marché est en discount par rapport à l'ouverture du jour, cohérent
  avec un retour à la hausse qui ramènerait le prix au-dessus).
- TS bearish -> symétriquement, prix AU-DESSUS du True Day Open.
Si cette lecture ne correspond pas à l'intention, il suffit d'ajuster
_check_kl_confluence() ci-dessous.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, ClassVar, Literal

from config.constants import ASIA_SESSION_HOURS_NY, NY_TZ
from models.market_data import Candle
from rules.crt_rule import CRT_MAX_ANCHOR_AGE_BY_TIMEFRAME, CrtPattern, _detect_crt_pattern
from rules.engine import BaseRule, MarketContext, RuleResult, resolve_now
from rules.keylevel_rule import KeyLevelZone, find_key_levels

Bias = Literal["bullish", "bearish"]

DEFAULT_MIN_CANDLES_REQUIRED = 20


# ---------------------------------------------------------------------
# 1. Entrée bougie de distribution (bougie 3 après confirmation bougie 2)
# ---------------------------------------------------------------------


@dataclass
class DistributionEntry:
    """
    Timing d'entrée sur la bougie de distribution (bougie 3), la plus
    sûre selon la stratégie documentée — ou son anticipation sur la
    bougie de confirmation (bougie 2) si la bougie 3 n'est pas encore
    formée.

    Attributes:
        predicted: True si la bougie 3 n'est pas encore formée (le prix
            affiché est une anticipation, pas une entrée déjà réalisée) ;
            False si la bougie 3 a déjà clôturé (entrée historique,
            utile pour du backtest).
        entry_price: Prix d'entrée proposé — close de la bougie 2 si
            predicted=True (proxy du marché actuel), open de la bougie
            3 sinon.
        stop_loss: Niveau de stop — le High (setup bearish) ou le Low
            (setup bullish) de la bougie 2 (la purge qui valide le
            CRT), conformément à la règle indiquée : le SL des modèles
            classiques se place au H/L de la bougie de confirmation.
        confirmation_candle_index: Index de la bougie de confirmation
            (bougie 2) dans la série fournie.
    """

    predicted: bool
    entry_price: float
    stop_loss: float
    confirmation_candle_index: int


def _compute_distribution_entry(
    crt: CrtPattern, candles: list[Candle]
) -> DistributionEntry | None:
    """
    Détermine l'entrée bougie 3 (distribution) à partir d'un CRT déjà
    détecté, UNIQUEMENT si la confirmation (bougie 2 = purge_candle) a
    eu lieu immédiatement après l'ancre (bougie 1), sans containment
    prolongé entre les deux — sinon la notion de "bougie 2"/"bougie 3"
    telle que documentée ne s'applique pas (ce serait une bougie N,
    pas nécessairement la 2ème après l'ancre).

    Args:
        crt: Le CrtPattern détecté (voir rules/crt_rule.py).
        candles: La même liste de bougies closes utilisée pour détecter
            ce CRT (nécessaire pour localiser la bougie suivant la
            confirmation).

    Returns:
        None si le CRT n'est pas un cas strict "ancre + confirmation
        immédiate" (containment prolongé avant la purge), sinon un
        DistributionEntry.
    """
    try:
        anchor_index = candles.index(crt.anchor_candle)
        purge_index = candles.index(crt.purge_candle)
    except ValueError:
        return None

    if purge_index != anchor_index + 1:
        # Containment prolongé avant la purge : pas le cas strict
        # "bougie 2" documenté, on ne propose pas d'entrée distribution.
        return None

    # SL = H/L de la bougie 2 (la purge qui valide le CRT) : le Low pour
    # un setup bullish (le prix ne doit pas revenir sous ce point), le
    # High pour un setup bearish.
    stop_loss = crt.purge_candle.low if crt.bias == "bullish" else crt.purge_candle.high

    if purge_index == len(candles) - 1:
        # La confirmation vient de clore, bougie 3 pas encore formée
        # -> entrée anticipée, proche du marché actuel.
        return DistributionEntry(
            predicted=True,
            entry_price=crt.purge_candle.close,
            stop_loss=stop_loss,
            confirmation_candle_index=purge_index,
        )

    # Bougie 3 déjà formée (et close, puisque `candles` ne contient que
    # des bougies closes) -> entrée historique à son prix d'ouverture.
    distribution_candle = candles[purge_index + 1]
    return DistributionEntry(
        predicted=False,
        entry_price=distribution_candle.open,
        stop_loss=stop_loss,
        confirmation_candle_index=purge_index,
    )


# ---------------------------------------------------------------------
# 2. Turtle Soup : True Day Open, liquidity pools, sweep, 4 modèles
# ---------------------------------------------------------------------


def _get_true_day_open(candles: list[Candle], for_date: date) -> Candle | None:
    """
    Trouve la bougie représentant le True Day Open (minuit heure NY) du
    jour `for_date`. Nécessite des bougies à granularité fine (1h côté
    côté Binance) alignées sur la grille horaire NY — si aucune bougie
    ne tombe exactement à minuit (ex: trou de données), renvoie None
    plutôt que d'approximer silencieusement.
    """
    for candle in candles:
        local = candle.timestamp.astimezone(NY_TZ)
        if local.date() == for_date and local.hour == 0 and local.minute == 0:
            return candle
    return None


def _get_period_high_low(
    candles: list[Candle], start_ny: datetime, end_ny: datetime
) -> tuple[float, float] | None:
    """High/Low sur une fenêtre [start_ny, end_ny) en heure NY. None si aucune bougie dans la fenêtre."""
    relevant = [c for c in candles if start_ny <= c.timestamp.astimezone(NY_TZ) < end_ny]
    if not relevant:
        return None
    return max(c.high for c in relevant), min(c.low for c in relevant)


def _get_liquidity_pools(candles: list[Candle], today: date) -> dict[str, float]:
    """
    Construit le dict des liquidity pools de référence : High/Low de la
    journée précédente (00:00 -> 00:00 NY) et de la session asiatique la
    plus récente (18:00 -> 00:00 NY, la veille).

    Returns:
        Dict {"PDH": ..., "PDL": ..., "Asia High": ..., "Asia Low": ...}
        — une clé est absente si la fenêtre correspondante n'a aucune
        donnée disponible.
    """
    pools: dict[str, float] = {}

    yesterday = today - timedelta(days=1)
    day_start = datetime(yesterday.year, yesterday.month, yesterday.day, tzinfo=NY_TZ)
    day_end = datetime(today.year, today.month, today.day, tzinfo=NY_TZ)
    previous_day = _get_period_high_low(candles, day_start, day_end)
    if previous_day is not None:
        pools["PDH"], pools["PDL"] = previous_day

    asia_start_hour, _asia_end_hour = ASIA_SESSION_HOURS_NY
    asia_start = datetime(
        yesterday.year, yesterday.month, yesterday.day, asia_start_hour, tzinfo=NY_TZ
    )
    # La borne de fin de session asiatique (minuit) coïncide avec day_end.
    asia_end = day_end
    asia = _get_period_high_low(candles, asia_start, asia_end)
    if asia is not None:
        pools["Asia High"], pools["Asia Low"] = asia

    return pools


@dataclass
class TurtleSoupEvent:
    """
    Un Turtle Soup confirmé : la dernière bougie disponible a balayé un
    niveau de liquidité connu (mèche au-delà) puis clôturé de l'autre
    côté (rejet) — signe classique d'un piège à la liquidité annonçant
    un renversement dans la direction opposée au balayage.
    """

    direction: Bias
    swept_level_name: str
    swept_level_price: float
    sweep_candle_index: int
    sweep_candle: Candle


def _detect_turtle_soup(
    candles: list[Candle], liquidity_pools: dict[str, float]
) -> TurtleSoupEvent | None:
    """
    Détecte un Turtle Soup sur la DERNIÈRE bougie disponible (pour
    repérer un sweep qui vient tout juste de se produire). Une mèche
    au-delà d'un niveau "High" (PDH/Asia High) avec clôture en dessous
    -> renversement bearish attendu ; symétriquement pour un niveau
    "Low" avec clôture au-dessus -> renversement bullish attendu.
    """
    if not candles:
        return None

    last = candles[-1]
    last_index = len(candles) - 1

    for name, level in liquidity_pools.items():
        is_high_level = name.endswith("H") or "High" in name
        if is_high_level and last.high > level and last.close < level:
            return TurtleSoupEvent(
                direction="bearish",
                swept_level_name=name,
                swept_level_price=level,
                sweep_candle_index=last_index,
                sweep_candle=last,
            )
        if not is_high_level and last.low < level and last.close > level:
            return TurtleSoupEvent(
                direction="bullish",
                swept_level_name=name,
                swept_level_price=level,
                sweep_candle_index=last_index,
                sweep_candle=last,
            )

    return None


def _check_kl_confluence(
    ts_event: TurtleSoupEvent,
    kl_zones: list[KeyLevelZone],
    true_day_open_price: float | None,
) -> KeyLevelZone | None:
    """
    Vérifie la confluence KL requise : un KL de la même direction que
    le renversement attendu, touché par la bougie du TS, ET le prix
    actuel du bon côté du True Day Open (voir l'hypothèse documentée en
    tête de fichier). Renvoie None si le True Day Open est indisponible
    (pas de bougie à minuit dans les données) plutôt que de deviner.
    """
    if true_day_open_price is None:
        return None

    current_price = ts_event.sweep_candle.close
    if ts_event.direction == "bullish" and current_price >= true_day_open_price:
        return None
    if ts_event.direction == "bearish" and current_price <= true_day_open_price:
        return None

    touched = [
        zone
        for zone in kl_zones
        if zone.direction == ts_event.direction
        and zone.zone_low <= current_price <= zone.zone_high
    ]
    return touched[0] if touched else None


@dataclass
class EntryModel:
    """
    Un des 4 modèles d'entrée déclenchés après un Turtle Soup confirmé.

    stop_loss : H/L de la bougie du TS elle-même — dans cette
    micro-structure, la bougie du TS joue le rôle de "bougie 2" (la
    purge qui valide le pattern), donc même règle que pour l'entrée
    distribution : Low pour un setup bullish, High pour un setup
    bearish. Identique pour les 4 modèles (ils partagent le même TS).
    """

    model_number: int
    model_name: str
    direction: Bias
    entry_price: float
    stop_loss: float
    confirmation_zone: dict[str, Any] | None = None


def _detect_entry_models(
    ts_event: TurtleSoupEvent, kl_zones: list[KeyLevelZone]
) -> list[EntryModel]:
    """
    Détecte les modèles 1/2/3 (OB/FVG/BRK valides formés APRÈS le TS,
    même direction que le renversement attendu) et ajoute toujours le
    modèle 4 (entrée agressive au marché, sans attendre de
    confirmation).

    "Après le TS" est vérifié via KeyLevelZone.formed_at, comparé à
    l'horodatage de la bougie du TS — les zones formées avant ne
    comptent pas, conformément à la consigne reçue.
    """
    models: list[EntryModel] = []

    # SL commun aux 4 modèles : H/L de la bougie du TS (la "bougie 2"
    # de cette micro-structure).
    stop_loss = (
        ts_event.sweep_candle.low if ts_event.direction == "bullish" else ts_event.sweep_candle.high
    )

    post_ts_zones = [
        zone
        for zone in kl_zones
        if zone.formed_at >= ts_event.sweep_candle.timestamp
        and zone.direction == ts_event.direction
    ]

    kind_to_model = {
        "order_block": (1, "Order Block"),
        "fvg": (2, "FVG"),
        "breaker_block": (3, "Breaker Block"),
    }

    for kind, (model_number, model_name) in kind_to_model.items():
        match = next((zone for zone in post_ts_zones if zone.kind == kind), None)
        if match is not None:
            entry_price = match.zone_high if ts_event.direction == "bullish" else match.zone_low
            models.append(
                EntryModel(
                    model_number=model_number,
                    model_name=model_name,
                    direction=ts_event.direction,
                    entry_price=entry_price,
                    stop_loss=stop_loss,
                    confirmation_zone={
                        "kind": match.kind,
                        "zone_low": match.zone_low,
                        "zone_high": match.zone_high,
                        "formed_at": match.formed_at.isoformat(),
                    },
                )
            )

    # Modèle 4 : toujours disponible, entrée agressive directement sur
    # le TS, sans attendre de confirmation structurelle.
    models.append(
        EntryModel(
            model_number=4,
            model_name="Agressif (marché, sans confirmation)",
            direction=ts_event.direction,
            entry_price=ts_event.sweep_candle.close,
            stop_loss=stop_loss,
        )
    )

    return models


# ---------------------------------------------------------------------
# Règle
# ---------------------------------------------------------------------


class EntryRule(BaseRule):
    """
    Règle de détection des points d'entrée fins : bougie de distribution
    (CRT strict) et Turtle Soup (liquidity pools + KL + 4 modèles).

    Ne calcule aucun SL/TP — signale uniquement des points d'entrée et
    leur contexte. Opère sur la timeframe "intraday" de MarketContext
    (True Day Open, sessions, et liquidity pools n'ont de sens qu'à
    cette granularité ; une future extension pourrait généraliser aux
    paires HTF/LTF plus larges, ex: weekly->daily, en substituant "jour"
    par "période" et en omettant la session asiatique).
    """

    name: ClassVar[str] = "entry_rule"

    def evaluate(self, context: MarketContext, config: dict[str, Any]) -> RuleResult:
        cfg = config.get("entry_rule", {})
        min_candles_required: int = cfg.get("min_candles_required", DEFAULT_MIN_CANDLES_REQUIRED)
        keylevel_cfg = config.get("keylevel_rule", {})

        if not context.candles or "intraday" not in context.candles:
            return RuleResult(
                rule_name=self.name,
                triggered=False,
                message="Historique intraday indisponible pour la détection d'entrée",
            )

        intraday_series = context.candles["intraday"]
        closed_candles = intraday_series.candles[:-1]

        if len(closed_candles) < min_candles_required:
            return RuleResult(
                rule_name=self.name,
                triggered=False,
                message="Historique intraday insuffisant pour la détection d'entrée",
            )

        current_price = context.market_data.price_usd
        # resolve_now() : context.simulated_now si fourni (backtest), sinon
        # l'horloge réelle (live, comportement inchangé) -- voir rules/engine.py.
        now_ny = resolve_now(context)
        today = now_ny.date()

        # --- 1. Entrée bougie de distribution (sur le CRT daily, si détecté) ---
        distribution_entry: DistributionEntry | None = None
        daily_series = context.candles.get("daily")
        if daily_series is not None and len(daily_series.candles) >= 3:
            daily_closed = daily_series.candles[:-1]
            daily_crt = _detect_crt_pattern(
                daily_closed,
                current_price=current_price,
                max_anchor_age_candles=CRT_MAX_ANCHOR_AGE_BY_TIMEFRAME.get("daily"),
            )
            if daily_crt is not None:
                distribution_entry = _compute_distribution_entry(daily_crt, daily_closed)

        # --- 2. Turtle Soup : liquidity pools, sweep, confluence KL, 4 modèles ---
        liquidity_pools = _get_liquidity_pools(closed_candles, today)
        ts_event = _detect_turtle_soup(closed_candles, liquidity_pools) if liquidity_pools else None

        entry_models: list[EntryModel] = []
        kl_confluence: KeyLevelZone | None = None
        true_day_open = _get_true_day_open(closed_candles, today)

        if ts_event is not None:
            kl_zones = find_key_levels(closed_candles, keylevel_cfg)
            kl_confluence = _check_kl_confluence(
                ts_event, kl_zones, true_day_open.open if true_day_open else None
            )
            if kl_confluence is not None:
                entry_models = _detect_entry_models(ts_event, kl_zones)

        triggered = distribution_entry is not None or bool(entry_models)

        message_parts = []
        if distribution_entry is not None:
            timing = (
                "anticipée (bougie 3 pas encore formée)"
                if distribution_entry.predicted
                else "bougie 3 déjà formée"
            )
            message_parts.append(
                f"Entrée distribution disponible ({timing}) @ ${distribution_entry.entry_price:,.2f}"
            )
        if entry_models:
            models_label = ", ".join(f"Modèle {m.model_number} ({m.model_name})" for m in entry_models)
            message_parts.append(
                f"Turtle Soup confirmé ({ts_event.swept_level_name} @ ${ts_event.swept_level_price:,.2f}) "
                f"-> {models_label}"
            )
        elif ts_event is not None:
            message_parts.append(
                f"Turtle Soup détecté ({ts_event.swept_level_name}) mais confluence "
                "KL/True Day Open non validée"
            )

        message = " | ".join(message_parts) if message_parts else "Aucun point d'entrée détecté ce cycle"

        return RuleResult(
            rule_name=self.name,
            triggered=triggered,
            message=message,
            severity="warning" if triggered else "info",
            metadata={
                "distribution_entry": (
                    {
                        "predicted": distribution_entry.predicted,
                        "direction": daily_crt.bias,
                        "entry_price": distribution_entry.entry_price,
                        "stop_loss": distribution_entry.stop_loss,
                    }
                    if distribution_entry
                    else None
                ),
                "liquidity_pools": liquidity_pools,
                "true_day_open": true_day_open.open if true_day_open else None,
                "turtle_soup": (
                    {
                        "direction": ts_event.direction,
                        "swept_level_name": ts_event.swept_level_name,
                        "swept_level_price": ts_event.swept_level_price,
                    }
                    if ts_event
                    else None
                ),
                "kl_confluence": (
                    {
                        "kind": kl_confluence.kind,
                        "zone_low": kl_confluence.zone_low,
                        "zone_high": kl_confluence.zone_high,
                    }
                    if kl_confluence
                    else None
                ),
                "entry_models": [
                    {
                        "model_number": m.model_number,
                        "model_name": m.model_name,
                        "direction": m.direction,
                        "entry_price": m.entry_price,
                        "stop_loss": m.stop_loss,
                        "confirmation_zone": m.confirmation_zone,
                    }
                    for m in entry_models
                ],
            },
        )
