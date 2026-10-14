# PCAI 3D Visualization PoC — Technical Design & Architecture Plan

**Project:** AI-Assisted 3D Texture & Visualization Demo (ADM "3D Visualization Area" use case)
**Objective:** End-to-end PoC on PCAI: reference photo → AI-generated 3D texture maps (albedo + height) → live 3D preview + shared asset library → real Blender integration (artist-side)
**Deployment Target:** CS1 platform (HPE Ezmeral / MLIS), new namespace `viz-demo`, Helm chart, same registry/build pipeline as the Styling Demo
**Reference:** Arya's slide — *AI Use Case in IDD – 3D Visualization* (2026-10-01)
**Author:** Solution Architect
**Date:** 2026-10-01
**Status:** v1 — for review (scope confirmed with customer stakeholder 2026-10-01)

---

## 0. Requirement Understanding (from Arya's slide)

The slide describes ADM 3D Visualization team's **current** AI-assisted workflow and their **future** direction:

> *"By far, we are currently effective on AI-Assisted workflow. The utilization is mainly for generating texture images / image editing."*

Three-stage pipeline as drawn on the slide:

| Slide Stage | What it is | PoC equivalent |
|---|---|---|
| **1. Reference** — "Google Images, Pinterest, etc" | Artist gathers flat reference photos (slide shows: marble-like texture, classical bust) | Upload a reference image **or** pick from a bundled sample library — 3 real refs (speckled cup / merle dog / weathered wood); offline-safe |
| **2. AI: Generate 3D Texture images** *(red box = the core AI step)* | Each reference is turned into texture maps — a **color** variant and a **grayscale/height-map-like** variant (2×2 grid on slide) | **Qwen-Image-Edit (i2i)** on MLIS generates the albedo; the height map is derived **deterministically** (luminance) in Python — one model call per texture, no second GPU model |
| **3. Blender 3D** | Generated maps applied to 3D geometry in Blender | **Per-sample 3D shape**: each sample produces its own object (cup→mug, dog→recognizable low-poly quadruped, wood→beveled slab) in both the Three.js preview and the Blender pack. Two-tier: (a) **Blender-pack** — downloadable zip (maps + ready-to-run `scene.py` that builds the shape from a `SHAPE` param) the artist opens in their own Blender; (b) **LLM modeling box** in a small Blender add-on (prompt → generated bpy, with retry) powered by the **existing CS1 LLM** |

**Future direction (also on the slide, "still not sure"):** *store/organize digital assets accessible to every user.* PoC covers this with a **minimal file-based asset library** on an NFS PVC (browse / tag / download, no database, no accounts).

### Confirmed scope decisions (2026-10-01)

| # | Decision | Choice |
|---|---|---|
| D1 | Stage 3 coverage | Browser 3D preview (Three.js) **and** real Blender integration |
| D2 | Asset library | Yes — minimal file-based on NFS PVC, browse/tag/download, no DB |
| D3 | AI model for texture gen | **Reuse the existing MLIS `pcai-qwen-image-edit-gguf` i2i predictor** — no new model, no new GPU, no weight seeding |
| D4 | Reference input | User upload + small bundled sample library |
| D5 | Deployment | New namespace `viz-demo`, registry `registry.ctc.sg.lab:5000`, Helm pipeline identical to Styling Demo |
| D6 | Blender integration style | **Client-side**: web app outputs a Blender-pack + small add-on installed once in the artist's Blender (no headless Blender on the platform) |
| D7 | LLM for the Blender modeling box | **Existing CS1 LLM** (`qwen3-8-27b-int4-dflash2-r2` via LiteLLM) — zero new deployment |
| D8 | Demo emphasis for Blender | Tier 1 (deterministic pack) **and** Tier 2 (LLM prompt box) |

**Explicitly out of scope (PoC):** authentication/accounts, microservices, database, headless Blender container, BlenderLLM deployment, LL3M-style multi-agent system, tiling/UV-unwrapping, normal-map generation, video.

---

## 1. Executive Summary

One backend pod (FastAPI, CPU-only) + one static frontend (served by the same pod) + one NFS PVC + **one already-deployed GPU model** (Qwen-Image-Edit i2i predictor) + **one already-deployed LLM** (CS1 vision/coding LLM via LiteLLM). The frontend covers stages 1–2 of the slide plus a live 3D preview; the Blender-pack + add-on complete stage 3 in the artist's own Blender.

**Why this is minimum-complexity:**
- **Zero new models, zero new GPU resources, zero weight downloads.** The only AI on CS1 already runs; we just add HTTP clients.
- **One pod, one PVC, no queue, no DB, no auth.** Generation is a synchronous request (single ~15–60 s diffusion call, 5-min client timeout).
- **Deterministic wherever possible:** the height map is pure Python (PIL/numpy) — reproducible, free, and never drifts from the albedo. LLM-generated Blender code is optional (Tier 2), not on the critical path.
- **Blender stays on the artist's machine** — no Blender container image to build, maintain, or render in.

---

## 2. Architecture Overview

```
┌────────────────────────────────────────────────────────────────────────────┐
│                        Artist Browser (static SPA)                         │
│  [Reference: upload | sample library]  [Generate]  [Three.js 3D preview]  │
│  [Asset library: browse / tag / download / Blender-pack]                   │
└──────────────────────────────┬─────────────────────────────────────────────┘
                               │ HTTP (REST)
                               ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                     Backend (FastAPI, 1 pod, CPU-only)                      │
│  • POST /api/generate        i2i albedo (MLIS) + deterministic height      │
│  • GET  /api/assets, PATCH /api/assets/{id}   file-based library           │
│  • GET  /api/assets/{id}/blender-pack        zip: maps + scene.py + README │
│  • GET  /api/samples                                     bundled refs      │
│  • serves static frontend (Three.js)                                 │
└──────┬──────────────────────────────┬──────────────────────────┬───────────┘
       │ (1) i2i, OpenAI-compat       │ (2) not needed (PoC)     │
       ▼                              ▼                          ▼
┌─────────────────────────────┐ ┌───────────────────┐  ┌───────────────────┐
│ MLIS predictor (existing)   │ │ NFS PVC 10Gi      │  │ Artist workstation│
│ pcai-qwen-image-edit-gguf   │ │ /data/references/ │  │ Blender + add-on  │
│ (1 A100, Knative, ISVC in   │ │ /data/generated/  │  │  Tier1: Load pack │
│ project-user-aieadmin)      │ │ /data/samples/    │  │  Tier2: LLM box ──┼──▶ CS1 LiteLLM
│ POST /v1/images/edits       │ │ (NFS-backed,      │  │  (bpy code gen,   │    (existing,
│ single-reference i2i        │ │  survives restarts)│ │   auto-retry)     │     zero new deploy)
└─────────────────────────────┘ └───────────────────┘  └───────────────────┘
```

**Component inventory (what actually gets built):**

| Component | New? | Size |
|---|---|---|
| `viz-demo-backend` image (FastAPI + PIL/numpy + static frontend) | New image | ~1 pod, 500 m–2 CPU / 2–4 Gi |
| Helm chart `viz-demo` (deployment, service, PVC, VirtualService) | New chart | 4 templates |
| Blender add-on (`blender-addon/` in repo, client-side) | New (repo file only) | 1 Python file |
| `scene.py` Blender-pack template | New (repo file only) | 1 Python file |
| MLIS i2i model, CS1 LLM, LiteLLM, PVC backend | **Existing** | 0 |

---

## 3. Model & Inference Design

### 3.1 Texture generation — reuse `pcai-qwen-image-edit-gguf` (confirmed on CS1)

- **Endpoint (cluster-internal):** `http://pcai-qwen-image-edit-gguf-predictor.project-user-aieadmin.svc.cluster.local:80`
- **API:** OpenAI-compatible **image-to-image** — `POST /v1/images/edits` (multipart), single reference image + text prompt. *This predictor is single-reference only (verified in-pod 2026-09-29 for the Styling Demo); we never need two images.*
- **Auth:** Bearer token from the `mlis-key` secret (same secret the Styling Demo uses; copied into `viz-demo` ns once, see §7.4).
- **Call shape (proven — copy from Styling Demo `backend/main.py::_img_generate`, i2i branch):**
  - form fields: `prompt`, `size` = `WxH` (both **divisible by 16**, max side **768**), `num_inference_steps` (default 8), `strength` (default **0.7**, tunable slider 0.3–0.95)
  - sd-cpp quirk: this server ignores plain form params — effective params are passed via an embedded JSON block in the prompt: `…<sd_cpp_extra_args>{"strength":0.7,"sample_steps":8}</sd_cpp_extra_args>` (the Styling Demo already does this; reuse verbatim)
  - response: OpenAI-style `data[0].b64_json`
- **Aspect handling:** preserve the reference's aspect ratio (cap 768, align to 16) — same helper as Styling Demo.
- **Latency:** one diffusion call ≈ **15–60 s** on the A100; client timeout 5 min.
- **Fallback (model cold/down):** if the predictor is unreachable (Knative scale-to-0 first boot, or ISVC not Ready), the backend returns the reference image itself as the albedo with `mode: "fallback-reference"` and still derives the height map — **the entire pipeline, asset library, and Blender-pack keep working** without the GPU model (same fallback philosophy as the Styling Demo's pencil/composite fallbacks).

**Prompts (initial, to be tuned during Phase 5):**
- Albedo: `"Convert this reference image into a clean, flat, evenly-lit texture map suitable for 3D modeling. Preserve the material's color, pattern and detail. Output a flat texture with no objects, no background, no perspective, no shading artifacts."`
  - (Per-material variants — marble / wood / fabric — are optional presets in the frontend; the raw prompt stays editable by the artist.)
- Height: **no model call** — derived deterministically from the albedo (§3.2).

### 3.2 Height map — deterministic (PIL/numpy, ~ms)

```python
# luminance → percentile-contrast-stretched grayscale "height map"
lum   = albedo.convert("L")
lo, hi = np.percentile(lum, (2, 98))
norm  = np.clip((lum - lo) / (hi - lo), 0, 1)
height = Image.fromarray((norm * 255).astype("uint8"))
```
- Guarantees albedo/height always correspond (no model drift), is free, reproducible, and unit-testable (the Styling Demo already proves this pattern with its deterministic recolor tests).
- Optional `height_contrast` and `height_smooth` (1px blur) parameters in the UI; off by default.

### 3.3 LLM for the Blender modeling box (Tier 2) — existing CS1 LLM

- The add-on talks **OpenAI-compatible** to the CS1 LiteLLM proxy (same endpoint/key the Styling Demo uses: model `qwen3-8-27b-int4-dflash2-r2`, secret `litellm-key`).
- **Zero new deployment.** If an artist's workstation cannot reach the internal LiteLLM URL (network policy), the add-on falls back to a **local Ollama** endpoint (`http://localhost:11434`) with any coder model — the config is two fields in the add-on preferences.
- **Upgrade path (not built now):** if bpy code quality is weak, swap the endpoint to **BlenderLLM** (`pixofix/BlenderLLM`, 8B, Qwen2.5-Coder-7B fine-tune on BlendNet) served via vLLM on MLIS — same client code, one URL change. See §10.

---

## 4. Pipeline (Step by Step)

### Step 1 — Reference in (frontend)
- Upload image (PNG/JPG, ≤ 8 MB) **or** pick from the bundled sample library — 3 real reference photos (a speckled ceramic **cup**, a tricolor **merle dog** = fur reference, and **weathered wood grain**). Each sample also maps to a material preset, so clicking it auto-selects the right prompt for a one-click Generate.
- Optional: choose a material preset (just a prompt template) and strength slider (default 0.7).

### Step 2 — Generate (backend, synchronous)
1. Normalize reference (decode, cap 768, align 16) → store under `/data/references/{id}.png`.
2. i2i call → albedo PNG → `/data/generated/{id}/albedo.png` (fallback per §3.1 if model down; response carries `mode`).
3. Derive height map → `/data/generated/{id}/height.png`.
4. Write `meta.json` (id, name, tag, source, prompt, strength, seed, mode, created_at) → the asset is now in the library.
5. Return both map URLs + `mode` + `elapsed_ms`.

### Step 3 — Preview (frontend, Three.js)
- A **per-sample 3D shape** in a small Three.js scene (the shape follows the selected sample/preset — cup→mug, dog→recognizable low-poly quadruped, wood→beveled slab; `buildShapeGeometry` dispatches to `buildCupGeometry`/`buildDogGeometry`/`buildWoodGeometry`):
  - albedo → `MeshStandardMaterial.map`
  - height → `material.bumpMap` (+ `bumpScale` slider) — visible surface relief while the object's silhouette stays clean (true displacement of a high-contrast AI height map would mangle thin shells into wavy ridges, so the PoC uses bump for the relief variant).
- Bundled `three.min.js` (CDN is not assumed on ADM's internal network — the JS file ships in the frontend image).
- "Save to library" happens automatically on generation; the artist can rename/tag.

### Step 4 — Asset library (file-based, no DB)
- `GET /api/assets` → scan `/data/generated/*/meta.json` → list (thumbnail = albedo, name, tag, date).
- `PATCH /api/assets/{id}` → rename/tag (rewrites `meta.json`).
- `GET /api/assets/{id}/download` → albedo + height + meta (zip).
- Search = substring match over name/tag (client-side filter; no index needed at PoC scale).

### Step 5 — Blender integration (artist's machine)

**Tier 1 — Blender-pack (deterministic, always works):**
- `GET /api/assets/{id}/blender-pack` → zip containing:
  - `albedo.png`, `height.png`
  - `scene.py` — template-generated (NOT LLM) Blender script, **version-robust across 4.x and 5.x**: builds the asset's **per-sample 3D shape** from a `SHAPE` param (cup→mug via `bmesh` lathe + parametric half-torus C-handle; dog→recognizable low-poly quadruped — elongated raised torso (legs visible), chest + haunch, neck, head + muzzle + nose, two ears, 4 legs + paws, raised tail — from `create_uvsphere`/`create_cone`; wood→beveled slab), gives `<SHAPE>_Albedo` the albedo in Base Color and `<SHAPE>_Relief` the albedo + height-map **surface relief via a Bump node** (height → `ShaderNodeBump` → Principled `Normal`, identical wiring in 4.x and 5.x — true displacement was rejected because it mangles thin shells), exposes `HEIGHT_SCALE` as a top-of-file constant, saves `<OUT_NAME>.blend` (via `wm.save_as_mainfile`, with a 4.x fallback).
  - `README.md` — 30-second instructions (two ways: add-on "Load pack" button, or `blender --background scene.py`).
- Add-on **"Load Blender Pack"**: artist picks the unzipped folder → add-on runs `scene.py` → a textured object (albedo + height-relief variants) in the viewport, `.blend` saved. **No LLM involved; guaranteed demo path.**

**Tier 2 — LLM modeling box (optional iteration):**
- Same add-on, second panel: prompt box + model config (base URL, model name, API key — stored in add-on preferences).
- Flow (adapted from the proven `mac999/blender-llm-addin` pattern):
  1. System prompt: *"You are an expert Blender Python (bpy) coder for Blender 4.x. Generate only the code, no explanations, in a single ```python block. Never delete existing objects unless asked. Use image texture nodes for textures."*
  2. LLM call (OpenAI-compatible) → extract ```python block → safe-check (blocklist: `os/subprocess/socket/eval/exec/…`, AST parse) → `exec` in Blender.
  3. **Auto-retry ×3** on error: feed the traceback back to the LLM (`"Fix the error {e} in {code}"`) — same loop as the reference add-in.
  4. Artist sees the result in the viewport instantly.
- Example prompts for the live demo: *"add a stone pedestal under the cup"*, *"make the surface relief stronger"*, *"tilt the camera and save a screenshot"*.

**Demo narrative (live):** reference photo on screen → Generate → albedo+height appear → Three.js plane shows the texture with live displacement → click "Download Blender-pack" → cut to artist's Blender → Load pack → textured plane + relief in real Blender → one LLM prompt ("add a pedestal") → it happens. **This is the exact 3-stage flow from Arya's slide, end to end.**

---

## 5. API Contracts

### `POST /api/generate` (multipart)
| Field | Type | Notes |
|---|---|---|
| `reference` | file | PNG/JPG ≤ 8 MB |
| `name` | str | asset name (default: `texture-{timestamp}`) |
| `tag` | str | optional tag (e.g. `marble`, `bust`) |
| `prompt` | str | optional; default = material preset or the §3.1 albedo prompt |
| `strength` | float 0.3–0.95 | default 0.7 |
| `seed` | int? | optional |
| `shape` | str? | `cup` / `dog` / `wood` — the 3D object the pack builds (default `cup`; the frontend sends the selected sample's shape) |

**Response (200, synchronous):**
```json
{
  "id": "9f2c1ab3",
  "mode": "model",                       // "model" | "fallback-reference"
  "albedo_url": "/api/assets/9f2c1ab3/maps/albedo.png",
  "height_url": "/api/assets/9f2c1ab3/maps/height.png",
  "prompt": "…", "strength": 0.7,
  "shape": "cup",                        // "cup" | "dog" | "wood" (the pack's 3D object)
  "elapsed_ms": 41250
}
```

### `GET /api/assets`
```json
{ "assets": [
  { "id": "9f2c1ab3", "name": "marble-01", "tag": "marble",
    "thumb": "/api/assets/9f2c1ab3/maps/albedo.png",
    "mode": "model", "created": "2026-10-01T10:22:00Z" }
] }
```

### `PATCH /api/assets/{id}`
```json
{ "name": "marble-01-final", "tag": "marble" }
```

### `GET /api/assets/{id}/maps/{albedo|height}.png` → image
### `GET /api/assets/{id}/blender-pack` → `application/zip` (albedo, height, `scene.py`, `README.md`)
### `GET /api/samples` → `[{ "id": "sample_cup", "name": "Cup (sample)", "url": "/api/samples/sample_cup.png", "shape": "cup" }, { "id": "sample_dog", "name": "Dog (sample)", "url": "/api/samples/sample_dog.jpg", "shape": "dog" }, { "id": "sample_wood", "name": "Wood (sample)", "url": "/api/samples/sample_wood.jpg", "shape": "wood" }]` (serves mixed PNG/JPG via `ALLOWED_EXT`; `shape` is the 3D object that sample generates — cup/dog/wood)
### `GET /api/health` → `{ "ok": true, "image_model": "ready|down", "version": "<image tag>" }`
(`version` is the deployed image tag, injected via the `APP_VERSION` env var so it never drifts — e.g. `v3`.)
(Frontend uses `/api/health` to show a "model warming up" banner — mirrors the Styling Demo's fallback UX.)

---

## 6. Frontend Design (static SPA, served by the backend pod)

Single `index.html` + `app.js` + bundled `three.min.js`. No build step, no framework.

- **Layout (3 columns):**
  1. *Reference*: upload dropzone + sample thumbnails + material preset select + strength slider
  2. *Generate & result*: Generate button (spinner, "can take ~1 min"), albedo + height thumbnails, `mode` badge (model vs fallback)
  3. *3D preview*: Three.js canvas (plane + displacement, orbit controls, displacement-scale slider) + buttons: **Download maps**, **Download Blender-pack**, **Save/tag**
- **Library tab:** grid of assets (thumb, name, tag, date), search box, rename/tag inline, per-asset download + Blender-pack buttons.
- No auth; internal host only. All fetch handlers parse JSON defensively (the Styling Demo learned this — `parseJSON()` on every handler).

---

## 7. Deployment (Helm, CS1)

### 7.1 Repo layout

```
pcai-3d-viz-demo/
├── helm-chart/
│   ├── Chart.yaml
│   ├── values.yaml
│   └── templates/
│       ├── namespace.yaml            # viz-demo
│       ├── backend-deployment.yaml   # image + PVC mount + env
│       ├── backend-service.yaml
│       ├── pvc.yaml                  # viz-assets, 10Gi, NFS
│       └── virtualservice.yaml       # istio-system/ezaf-gateway
├── backend/
│   ├── main.py                       # FastAPI app (single file, ~500 lines)
│   ├── texture.py                    # i2i client + height derivation
│   ├── blender_pack.py               # scene.py template + zip assembly
│   ├── templates/scene.py.template
│   ├── requirements.txt              # fastapi uvicorn httpx pillow numpy
│   └── Dockerfile
├── frontend/
│   ├── index.html
│   ├── app.js
│   └── vendor/three.min.js           # bundled — no CDN
├── blender-addon/
│   ├── viz_demo_addon.py             # Load-pack operator + LLM box (adapted from mac999 pattern)
│   └── README.md                     # install: Edit > Preferences > Add-ons > Install
├── sample_data/                      # 3 real reference photos (cup / dog / wood) — see README
├── scripts/
│   ├── build-deploy.sh               # docker build+push to registry.ctc.sg.lab:5000 (same pattern as Styling Demo)
│   └── test_e2e.py                   # end-to-end via port-forward
└── README.md
```

### 7.2 `values.yaml` (key sections)

```yaml
namespace: viz-demo
registry: registry.ctc.sg.lab:5000

backend:
  image: registry.ctc.sg.lab:5000/viz-demo-backend:v1
  replicas: 1
  resources:
    requests: { cpu: "500m", memory: "2Gi" }
    limits:   { cpu: "2",    memory: "4Gi" }

pvc:
  name: viz-assets
  size: 10Gi
  storageClass: ""            # NFS-backed default, same as styling-results

# Existing MLIS i2i predictor (no new model)
imageGen:
  url: "http://pcai-qwen-image-edit-gguf-predictor.project-user-aieadmin.svc.cluster.local:80"
  keySecret: { name: "mlis-key", key: "key" }     # created in viz-demo (see 7.4)

# Existing CS1 LLM (Tier 2 add-on config is client-side; backend does not call it)
# (documented for the artist in blender-addon/README.md)

ingress:
  enabled: true
  host: "viz-demo.aie.cs1.ctc.sg.lab"
  gateway: "istio-system/ezaf-gateway"
```

### 7.3 Deploy procedure (identical to Styling Demo pipeline)
1. `TAG=v1 bash scripts/build-deploy.sh` (one command: bastion sync → docker build+push to registry → secrets → deploy → wait)
2. Sub-steps also available individually: `secrets`, `image`, `deploy`, `verify`
3. E2E: `python scripts/test_e2e.py --base https://viz-demo.aie.cs1.ctc.sg.lab` (add `-k` / CA for the internal cert)
4. Public check: `curl -sk https://viz-demo.aie.cs1.ctc.sg.lab/api/health`

### 7.4 One-time manual step (secret)
```bash
# copy the MLIS token into the new namespace (same value as styling-demo)
TOKEN=*** get secret -n styling-demo mlis-key -o jsonpath='{.data.key}' | base64 -d)
kubectl -n viz-demo create secret generic mlis-key --from-literal=key="$TOKEN"
```
(No secrets in values.yaml; the chart only references by name. `scripts/build-deploy.sh secrets` does this automatically.)

### 7.5 Verified deployment status (2026-10-04)

Deployed and verified end-to-end on CS1 (image `v10` — **per-sample 3D geometry**: the left sample library is the 3 real reference photos (speckled cup / merle dog / weathered wood), and each now produces its OWN distinct 3D shape — cup→mug, dog→recognizable low-poly quadruped (raised torso with visible legs, neck, head + muzzle + nose, two ears, 4 legs, raised tail), wood→beveled slab — in BOTH the Three.js preview and the Blender pack):

| Item | State |
|---|---|
| Image | `registry.ctc.sg.lab:5000/pcai-viz-app:v10`. **Per-sample 3D geometry**: each sample produces its own shape — cup→mug (hollow body + C-handle + rim/base), dog→recognizable low-poly quadruped (elongated raised torso so the legs show, chest + haunch, neck, head + muzzle + nose, two ears, 4 legs + paws, raised tail), wood→beveled slab — in both the Three.js preview and the Blender pack (`scene.py.template` builds the shape from a `SHAPE` param), with height relief via a **Bump node** (not true displacement, which mangles the thin shell). The left sample library holds 3 real reference photos. Fidelity = "same object + same surface" (procedural, no new AI model — user-confirmed). **v10 redesign:** the v8 dog read as a green ball-with-legs (fat torso buried the legs, no neck, tiny head bump); it was rebuilt for a clear quadruped silhouette and verified by headless render + numerical bbox. |
| Workload | `deploy/viz-demo-app` in ns `viz-demo` — 2/2 Running (app + istio-proxy) |
| PVC | `viz-results` 10Gi NFS, Bound; assets persist across restarts |
| Ingress | `https://viz-demo.aie.cs1.ctc.sg.lab` → 200, `/api/health` `{"ok":true,"image_model":"ready","version":"v10"}` (internal cert, use `-k`) |
| Generate | `POST /api/generate` → **`mode:"model"`** (real MLIS Qwen-Image-Edit i2i on GPU), ~30 s, 512×512 albedo (RGB) + height (L), persisted to PVC, visible in `/api/assets` |
| Blender pack | `GET /api/assets/{id}/blender-pack` → zip (albedo.png, height.png, scene.py, README.md); `scene.py` is **version-robust (4.x + 5.x)** and compiles |
| Blender add-on | Verified in **real Blender 5.2.2 LTS (headless)**: install/enable, Tier 1 load-pack (real cluster pack → saved `.blend` + displacement wired), reopen, Tier 2 LLM box (urllib + safe-check + retry + exec), idempotent unregister — **0 errors**. |
| E2E | `scripts/test_e2e.py` **56/56 against live v10** (28 legacy + 13 visual + 15 export-open). Includes a **visual-inspection step [8]**: for each sample it generates, downloads the Blender pack, builds the `.blend`, renders it **headless** (Blender 5.2 + `scripts/render_blend.py`), and confirms the rendered object's **surface colour-composition matches the ORIGINAL sample photo** (brightness-invariant luminance + chromaticity histogram similarity, ≥ 0.50; real scores cup 0.920 / dog 0.937 / wood 0.972) + an object is present (fg ≥ 0.10). **Step [9] export-open validation** (`scripts/validate_blend_import.py`): the exported `.blend` opens via File>Open, holds the 2 mesh variants + UVs + resolved textures, round-trips, and imports correctly with the source PNGs removed (proves textures are packed — portable to an artist's machine). Run: `.venv/Scripts/python scripts/test_e2e.py --base https://viz-demo.aie.cs1.ctc.sg.lab --blender "C:/Program Files/Blender Foundation/Blender 5.2/blender.exe"`. |

**Blender 5.x compatibility (verified 2026-10-02):** the add-on was originally written
for Blender 4.x; the artist machine now runs **Blender 5.2**. Running the real add-on
headless in 5.2 exposed and fixed three 4.x-only breakages in the generated
`scene.py` (now version-robust, guarded by `hasattr`):
1. `material.displacement_scale` was removed in 5.x → 5.x uses
   `material.max_vertex_displacement` (the template sets whichever attribute exists).
2. The Principled BSDF lost its `Displacement` input in 5.x → 5.x wires a
   `ShaderNodeDisplacement` node to the **Material Output** node's `Displacement`
   input (the template branches on version).
3. `bpy.ops.file.save_as_mainfile` was removed in 5.x → the template uses the
   canonical `bpy.ops.wm.save_as_mainfile` (exists in both 4.x and 5.x) with a
   4.x fallback.
The add-on's `register()`/`unregister()` are now idempotent so a double-unregister
(manual + Blender's auto-disable on exit) cannot raise.

**Pitfall hit (fix in `manifests/app.yaml` + `helm-chart/templates/app-deploy.yaml`):**
the NFS volume arrives `root:root`, and a root initContainer to chown it is **blocked**
— Kyverno enforces pod-level `runAsNonRoot: true, runAsUser: 1000` on the platform,
so `runAsUser: 0` on an init container fails with `container's runAsUser breaks
non-root policy`. The working fix is `fsGroup: 1000` on the pod
securityContext: kubelet chowns the mount to gid 1000 with the setgid bit
(`drwxrwsr-x root app`), which the non-root app can then write to. Do **not**
re-introduce a root init container for this.

**Image-tag caveat (hit 2026-10-02):** re-pushing a *same* tag (`v2`) does not re-pull
on the node because `imagePullPolicy: IfNotPresent` — the node serves the stale cached
image. Always **bump the tag** (`v3`, …) for a clean rollout after a code change.

Note: the deployment is named `viz-demo-app` (single pod serves backend + static
frontend), not `viz-backend` as in the earlier draft of this document.

---

## 8. Execution Plan

| Phase | Work | Est. |
|---|---|---|
| **1. Backend core** | FastAPI skeleton; i2i client (copy proven path from Styling Demo, incl. `sd_cpp_extra_args`); height derivation + unit test; asset store (dir-scan) + API endpoints; `/api/health` with model readiness probe | 0.5–1 d |
| **2. Blender pack** | `scene.py.template` (test headless if a local Blender is available; otherwise verify syntax + API against Blender 4.x docs); zip assembly endpoint; README | 0.5 d |
| **3. Add-on** | Adapt `blender-llm-addin`: Load-pack operator, LLM box (base_url/model/key prefs), safe-exec + retry, Blender 4.x compatibility pass | 0.5–1 d |
| **4. Frontend** | SPA: reference panel, generate + result, Three.js preview, library tab; bundle three.min.js | 1 d |
| **5. Deploy + E2E** | Helm chart, secret, image build/push, deploy to `viz-demo`, `test_e2e.py` (generate from sample → assert maps + zip + library entry; fallback path when model down), public URL check, **strength/prompt tuning** with real references (marble, bust) | 0.5–1 d |
| **6. Demo rehearsal** | Run the §4 narrative end-to-end on the artist machine (Blender 4.x + add-on + CS1 LLM reachable), capture screenshots for the follow-up email | 0.5 d |

**E2E test outline (`scripts/test_e2e.py` — 56 checks on v10):**
1. `GET /api/health` → 200
2. `POST /api/generate` (bundled sample) → 200, `albedo_url`/`height_url` exist, `mode ∈ {model, fallback-reference}`, `shape` present
3. Height determinism: regenerate with same seed+strength → albedo may differ (model), but re-deriving height from a stored albedo is bit-identical (pure function test)
4. `GET /api/assets` contains the new id; `PATCH` rename works
5. `GET /api/assets/{id}/blender-pack` → valid zip with exactly 4 entries; `scene.py` passes `python -m py_compile`
6. `GET /api/samples` → 3 samples (cup/dog/wood), each carrying its `shape`
7. Fallback path (second instance, dead `IMG_ENDPOINT`) → `mode=fallback-reference`, albedo == reference (pixels), pack still assembles
8. **Visual inspection** (needs Blender): for each sample — generate → download pack → build `.blend` (`blender --background --python scene.py`) → render headless (`scripts/render_blend.py`) → assert the rendered object is present (fg ≥ 0.10) and its **surface colour-composition matches the original sample photo** (brightness-invariant luminance + chromaticity histogram similarity ≥ 0.50) and its AI albedo. Runs in fallback mode too (albedo==sample there). `--skip-visual` / `--blender` flags.
9. **Export .blend open/import** (needs Blender): for each sample's built `.blend` — run `scripts/validate_blend_import.py` in a separate Blender process: open via `bpy.ops.wm.open_mainfile` (the real File>Open operator), assert 2 mesh variants + UVs + all textures resolve, round-trip (save→re-open), and a **moved-to-another-machine** test (source PNGs removed — passes only because `scene.py` packs the textures into the .blend; an external file reference would break on an artist's machine).

---

## 9. Success Criteria

| # | Criterion | Target |
|---|---|---|
| S1 | Deploys via Helm to `viz-demo` on CS1; public host `viz-demo.aie.cs1.ctc.sg.lab` serves the SPA + `/api/health` | ✅/❌ at deploy |
| S2 | Generate works with the existing i2i predictor: one reference → albedo + height in < 2 min, `mode=model` | measured `elapsed_ms` |
| S3 | Fallback: with the predictor cold/down, pipeline still completes (`mode=fallback-reference`) and Blender-pack is still produced | tested in E2E |
| S4 | Three.js preview renders albedo + displacement live in the browser (internal network, no CDN) | visual check |
| S5 | Asset library: ≥ 3 assets browseable, rename/tag persists across pod restart (NFS PVC) | E2E + restart test |
| S6 | Blender-pack: unzipped on an artist workstation, `scene.py` runs in Blender 4.x / 5.x → the per-sample shape (cup/dog/wood) with height relief, `.blend` saved | visual check + E2E [8] |
| S7 | LLM box: ≥ 2 of 3 simple prompts (e.g. "add a pedestal") execute correctly in Blender with ≤ 2 retries | live demo |
| S8 | Zero new GPU/model deployments — only existing MLIS predictor + existing CS1 LLM consumed | `kubectl` inventory |
| S9 | **Visual inspection**: each sample's Blender scene renders headless and its surface colour-composition matches the original sample photo (≥ 0.50) with an object present | E2E [8], 56/56 on v10 |
| S10 | **Export .blend open/import**: the exported `.blend` opens via File>Open, holds the 2 mesh variants + UVs + resolved textures, round-trips, and imports correctly with the source PNGs removed (textures packed → portable to an artist's machine) | E2E [9], 56/56 on v10 |

---

## 10. Beyond the PoC (reference points, not built now)

1. **BlenderLLM upgrade (Tier 2 LLM):** if generic-LLM bpy quality is weak, serve `pixofix/BlenderLLM` (8B, Qwen2.5-Coder-7B fine-tuned on BlendNet, Apache-2.0, fits one A100) via vLLM on MLIS — the add-on client code is unchanged (one `base_url` swap). Ollama import (`FROM hf.co/pixofix/BlenderLLM`) is the local-workstation alternative.
2. **LL3M (threedle) as north star:** the research ceiling — 6 agents + BlenderRAG + VLM critic loop. Its *minimal* reduction (1 coding LLM + small RAG over Blender material/texture-node docs + 1 VLM render-check) is essentially Tier 2 done thoroughly. Relevant only if ADM wants a funded Phase 2.
3. **Server-side headless Blender:** rejected for the PoC (container image + render time for output artists must open in desktop Blender anyway); revisit only if "no Blender installed" becomes a requirement.
4. **Normal maps / tiling / UV unwrap:** out of scope; the deterministic height-map can later be upgraded to a Sobel-derived normal map in pure Python if the team wants relief lighting in Blender.

---

## Appendix A — `scene.py` template (Blender-pack, Tier 1)

The authoritative template lives in the repo at **`backend/templates/scene.py.template`**
(kept as the single source of truth so the doc never drifts). It is a **version-robust
Blender 4.x / 5.x** script. Key behaviour:

- Two textured planes (`Texture_A` albedo-only, `Relief_B` albedo + height displacement),
  mirroring Stage 3 of the slide.
- Displacement is set **per-version** (guarded by `hasattr`):
  - **4.x:** `material.displacement_scale` + Principled BSDF `Displacement` input.
  - **5.x:** `material.max_vertex_displacement` + a `ShaderNodeDisplacement` node wired
    to the **Material Output** node's `Displacement` input (Principled lost its
    `Displacement` input in 5.x).
- Saves the `.blend` via `bpy.ops.wm.save_as_mainfile` (present in both 4.x and 5.x),
  with a `bpy.ops.file.save_as_mainfile` fallback for older builds.

`HEIGHT_SCALE` / `PLANE_SIZE` / `SUBDIV` / `OUT_NAME` are top-of-file constants the
artist can tune. The template is rendered per-asset by `backend/blender_pack.py`
(`__PLANE_SIZE__` / `__SUBDIV__` / `__HEIGHT_SCALE__` / `__OUT_NAME__` placeholders).

## Appendix B — Add-on: LLM box core (adapted from `mac999/blender-llm-addin`)

```python
# Preferences: base_url, api_key, model
#   default base_url = http://<cs1-litellm-host>:4001/v1  (CS1, internal)
#   fallback        = http://localhost:11434/v1           (local Ollama)
import openai, re

def llm_call(prompt, prefs):
    client = openai.OpenAI(base_url=prefs.base_url, api_key=prefs.api_key)
    r = client.chat.completions.create(
        model=prefs.model, temperature=0.1, max_tokens=1024,
        messages=[{"role": "system",
                   "content": "You are an expert Blender Python (bpy) coder for "
                              "Blender 4.x. Output ONLY code in a single ```python "
                              "block. Never delete existing objects unless asked."},
                  {"role": "user", "content": prompt}])
    return r.choices[0].message.content

def run_with_retry(prompt, prefs, attempts=3):
    out = llm_call(prompt, prefs)
    for i in range(attempts):
        m = re.search(r"```python\n(.*?)```", out, re.DOTALL)
        code = (m.group(1) if m else out).strip()
        try:
            exec(compile(code, "<viz-demo>", "exec"))   # + blocklist check as in reference add-in
            return True
        except Exception as e:
            out = llm_call(f"Fix the error {e} in\n{code}", prefs)
    return False
```

## Appendix C — MLIS i2i call (proven shape, from Styling Demo `main.py::_img_generate`)

```python
form = {
  "prompt": f"{prompt}<sd_cpp_extra_args>{json.dumps({'strength': 0.7, 'sample_steps': 8})}</sd_cpp_extra_args>",
  "size": f"{width}x{height}",          # both divisible by 16, max side 768
  "num_inference_steps": "8",
  "strength": "0.7",
}
files = {"image": ("ref.png", png_bytes, "image/png")}
r = httpx.post(f"{IMG_ENDPOINT}/v1/images/edits",
               headers={"Authorization": f"Bearer {MLIS_KEY}"},
               data=form, files=files, timeout=300)
albedo_b64 = r.json()["data"][0]["b64_json"]
```

---

**End of Document**
