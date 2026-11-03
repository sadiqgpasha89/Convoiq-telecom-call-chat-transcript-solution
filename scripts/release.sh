#!/usr/bin/env bash
# Canary release of all ConvoIQ Cloud Run services with automatic rollback.
#   PROJECT=.. REGION=.. ENV=dev IMAGE=region-docker.pkg.dev/p/convoiq/convoiq:tag scripts/release.sh
# Order: consumers first, producers (gateway) last, so a new producer never emits events a still-old consumer cannot parse
# (event contracts are additive-only; see docs/ARCHITECTURE.md#schema-evolution).
set -euo pipefail
: "${PROJECT:?}" "${REGION:?}" "${ENV:?}" "${IMAGE:?}"
CANARY_PCT="${CANARY_PCT:-10}"; CANARY_WAIT_S="${CANARY_WAIT_S:-300}"
P="convoiq-${ENV}"
ALL=(redaction enrichment orchestrator action-broker analytics monitor telecom-sim gateway)
SERVICES=()
for s in "${ALL[@]}"; do
  gcloud run services describe "$P-$s" --region "$REGION" --project "$PROJECT" >/dev/null 2>&1 && SERVICES+=("$s")
done
[ ${#SERVICES[@]} -gt 0 ] || { echo "no services found for $P - run terraform first"; exit 1; }
echo "releasing $IMAGE to: ${SERVICES[*]}"

declare -A PREV NEW
for s in "${SERVICES[@]}"; do
  PREV[$s]="$(gcloud run services describe "$P-$s" --region "$REGION" --project "$PROJECT" --format='value(status.latestReadyRevisionName)')"
  # --no-traffic: the revision must pass its startup probe (models load, /healthz) before it can receive anything
  gcloud run deploy "$P-$s" --image "$IMAGE" --region "$REGION" --project "$PROJECT" --no-traffic --tag cand --quiet \
    --update-env-vars "CONVOIQ_RELEASE=${IMAGE##*:}"
  NEW[$s]="$(gcloud run services describe "$P-$s" --region "$REGION" --project "$PROJECT" --format='value(status.latestCreatedRevisionName)')"
done

rollback() {
  echo "!! rolling back"
  for s in "${SERVICES[@]}"; do
    if [ -n "${PREV[$s]}" ]; then
      gcloud run services update-traffic "$P-$s" --region "$REGION" --project "$PROJECT" --to-revisions "${PREV[$s]}=100" --quiet || true
    fi
    gcloud run services update-traffic "$P-$s" --region "$REGION" --project "$PROJECT" --remove-tags cand --quiet || true
  done
}
trap 'rc=$?; [ $rc -ne 0 ] && rollback; exit $rc' EXIT

# first deploy of a brand-new service has no previous revision: send everything straight to it
for s in "${SERVICES[@]}"; do
  if [ -n "${PREV[$s]}" ] && [ "${PREV[$s]}" != "${NEW[$s]}" ]; then
    gcloud run services update-traffic "$P-$s" --region "$REGION" --project "$PROJECT" --to-tags "cand=${CANARY_PCT}" --quiet
  else
    gcloud run services update-traffic "$P-$s" --region "$REGION" --project "$PROJECT" --to-latest --quiet
  fi
done

echo "canary at ${CANARY_PCT}% - observing ${CANARY_WAIT_S}s"
sleep "$CANARY_WAIT_S"
for s in "${SERVICES[@]}"; do
  [ "${PREV[$s]}" = "${NEW[$s]}" ] && continue
  python3 "$(dirname "$0")/canary_check.py" --project "$PROJECT" --service "$P-$s" --revision "${NEW[$s]}" --window-s "$CANARY_WAIT_S"
done

for s in "${SERVICES[@]}"; do
  gcloud run services update-traffic "$P-$s" --region "$REGION" --project "$PROJECT" --to-latest --quiet
  gcloud run services update-traffic "$P-$s" --region "$REGION" --project "$PROJECT" --remove-tags cand --quiet || true
done
trap - EXIT
echo "release complete: $IMAGE"
