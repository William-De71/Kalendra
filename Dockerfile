# syntax=docker/dockerfile:1
#
# Kalendra ne dépend que de la bibliothèque standard : la construction de
# l'image n'installe aucun paquet Python et ne nécessite donc aucun accès
# réseau vers PyPI. Résultat : une image d'une soixantaine de mégaoctets,
# reproductible, sans chaîne d'approvisionnement à surveiller.

FROM python:3.14-alpine AS runtime

# tzdata est indispensable : les TZID iCalendar (Europe/Paris…) sont résolus
# par zoneinfo, qui lit la base de données système.
RUN apk add --no-cache tzdata \
    && adduser -D -H -u 10001 kalendra \
    && mkdir -p /data \
    && chown kalendra:kalendra /data

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    KALENDRA_DB=/data/kalendra.db \
    KALENDRA_HOST=0.0.0.0 \
    KALENDRA_PORT=5232

WORKDIR /app
COPY src/kalendra /app/kalendra

# Pré-compilation : démarrage plus rapide et détection immédiate d'une erreur
# de syntaxe au moment de la construction plutôt qu'à l'exécution.
RUN python -m compileall -q /app/kalendra

USER kalendra
VOLUME ["/data"]
EXPOSE 5232

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import os,sys,urllib.request; \
url='http://127.0.0.1:%s/health' % os.environ.get('KALENDRA_PORT','5232'); \
sys.exit(0 if urllib.request.urlopen(url, timeout=4).status == 200 else 1)"

ENTRYPOINT ["python", "-m", "kalendra"]
CMD ["serve"]

LABEL org.opencontainers.image.title="Kalendra" \
      org.opencontainers.image.description="Serveur CalDAV autonome sur SQLite, avec flux ICS pour Google Calendar et Proton Calendar" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/William-De71/Kalendra"
