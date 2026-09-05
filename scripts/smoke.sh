#!/usr/bin/env bash
# Test de fumée d'un conteneur Kalendra déjà démarré.
#
#   scripts/smoke.sh [URL_DE_BASE] [IDENTIFIANT] [MOT_DE_PASSE]
#
# Vérifie la chaîne complète : santé, découverte CalDAV, création d'agenda,
# dépôt d'un événement, requête temporelle, synchronisation et flux ICS.

set -euo pipefail

BASE="${1:-http://127.0.0.1:5232}"
USER="${2:-${KALENDRA_ADMIN_USER:-admin}}"
PASS="${3:-${KALENDRA_ADMIN_PASSWORD:-motdepasse}}"
AUTH=(-u "${USER}:${PASS}")
CAL="${BASE}/calendars/${USER}/fumee/"

failures=0

check() {
    local label="$1" expected="$2" actual="$3"
    if [[ "$actual" == "$expected" ]]; then
        printf '  ok   %-46s %s\n' "$label" "$actual"
    else
        printf '  KO   %-46s attendu %s, obtenu %s\n' "$label" "$expected" "$actual"
        failures=$((failures + 1))
    fi
}

contains() {
    local label="$1" needle="$2" haystack="$3"
    if [[ "$haystack" == *"$needle"* ]]; then
        printf '  ok   %-46s contient « %s »\n' "$label" "$needle"
    else
        printf '  KO   %-46s « %s » absent\n' "$label" "$needle"
        printf '       réponse : %s\n' "${haystack:0:400}"
        failures=$((failures + 1))
    fi
}

status() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }

echo "== Attente du démarrage de ${BASE}"
for _ in $(seq 1 40); do
    if curl -fsS "${BASE}/health" >/dev/null 2>&1; then break; fi
    sleep 1
done

echo "== Santé et découverte"
contains "GET /health" '"status": "ok"' "$(curl -sS "${BASE}/health")"
check "GET /.well-known/caldav" "301" "$(status "${BASE}/.well-known/caldav")"
check "OPTIONS /" "204" "$(status -X OPTIONS "${AUTH[@]}" "${BASE}/")"
contains "OPTIONS annonce calendar-access" "calendar-access" \
    "$(curl -sS -D - -o /dev/null -X OPTIONS "${AUTH[@]}" "${BASE}/")"
check "PROPFIND anonyme -> 401" "401" "$(status -X PROPFIND "${BASE}/")"

principal=$(curl -sS -X PROPFIND -H 'Depth: 0' -H 'Content-Type: application/xml' "${AUTH[@]}" \
    --data '<?xml version="1.0"?><D:propfind xmlns:D="DAV:"><D:prop><D:current-user-principal/></D:prop></D:propfind>' \
    "${BASE}/")
contains "PROPFIND / expose le principal" "/principals/${USER}/" "$principal"

home=$(curl -sS -X PROPFIND -H 'Depth: 0' -H 'Content-Type: application/xml' "${AUTH[@]}" \
    --data '<?xml version="1.0"?><D:propfind xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav"><D:prop><C:calendar-home-set/></D:prop></D:propfind>' \
    "${BASE}/principals/${USER}/")
contains "PROPFIND principal -> calendar-home-set" "/calendars/${USER}/" "$home"

echo "== Cycle de vie d'un agenda"
curl -sS -o /dev/null -X DELETE "${AUTH[@]}" "${CAL}" || true
check "MKCALENDAR" "201" "$(status -X MKCALENDAR "${AUTH[@]}" "${CAL}")"

event=$'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//smoke//FR\r\nBEGIN:VEVENT\r\nUID:smoke-1\r\nDTSTAMP:20260101T090000Z\r\nDTSTART:20260310T090000Z\r\nDTEND:20260310T100000Z\r\nSUMMARY:Test de fumee\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n'
check "PUT événement" "201" \
    "$(status -X PUT -H 'Content-Type: text/calendar' "${AUTH[@]}" --data-binary "$event" "${CAL}smoke-1.ics")"
contains "GET événement" "UID:smoke-1" "$(curl -sS "${AUTH[@]}" "${CAL}smoke-1.ics")"
check "PUT invalide refusé" "403" \
    "$(status -X PUT -H 'Content-Type: text/calendar' "${AUTH[@]}" --data 'pas du tout ical' "${CAL}mauvais.ics")"

echo "== Rapports CalDAV"
query='<?xml version="1.0"?><C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav"><D:prop><D:getetag/><C:calendar-data/></D:prop><C:filter><C:comp-filter name="VCALENDAR"><C:comp-filter name="VEVENT"><C:time-range start="20260301T000000Z" end="20260401T000000Z"/></C:comp-filter></C:comp-filter></C:filter></C:calendar-query>'
report=$(curl -sS -X REPORT -H 'Depth: 1' -H 'Content-Type: application/xml' "${AUTH[@]}" --data "$query" "${CAL}")
contains "calendar-query (plage)" "smoke-1.ics" "$report"

sync='<?xml version="1.0"?><D:sync-collection xmlns:D="DAV:"><D:sync-token/><D:sync-level>1</D:sync-level><D:prop><D:getetag/></D:prop></D:sync-collection>'
synced=$(curl -sS -X REPORT -H 'Depth: 1' -H 'Content-Type: application/xml' "${AUTH[@]}" --data "$sync" "${CAL}")
contains "sync-collection initiale" "urn:kalendra:sync:" "$synced"

echo "== Flux ICS public"
token=$(curl -sS -H 'Accept: text/html' "${AUTH[@]}" "${BASE}/admin" \
    | grep -o "/feed/[A-Za-z0-9_-]\+\.ics" | head -n 1)
if [[ -z "$token" ]]; then
    echo "  KO   aucune URL de flux trouvée dans l'interface d'administration"
    failures=$((failures + 1))
else
    feed=$(curl -sS "${BASE}${token}")
    contains "flux ICS anonyme" "BEGIN:VCALENDAR" "$feed"
    contains "flux ICS nommé" "X-WR-CALNAME" "$feed"
fi

echo "== Nettoyage"
check "DELETE agenda" "204" "$(status -X DELETE "${AUTH[@]}" "${CAL}")"

if (( failures > 0 )); then
    echo "== ÉCHEC : ${failures} vérification(s) en erreur"
    exit 1
fi
echo "== Toutes les vérifications sont passées"
