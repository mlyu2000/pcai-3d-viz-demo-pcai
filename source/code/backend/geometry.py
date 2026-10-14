"""PCAI 3D Viz PoC — image -> 3D geometry reconstruction (CPU, no GPU).

Reconstructs a textured 3D mesh FROM THE IMAGE ITSELF:
  1. SILHOUETTE — rembg (U2NetP, CPU onnx) segments the object from the
     background. This is the object's real outline (what the user means by
     "the 3D model built from the image object", not a prebuilt shape).
  2. DEPTH      — Depth-Anything V2 Small (CPU torch) estimates the surface
     depth (relief: nose/legs/head bulge out, recessed parts pull back).
  3. MESH       — a textured front surface (the image's pixel grid placed on a
     plane, extruded by the depth map) + a solid back plane so the object is
     not see-through. UVs map the albedo texture onto the front surface.

Output: an OBJ (vertices + UVs + a single texture) + the albedo PNG. This OBJ
is what the Three.js preview and the Blender pack render — the geometry is
derived from the reference image, not a hardcoded cup/dog/wood.
"""

from __future__ import annotations

import io
import logging
import os
from typing import Optional

import numpy as np
from PIL import Image

log = logging.getLogger("viz-demo.geometry")

# Lazy, process-wide singletons (heavy to build; load once per pod).
_DEPTH_MODEL = None
_DEPTH_PROC = None
_RMGB = None
_DEPTH_MODEL_NAME = "depth-anything/Depth-Anything-V2-Small-hf"


def _load_depth():
    global _DEPTH_MODEL, _DEPTH_PROC
    if _DEPTH_MODEL is None:
        from transformers import DepthAnythingForDepthEstimation, AutoImageProcessor
        import torch
        log.info("loading Depth-Anything V2 Small (CPU) ...")
        _DEPTH_MODEL = DepthAnythingForDepthEstimation.from_pretrained(_DEPTH_MODEL_NAME)
        _DEPTH_MODEL.eval()
        _DEPTH_PROC = AutoImageProcessor.from_pretrained(_DEPTH_MODEL_NAME)
        log.info("Depth-Anything V2 Small loaded")
    return _DEPTH_MODEL, _DEPTH_PROC


def _load_rembg():
    global _RMGB
    if _RMGB is None:
        from rembg import new_session
        log.info("loading rembg (U2NetP, CPU) ...")
        _RMGB = new_session("u2netp")
        log.info("rembg loaded")
    return _RMGB


def estimate_depth(image: Image.Image, max_side: int = 512) -> np.ndarray:
    """Return a float32 [H, W] map where LARGER = CLOSER to the camera (relief)."""
    model, proc = _load_depth()
    import torch
    # Resize for a bounded CPU inference cost (depth is downsampled back).
    w, h = image.size
    scale = min(1.0, max_side / max(w, h))
    if scale < 1.0:
        image = image.resize((max(8, int(w * scale)), max(8, int(h * scale))), Image.BILINEAR)
    with torch.no_grad():
        inputs = proc(images=image, return_tensors="pt")
        out = model(**inputs).predicted_depth
    depth = out.squeeze().cpu().numpy().astype(np.float32)
    # Resize back to the (already resized) source dims the model saw.
    h, w = image.size[1], image.size[0]
    if depth.shape != (h, w):
        depth = Image.fromarray(depth).resize((w, h), Image.BILINEAR)
        depth = np.asarray(depth, dtype=np.float32)
    # Normalise to [0, 1]; larger = closer (Depth-Anything is inverse-depth).
    dmin, dmax = float(depth.min()), float(depth.max())
    if dmax - dmin < 1e-6:
        return np.zeros_like(depth)
    return (depth - dmin) / (dmax - dmin)


def cutout_mask(image: Image.Image, max_side: int = 512) -> np.ndarray:
    """Return a float32 [H, W] alpha mask (1.0 = object, 0.0 = background)."""
    from rembg import remove
    session = _load_rembg()
    w, h = image.size
    scale = min(1.0, max_side / max(w, h))
    if scale < 1.0:
        image = image.resize((max(8, int(w * scale)), max(8, int(h * scale))), Image.BILINEAR)
    out = remove(image, session=session)
    # out has an alpha channel
    alpha = np.asarray(out.convert("RGBA").split()[-1], dtype=np.float32) / 255.0
    h, w = image.size[1], image.size[0]
    if alpha.shape != (h, w):
        alpha = np.asarray(Image.fromarray((alpha * 255).astype(np.uint8)).resize((w, h), Image.BILINEAR), dtype=np.float32) / 255.0
    return alpha


def _silhouette_mode(mask: np.ndarray) -> str:
    """Classify the mask: 'silhouette' (object on background), 'slab' (full-frame).

    A full-frame image (wood grain, a texture that fills the whole frame) has
    no background to cut out, so it becomes a solid slab. A photo of an object
    on a background becomes a silhouette-clipped 2.5D object.
    """
    frac = float((mask > 0.5).mean())
    if frac >= 0.92 or frac < 0.03:
        return "slab"
    return "silhouette"


def _smooth2d(a: np.ndarray, sigma: float) -> np.ndarray:
    """Separable Gaussian blur (numpy only; scipy not required).

    Edge-padded so borders don't darken. This is what kills the per-cell
    relief jumps that make the front face read as a comb of twisted
    triangles ("spikes") — Depth-Anything at 96x96 has up to ~0.6 of
    full-range z-jump between adjacent cells.
    """
    r = int(max(1, sigma * 3))
    x = np.arange(-r, r + 1, dtype=np.float64)
    k = np.exp(-(x * x) / (2.0 * sigma * sigma))
    k = k / k.sum()

    def conv1d(arr, kernel, axis):
        n = arr.shape[axis]
        m = len(kernel)
        out = np.zeros_like(arr)
        for j in range(m):
            # window over kernel index j: take(arr, i + j - r) for i in 0..n-1,
            # with out-of-range indices clamped to the edge (edge padding).
            idx = np.clip(np.arange(-r, n - r, dtype=np.int64) + j, 0, n - 1)
            out += kernel[j] * np.take(arr, idx, axis=axis)
        return out

    a = a.astype(np.float64)
    a = conv1d(a, k, axis=1)
    a = conv1d(a, k, axis=0)
    return a


def _close_mask(mask: np.ndarray) -> np.ndarray:
    """Morphological CLOSING with a 3x3 structuring element (dilate then erode).

    Fills 1-2 cell background holes inside the object (rembg noise — every
    such hole edge otherwise becomes a wall quad / fringed edge in the mesh)
    while preserving the object silhouette exactly (closing never shrinks an
    object and never removes a real gap: the real gaps here — the hollow cup
    interior, the body-vs-handle split — are tens of cells wide and survive).
    We deliberately do NOT also run an opening: an opening would erode the
    whole silhouette inward by 1 cell, and the 1px floating anti-alias
    islands it would remove are now harmless (edge-replicated z-padding
    keeps their wall quads thin).
    """
    def shift_or(m):   # dilation
        out = m.copy()
        out[1:, :] |= m[:-1, :]
        out[:-1, :] |= m[1:, :]
        out[:, 1:] |= m[:, :-1]
        out[:, :-1] |= m[:, 1:]
        return out

    def shift_and(m):  # erosion
        out = m.copy()
        out[1:, :] &= m[:-1, :]
        out[:-1, :] &= m[1:, :]
        out[:, 1:] &= m[:, :-1]
        out[:, :-1] &= m[:, 1:]
        return out

    return shift_and(shift_or(mask))  # closing


def build_mesh(
    image: Image.Image,
    albedo: Image.Image,
    size: int = 96,
    z_scale: float = 0.35,
) -> tuple[np.ndarray, np.ndarray, bytes, bytes, str]:
    """Build a textured 2.5D mesh FROM THE IMAGE (silhouette + depth relief).

    Returns (vertices, uvs, obj_bytes, albedo_png_bytes, mode).

    `image` is the reference photo — its SILHOUETTE (rembg) defines the object's
    outline and its monocular DEPTH (Depth-Anything) defines the surface relief.
    The mesh is a grid extrusion: a front face at the depth relief (clipped to the
    silhouette so the object IS the shape in the photo), a flat back, and side
    walls along the silhouette boundary. The albedo texture is wrapped onto the
    front face. This is the object reconstructed from the image, not a
    prebuilt cup/dog/wood with the image pasted on it.
    """
    work = image.convert("RGB").resize((size, size), Image.BILINEAR)
    depth = estimate_depth(work, max_side=size)        # [size, size], larger = closer
    mask = (cutout_mask(work, max_side=size) > 0.5)    # [size, size] bool (object)
    mode = _silhouette_mode(mask)
    if mode == "slab":
        mask = np.ones((size, size), dtype=bool)
    else:
        # Clean the raw rembg mask (1-2 cell noise holes / floating islands)
        # BEFORE meshing: every hole edge otherwise becomes a full-height
        # wall quad (the "white stripes" artifact). Real gaps (body vs
        # handle) are tens of cells wide and survive the 3x3 close/open.
        mask = _close_mask(mask)

    # Relief: closer => larger z (toward camera). Centre in [-z_scale, +z_scale].
    # The raw monocular depth at 96x96 has per-cell jumps of up to ~0.6 of the
    # full range — those jump the front face into a comb of twisted triangles
    # ("spikes"). Smooth (Gaussian sigma=2 cells) BEFORE normalising so the
    # blur doesn't just rescale the noise, then re-normalise to [0,1].
    relief = 1.0 - depth
    relief = _smooth2d(relief, sigma=2.0)
    rmin, rmax = float(relief.min()), float(relief.max())
    relief = np.full_like(relief, 0.5) if (rmax - rmin) < 1e-6 else (relief - rmin) / (rmax - rmin)
    z = (relief * 2.0 - 1.0) * z_scale                 # [size, size] corner z

    # Corner grids (size+1)^2. x: -1..1 left->right; y: 1..-1 (image row 0 = +y).
    cx = np.repeat(np.linspace(-1.0, 1.0, size + 1, dtype=np.float32)[None, :], size + 1, axis=0)
    cy = np.repeat((1.0 - 2.0 * np.linspace(0.0, 1.0, size + 1, dtype=np.float32))[:, None], size + 1, axis=1)
    cmask = np.pad(mask, 1, constant_values=False)     # (size+2)^2, out-of-bounds = background
    # Edge-replicate (mode='edge') instead of constant 0: a background edge
    # corner takes the z of the nearest front cell, so silhouette-edge wall
    # quads stay thin instead of jumping to the mid-plane.
    cz = np.pad(z, 1, mode="edge")                     # (size+2)^2 corner z (edge fill)
    back_z = float(z.min()) - 0.02

    # UV per corner (image space; v flipped so image-top = v=1).
    u0 = np.repeat(np.linspace(0.0, 1.0, size + 1, dtype=np.float32)[None, :], size + 1, axis=0)
    v0 = np.repeat((1.0 - np.linspace(0.0, 1.0, size + 1, dtype=np.float32))[:, None], size + 1, axis=1)

    verts: list[tuple[float, float, float]] = []
    uvs: list[tuple[float, float]] = []
    faces: list[tuple[int, int, int, int]] = []
    idx = 0

    def add_v(x, y, z_, uu, vv):
        nonlocal idx
        i = idx
        idx += 1
        verts.append((float(x), float(y), float(z_)))
        uvs.append((float(uu), float(vv)))
        return i

    def quad(v0, v1, v2, v3):
        faces.append((v0 + 1, v1 + 1, v2 + 1, v3 + 1))

    def wall(a, b, ua, va, ub, vb):
        # a=(x,y,z), b=(x,y,z) front edge; back edge at z=back_z.
        pa = add_v(a[0], a[1], a[2], ua, va)
        pb = add_v(b[0], b[1], b[2], ub, vb)
        pc = add_v(b[0], b[1], back_z, ub, vb)
        pd = add_v(a[0], a[1], back_z, ua, va)
        quad(pa, pb, pc, pd)

    for r in range(size):
        for c in range(size):
            # cell corners: TL, TR, BR, BL (index into corner grid)
            tl = (r, c);      tr = (r, c + 1);      br = (r + 1, c + 1); bl = (r + 1, c)
            visible = cmask[tl] or cmask[tr] or cmask[br] or cmask[bl]
            if not visible:
                continue
            xTL, yTL = cx[tl], cy[tl];   xTR, yTR = cx[tr], cy[tr]
            xBR, yBR = cx[br], cy[br];   xBL, yBL = cx[bl], cy[bl]
            zTL, zTR = cz[tl], cz[tr];   zBR = cz[br]; zBL = cz[bl]
            # Front face (viewed from +Z, CCW): TL, TR, BR, BL.
            f0 = add_v(xTL, yTL, zTL, u0[tl], v0[tl])
            f1 = add_v(xTR, yTR, zTR, u0[tr], v0[tr])
            f2 = add_v(xBR, yBR, zBR, u0[br], v0[br])
            f3 = add_v(xBL, yBL, zBL, u0[bl], v0[bl])
            quad(f0, f1, f2, f3)
            # Back face (viewed from -Z, CCW): TL, BL, BR, TR.
            b0 = add_v(xTL, yTL, back_z, u0[tl], v0[tl])
            b3 = add_v(xBL, yBL, back_z, u0[bl], v0[bl])
            b2 = add_v(xBR, yBR, back_z, u0[br], v0[br])
            b1 = add_v(xTR, yTR, back_z, u0[tr], v0[tr])
            quad(b0, b3, b2, b1)
            # Side walls where an edge meets background (constant UV).
            ov = 0.0
            if not cmask[tl] and not cmask[tr]:      # left edge TL->BL
                wall((xTL, yTL, zTL), (xBL, yBL, zBL), ov, ov, ov, ov)
            if not cmask[tr] and not cmask[br]:      # right edge TR->BR
                wall((xTR, yTR, zTR), (xBR, yBR, zBR), ov, ov, ov, ov)
            if not cmask[tl] and not cmask[bl]:      # top edge TL->TR
                wall((xTL, yTL, zTL), (xTR, yTR, zTR), ov, ov, ov, ov)
            if not cmask[bl] and not cmask[br]:      # bottom edge BL->BR
                wall((xBL, yBL, zBL), (xBR, yBR, zBR), ov, ov, ov, ov)

    lines = ["# PCAI 3D Viz — image-derived geometry", "o ImageObject"]
    for x, y, z in verts:
        lines.append(f"v {x:.5f} {y:.5f} {z:.5f}")
    for uu, vv in uvs:
        lines.append(f"vt {uu:.5f} {vv:.5f}")
    for v0, v1, v2, v3 in faces:
        lines.append(f"f {v0}/{v0} {v1}/{v1} {v2}/{v2} {v3}/{v3}")
    obj_bytes = "\n".join(lines).encode("utf-8")

    ab = albedo.convert("RGB").resize((size, size), Image.BILINEAR)
    buf = io.BytesIO()
    ab.save(buf, format="PNG")
    return np.asarray(verts, dtype=np.float32), np.asarray(uvs, dtype=np.float32), obj_bytes, buf.getvalue(), mode


def build_from_image(image: Image.Image, albedo: Image.Image, **kw) -> tuple[bytes, bytes]:
    """Convenience: returns (obj_bytes, albedo_png_bytes)."""
    _, _, obj, ab, _mode = build_mesh(image, albedo, **kw)
    return obj, ab
