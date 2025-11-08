# Data in Motion – GCP Deployment Guide

This README explains how to deploy the hackathon prototype (Python FastAPI policy service, Go migration mover, optional Kafka substitute, and React UI) onto Google Cloud Platform using the Free Tier / Always Free allowances where possible.

Contents:
1. Overview
2. Architecture Variants (Kafka vs Pub/Sub)
3. Prerequisites
4. Resource Planning & Cost Awareness
5. Service Accounts & IAM
6. Container Build & Artifact Registry
7. Deploying Python & Go services to Cloud Run
8. Replacing Local Kafka (Options)
9. Cloud Storage Static Hosting for UI
10. Environment Configuration
11. Secret Management
12. Optional: Terraform Skeleton
13. Cleanup
14. Troubleshooting
15. Roadmap for Production Hardening

---

## 1. Overview

GCP Targets:
- Cloud Run (fully managed containers) for:
  - python-api (FastAPI + ML)
  - go-mover (migration orchestrator)
- Artifact Registry for container images
- Secret Manager for credentials (e.g., GCS bucket, service account keys if needed for cross-project)
- Cloud Storage bucket for static UI hosting (React build artifacts)
- Pub/Sub or temporary Redpanda in a VM for streaming (choose one)
- (Optional) Cloud Scheduler for periodic ML training job (invokes a /train endpoint or Cloud Run Job)

---

## 2. Architecture Variants

### Variant A: Use Kafka (Self-Managed)
- Deploy Single VM (e2-micro) with Redpanda or Kafka.
- Pros: Closer to original design
- Cons: Adds ops overhead; micro VM resource limits

### Variant B: Use Pub/Sub (Recommended for GCP)
- Replace Kafka topics:
  - access_events → pubsub topic `access-events`
  - migration_jobs → `migration-jobs`
  - migration_status → `migration-status`
- Refactor producers/consumers:
  - Python publishes events via Pub/Sub client library.
  - Go polls or receives push subscription.
- Pros: Managed, scales, no server maintenance.
- Cons: Semantic differences (ordering, partitioning).

### Variant C: Use Cloud Run Jobs for Migration
- Each migration job triggers a Cloud Run Job instance that executes copy/verify.
- Pros: Simplifies mover scaling.
- Cons: Adds cold start latency; requires adapter layer.

---

## 3. Prerequisites

Install:
- gcloud CLI
- Docker (or build via Cloud Build only)
- Node.js (if building UI locally)
- Python 3.11 / Go 1.22 (optional for local tests)

Authenticate:
```bash
gcloud auth login
gcloud auth application-default login
```

Set Project & Region:
```bash
PROJECT_ID="your-project-id"
REGION="us-central1"  # or any preferred region
gcloud config set project "$PROJECT_ID"
gcloud config set run/region "$REGION"
```

Enable Required APIs:
```bash
gcloud services enable \
  artifactregistry.googleapis.com \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  pubsub.googleapis.com \
  secretmanager.googleapis.com \
  storage.googleapis.com
```

---

## 4. Resource Planning & Cost Awareness

Always Free Tier Snapshot (check current GCP docs for updates):
- Cloud Run: limited free CPU & requests
- Cloud Storage: small bucket (e.g. 5GB in select regions)
- Pub/Sub: generous free operations tier
- e2-micro VM: one per billing account (if using VM-based Kafka)

Keep usage minimal:
- Disable high frequency polling
- Limit container memory (256Mi–512Mi)
- Use compressed artifacts in bucket

---

## 5. Service Accounts & IAM

Create dedicated service accounts:

```bash
gcloud iam service-accounts create python-api-sa \
  --display-name "Python API Service Account"

gcloud iam service-accounts create go-mover-sa \
  --display-name "Go Mover Service Account"
```

Grant roles (minimum principle of least privilege):

```bash
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:python-api-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/pubsub.publisher"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:python-api-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/storage.objectViewer"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:go-mover-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/pubsub.subscriber"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:go-mover-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/storage.objectAdmin"
```

(Adjust roles depending on design—avoid objectAdmin if not needed.)

---

## 6. Container Build & Artifact Registry

Create repository:
```bash
gcloud artifacts repositories create data-motion-repo \
  --repository-format=docker \
  --location="$REGION" \
  --description="Repository for Data in Motion services"
```

Configure Docker to use gcloud auth:
```bash
gcloud auth configure-docker "$REGION-docker.pkg.dev"
```

Build & Push Python API:
```bash
docker build -f deploy/docker/Dockerfile.python -t python-api:latest .
docker tag python-api:latest "$REGION-docker.pkg.dev/$PROJECT_ID/data-motion-repo/python-api:latest"
docker push "$REGION-docker.pkg.dev/$PROJECT_ID/data-motion-repo/python-api:latest"
```

Build & Push Go Mover:
```bash
docker build -f deploy/docker/Dockerfile.go -t go-mover:latest .
docker tag go-mover:latest "$REGION-docker.pkg.dev/$PROJECT_ID/data-motion-repo/go-mover:latest"
docker push "$REGION-docker.pkg.dev/$PROJECT_ID/data-motion-repo/go-mover:latest"
```

---

## 7. Deploying to Cloud Run

Python API (HTTP Port 8080):
```bash
gcloud run deploy python-api \
  --image "$REGION-docker.pkg.dev/$PROJECT_ID/data-motion-repo/python-api:latest" \
  --service-account "python-api-sa@$PROJECT_ID.iam.gserviceaccount.com" \
  --set-env-vars SQLITE_PATH=/data/metadata.db,MODEL_DIR=/models \
  --cpu=1 --memory=512Mi \
  --allow-unauthenticated
```

Go Mover (Port 8090 by default in Dockerfile):
```bash
gcloud run deploy go-mover \
  --image "$REGION-docker.pkg.dev/$PROJECT_ID/data-motion-repo/go-mover:latest" \
  --service-account "go-mover-sa@$PROJECT_ID.iam.gserviceaccount.com" \
  --set-env-vars DISABLE_KAFKA=true \
  --cpu=1 --memory=256Mi \
  --allow-unauthenticated
```

Mount persistent data? Cloud Run filesystem is ephemeral. Use:
- Cloud SQL / Firestore for metadata in production
- For prototype: Accept ephemeral; store metadata externally (e.g., Cloud Storage objects or move to Postgres Cloud SQL).
(Migrate away from local SQLite for production reliability.)

---

## 8. Replacing Local Kafka

### Using Pub/Sub

Create topics:
```bash
for t in access-events migration-jobs migration-status; do
  gcloud pubsub topics create "$t"
done
```

Create subscriptions:
```bash
gcloud pubsub subscriptions create migration-jobs-sub --topic=migration-jobs
gcloud pubsub subscriptions create migration-status-sub --topic=migration-status
```

Python: Replace Kafka producer calls with Pub/Sub publish (pseudo-code):
```python
from google.cloud import pubsub_v1
publisher = pubsub_v1.PublisherClient()
topic_path = publisher.topic_path(PROJECT_ID, "access-events")
publisher.publish(topic_path, json.dumps(event_payload).encode("utf-8"))
```

Go: Use Pub/Sub Go client for subscriber or pull model.

### Using Cloud Tasks (Alternative)
- For migration_jobs, a Cloud Task could enqueue an HTTP POST to go-mover endpoint.
- Simplifies consumer loop but less streaming semantics.

---

## 9. Cloud Storage Static Hosting (UI)

Create bucket (choose globally unique name):
```bash
UI_BUCKET="data-motion-ui-$PROJECT_ID"
gsutil mb -l "$REGION" "gs://$UI_BUCKET"
```

(Optional: enable public read for demo)
```bash
gsutil iam ch allUsers:objectViewer "gs://$UI_BUCKET"
```

Build UI:
```bash
cd ui
npm install
npm run build
gsutil -m rsync -r dist "gs://$UI_BUCKET"
```

Access URL:
- https://storage.googleapis.com/$UI_BUCKET/index.html
(Or configure custom domain + Cloud CDN; out of scope for hackathon.)

---

## 10. Environment Configuration

Runtime env examples (Python API):
- PUBSUB_PROJECT_ID
- ACCESS_EVENTS_TOPIC=access-events
- MIGRATION_JOBS_TOPIC=migration-jobs
- MIGRATION_STATUS_TOPIC=migration-status
- MODEL_DIR=/models (if using ephemeral volume for test)
- GCS_BUCKET_HOT= (optional)
- GCS_BUCKET_COLD= (optional)
- LOG_LEVEL=INFO

Go Mover:
- PUBSUB_PROJECT_ID
- MIGRATION_JOBS_SUB=migration-jobs-sub
- MIGRATION_STATUS_TOPIC=migration-status
- STORAGE_LOCAL_ROOT=/tmp (if ephemeral)
- LOG_LEVEL=INFO

---

## 11. Secret Management

Store secrets (if any—avoid storing DB passwords directly in env):
```bash
echo -n "my-secret-value" | gcloud secrets create api-secret --data-file=-
```

Access in Cloud Run:
```bash
gcloud run services update python-api \
  --update-secrets API_SECRET=api-secret:latest
```

In container:
```python
import os
api_secret = os.getenv("API_SECRET")
```

---

## 12. Optional: Terraform Skeleton

Example snippet (main.tf):
```hcl
provider "google" {
  project = var.project_id
  region  = var.region
}

resource "google_artifact_registry_repository" "repo" {
  location        = var.region
  repository_id   = "data-motion-repo"
  format          = "DOCKER"
  description     = "Data in Motion Images"
}

resource "google_pubsub_topic" "access_events" {
  name = "access-events"
}

resource "google_pubsub_topic" "migration_jobs" {
  name = "migration-jobs"
}

resource "google_pubsub_topic" "migration_status" {
  name = "migration-status"
}

resource "google_pubsub_subscription" "migration_jobs_sub" {
  name  = "migration-jobs-sub"
  topic = google_pubsub_topic.migration_jobs.name
}

# Cloud Run service definitions would follow with google_cloud_run_service
```

Variables (variables.tf):
```hcl
variable "project_id" { type = string }
variable "region"     { type = string  default = "us-central1" }
```

Apply:
```bash
terraform init
terraform apply -var="project_id=$PROJECT_ID"
```

---

## 13. Cleanup

Remove Cloud Run services:
```bash
gcloud run services delete python-api
gcloud run services delete go-mover
```

Delete topics:
```bash
for t in access-events migration-jobs migration-status; do
  gcloud pubsub topics delete "$t"
done
```

Remove bucket (be careful):
```bash
gsutil -m rm -r "gs://$UI_BUCKET"
```

Delete Artifact Registry repository (or manually purge images):
```bash
gcloud artifacts repositories delete data-motion-repo --location="$REGION"
```

---

## 14. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| 403 on UI assets | Bucket not public | Adjust IAM or use signed URLs |
| Cloud Run cold starts | Low traffic | Consider min instances (cost trade-off) |
| Pub/Sub ordering differences | Pub/Sub is only per subscription best-effort | Include sequence or timestamp; enforce ordering in app logic |
| SQLite data lost | Ephemeral FS | Move to Cloud SQL or Firestore |
| Memory exceeded | Container limit too low | Increase memory or profile usage |
| Pub/Sub permission errors | Missing IAM role | Grant roles/pubsub.publisher or subscriber to service account |

---

## 15. Roadmap for Production Hardening

- Migrate metadata to Cloud SQL (Postgres) + SQLAlchemy migration scripts
- Replace heuristic ML with scheduled training via Cloud Scheduler + Cloud Run Job
- Integrate Cloud Logging & Cloud Monitoring dashboards
- Add IAM-scoped endpoints + OAuth (Identity Platform)
- Set up separate dev/stage/prod environments with Terraform
- Implement automated canary & rollback (Cloud Deploy)
- Enable VPC egress controls + Private Service Connect if using private resources
- Add SLO dashboards for migration success latency, recommendation freshness

---

## Quick Command Summary

```bash
# Enable APIs
gcloud services enable run.googleapis.com artifactregistry.googleapis.com pubsub.googleapis.com

# Build images
docker build -f deploy/docker/Dockerfile.python -t python-api:latest .
docker build -f deploy/docker/Dockerfile.go -t go-mover:latest .

# Push images
docker tag python-api:latest "$REGION-docker.pkg.dev/$PROJECT_ID/data-motion-repo/python-api:latest"
docker push "$REGION-docker.pkg.dev/$PROJECT_ID/data-motion-repo/python-api:latest"

# Deploy Cloud Run
gcloud run deploy python-api --image "$REGION-docker.pkg.dev/$PROJECT_ID/data-motion-repo/python-api:latest" --allow-unauthenticated
gcloud run deploy go-mover   --image "$REGION-docker.pkg.dev/$PROJECT_ID/data-motion-repo/go-mover:latest"   --allow-unauthenticated

# Pub/Sub topics
for t in access-events migration-jobs migration-status; do
  gcloud pubsub topics create "$t"
done
```

---

## Final Notes

This guide optimizes for rapid hackathon deployment while indicating upgrade paths to a production-grade, secure, and scalable GCP footprint. Adjust resource sizes, apply least-privilege IAM, and add observability before any real data workload.

Happy deploying!