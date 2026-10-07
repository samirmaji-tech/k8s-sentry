# K8s-Sentry 🛡️

![Python](https://img.shields.io/badge/python-3.12-blue)
![Terraform](https://img.shields.io/badge/IaC-Terraform-7B42BC)
![Azure AKS](https://img.shields.io/badge/cloud-Azure%20AKS-0078D4)
![Agent mode](https://img.shields.io/badge/agent-read--only-green)
![CI](https://img.shields.io/badge/CI-GitHub%20Actions-2088FF)

## Architecture

<p align="center">
  <img src="https://raw.githubusercontent.com/samirmaji-tech/k8s-sentry/main/docs/architecture.png" alt="K8s-Sentry architecture" width="100%">
</p>**An Autonomous AI-Powered Infrastructure Troubleshooting & Incident Commander Agent for Azure AKS.**

> Alert fires → agent collects live Kubernetes evidence → secrets are masked → Claude finds the root cause → a safe, advisory fix lands in Slack for a human to review.

K8s-Sentry is a read-only AI SRE agent. It listens for alerts (Azure Monitor;
AWS SNS/CloudWatch also supported), pulls live Kubernetes telemetry from the
failing workload, sanitises it, asks an LLM to perform root-cause analysis, and
posts a clean, actionable incident summary to Slack — with **safe, advisory**
remediation steps a human reviews before running.

> Portfolio context — Project 3 of a Cloud/DevOps series
> (Project 1: GKE GitOps & Observability · Project 2: Zero-Trust DevSecOps).
> This project demonstrates event-driven automation, the Kubernetes API,
> LLM tool-use with strict output contracts, and defence-in-depth guardrails.

---

## Table of contents

1. [Architecture](#architecture)
2. [How it works — the incident pipeline](#how-it-works--the-incident-pipeline)
3. [Demo](#demo)
4. [Repository layout](#repository-layout)
5. [Prerequisites](#prerequisites)
6. [Setup](#setup)
7. [Running & testing the agent](#running--testing-the-agent)
8. [Tests & CI/CD](#tests--cicd)
9. [Security guardrails](#security-guardrails)
10. [Cost & teardown](#cost--teardown)
11. [Troubleshooting](#troubleshooting)
12. [Roadmap ideas](#roadmap-ideas)
13. [License](#license)

---

## Architecture

![K8s-Sentry architecture](docs/architecture.png)

*Terraform provisions the Azure AKS infrastructure; K8s-Sentry then detects incidents, collects Kubernetes evidence, performs AI-assisted root cause analysis and delivers actionable notifications.*

<details>
<summary>Text version of the platform (Mermaid)</summary>

```mermaid
flowchart TD
    DEV[Developer] -->|git push| GH[GitHub repository]
    GH --> CI["GitHub Actions<br/>lint, test, az acr build"]
    CI -->|push image| ACR[Azure Container Registry]
    GH -->|terraform apply| TF[Terraform]
    TF -->|provisions| AKS

    AM[Azure Monitor alert rule] --> AG["Action Group<br/>webhook POST"]
    AG --> P

    subgraph AKS["AKS cluster · devops-lab namespace"]
        direction LR
        P["Parse<br/>main.py"] --> C["Collect<br/>agent.py"]
        C --> M["Mask secrets and PII<br/>masking.py"]
        M --> A["Analyse<br/>llm_analyzer.py"]
        A --> N["Notify<br/>notifier.py"]
    end

    ACR -->|image pull| AKS
    C -->|"get / list / watch"| K8S["Kubernetes API<br/>read-only RBAC"]
    A -->|masked evidence| LLM["Claude API<br/>strict JSON"]
    N --> SL["Slack<br/>Block Kit card"]
    SL --> HR[Human review]

    style M fill:#FAECE7,stroke:#993C1D,color:#712B13
```

</details>

Code-level sketch of the agent:

```
                    ┌──────────────────────┐
   Azure Monitor ──►│   Action Group        │
   alert rule       │   (webhook action)    │
                    └───────────┬───────────┘
                                │  HTTP POST (alert JSON)
                                ▼
   ┌───────────────────────────────────────────────────────────┐
   │  K8s-Sentry  (FastAPI, runs in-cluster on AKS)             │
   │                                                            │
   │   main.py      ── parse alert, orchestrate                 │
   │      │                                                     │
   │      ▼                                                     │
   │   agent.py     ── READ-ONLY: describe pod, events, logs ───┼──► AKS API
   │      │                                          (RBAC: get/list/watch)
   │      ▼                                                     │
   │   masking.py   ── strip secrets / PII                      │
   │      │                                                     │
   │      ▼                                                     │
   │   llm_analyzer ── Anthropic API, strict JSON contract ─────┼──► Claude
   │      │                                                     │
   │      ▼                                                     │
   │   notifier.py  ── format Block Kit summary ───────────────┼──► Slack
   └───────────────────────────────────────────────────────────┘
```

The agent runs as a Deployment inside the `devops-lab` namespace using a
ServiceAccount bound to a **read-only** ClusterRole. It never mutates cluster
state; remediation is always advice for a human.

> **Cloud:** this repo's Terraform provisions **Azure AKS**. For a full
> beginner-friendly Azure walkthrough (Mac + Windows), see
> [`docs/K8s-Sentry-AKS-Guide.md`](docs/K8s-Sentry-AKS-Guide.md). The webhook
> also accepts AWS SNS/CloudWatch payloads, so the agent is cloud-portable.

---

## How it works — the incident pipeline

An alert hits `POST /webhook`. The agent then:

1. **Parses the alert.** Handles the **Azure Monitor common alert schema**
   (`data.alertContext.condition.allOf[].dimensions`), the AWS SNS envelope
   (including the one-time `SubscriptionConfirmation` handshake), CloudWatch
   alarm JSON (`Trigger.Dimensions`), or a plain `{ "namespace": ..., "pod": ... }`
   body. The target namespace is **coerced to the configured scope** so an alert
   can never point the agent at a namespace it isn't allowed to inspect.
2. **Collects evidence** (`agent.py`) via the Kubernetes API — the same things
   a human runs by hand: `describe pod`, recent `events`, and the last
   `LOG_TAIL_LINES` (default 50) log lines per container. For crash-looping
   pods it automatically falls back to the **previous** container instance's
   logs, where the real error lives.
3. **Masks** (`masking.py`) the entire evidence bundle — API keys, JWTs, AWS
   keys, connection strings, emails, IPs, and `password=`/`token=` style
   pairs are redacted **before anything leaves the cluster**.
4. **Analyses** (`llm_analyzer.py`) by sending the masked evidence to Claude
   under a strict system prompt that forces a single JSON object:

   ```json
   {
     "root_cause": "…",
     "severity": "CRITICAL | HIGH | MEDIUM | LOW | INFO",
     "remediation_steps": ["safe kubectl / diagnostic commands", "…"],
     "prevention_tip": "…"
   }
   ```

   The response is defensively parsed (code-fence tolerant) and schema-validated
   with Pydantic. If the LLM is unavailable or misbehaves, a deterministic
   heuristic fallback keeps the agent useful.
5. **Notifies** (`notifier.py`) Slack with a colour-coded Block Kit summary.
   Slack failures are logged, never fatal.
6. **Returns** the structured result over HTTP for logging/automation.

If no pod is named in the alert, the agent **scans** the namespace and analyses
every unhealthy pod it finds.

---

## Demo

<!-- Add a screenshot of a real incident card in Slack at docs/slack-incident.png -->
![Slack incident card](docs/slack-incident.png)

*An incident card posted to Slack for the `crashloop-app` test fault: root
cause, severity, safe remediation commands and a prevention tip.*

---

## Repository layout

```
k8s-sentry/
├── app/
│   ├── __init__.py
│   ├── main.py            # FastAPI app: /webhook, /analyze, /healthz
│   ├── agent.py           # Kubernetes read-only telemetry collector
│   ├── llm_analyzer.py    # Anthropic root-cause analysis (strict JSON)
│   ├── notifier.py        # Slack Block Kit notifier
│   ├── masking.py         # Secret / PII redaction (pre-LLM)
│   └── config.py          # Env-driven settings
├── tests/                 # pytest suite (masking, LLM parsing, alert parsing)
│   ├── conftest.py
│   ├── test_masking.py
│   ├── test_llm_analyzer.py
│   └── test_alert_parsing.py
├── charts/k8s-sentry/     # Helm chart (namespace, RBAC, deploy, svc, secret)
│   ├── Chart.yaml
│   ├── values.yaml
│   └── templates/
├── .github/workflows/
│   └── ci.yml             # lint + test + build & push image (see note in CI/CD)
├── manifests/
│   ├── 00-namespace.yaml          # devops-lab namespace
│   ├── 01-rbac.yaml               # read-only SA + ClusterRole + RoleBinding
│   ├── 10-fault-crashloop.yaml    # test fault: CrashLoopBackOff pod
│   ├── 11-fault-imagepull.yaml    # test fault: ImagePullBackOff deployment
│   ├── 20-sentry-deployment.yaml  # the agent Deployment + Service
│   └── 21-secret.example.yaml     # secret template (do NOT commit real keys)
├── terraform/
│   ├── versions.tf        # azurerm provider + (optional) azurerm backend
│   ├── variables.tf       # location, resource group, cluster, node sizing
│   ├── main.tf            # resource group + AKS (aks-sentry-cluster)
│   └── outputs.tf         # kubeconfig command, resource_group_name, cluster_name
├── docs/
│   ├── architecture.png           # platform architecture diagram
│   ├── slack-incident.png         # demo screenshot
│   └── K8s-Sentry-AKS-Guide.md    # full Mac+Windows Azure walkthrough
├── examples/
│   ├── azure-monitor-alert.json   # sample Azure Monitor common alert schema
│   ├── sns-cloudwatch-alert.json  # sample AWS SNS/CloudWatch payload
│   ├── manual-trigger.json        # {namespace, pod}
│   └── scan-namespace.json        # {namespace} only -> scan mode
├── Dockerfile             # multi-stage, non-root, healthcheck
├── requirements.txt
├── requirements-dev.txt   # pytest, ruff
├── pyproject.toml         # ruff + pytest config
├── .env.example
├── .dockerignore
├── .gitignore
└── README.md
```

---

## Prerequisites

- Azure account + `az login` (the Azure CLI signed in)
- Terraform ≥ 1.5, `kubectl`, `az` (Azure CLI), Docker
- Python 3.10+ (3.12 recommended, for local runs)
- An **Anthropic API key** (`ANTHROPIC_API_KEY`)
- A **Slack incoming webhook** URL (optional — set `SLACK_ENABLED=false` to skip)

> New to the tooling? The [AKS guide](docs/K8s-Sentry-AKS-Guide.md) has copy-paste
> install commands for macOS (Homebrew) and Windows (winget/Chocolatey).

---

## Setup

### 1. Provision the AKS cluster (Terraform)

```bash
az login                 # sign in; pick a subscription with `az account set`
cd terraform
terraform init
terraform apply          # creates aks-sentry-cluster (~5–10 min)

# Point kubectl at the new cluster (command is also a Terraform output):
az aks get-credentials \
  --resource-group "$(terraform output -raw resource_group_name)" \
  --name "$(terraform output -raw cluster_name)"
kubectl get nodes
```

> Prefer a different region/size? Override the variables, e.g.
> `terraform apply -var="location=centralindia" -var="node_count=1"`.

### 2. Deploy the namespace, RBAC and test faults

```bash
cd ..
kubectl apply -f manifests/00-namespace.yaml
kubectl apply -f manifests/01-rbac.yaml
kubectl apply -f manifests/10-fault-crashloop.yaml
kubectl apply -f manifests/11-fault-imagepull.yaml

# Watch the faults appear:
kubectl -n devops-lab get pods -w
# crashloop-app        0/1   CrashLoopBackOff
# imagepull-app-...    0/1   ImagePullBackOff
```

### 3. Create the agent's secret

```bash
kubectl -n devops-lab create secret generic k8s-sentry-secrets \
  --from-literal=ANTHROPIC_API_KEY="sk-ant-..." \
  --from-literal=ANTHROPIC_MODEL="claude-sonnet-4-5-20250929" \
  --from-literal=SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..." \
  --from-literal=SLACK_ENABLED="true"
```

### 4. Build & push the image, then deploy the agent

```bash
# Create an Azure Container Registry (once) and attach it to the cluster so AKS
# can pull without imagePullSecrets:
RG=$(terraform -chdir=terraform output -raw resource_group_name)
AKS=$(terraform -chdir=terraform output -raw cluster_name)
ACR=acrsentry$RANDOM          # must be globally unique, lowercase alphanumeric
az acr create --resource-group "$RG" --name "$ACR" --sku Basic
az aks update --resource-group "$RG" --name "$AKS" --attach-acr "$ACR"

# Build & push straight to ACR (no local Docker needed with `az acr build`):
az acr build --registry "$ACR" --image k8s-sentry:0.1.0 .
LOGIN=$(az acr show --name "$ACR" --query loginServer -o tsv)   # e.g. acrsentry123.azurecr.io

# Point the Deployment at your image, then apply:
sed -i.bak "s|image: k8s-sentry:0.1.0|image: $LOGIN/k8s-sentry:0.1.0|" manifests/20-sentry-deployment.yaml
kubectl apply -f manifests/20-sentry-deployment.yaml
kubectl -n devops-lab rollout status deploy/k8s-sentry
```

### 5. (Alternative) Deploy with Helm

Instead of raw manifests, install the whole agent — namespace, read-only RBAC,
ConfigMap, Secret, Deployment and Service — from the bundled chart:

```bash
helm lint charts/k8s-sentry
helm template k8s-sentry charts/k8s-sentry   # render & review before applying

helm upgrade --install k8s-sentry charts/k8s-sentry \
  --set image.repository="$LOGIN/k8s-sentry" \
  --set image.tag=0.1.0 \
  --set secrets.anthropicApiKey="sk-ant-..." \
  --set secrets.slackWebhookUrl="https://hooks.slack.com/services/..."
```

For production, keep secrets out of Helm: set `secrets.create=false` and point
the chart at a Secret you manage (e.g. External Secrets Operator):

```bash
helm upgrade --install k8s-sentry charts/k8s-sentry \
  --set secrets.create=false --set secrets.existingSecret=k8s-sentry-secrets \
  --set image.repository="$LOGIN/k8s-sentry" --set image.tag=0.1.0
```

Key `values.yaml` knobs: `namespace.*`, `image.*`, `config.*` (model, log tail,
unhealthy reasons), `secrets.*`, `rbac.scope` (`namespaced` | `cluster`), and
the `resources`/`securityContext` blocks.

---

## Running & testing the agent

### Trigger it manually (in-cluster)

```bash
kubectl -n devops-lab port-forward svc/k8s-sentry 8080:80 &

# Analyse a specific failing pod:
curl -s -X POST localhost:8080/analyze \
  -H 'Content-Type: application/json' \
  -d @examples/manual-trigger.json | jq

# Or scan the whole namespace for unhealthy pods:
curl -s -X POST localhost:8080/analyze \
  -H 'Content-Type: application/json' \
  -d @examples/scan-namespace.json | jq
```

### Simulate an Azure Monitor alert

```bash
curl -s -X POST localhost:8080/webhook \
  -H 'Content-Type: application/json' \
  -d @examples/azure-monitor-alert.json | jq
# (examples/sns-cloudwatch-alert.json also works — the webhook is cloud-portable)
```

You should get JSON like:

```json
{
  "namespace": "devops-lab",
  "analysed": 1,
  "incidents": [
    {
      "pod": "crashloop-app",
      "reason": "CrashLoopBackOff",
      "analysis": {
        "root_cause": "The container exits non-zero at startup because a required DB password env var (***REDACTED***) is unset.",
        "severity": "HIGH",
        "remediation_steps": [
          "kubectl -n devops-lab logs crashloop-app --previous --tail=50",
          "kubectl -n devops-lab describe pod crashloop-app",
          "Provide the missing DB_PASSWORD via a Secret and redeploy."
        ],
        "prevention_tip": "Validate required env vars at boot and add a startup probe."
      },
      "slack_delivered": true
    }
  ]
}
```

...and a matching card in Slack.

### Wire up real alerts (Azure Monitor → agent)

Enable **Container Insights** on the AKS cluster, create an **alert rule**
(e.g. on `pod_number_of_container_restarts`) scoped to `devops-lab`, and attach
an **Action Group** with a **webhook** action pointing at the agent's `/webhook`
URL. The agent reads the namespace/pod straight from the Azure Monitor common
alert schema; if the alert names no pod, it scans the namespace instead. (An AWS
SNS topic + CloudWatch alarm works too — the agent auto-confirms the SNS
subscription handshake on first call.)

### Run locally (no cluster deploy)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # fill in your keys; uses your local kubeconfig
uvicorn app.main:app --reload --port 8080
```

---

## Tests & CI/CD

The `tests/` suite runs fully offline (no cluster, no API key — `conftest.py`
forces the fallback paths) and covers the three things most likely to break: the
secret/PII masking guardrail, the LLM JSON-extraction logic, and webhook alert
parsing.

```bash
pip install -r requirements-dev.txt
ruff check app tests      # lint
pytest -q                 # 33 tests
```

`.github/workflows/ci.yml` runs on every push and PR:

- **lint-test** — installs deps, runs `ruff check` and `pytest`.
- **build-push** — on pushes to `main`/tags only, signs in to Azure via
  **GitHub OIDC** (no static credentials) and runs `az acr build` to build the
  image server-side in **Azure Container Registry** and push it, tagged with the
  commit SHA and `latest`.

To enable the publish job, set up a **federated credential** so GitHub Actions
can sign in to Azure without secrets:

```bash
# 1. Create an app registration (service principal) and note its appId.
az ad sp create-for-rbac --name "k8s-sentry-ci"
APP_ID=$(az ad sp list --display-name "k8s-sentry-ci" --query "[0].appId" -o tsv)

# 2. Grant it push rights on your ACR.
ACR_ID=$(az acr show --name <ACR_NAME> --query id -o tsv)
az role assignment create --assignee "$APP_ID" --role AcrPush --scope "$ACR_ID"

# 3. Add a federated credential trusting your repo's main branch.
az ad app federated-credential create --id "$APP_ID" --parameters '{
  "name": "github-main",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:<OWNER>/<REPO>:ref:refs/heads/main",
  "audiences": ["api://AzureADTokenExchange"]
}'

# 4. (Only if you cut versioned releases with git tags, e.g. v1.0.0)
#    Add a SECOND federated credential trusting version-tag pushes.
#    Without this, the build-push job fails when triggered by a `v*` tag,
#    because a tag run is a different "subject" than a main-branch run.
az ad app federated-credential create --id "$APP_ID" --parameters '{
  "name": "github-tags",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:<OWNER>/<REPO>:ref:refs/tags/v*",
  "audiences": ["api://AzureADTokenExchange"]
}'
```

> **Branch vs tag runs.** A federated credential's `subject` must match the exact
> trigger. The workflow runs on both pushes to `main` and pushes of `v*` tags, so
> each needs its own credential (steps 3 and 4). If you only ever push to `main`,
> step 4 is optional.

Then add these to your GitHub repo (**Settings → Secrets and variables → Actions**):

| Kind | Name | Value |
| --- | --- | --- |
| Secret | `AZURE_CLIENT_ID` | the app registration's `appId` |
| Secret | `AZURE_TENANT_ID` | `az account show --query tenantId -o tsv` |
| Secret | `AZURE_SUBSCRIPTION_ID` | `az account show --query id -o tsv` |
| Variable | `ACR_NAME` | your registry name, e.g. `acrsentry123` (not the login server) |

---

## Security guardrails

Defence-in-depth is the point of this project. The guardrails are layered so a
failure of any single one does not lead to a destructive action or a secret
leak.

### 1. Read-only Kubernetes access (enforced by RBAC, not by trust)

The agent's ServiceAccount is bound to a ClusterRole with **only**
`get`, `list`, `watch` on pods, pod logs, events, and workload objects
(`manifests/01-rbac.yaml`). There is **no** `create/update/patch/delete/exec`
verb anywhere, and Secrets are deliberately excluded from the rules. Even if the
LLM were prompt-injected into "suggesting" `kubectl delete`, the agent's
credentials physically cannot perform it against the API server.

### 2. Advisory-only remediation

`AGENT_MODE=read_only` and the LLM system prompt both constrain output to
*suggested* commands. The agent never shells out to `kubectl` or the API to make
changes — `remediation_steps` are strings for a human to review. Namespace,
PersistentVolume, and cluster-scoped deletions are explicitly forbidden by the
prompt.

### 3. Secret & PII masking before the LLM

`masking.py` scrubs the **entire** evidence bundle before it is sent to Claude:
API keys (`sk-…`), AWS access keys (`AKIA…`), JWTs, GitHub/Slack tokens, private
key blocks, `scheme://user:pass@host` connection strings, `password=/token=`
pairs, `Bearer` tokens, email addresses, and IPv4 addresses. Redactions keep the
*shape* (`***REDACTED_JWT***`) so the model still understands "a secret was here"
without ever seeing it. Extend the `_RULES` list for org-specific patterns.

### 4. Namespace scoping

Alerts are coerced to `TARGET_NAMESPACE`; the agent will not inspect a namespace
outside its configured scope even if an alert asks it to. Combine with the
namespace-scoped RoleBinding for two independent layers.

### 5. Bounded data collection

Logs are capped at `LOG_TAIL_LINES` (default 50) per container, limiting both
LLM cost and the volume of potentially sensitive data handled.

### 6. Hardened container & least privilege

The image is multi-stage, runs as a **non-root** user (UID 10001) with a
read-only root filesystem, `allowPrivilegeEscalation: false`, all Linux
capabilities dropped, and `seccompProfile: RuntimeDefault`
(`manifests/20-sentry-deployment.yaml`).

### 7. Secrets hygiene

`.env` and `*.tfvars`/state are git-ignored. Prefer creating the Kubernetes
Secret from the CLI (or, in production, the **Azure Key Vault Secrets Store CSI
driver** / **External Secrets Operator**) over committing a Secret manifest.

> **Production hardening checklist:** restrict the AKS API server to known IP
> ranges (authorized IP ranges), front the webhook with auth, store secrets in
> **Azure Key Vault**, enable AKS control-plane diagnostic logs, and run the LLM
> calls through a rate limiter and per-incident budget cap.

---

## Cost & teardown

This lab runs an AKS cluster (Free control-plane tier) plus 2× `Standard_B2s`
nodes and a standard load balancer — a few dollars a day. **Tear it down when
you're finished:**

```bash
kubectl delete -f manifests/ --ignore-not-found
cd terraform && terraform destroy
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `403 Forbidden` from the K8s API | RBAC not applied | `kubectl apply -f manifests/01-rbac.yaml` |
| Agent returns heuristic fallback text | `ANTHROPIC_API_KEY` unset/invalid | Check the `k8s-sentry-secrets` Secret |
| No Slack message | `SLACK_ENABLED=false` or bad webhook | Verify `SLACK_WEBHOOK_URL`; check pod logs |
| `crashloop-app` logs are empty | Logs are in the previous instance | The agent already falls back to `--previous`; manually: `kubectl -n devops-lab logs crashloop-app --previous` |
| Terraform auth errors | Not logged in / wrong subscription | `az account show`; re-run `az login` and `az account set` |

---

## Roadmap ideas

- Persist incidents to a datastore (e.g. Azure Table Storage / Cosmos DB) and
  correlate recurring root causes.
- Add a "confidence" score and auto-open a Jira/GitHub issue on HIGH+.
- Optional, human-approved auto-remediation via a separate, tightly-scoped
  write identity behind a manual gate.

---

## License

Released under the [MIT License](LICENSE).

---

_Built by Samir Maji · Cloud/DevOps portfolio._
