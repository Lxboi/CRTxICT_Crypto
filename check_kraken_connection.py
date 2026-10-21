"""
check_kraken_connection.py

Vérifie que vos identifiants Kraken FUTURES fonctionnent (PAS Kraken
Pro Spot/Marge — clés API séparées) — lit SEULEMENT le solde et les
positions ouvertes, ne passe AUCUN ordre. À lancer avant toute
activation de l'exécution automatique.

Usage :
    python check_kraken_connection.py
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

if __name__ == "__main__":
    from config.settings import get_settings
    from execution.kraken_client import KrakenClient

    settings = get_settings()
    if not settings.kraken_api_key or not settings.kraken_api_secret:
        print("❌ KRAKEN_API_KEY / KRAKEN_API_SECRET absentes de .env")
        raise SystemExit(1)

    # Affichage MASQUÉ de la clé (jamais le secret) -- pour vérifier
    # visuellement que c'est bien la clé attendue qui a été chargée,
    # sans jamais exposer de valeur sensible. Comparez ce début/fin
    # avec ce qui est affiché sur le tableau de bord Kraken.
    key = settings.kraken_api_key
    masked = f"{key[:6]}...{key[-4:]}" if len(key) > 12 else "(trop courte pour être masquée en sécurité)"
    print(f"Clé API chargée : {masked} (longueur : {len(key)} caractères)")

    client = KrakenClient(settings.kraken_api_key, settings.kraken_api_secret)

    print("Connexion à Kraken...")
    try:
        balance = client.get_account_balance()
        print(f"✅ Solde (marge disponible, compte Futures) récupéré : {balance}")
    except Exception as e:
        error_text = str(e)
        print(f"❌ Échec récupération du solde : {error_text}")

        if "Permission denied" in error_text:
            print(
                "\nCause : la clé est bien reconnue par Kraken Futures, mais il lui "
                "manque la permission nécessaire.\n"
                "-> Dans Kraken, section FUTURES (pas Spot) > gestion des clés API > "
                "vérifiez que 'General API - Full Access' est bien coché pour cette clé.\n"
                "Rappel : les clés Futures sont GÉNÉRÉES SÉPARÉMENT des clés Spot Pro "
                "habituelles -- vérifiez que vous n'avez pas collé une clé Spot par erreur."
            )
        elif "Invalid key" in error_text:
            print(
                "\nCauses fréquentes de clé invalide :\n"
                "  1. Clé/secret mal copiés (espace, retour à la ligne, coupure) -- "
                "comparez la clé masquée ci-dessus avec le tableau de bord Kraken\n"
                "  2. Clé Spot Pro utilisée par erreur au lieu d'une clé FUTURES "
                "(deux systèmes de clés séparés chez Kraken)\n"
                "  3. Clé désactivée ou supprimée côté Kraken\n"
                "  4. Clé et secret inversés (le secret dans KRAKEN_API_KEY et vice-versa)"
            )
        elif "Invalid signature" in error_text:
            print(
                "\nCause probable : le SECRET (pas la clé) est incorrect ou mal copié -- "
                "la signature HMAC calculée ne correspond pas à ce que Kraken Futures "
                "attend. Vérifiez KRAKEN_API_SECRET dans .env."
            )
        else:
            print(
                "\nErreur non reconnue par ce diagnostic -- consultez la documentation "
                "Kraken Futures pour ce code d'erreur précis : "
                "https://docs.kraken.com/api/docs/guides/futures-rest"
            )
        raise SystemExit(1)

    try:
        positions = client.get_open_positions()
        print(f"✅ Positions ouvertes : {len(positions)}")
        for p in positions:
            print(f"   - {p.symbol} {p.side} {p.quantity} @ {p.entry_price} (levier {p.leverage}x)")
    except Exception as e:
        print(f"❌ Échec récupération des positions : {e}")
        raise SystemExit(1)

    print("\n✅ Connexion Kraken fonctionnelle. Aucun ordre n'a été passé.")
