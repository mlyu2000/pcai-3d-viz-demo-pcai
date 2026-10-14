"""PCAI 3D Viz PoC — texture helpers.

Four concerns (per the ADM IDD "Photo -> AI Texture Maps -> Blender" brief):
1. Ingest prep: reference validation (min size), delighting / illumination
   normalization so the albedo is "flat".
2. MLIS i2i client (Qwen-Image-Edit via stable-diffusion.cpp predictor).
   Proven call shape from the Styling Demo: multipart POST to
   {IMG_ENDPOINT}/v1/images/edits; the server ignores plain form params,
   effective params go in an embedded <sd_cpp_extra_args> JSON block in the
   prompt. Fallback: on any failure return the reference itself with
   mode='fallback-reference' — the pipeline must never fail on a cold model.
3. Deterministic PBR map synthesis from the albedo (brief Stage 2): height
   (luminance + contrast stretch), normal (Sobel derivative, flat = 128,128,255),
   roughness (inverted height, smoothed), metallic (constant). All pure
   functions: same albedo in -> bit-identical maps out.
4. Quality gates + seamless tiling (brief AC2/AC3/AC6): per-channel gates with
   a retry loop in main.py, and a crossfade pass that makes maps tileable.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
from typing import Optional

import httpx
import numpy as np
from PIL import Image

logger = logging.getLogger("viz-demo.texture")

IMG_ENDPOINT = os.environ.get("IMG_ENDPOINT", "")
MLIS_KEY = os.environ.get("MLIS_KEY", "")

MAX_SIDE = 1024         # cap longest side (brief: working resolution ~1024²)
ALIGN = 16              # both dims divisible by 16
MIN_SIDE = 16           # model floor (never go smaller for inference)
MIN_REF_SIDE = 512      # brief Stage 1: reject references smaller than 512px
DEFAULT_PROMPT = (
    "Convert this reference image into a clean, flat, evenly-lit texture map "
    "suitable for 3D modeling. Preserve the material's color, pattern and "
    "detail. Output a flat texture with no objects, no background, no "
    "perspective, no shading artifacts."
)
CONNECT_TIMEOUT = 30.0
READ_TIMEOUT = 300.0    # diffusion takes 15-60s on the A100


def compute_target_size(w: int, h: int) -> tuple[int, int]:
    """Preserve aspect ratio, cap max side at MAX_SIDE, both dims divisible by ALIGN."""
    scale = min(1.0, MAX_SIDE / max(w, h))
    tw = max(MIN_SIDE, int(w * scale))
    th = max(MIN_SIDE, int(h * scale))
    tw -= tw % ALIGN
    th -= th % ALIGN
    tw = max(MIN_SIDE, tw)
    th = max(MIN_SIDE, th)
    return tw, th


def resize_reference(img: Image.Image) -> Image.Image:
    """Decode/normalize the reference and return the model-sized RGB PNG."""
    img = img.convert("RGB")
    tw, th = compute_target_size(img.width, img.height)
    if (img.width, img.height) != (tw, th):
        img = img.resize((tw, th), Image.Resampling.LANCZOS)
    return img


def _luminance_normalized(albedo_bytes: bytes) -> np.ndarray:
    """Shared helper: albedo bytes -> float64 [H, W] in [0, 1] (percentile-stretched)."""
    albedo = Image.open(io.BytesIO(albedo_bytes)).convert("L")
    arr = np.asarray(albedo, dtype=np.float64)
    lo, hi = np.percentile(arr, (2.0, 98.0))
    if hi <= lo:  # constant image: avoid division by zero
        return np.zeros_like(arr)
    return np.clip((arr - lo) / (hi - lo), 0.0, 1.0)


def derive_height_map(albedo_bytes: bytes) -> bytes:
    """Deterministic height map: luminance, 2nd/98th-percentile contrast stretch,
    8-bit grayscale PNG. Pure function — identical input -> bit-identical output."""
    norm = _luminance_normalized(albedo_bytes)
    height = Image.fromarray((norm * 255.0).round().astype("uint8"), mode="L")
    buf = io.BytesIO()
    height.save(buf, format="PNG")
    return buf.getvalue()


def derive_normal_map(albedo_bytes: bytes, strength: float = 1.0) -> bytes:
    """Deterministic normal map from the albedo (brief Stage 2 / AC2).

    Sobel derivative of the (contrast-stretched) luminance -> surface tilt,
    encoded as RGB where flat = (128, 128, 255): R = X tilt, G = Y tilt, B = Z
    (depth, ~255 for a mostly-flat surface). Pure function.
    """
    h = _luminance_normalized(albedo_bytes)
    # Gradient (dx toward +x, dy toward +y); Sobel, then normalise by the 96
    # that scales a unit ramp to a ~45° tilt (the standard GL normal-map factor).
    from numpy.lib.stride_tricks import sliding_window_view

    k = np.array([[-1.0, 0.0, 1.0], [-2.0, 0.0, 2.0], [-1.0, 0.0, 1.0]], dtype=np.float64)
    kx, ky = k.T, k
    # Pad so every pixel has a full 3x3 window (edge-reflect).
    p = np.pad(h, 1, mode="edge")
    win = sliding_window_view(p, (3, 3))
    dx = float(strength) * np.tensordot(win, kx, axes=([-1, -2], [0, 1])) / 96.0
    dy = float(strength) * np.tensordot(win, ky, axes=([-1, -2], [0, 1])) / 96.0
    # Normal = normalise(-dx, -dy, 1).
    nz = np.ones_like(dx)
    mag = np.sqrt(dx * dx + dy * dy + nz * nz)
    nx, ny, nzz = -dx / mag, -dy / mag, nz / mag
    rgb = np.stack([(nx * 0.5 + 0.5) * 255.0, (ny * 0.5 + 0.5) * 255.0, (nzz * 0.5 + 0.5) * 255.0], axis=-1)
    img = Image.fromarray(np.clip(rgb, 0, 255).round().astype("uint8"), mode="RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def derive_roughness_map(albedo_bytes: bytes, base: float = 0.7, spread: float = 0.4) -> bytes:
    """Deterministic roughness map: invert the height luminance (bright/recessed
    -> smoother, dark/raised -> rougher for woven material), then a light box
    smooth. Output 8-bit grayscale PNG (value = roughness 0..1). Pure function."""
    h = _luminance_normalized(albedo_bytes)
    rough = np.clip(base + spread * (1.0 - h), 0.0, 1.0)
    # Light 3x3 box smooth to kill single-pixel noise.
    p = np.pad(rough, 1, mode="edge")
    hh, ww = h.shape
    win = np.zeros_like(rough)
    for i in (-1, 0, 1):
        for j in (-1, 0, 1):
            win += p[i + 1 : i + 1 + hh, j + 1 : j + 1 + ww]
    win /= 9.0
    img = Image.fromarray((win * 255.0).round().astype("uint8"), mode="L")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def derive_metallic_map(value: float, size: tuple[int, int]) -> bytes:
    """Constant metallic map (brief: usually 0 for woven fabric/leather).
    8-bit grayscale PNG of the given size."""
    v = int(round(np.clip(value, 0.0, 1.0) * 255.0))
    img = Image.new("L", (int(size[0]), int(size[1])), v)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Seamless tiling (brief AC3: wrap-edge gradient <= 1.5x mean internal gradient)
# ---------------------------------------------------------------------------
def _tile_crossfade(arr: np.ndarray) -> np.ndarray:
    """Make a 2D float array tileable by crossfading the left->right and
    top->bottom wrap seams (classic offset-and-blend 'make seamless')."""
    out = arr.copy().astype(np.float64)
    w, h = out.shape[1], out.shape[0]
    seam = max(4, min(w, h) // 8)  # blend width across each seam
    # Horizontal seam: blend the left edge into the right edge.
    for i in range(seam):
        t = i / seam  # 0 at the seam, 1 inside
        out[:, i] = (1.0 - t) * out[:, w - seam + i] + t * out[:, i]
        out[:, w - seam + i] = (1.0 - t) * out[:, i] + t * out[:, w - seam + i]
    # Vertical seam: blend the top edge into the bottom edge.
    for i in range(seam):
        t = i / seam
        out[i, :] = (1.0 - t) * out[h - seam + i, :] + t * out[i, :]
        out[h - seam + i, :] = (1.0 - t) * out[i, :] + t * out[h - seam + i, :]
    return out


def make_tileable(png_bytes: bytes) -> bytes:
    """Return a tileable version of a map (RGB or L) via seam crossfade.
    Pure function; preserves mode and PNG format."""
    img = Image.open(io.BytesIO(png_bytes))
    img = img.convert("RGBA") if img.mode == "RGBA" else img.convert("RGB" if img.mode != "L" else "L")
    arr = np.asarray(img, dtype=np.float64)
    if arr.ndim == 2:
        t = _tile_crossfade(arr)
        out = Image.fromarray(t.round().astype("uint8"), mode="L")
    else:
        t = np.stack([_tile_crossfade(arr[..., c]) for c in range(arr.shape[2])], axis=-1)
        out = Image.fromarray(t.round().astype("uint8"), mode="RGB")
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Ingest prep: validation + delighting (brief Stage 1)
# ---------------------------------------------------------------------------
def validate_reference(img: Image.Image) -> tuple[bool, str]:
    """Brief Stage 1: reject references that are too small / not usable."""
    w, h = img.size
    if max(w, h) < MIN_REF_SIDE:
        return False, f"reference is {w}x{h}; the longest side must be >= {MIN_REF_SIDE}px (it would upscale to mush)"
    if w * h < 256 * 256:
        return False, "reference is too small (below 256x256)"
    return True, ""


def delight(img: Image.Image) -> Image.Image:
    """Brief Stage 1: remove photographic lighting so the albedo is 'flat'.

    Two deterministic passes: (1) gray-world white balance (neutralise the
    overall colour cast from the lighting), (2) illumination normalisation
    (rescale so the mean luminance is a neutral mid-grey). Pure, reversible-
    enough for a texture: it flattens the lighting without erasing the pattern.
    """
    arr = np.asarray(img.convert("RGB"), dtype=np.float64)
    # (1) Gray-world white balance.
    means = arr.reshape(-1, 3).mean(axis=0)
    g = means.mean()
    if np.all(means > 1e-3):
        arr = arr * (g / means)[None, None, :]
    arr = np.clip(arr, 0, 255)
    # (2) Illumination normalisation: rescale luminance to a neutral mean (~128).
    lum = 0.2126 * arr[..., 0] + 0.7152 * arr[..., 1] + 0.0722 * arr[..., 2]
    m = float(lum.mean())
    if m > 1e-3:
        arr = arr * (128.0 / m)
    arr = np.clip(arr, 0, 255).astype("uint8")
    return Image.fromarray(arr, mode="RGB")


# ---------------------------------------------------------------------------
# Quality gates (brief AC2/AC3 + Stage 2 per-channel gates)
# Each returns a dict {ok, stats}. main.py runs these after synthesis and
# retries (max N) when a gate fails.
# ---------------------------------------------------------------------------
def gate_normal(png_bytes: bytes) -> dict:
    """AC2: normal map must be 'blue' — B>R and B>G in >90% of pixels, flat
    regions decode to ~(0.5,0.5,1.0)."""
    arr = np.asarray(Image.open(io.BytesIO(png_bytes)).convert("RGB"), dtype=np.float64) / 255.0
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    br = float((b > r).mean())
    bg = float((b > g).mean())
    # Flat-region check: pixels near (128,128,255) => small R,G deviation.
    flat_dev = np.abs((arr[..., 0] - 0.5)) + np.abs((arr[..., 1] - 0.5))
    flat_frac = float((flat_dev < 0.06).mean())
    ok = br > 0.90 and bg > 0.90 and flat_frac > 0.15
    return {"ok": ok, "B>red_frac": round(br, 3), "B>green_frac": round(bg, 3),
            "flat_region_frac": round(flat_frac, 3)}


def _edge_gradient_ratio(arr: np.ndarray) -> float:
    """Ratio of wrap-edge gradient to mean internal gradient (lower = more
    tileable). < 1.5 passes AC3."""
    a = arr.astype(np.float64)
    w, h = a.shape[1], a.shape[0]
    if w < 4 or h < 4:
        return 0.0
    # Internal gradient: magnitude of the gradient on the central 80%.
    c = a[h // 10 : h - h // 10, w // 10 : w - w // 10]
    gy, gx = np.gradient(c)
    internal = float(np.sqrt(gx ** 2 + gy ** 2).mean())
    # Wrap edges: difference across the seam (left/right, top/bottom).
    seam_lr = float(np.abs(a[:, 0] - a[:, w - 1]).mean())
    seam_tb = float(np.abs(a[0, :] - a[h - 1, :]).mean())
    seam = (seam_lr + seam_tb) / 2.0
    return seam / internal if internal > 1e-6 else 0.0


def gate_tileable(png_bytes: bytes) -> dict:
    """AC3: maps tile horizontally and vertically (wrap-edge gradient <=
    1.5x mean internal gradient)."""
    img = Image.open(io.BytesIO(png_bytes))
    arr = np.asarray(img.convert("L"), dtype=np.float64)
    ratio = _edge_gradient_ratio(arr)
    ok = ratio <= 1.5
    return {"ok": ok, "wrap_edge_grad_ratio": round(ratio, 3)}


def gate_albedo_delit(png_bytes: bytes) -> dict:
    """Delit check: albedo should be low-frequency flat (not strongly
    shaded). Measured by the fraction of near-uniform low-frequency energy."""
    img = Image.open(io.BytesIO(png_bytes)).convert("L")
    small = img.resize((64, 64), Image.Resampling.BILINEAR)
    arr = np.asarray(small, dtype=np.float64)
    lum = arr
    # Low-frequency flatness: after a heavy blur, the image should be close to
    # its local mean (low residual = flat). Residual ratio = mean(residual)/mean.
    from PIL import ImageFilter
    blurred = np.asarray(img.filter(ImageFilter.GaussianBlur(8)).resize((64, 64), Image.Resampling.BILINEAR), dtype=np.float64)
    residual = np.abs(lum - blurred)
    ratio = float(residual.mean() / max(1e-6, lum.mean()))
    ok = ratio < 0.25  # strongly shaded (high ratio) fails the delit check
    return {"ok": ok, "low_freq_flatness_ratio": round(ratio, 3)}


def _extra_args(strength: float, steps: int, seed: Optional[int]) -> str:
    args: dict = {"strength": strength, "sample_steps": steps}
    if seed is not None:
        args["seed"] = int(seed)
    return json.dumps(args)


def generate_albedo(
    reference_bytes: bytes,
    prompt: str,
    strength: float,
    seed: Optional[int] = None,
    steps: int = 8,
    endpoint: Optional[str] = None,
    api_key: Optional[str] = None,
) -> tuple[bytes, str]:
    """Call the MLIS i2i predictor. Returns (albedo_png_bytes, mode).

    mode is 'model' on success, 'fallback-reference' if the model is
    unreachable/errored (the reference image itself becomes the albedo).
    """
    endpoint = endpoint if endpoint is not None else IMG_ENDPOINT
    api_key = api_key if api_key is not None else MLIS_KEY

    if not endpoint:
        logger.warning("IMG_ENDPOINT not set — falling back to reference as albedo")
        return reference_bytes, "fallback-reference"

    ref = Image.open(io.BytesIO(reference_bytes)).convert("RGB")
    tw, th = compute_target_size(ref.width, ref.height)
    ref_buf = io.BytesIO()
    ref.resize((tw, th), Image.Resampling.LANCZOS).save(ref_buf, format="PNG")
    ref_buf.seek(0)

    extra = _extra_args(strength, steps, seed)
    full_prompt = f"{prompt}<sd_cpp_extra_args>{extra}</sd_cpp_extra_args>"
    form = {
        "prompt": full_prompt,
        "size": f"{tw}x{th}",
        "num_inference_steps": str(steps),
        "strength": f"{strength}",
    }
    files = {"image": ("ref.png", ref_buf, "image/png")}
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    try:
        r = httpx.post(
            f"{endpoint}/v1/images/edits",
            headers=headers,
            data=form,
            files=files,
            timeout=httpx.Timeout(CONNECT_TIMEOUT, read=READ_TIMEOUT),
        )
        if r.status_code != 200:
            logger.warning("i2i HTTP %s: %s — falling back to reference", r.status_code, r.text[:300])
            return reference_bytes, "fallback-reference"
        b64 = r.json()["data"][0]["b64_json"]
        return base64.b64decode(b64), "model"
    except Exception as e:  # noqa: BLE001 — any failure -> fallback, never 500
        logger.warning("i2i call failed (%s: %s) — falling back to reference", type(e).__name__, e)
        return reference_bytes, "fallback-reference"


def model_ready(endpoint: Optional[str] = None, api_key: Optional[str] = None) -> bool:
    """Cheap readiness probe: GET {endpoint}/v1/models with a 3s timeout."""
    endpoint = endpoint if endpoint is not None else IMG_ENDPOINT
    api_key = api_key if api_key is not None else MLIS_KEY
    if not endpoint:
        return False
    try:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        r = httpx.get(f"{endpoint}/v1/models", headers=headers, timeout=3.0)
        return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False
