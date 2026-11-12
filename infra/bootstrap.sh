#!/usr/bin/env bash
# One-time Google Cloud setup so GitHub Actions can deploy Sentinel to Cloud Run.
#
#   PROJECT_ID=my-project REGION=europe-west1 GITHUB_REPO=owner/sentinel ./infra/bootstrap.sh
#
# Safe to re-run: every step checks before it creates. Needs gcloud, logged in as a
# project owner, on a project with billing enabled (Cloud Run requires it). See
# infra/README.md for what this creates and why.
set -euo pipefail

: "${PROJECT_ID:?set PROJECT_ID, e.g. PROJECT_ID=my-project}"
: "${REGION:?set REGION, e.g. REGION=europe-west1}"
: "${GITHUB_REPO:?set GITHUB_REPO, e.g. GITHUB_REPO=owner/sentinel}"

AR_REPO="sentinel"
POOL_ID="github"
PROVIDER_ID="github-oidc"
DEPLOYER_SA="sentinel-deployer@${PROJECT_ID}.iam.gserviceaccount.com"
RUNTIME_SA="sentinel-runtime@${PROJECT_ID}.iam.gserviceaccount.com"
SECRETS=("sentinel-google-api-key" "sentinel-api-keys")

quietly() { "$@" >/dev/null 2>&1; }
step() { printf '\n==> %s\n' "$1"; }

gcloud config set project "$PROJECT_ID" --quiet >/dev/null
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"

step "Enabling APIs"
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  sts.googleapis.com

step "Artifact Registry repository '${AR_REPO}' in ${REGION}"
quietly gcloud artifacts repositories describe "$AR_REPO" --location "$REGION" ||
  gcloud artifacts repositories create "$AR_REPO" \
    --repository-format=docker --location="$REGION" --description="Sentinel images"

step "Service accounts"
# sentinel-deployer: used by GitHub Actions to push images and deploy.
# sentinel-runtime:  the identity the Cloud Run service runs as; it can only read its two secrets.
for name in sentinel-deployer sentinel-runtime; do
  quietly gcloud iam service-accounts describe "${name}@${PROJECT_ID}.iam.gserviceaccount.com" ||
    gcloud iam service-accounts create "$name" --display-name="$name"
done

step "Deployer permissions"
# run.admin (not run.developer) because the deploy makes the service publicly reachable,
# which sets the service's IAM policy. The app itself still requires an API key.
gcloud projects add-iam-policy-binding "$PROJECT_ID" --condition=None --quiet \
  --member="serviceAccount:${DEPLOYER_SA}" --role="roles/run.admin" >/dev/null
gcloud artifacts repositories add-iam-policy-binding "$AR_REPO" --location="$REGION" --quiet \
  --member="serviceAccount:${DEPLOYER_SA}" --role="roles/artifactregistry.writer" >/dev/null
gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA" --quiet \
  --member="serviceAccount:${DEPLOYER_SA}" --role="roles/iam.serviceAccountUser" >/dev/null

step "Workload Identity Federation: GitHub OIDC, limited to ${GITHUB_REPO} on main and v* tags"
quietly gcloud iam workload-identity-pools describe "$POOL_ID" --location=global ||
  gcloud iam workload-identity-pools create "$POOL_ID" \
    --location=global --display-name="GitHub Actions"
quietly gcloud iam workload-identity-pools providers describe "$PROVIDER_ID" \
  --workload-identity-pool="$POOL_ID" --location=global ||
  gcloud iam workload-identity-pools providers create-oidc "$PROVIDER_ID" \
    --workload-identity-pool="$POOL_ID" --location=global \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
    --attribute-condition="assertion.repository=='${GITHUB_REPO}' && (assertion.ref=='refs/heads/main' || assertion.ref.startsWith('refs/tags/v'))"
gcloud iam service-accounts add-iam-policy-binding "$DEPLOYER_SA" --quiet \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/attribute.repository/${GITHUB_REPO}" \
  >/dev/null

step "Secrets (values are read from this terminal, never from arguments or history)"
for secret in "${SECRETS[@]}"; do
  quietly gcloud secrets describe "$secret" ||
    gcloud secrets create "$secret" --replication-policy=automatic
  if [ -z "$(gcloud secrets versions list "$secret" --limit=1 --format='value(name)')" ]; then
    case "$secret" in
      sentinel-google-api-key) prompt="Gemini API key" ;;
      sentinel-api-keys) prompt="SENTINEL_API_KEYS value (name:key pairs, keys >= 24 chars)" ;;
    esac
    read -r -s -p "${prompt}: " value
    echo
    printf '%s' "$value" | gcloud secrets versions add "$secret" --data-file=- >/dev/null
    unset value
  else
    echo "  ${secret}: already has a version (add a new one with: gcloud secrets versions add ${secret} --data-file=-)"
  fi
  gcloud secrets add-iam-policy-binding "$secret" --quiet \
    --member="serviceAccount:${RUNTIME_SA}" --role="roles/secretmanager.secretAccessor" >/dev/null
done

WIF_PROVIDER="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/providers/${PROVIDER_ID}"
step "Done. Set these GitHub repository variables (Settings > Secrets and variables > Actions > Variables)"
cat <<EOF
  GCP_PROJECT_ID=${PROJECT_ID}
  GCP_REGION=${REGION}
  GCP_WIF_PROVIDER=${WIF_PROVIDER}
  GCP_DEPLOYER_SA=${DEPLOYER_SA}
  GCP_RUNTIME_SA=${RUNTIME_SA}

Or with the GitHub CLI:
  gh variable set GCP_PROJECT_ID   --repo ${GITHUB_REPO} --body '${PROJECT_ID}'
  gh variable set GCP_REGION       --repo ${GITHUB_REPO} --body '${REGION}'
  gh variable set GCP_WIF_PROVIDER --repo ${GITHUB_REPO} --body '${WIF_PROVIDER}'
  gh variable set GCP_DEPLOYER_SA  --repo ${GITHUB_REPO} --body '${DEPLOYER_SA}'
  gh variable set GCP_RUNTIME_SA   --repo ${GITHUB_REPO} --body '${RUNTIME_SA}'

Then deploy by pushing a version tag:  git tag v0.5.0 && git push origin v0.5.0
EOF
