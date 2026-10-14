"""PCAI 3D Viz PoC — backend (phases 1+2 of docs/TECHNICAL_DESIGN.md).

FastAPI app, single file. Synchronous generate (no queue), file-based asset
store (no DB), static frontend served from ../frontend (lazy mount — the
app starts fine while frontend/ doesn't exist yet).

Env:
  IMG_ENDPOINT  MLIS i2i predictor URL (optional; unset -> fallback mode)
  MLIS_KEY      Bearer key for the predictor
  DATA_DIR      asset store root (default: <repo>/data)
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image

import blender_pack
import texture
import geometry
import real3d

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("viz-demo")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(REPO_ROOT, "data"))
REFS_DIR = os.path.join(DATA_DIR, "references")
GEN_DIR = os.path.join(DATA_DIR, "generated")
SAMPLES_DIR = os.path.join(REPO_ROOT, "sample_data")
FRONTEND_DIR = os.path.join(REPO_ROOT, "frontend")

MAX_UPLOAD = 8 * 1024 * 1024
ALLOWED_EXT = {".png", ".jpg", ".jpeg"}

app = FastAPI(title="PCAI 3D Viz PoC", version="v1")


def _prewarm_geometry() -> None:
    """Load the CPU geometry models (Depth-Anything V2 Small + rembg U2NetP)
    in the BACKGROUND so a `do_geometry=true` request doesn't stall on the
    ~30s first-load. Called from the startup hook as a daemon thread — it
    must never block app startup.

    Why this matters: `generate()` is a *sync* endpoint (runs in FastAPI's
    threadpool, not the event loop), so a long geometry build no longer
    starves /api/health — but prewarming keeps first-use latency short too.
    If the models can't load (offline cache missing, OOM) the first
    do_geometry request surfaces the real error via build_mesh's exception
    path; prewarming failures are logged, not fatal.
    """
    try:
        import geometry
        geometry._load_depth()
        geometry._load_rembg()
        log.info("geometry models prewarmed (Depth-Anything V2 Small + rembg U2NetP)")
    except Exception as e:  # noqa: BLE001 — best-effort, non-fatal
        log.warning("geometry prewarm failed (first do_geometry will retry load): %r", e)


@app.on_event("startup")
def _startup() -> None:
    import threading
    threading.Thread(target=_prewarm_geometry, daemon=True).start()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _asset_dir(asset_id: str) -> str:
    return os.path.join(GEN_DIR, asset_id)


def _load_meta(asset_id: str) -> Optional[dict]:
    p = os.path.join(_asset_dir(asset_id), "meta.json")
    if not os.path.isfile(p):
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_meta(meta: dict) -> None:
    os.makedirs(_asset_dir(meta["id"]), exist_ok=True)
    with open(os.path.join(_asset_dir(meta["id"]), "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)


def _check_image(img: Image.Image) -> bytes:
    """Re-encode an uploaded image as RGB PNG (normalizes JPG, verifies it)."""
    img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Health — cheap model readiness probe (3s), never blocks long
# ---------------------------------------------------------------------------
@app.get("/api/health")
def health():
    return {
        "ok": True,
        "version": os.environ.get("APP_VERSION", "v14"),
        "image_model": "ready" if texture.model_ready() else "down",
        # Real 3D (Hunyuan3D-2.1) is optional: "ready" when the predictor is
        # answering healthy (do_geometry=true uses it first), "unavailable"
        # when down/disabled (do_geometry=true auto-falls back to the CPU
        # 2.5D relief instead).
        "real3d": "ready" if real3d.available() else "unavailable",
    }


@app.get("/healthz")
def healthz():
    """Trivial liveness endpoint: no I/O, no model checks, no CPU.

    The heavy do_geometry job (CPU mesh build, ~40s under the pod's 1-vCPU
    limit) can starve /api/health — whose model_ready() does a network ping
    to the MLIS predictor — past the liveness timeout, and kubelet would then
    restart the pod MID-JOB. Pointing the liveness probe here makes the pod
    unrestartable while it's alive: a hung job shows up as a slow response,
    never a crash/restart loop. Readiness (/api/health) is unaffected.
    """
    return {"ok": True}


# ---------------------------------------------------------------------------
# Ingest helpers (brief Stage 1: URL / upload / local path)
# ---------------------------------------------------------------------------
def _fetch_reference(url: str) -> tuple[bytes, str]:
    """Accept an image URL (Google Images / Pinterest, etc.). Returns
    (raw_bytes, source_label). http/https only, 30s budget."""
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "reference_url must be an http(s) URL")
    try:
        r = httpx.get(
            url,
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (PCAI 3D Viz PoC)"},
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"could not fetch reference_url: {type(e).__name__}: {e}")
    if r.status_code != 200:
        raise HTTPException(400, f"reference_url returned HTTP {r.status_code}")
    if len(r.content) > MAX_UPLOAD:
        raise HTTPException(400, f"reference exceeds {MAX_UPLOAD // (1024*1024)} MB")
    return r.content, f"url:{url}"


def _asset_response(asset_id: str, meta: dict, reused: bool = False,
                    elapsed_ms: Optional[int] = None) -> dict:
    """Uniform generate() response for a stored asset (fresh or reused)."""
    resp = {
        "id": asset_id,
        "mode": meta.get("mode", ""),
        "albedo_url": f"/api/assets/{asset_id}/maps/albedo.png",
        "height_url": f"/api/assets/{asset_id}/maps/height.png",
        "normal_url": f"/api/assets/{asset_id}/maps/normal.png",
        "roughness_url": f"/api/assets/{asset_id}/maps/roughness.png",
        "metallic_url": f"/api/assets/{asset_id}/maps/metallic.png",
        "prompt": meta.get("prompt", ""),
        "strength": meta.get("strength"),
        "shape": meta.get("shape", blender_pack.DEFAULT_SHAPE),
        "gates": meta.get("gates", {}),
        "reused": reused,
        "has_model": bool(meta.get("geometry") and not meta.get("geometry", {}).get("error")),
    }
    if meta.get("geometry"):
        geom = meta["geometry"]
        # v14: a real-3D GLB (Hunyuan3D-2.1) supersedes the 2.5D relief OBJ.
        model_type = geom.get("type", "obj")
        resp["model_type"] = model_type
        if model_type == "glb" and not geom.get("error"):
            resp["model_url"] = f"/api/assets/{asset_id}/model.glb"
        elif not geom.get("error"):
            resp["model_url"] = f"/api/assets/{asset_id}/model.obj"
        resp["geometry_mode"] = geom.get("mode")
        resp["geometry_verts"] = geom.get("verts")
    if elapsed_ms is not None:
        resp["elapsed_ms"] = elapsed_ms
    return resp


# ---------------------------------------------------------------------------
# Stage 2 — PBR map synthesis + quality gates (brief AC1/AC2/AC3/AC6)
# ---------------------------------------------------------------------------
def _synth_maps(albedo_bytes: bytes) -> dict:
    """Derive the full PBR set from the albedo and run the quality gates.

    Returns {kind: png_bytes, ..., 'gates': {...}}.

    Gate semantics (brief AC2/AC3/AC6):
      - `normal` (AC2) and `albedo_delit` are DETERMINISTIC and re-runnable,
        so they define `gates['passed']` and drive the retry loop (AC6).
      - `tile_*` (AC3) is best-effort: a one-off object photo (dog/cup/wood)
        cannot be made truly seamless without a seam-inpaint model (out of
        scope for the CPU-only PoC). We apply the crossfade only when it
        IMPROVES the map (never degrade a non-periodic photo), and report the
        per-map ratio without failing the asset.
    """
    maps = {
        "albedo": albedo_bytes,
        "height": texture.derive_height_map(albedo_bytes),
        "normal": texture.derive_normal_map(albedo_bytes),
        "roughness": texture.derive_roughness_map(albedo_bytes),
    }
    sz = Image.open(io.BytesIO(albedo_bytes)).size
    maps["metallic"] = texture.derive_metallic_map(0.0, (sz[0], sz[1]))

    gates: dict = {
        "normal": texture.gate_normal(maps["normal"]),
        "albedo_delit": texture.gate_albedo_delit(maps["albedo"]),
    }
    for kind in ("albedo", "height", "normal", "roughness"):
        g = texture.gate_tileable(maps[kind])
        before = g["wrap_edge_grad_ratio"]
        if not g["ok"]:
            cand = texture.make_tileable(maps[kind])
            after = texture.gate_tileable(cand)
            # Keep the tiled version only if it actually improves (lower ratio).
            if after["wrap_edge_grad_ratio"] < before:
                maps[kind] = cand
                gates[f"tile_{kind}"] = {**g, "tiled": True, "after": after}
            else:
                gates[f"tile_{kind}"] = {**g, "tiled": False,
                                         "note": "crossfade degraded a non-periodic photo; kept original"}
        else:
            gates[f"tile_{kind}"] = {**g, "tiled": False}
    # Deterministic, re-runnable gates define pass/fail (AC2 + delit check).
    gates["passed"] = gates["normal"]["ok"] and gates["albedo_delit"]["ok"]
    maps["gates"] = gates
    return maps


def _gates_hard_fail(gates: dict) -> list:
    """Gates that a *re-run of the generator* could fix (AC6 retry loop)."""
    fails = []
    if not gates.get("normal", {}).get("ok"):
        fails.append("normal")
    if not gates.get("albedo_delit", {}).get("ok"):
        fails.append("albedo_delit")
    return fails


# ---------------------------------------------------------------------------
# Generate (synchronous) — Stage 1 ingest -> Stage 2 AI + PBR -> asset store
# ---------------------------------------------------------------------------
MAX_GATE_RETRIES = 2  # brief AC6: re-run generation at most N times on gate fail


@app.post("/api/generate")
def generate(
    reference: Optional[UploadFile] = File(None),
    reference_url: Optional[str] = Form(None),
    reference_path: Optional[str] = Form(None),
    name: Optional[str] = Form(None),
    tag: Optional[str] = Form(None),
    prompt: Optional[str] = Form(None),
    strength: float = Form(0.7),
    seed: Optional[int] = Form(None),
    shape: Optional[str] = Form(None),
    do_geometry: bool = Form(False),
):
    t0 = time.monotonic()

    if not (0.3 <= strength <= 0.95):
        raise HTTPException(400, "strength must be in 0.3..0.95")
    prompt = (prompt or "").strip() or texture.DEFAULT_PROMPT

    # ---- Stage 1: ingest (exactly one source: upload / URL / local path)
    if reference is not None:
        raw = reference.file.read()
        if len(raw) > MAX_UPLOAD:
            raise HTTPException(400, f"reference exceeds {MAX_UPLOAD // (1024*1024)} MB")
        ext = os.path.splitext(reference.filename or "upload.png")[1].lower()
        if ext and ext not in ALLOWED_EXT:
            raise HTTPException(400, f"unsupported file type '{ext}' (PNG/JPG only)")
        source = f"upload:{os.path.basename(reference.filename or 'upload.png')}"
    elif reference_url:
        raw, source = _fetch_reference(reference_url.strip())
    elif reference_path:
        p = os.path.normpath(reference_path.strip())
        if not os.path.isfile(p):
            raise HTTPException(400, f"reference_path not found: {p}")
        ext = os.path.splitext(p)[1].lower()
        if ext not in ALLOWED_EXT:
            raise HTTPException(400, f"unsupported file type '{ext}' (PNG/JPG only)")
        with open(p, "rb") as f:
            raw = f.read()
        if len(raw) > MAX_UPLOAD:
            raise HTTPException(400, f"reference exceeds {MAX_UPLOAD // (1024*1024)} MB")
        source = f"path:{os.path.basename(p)}"
    else:
        raise HTTPException(
            400, "provide one of: reference (file upload), reference_url, or reference_path")

    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:
        raise HTTPException(400, "reference is not a valid image")

    # Brief Stage 1: validate (>=512px), then delight (remove photographic
    # lighting) so the albedo is flat.
    ok, why = texture.validate_reference(img)
    if not ok:
        raise HTTPException(400, why)
    img_orig = img.convert("RGB")           # original photo (for geometry surface)
    ref_png = _check_image(texture.delight(img))

    # ---- Idempotency (brief cross-cutting): content-hash asset ID. The same
    # photo always maps to the same asset; a re-generate returns the existing
    # asset without re-running the pipeline.
    content_hash = hashlib.sha256(ref_png).hexdigest()[:16]
    asset_id = f"t{content_hash}"
    existing = _load_meta(asset_id)
    if existing and os.path.isdir(_asset_dir(asset_id)):
        geom = existing.get("geometry")
        geom_ok = bool(geom) and not geom.get("error")
        # Self-healing: an asset whose mesh was built with the OLD spiky
        # pipeline (pre-smoothing, z_scale 0.55) is rebuilt on the next
        # do_geometry call so existing sample assets pick up the fixed mesh
        # without re-running the whole 5-stage pipeline. A real-3D GLB asset
        # (type == "glb", Hunyuan3D-2.1) is NOT stale — its mesh is a true
        # textured mesh, not a z_scale relief — so it is never "re-healed".
        #
        # "Stale" = any NON-GLB geometry. The 2.5D relief (type=obj) is only a
        # degraded fallback for when the Hunyuan3D-2.1 predictor is unavailable;
        # it was explicitly rejected as "not a 3D object conversion". So when
        # the user checks "Build 3D from the image" (do_geometry=true) and real
        # 3D is available, a 2.5D-relief asset must be UPGRADED to a real GLB.
        # (A real-3D GLB, type=glb, is never stale — it IS the target.) The old
        # spiky mesh (z_scale != 0.35) is also non-GLB, so it is covered too.
        geom_stale = bool(geom) and geom.get("type") != "glb"
        if do_geometry and (not geom_ok or geom_stale):
            # Bug fix: reusing do_geometry=true for an existing 2D-only asset
            # silently returned the old asset without ever building the 3D
            # model (the user uploads a photo, then toggles "Build 3D from the
            # image" and gets the unchanged 2D asset). Backfill: build the
            # image-derived mesh from the stored albedo + this reference photo,
            # save model.obj + model_albedo.png, and update the metadata.
            # v14.1 backfill fix: when a *reused* asset is re-generate'd with
            # do_geometry=true, try REAL 3D (Hunyuan3D-2.1) first — the same as
            # the fresh-asset path — before falling back to the 2.5D relief.
            # (Before this, a pre-seeded sample like the cup would be checked
            # for "Build 3D" and silently return the stale 2.5D OBJ.) The
            # stored reference image (REFS_DIR/{id}.png) matches the original
            # photo's composition, so it is the right input for real3d.
            real_glb = None
            t_g = time.monotonic()
            if real3d.available():
                try:
                    # Use img_orig (the original upload) — the same input the
                    # fresh-asset path uses for real3d, so a reused asset gets
                    # the identical real-3D result. (The stored reference is
                    # the delighted version, which differs slightly.)
                    glb = real3d.generate_glb(img_orig, seed=seed)
                    with open(os.path.join(_asset_dir(asset_id), "model.glb"), "wb") as f:
                        f.write(glb)
                    real_glb = glb
                    log.info("generate id=%s REAL 3D backfilled (reused asset) %d bytes (%ds)",
                             asset_id, len(glb), int((time.monotonic() - t_g) * 1000))
                except Exception as e:  # noqa: BLE001 — real 3D is best-effort
                    log.warning("REAL 3D backfill failed for %s: %r — falling back to 2.5D relief",
                                asset_id, e)
            if real_glb is None:
                # Fallback: CPU 2.5D relief from the stored albedo + reference.
                t_g = time.monotonic()
                try:
                    with open(os.path.join(_asset_dir(asset_id), "albedo.png"), "rb") as f:
                        aimg = Image.open(io.BytesIO(f.read())).convert("RGB")
                    verts, uvs, obj_bytes, mesh_albedo, geom_mode = geometry.build_mesh(
                        img_orig, aimg, size=96, z_scale=0.35)
                    with open(os.path.join(_asset_dir(asset_id), "model.obj"), "wb") as f:
                        f.write(obj_bytes)
                    with open(os.path.join(_asset_dir(asset_id), "model_albedo.png"), "wb") as f:
                        f.write(mesh_albedo)
                    existing["geometry"] = {
                        "mode": geom_mode,
                        "type": "obj",
                        "verts": int(len(verts)),
                        "size": 96,
                        "z_scale": 0.35,
                        "build_ms": int((time.monotonic() - t_g) * 1000),
                    }
                except Exception as e:  # noqa: BLE001 — best-effort
                    log.warning("geometry backfill failed for %s: %r", asset_id, e)
                _save_meta(existing)
            else:
                existing["geometry"] = {
                    "mode": "hunyuan3d-2.1",
                    "type": "glb",
                    "size": len(real_glb),
                    "build_ms": int((time.monotonic() - t_g) * 1000),
                }
                _save_meta(existing)
        return _asset_response(asset_id, existing, reused=True)

    os.makedirs(REFS_DIR, exist_ok=True)
    os.makedirs(_asset_dir(asset_id), exist_ok=True)
    with open(os.path.join(REFS_DIR, f"{asset_id}.png"), "wb") as f:
        f.write(ref_png)

    # ---- Stage 2: AI albedo (i2i) + PBR synthesis, with a quality-gate
    # retry loop (brief AC6). Retries only when the MODEL is reachable —
    # in fallback mode there is nothing to re-run (same reference in, same
    # maps out), so the loop would be pointless.
    albedo, mode, attempt = None, "model", 0
    while True:
        use_seed = seed if attempt == 0 else (seed + 1) if seed is not None else 1000 + attempt
        albedo, mode = texture.generate_albedo(ref_png, prompt, strength, use_seed)
        maps = _synth_maps(albedo)
        gates = maps["gates"]
        hard_fail = _gates_hard_fail(gates)
        if not hard_fail or mode == "fallback-reference" or attempt >= MAX_GATE_RETRIES:
            break
        log.warning("generate gates failed (%s) — retry %d/%d", hard_fail, attempt + 1, MAX_GATE_RETRIES)
        attempt += 1

    for kind in ("albedo", "height", "normal", "roughness", "metallic"):
        with open(os.path.join(_asset_dir(asset_id), f"{kind}.png"), "wb") as f:
            f.write(maps[kind])

    meta = {
        "id": asset_id,
        "name": (name or "").strip() or f"texture-{int(time.time())}",
        "tag": (tag or "").strip(),
        "source": source,
        "content_hash": content_hash,
        "prompt": prompt,
        "strength": strength,
        "seed": seed,
        "mode": mode,
        "shape": blender_pack.normalize_shape(shape),
        "gates": maps["gates"],
        "gates_retries": attempt,
        "created_at": _now_iso(),
    }

    # ---- Optional: image-derived 3D geometry (brief + user request D).
    # v14: try REAL 3D first (Hunyuan3D-2.1 textured mesh, GPU), with an
    # automatic fallback to the CPU 2.5D relief (the mesh IS the object in the
    # photo: silhouette + depth relief). do_geometry stays opt-in (the "Build
    # 3D from the image" checkbox); when it is checked we try the real service
    # and transparently fall back if it is down or the generation fails.
    if do_geometry:
        real_glb = None
        t_r = time.monotonic()
        if real3d.available():
            try:
                glb = real3d.generate_glb(img_orig, seed=seed)
                with open(os.path.join(_asset_dir(asset_id), "model.glb"), "wb") as f:
                    f.write(glb)
                real_glb = glb
                log.info("generate id=%s real 3D mesh %d bytes (%ds)",
                         asset_id, len(glb), int((time.monotonic() - t_r) * 1000))
            except Exception as e:  # noqa: BLE001 — real 3D is best-effort
                log.warning("real 3D failed for %s: %r — falling back to 2.5D relief",
                            asset_id, e)
        if real_glb is not None:
            meta["geometry"] = {
                "mode": "hunyuan3d-2.1",
                "type": "glb",
                "size": len(real_glb),
                "build_ms": int((time.monotonic() - t_r) * 1000),
            }
        else:
            # Fallback: CPU 2.5D relief (loaded lazily; first call is slow).
            t_g = time.monotonic()
            try:
                aimg = Image.open(io.BytesIO(albedo)).convert("RGB")
                verts, uvs, obj_bytes, mesh_albedo, geom_mode = geometry.build_mesh(
                    img_orig, aimg, size=96, z_scale=0.35)
                with open(os.path.join(_asset_dir(asset_id), "model.obj"), "wb") as f:
                    f.write(obj_bytes)
                with open(os.path.join(_asset_dir(asset_id), "model_albedo.png"), "wb") as f:
                    f.write(mesh_albedo)
                meta["geometry"] = {
                    "mode": geom_mode,
                    "type": "obj",
                    "verts": int(len(verts)),
                    "size": 96,
                    "z_scale": 0.35,
                    "build_ms": int((time.monotonic() - t_g) * 1000),
                }
            except Exception as e:  # noqa: BLE001 — geometry is best-effort
                log.warning("geometry build failed: %r", e)
                meta["geometry"] = {"error": f"{type(e).__name__}: {e}"}

    _save_meta(meta)
    log.info("generate id=%s mode=%s strength=%.2f shape=%s geometry=%s gates_passed=%s (%ds, %d retries)",
             asset_id, mode, strength, meta["shape"],
             (meta.get("geometry") or {}).get("mode", "off"),
             maps["gates"]["passed"], int(time.monotonic() - t0), attempt)
    return _asset_response(asset_id, meta, reused=False,
                           elapsed_ms=int((time.monotonic() - t0) * 1000))


# ---------------------------------------------------------------------------
# Asset library (file-based, dir scan)
# ---------------------------------------------------------------------------
@app.get("/api/assets")
def list_assets():
    assets = []
    if os.path.isdir(GEN_DIR):
        for entry in os.listdir(GEN_DIR):
            meta = _load_meta(entry)
            if meta:
                assets.append(
                    {
                        "id": meta["id"],
                        "name": meta.get("name", ""),
                        "tag": meta.get("tag", ""),
                        "thumb": f"/api/assets/{meta['id']}/maps/albedo.png",
                        "mode": meta.get("mode", ""),
                        "shape": meta.get("shape", "cup"),
                        "created": meta.get("created_at", ""),
                        "has_model": bool(meta.get("geometry")),
                        "model_type": (meta.get("geometry") or {}).get("type", ""),
                        "model_url": (
                            f"/api/assets/{meta['id']}/model.glb"
                            if (meta.get("geometry") or {}).get("type") == "glb"
                            and not (meta.get("geometry") or {}).get("error")
                            else (
                                f"/api/assets/{meta['id']}/model.obj"
                                if bool(meta.get("geometry"))
                                else ""
                            )
                        ),
                    }
                )
    assets.sort(key=lambda a: a["created"], reverse=True)  # newest first
    return {"assets": assets}


@app.patch("/api/assets/{asset_id}")
async def patch_asset(asset_id: str, request: Request):
    meta = _load_meta(asset_id)
    if not meta:
        raise HTTPException(404, "asset not found")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(400, "body must be a JSON object")
    if "name" in body and isinstance(body["name"], str) and body["name"].strip():
        meta["name"] = body["name"].strip()
    if "tag" in body and isinstance(body["tag"], str):
        meta["tag"] = body["tag"].strip()
    _save_meta(meta)
    return meta


MAP_KINDS = ("albedo", "height", "normal", "roughness", "metallic")


@app.get("/api/assets/{asset_id}/maps/{kind}.png")
def get_map(asset_id: str, kind: str):
    if kind not in MAP_KINDS:
        raise HTTPException(404, "unknown map kind")
    p = os.path.join(_asset_dir(asset_id), f"{kind}.png")
    if not os.path.isfile(p):
        raise HTTPException(404, "map not found")
    return FileResponse(p, media_type="image/png")


@app.get("/api/assets/{asset_id}/model.obj")
def get_model(asset_id: str):
    meta = _load_meta(asset_id)
    if not meta or not meta.get("geometry"):
        raise HTTPException(404, "no image-derived model for this asset")
    p = os.path.join(_asset_dir(asset_id), "model.obj")
    if not os.path.isfile(p):
        raise HTTPException(404, "model.obj not found")
    return FileResponse(p, media_type="model/obj",
                        headers={"Content-Disposition": f"attachment; filename=\"model-{asset_id}.obj\""})


@app.get("/api/assets/{asset_id}/model.glb")
def get_model_glb(asset_id: str):
    """Real-3D textured mesh (Hunyuan3D-2.1) — a binary glTF with baked PBR
    textures. Present only when the real-3D service succeeded for this asset;
    the 2.5D relief fallback has no GLB (it is an OBJ)."""
    meta = _load_meta(asset_id)
    geom = (meta or {}).get("geometry") or {}
    if geom.get("type") != "glb" or geom.get("error"):
        raise HTTPException(404, "no real-3D model.glb for this asset (real-3D service was unavailable or failed)")
    p = os.path.join(_asset_dir(asset_id), "model.glb")
    if not os.path.isfile(p):
        raise HTTPException(404, "model.glb not found")
    return FileResponse(p, media_type="model/gltf-binary",
                        headers={"Content-Disposition": f"attachment; filename=\"model-{asset_id}.glb\""})


# ---------------------------------------------------------------------------
# Blender-pack (Tier 1)
# ---------------------------------------------------------------------------
@app.get("/api/assets/{asset_id}/blender-pack")
def blender_pack_route(asset_id: str):
    meta = _load_meta(asset_id)
    if not meta:
        raise HTTPException(404, "asset not found")
    ad = _asset_dir(asset_id)

    def _read(fn: str) -> bytes:
        p = os.path.join(ad, fn)
        if not os.path.isfile(p):
            raise HTTPException(404, f"missing {fn}")
        with open(p, "rb") as f:
            return f.read()

    maps = {k: _read(f"{k}.png") for k in ("albedo", "height", "normal", "roughness", "metallic")}

    model_obj = None
    model_glb = None
    geometry_mode = None
    geom = meta.get("geometry")
    if geom and not geom.get("error"):
        if geom.get("type") == "glb":
            # REAL 3D: ship the .glb (scene.py imports it + keeps baked PBR).
            gp = os.path.join(ad, "model.glb")
            if os.path.isfile(gp):
                with open(gp, "rb") as f:
                    model_glb = f.read()
                geometry_mode = geom.get("mode")
        else:
            mp = os.path.join(ad, "model.obj")
            if os.path.isfile(mp):
                with open(mp, "rb") as f:
                    model_obj = f.read()
                geometry_mode = geom.get("mode")

    data = blender_pack.make_pack(
        asset_id,
        meta.get("name", asset_id),
        maps,
        shape=meta.get("shape", blender_pack.DEFAULT_SHAPE),
        model_obj=model_obj,
        model_glb=model_glb,
        geometry_mode=geometry_mode,
    )
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="pcai-viz-pack-{asset_id}.zip"'},
    )


# ---------------------------------------------------------------------------
# Export .blend (Tier 1) — downloads a ready-to-open .blend via the sidecar.
# The sidecar (separate container, headless Blender 5.2.2) runs the SAME
# scene.py the artist's Load Pack uses, so the .blend is byte-equivalent to
# the artist's. The main app only proxies/streams it.
# ---------------------------------------------------------------------------
BLEND_SIDECAR_URL = os.environ.get("BLEND_SIDECAR_URL", "http://127.0.0.1:8100")


@app.get("/api/assets/{asset_id}/blend")
async def blend_route(asset_id: str):
    """Proxy to the .blend sidecar. First call builds (~10-60s); later calls
    stream the cached .blend from the shared PVC (instant). 503 while a build
    is in flight; 500 on a real build failure (message in the body)."""
    meta = _load_meta(asset_id)
    if not meta:
        raise HTTPException(404, "asset not found")
    url = f"{BLEND_SIDECAR_URL}/build?asset={asset_id}"
    # The sidecar build (Blender startup + scene build) can take well over a
    # minute; give it up to 290s (under the VirtualService 300s route timeout).
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(290.0, connect=5.0)) as client:
            r = await client.get(url)
    except httpx.ConnectError:
        raise HTTPException(503, "blend builder unavailable (sidecar not ready)")
    except httpx.TimeoutException:
        raise HTTPException(504, "blend build timed out — try again")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"blend builder error: {type(e).__name__}")

    # Stream the .blend back to the browser as a download.
    if r.status_code == 503:
        raise HTTPException(503, "blend is building — retry in a moment")
    if r.status_code >= 400:
        try:
            msg = r.json().get("error", r.text[:500])
        except Exception:
            msg = r.text[:500]
        raise HTTPException(500, f"blend build failed: {msg}")

    return Response(
        content=r.content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{asset_id}.blend"',
            "Cache-Control": "no-cache",
        },
    )


# ---------------------------------------------------------------------------
# Samples (bundled reference library)
# ---------------------------------------------------------------------------
SAMPLE_NAMES = {
    "sample_hat.png": "Hat (sample)",
    "sample_car.png": "Car (sample)",
    "sample_cup.png": "Cup (sample)",
}
# Each sample's matching 3D shape (preview + Blender pack): the sample photo
# maps to its own object — hat photo -> hat, car photo -> stylized car,
# cup photo -> cup. Samples without an entry fall back to the cup.
SAMPLE_SHAPES = {
    "sample_hat.png": "hat",
    "sample_car.png": "car",
    "sample_cup.png": "cup",
}


@app.get("/api/samples")
def list_samples():
    out = []
    if os.path.isdir(SAMPLES_DIR):
        for fn in sorted(os.listdir(SAMPLES_DIR)):
            ext = os.path.splitext(fn)[1].lower()
            if ext in ALLOWED_EXT and os.path.isfile(os.path.join(SAMPLES_DIR, fn)):
                out.append(
                    {
                        "id": os.path.splitext(fn)[0],
                        "name": SAMPLE_NAMES.get(fn, fn),
                        "url": f"/api/samples/{fn}",
                        "shape": SAMPLE_SHAPES.get(fn, blender_pack.DEFAULT_SHAPE),
                    }
                )
    return out


@app.get("/api/samples/{filename}")
def get_sample(filename: str):
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(400, "bad filename")
    p = os.path.join(SAMPLES_DIR, filename)
    ext = os.path.splitext(filename)[1].lower()
    if not os.path.isfile(p) or ext not in ALLOWED_EXT:
        raise HTTPException(404, "sample not found")
    return FileResponse(
        p,
        media_type={".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}[ext],
    )


# ---------------------------------------------------------------------------
# Static frontend (lazy mount — /api/* routes are registered above and win)
# ---------------------------------------------------------------------------
class _NoCacheStaticFiles(StaticFiles):
    """SPA static files must be revalidated on every load (no-cache) so a
    browser never keeps serving a stale app.js/index.html after a deploy
    (the v14.5 incident: a cached v14.3 app.js rendered every new sample as
    the old cup because 'hat'/'car' fell through to buildCupGeometry)."""

    async def get_response(self, path: str, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp


if os.path.isdir(FRONTEND_DIR) and os.path.isfile(os.path.join(FRONTEND_DIR, "index.html")):
    app.mount("/", _NoCacheStaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
else:
    log.info("frontend/ not present yet — serving API only (SPA mount deferred)")

    @app.get("/", include_in_schema=False)
    def root_stub():
        return JSONResponse({"ok": True, "note": "frontend not built yet — API is live at /api/*"})
