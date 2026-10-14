# PCAI 3D Visualization — Project Overview

**AI-Assisted 3D Texture & Asset Generation for ADM's 3D Visualization Team**

| | |
|---|---|
| **Project** | PCAI 3D Visualization PoC (→ production roadmap) |
| **Customer** | Astra Daihatsu Motor (ADM) — 3D Visualization / IDD |
| **Delivered by** | HPE — Private Cloud AI (Solution Architecture, presales + delivery) |
| **Platform** | HPE Ezmeral / PCAI — CS1 cluster (Kubernetes + MLIS + Istio) |
| **Codebase** | `pcai-3d-viz-demo` @ **v14.x** (v14.11, latest) |
| **Reference** | Arya's slide — *AI Use Case in IDD – 3D Visualization* (2026-10-01) |
| **Status** | **PoC built, deployed to CS1, and verified end-to-end** · production roadmap defined |
| **Date** | 2026-10 |

---

## 1. Executive Summary

This project turns a **flat reference photo** into a **production-ready 3D textured asset** — and hands it to the artist's **own Blender** — using the AI already running on ADM's PCAI platform. It was built as a **minimum-complexity proof of concept** (zero new models, zero new GPU spend, one pod, no database, no auth) to de-risk the idea, and it now includes a defined **roadmap to production grade**.

In one sentence: *an artist drops in a photo, clicks Generate, and in under a minute gets a 3D object with real PBR materials in the browser and a ready-to-open `.blend` file for their workstation — powered by HPE's on-prem AI.*

**What is delivered today (verified on CS1):**
- **Reference → AI texture:** a reference image (upload, URL, or bundled sample) becomes a clean **albedo** via the existing on-prem **Qwen-Image-Edit** model, plus a full deterministic **PBR set** (albedo, height, normal, roughness, metallic) with automatic **quality gates**.
- **Reference → real 3D:** an opt-in *"Build 3D from the image"* step produces a **true textured mesh (GLB)** via **Hunyuan3D-2.1** on the existing A100, with an automatic **CPU 2.5D-relief fallback** when the GPU service is busy — so the flow never dead-ends.
- **Live preview:** a Three.js web preview shows the texture and the 3D object in the browser (works on ADM's internal network — no CDN).
- **Shared asset library:** a persistent, file-based library on an NFS PVC (browse / rename / tag / download) — the seed of ADM's stated future goal of *"assets accessible to every user."*
- **Real Blender integration:** a downloadable **Blender-pack** (Tier 1, deterministic, always works) **plus** an **AI Model Box** (Tier 2) where the artist types a natural-language prompt and the on-prem LLM writes and runs Blender code — building complex objects like a car or a teddy bear.

**Why it matters (business):** ADM's 3D Visualization team already uses AI to make texture images, but today that work lives in flat images and manual, repeatable, per-artist effort. This project closes the loop from *image → 3D asset → shared, reusable library → the artist's DCC tool*, on-prem (no data leaves ADM's network), reusing existing AI assets — and lays the groundwork for a production multi-user 3D asset platform.

---

## 2. Business Perspective

### 2.1 The problem (from ADM's slide)

ADM's 3D Visualization area describes its current state:

> *"By far, we are currently effective on AI-Assisted workflow. The utilization is mainly for generating texture images / image editing."*

The slide's three-stage workflow is:

| Slide stage | What the artist does today | Where it stops |
|---|---|---|
| **1. Reference** | Gathers flat photos (Google Images, Pinterest, a scan of a material) | A flat image |
| **2. AI: generate 3D texture images** | Runs AI to make a color + a grayscale/height variant | A set of flat maps |
| **3. Blender 3D** | **Manually** builds geometry, UVs, and applies the maps in Blender | The artist's hours, not automated |

And the stated **future direction** (still "not sure" per the slide): *store and organize digital assets accessible to every user.*

**The gap the PoC closes:** stage 3 (the expensive part) and the future goal (shared assets) are exactly where an artist spends time. Today the AI produces *textures*; the PoC produces *3D assets* and *a shared library*, and connects them to the tool the artist already uses (Blender).

### 2.2 Value proposition

| Dimension | What ADM gets |
|---|---|
| **Time** | Cuts the manual "photo → textured 3D object" cycle from hours to minutes; the 3D and textures are generated, not hand-built. |
| **Consistency** | Deterministic PBR synthesis + quality gates mean every asset is built from the same recipe (reproducible seed/prompt), not a different artist's guesswork. |
| **Reuse** | A shared asset library (the seed of the "accessible to every user" goal) — generate once, reuse across projects. |
| **Data control** | 100% on-prem on ADM's own PCAI cluster. Reference photos and generated IP never leave the corporate network — a real concern for automotive design IP. |
| **No new spend** | Reuses AI already paid for (Qwen-Image-Edit, the CS1 LLM, one existing A100). Minimum complexity by design. |
| **Strategic fit** | A concrete, working example of HPE Private Cloud AI delivering a real business use case on ADM's Ezmeral platform — a reference for further PCAI adoption. |

### 2.3 Stakeholders & users

| Role | What they care about |
|---|---|
| **ADM 3D Visualization artists** | Fast, good-looking 3D assets that drop straight into their Blender; a place to find and reuse assets. |
| **ADM 3D Visualization lead / IDD** | A working proof that the AI-assisted 3D workflow is viable; a path to scale it. |
| **ADM IT / platform (CS1 owners)** | A well-behaved workload on Ezmeral: reuses existing models, no new infra, standard Helm/Istio deploy. |
| **HPE (this engagement)** | A delivered PoC that demonstrates PCAI value and opens a funded Phase 2 (production platform). |

### 2.4 As-is vs. enabled workflow

| Step | **As-is** (today) | **Enabled by this PoC** |
|---|---|---|
| Reference | Manual sourcing, stored locally | Upload / URL / sample library, normalized + lit-removed automatically |
| Texture | AI → flat color + height images | AI albedo + full PBR set + **automatic quality gates** |
| 3D geometry | Artist models + UVs by hand | **One click**: real textured mesh (Hunyuan3D-2.1) or a CPU relief fallback |
| Preview | Render in DCC (minutes) | Live Three.js preview in the browser (seconds) |
| Library | Personal folders / DMS | Shared persistent library (browse / tag / download) |
| Into Blender | Manual node wiring | **Tier 1:** load a pack → done · **Tier 2:** type a prompt → the LLM builds it |

### 2.5 Business outcomes / success criteria (PoC)

| # | Criterion | State |
|---|---|---|
| S1 | Deploys via Helm to CS1; public host serves the SPA + health | ✅ |
| S2 | Generate works with the existing i2i model: photo → albedo + PBR in < 2 min | ✅ |
| S3 | Fallback: with a model cold/down, the pipeline still completes and still produces a Blender pack | ✅ |
| S4 | Three.js preview renders texture + 3D live (internal network, no CDN) | ✅ |
| S5 | Asset library persists across pod restart (NFS PVC) | ✅ |
| S6 | Blender pack opens in real Blender (4.x / 5.x) and builds the object + PBR | ✅ |
| S7 | LLM box: natural-language prompts (e.g. "build a car") execute in Blender with ≤ 3 retries | ✅ |
| S8 | Zero new GPU/model deployments (reuse existing MLIS + LLM) | ✅ |
| S9 | Automated E2E suite passes against the live deploy | ✅ |

### 2.6 Scope & guardrails (PoC decisions, confirmed with the customer)

| # | Decision | Choice |
|---|---|---|
| D1 | Stage 3 coverage | Browser 3D preview **and** real Blender integration |
| D2 | Asset library | Minimal **file-based** on NFS PVC (browse/tag/download, **no DB**) |
| D3 | Texture model | **Reuse** existing MLIS `pcai-qwen-image-edit-gguf` (no new model/GPU) |
| D4 | Reference input | Upload + small bundled sample library |
| D5 | Deployment | New namespace `viz-demo`, CS1 registry, Helm pipeline (same as Styling Demo) |
| D6 | Blender integration | **Client-side** (artist's own Blender + add-on; no headless Blender *as the product* — though a build sidecar exists for `.blend` export) |
| D7 | LLM for the Blender box | **Reuse** existing CS1 LLM `qwen3-8-27b` via LiteLLM (zero new deployment) |
| D8 | Demo emphasis | Tier 1 (deterministic pack) **and** Tier 2 (LLM prompt box) |

**Explicitly out of scope for the PoC:** authentication/accounts, microservices, database, multi-tenancy, a managed Blender render farm, tiling/UV-unwrap, video. These are the **production roadmap** (Section 5).

---

## 3. Technical Perspective

> Two diagrams accompany this section:
> - **`docs/diagrams/architecture.html`** — system architecture (components + data flow + CS1 deployment)
> - **`docs/diagrams/pipeline.html`** — the end-to-end generate pipeline (stages, AI models, fallbacks)

### 3.1 System architecture (summary)

The system is **one backend pod + one NFS PVC**, consuming **three existing/cheap AI services**:

```
Artist browser (Three.js SPA)                 Artist workstation
   └─ /api/* (REST) + static UI                 Blender 5.x + add-on
        │                                            │  Tier 1: Load Blender-pack (scene.py)
        ▼                                            │  Tier 2: AI Model Box ────────────────┐
┌─────────────────────────────────────────────┐      │                                        │
│ viz-demo-app (pod, ns: viz-demo)             │      │                                        ▼
│  ┌───────────────────────────────────────┐  │   ┌──────────────────────┐        ┌────────────────────────┐
│  │ app (FastAPI, CPU-only)               │  │   │ existing MLIS i2i    │        │ CS1 LiteLLM (existing)   │
│  │  • generate (i2i + PBR + gates)       │  │   │ Qwen-Image-Edit      │        │ qwen3-8-27b (reasoning)  │
│  │  • real-3d client (Hunyuan3D-2.1)     │  │   │ (A100) → albedo      │        │ → bpy code (Tier 2)      │
│  │  • 2.5D CPU geometry (fallback)       │  │   └──────────────────────┘        └────────────────────────┘
│  │  • asset store, blender-pack, sidecar │  │   ┌──────────────────────┐
│  │  │   proxy, static frontend           │  │   │ Hunyuan3D-2.1        │
│  └───────────────────────────────────────┘  │   │ (A100) → textured GLB│
│  ┌───────────────────────────────────────┐  │   └──────────────────────┘
│  │ blend (sidecar, headless Blender)     │  │
│  │  → builds .blend from the pack         │  │
│  └───────────────────────────────────────┘  │
│        │  /data (references, generated, blends)
└────────┼────────────────────────────────────┘
         ▼
   NFS PVC  viz-results (10Gi, RWO)
   Istio VirtualService → https://viz-demo.aie.cs1.ctc.sg.lab  (420s route timeout)
```

**Component inventory (what is actually built):**

| Component | New? | Role |
|---|---|---|
| `pcai-viz-app` image (FastAPI + Three.js SPA + CPU geometry) | New image | The app + UI |
| `pcai-viz-blend` image (headless Blender via `bpy` wheel) | New image | `.blend` export sidecar |
| `pcai-hunyuan3d-serve` image (Hunyuan3D-2.1) | New image (GPU) | Image → textured GLB |
| Helm chart + manifests (`viz-demo`) | New | Deploy |
| `blender-addon/viz_demo_addon.py` | New (repo file) | Tier 1 + Tier 2 in the artist's Blender |
| MLIS Qwen-Image-Edit, CS1 LLM + LiteLLM, A100, PVC backend | **Existing** | Consumed, not built |

### 3.2 End-to-end generate pipeline

`POST /api/generate` (synchronous) runs these stages:

1. **Ingest & normalize** — accept a reference (file / URL / path), validate (≥ 512 px, PNG/JPG ≤ 8 MB), then **delight** (remove photographic lighting) so the albedo is flat. Compute a **content-hash asset ID** → **idempotent**: the same photo always maps to the same asset; re-generating returns the existing one.
2. **AI albedo** — call the existing **Qwen-Image-Edit i2i** (`POST /v1/images/edits`, single reference, `strength` 0.3–0.95). **Fallback:** if the predictor is cold/down, the reference itself is used as the albedo (`mode: "fallback-reference"`) so the rest of the pipeline still works.
3. **PBR synthesis (deterministic, PIL/numpy)** — derive **height** (luminance), **normal**, **roughness**, **metallic** from the albedo. Run **quality gates** (normal + albedo de-light check); on failure, **retry the generation (≤ 2×)**. This keeps albedo/height perfectly correlated and the process reproducible.
4. **Optional 3D (`do_geometry`)** — first try **real 3D**: **Hunyuan3D-2.1** (async `/send` + short `/status` polls, octree 512, 10 steps, ~3–4 min) → a **textured GLB** (true mesh + baked PBR). **Automatic fallback:** if the service is busy/down, build a **CPU 2.5D relief** from the image itself (**rembg U2NetP** silhouette + **Depth-Anything V2** depth → textured front surface + back plane → OBJ). The flow never dead-ends.
5. **Persist** — write maps + `meta.json` to the PVC → the asset is in the library.
6. **Consume** — the frontend previews (Three.js: albedo map + bump, or the GLB via GLTFLoader); the artist downloads maps / Blender-pack / `.blend`.

### 3.3 AI models & inference

| Model | Where | Used for | Notes |
|---|---|---|---|
| **Qwen-Image-Edit (GGUF)** | Existing MLIS predictor, A100 | Albedo (i2i) | OpenAI-compatible `/v1/images/edits`; single-reference; `strength` tunable; ~15–60 s |
| **Hunyuan3D-2.1** | **New** predictor on the existing A100 | Image → textured GLB | Direct k8s Deployment+Service (not MLIS — see 3.8); async `/send`+`/status`; `texture=true`; ~3–4 min at octree 512 / 10 steps |
| **Qwen3-8-27B (reasoning)** | Existing CS1 LLM via LiteLLM | Tier 2: Blender (bpy) code gen | Client-side (artist's add-on calls it); reasoning model — needs a large token budget (10240) + 300 s timeout; empty-code guard prevents silent no-ops |
| **rembg U2NetP** | CPU (in-app, ONNX) | 2.5D fallback: object silhouette | CPU-only, cached in image |
| **Depth-Anything V2 Small** | CPU (in-app, torch) | 2.5D fallback: surface depth | CPU-only, cached in image |

**Cost posture:** the only *new* GPU workload is Hunyuan3D-2.1, which runs on the cluster's **existing** A100s (a new image, not a new card). Everything else is existing or CPU-only — **no new GPU or model spend for the PoC**.

### 3.4 Backend (FastAPI, CPU-only)

Single-file app (`backend/main.py`) + focused modules:

| Module | Responsibility |
|---|---|
| `main.py` | Routes: `generate`, asset CRUD, maps/model/GLB, blender-pack, `.blend` proxy, samples, health; static SPA (no-cache) |
| `texture.py` | i2i client (incl. the `sd_cpp_extra_args` quirk), delight, deterministic PBR derivation, quality gates |
| `real3d.py` | Hunyuan3D-2.1 client: async `/send` + resilient `/status` polling (survives transient 502/503/504 during GPU work) |
| `geometry.py` | CPU 2.5D relief: rembg silhouette + Depth-Anything depth → textured OBJ |
| `blender_pack.py` | Assembles the pack zip (5 maps + rendered `scene.py` + README + `model.glb`/`model.obj` + CA + key) |

**Key resilience patterns:**
- **Liveness vs readiness separation** — `/healthz` is a no-op (never blocks on I/O), `/api/health` checks model readiness. A long 3D build can slow the app but **never** trips the liveness probe and restart the pod mid-job.
- **Synchronous `generate` in the threadpool** (not the event loop), with geometry models **prewarmed** in a background daemon thread so first-use latency is short.
- **CPU thread capping** (`OMP/ONNX/MKL=1`) to match the pod's 1-vCPU quota — prevents oversubscription thrash on the 64-core node.
- **Idempotent asset IDs** (content hash) + **self-healing backfill** (a stale 2.5D asset is upgraded to real 3D on the next `do_geometry`).
- **No-cache static files** so a browser never serves a stale `app.js` after a deploy.

### 3.5 Frontend (Three.js SPA)

Static `index.html` + `app.js` + **bundled** `three.min.js` + `three-gltfloader.js` (no CDN — ADM's internal network). Served by the backend pod.

- **Two tabs:** *Studio* (generate) and *Library*.
- **Studio — 3 columns:** ① Reference (dropzone + sample thumbnails + strength) · ② Generate & result (spinner, albedo/PBR thumbnails, mode badge, "Build 3D from the image" toggle) · ③ **3D preview** (Three.js: albedo map + height bump, or the real GLB; orbit controls; download maps / Blender-pack / `.blend`).
- **Library:** grid of assets (thumb, name, tag, date), search, rename/tag, per-asset downloads.
- Defensive JSON parsing on every handler (learned from the Styling Demo).

### 3.6 Blender integration (artist-side)

A single-file add-on (`blender-addon/viz_demo_addon.py`), Blender 4.x/5.x, stdlib + `bpy`. Installed once in the artist's Blender.

**Tier 1 — Load Blender Pack (deterministic, always works, no LLM):**
- Artist picks the unzipped pack folder → the add-on runs `scene.py` → the object + full PBR appear in the viewport, and a `.blend` is saved.
- `scene.py` is **version-robust** (4.x + 5.x, `hasattr`-guarded) and prioritizes: **real GLB** (Hunyuan3D-2.1, keeps baked PBR) → **image-derived OBJ relief** → **procedural shape** (cup/dog/wood/hat/car). It wires the full PBR set into the Principled BSDF and packs textures into the `.blend` (portable across machines).

**Tier 2 — AI Model Box (LLM code gen):**
- Prompt → **OpenAI-compatible** LLM call (urllib only, no SDK) → extract the ```` ```python ```` block → **safe-check** (module blocklist: `os/subprocess/shutil/socket/ctypes/pickle/http/urllib` + AST parse) → `exec` in Blender → **auto-retry ×3** feeding the traceback back to the LLM.
- **Blender 5.x API-aware system prompt** (explicitly forbids removed 2.8/3.x APIs like `links`) — the fix that stopped the model emitting stale code.
- **Reasoning-model handling:** `LLM_MAX_TOKENS = 10240` (Qwen3-8-27B burns budget on reasoning), `timeout = 300 s`, and an **empty-code guard** so an exhausted token budget is treated as a *retryable failure*, never a silent no-op success.
- **Prompting guidance (proven):** natural-language but fully specified (name every object + exact location/size, "keep it short, import only bpy") → a car builds in ~119 s, first attempt. For anything heavier, split into 2–3 small fully-specified prompts (stays under the 300 s ingress cap).
- **Two connection paths** (both shipped in the pack): ① public ingress `https://litellm.aie.cs1.ctc.sg.lab/v1` + the private CA bundle + a scoped **virtual key** (`ca/key.txt`), or ② `kubectl port-forward` to `http://localhost:4001` (no TLS/CA).

### 3.7 Asset library & storage

- **File-based, no DB** (PoC): each asset is a directory under `/data/generated/{id}/` holding the 5 maps, optional `model.glb`/`model.obj`, and `meta.json`. The library is a directory scan.
- **PVC:** `viz-results`, 10 Gi, NFS, `ReadWriteOnce`. Assets **persist across pod restarts**.
- `meta.json` records id, name, tag, source, prompt, strength, seed, mode, geometry info, gates, timestamp — the seed for **data lineage / reproducibility** in production.

### 3.8 Deployment (Helm, CS1)

- **Namespace** `viz-demo`; **images** from `registry.ctc.sg.lab:5000` (Basic-auth `admin:admin`).
- **`scripts/build-deploy.sh`** (run from the Windows host → CS1 bastion): `secrets` · `image` (build+push app + sidecar) · `deploy` (`kubectl apply`, `VIZ_TAG` substitution) · `verify` (pods, endpoints, in-cluster + public health) · `register` (publish the EzAppConfig so the app appears as an Ezmeral UI tile).
- **VirtualService** `viz-demo` → `https://viz-demo.aie.cs1.ctc.sg.lab`, **route timeout 420 s** (raised for the ~3–4 min real-3D path).
- **Pod spec:** `app` (1 vCPU / 2 Gi) + `blend` sidecar (1 vCPU / 2 Gi), `fsGroup: 1000` (Kyverno enforces `runAsNonRoot`; no root init container).
- **Hunyuan3D-2.1** is a **direct k8s Deployment+Service** in `project-user-aieadmin` (pinned to the A100 via `nodeSelector` + `mlis-gpu` priority class + ezua GPU-scheduler labels). *Why not MLIS:* MLIS's pre-validation does an unauthenticated `HEAD` on the private registry and gets 401 (no registry credential), so this is the working path — reversible to an MLIS PackagedModel once the CS1 admin adds the registry cred.

### 3.9 API contract (key endpoints)

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/generate` | POST | Ingest reference → albedo + PBR (+ optional real-3D / 2.5D) → asset |
| `/api/assets` | GET | List the library (dir scan) |
| `/api/assets/{id}` | PATCH | Rename / tag |
| `/api/assets/{id}/maps/{kind}.png` | GET | albedo / height / normal / roughness / metallic |
| `/api/assets/{id}/model.glb` / `model.obj` | GET | Real-3D GLB / 2.5D relief OBJ |
| `/api/assets/{id}/blender-pack` | GET | Zip: 5 maps + `scene.py` + README + model + CA + key |
| `/api/assets/{id}/blend` | GET | Ready-to-open `.blend` (proxied to the sidecar) |
| `/api/samples` / `/api/samples/{file}` | GET | Bundled reference library |
| `/api/health` / `/healthz` | GET | Readiness (checks models) / liveness (no-op) |

### 3.10 Quality & verification

- **E2E suite** (`scripts/test_e2e.py`) runs against the **live** deploy: generate from each sample → assert maps + pack + library entry; **fallback path** (dead model); **visual inspection** (build the `.blend`, render headless, assert the surface colour-composition matches the original photo, ≥ 0.50); **export-open validation** (the `.blend` opens via File>Open, holds both mesh variants + UVs + packed textures, round-trips, and imports with the source PNGs removed — proving portability).
- **Add-on unit tests** (25/25) cover the LLM-box core (block extraction, safe-check, retry, empty-code guard) outside Blender.
- **Live Tier 2 verification** in real Blender 5.2 (car, 6 objects, first attempt).

### 3.11 Key engineering decisions & trade-offs

| Decision | Why |
|---|---|
| **Reuse existing AI, zero new models** | Minimum complexity + no new spend; de-risks the concept fastest |
| **Synchronous `generate`, no queue** | Fine for PoC scale; the 300 s/420 s timeouts are the cost (see production roadmap for async) |
| **File-based library, no DB** | Fastest path; `meta.json` is the upgrade point to a real DB |
| **Client-side Blender (add-on)** | No Blender container to maintain/render; the artist's machine is the DCC. (A build **sidecar** still exists purely for `.blend` export.) |
| **Deterministic PBR (PIL/numpy), not a 2nd GPU model** | Free, reproducible, never drifts from the albedo |
| **Real-3D first, 2.5D CPU fallback** | Best quality when the A100 is free; never dead-ends when it isn't |
| **Liveness vs readiness split + thread capping** | Long GPU/CPU jobs must not crash-loop the pod |
| **Private-CA + scoped virtual key shipped in the pack** | Secure, revocable, self-contained Tier 2 connection (no master key in the artist's hands) |

---

## 4. Current status (v14.x / v14.11)

| Item | State |
|---|---|
| **Codebase** | v14.x (v14.11 latest commit); clean git history |
| **Deploy** | `viz-demo-app` in ns `viz-demo`, live on CS1; public host `https://viz-demo.aie.cs1.ctc.sg.lab` → 200 |
| **Texture (i2i)** | `mode: model` (real Qwen-Image-Edit on A100), ~30 s, albedo + full PBR, persisted to PVC |
| **Real 3D** | Hunyuan3D-2.1 → textured GLB (octree 512 / 10 steps), ~3–4 min; automatic 2.5D CPU relief fallback |
| **Preview** | Three.js: PBR + height bump, or real GLB via GLTFLoader |
| **Library** | Persistent (NFS PVC), browse / tag / download |
| **Blender Tier 1** | Verified in real Blender 5.2 (pack → `.blend`, PBR wired, packed, portable) |
| **Blender Tier 2** | LLM box verified (car: 6 objects, first attempt, ~119 s); 5.x API-aware prompt + empty-code guard + retry |
| **E2E** | Full suite green against the live deploy (visual inspection + export-open) |
| **Add-on tests** | 25/25 |

**Known PoC limitations (by design, addressed in the roadmap):** no auth, single replica, synchronous long jobs (300 s/420 s caps), file-based library (no search index / no concurrency guard on rename), single-tenant, no observability stack, no backup/DR of the PVC, the Tier-2 LLM is a general reasoning model (not Blender-specific), no server-side rendering.

---

## 5. Future expansion path → production grade

> Companion diagram: **`docs/diagrams/production-roadmap.html`** (PoC → Pilot → Enterprise)

The PoC is deliberately minimum-complexity. Moving to a production, multi-user, multi-tenant platform is a **phased** effort. Each phase is independently valuable and reduces the next.

### 5.1 Phased roadmap

**Phase 1 — Harden & Pilot (make it safe for named users)**
| Capability | What to add |
|---|---|
| **Authentication & authorization** | OIDC/SAML via Istio; RBAC per ADM user; per-user asset ownership. Replaces "internal host only, no auth." |
| **Async job pipeline** | Replace synchronous `generate` (300 s cap) with a **queue** (Celery/Arq/RQ + Redis) + a **job status API** (poll or SSE). The 3–4 min real-3D and multi-minute LLM runs no longer hold an HTTP connection; the 300 s/420 s ingress caps disappear. |
| **Database + object store** | Move metadata to **Postgres** (assets, jobs, users, tags, lineage) and large binaries to **S3/MinIO** (or keep the PVC for now). Enables real search, concurrency-safe updates, and multi-replica. |
| **Scaling** | Multi-replica backend + **HPA** (scale the app, not the GPU). Knative **scale-to-0** for the Hunyuan3D-2.1 predictor (GPU cost). |
| **Observability** | Prometheus `/metrics` (jobs, latency, GPU utilization, gate pass/fail), structured logs, tracing, dashboards + alerts. |
| **Cost & abuse controls** | Rate limiting, per-user quota, per-request cost metering. |
| **Hardening** | Secret management (Vault), network policies, image signing, input validation, audit logging. |

**Phase 2 — Multi-tenant platform (the "accessible to every user" goal)**
| Capability | What to add |
|---|---|
| **Multi-tenancy** | Per-team isolation (storage prefix / namespace, RBAC, data isolation), shared-vs-private asset scopes. |
| **Collaboration** | Asset versioning, favorites, sharing, comments, **approval workflow** (submit → review → publish). |
| **Better asset search** | Indexed search (full-text + tags), and **semantic search** (embed the reference/albedo) so artists can "find the wood texture I made last month." |
| **Quality at scale** | Automated scoring (VLM critic loop: render → score → reject/retry) + **human review** queue; keep the deterministic gates. |
| **Data lineage & reproducibility** | Record seed, prompt, model version, and parameters per asset (already in `meta.json`) — full provenance and "rebuild this exactly." |
| **DAM/PDM/PLM integration** | Sync with ADM's existing Digital Asset Management / Product Data Management so assets flow into the real design process. |
| **DR & operations** | PVC/object-store backup, GitOps (ArgoCD) + IaC (Terraform) for the whole stack, SLAs, capacity planning, load testing. |

**Phase 3 — Advanced AI capabilities (the "wow" tier)**
| Capability | What to add |
|---|---|
| **Blender-specific LLM** | Swap the general reasoning model for **BlenderLLM** (8B, Qwen2.5-Coder fine-tune on BlendNet) via vLLM on MLIS — same add-on client, one URL change — for higher-quality, faster bpy code. |
| **Agentic modeling (LL3M-style)** | 1 coding LLM + RAG over Blender material/texture-node docs + a VLM render-check loop — the research ceiling, now feasible as a funded tier. |
| **Production texture quality** | Sobel-derived normal maps, **UV unwrap**, **seamless tiling** (seam-inpaint model), and PBR from multiple views (not just a single flat map). |
| **Multi-view / 4D** | Multi-view 3D reconstruction (turntable), and short video / animation of the asset (connects to the Styling Demo's video stage). |
| **Managed Blender** | Optional server-side render farm / headless Blender for "no Blender installed" users and automated render checks (currently client-side only). |
| **Mobile / tablet** | Asset browsing + approval on the shop floor (the web SPA is the base). |

### 5.2 Capability gaps: PoC → production (summary table)

| Area | PoC (today) | Production target |
|---|---|---|
| **Security** | No auth, internal host | OIDC/SAML, RBAC, per-user ownership, Vault, network policies, image signing |
| **Data** | File-based, single PVC, RWO | Postgres + S3/MinIO, indexed + semantic search, multi-replica |
| **Jobs** | Synchronous (300 s / 420 s caps) | Async queue + status API (SSE/poll); no connection held across GPU work |
| **Scale** | 1 replica, 1 GPU | HPA (app) + Knative scale-to-0 (GPU); horizontal |
| **Tenancy** | Single-tenant | Multi-team isolation, shared/private scopes |
| **Observability** | Logs + health | Prometheus, tracing, dashboards, alerts, cost metering |
| **Quality** | Deterministic gates | Gates + VLM critic + human review; lineage/reproducibility |
| **LLM** | General reasoning (Qwen3-8-27B) | BlenderLLM (8B) / agentic (RAG + VLM) |
| **3D** | Single-view real-3D + 2.5D fallback | Multi-view, UV/tiling, video; optional managed render |
| **Operations** | Helm + build-deploy script | GitOps (ArgoCD) + IaC (Terraform), DR/backup, SLAs |
| **Ecosystem** | Standalone | DAM/PDM/PLM + mobile |

### 5.3 Target production architecture (Phase 2+ shape)

```
Web / mobile SPA  ──►  API gateway (Istio, OIDC/SAML, RBAC)
                          │
              ┌───────────┴────────────┐
              │   API / job service     │  (multi-replica, HPA)
              │   (FastAPI, Postgres)   │
              └───────────┬────────────┘
        ┌──────────────────┼───────────────────────┐
        ▼                  ▼                        ▼
  Job queue (Redis)   Object store (S3/MinIO)   Vector index (semantic search)
        │
   Workers: i2i · Hunyuan3D-2.1 (Knative GPU, scale-to-0) · LLM/BlenderLLM
        │
   Postgres (assets, users, jobs, lineage)  +  VLM critic / human review
        │
   DAM / PDM / PLM sync  ·  Prometheus / tracing / alerts
```

### 5.4 Effort, dependencies & risks (indicative)

| Phase | Indicative effort | Key dependencies | Main risk / mitigation |
|---|---|---|---|
| **1 — Harden/Pilot** | ~4–8 wks | OIDC provider, Postgres/Redis (in-cluster), Redis for queue | Long-job UX → solved by async queue; auth integration with AD's IdP |
| **2 — Multi-tenant** | ~6–10 wks | DAM/PDM/PLM interfaces, vector index, review workflow | Data isolation & performance at scale → per-team storage + sharding; load-test early |
| **3 — Advanced AI** | ~8–16 wks (research) | BlenderLLM on MLIS, VLM critic, multi-view data | LLM code quality / agentic cost → cap attempts, cache, human-in-the-loop |

**Dependencies (ADM side):** an IdP for OIDC; DAM/PDM/PLM contact + interface docs; a business owner for the asset-review workflow; GPU headroom if multi-view/advanced 3D is added.

**Strategic note:** Phase 1 alone is often the "go-live for named users" milestone and the natural **funded Phase 2** ask — it converts the PoC into a safe, observed, multi-user internal tool with minimal new infra (queue + DB + auth all run in-cluster).

---

## Appendix A — Repository layout

```
pcai-3d-viz-demo/
├── backend/            FastAPI app: main.py, texture.py, real3d.py, geometry.py,
│                       blender_pack.py, requirements{,-3d}.txt, templates/scene.py.template
├── frontend/           static SPA: index.html, app.js, vendor/ (three.min.js, GLTFLoader)
├── sidecar/            headless Blender .blend builder (build_blend.py, Dockerfile)
├── blender-addon/      viz_demo_addon.py (Tier 1 + Tier 2), tests, README
├── hunyuan/            Hunyuan3D-2.1 predictor (Dockerfile, predictor-deploy.yaml, seed-job)
├── manifests/          namespace, pvc, app (deployment+service), virtualservice, ezappconfig
├── chart/              Ezmeral registration chart (EzAppConfig)
├── ca/                 private CA bundle (ezaf-aie-root-ca.pem) + key.txt (gitignored)
├── models/             CPU geometry model caches (rembg U2NetP, Depth-Anything V2 Small)
├── sample_data/        bundled reference photos (hat / car / cup)
├── scripts/            build-deploy.sh, test_e2e.py, render_blend.py, validate_blend_import.py
└── docs/               TECHNICAL_DESIGN.md (v1) + PROJECT_OVERVIEW.md + diagrams/
```

## Appendix B — Glossary

| Term | Meaning |
|---|---|
| **PCAI / Ezmeral** | HPE Private Cloud AI / HPE Ezmeral — ADM's on-prem AI platform (CS1 cluster) |
| **MLIS** | Model Lifecycle & Inference Serving — HPE's inference-serving layer (predictors, OpenAI-compatible endpoints) |
| **i2i** | image-to-image (Qwen-Image-Edit) |
| **PBR** | Physically-Based Rendering material set (albedo, normal, roughness, metallic, height) |
| **GLB / OBJ** | glTF-binary / Wavefront — 3D mesh formats |
| **Hunyuan3D-2.1** | Tencent's image → textured 3D mesh model (used for real 3D) |
| **2.5D relief** | CPU fallback: the image's silhouette + depth extruded into a textured surface |
| **Tier 1 / Tier 2** | Deterministic Blender-pack / LLM Blender code-gen (both in the add-on) |
| **PVC** | Kubernetes PersistentVolumeClaim (here: the NFS-backed asset store) |

## Appendix C — References

- Arya's slide: *AI Use Case in IDD – 3D Visualization* (2026-10-01) — source of the 3-stage workflow + future goal.
- `docs/TECHNICAL_DESIGN.md` — original v1 design & execution plan (scope decisions D1–D8, E2E outline).
- CS1 platform: `cs1_platform/kubeconfig-cs1.conf`, registry `registry.ctc.sg.lab:5000`, Istio gateway `ezaf-gateway`.

---

*Prepared by HPE Solution Architecture (Private Cloud AI). Figures are from the live v14.x implementation on CS1.*
