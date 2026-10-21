"""
messaging/formatter.py

Compose le message final destiné à Telegram : titre (actif + biais),
puis un résumé structuré et EXACT des éléments qui ont mené à la
décision (DOL long terme, Narrative, KL, CRT, Purge), suivi des
take-profits. Ces informations proviennent directement des champs
structurés du DecisionReport, jamais d'un parsing de texte — le format
est volontairement concis (l'utilisateur veut l'essentiel, pas le
journal de raisonnement complet).

Le texte généré par l'IA n'est inclus que s'il apporte réellement
quelque chose (l'IA a répondu normalement) ; en cas de repli sur
report.to_text() (IA indisponible/échec), il est omis pour éviter de
dupliquer le résumé structuré qui couvre déjà l'essentiel plus
proprement.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from config.constants import NY_TZ
from models.market_data import format_price_adaptive
from rules.crt_rule import MAX_ANCHOR_DISTANCE_RATIO
from rules.decision_engine import DecisionReport, TimeframeSetup

BIAS_EMOJI: dict[str, str] = {"bullish": "🟢", "bearish": "🔴"}
DIRECTION_LABEL: dict[str, str] = {"bullish": "LONG", "bearish": "SHORT"}
DISCLAIMER = "_Analyse technique automatisée — ne constitue pas un conseil financier._"

# Noms lisibles pour les profils narrative HTF (les clés internes sont
# en snake_case/techniques — voir rules/narrative_rule.py::WEEKLY_PROFILE_SIGNATURES).
DAY_ABBREVIATIONS: dict[int, str] = {0: "Lun", 1: "Mar", 2: "Mer", 3: "Jeu", 4: "Ven", 5: "Sam", 6: "Dim"}

NARRATIVE_DISPLAY_NAMES: dict[str, str] = {
    "CEW1": "CEW1 (Consolidation-Expansion Week 1)",
    "CEW2": "CEW2 (Consolidation-Expansion Week 2)",
    "midweek_reversal": "Midweek Reversal",
    "consolidation_reversal": "Consolidation Reversal",
}

DOL_SOURCE_LABELS: dict[str, str] = {
    "bias": "Cassure de biais",
    "double_purge": "Double Purge",
    "crt": "CRT",
}

# Noms lisibles pour les types de Key Level (les clés internes sont en
# snake_case — voir rules/keylevel_rule.py::KeyLevelKind). Élimine au
# passage les underscores (order_block, old_high, etc.) qui cassaient
# le parsing Markdown de Telegram avant échappement.
KL_KIND_DISPLAY_NAMES: dict[str, str] = {
    "fvg": "FVG",
    "old_high": "Ancien High",
    "old_low": "Ancien Low",
    "order_block": "Order Block",
    "breaker_block": "Breaker Block",
    "rejection_block": "Rejection Block",
}

# Caractères spéciaux du mode Markdown "legacy" de Telegram (pas
# MarkdownV2). Un "[" isolé, par exemple, est interprété comme le début
# d'un lien [texte](url) — sans fermeture au bon format, Telegram
# renvoie "can't find end of the entity". Le texte dynamique (généré
# par l'IA) doit donc être échappé avant insertion dans le message.
_MARKDOWN_SPECIAL_CHARS = ("_", "*", "`", "[")


def _format_price(price: float) -> str:
    """Voir models/market_data.py::format_price_adaptive (partagée avec le log interne)."""
    return format_price_adaptive(price)


def _escape_markdown_legacy(text: str) -> str:
    """
    Échappe les caractères spéciaux Markdown dans du texte dynamique,
    pour qu'ils s'affichent littéralement au lieu d'être interprétés
    comme de la mise en forme (et potentiellement casser le parsing
    Telegram si mal appairés).
    """
    for char in _MARKDOWN_SPECIAL_CHARS:
        text = text.replace(char, f"\\{char}")
    return text


def _format_dol_long_terme(report: DecisionReport) -> str:
    """
    DOL long terme retenue, avec justification explicite de la
    timeframe qui l'a déterminée (cascade Monthly -> Weekly -> Daily,
    voir rules/decision_engine.py::_resolve_timeframe_bias) : le
    mécanisme source (cassure de biais / CRT / double purge), son prix,
    et — si la timeframe Monthly n'a pas fourni de biais clair
    (souvent car sa DOL a déjà été liquidée ou ses mécanismes se
    contredisent) — pourquoi on est descendu à Weekly, voire Daily.
    """
    if not report.effective_bias or report.bias_source_timeframe is None:
        return "Aucune DOL long terme active (Monthly, Weekly et Daily sans biais clair)"

    candidates_by_timeframe = {
        "monthly": report.monthly_dol_candidates,
        "weekly": report.weekly_dol_candidates,
        "daily": report.daily_dol_candidates,
    }
    source_timeframe = report.bias_source_timeframe
    candidates = candidates_by_timeframe.get(source_timeframe, [])
    match = next((c for c in candidates if c.get("bias") == report.effective_bias), None)
    if match is None:
        return "Aucune DOL long terme active"

    source_label = DOL_SOURCE_LABELS.get(match["source"], match["source"])
    timeframe_label = source_timeframe.capitalize()
    text = f"{timeframe_label} — {source_label} @ {_format_price(match['dol_price'])}"

    if source_timeframe == "monthly" and report.monthly_liquidated_zones:
        kinds = ", ".join(
            KL_KIND_DISPLAY_NAMES.get(z["kind"], z["kind"]) for z in report.monthly_liquidated_zones
        )
        text += f" (zone liquidée : {kinds})"
    elif source_timeframe == "weekly":
        text += " — Monthly sans biais clair, on descend d'une timeframe"
    elif source_timeframe == "daily":
        text += " — Monthly et Weekly sans biais clair, on descend jusqu'en Daily"

    return _escape_markdown_legacy(text)


def _format_narrative(report: DecisionReport) -> str:
    """
    Nom de la narrative HTF retenue si un profil est pleinement
    confirmé (confiance = 1.0) ; sinon, liste les configurations
    candidates avec leur niveau de confiance, ET la séquence de rôles
    (range/purge/expansion/correction) réellement observée jour par
    jour cette semaine — pour que la justification soit vérifiable, et
    que si le scénario évolue d'un cycle à l'autre (ex: un candidat
    CEW2 qui devient consolidation_reversal), on voit directement dans
    quelle mesure les jours observés eux-mêmes ont changé, plutôt qu'une
    liste de noms de scénarios sans lien apparent entre eux.
    """
    profiles = report.weekly_narrative_profiles
    if not profiles:
        return "Aucune configuration détectée cette semaine"

    observed_label = ""
    if report.weekly_day_roles:
        roles = ", ".join(
            f"{DAY_ABBREVIATIONS.get(d['weekday'], '?')}={d['role']}" for d in report.weekly_day_roles
        )
        observed_label = f" (jours observés : {roles})"

    best_name, best_confidence = max(profiles.items(), key=lambda kv: kv[1])
    if best_confidence >= 1.0:
        return _escape_markdown_legacy(
            f"{NARRATIVE_DISPLAY_NAMES.get(best_name, best_name)}{observed_label}"
        )

    candidates = sorted(profiles.items(), key=lambda kv: kv[1], reverse=True)
    candidates_label = ", ".join(
        f"{NARRATIVE_DISPLAY_NAMES.get(name, name)} ({confidence:.0%})"
        for name, confidence in candidates
    )
    provisional_note = (
        " — provisoire, peut encore évoluer selon les prochains jours"
        if len(report.weekly_day_roles) < 5
        else ""
    )
    return _escape_markdown_legacy(
        f"Aucun scénario confirmé — candidats en cours : {candidates_label}"
        f"{observed_label}{provisional_note}"
    )


def _format_narrative_ltf(report: DecisionReport) -> list[str]:
    """
    Regroupe, dans le même "onglet" que la narrative, tout le contexte
    LTF pertinent : profils intraday, dealing range LTF, KL LTF proches
    du prix, double purge LTF, CRT LTF (déjà filtré dans le sens de la
    DOL HTF par decision_engine.py), et cohérence OHLC de la journée en
    cours.

    Returns:
        Une liste de lignes à insérer dans le message — vide si rien
        de pertinent à signaler (pas de bruit les cycles sans signal LTF).
    """
    lines: list[str] = []

    news = report.high_impact_news
    if news.get("high_impact_today"):
        events_label = _escape_markdown_legacy(", ".join(news["high_impact_today"]))
        if news.get("currently_in_avoid_window"):
            calendar_status = f"⚠️ Fenêtre à risque active ({events_label}) — volatilité accrue possible"
        else:
            calendar_status = f"⚠️ News à haut impact aujourd'hui : {events_label}"
    else:
        calendar_status = "RAS"
    lines.append(f"  • *Calendrier économique* : {calendar_status}")

    smt = report.smt_context
    if smt and smt.get("available") and smt.get("divergence"):
        divergence = smt["divergence"]
        lines.append(f"  • *SMT* (vs {smt['secondary_symbol']}) : {divergence['detail']}")

    ltf_profiles = report.narrative_ltf_profiles
    if ltf_profiles:
        best_name, best_confidence = max(ltf_profiles.items(), key=lambda kv: kv[1])
        if best_confidence >= 1.0:
            lines.append(f"  • *Profil intraday* : {_escape_markdown_legacy(best_name)}")
        else:
            candidates_label = ", ".join(
                f"{name} ({confidence:.0%})"
                for name, confidence in sorted(ltf_profiles.items(), key=lambda kv: kv[1], reverse=True)
            )
            lines.append(f"  • *Profils intraday candidats* : {_escape_markdown_legacy(candidates_label)}")

    dealing_range = report.dealing_range_ltf
    if dealing_range:
        lines.append(
            f"  • *Dealing range LTF* : {_format_price(dealing_range['range_low'])} - "
            f"{_format_price(dealing_range['range_high'])} "
            f"(zone : {_escape_markdown_legacy(str(dealing_range['zone']))})"
        )

    if report.double_purge_ltf:
        dp = report.double_purge_ltf
        ref_time = datetime.fromisoformat(dp["reference_timestamp"]).astimezone(NY_TZ)
        lines.append(
            f"  • *Double purge LTF* détecté — bougie de référence : "
            f"{ref_time.strftime('%d/%m %Hh')} (heure NY), timeframe {dp['timeframe']}, "
            f"high {_format_price(dp['reference_high'])} / low {_format_price(dp['reference_low'])}"
        )

    if report.keylevel_ltf_touched:
        kl_label = ", ".join(
            f"{KL_KIND_DISPLAY_NAMES.get(z['kind'], z['kind'])} ({z['direction']})"
            for z in report.keylevel_ltf_touched
        )
        lines.append(f"  • *KL LTF* proche du prix : {_escape_markdown_legacy(kl_label)}")

    if report.crt_ltf:
        lines.append(
            f"  • *CRT LTF* (sens DOL HTF) : {report.crt_ltf['bias']} @ "
            f"{_format_price(report.crt_ltf['dol_price'])}"
        )

    if report.ohlc_coherence is not None:
        lines.append(f"  • *Cohérence OHLC* du jour : {report.ohlc_coherence:.0%}")

    return lines


KL_TIMEFRAME_WEIGHT: dict[str, int] = {"monthly": 3, "weekly": 2, "daily": 1}


def _format_kl_names(report: DecisionReport, current_price: float, max_shown: int = 3) -> str:
    """
    Classe les zones KL HTF (Monthly/Weekly/Daily — intraday exclu, ce
    n'est pas "HTF") par puissance estimée, et n'affiche que les plus
    fortes : plutôt que de lister toutes les zones cataloguées sans
    distinction, on ne montre que celles les plus susceptibles de
    réellement influencer le prix.

    Score = poids de la timeframe (Monthly > Weekly > Daily) × largeur
    relative de la zone (% du prix qu'elle couvre) — une grosse zone
    Monthly l'emporte largement sur une petite zone Daily, mais une
    zone Daily inhabituellement large peut dépasser une petite zone
    Weekly. Le "mouvement" qui a formé la zone n'est pas mesuré
    directement (non stocké sur KeyLevelZone) — la largeur en est le
    meilleur proxy disponible : un déplacement plus violent laisse
    généralement une zone plus large.

    Écarte aussi les zones trop éloignées du prix actuel (même seuil
    que pour les CRT), pour ne jamais proposer un niveau hors de
    portée réaliste.
    """
    scored: list[tuple[float, dict[str, Any], str]] = []
    for timeframe in ("monthly", "weekly", "daily"):
        weight = KL_TIMEFRAME_WEIGHT[timeframe]
        for zone in report.major_kls.get(timeframe, []):
            midpoint = (zone["zone_low"] + zone["zone_high"]) / 2
            if midpoint <= 0:
                continue
            distance_ratio = abs(current_price - midpoint) / midpoint
            if distance_ratio > MAX_ANCHOR_DISTANCE_RATIO:
                continue
            size_pct = (zone["zone_high"] - zone["zone_low"]) / midpoint
            score = weight * size_pct
            scored.append((score, zone, timeframe))

    if not scored:
        return "Aucun KL HTF pertinent identifié"

    scored.sort(key=lambda t: t[0], reverse=True)
    top = scored[:max_shown]
    labels = [
        f"{KL_KIND_DISPLAY_NAMES.get(zone['kind'], zone['kind'])} ({zone['direction']}, {timeframe.capitalize()})"
        for _, zone, timeframe in top
    ]
    suffix = f" (+{len(scored) - max_shown} autre(s) moins significative(s))" if len(scored) > max_shown else ""
    return _escape_markdown_legacy(", ".join(labels) + suffix)


def compute_risk_reward_ratio(current_price: float, targets: dict[str, Any]) -> float | None:
    """
    Calcule le ratio risque/récompense NUMÉRIQUE (récompense/risque) du
    setup, basé sur le DERNIER objectif disponible (le plus ambitieux :
    TP3 si présent, sinon TP2, sinon TP1) — la convention usuelle pour
    décrire le R:R global d'un setup, plutôt qu'un R:R par palier
    intermédiaire.

    Risque = distance entre le prix actuel (référence d'entrée "au
    marché", cohérente avec la façon dont le robot entre ses positions)
    et le SL. Récompense = distance entre le prix actuel et ce dernier
    objectif.

    Fonction PARTAGÉE entre l'affichage (voir _format_risk_reward
    ci-dessous) et le filtre d'exécution (voir main.py::dispatch_alert,
    qui refuse d'exécuter en dessous d'un R:R minimum) — un seul calcul,
    jamais deux implémentations qui pourraient diverger.

    Returns:
        Le ratio (ex: 2.4 pour un R:R de "1:2.4"), ou None si non
        calculable (SL manquant, aucun TP disponible, ou risque nul —
        prix actuel déjà égal au SL).
    """
    stop_loss = targets.get("stop_loss")
    if stop_loss is None:
        return None

    final_target = targets.get("tp_htf_dol") or targets.get("tp_100") or targets.get("tp_50")
    if final_target is None:
        return None

    risk = abs(current_price - stop_loss)
    if risk == 0:
        return None

    reward = abs(final_target - current_price)
    return reward / risk


def _format_risk_reward(current_price: float, targets: dict[str, Any]) -> str | None:
    """
    Formate le ratio risque/récompense (R:R) du setup pour l'affichage
    — voir compute_risk_reward_ratio() pour le calcul lui-même.

    Returns:
        Une chaîne du type "1:2.4" (récompense:risque), ou None si le
        R:R n'est pas calculable.
    """
    ratio = compute_risk_reward_ratio(current_price, targets)
    if ratio is None:
        return None
    return f"1:{ratio:.1f}"


def _format_purge(report: DecisionReport, setup: TimeframeSetup) -> str:
    """
    Probabilité de purge et heure exacte (heure NY) pour le CRT de ce
    setup — la purge est évaluée par timeframe (crt_scope), donc on
    cherche l'évaluation correspondant à la timeframe du setup.
    """
    assessment = next(
        (a for a in report.purge_assessments if a.get("crt_scope") == setup.timeframe),
        None,
    )
    if assessment is None:
        return "Non évaluée"

    status = "HIGH probability" if assessment["high_probability"] else "probabilité standard"

    if not assessment.get("moment_timestamp"):
        return status

    moment = datetime.fromisoformat(assessment["moment_timestamp"]).astimezone(NY_TZ)
    time_label = f"{moment.strftime('%d/%m %H:%M')} (heure NY)"
    if assessment.get("killzone"):
        time_label += f", {assessment['killzone']}"

    return _escape_markdown_legacy(f"{status} — {time_label}")


def _format_market_alerts(report: DecisionReport) -> str | None:
    """
    Signale un jour férié (US et/ou France) et/ou une journée blanche
    détectée (volatilité anormalement faible — grève, liquidité
    réduite). Purement informatif : ne bloque jamais l'entrée, se
    contente d'en signaler le risque et la raison.

    Le calendrier économique (news à haut impact) est affiché à part,
    dans la Narrative (voir _format_narrative_ltf) — toujours visible,
    contrairement à cette fonction qui ne remonte que s'il y a
    effectivement quelque chose à signaler.

    Returns:
        None si rien à signaler — dans ce cas, la ligne n'est pas
        affichée du tout dans le message, pour ne pas ajouter de bruit
        les jours normaux.
    """
    parts = []

    holiday = report.holiday
    holiday_names = [name for name in (holiday.get("us_holiday"), holiday.get("france_holiday")) if name]
    if holiday_names:
        parts.append(f"⚠️ Jour férié : {_escape_markdown_legacy(', '.join(holiday_names))}")

    quiet = report.quiet_day
    if quiet.get("is_quiet"):
        ratio = quiet.get("range_ratio")
        ratio_label = f" ({ratio:.0%} de la moyenne récente)" if ratio is not None else ""
        parts.append(f"⚠️ Journée blanche détectée{ratio_label} — grève ou liquidité réduite ?")

    return " | ".join(parts) if parts else None


def format_telegram_message(
    report: DecisionReport,
    current_price: float,
    symbol: str,
    setups: list[TimeframeSetup] | None = None,
    btc_bias_context: dict[str, Any] | None = None,
) -> str:
    """
    Assemble le message final, en Markdown (compatible Telegram) :
      1. Titre : pastille (🟢/🔴) + actif + LONG/SHORT + confiance
      2. Alertes de marché (news à haut impact, jour férié, journée blanche)
      3. Résumé structuré : DOL long terme, Narrative, KL, puis par setup
         affiché : CRT (timeframe) et Purge (probabilité + heure)
      4. Take-profits exacts du/des setup(s)
      5. Disclaimer

    Aucune analyse IA n'intervient dans ce message : uniquement les
    critères déterministes (DOL/CRT/Narrative/Purge/KL) et le contexte
    factuel (calendrier économique, jours fériés). Voir la conversation
    de conception : l'IA a été retirée du pipeline car jugée trop
    imprécise pour ce cas d'usage.

    Args:
        report: Le DecisionReport source des chiffres exacts.
        current_price: Prix de l'actif au moment de l'alerte.
        symbol: Symbole de l'actif (ex: "BTC", "ETH", "SPY") — affiché
            dans le titre.
        setups: Setup(s) à afficher (CRT + Purge + TPs). Si None
            (défaut), affiche tous les setups recommandés du report.
            Pour un message dédié à un seul setup nouvellement
            déclenché (voir main.py::dispatch_alert), passez une liste
            à un seul élément.

    Returns:
        Le message complet, prêt à être envoyé tel quel par
        notifications/telegram_sender.py.
    """
    bias = report.effective_bias
    emoji = BIAS_EMOJI.get(bias, "⚪")
    direction_label = DIRECTION_LABEL.get(bias, "N/A")

    recommended_setups = (
        setups if setups is not None else [s for s in report.timeframe_setups if s.entry_recommended]
    )

    lines = [
        f"{emoji} *{symbol} — {direction_label}* (confiance : {report.confidence})",
        f"Prix actuel : {_format_price(current_price)}",
    ]
    if btc_bias_context is not None:
        btc_bias = btc_bias_context.get("bias")
        btc_timeframe = btc_bias_context.get("source_timeframe")
        timeframe_label = f" ({btc_timeframe.capitalize()})" if btc_timeframe else ""
        lines.append(f"*Biais BTC (référence)* : {btc_bias}{timeframe_label}")
    lines.append("")

    market_alerts = _format_market_alerts(report)
    if market_alerts is not None:
        lines.append(market_alerts)
        lines.append("")

    lines.extend(
        [
            f"*DOL long terme* : {_format_dol_long_terme(report)}",
            f"*Narrative* : {_format_narrative(report)}",
        ]
    )
    lines.extend(_format_narrative_ltf(report))
    lines.extend(
        [
            f"*KL* : {_format_kl_names(report, current_price)}",
            "",
        ]
    )

    for setup in recommended_setups:
        lines.append(f"*Setup {_escape_markdown_legacy(setup.setup_name)}*")
        lines.append(f"  • *CRT* : {setup.timeframe.capitalize()}")
        lines.append(f"  • *Purge* : {_format_purge(report, setup)}")

        targets = setup.crt_targets or {}
        bias = setup.htf_crt.get("bias") if setup.htf_crt else None

        def _already_reached(tp_price: float) -> bool:
            """Le TP est-il déjà dépassé par le prix actuel au moment de cette notification ?"""
            if bias == "bullish":
                return current_price >= tp_price
            if bias == "bearish":
                return current_price <= tp_price
            return False

        if targets.get("stop_loss") is not None:
            lines.append(f"  • *SL* : {_format_price(targets['stop_loss'])}")
        if targets.get("tp_50") is not None:
            suffix = " (déjà atteint)" if _already_reached(targets["tp_50"]) else ""
            lines.append(f"  • *TP1 (50%)* : {_format_price(targets['tp_50'])}{suffix}")
        if targets.get("tp_100") is not None:
            suffix = " (déjà atteint)" if _already_reached(targets["tp_100"]) else ""
            lines.append(f"  • *TP2 (100% / DOL)* : {_format_price(targets['tp_100'])}{suffix}")
        if targets.get("tp_htf_dol") is not None:
            suffix = " (déjà atteint)" if _already_reached(targets["tp_htf_dol"]) else ""
            lines.append(f"  • *TP3 (DOL HTF)* : {_format_price(targets['tp_htf_dol'])}{suffix}")

        rr_label = _format_risk_reward(current_price, targets)
        if rr_label is not None:
            lines.append(f"  • *R:R* : {rr_label}")

        lines.append("")

    if report.priority_entry is not None:
        refined = report.priority_entry
        direction_label = DIRECTION_LABEL.get(refined.direction, refined.direction)
        line = (
            f"*Entrée précise* : {_escape_markdown_legacy(refined.source)} — {direction_label} "
            f"@ {_format_price(refined.entry_price)}, SL {_format_price(refined.stop_loss)}"
        )
        if refined.take_profits:
            tp_label = " / ".join(f"{_format_price(tp)}" for tp in refined.take_profits)
            line += f", TP {tp_label}"
        lines.append(line)
        lines.append("")

    lines.append(DISCLAIMER)

    return "\n".join(lines)
