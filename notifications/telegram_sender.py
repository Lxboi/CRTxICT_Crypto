"""
notifications/telegram_sender.py

Envoi du message final (déjà décidé par DecisionEngine, déjà formaté
par messaging/formatter.py) sur Telegram. Ce module n'a aucun rôle de
décision ni de mise en forme : il achemine un texte déjà prêt, rien de
plus.

Dégradation gracieuse : si les identifiants Telegram sont absents ou si
l'envoi échoue (réseau, bot bloqué, chat_id invalide), le message reste
visible dans les logs et le bot continue de tourner — une panne de
notification ne doit jamais faire planter un cycle.
"""

from __future__ import annotations

import asyncio
import time

from loguru import logger
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import NetworkError, TelegramError

from config.settings import get_settings

DEFAULT_PARSE_MODE = ParseMode.MARKDOWN

# Caractères ayant un sens spécial en Markdown "legacy" de Telegram
# (ParseMode.MARKDOWN, PAS MarkdownV2) : *gras*, _italique_, `code`,
# [lien](url). Un seul de ces caractères non apparié dans le texte
# (ex: un identifiant utilisateur contenant un underscore, comme
# "demo_user") fait échouer TOUT le message avec une erreur du type
# "Can't parse entities: can't find end of the entity" -- même si le
# reste du message est parfaitement valide.
_MARKDOWN_SPECIAL_CHARS = ("_", "*", "`", "[")


def escape_markdown(text: str) -> str:
    """
    Échappe les caractères spéciaux du Markdown legacy de Telegram dans
    un texte DYNAMIQUE (identifiant utilisateur, message d'erreur brut
    d'un exchange, etc.) avant de l'insérer dans un message par
    ailleurs formaté en Markdown (*gras*, etc.) — à appeler sur
    CHAQUE valeur non contrôlée par le code (jamais sur les `*bold*`
    volontaires écrits directement dans les templates de message).
    """
    for char in _MARKDOWN_SPECIAL_CHARS:
        text = text.replace(char, f"\\{char}")
    return text


def send_telegram_message(message: str, max_retries: int = 2) -> bool:
    """
    Envoie un message sur Telegram.

    main.py fonctionne de façon synchrone (APScheduler BlockingScheduler),
    alors que python-telegram-bot est une librairie asynchrone : cette
    fonction fait le pont via asyncio.run(), pour que le reste du code
    n'ait pas à se soucier d'async/await.

    Args:
        message: Le texte déjà formaté à envoyer tel quel (Markdown).
        max_retries: Nombre de nouvelles tentatives en cas d'erreur
            RÉSEAU uniquement (timeout, connexion impossible — voir
            NetworkError) avec un backoff exponentiel court (1s, 2s...).
            Une erreur de CONTENU (Markdown mal formé, permissions,
            chat_id invalide — voir TelegramError plus générique)
            échouerait identiquement à chaque tentative, donc n'est
            JAMAIS réessayée : seuls les problèmes réseau, par nature
            transitoires, le sont.

    Returns:
        True si l'envoi a réussi, False sinon (identifiants absents ou
        erreur d'envoi, après épuisement des tentatives) — permet à
        l'appelant de décider s'il faut marquer l'alerte comme envoyée
        en base.
    """
    settings = get_settings()
    bot_token = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id

    if not bot_token or not chat_id:
        logger.warning(
            "TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID absent de l'environnement "
            "-> message non envoyé (reste visible dans les logs uniquement)"
        )
        return False

    for attempt in range(max_retries + 1):
        try:
            asyncio.run(_send_async(bot_token, chat_id, message))
            logger.info("Message envoyé sur Telegram avec succès")
            return True
        except NetworkError as e:
            if attempt < max_retries:
                wait_seconds = 2**attempt  # 1s, puis 2s...
                logger.warning(
                    f"Échec réseau Telegram (tentative {attempt + 1}/{max_retries + 1}) -- "
                    f"nouvelle tentative dans {wait_seconds}s : {e}"
                )
                time.sleep(wait_seconds)
                continue
            logger.exception("Échec de l'envoi Telegram après plusieurs tentatives (erreur réseau persistante)")
            return False
        except TelegramError:
            logger.exception("Échec de l'envoi Telegram (erreur API -- pas réseau, non réessayé)")
            return False
        except Exception:
            logger.exception("Erreur inattendue lors de l'envoi Telegram")
            return False

    return False


async def _send_async(bot_token: str, chat_id: str, message: str) -> None:
    bot = Bot(token=bot_token)
    await bot.send_message(chat_id=chat_id, text=message, parse_mode=DEFAULT_PARSE_MODE)