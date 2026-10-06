# Smart AWX + AI Agent

A single installer that turns a plain Ubuntu machine into:

```
Ubuntu  ->  K3s  ->  AWX  ->  AWX Dashboard  ->  AI Agent  ->  Local AI model
                                                      |
                                                      +-> Linux / Kubernetes / AWX tools
```

Everything runs on the machine itself. No OpenAI key, no Anthropic key, no
subscription, no telemetry. After the initial download the stack works without
internet access.

```bash
sudo python3 install.py
```

---

## Table of contents

1. [What you actually get](#1-what-you-actually-get)
2. [Requirements and supported hardware](#2-requirements-and-supported-hardware)
3. [Installation](#3-installation)
4. [Architecture](#4-architecture)
5. [How the AI Agent works](#5-how-the-ai-agent-works)
6. [Accessing AWX and the AI Agent](#6-accessing-awx-and-the-ai-agent)
7. [Permissions and approvals](#7-permissions-and-approvals)
8. [Commands](#8-commands)
9. [8 GB machine: what to expect](#9-8-gb-machine-what-to-expect)
10. [Security](#10-security)
11. [Logs and audit trail](#11-logs-and-audit-trail)
12. [Troubleshooting](#12-troubleshooting)
13. [Recovery and self-healing](#13-recovery-and-self-healing)
14. [Uninstall](#14-uninstall)
15. [Testing](#15-testing)
16. [Design decisions and honest limitations](#16-design-decisions-and-honest-limitations)

---

## 1. What you actually get

| Component | What it is | Where it runs |
|---|---|---|
| K3s | Single-node Kubernetes | systemd service `k3s` |
| AWX Operator | Pinned release (default 2.19.1) | namespace `awx` |
| AWX | Web + task + PostgreSQL, sized for your hardware | namespace `awx` |
| AI runtime | Ollama + one quantised local model | systemd service `ollama` |
| AI Agent backend | FastAPI service, 53 declared tools | systemd service `smart-awx-ai-agent` |
| AI Agent UI | Chat UI with live progress and approvals | served by the backend |
| AI proxy | nginx: one entry point for AWX + the agent | namespace `awx`, NodePort 30080 |

The agent is not a chatbot wrapper. Every factual answer it gives is produced
by running a real command or API call on this machine, and the UI shows you
exactly which command produced which number.

---

## 2. Requirements and supported hardware

### Minimum (installer refuses below this)

| Resource | Hard minimum | Recommended |
|---|---|---|
| RAM | 3.5 GB | 8 GB |
| Free disk | 20 GB | 50 GB |
| CPU cores | 2 | 4 |
| OS | Ubuntu 20.04+ / Debian 11+ | Ubuntu 22.04 LTS |
| Architecture | x86_64 or aarch64 | x86_64 |
| Other | systemd, outbound HTTPS for the initial download | |

### Hardware profiles

The installer detects RAM and picks a profile; everything else (AWX memory
limits, PostgreSQL volume size, K3s flags, AI model) follows from it.

| Profile | RAM | AI model (first choice) | Notes |
|---|---|---|---|
| `LOW` | <= 4 GB | `qwen2.5:0.5b-instruct-q4_K_M` | Very tight. AWX will be slow. |
| `TEST` | 4-8 GB | `qwen2.5:3b-instruct-q4_K_M` | **The reference 8 GB machine.** |
| `NORMAL` | 8-16 GB | `qwen2.5:7b-instruct-q4_K_M` | Comfortable. |
| `HIGH` | > 16 GB | `qwen2.5:14b-instruct-q4_K_M` | Larger model possible. |

All of this lives in [`config/compatibility.yaml`](config/compatibility.yaml) -
change the matrix there, not the Python.

### What is detected

OS, OS version, codename, kernel, architecture, CPU model, logical/physical
cores, load average, total/available RAM, swap, total/free disk, network
interfaces, default gateway, DNS servers, firewall state, GPU and VRAM,
virtualization, cgroup version and whether systemd is present.

```bash
sudo python3 install.py --dry-run      # see the detection and the plan, change nothing
```

---

## 3. Installation

### One command on a fresh machine

```bash
curl -fsSL https://raw.githubusercontent.com/BHASHKARKUSHWAHA/Test-awx/claude/ubuntu-awx-ai-agent-platform-oxyj69/smart-awx-ai/bootstrap.sh -o bootstrap.sh
sudo bash bootstrap.sh
```

`bootstrap.sh` installs only what the installer cannot install for itself
(`git`, `python3`, `curl`), clones the repository to `/opt/smart-awx-ai-src`
and hands over to `install.py`. Every flag is forwarded:

```bash
sudo bash bootstrap.sh --dry-run
sudo bash bootstrap.sh --profile TEST --keep-ufw
```

Re-running it updates the checkout instead of re-cloning.

### Or clone it yourself

```bash
git clone -b claude/ubuntu-awx-ai-agent-platform-oxyj69 https://github.com/BHASHKARKUSHWAHA/Test-awx.git
cd Test-awx/smart-awx-ai
sudo python3 install.py
```

One command runs eight phases. Each one installs, **verifies**, and
**health-checks** before the next begins:

| Phase | Name | What it does |
|---|---|---|
| 1 | `preflight` | Detect hardware, enforce compatibility, pick the profile |
| 2 | `dependencies` | apt packages (found -> verify -> reuse, missing -> install -> verify) |
| 3 | `k3s` | K3s server, wait for node `Ready`, CoreDNS, storage class |
| 4 | `awx_operator` | `kubectl apply -k` the pinned operator, wait for the CRD and deployment |
| 5 | `awx` | AWX custom resource sized for the profile, wait for web+task, verify `/api/v2/ping/` |
| 6 | `ai_runtime` | Ollama, then pull the first model that downloads **and answers a test prompt** |
| 7 | `ai_agent` | venv, systemd unit, credentials, wait for `/health` |
| 8 | `ai_ui` | nginx proxy, AWX dashboard integration, firewall ports |
| 9 | `health` | Full stack report |

Expect **20-45 minutes** on an 8 GB machine: most of it is AWX pulling images
and running database migrations, plus the model download.

### Failure handling

```
ERROR -> COLLECT LOGS -> DIAGNOSE -> SAFE RECOVERY -> RETRY (max 3) -> VERIFY
```

A failed phase writes a diagnostics bundle to
`/var/log/smart-awx-ai/diagnostics-<phase>-<timestamp>.log`, matches the error
against known signatures (out of disk, OOM, image pull, DNS, crash loop, ...),
prints the cause and the next step, runs a **safe** recovery (restart a unit,
delete a crashed pod - never a wipe), and retries. Three attempts, then it
stops with a real error. There are no infinite loops.

### Resume

Completed phases are recorded in `/var/lib/smart-awx-ai/state.json`. Re-running
`sudo python3 install.py` after a failure continues from the phase that failed:

```
Already complete: preflight, dependencies, k3s
PHASE: AWX_OPERATOR - AWX Operator      <- resumes here
```

---

## 4. Architecture

```
                         Ubuntu host
  +--------------------------------------------------------------+
  |                                                              |
  |  systemd: k3s          systemd: ollama      systemd:         |
  |      |                      |               smart-awx-ai-    |
  |      |                      |               agent (:8900)    |
  |      v                      |                   |            |
  |  +-----------------------+  |                   |            |
  |  | K3s - namespace awx   |  |                   |            |
  |  |                       |  |                   |            |
  |  |  awx-operator         |  |                   |            |
  |  |  awx-web / awx-task   |<-+-------------------+            |
  |  |  awx-postgres         |  |   AWX API    tools: linux,     |
  |  |                       |  |              k8s, awx,         |
  |  |  awx-ai-proxy (nginx) |  |              monitoring, net   |
  |  |    NodePort 30080 ----+--+---------------------------------> users
  |  +-----------------------+                                   |
  +--------------------------------------------------------------+

  http://SERVER_IP:30080            AWX dashboard + the AI Agent entry point
  http://SERVER_IP:30080/ai-agent/  AI Agent UI directly
  http://SERVER_IP:30081            AWX without the proxy (fallback)
```

**Why the agent runs on the host and not in a pod:** Linux tools must observe
the real machine - its PIDs, systemd units, journal and memory. A container
sees its own namespaces. Putting the agent in a pod would mean either lying
about the host or giving a pod host PID, host network and host mounts, which is
strictly worse for security than one well-scoped systemd service.

**Why nginx sits in front of AWX:** see
[section 16](#16-design-decisions-and-honest-limitations).

### Repository layout

```
smart-awx-ai/
├── bootstrap.sh            fresh-machine entry point: deps, clone, hand over
├── install.py              single entry point, phase orchestration, resume
├── uninstall.py            staged removal
├── requirements.txt        fastapi, uvicorn, pydantic, pyyaml - nothing else
├── installer/
│   ├── common.py           logging, redaction, command execution, state, retry
│   ├── minyaml.py          YAML subset parser (config is read before apt runs)
│   ├── hardware.py         detection and profile selection
│   ├── compatibility.py    hard/soft requirement gate
│   ├── dependencies.py     apt phase
│   ├── kubernetes.py       K3s install and kubectl plumbing
│   ├── awx.py              operator + AWX CR + verification
│   ├── ai.py               Ollama, model selection, agent service, proxy
│   ├── recovery.py         diagnose, collect logs, safe recovery
│   └── health.py           final report
├── agent/
│   ├── api.py              FastAPI app: /health, /status, /api/ai/*
│   ├── agent.py            plan -> permission -> execute -> verify -> answer
│   ├── planner.py          rule router + LLM tool selection + fallback
│   ├── permissions.py      levels, escalation, approval tokens
│   ├── auth.py             AWX session reuse, AWX login, break-glass account
│   ├── llm.py              local Ollama client (the only model endpoint)
│   ├── memory.py           conversation history, redacted, in memory only
│   ├── audit.py            JSONL audit log + secret redaction
│   ├── factory.py          create restricted specialised agents
│   ├── render.py           deterministic formatting (used when the LLM is down)
│   └── models.py           request/response schemas
├── tools/
│   ├── registry.py         tool definition, schema validation, command guard
│   ├── linux.py            19 host tools
│   ├── kubernetes.py       12 cluster tools
│   ├── awx.py              AWX API client + 10 tools
│   ├── monitoring.py       5 cross-layer tools
│   └── networking.py       7 network tools
├── ui/ai-agent/            chat UI (index.html, app.js, style.css) + inject.js
├── manifests/              awx.yaml, ai-agent.yaml, secrets.yaml, proxy-nginx.conf
├── config/compatibility.yaml
├── policies/permissions.yaml
└── tests/                  126 tests
```

---

## 5. How the AI Agent works

### The one rule

> **System data = tool output. Never a model guess.**

Ask "how much RAM does this server have?" and the agent does **not** answer from
model knowledge. It runs `get_memory_info`, which reads `/proc/meminfo` and
`free -h`, and only then phrases the answer. The model is given the tool output
and an instruction that it may not add a single number of its own. If a tool
fails, the answer says the tool failed - it does not fill the gap.

### Request pipeline

```
User request
   |
   v
Planner            rules first (English + Hinglish), then the local model,
   |               then an honest fallback; invented tool names are discarded
   v
Permission check   stricter of: tool level, policy override, target context
   |
   +-- read_only   -> run
   +-- safe_write  -> run if policy allows, otherwise ask
   +-- medium_risk -> always ask
   +-- high_risk   -> ask and require the word CONFIRM typed
   v
Execute            argv only, allow-listed binaries, per-tool timeout
   |
   v
Verify             re-read the state that was changed
   |
   v
Answer             model phrases the tool output; evidence attached
   |
   v
Audit              one redacted JSON line per action
```

### Live progress

The UI streams operational status while the agent works:

```
Plan: Produce a full stack health report from live data
  ✓ Collect system, Kubernetes, AWX and AI runtime health   [read only]
Summarising the results...
```

Only operational status is streamed. The model's internal reasoning is never
emitted to the browser.

### Specialised agents

A main orchestrator routes each step to a specialised worker:

| Agent | Tools |
|---|---|
| Linux Agent | OS, CPU, RAM, disk, processes, services, ports, logs, packages |
| Kubernetes Agent | nodes, pods, services, deployments, events, logs, restarts |
| AWX Agent | projects, inventories, templates, jobs, launch, relaunch |
| Network Agent | ping, DNS, port checks, routes, firewall |
| Monitoring Agent | resource summary, top consumers, pod restarts, health report |
| Security Agent | firewall, listening ports, auth log review |

These are **tool-scoped workers over one model**, not six model processes. On an
8 GB machine running six LLMs would be the fastest way to break the box.

You can create your own from the UI ("+ Create agent"): name, purpose, tool
categories and a maximum permission level, capped by
`policies/permissions.yaml -> agent_factory`.

### Example session

```
You: Server ka complete health report do

AI Agent:
CPU             : 22.4%
RAM             : 61.0% (4998 / 8192 MB)
Disk            : 43.1% (28.4 GB free)
K3s             : active
Kubernetes      : healthy
AWX             : healthy
Failed pods     : 0
Failed AWX jobs : 1

Overall: DEGRADED
Problems found:
  - 1 failed AWX job(s)

You: Fix the first one.

AI Agent:
I need your approval before I continue.
Action: Re-run failed AWX job 42
Tool: relaunch_job
Risk: SAFE WRITE
```

---

## 6. Accessing AWX and the AI Agent

| What | URL |
|---|---|
| AWX + AI Agent (use this) | `http://SERVER_IP:30080` |
| AI Agent UI directly | `http://SERVER_IP:30080/ai-agent/` |
| AWX without the proxy | `http://SERVER_IP:30081` |
| Agent health | `http://SERVER_IP:30080/ai-health` |

AWX admin password:

```bash
kubectl -n awx get secret awx-admin-password -o jsonpath='{.data.password}' | base64 -d; echo
```

Inside the AWX dashboard the agent appears three ways:

1. an **AI Agent** item appended to the left navigation;
2. a floating launcher button in the bottom right (always present);
3. the keyboard shortcut **Alt+A**.

All three open a slide-over panel hosting the agent UI, so AWX keeps its own
router and state.

### Signing in to the agent

* **Already signed into AWX** - the agent reuses your AWX session cookie and
  verifies it against `/api/v2/me/`. Nothing to type.
* **Opened directly** - sign in with your AWX username and password; they are
  verified against the AWX API and exchanged for a signed, HttpOnly cookie. The
  password itself is never stored.
* **AWX is down** - use the local break-glass account in
  `/etc/smart-awx-ai/credentials.txt` (root only). This exists precisely so you
  can ask the agent to diagnose AWX when AWX cannot authenticate you.

---

## 7. Permissions and approvals

Levels, from `policies/permissions.yaml`:

| Level | Examples | Behaviour |
|---|---|---|
| `read_only` | CPU, RAM, disk, processes, services, logs, pods, AWX status | runs immediately |
| `safe_write` | restart an allow-listed service, restart a pod, re-run a job | runs if `auto_safe_write: true` |
| `medium_risk` | install a package, scale a deployment, change limits, launch a template | always asks |
| `high_risk` | delete a namespace, delete a file, disable the firewall | asks **and** requires typing `CONFIRM` |

Three things make this real rather than decorative:

1. **The stricter level wins.** The level is the maximum of the tool's declared
   level, the policy override, and the target context. `restart_service` is
   `safe_write`, but `restart_service k3s` is `high_risk` - and `k3s` is in
   `critical_services`, so the tool refuses it outright.
2. **The model never supplies the level.** It only picks a tool name; the level
   is looked up server-side.
3. **Approvals are server-side tokens** bound to one exact (tool, arguments)
   pair, single use, with a 5-minute TTL. A high-risk token also requires the
   exact confirmation word.

The agent cannot approve its own actions. There is no code path from a model
response to an approval token.

Edit the policy and the agent picks it up on the next request (the file's mtime
is watched):

```bash
sudo nano /opt/smart-awx-ai/policies/permissions.yaml
```

Want a strictly read-only agent? Set `auto_safe_write: false`.

---

## 8. Commands

```bash
sudo python3 install.py                        # install or resume
sudo python3 install.py --dry-run              # show the plan, change nothing
sudo python3 install.py --status               # health report for what is installed
sudo python3 install.py --repair               # re-run only the unhealthy phases
sudo python3 install.py --keep-ufw             # never touch the firewall
sudo python3 install.py --operator-version 2.19.1
sudo python3 install.py --model qwen2.5:1.5b-instruct-q4_K_M
sudo python3 install.py --profile TEST         # override the detected profile
sudo python3 install.py --phase ai_agent       # re-run one phase
sudo python3 install.py --skip-model           # runtime only, no model download
sudo python3 install.py --proxy-nodeport 30080 --awx-nodeport 30081
sudo python3 install.py --force                # ignore a hard requirement (at your risk)
sudo python3 install.py --reset-state          # forget phase state
sudo python3 install.py --verbose              # print every command

sudo python3 uninstall.py                      # remove the agent, keep AWX and K3s
sudo python3 uninstall.py --all                # remove everything
```

Service management:

```bash
sudo systemctl status smart-awx-ai-agent
sudo systemctl restart smart-awx-ai-agent
sudo journalctl -u smart-awx-ai-agent -f
kubectl -n awx get pods
```

---

## 9. 8 GB machine: what to expect

This is the reference target, and the installer is tuned for it. It works, with
real limits that are stated rather than hidden.

**What the installer does on 8 GB (`TEST` profile):**

* K3s with Traefik, servicelb and metrics-server disabled (local-path storage
  stays - AWX needs a volume for PostgreSQL)
* AWX web and task capped at 1.5 GB each, PostgreSQL at 768 MB, 1 replica
* a 3B parameter 4-bit quantised model (~2 GB), one model loaded at a time,
  4096 token context, unloaded after 5 minutes idle
* the agent backend capped at 512 MB by systemd
* no monitoring stack, no extra databases, no second LLM process

**Rough steady-state budget:**

| Component | RAM |
|---|---|
| K3s + system | ~1.0 GB |
| AWX (web + task + postgres) | ~2.5-3.5 GB |
| AI model while answering | ~2.5 GB |
| AI backend + proxy | ~0.3 GB |
| **Peak** | **~7 GB of 8 GB** |

**Honest limits on 8 GB:**

* The first AWX boot (image pulls + migrations) is the tightest moment. Adding
  2 GB of swap makes it noticeably more reliable.
* Model answers take roughly 5-25 seconds on CPU. Tool output appears
  immediately; only the prose is slow.
* Running several AWX jobs while the model is answering can push the box into
  swap. Prefer one at a time.
* With no swap and a heavy AWX job, the kernel may OOM-kill a pod. The installer
  warns about missing swap during preflight for exactly this reason.
* If the stack cannot run reliably, the installer says so in the final report
  rather than printing a green banner.

To trade quality for headroom:

```bash
sudo python3 install.py --model qwen2.5:1.5b-instruct-q4_K_M --phase ai_runtime
sudo systemctl restart smart-awx-ai-agent
```

---

## 10. Security

* **No arbitrary shell.** The model cannot run commands. It can only name a
  declared tool. Tools execute `argv` lists - never a shell string - and only
  binaries on an allow-list in `tools/registry.py`.
* **Every argument is schema-validated** before a tool runs: type, enum,
  numeric range, length and regex. `{"name": "nginx; rm -rf /"}` is rejected by
  the schema, and would be inert anyway because there is no shell.
* **Nothing is exposed unauthenticated.** Every `/api/ai/*` endpoint requires an
  AWX-backed session. Session cookies are HttpOnly and HMAC-signed.
* **No hardcoded credentials.** AWX credentials are generated/read at install
  time and stored in a Kubernetes secret plus `/etc/smart-awx-ai/agent.env`
  (mode 0600, root only). The repository contains no secret.
* **Secrets are redacted everywhere**: installer logs, agent logs, the audit
  log, conversation memory and chat responses. Passwords, tokens, API keys,
  bearer headers, JWTs and private key blobs are masked as `********`.
* **Conversation memory is in-process only**, capped, redacted on write, and
  never persisted to disk.
* **Protected resources** cannot be touched: `kube-system`, `kube-public`,
  `kube-node-lease`, `default` and `awx` namespaces cannot be deleted; `k3s`,
  `ssh`, `systemd-journald` and `dbus` cannot be restarted or stopped;
  `delete_file` refuses `/etc`, `/usr`, `/boot`, `/home`, `/root/.ssh` and other
  system paths.
* **Local-first by default.** The only model endpoint in the code is
  `OLLAMA_URL` on localhost. No server data leaves the machine.

The agent service runs as root because restarting services and installing
packages requires it. That privilege is constrained by the policy file and the
tool allow-list, which is the trade-off this design makes explicitly.

---

## 11. Logs and audit trail

| File | Contents |
|---|---|
| `/var/log/smart-awx-ai/install.log` | every installer step and command |
| `/var/log/smart-awx-ai/agent.log` | agent service stdout/stderr |
| `/var/log/smart-awx-ai/audit.log` | one JSON line per action (below) |
| `/var/log/smart-awx-ai/diagnostics-*.log` | bundle written on each phase failure |
| `/var/lib/smart-awx-ai/state.json` | phase state and installation facts |

Audit entries record the request, the plan, every tool execution with its
commands, approvals requested and granted, and the answer:

```json
{"timestamp":"2026-10-06T12:30:11+00:00","event":"tool_execution","user":"admin",
 "tool":"restart_service","args":{"name":"nginx"},"level":"safe_write",
 "approved":false,"success":true,"duration_ms":2143,
 "commands":["systemctl restart nginx.service","systemctl show nginx.service ..."]}
```

Read it from the UI (superusers) or:

```bash
sudo tail -f /var/log/smart-awx-ai/audit.log | jq .
```

---

## 12. Troubleshooting

**Start here:**

```bash
sudo python3 install.py --status
```

| Symptom | Cause | Fix |
|---|---|---|
| `systemd: FAIL` in preflight | container/WSL without systemd | install on a real VM or host |
| K3s node never `Ready` | low memory or cgroup issues | `journalctl -u k3s -n 50`; free RAM; `--profile LOW` |
| AWX pods stuck `Pending` | no storage class or not enough RAM | `kubectl -n awx describe pod <pod>` |
| AWX pods `CrashLoopBackOff` | OOM during migration | add 2 GB swap, then `sudo python3 install.py --repair` |
| `ImagePullBackOff` | no registry access | check DNS/proxy; re-run (the installer retries) |
| Model pull fails | no disk or bad tag | the installer falls back automatically; `--model <other>` forces one |
| Agent `/health` unreachable | backend failed to start | `journalctl -u smart-awx-ai-agent -n 50` |
| Agent answers but says "model not reachable" | Ollama down | `sudo systemctl restart ollama` |
| No AI Agent item in the AWX nav | AWX changed its nav markup | the floating launcher and Alt+A still work; `/ai-agent/` always works |
| `401` from the agent API | not signed in | sign in with AWX credentials, or the break-glass account |
| Port 30080 unreachable from outside | firewall | `sudo ufw allow 30080/tcp` (or re-run without `--keep-ufw`) |

Useful commands:

```bash
kubectl -n awx get pods -o wide
kubectl -n awx get events --sort-by=.lastTimestamp | tail -20
kubectl -n awx logs deployment/awx-ai-proxy --tail 50
curl -s http://127.0.0.1:8900/health | jq .
ollama list
```

---

## 13. Recovery and self-healing

**Installer level:**

```bash
sudo python3 install.py --repair
```

Checks every component, works out which phases own the broken ones, and re-runs
only those.

**Service level:** both systemd units use `Restart=always`. The agent restarts
within 5 seconds of a crash, bounded by `StartLimitBurst=5` in 5 minutes so a
genuinely broken build does not restart forever.

**Agent level:** ask it. "AWX down hai, fix karo" produces a read-only
diagnosis - AWX API, pods, warning events, deployments, host memory - then
proposes a specific repair with its risk level and waits for your approval:

```
Proposed repair: restart pod awx-web-6d9f in namespace awx.
The evidence points at memory pressure, so a restart may only buy time -
consider lowering the AI model size or raising the pod memory limit.
Approve to proceed.
```

The repair is derived from what the diagnosis actually found, not from the
model's opinion, and it is executed only after you approve - then verified by
re-reading the state.

---

## 14. Uninstall

```bash
sudo python3 uninstall.py              # agent + proxy; AWX, K3s and models stay
sudo python3 uninstall.py --all        # everything, including K3s and the models
sudo python3 uninstall.py --all --keep-logs
sudo python3 uninstall.py --dry-run    # show what would be removed
```

---

## 15. Testing

```bash
python3 -m pip install pytest
python3 -m pytest tests/ -v
```

129 tests covering: the YAML fallback parser against PyYAML, profile selection
boundaries, compatibility gating, state/resume, failure diagnosis, the tool
registry contract, command-guard escapes, schema validation, real tool output
against `/proc`, permission escalation, approval tokens and TTL, secret
redaction, planner routing for every supported phrasing (English and Hinglish),
agent grounding, approval enforcement, post-action verification, and the HTTP
API including SSE streaming and the agent factory.

The spec's own acceptance list is covered in
`tests/test_planner.py` and `tests/test_agent.py` - RAM, CPU, pods, AWX status,
failed jobs, health report, pod diagnosis, service restart, namespace deletion
confirmation, and AI service restart.

---

## 16. Design decisions and honest limitations

**AWX has no supported plugin API for adding navigation items.** The AWX UI is a
compiled React single-page application. There is no documented extension point
that lets a third party add a menu entry, and no upgrade-safe way to patch the
bundle. Three options existed:

1. *Rebuild the AWX UI image with a custom menu entry.* Works, but has to be
   redone and re-tested on every AWX release, and a mistake breaks AWX itself.
2. *Pretend and ship a separate page with a fake "integrated" screenshot.* Not
   acceptable.
3. *Inject one script tag at the edge.* The nginx proxy adds
   `<script src="/ai-agent-inject.js" defer>` before `</body>` in the AWX HTML.
   The AWX container is byte-for-byte untouched, AWX upgrades cannot break it,
   and the script adds a nav item plus a floating launcher.

This project takes option 3, and the installer verifies the injection actually
happened rather than assuming it. If a future AWX release changes its nav
markup, the nav item disappears but the floating launcher, Alt+A and
`/ai-agent/` keep working - and `--status` reports it as `DEGRADED` instead of
claiming success.

**Other deliberate choices:**

* **The agent runs on the host, not in a pod** - a container cannot truthfully
  answer "how much RAM does this server have".
* **Rules before the model in the planner** - a 3B quantised model is not a
  reliable function caller. Rules handle the common questions deterministically
  (in English and Hinglish); the model handles the rest and its output is
  validated against the registry.
* **The agent degrades instead of guessing** - with the model down it still
  answers from tool output and says so.
* **Specialised agents are tool scopes, not processes** - six models on 8 GB
  would not fit.
* **Four Python dependencies** (`fastapi`, `uvicorn`, `pydantic`, `PyYAML`).
  Everything else is the standard library, so the stack keeps working offline
  and on a constrained machine.

**Known limitations:**

* Single node. No HA, no multi-node scheduling.
* AWX job *execution* still happens through AWX and Ansible; the agent
  orchestrates it, it does not replace it.
* The agent's write capabilities are intentionally narrow. Anything outside the
  declared tools has to be added to `tools/` deliberately - it cannot be reached
  by prompting.
* Model quality on an 8 GB machine is what a 3B quantised model can do: good at
  summarising real tool output, not a substitute for an engineer.
