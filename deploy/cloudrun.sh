#!/usr/bin/env bash
# Deploy the EchoCheck AI prototype to Cloud Run with Firestore and Gemini.
# Prerequisites (run by the project owner):
#   gcloud auth login && gcloud config set project <PROJECT_ID>   # billing enabled
#   a Secret Manager secret named gemini-api-key holding a Gemini API key
# Usage: GEMINI_MODEL=<model-id> deploy/cloudrun.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-asia-southeast1}"
SERVICE="${SERVICE:-echocheck-ai}"
MODEL="${GEMINI_MODEL:?Set GEMINI_MODEL to a Gemini model that accepts audio and structured output}"
[ -n "$PROJECT" ] || { echo "Set a project: gcloud config set project <PROJECT_ID>" >&2; exit 1; }

NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
RUNTIME_SA="${NUMBER}-compute@developer.gserviceaccount.com"
ORIGIN="https://${SERVICE}-${NUMBER}.${REGION}.run.app"

gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  firestore.googleapis.com secretmanager.googleapis.com --project "$PROJECT"

if ! gcloud firestore databases describe --database='(default)' --project "$PROJECT" >/dev/null 2>&1; then
  gcloud firestore databases create --database='(default)' --location="$REGION" --type=firestore-native --project "$PROJECT"
fi

# Least privilege for the runtime identity: Firestore read/write and the Gemini secret only.
gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:${RUNTIME_SA}" \
  --role=roles/datastore.user --condition=None >/dev/null
gcloud secrets add-iam-policy-binding gemini-api-key --member="serviceAccount:${RUNTIME_SA}" \
  --role=roles/secretmanager.secretAccessor --project "$PROJECT" >/dev/null

# One instance keeps the embedded SQLite workflow store consistent; Firestore keeps durable history.
gcloud run deploy "$SERVICE" --source . --region "$REGION" --project "$PROJECT" \
  --service-account "$RUNTIME_SA" --allow-unauthenticated \
  --min-instances 1 --max-instances 1 --concurrency 40 --cpu 1 --memory 1Gi --timeout 300 \
  --set-env-vars "ECHOCHECK_MODE=prototype,APP_ORIGIN=${ORIGIN},PROVIDER_PROCESSING_ENABLED=true,GEMINI_MODEL=${MODEL},FIRESTORE_MIRROR_ENABLED=true,FIRESTORE_PROJECT_ID=${PROJECT},LIVE_DAILY_LIMIT=300,LIVE_SESSION_HOURLY_LIMIT=30" \
  --set-secrets "GEMINI_API_KEY=gemini-api-key:latest"

# Cloud Run also serves a hash-style URL; accept it as an exact additional origin.
STATUS_URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --project "$PROJECT" --format='value(status.url)')"
if [ "$STATUS_URL" != "$ORIGIN" ]; then
  gcloud run services update "$SERVICE" --region "$REGION" --project "$PROJECT" \
    --update-env-vars "ALLOWED_ORIGINS=${STATUS_URL}" >/dev/null
fi
echo "Deployed: ${ORIGIN}"
curl -fsS "${ORIGIN}/api/health" | python3 -c 'import json,sys; h=json.load(sys.stdin); print({k:h[k] for k in ("mode","provider_configured","provider_model","firestore_mirror","release_enabled")})'
