# Dockerfile
#
# Image multi-architecture (fonctionne telle quelle sur Raspberry Pi
# ARM64/ARMv7 et sur du cloud x86_64) : python:3.12-slim est publiée
# pour les deux architectures, Docker choisit automatiquement la bonne
# selon la machine sur laquelle l'image est construite/exécutée.

FROM python:3.12-slim

WORKDIR /app

# Certificats SSL nécessaires pour les appels HTTPS sortants
# (Binance, Polygon, SoSoValue, Telegram).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Dépendances déclarées dans pyproject.toml (installation du projet
# lui-même, qui tire automatiquement toutes ses dépendances).
COPY . .
RUN pip install --no-cache-dir .

# database/, logs/ et execution_state/ sont montés en volumes (voir
# docker-compose.yml) pour survivre aux reconstructions/redémarrages du
# conteneur — sans ça, la base SQLite, les logs, ET l'état d'exécution
# (kill-switches, registre des ordres stop, pannes API détectées)
# seraient perdus à chaque déploiement.
VOLUME ["/app/database", "/app/logs", "/app/execution_state"]

CMD ["python", "main.py"]