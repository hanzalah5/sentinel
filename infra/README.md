# Deploying Sentinel to Google Cloud Run

Sentinel's API deploys to Cloud Run from GitHub Actions (`.github/workflows/deploy.yml`):

1. pushing a `v*` tag builds the image;
2. the workflow pushes it to Artifact Registry and deploys a new revision;
3. it smoke-tests the live URL.

There are no service-account keys anywhere. GitHub's OIDC token is exchanged for short-lived credentials through Workload Identity Federation.

## What this deployment is (and isn't)

It's **API-only**, a deliberate choice to keep the demo cheap:

| Feature | In this deployment |
|---|---|
| `/investigate`, `/incidents`, approvals | ✅ behind API keys (`SENTINEL_API_KEYS`) |
| Runbook retrieval (pgvector) | ❌ no database, so the agent answers without runbook grounding |
| Durable human-in-the-loop | ⚠️ paused incidents live in memory; they're lost when the service scales to zero or a new revision rolls out |
| Redis ingestion worker | ❌ not deployed; use Docker Compose locally |

The full setup (Postgres + pgvector, Redis, worker) runs with `docker compose up`.

**Gemini quota.** The project's API key is on the free tier: **20 requests per day** for the model. The deployed API and the CI eval share that quota, and one eval run uses 12. The deploy smoke test never calls Gemini.

## One-time setup

**Prerequisites:**
- a Google Cloud project **with billing enabled** (Cloud Run and Artifact Registry require it, even when usage stays within free allowances);
- `gcloud` logged in as a project owner;
- your Gemini API key;
- an API key for callers, generated with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

```bash
PROJECT_ID=my-project REGION=europe-west1 GITHUB_REPO=owner/sentinel ./infra/bootstrap.sh
```

The script is safe to re-run, and it reads both secret values from your terminal, never from arguments. It creates:

| Resource | Purpose | Permissions |
|---|---|---|
| Artifact Registry repo `sentinel` | Container images | n/a |
| SA `sentinel-deployer` | Used by GitHub Actions | `run.admin` (the deploy makes the service public, which sets its IAM policy), `artifactregistry.writer` on the repo, `iam.serviceAccountUser` on the runtime SA |
| SA `sentinel-runtime` | The identity the service runs as | Read access to the two secrets only |
| Workload Identity pool `github` + provider `github-oidc` | Lets GitHub Actions act as the deployer | Only for this repository, and only from `main` or `v*` tags; PR branches can't deploy |
| Secrets `sentinel-google-api-key`, `sentinel-api-keys` | `GOOGLE_API_KEY`, `SENTINEL_API_KEYS` | Mounted as env vars at deploy |

At the end it prints the five repository variables to set: `GCP_PROJECT_ID`, `GCP_REGION`, `GCP_WIF_PROVIDER`, `GCP_DEPLOYER_SA` and `GCP_RUNTIME_SA`. Until they exist, the Deploy workflow skips instead of failing.

## Deploy

```bash
git tag v0.5.0 && git push origin v0.5.0
```

You can also use **Actions → Deploy → Run workflow**. The service is deployed with:

| Setting | Value |
|---|---|
| Scaling | min 0 / max 2 instances (a hard cost ceiling) |
| Resources | 1 vCPU, 512 MiB |
| Concurrency | 20 requests per instance |
| Request timeout | 120 s |

After deploying, the workflow checks, without spending Gemini quota:
- `/healthz` returns 200;
- `/readyz` returns 200, meaning both keys are configured;
- an unauthenticated `/investigate` gets a 401.

## Call it

```bash
URL=$(gcloud run services describe sentinel --region "$REGION" --format='value(status.url)')
curl -s -X POST "$URL/investigate" \
  -H "Authorization: Bearer $SENTINEL_API_KEY" -H 'Content-Type: application/json' \
  -d '{"alert_id":"demo-1","title":"High p99 latency on checkout-api",
       "description":"p99 rose to 2.4s; DB connection pool saturation warnings.",
       "severity":"critical","service":"checkout-api","fired_at":"2026-07-12T09:31:00Z"}'
```

## Operate

```bash
# Roll back: list revisions, then send all traffic to a previous one
gcloud run revisions list --service sentinel --region "$REGION"
gcloud run services update-traffic sentinel --region "$REGION" --to-revisions=<revision>=100

# Rotate a key: add a new secret version, then redeploy so new instances pick it up
gcloud secrets versions add sentinel-api-keys --data-file=-

# Tear down
gcloud run services delete sentinel --region "$REGION"
gcloud artifacts repositories delete sentinel --location "$REGION"
```
