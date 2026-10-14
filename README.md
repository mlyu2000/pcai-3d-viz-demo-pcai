# 3D Visualization Asset Generator

> **Personal standalone copy** of the *3D Visualization Asset Generator* demo (photo → PBR + textured 3D asset → Blender integration), validated on HPE Private Cloud AI (CS1). Restructured to the `ai-solution-eng/ai-solution-demos` contribution layout. See the [contribution guidelines](https://github.com/ai-solution-eng/ai-solution-demos/blob/main/CONTRIBUTING.md).
*An artist drops in a reference photo, clicks Generate, and gets a production-ready 3D textured asset in the browser plus a ready-to-open `.blend` file — powered by AI running on HPE Private Cloud AI. Author: Eric Yu (HPE).*

| Owner                 | Name    | Email            |
| ----------------------|---------|------------------|
| Use Case Owner        | Eric Yu | eric.yu@hpe.com  |
| PCAI Deployment Owner | Eric Yu | eric.yu@hpe.com  |

*Use Case Owner: brought and designed this demo for ADM's 3D Visualization team. Contact for more information.*

*PCAI Deployment Owner: ported and validated the demo on PCAI (CS1 / Ezmeral). Contact on deployment issues.*

## Abstract

This demo closes the loop from **reference photo → AI texture → real 3D asset → shared library → the artist's Blender**, all on-prem. ADM's 3D Visualization team already uses AI to make *texture images*; today the expensive step — building the geometry and wiring the maps in Blender — is manual. This tool automates it.

- **Generates PBR materials from a photo** — a reference image becomes a clean albedo via the on-prem Qwen-Image-Edit model, plus a deterministic PBR set (albedo, height, normal, roughness, metallic) with automatic quality gates.
- **Generates a real textured 3D mesh** — an opt-in step produces a true textured GLB via **Hunyuan3D-2.1** on the existing A100, with an automatic CPU 2.5D-relief fallback so the flow never dead-ends.
- **Live 3D preview** — a Three.js web preview shows the texture and the 3D object in the browser (works on the internal network, no CDN).
- **Real Blender integration** — a downloadable **Blender pack** (Tier 1, deterministic) plus an **AI Model Box** (Tier 2): the artist types a natural-language prompt and the on-prem LLM writes and runs Blender code to build complex objects.
- **Shared asset library** — a persistent, file-based library on an NFS PVC (browse / rename / tag / download) — the seed of a future multi-user asset platform.
- **Zero new models or GPU spend** — reuses the existing MLIS image model and the existing LiteLLM LLM.

Features:
- Idempotent generation (content-hash asset ID — the same photo always maps to the same asset).
- Deterministic PBR synthesis (reproducible, never drifts from the albedo).
- Automatic fallback when the GPU 3D service is busy.
- Private-CA + scoped virtual key shipped in the Blender pack for secure, self-contained Tier 2.

Recordings:
- Demo video is **not hosted on GitHub** per repo policy; available on request / at review.

## Description

### Overview

The system is **one backend pod + one NFS PVC**, consuming three existing/cheap AI services. An Istio VirtualService serves the public hostname; the FastAPI backend (CPU-only) orchestrates texture generation, 3D reconstruction, the asset library, and a headless-Blender sidecar for `.blend` export.

```
Artist browser (Three.js SPA)                 Artist workstation
   └─ /api/* (REST) + static UI                 Blender 4.x/5.x + add-on
        │                                            │  Tier 1: Load Blender-pack (scene.py)
        ▼                                            │  Tier 2: AI Model Box ───────────────┐
┌─────────────────────────────────────────────┐      │                                        ▼
│ viz-demo-app (pod, ns: viz-demo)             │      │                                        │
│  app (FastAPI, CPU-only)                     │      │   ┌──────────────────────┐   ┌────────────────────────┐
│   • generate (i2i + PBR + gates)             │      │   │ MLIS Qwen-Image-Edit │   │ LiteLLM qwen3-8-27b    │
│   • real-3d client (Hunyuan3D-2.1)           │◄─────┼───│ (A100) → albedo      │   │ → bpy code (Tier 2)    │
│   • 2.5D CPU geometry (fallback)             │      │   └──────────────────────┘   └────────────────────────┘
│   • asset store, blender-pack, static UI     │      │   ┌──────────────────────┐
│  blend (sidecar, headless Blender) → .blend  │      │   │ Hunyuan3D-2.1 (A100) │
│        │  /data (references, generated, blends)     │   │ → textured GLB       │
└────────┼────────────────────────────────────┘      │   └──────────────────────┘
         ▼
   NFS PVC  viz-results (10Gi, RWO)
   Istio VirtualService → https://viz-demo.aie.cs1.ctc.sg.lab  (420s route timeout)
```

Interactive diagrams (open in a browser):
[`doc/diagrams/architecture.html`](doc/diagrams/architecture.html) (system architecture + data flow + CS1 deployment),
[`doc/diagrams/pipeline.html`](doc/diagrams/pipeline.html) (end-to-end generate pipeline + fallbacks),
[`doc/diagrams/process-flow.html`](doc/diagrams/process-flow.html),
[`doc/diagrams/data-flow.html`](doc/diagrams/data-flow.html),
[`doc/diagrams/production-roadmap.html`](doc/diagrams/production-roadmap.html).

UI screenshots in [`doc/screenshots/`](doc/screenshots/): `ui_studio.png`, `ui_result.png`, `ui_library.png`, `ui_blender.png`, `ui_blender_tier2.png`.

| Component | New? | Role |
|---|---|---|
| `pcai-viz-app` (FastAPI + Three.js SPA + CPU geometry) | New image | The app + UI |
| `pcai-viz-blend` (headless Blender via `bpy`) | New image | `.blend` export sidecar |
| `pcai-hunyuan3d-serve` (Hunyuan3D-2.1) | New GPU image | Image → textured GLB |
| `blender-addon/viz_demo_addon.py` | New (repo file) | Tier 1 + Tier 2 in the artist's Blender |
| MLIS Qwen-Image-Edit, CS1 LLM + LiteLLM, A100, PVC | **Existing** | Consumed, not built |

### Workflow

`POST /api/generate` (synchronous) runs these stages:

1. **Ingest & normalize** — accept a reference (file / URL / path), validate (≥ 512 px, PNG/JPG ≤ 8 MB), then **delight** (remove photographic lighting) so the albedo is flat. A **content-hash asset ID** makes it idempotent.
2. **AI albedo** — call the existing **Qwen-Image-Edit i2i** (single reference) → clean albedo.
3. **Deterministic PBR** — derive height, normal, roughness, metallic from the albedo (PIL/numpy) with quality gates.
4. **Real 3D (opt-in)** — **Hunyuan3D-2.1** on the A100 → textured GLB (~3–4 min); if the GPU service is busy, fall back to a **CPU 2.5D-relief** mesh so the flow still completes.
5. **Blender pack / `.blend`** — bundle the 5 maps + `scene.py` + model + CA + scoped key into a zip; the headless sidecar builds a ready-to-open `.blend`.
6. **Library** — persist to the NFS PVC (browse / tag / download); the browser preview (Three.js) shows the PBR + height bump or the real GLB.

**Tier 2 (AI Model Box)** — the artist types e.g. "build a car" in the Blender add-on; the on-prem LLM (via LiteLLM) writes `bpy` code, which the add-on safely executes (block extraction, safe-check, ≤ 3 retries, empty-code guard).

Key endpoints: `/api/generate`, `/api/assets`, `/api/assets/{id}/maps/{kind}.png`, `/api/assets/{id}/model.glb`, `/api/assets/{id}/blender-pack`, `/api/assets/{id}/blend`, `/api/samples`, `/api/health`, `/healthz`.

## Deployment

### Prerequisites

- **HPE Private Cloud AI** (Ezmeral Kubernetes) with **MLIS / Aioli** (an image-to-image model — Qwen-Image-Edit), **LiteLLM** + an LLM (qwen3-8-27b), Istio, and an NFS PVC.
- **1 GPU (A100)** for the real-3D step (Hunyuan3D-2.1). The texture path and Blender box need **no new GPU** — they reuse existing services. The 2.5D fallback runs CPU-only.
- App pod resources are small: `app` (1 vCPU / 2 Gi) + `blend` sidecar (1 vCPU / 2 Gi); `fsGroup: 1000` (Kyverno enforces `runAsNonRoot`).
- An internal registry for the airgapped build, and a hostname routed through the Ezmeral VirtualService (route timeout raised to 420 s for the ~3–4 min real-3D path).

All environment-specific values (namespace, registry, endpoints, model identity, secret names) live in [`deploy/config/values.yaml`](deploy/config/values.yaml).

### Installation and configuration

1. **Build & push images** (from the Windows host → CS1 bastion):
   ```bash
   bash source/code/scripts/build-deploy.sh
   ```
   Sub-steps: `secrets` · `image` (build+push app + blend sidecar) · `deploy` (kubectl apply, tag substitution) · `verify` (pods, endpoints, in-cluster + public health) · `register` (publish the EzAppConfig so the app appears as an Ezmeral UI tile).

2. **Deploy the Hunyuan3D-2.1 GPU service** — it runs as a direct Deployment+Service (pinned to the A100 via `nodeSelector` + `mlis-gpu` priority class + ezua GPU-scheduler labels). Manifest: [`source/chart/pcai-hunyuan3d-serve/deployment.yaml`](source/chart/pcai-hunyuan3d-serve/deployment.yaml). *(Why not MLIS: MLIS pre-validation does an unauthenticated HEAD on the private registry and gets 401; a direct Deployment is the working path, reversible to an MLIS PackagedModel once the registry credential is added.)*

3. **Deploy the app** with the workload chart:
   ```bash
   helm upgrade --install pcai-viz-demo deploy/chart/pcai-viz-demo-1.0.0.tgz \
     -f deploy/config/values.yaml -n viz-demo --create-namespace
   ```

4. **Register the EzApps tile** — apply the registration ConfigMap in [`deploy/config/chart-registration/`](deploy/config/chart-registration/).

5. **Install the Blender add-on** on each artist's workstation — copy [`source/code/blender-addon/viz_demo_addon.py`](source/code/blender-addon/viz_demo_addon.py) (Edit → Preferences → Add-ons → Install).

> The private root CA for TLS verification ships in [`source/code/ca/ezaf-aie-root-ca.pem`](source/code/ca/ezaf-aie-root-ca.pem) and is bundled into each Blender pack.

## Running the demo

### Demo 1 — Photo → PBR + 3D asset (the core loop)

1. Open `https://viz-demo.aie.cs1.ctc.sg.lab`.
2. Pick a bundled sample (or drop in a reference photo) and click **Generate**.
3. Watch the Three.js preview fill in: the albedo + height bump first, then the real GLB.
4. Open the **Library** tab → the asset persists (survives a pod restart); rename/tag/download it.

### Demo 2 — Real Blender integration (Tier 1 + Tier 2)

1. From the asset, click **Download Blender pack** (Tier 1) — open it in the artist's own Blender; the object builds with the PBR maps wired and packed (portable — works with the source PNGs removed).
2. In the add-on's **AI Model Box** (Tier 2), type a prompt like *"build a car"* — the on-prem LLM writes and runs the `bpy` code, building the object in ≤ 3 retries.

### Validation / tests

- `python source/code/scripts/test_e2e.py` — full suite against the live deploy: generate from each sample → assert maps + pack + library; fallback path (dead model); visual inspection (render the `.blend` headless, assert colour-composition ≥ 0.50 vs the original); export-open validation (round-trip, imports with source PNGs removed).
- `python source/code/blender-addon/test_viz_demo_addon.py` — 25 unit tests for the LLM-box core (block extraction, safe-check, retry, empty-code guard).
- `python source/code/scripts/validate_blend_import.py` — Blender import validation.

## Limitations

- **No auth / single tenant / single replica** — PoC scope; authentication, multi-tenancy, and a DB are the production roadmap.
- **Synchronous long jobs** — `generate` is blocking with 300 s / 420 s timeout caps; an async queue is a roadmap item.
- **File-based library** — no search index and no concurrency guard on rename; `meta.json` is the upgrade point to a real DB.
- **Single-photo reconstruction is approximate** — Hunyuan3D-2.1 from one photo yields a boxy mesh; visual gain from higher detail settings is marginal. Better inputs (or more views) are the real lever.
- **GPU contention** — the real-3D path needs the A100 free; when busy it falls back to a CPU 2.5D relief (lower fidelity).
- **Internal CA / airgapped** — the hostname uses an internal certificate and the build assumes an internal registry; not a public-internet demo.

See [`doc/PROJECT_OVERVIEW.md`](doc/PROJECT_OVERVIEW.md) for the full project overview and [`doc/TECHNICAL_DESIGN.md`](doc/TECHNICAL_DESIGN.md) for the technical design.