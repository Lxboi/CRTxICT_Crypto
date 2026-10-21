"""
execution/exchange_client.py

Interface abstraite pour passer des ordres sur un exchange — définit
le CONTRAT que toute implémentation concrète doit respecter, sans
dépendre d'un exchange précis ici.

Implémentation concrète disponible : kraken_client.py (voir
client_factory.py -- seul exchange supporté depuis la simplification
architecture crypto-only, août 2026). Testez toute modification avec
des montants minuscules (ou l'environnement démo Kraken Futures)
avant tout usage avec de vrais fonds.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal


@dataclass
class OrderResult:
    """Résultat d'une tentative de passage d'ordre — toujours renvoyé, même en cas d'échec."""

    success: bool
    order_id: str | None
    filled_price: float | None
    filled_quantity: float | None
    error_message: str | None = None


@dataclass
class Position:
    """Position ouverte sur l'exchange, telle que rapportée par lui (pas déduite localement)."""

    symbol: str
    side: Literal["long", "short"]
    entry_price: float
    quantity: float
    unrealized_pnl: float
    leverage: float


class ExchangeClient(ABC):
    """
    Contrat que toute implémentation concrète d'exchange doit respecter.

    Principes de sécurité pour toute implémentation :
      - Ne JAMAIS lever d'exception non gérée depuis place_order() —
        toujours renvoyer un OrderResult(success=False, ...) en cas
        d'échec, pour que l'appelant (execution/engine.py) puisse
        réagir proprement sans crash.
      - get_open_positions() doit toujours interroger l'exchange
        directement (source de vérité), jamais un état local qui
        pourrait diverger de la réalité.
    """

    @abstractmethod
    def place_market_order(
        self, user_id: str, symbol: str, side: Literal["long", "short"], quantity: float,
        stop_loss: float, leverage: float, take_profit_levels: list[float] | None = None,
    ) -> OrderResult:
        """
        Place un ordre au marché avec un stop-loss attaché, au levier
        indiqué (calculé par execution/position_sizing.py::
        compute_required_leverage — jamais fixe/codé en dur ici). Doit
        renvoyer un OrderResult même en cas d'échec (jamais lever
        d'exception non gérée).

        `user_id` sert à persister l'identifiant de l'ordre stop créé
        (voir execution/stop_order_registry.py) — nécessaire pour que
        move_stop_loss() puisse le retrouver même après un redémarrage
        du processus entre l'entrée et un déplacement au Breakeven.

        `take_profit_levels`, si fourni, place EN PLUS un ordre take-
        profit réel (reduce-only) sur l'exchange pour CHAQUE niveau,
        dans l'ordre croissant de proximité (le premier de la liste =
        le plus proche) — divise la quantité en parts égales entre les
        niveaux (le dernier absorbe l'arrondi restant, pour que la
        somme corresponde exactement à `quantity`). Sécurité : si le
        robot s'arrête, ces ordres continuent d'exister sur l'exchange
        et s'exécutent normalement, sans dépendre du robot pour
        détecter le franchissement du prix. Les identifiants sont
        persistés (voir execution/tp_order_registry.py).
        """
        ...

    @abstractmethod
    def ensure_take_profit_orders(
        self, user_id: str, symbol: str, side: Literal["long", "short"],
        quantity: float, take_profit_levels: list[float],
    ) -> OrderResult:
        """
        Place les ordres take-profit RÉELS pour une position DÉJÀ
        OUVERTE qui n'en aurait pas (ou plus) — utilisé par
        main.py::_check_position_protection pour réparer une position
        dont les TP manqueraient (ex: échec de placement à l'entrée
        jamais rattrapé, ou robot arrêté avant que le placement initial
        n'ait pu se terminer). Idempotent par construction : à appeler
        UNIQUEMENT si execution/tp_order_registry.py::get_tp_order_ids
        renvoie une liste vide pour ce symbole — sinon, des TP
        existeraient déjà et cet appel en créerait des doublons.

        `quantity` doit être la quantité RÉELLEMENT ouverte au moment
        de l'appel (voir get_open_positions()), pas celle calculée à
        l'entrée d'origine — pour refléter d'éventuelles clôtures
        partielles déjà survenues entre-temps.
        """
        ...

    @abstractmethod
    def close_position(self, user_id: str, symbol: str, fraction: float = 1.0) -> OrderResult:
        """
        Ferme une FRACTION de la position ouverte sur ce symbole, au
        marché (1.0 = intégralement, valeur par défaut — comportement
        historique inchangé). `fraction` s'applique à la quantité
        RÉELLEMENT ouverte au moment de l'appel (interrogée fraîchement
        via get_open_positions(), jamais une valeur mise en cache) —
        des clôtures partielles successives (une par TP atteint)
        finissent donc toujours par fermer exactement ce qui reste,
        sans dérive d'arrondi cumulée.

        Lors d'une clôture INTÉGRALE (fraction >= 1.0), annule aussi
        tout ordre take-profit encore en attente pour ce symbole (voir
        execution/tp_order_registry.py) — sans ça, un TP non déclenché
        (ex: position fermée via le SL avant d'atteindre un TP plus
        lointain) resterait un ordre ORPHELIN sur l'exchange, qui
        pourrait s'exécuter à tort contre une FUTURE position ouverte
        plus tard sur le même symbole.
        """
        ...

    @abstractmethod
    def move_stop_loss(self, user_id: str, symbol: str, new_stop_loss: float) -> OrderResult:
        """
        Déplace le stop-loss d'une position déjà ouverte vers un
        nouveau niveau — utilisé pour la mécanique Breakeven (SL
        déplacé au prix d'entrée dès TP1 atteint, voir
        execution/engine.py::try_move_stop_to_breakeven). L'implémentation
        concrète doit annuler l'ordre stop existant (retrouvé via
        execution/stop_order_registry.py::get_stop_order_id(user_id,
        symbol) — PERSISTANT, survit à un redémarrage du processus)
        puis en replacer un nouveau, et renvoyer OrderResult(success=False,
        ...) si l'annulation réussit mais le replacement échoue — ne
        JAMAIS laisser la position sans stop-loss actif sans le signaler
        bruyamment à l'appelant.
        """
        ...

    @abstractmethod
    def get_open_positions(self) -> list[Position]:
        """Interroge l'exchange (pas un état local) pour la liste des positions réellement ouvertes."""
        ...

    @abstractmethod
    def get_account_balance(self) -> float:
        """Solde du compte (en devise de cotation, ex: USDT), tel que rapporté par l'exchange."""
        ...
