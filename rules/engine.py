"""
rules/engine.py

Moteur de règles métier : orchestre l'exécution des différentes règles
(DOL, Narrative, KeyLevel, CRT, Purge) et détermine si une alerte doit être
déclenchée.

Important : ce module ne prend AUCUNE décision basée sur l'IA. Aucune IA
n'intervient nulle part dans le pipeline (testée puis entièrement
retirée, jugée trop imprécise) — la décision de déclenchement d'une
alerte repose exclusivement sur la logique métier définie dans les
classes de règles, et le message final est construit de façon 100%
déterministe par `messaging/formatter.py`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field

from config.constants import NY_TZ
from models.macro_data import MacroEvent
from models.market_data import CandleSeries, MarketData


class MarketContext(BaseModel):
    """
    Regroupe l'ensemble des données disponibles pour une exécution du
    moteur de règles.

    Ce modèle est le point d'extension central de l'architecture : toute
    nouvelle source de données (actualités, données macro, etc.)
    s'ajoute ici en tant que champ optionnel, sans jamais casser la
    signature de BaseRule.evaluate() ni celle des règles déjà écrites.

    Attributes:
        market_data: Instantané courant des données de marché de l'actif analysé.
        candles: Historique de bougies par timeframe, sous la forme
            {"monthly": CandleSeries, "weekly": CandleSeries,
            "daily": CandleSeries, "intraday": CandleSeries,
            "m15": CandleSeries}. La stratégie étant fractale (même
            logique quelle que soit la timeframe, mais Monthly domine
            Weekly qui domine Daily), les règles qui en ont besoin (CRT,
            Purge, DOL) itèrent sur ce dict plutôt que de recevoir une
            série unique. "intraday" (1h, ~42 jours de profondeur) sert
            à EntryRule/TsqRule (Turtle Soup, True Day Open) ; "m15" (15
            min, ~10 jours de profondeur, séparée pour ne pas réduire la
            profondeur de "intraday") sert à PurgeRule pour le
            resampling précis en CRT 4H et la résolution fine du
            "moment de purge" (KillZone/Algo Times). None si aucun
            historique n'a été fourni.
        economic_events: Calendrier macro-économique (SoSoValue), utilisé
            par NarrativeRule pour signaler les événements du jour. Ne
            porte aucune analyse d'impact — purement informatif. None
            si non fourni.
        symbol: Symbole de l'actif principal analysé dans ce contexte
            (ex: "BTC", "ETH"). Permet au pipeline multi-paires de
            savoir quel actif est traité, et sert à identifier les
            décisions/alertes en base par paire.
        secondary_symbol: Symbole de la paire corrélée utilisée pour le
            SMT (ex: "ETH" quand symbol="BTC"). None si aucune paire
            corrélée n'est configurée pour le SMT.
        secondary_candles: Historique de bougies de la paire corrélée,
            même structure que `candles` ({"monthly": ..., "weekly":
            ..., "daily": ..., "intraday": ...}). Utilisé uniquement
            par NarrativeRule pour la détection de divergence SMT.
            None si non fourni.
        simulated_now: Horodatage "actuel" à utiliser À LA PLACE de
            `datetime.now(NY_TZ)` (ex: rules/entry_rule.py::EntryRule,
            rules/tsq_rule.py::TsqRule -- True Day Open, sessions,
            liquidity pools). None en LIVE (comportement inchangé,
            l'horloge réelle est utilisée) -- réservé au backtest, qui
            doit pouvoir rejouer ces règles à un instant simulé sans
            fuiter l'horloge réelle du conteneur (voir CLAUDE.md,
            correctif du backtest intraday). Any tz acceptée en entrée,
            convertie en NY_TZ par l'appelant.
    """

    market_data: MarketData
    candles: dict[str, CandleSeries] | None = None
    economic_events: list[MacroEvent] | None = None
    symbol: str = "BTC"
    secondary_symbol: str | None = None
    secondary_candles: dict[str, CandleSeries] | None = None
    simulated_now: datetime | None = None


def resolve_now(context: MarketContext) -> datetime:
    """
    Horodatage NY "actuel" à utiliser par une règle qui a besoin de savoir
    quel jour on est (ex: rules/entry_rule.py::EntryRule, rules/tsq_rule.py
    ::TsqRule -- True Day Open, sessions, liquidity pools) : `context.
    simulated_now` s'il est fourni (converti en NY_TZ, quelle que soit sa
    tz d'origine), sinon l'horloge réelle du conteneur (`datetime.now
    (NY_TZ)`, comportement live inchangé). Centralisé ici (plutôt que
    dupliqué dans chaque règle) pour qu'un futur simulateur de backtest
    n'ait qu'un seul point à câbler, et pour rester testable isolément.
    """
    if context.simulated_now is not None:
        return context.simulated_now.astimezone(NY_TZ)
    return datetime.now(NY_TZ)


class RuleResult(BaseModel):
    """
    Résultat structuré renvoyé par une règle après évaluation.

    Attributes:
        rule_name: Nom identifiant la règle (ex: "dol_rule").
        triggered: True si la règle considère qu'une alerte doit être levée.
        message: Message explicatif lisible, utile pour le contexte envoyé
            à l'IA et pour l'historisation en base de données.
        severity: Niveau de gravité de la règle si déclenchée.
        metadata: Données additionnelles libres (ex: niveaux de prix
            concernés, seuils dépassés) utiles au débogage et à l'IA.
    """

    rule_name: str
    triggered: bool
    message: str | None = None
    severity: Literal["info", "warning", "critical"] = "info"
    metadata: dict[str, Any] = Field(default_factory=dict)


class BaseRule(ABC):
    """
    Interface commune que toute règle métier doit implémenter.

    Chaque règle concrète (DolRule, NarrativeRule, KeyLevelRule, CrtRule,
    PurgeRule) doit :
      - définir un attribut de classe `name` unique ;
      - implémenter `evaluate()`, qui reçoit les données de marché et une
        configuration, et renvoie systématiquement un RuleResult (avec
        `triggered=False` si la règle ne se déclenche pas).
    """

    name: ClassVar[str]

    @abstractmethod
    def evaluate(self, context: MarketContext, config: dict[str, Any]) -> RuleResult:
        """
        Évalue la règle sur la base du contexte de marché fourni.

        Args:
            context: Contexte de marché courant (instantané + historique
                de bougies éventuel). Une règle qui n'a pas besoin d'un
                champ (ex: les bougies) peut simplement l'ignorer.
            config: Configuration de la règle (seuils, paramètres), issue
                par exemple de config/alerts.yaml.

        Returns:
            Un RuleResult indiquant si la règle est déclenchée ou non.
        """
        raise NotImplementedError
