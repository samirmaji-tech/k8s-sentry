# K8s-Sentry on Azure AKS — End-to-End Deployment Guide

**An Autonomous AI-Powered Infrastructure Troubleshooting & Incident Commander Agent**

This guide walks you from an empty machine to a running AI SRE agent on **Azure
Kubernetes Service (AKS)**. It is written to be **beginner-friendly** and gives
commands for **both macOS and Windows (PowerShell)** wherever they differ.

> **How to read this guide.** Commands are shown in labelled blocks — 🍎 **macOS**
> and 🪟 **Windows (PowerShell)**. When a command is identical on both systems it
> is shown once under **Both**. Copy the block that matches your machine.

---

## Table of contents

1. [Project Overview & Architecture](#1-project-overview--architecture)
2. [Prerequisites & Tools Installation](#2-prerequisites--tools-installation)
3. [Step-by-Step Implementation Guide](#3-step-by-step-implementation-guide)
4. [Production Guardrails & Security](#4-production-guardrails--security)
5. [Cleanup / Teardown](#5-cleanup--teardown)
6. [Troubleshooting](#6-troubleshooting)

---

## 1. Project Overview & Architecture

### 1.1 What K8s-Sentry does (in plain English)

When something breaks in a Kubernetes cluster — a pod keeps crashing, or an image
won't download — a human SRE (Site Reliability Engineer) normally opens a
terminal and runs a few `kubectl` commands to *describe* the pod, read its
*events*, and tail its *logs*. They then eyeball all that output, figure out the
root cause, and decide what to do.

**K8s-Sentry automates the tedious first 80% of that job.** It is a small web
service that:

1. **Listens** for an alert (from Azure Monitor, or a manual trigger).
2. **Gathers evidence** from the cluster automatically — the same describe /
   events / logs a human would collect — using **read-only** access.
3. **Sanitises** that evidence, stripping out passwords, tokens, and other
   secrets before anything leaves the cluster.
4. **Asks an LLM (Claude)** to act as a senior SRE and return a structured
   diagnosis: root cause, severity, safe remediation steps, and a prevention tip.
5. **Posts a clean summary to Slack** so a human can review and act.

Crucially, the agent **never changes the cluster**. It only ever *reads*, and the
remediation steps it suggests are advice for a human to run — not commands it
executes itself. (More on this in [Section 4](#4-production-guardrails--security).)

### 1.2 How the components interact

```
   ┌──────────────┐   pod restarts / not-ready    ┌───────────────────────┐
   │   AKS         │ ─────────────────────────────►│   Azure Monitor        │
   │  (your        │      metrics & container      │  (alert rule +         │
   │   cluster)    │      insights                 │   Action Group)        │
   └──────────────┘                                └───────────┬───────────┘
          ▲                                                     │ HTTP POST
          │ read-only                                           │ (webhook)
          │ Kubernetes API                                      ▼
          │ (get/list/watch)                    ┌───────────────────────────┐
          │                                      │  K8s-Sentry (FastAPI)      │
          └──────────────────────────────────────┤                            │
                                                  │  1. parse the alert        │
                                                  │  2. collect evidence ──────┼──► AKS API
                                                  │  3. mask secrets / PII     │
                                                  │  4. analyse ───────────────┼──► Claude (LLM)
                                                  │  5. notify ────────────────┼──► Slack
                                                  └────────────────────────────┘
```

Optional (for those with the Mermaid extension in VS Code):

```mermaid
flowchart LR
    AKS[AKS cluster] -->|metrics / container insights| AM[Azure Monitor + Action Group]
    AM -->|webhook POST| API[K8s-Sentry / FastAPI]
    API -->|read-only get/list/watch| K8S[(Kubernetes API)]
    API -->|masked evidence| LLM[Claude LLM]
    LLM -->|JSON diagnosis| API
    API -->|incident summary| SLACK[Slack]
```

**The end-to-end flow:**
`AKS → Azure Monitor / Webhook → FastAPI → Kubernetes API → Claude LLM → Slack`

1. A pod misbehaves in AKS (e.g. `CrashLoopBackOff`).
2. **Azure Monitor** (via Container Insights) fires an **alert rule**; its
   **Action Group** calls a **webhook** — the agent's `/webhook` endpoint.
3. **FastAPI** receives the alert and runs the pipeline.
4. The agent calls the **Kubernetes API** (read-only) to pull the pod's
   description, events, and last 50 log lines.
5. After masking secrets, the evidence goes to **Claude**, which returns a strict
   JSON diagnosis.
6. The agent formats that into a **Slack** message.

> **Note on the Azure webhook.** The agent's `/webhook` natively understands the
> **Azure Monitor common alert schema** — it reads the `namespace` and `pod`
> straight from the alert's dimensions. If the alert names a specific pod, the
> agent analyses that pod; if it doesn't, the agent simply **scans the
> `devops-lab` namespace** and analyses every unhealthy pod it finds. That means
> even a generic Azure Monitor webhook (which just needs to reach the endpoint)
> triggers a useful run — no Logic App or payload reshaping required.

### 1.3 Tech stack

| Layer | Technology |
| --- | --- |
| Cloud & orchestration | **Azure AKS**, Azure Monitor (Container Insights) |
| Infrastructure as Code | **Terraform** (`azurerm` provider) |
| Backend API | **Python 3.10+**, **FastAPI**, **Uvicorn** |
| Cluster access | **kubernetes** Python client (read-only) |
| AI analysis | **Anthropic Claude** (via the `anthropic` Python SDK) |
| Notifications | **Slack** incoming webhook (via `httpx`) |
| Packaging | **Docker** (multi-stage, non-root) |

### 1.4 Project layout (what's in the repo)

```
k8s-sentry/
├── app/
│   ├── main.py            # FastAPI app: /webhook, /analyze, /healthz
│   ├── agent.py           # Read-only Kubernetes telemetry collector
│   ├── llm_analyzer.py    # Claude root-cause analysis (strict JSON output)
│   ├── notifier.py        # Slack notifier
│   ├── masking.py         # Secret / PII redaction (runs BEFORE the LLM)
│   └── config.py          # Environment-driven settings
├── manifests/             # Kubernetes YAML: namespace, RBAC, test faults, agent
├── terraform/             # Azure AKS infrastructure (azurerm)
├── examples/              # Sample alert payloads for testing
├── requirements.txt       # Python dependencies
├── .env.example           # Environment variable template
└── docs/                  # This guide
```

---

## 2. Prerequisites & Tools Installation

You will install five tools: **Azure CLI**, **kubectl**, **Terraform**,
**Python 3.10+**, and **Git**.

### 2.1 macOS — install Homebrew first

[Homebrew](https://brew.sh) is the standard macOS package manager. If you don't
have it yet:

```bash
# 🍎 macOS
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Then install everything in one line:

```bash
# 🍎 macOS
brew update
brew install azure-cli kubectl terraform python@3.12 git
```

> `brew install kubectl` gives you the standalone client. Alternatively,
> `az aks install-cli` (available after the Azure CLI is installed) installs both
> `kubectl` **and** `kubelogin`, which AKS uses for Microsoft Entra ID sign-in.

### 2.2 Windows — pick winget **or** Chocolatey

Windows 10/11 ships with **winget**. [Chocolatey](https://chocolatey.org/install)
is a popular alternative. Use whichever you prefer — you don't need both.

**Option A — winget (built in):**

```powershell
# 🪟 Windows (PowerShell)
winget install --exact --id Microsoft.AzureCLI
winget install --exact --id Kubernetes.kubectl
winget install --exact --id Hashicorp.Terraform
winget install --exact --id Python.Python.3.12
winget install --exact --id Git.Git
```

**Option B — Chocolatey (run PowerShell as Administrator):**

```powershell
# 🪟 Windows (PowerShell, as Administrator)
choco install azure-cli kubernetes-cli terraform python git -y
```

> **After installing, close and re-open PowerShell** so the new tools are on your
> `PATH`. On Windows you may prefer `az aks install-cli` for kubectl + kubelogin.

### 2.3 Verify every tool

Run these on **both** operating systems (the commands are the same):

```bash
# Both
az version
kubectl version --client
terraform version
python --version     # 🪟 Windows   → expect Python 3.12.x
python3 --version    # 🍎 macOS      → expect Python 3.12.x
git --version
```

> **Python command name.** On macOS the interpreter is usually `python3`; on
> Windows it's usually `python`. Use whichever prints a 3.10+ version on your
> machine. Throughout this guide, substitute the one that works for you.

You will also want **`jq`** (to pretty-print JSON) on macOS, and it's optional on
Windows because PowerShell formats JSON natively:

```bash
# 🍎 macOS (optional but handy)
brew install jq
```

---

## 3. Step-by-Step Implementation Guide

### 3.1 Open the project folder

```bash
# 🍎 macOS
cd ~/path/to/k8s-sentry
```

```powershell
# 🪟 Windows (PowerShell)
cd C:\path\to\k8s-sentry
```

### 3.2 Authenticate with Azure

Sign in. A browser window opens for you to log in.

```bash
# Both
az login
```

If your account has more than one subscription, pick the one to use:

```bash
# Both — list subscriptions, then select by ID or name
az account list --output table
az account set --subscription "<YOUR_SUBSCRIPTION_ID_OR_NAME>"
```

Confirm you're pointed at the right subscription:

```bash
# Both
az account show --output table
```

### 3.3 Provision the AKS infrastructure with Terraform

Terraform reads your Azure login automatically (via the `azurerm` provider), so
there are no keys to paste. Move into the `terraform` folder and run:

```bash
# Both (run from the project root)
cd terraform
terraform init          # downloads the Azure provider + modules
terraform plan          # preview what will be created (optional but recommended)
terraform apply         # type "yes" when prompted — takes ~5–10 minutes
```

This creates (typical AKS lab): a **resource group**, an **AKS cluster**
(e.g. `aks-sentry-cluster`), a default **node pool**, and the associated
networking. When it finishes, Terraform prints **outputs** you'll use next.

```bash
# Both — see the outputs again at any time
terraform output
```

> Want to change the region, cluster name, or node size? Edit
> `terraform/variables.tf` (or pass `-var` flags, e.g.
> `terraform apply -var="location=centralindia" -var="node_count=1"`).

Return to the project root when you're done:

```bash
# Both
cd ..
```

### 3.4 Connect kubectl to your new AKS cluster

`az aks get-credentials` merges the cluster's connection details into your local
`kubeconfig` so `kubectl` knows how to reach it. Use the resource group and
cluster name from your Terraform outputs.

**Using the Terraform output values directly:**

```bash
# 🍎 macOS
az aks get-credentials \
  --resource-group "$(terraform -chdir=terraform output -raw resource_group_name)" \
  --name "$(terraform -chdir=terraform output -raw cluster_name)"
```

```powershell
# 🪟 Windows (PowerShell)
$RG  = terraform -chdir=terraform output -raw resource_group_name
$AKS = terraform -chdir=terraform output -raw cluster_name
az aks get-credentials --resource-group $RG --name $AKS
```

**Or, if you know the names explicitly** (replace with your actual values):

```bash
# Both
az aks get-credentials --resource-group rg-k8s-sentry --name aks-sentry-cluster
```

Verify the connection — you should see your worker nodes in `Ready` state:

```bash
# Both
kubectl get nodes
```

> **If kubectl asks you to sign in again** (AKS with Microsoft Entra ID uses
> `kubelogin`), run `az aks install-cli` once to install `kubelogin`, then retry
> `kubectl get nodes`.

### 3.5 Set up environment variables (`.env`)

The app reads its configuration from a `.env` file. Start from the template:

```bash
# 🍎 macOS
cp .env.example .env
```

```powershell
# 🪟 Windows (PowerShell)
Copy-Item .env.example .env
```

Now open `.env` in VS Code and fill in your real values:

```bash
# Both
code .env
```

Set at least these keys:

```dotenv
# --- LLM (Anthropic Claude) ---
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxxxxxxxxxx
ANTHROPIC_MODEL=claude-sonnet-4-5-20250929

# --- Slack (optional: set SLACK_ENABLED=false to skip Slack entirely) ---
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T000/B000/xxxxxxxx
SLACK_ENABLED=true

# --- Kubernetes scope ---
TARGET_NAMESPACE=devops-lab
LOG_TAIL_LINES=50

# --- Agent behaviour (leave as-is) ---
AGENT_MODE=read_only
```

> **Where do these come from?**
> - `ANTHROPIC_API_KEY`: from the [Anthropic Console](https://console.anthropic.com/).
> - `SLACK_WEBHOOK_URL`: create an [Incoming Webhook](https://api.slack.com/messaging/webhooks)
>   in your Slack workspace. Don't have Slack? Set `SLACK_ENABLED=false` and the
>   agent will still return its analysis over HTTP.
>
> **Never commit `.env` to Git** — it contains secrets. The project's
> `.gitignore` already excludes it.

### 3.6 Create a Python virtual environment and install dependencies

A *virtual environment* keeps this project's Python packages isolated from the
rest of your system.

```bash
# 🍎 macOS
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

```powershell
# 🪟 Windows (PowerShell)
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt
```

> **Windows: "running scripts is disabled on this system"?** PowerShell blocks
> script activation by default. Allow it for the current window only, then
> activate again:
> ```powershell
> Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
> .\.venv\Scripts\Activate.ps1
> ```

When the environment is active you'll see `(.venv)` at the start of your prompt.
To leave it later, run `deactivate` on either OS.

### 3.7 Deploy the testbed: namespace, RBAC, and faulty workloads

These manifests are identical on both operating systems. They create the
`devops-lab` namespace, the agent's **read-only** permissions, and two
**intentionally broken** workloads so you have something for the agent to
diagnose.

```bash
# Both
kubectl apply -f manifests/00-namespace.yaml
kubectl apply -f manifests/01-rbac.yaml
kubectl apply -f manifests/10-fault-crashloop.yaml     # a pod that keeps crashing
kubectl apply -f manifests/11-fault-imagepull.yaml     # a deployment with a bad image tag
```

Watch the faults appear (press `Ctrl+C` to stop watching):

```bash
# Both
kubectl -n devops-lab get pods -w
```

Within a minute you should see something like:

```
NAME                             READY   STATUS             RESTARTS
crashloop-app                    0/1     CrashLoopBackOff   3
imagepull-app-6d4f8b9c7d-abcde   0/1     ImagePullBackOff   0
```

- **CrashLoopBackOff** — the container starts, fails, and Kubernetes keeps
  restarting it.
- **ImagePullBackOff** — Kubernetes can't download the container image (the tag
  doesn't exist).

These are two of the most common real-world Kubernetes failures — exactly what
K8s-Sentry is built to explain.

### 3.8 Run the FastAPI backend server

With your virtual environment still active (you should see `(.venv)`), start the
server. This command is the same on both systems:

```bash
# Both
uvicorn app.main:app --reload --port 8080
```

You should see log lines ending in `Application startup complete.` Leave this
terminal running. Open the interactive API docs in your browser to confirm it's
alive:

```
http://localhost:8080/docs
```

You can also hit the health check from a **second** terminal:

```bash
# 🍎 macOS
curl -s http://localhost:8080/healthz
```

```powershell
# 🪟 Windows (PowerShell)
Invoke-RestMethod -Uri http://localhost:8080/healthz
```

> **Running the server in-cluster instead?** Uvicorn/local is perfect for
> testing. To run the agent *inside* AKS (so it uses its own read-only
> ServiceAccount rather than your kubeconfig), build the Docker image, push it to
> **Azure Container Registry (ACR)**, and apply `manifests/20-sentry-deployment.yaml`.
> That path is covered in the repository's main `README.md`.

### 3.9 Trigger and test the AI agent

Open a **second terminal** (leave the server running in the first). Make sure the
two broken pods from step 3.7 exist.

#### a) Analyse one specific pod

```bash
# 🍎 macOS
curl -s -X POST http://localhost:8080/analyze \
  -H "Content-Type: application/json" \
  -d '{"namespace":"devops-lab","pod":"crashloop-app"}' | jq
```

```powershell
# 🪟 Windows (PowerShell)
$body = '{"namespace":"devops-lab","pod":"crashloop-app"}'
Invoke-RestMethod -Uri http://localhost:8080/analyze -Method Post `
  -ContentType "application/json" -Body $body | ConvertTo-Json -Depth 8
```

#### b) Scan the whole namespace (let the agent find the broken pods)

```bash
# 🍎 macOS
curl -s -X POST http://localhost:8080/analyze \
  -H "Content-Type: application/json" \
  -d '{"namespace":"devops-lab"}' | jq
```

```powershell
# 🪟 Windows (PowerShell)
Invoke-RestMethod -Uri http://localhost:8080/analyze -Method Post `
  -ContentType "application/json" -Body '{"namespace":"devops-lab"}' `
  | ConvertTo-Json -Depth 8
```

#### c) Simulate an Azure Monitor webhook

The `/webhook` endpoint is what Azure Monitor's Action Group calls. The repo
includes a realistic **Azure Monitor common alert schema** sample
(`examples/azure-monitor-alert.json`) — the agent reads the namespace and pod
directly from its dimensions:

```bash
# 🍎 macOS
curl -s -X POST http://localhost:8080/webhook \
  -H "Content-Type: application/json" \
  -d @examples/azure-monitor-alert.json | jq
```

```powershell
# 🪟 Windows (PowerShell)
$payload = Get-Content -Raw examples/azure-monitor-alert.json
Invoke-RestMethod -Uri http://localhost:8080/webhook -Method Post `
  -ContentType "application/json" -Body $payload | ConvertTo-Json -Depth 8
```

#### What a successful response looks like

```json
{
  "namespace": "devops-lab",
  "analysed": 1,
  "incidents": [
    {
      "pod": "crashloop-app",
      "reason": "CrashLoopBackOff",
      "analysis": {
        "root_cause": "The container exits with a non-zero code at startup because a required environment variable is not set.",
        "severity": "HIGH",
        "remediation_steps": [
          "kubectl -n devops-lab logs crashloop-app --previous --tail=50",
          "kubectl -n devops-lab describe pod crashloop-app",
          "Provide the missing configuration via a Secret/ConfigMap and redeploy."
        ],
        "prevention_tip": "Validate required environment variables at startup and add a startup probe."
      },
      "slack_delivered": true
    }
  ]
}
```

If Slack is enabled, a colour-coded incident card also appears in your Slack
channel. 🎉 You now have a working AI SRE agent.

> **No `ANTHROPIC_API_KEY` set?** The agent still responds — it falls back to a
> built-in rule-based diagnosis for common failures (CrashLoopBackOff,
> ImagePullBackOff), so you can test the full flow before adding a key.

#### Wiring up real Azure Monitor alerts (optional)

To make this fully hands-off in AKS:

1. Enable **Container Insights** on your AKS cluster (Azure Portal → your cluster
   → *Monitoring* → *Insights*).
2. Create an **alert rule** on a signal such as pod restart count or pod-ready
   status, scoped to the `devops-lab` namespace.
3. Attach an **Action Group** with a **Webhook** action (tick *"use common alert
   schema"*) pointing at your agent's public `/webhook` URL — expose it via an
   Ingress / Load Balancer. The agent parses the namespace and pod from the
   common alert schema automatically, so no Logic App is needed.

---

## 4. Production Guardrails & Security

K8s-Sentry is designed with **defence-in-depth** — several independent safety
layers — because it connects a cluster to an external LLM. Two of these
guardrails are built directly into the code.

### 4.1 Read-only Kubernetes access (enforced, not assumed)

The agent authenticates to the cluster with a Kubernetes **ServiceAccount** that
is bound to a **ClusterRole granting only `get`, `list`, and `watch`** on pods,
pod logs, events, and workload objects (see `manifests/01-rbac.yaml`). There is
**no** `create`, `update`, `patch`, `delete`, or `exec` permission anywhere, and
Kubernetes **Secrets are deliberately excluded** from the allowed resources.

Why this matters: even if the LLM were somehow manipulated into "suggesting" a
destructive command, the agent's credentials **physically cannot perform it**.
The remediation steps it returns are plain text for a human to review and run —
the agent itself only ever *reads*. This is reinforced by the `AGENT_MODE=read_only`
setting and by a system prompt that forbids the model from proposing deletion of
namespaces, volumes, or cluster-wide resources.

### 4.2 Secret & PII masking (before anything reaches the LLM)

Cluster logs and events routinely contain sensitive strings — database
passwords, API keys, tokens, connection strings, email addresses, IP addresses.
Before any evidence is sent to Claude, the agent runs it through
`app/masking.py`, which redacts these patterns and replaces them with typed
placeholders such as `***REDACTED_API_KEY***` or `***REDACTED***`.

The redaction keeps the *shape* of the data (so the model still understands "a
password was present here") without ever transmitting the secret itself. Patterns
covered include: `sk-...` / `AKIA...` style keys, JWTs, GitHub and Slack tokens,
private-key blocks, `scheme://user:pass@host` connection strings, `password=` /
`token=` key–value pairs, `Bearer` tokens, emails, and IPv4 addresses. The list
is easy to extend in `masking.py` for your organisation's own patterns.

### 4.3 Other layers (configured elsewhere)

- **Namespace scoping** — the agent only inspects `TARGET_NAMESPACE`
  (`devops-lab`); alerts pointing outside that scope are ignored.
- **Bounded data collection** — logs are capped at `LOG_TAIL_LINES` (default 50)
  per container, limiting both cost and data exposure.
- **Hardened container** — the Docker image runs as a **non-root** user with a
  read-only root filesystem and all Linux capabilities dropped.

> **For real production use**, also: restrict the AKS API server to known IP
> ranges, put authentication in front of the `/webhook` endpoint, store secrets
> in **Azure Key Vault** (via the Secrets Store CSI driver) instead of plain
> Kubernetes Secrets, and add a per-incident rate/cost limit on the LLM calls.

---

## 5. Cleanup / Teardown

Azure charges for the AKS cluster and its nodes while they run, so tear the lab
down when you're finished.

```bash
# Both — remove the test workloads
kubectl delete -f manifests/ --ignore-not-found
```

```bash
# Both — destroy all Azure infrastructure
cd terraform
terraform destroy      # type "yes" to confirm
cd ..
```

To leave the Python environment: run `deactivate`.

---

## 6. Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `az login` opens no browser | Headless/remote shell | Use `az login --use-device-code` and follow the printed URL. |
| `kubectl get nodes` → `Unable to connect` | kubeconfig not set | Re-run `az aks get-credentials ...` (step 3.4). |
| kubectl prompts for a login every time | AKS uses Entra ID (`kubelogin`) | Run `az aks install-cli` once, then retry. |
| PowerShell: "running scripts is disabled" | Execution policy | `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`, then activate the venv again. |
| `uvicorn: command not found` | venv not active | Activate it (step 3.6); confirm you see `(.venv)`. |
| `curl` not recognised on Windows | Use PowerShell native cmd | Use the `Invoke-RestMethod` block instead of `curl`. |
| Agent returns rule-based text, not LLM analysis | `ANTHROPIC_API_KEY` missing/invalid | Check the key in `.env`; restart the server. |
| No Slack message | `SLACK_ENABLED=false` or bad webhook | Verify `SLACK_WEBHOOK_URL`; check the server logs. |
| `crashloop-app` logs look empty | Logs are in the *previous* crashed container | The agent handles this automatically; manually: `kubectl -n devops-lab logs crashloop-app --previous`. |
| `terraform apply` auth error | Not logged in / wrong subscription | Re-run `az login` and `az account set` (step 3.2). |

---

### You're done

You've provisioned AKS with Terraform, connected `kubectl`, deployed intentional
faults, run the FastAPI agent, and watched it diagnose real Kubernetes failures
with an LLM — all behind read-only, secret-masked guardrails. From here, wire up
Azure Monitor for hands-off alerting, or deploy the agent into the cluster itself
using the manifests in `manifests/`.

*K8s-Sentry — Project 3 · Cloud/DevOps portfolio.*
