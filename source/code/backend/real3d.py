"""PCAI 3D Viz PoC — REAL 3D (image -> textured mesh) via Hunyuan3D-2.1.

Optional service, wired into ``do_geometry=true`` as the automatic FIRST
choice, with the CPU 2.5D relief (geometry.build_mesh) as the automatic
fallback when the service is down or a generation fails/times out.

Env:
  REAL3D_ENDPOINT  MLIS Hunyuan3D-2.1 predictor URL (in-cluster service URL,
                   like IMG_ENDPOINT). Unset -> real 3D disabled (the 2.5D
                   relief is always available).
  REAL3D_KEY       Bearer key for the predictor. Defaults to MLIS_KEY (the
                   same MLIS namespace key the i2i predictor uses).

Contract (Hunyuan3D-2.1 official ``api_server.py``):
  POST /send         {"image": <RAW base64>, "seed": int, ...} -> {"uid": "..."}
  GET  /status/{uid} {"status": "processing" | "texturing" | "completed" | "error"}
                     completed -> {"model_base64": "<base64 GLB>"}
  GET  /health       {"status": "healthy"}

We use the ASYNC /send + short /status polls (never one long-held call), so
no request ever holds a connection across the Istio route timeout. The whole
generation (shape DiT + PBR texturing) typically takes ~1-3 min on the A100;
the poll budget defaults to 180s so the total /api/generate time stays under
the 300s ingress timeout even after the i2i stage.
"""
from __future__ import annotations

import base64
import io
import logging
import os
import time
from typing import Optional

import httpx
from PIL import Image

log = logging.getLogger("viz-demo.real3d")

REAL3D_ENDPOINT = os.environ.get(
    "REAL3D_ENDPOINT",
    "http://pcai-hunyuan3d-predictor.project-user-aieadmin.svc.cluster.local:80",
).rstrip("/")
# Same MLIS namespace key as the i2i predictor (IMG uses MLIS_KEY).
REAL3D_KEY = os.environ.get("REAL3D_KEY") or os.environ.get("MLIS_KEY")

# Poll budget for one generation. Must leave headroom under the 300s Istio
# ingress timeout for /api/generate (i2i stage ~30-60s runs first).
GENERATE_TIMEOUT_S = float(os.environ.get("REAL3D_TIMEOUT_S", "180"))
POLL_INTERVAL_S = 3.0
HEALTH_TTL_S = 30.0  # cache the /health result so readiness stays cheap

_octree_resolution = int(os.environ.get("REAL3D_OCTREE", "256"))
_num_inference_steps = int(os.environ.get("REAL3D_STEPS", "5"))

# (value, timestamp) cache for the health probe.
_health_cache: tuple[bool, float] = (False, 0.0)


def available() -> bool:
    """Cheap readiness probe (GET /health, 2.5s, cached for 30s).

    Returns False when REAL3D_ENDPOINT is unset (real 3D disabled) or the
    predictor is not answering healthy. The caller must treat False as
    "use the 2.5D fallback", never as an error.
    """
    if not REAL3D_ENDPOINT:
        return False
    global _health_cache
    now = time.monotonic()
    value, ts = _health_cache
    if now - ts < HEALTH_TTL_S:
        return value
    try:
        headers = {"Authorization": f"Bearer {REAL3D_KEY}"} if REAL3D_KEY else {}
        r = httpx.get(f"{REAL3D_ENDPOINT}/health", headers=headers, timeout=2.5)
        ok = r.status_code == 200 and (r.json() or {}).get("status") == "healthy"
    except Exception:  # noqa: BLE001 — any failure = down
        ok = False
    _health_cache = (ok, now)
    return ok


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {REAL3D_KEY}"} if REAL3D_KEY else {}


def _input_png(img: Image.Image, max_side: int = 1024) -> bytes:
    """Normalize the source photo for the 3D model: optional downscale +
    re-encode as RGB PNG. (Background removal is intentionally NOT done here:
    the 2.1 worker's rembg is a no-op, and its shape DiT conditions on the
    photo as-is; a clean object shot works best, which is what the PoC's
    reference photos are.)"""
    img = img.convert("RGB")
    w, h = img.size
    scale = min(1.0, max_side / max(w, h))
    if scale < 1.0:
        img = img.resize((max(8, int(w * scale)), max(8, int(h * scale))),
                         Image.Resampling.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def generate_glb(img: Image.Image, seed: Optional[int] = None,
                 timeout_s: Optional[float] = None) -> bytes:
    """Generate a textured GLB mesh from the photo via Hunyuan3D-2.1.

    POST /send -> poll GET /status/{uid} -> base64 GLB. Raises on any failure
    (connect error, non-200, worker error, or timeout budget exceeded) so the
    caller can fall back to the 2.5D relief.
    """
    if not REAL3D_ENDPOINT:
        raise RuntimeError("REAL3D_ENDPOINT not configured")
    timeout_s = timeout_s or GENERATE_TIMEOUT_S
    png = _input_png(img)
    body = {
        "image": base64.b64encode(png).decode("ascii"),
        "seed": int(seed) if seed is not None else 42,
        # texture MUST be True: the official GenerationRequest defaults it to
        # False (geometry-only). The whole value of the real-3D stage is the PBR
        # texture (albedo/normal/metallic) that GLTFLoader renders.
        "texture": True,
        # remove_background defaults True but is effectively a no-op for the
        # worker (it converts to RGBA first, so the rembg branch never runs).
        "octree_resolution": _octree_resolution,
        "num_inference_steps": _num_inference_steps,
    }
    headers = _auth_headers()

    with httpx.Client(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
        r = client.post(f"{REAL3D_ENDPOINT}/send", json=body, headers=headers)
        if r.status_code != 200:
            raise RuntimeError(f"/send HTTP {r.status_code}: {r.text[:300]}")
        uid = (r.json() or {}).get("uid")
        if not uid:
            raise RuntimeError(f"/send returned no uid: {r.text[:300]}")

        deadline = time.monotonic() + timeout_s
        last = ""
        while time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL_S)
            # The predictor runs a single-worker (limit-model-concurrency=1)
            # generation in a background thread. During that ~2 min of GPU work
            # (shape DiT + paint texturing, OMP_NUM_THREADS=8) its FastAPI event
            # loop is starved, so the otherwise-trivial /status file-check
            # endpoint does not answer in time and the Istio sidecar resets the
            # request with 502/503/504 ("upstream connect error / connection
            # termination"). These are TRANSIENT: keep polling until the
            # deadline; when generation finishes the event loop unblocks and
            # the next /status returns "completed" + the GLB. Only hard
            # failures (4xx, a worker "error"/"failed" status, or the deadline)
            # abort. This is why a plain `status_code != 200 -> raise` here
            # used to drop a perfectly-good in-flight generation.
            try:
                s = client.get(f"{REAL3D_ENDPOINT}/status/{uid}", headers=headers)
            except (httpx.ConnectError, httpx.RemoteProtocolError,
                    httpx.ReadError, httpx.PoolTimeout) as e:
                log.info("real3d uid=%s status poll transient connection error "
                         "(upstream mid-generation): %r — retrying", uid, e)
                continue
            if s.status_code != 200:
                if s.status_code in (502, 503, 504):
                    log.info("real3d uid=%s status HTTP %d (transient upstream "
                             "reset during generation) — retrying", uid, s.status_code)
                    continue
                raise RuntimeError(f"/status HTTP {s.status_code}: {s.text[:300]}")
            data = s.json() or {}
            status = data.get("status", "")
            if status != last:
                log.info("real3d uid=%s status=%s", uid, status)
                last = status
            if status == "completed":
                b64 = data.get("model_base64")
                if not b64:
                    raise RuntimeError("completed but no model_base64 in response")
                glb = base64.b64decode(b64)
                if not glb.startswith(b"glTF"):
                    raise RuntimeError(f"response is not a GLB ({len(glb)} bytes)")
                return glb
            if status in ("error", "failed"):
                raise RuntimeError(f"worker error: {data.get('error') or s.text[:300]}")
        raise TimeoutError(f"real3d generation exceeded {timeout_s:.0f}s budget")


def glb_stats(glb: bytes) -> dict:
    """Best-effort GLB size summary (vertex/face counts without a mesh lib):
    parse the JSON chunk + look at the bufferView size. Kept for logging only.
    """
    info = {"size": len(glb)}
    try:
        if glb[:4] == b"glTF" and len(glb) > 12:
            json_len = int.from_bytes(glb[12:16], "little")
            import json as _json
            info["json_chunk"] = int(json_len)
    except Exception:  # noqa: BLE001
        pass
    return info
