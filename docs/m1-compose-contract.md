# A0: M1 installer and Compose contract

Approved scope: implement the smallest package that satisfies [Fresh Install Produces Papers](productization-plan.md#m1-fresh-install-produces-papers). This architecture contract specifies forthcoming implementation; it does not claim a published image or a tested installation. Ranking evaluation runs independently. Do not alter the Mini's systemd unit, cron, deployed checkout or state.

## Distribution and host qualification

Ship one versioned application image reference with a multi-platform manifest containing `linux/amd64` and `linux/arm64`. The release Compose file resolves that single reference; do not create separate Mac and Linux product images or force amd64 emulation on Apple Silicon. A release manifest pins the app manifest digest and a tested multi-platform Ollama image digest. Registry/name and exact runtime versions must be assigned before publishing the package; never advertise a placeholder as pullable.

M1 targets Linux x86-64 with Docker Engine/Compose and macOS Apple Silicon with Docker Desktop/Compose running Linux arm64 containers. macOS is a host target, not a macOS container image. CPU inference is the baseline on both; do not assume Apple GPU access inside Docker or require a GPU override. QA must record supported host/runtime versions and measured RAM, free disk and completion time on each target before claiming support. Reject Windows, Intel macOS, Linux arm64 and unqualified configurations with a clear explanation for this initial package. Do not silently install Docker, change host privileges or change Docker contexts.

## Services and network

| Service | Responsibility |
| --- | --- |
| `app` | One application image runs the existing web server and bounded manual pipeline worker. Include Python/SQLite, `sql/`, assets, scripts, clean initialization templates and `pdftotext`. No personal `data/`, secrets, model weights or learned profile enter the image. |
| `ollama` | Own inference, its API and model files. Use a private Compose network; do not publish its port to the host. |
| `prepare-model` | One-shot helper uses the pinned Ollama image, waits for Ollama API readiness and checks for `qwen2.5:1.5b-instruct`. Pull only if absent; report failure and permit an explicit retry. No parallel pulls or permanent helper service. |

Publish `127.0.0.1:8000` to app port 8000; bind the web process to `0.0.0.0` inside its container so publishing works. Container-wide binding does not authorize host-wide publishing. An occupied host port is a reported installation failure; an explicit infrastructure port override is allowed and preserved. Do not terminate the process occupying it.

The app uses `http://ollama:11434/api/generate` for extraction, Curator evidence and existing topic-runtime calls. Configure all call paths consistently; mounting `.env` alone is insufficient. Ollama may require network access for its first model pull, and source discovery requires outbound access. Do not mark the Compose network internal in a way that prevents those requests. No cloud credentials, host Ollama or host Python are required.

## Persistent state and initialization

Use a fixed Compose project identity per installation, recorded by the installer, with these named volumes:

| Volume | Container mount | Contents |
| --- | --- | --- |
| `paper-data` | `/app/data` in app | SQLite, profile seed/current compatibility file, paper artifacts, extraction and review files. Existing relative artifact paths remain valid. |
| `paper-config` | `/app/config` in app | Mutable `topics.yaml` and minimal customer research/source configuration. |
| `ollama-data` | `/root/.ollama` in Ollama | Persistent default model cache; the helper talks to Ollama over its API rather than writing model files itself. |

Keep read-only initialization templates outside mounted directories. Initialize only missing state, never overwrite customer content on restart or reinstall. A fresh profile has empty interests/signals/history; do not copy the creator profile or bootstrap known papers. Initial topics are empty until the user configures them, and arXiv is the only enabled source. Preserve valid existing files even if empty; malformed or partially initialized state needs a visible error, not destructive reseeding. Record initialized application/schema version. Validate writable volume ownership using the actual app runtime user before accepting startup; application execution should not require root.

Distribution uses an allowlist of source/templates plus a build-context exclusion for private data and credentials. Named volumes survive stop/start, container recreation, image rebuild and normal installer reruns. Normal commands must never use volume deletion. A new project name creates different volumes, so reruns must use the recorded identity even from a different shell directory. Do not automatically import or attach the Mini's host data. Import/migration is separate explicit work.

## Readiness, first use and failures

Start the app independently enough to display setup, saved papers and dependency status even when Ollama or model preparation fails. Distinguish app/DB readiness, Ollama API readiness, model preparation and discovery status. Compose process startup alone is not model readiness. Before enabling Run Scout, verify model presence and a bounded inference readiness check; record errors instead of accepting a tag listing as proof inference works. A successful readiness check does not prove ranking quality.

Model preparation exposes waiting/pulling/ready/failed states and actionable logs. Its overall timeout and retry count are explicit, finite release settings; QA qualifies them on both targets, including a first download. Installer timeout leaves state and useful diagnostics intact. Retry resumes/reuses Ollama storage; normal restarts with the cached model must not download weights again. Container restart policies must not create an endless failed-pull loop.

Manual Run Scout invokes the existing full `pipeline-daily` orchestration, not source-only discovery. One app-owned worker at a time uses shared exclusion with any supported CLI invocation. Show queued/running/completed/empty/failed status and preserve committed results. On restart, unfinished work becomes visibly interrupted and retryable; do not silently relaunch it. First use requires no host scheduler. Topic UX is PM-owned and reuses existing validation/persistence; this contract does not authorize Topic Agent redesign.

Gemini is explicitly off by default: saving feedback must not spawn Gemini or report missing Gemini as an application failure. Preserve raw feedback and existing deterministic Scout guidance; Curator continues consuming the active profile. OpenAlex and Semantic Scholar remain opt-in; absence of those services/credentials must not prevent default arXiv use. No scoring, retrieval or local profile synthesis changes are authorized.

## Installer behavior

The single installer detects host OS/architecture and the Docker daemon's container platform, verifies the expected supported pairing, active daemon, Compose capability, resources, selected release images, loopback port and writable installation directory. It emits specific prerequisite failures and instructions. Run it as the customer, without unnecessary sudo.

Create a clean `.env` from `.env.example` only when missing; preserve customer settings and restrict access. Record installation location, Compose identity, selected image/release and platform. Validate configuration before starting services. Detect an existing installation and report its version: an ordinary rerun repairs/restarts the same selected release, not an implicit upgrade to latest. Never source arbitrary customer `.env` as executable shell code or print secret values.

Start Compose, wait for bounded app/model readiness and print the actual loopback URL plus diagnostic/retry commands. Return nonzero with the failing stage when prerequisites, preparation or readiness fail. Keep volumes and existing configuration intact. Service logs distinguish errors from ordinary empty discovery. Re-running after an interrupted first install is safe. Do not enable automatic scouting, alter host cron/systemd, or delete resources belonging to another project.

## Acceptance and remaining assignments

Developer implements this contract; QA validates both host targets using empty app/config/model volumes and no credentials. Required proof: install, configure topics, one real arXiv recommendation for a documented productive topic, save exact feedback/score, restart and recreate containers, confirm data/topics/profile survive and model weights are reused. Also test interrupted pull, unavailable Ollama, port conflict, repeated installer/run requests and all-source failure. Preserve existing regressions; ranking calibration completion is not a dependency.

Actual pre-publication dependencies are the image registry/reference and build/publish ownership, tested Docker/Compose/Ollama version matrix, measured resource thresholds, and QA access to both target hosts. These are release inputs, not reasons to delay implementation of the package. PM supplies the minimal topic interaction decision. Architect/Developer must add an explicit schema compatibility check before public upgrades; no reset or volume removal is an upgrade strategy. See the [V1 backlog](productization-plan.md#phased-dependency-aware-backlog) for backup/migration and optional-integration work beyond M1.
