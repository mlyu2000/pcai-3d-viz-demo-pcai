"""PCAI 3D Viz PoC — Blender-pack assembly.

GET /api/assets/{id}/blender-pack -> zip with the full PBR map set + the
rendered scene.py + README.md (+ model.obj when the asset has image-derived
geometry). The scene.py (a) imports model.obj as the object when present
(image-derived 3D — the mesh IS the object in the photo), and (b) wires the
full PBR set (albedo + normal + roughness + metallic, Non-Color) into the
Principled BSDF, with a Bump node on the Relief variant for the height map.
"""

from __future__ import annotations

import io
import os
import zipfile
from typing import Optional

TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "scene.py.template")

# Per-sample 3D shapes (fallback when there is no image-derived model.obj):
# each bundled reference maps to its own object. Unknown shapes fall back to
# the cup (the proven default).
SHAPES = ("cup", "dog", "wood", "hat", "car")
DEFAULT_SHAPE = "cup"

# Map kinds shipped in the pack (brief Stage 3: albedo + normal + roughness +
# metallic, plus the height map that drives the Relief variant's Bump node).
MAP_KINDS = ("albedo", "height", "normal", "roughness", "metallic")

# Subtle relief: the AI height map adds surface detail to the object, it does
# not reshape it. 0.25 (the old flat-plane value) would mangle a ~1-unit object.
HEIGHT_SCALE = 0.05


def load_template() -> str:
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        return f.read()


def normalize_shape(shape) -> str:
    s = str(shape or "").strip().lower()
    return s if s in SHAPES else DEFAULT_SHAPE


def render_scene_py(asset_id: str, shape: Optional[str] = None,
                    has_model: bool = False, has_glb: bool = False) -> str:
    """Render the scene.py template. __OUT_NAME__ = asset id; __SHAPE__ = a
    validated shape (cup/dog/wood); __HAS_MODEL__ = True when the pack carries
    model.obj (image-derived geometry takes precedence over the shape);
    __HAS_GLB__ = True when the pack carries model.glb (REAL 3D — supersedes
    both model.obj and the procedural shape, and keeps its baked PBR)."""
    t = load_template()
    t = t.replace("__HEIGHT_SCALE__", str(HEIGHT_SCALE))
    t = t.replace("__OUT_NAME__", asset_id)
    t = t.replace("__SHAPE__", normalize_shape(shape))
    t = t.replace("__HAS_GLB__", "True" if has_glb else "False")
    t = t.replace("__HAS_MODEL__", "True" if has_model else "False")
    return t


def make_pack(
    asset_id: str,
    name: str,
    maps: dict,
    height_scale: Optional[float] = None,
    shape: Optional[str] = None,
    model_obj: Optional[bytes] = None,
    model_glb: Optional[bytes] = None,
    geometry_mode: Optional[str] = None,
) -> bytes:
    """Assemble the Blender-pack zip.

    `maps` is {kind: png_bytes} for each of MAP_KINDS (albedo/height/normal/
    roughness/metallic). `model_obj` (optional) is the image-derived 2.5D
    relief .obj; `model_glb` (optional) is a REAL-3D .glb (Hunyuan3D-2.1) with
    baked PBR. When `model_glb` is present it supersedes `model_obj` and the
    procedural shape (scene.py imports it and keeps its baked materials).
    """
    shape = normalize_shape(shape)
    has_glb = model_glb is not None
    has_model = (model_obj is not None) and not has_glb
    scene = render_scene_py(asset_id, shape, has_model=has_model, has_glb=has_glb)

    if has_glb:
        geom_desc = "real 3D model (`model.glb`, Hunyuan3D-2.1 — a textured mesh with baked PBR)"
    elif has_model:
        geom_desc = f"image-derived 3D model (`model.obj`, mode: {geometry_mode})"
    else:
        geom_desc = f"procedural **{shape.upper()}** (set `SHAPE` in `scene.py` to change it)"
    readme = (
        f"# Blender-pack — {name} (asset {asset_id})\n\n"
        "30-second setup:\n\n"
        "1. Unzip this folder.\n"
        "2. Load the folder with the `blender-addon`'s **Load Blender Pack** "
        "button, OR run the script headless:\n"
        "   ```\n   blender --background scene.py\n   ```\n"
        f"3. **The `{asset_id}.blend` file is generated when you run Load Pack** "
        f"(it is saved next to `scene.py` in this folder — it is not shipped "
        f"inside the zip). After running, you'll see two objects: "
        f"`*_Albedo` (full PBR material) and `*_Relief` (PBR + height bump).\n\n"
        f"The 3D object is the **{geom_desc}**.\n\n"
        "Material maps (all wired into the Principled BSDF, non-color maps set "
        "to Non-Color):\n"
        "  - `albedo.png`      -> Base Color (sRGB)\n"
        "  - `normal.png`      -> Normal Map node -> Normal\n"
        "  - `roughness.png`   -> Roughness (Non-Color)\n"
        "  - `metallic.png`    -> Metallic (Non-Color)\n"
        "  - `height.png`      -> Bump node -> Normal (Relief variant only)\n\n"
        "Tune `HEIGHT_SCALE` in `scene.py` for the surface relief and re-run.\n\n"
        "## Tier 2 (AI Model Box) — connecting to the CS1 LLM\n"
        "The add-on calls an OpenAI-compatible endpoint. Two working paths:\n"
        "  1. Port-forward (simplest — no TLS, no CA):\n"
        "     `kubectl -n project-user-aieadmin port-forward svc/litellm-helm 4001:4000`\n"
        "     Base URL `http://localhost:4001` · CA Bundle: *(empty)*\n"
        "  2. Public ingress (no kubectl):\n"
        "     Base URL `https://litellm.aie.cs1.ctc.sg.lab/v1`\n"
        "     CA Bundle: `ca/ezaf-aie-root-ca.pem` from this pack (private HPE\n"
        "     Ezmeral AIE Root CA — without it: `CERTIFICATE_VERIFY_FAILED`)\n"
        "  - Model: `qwen3-8-27b-int4-dflash2-r2`\n"
        "  - API Key: the value in **`ca/key.txt`** (a scoped LiteLLM virtual\n"
        "    key, included in this pack; paste it into the add-on's API Key\n"
        "    field — 17 characters, no line breaks)\n"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for kind in MAP_KINDS:
            zf.writestr(f"{kind}.png", maps[kind])
        if model_glb is not None:
            zf.writestr("model.glb", model_glb)
        elif model_obj is not None:
            zf.writestr("model.obj", model_obj)
        zf.writestr("scene.py", scene)
        zf.writestr("README.md", readme)
        # Ship the CS1 private-CA bundle so the add-on's Tier 2 (AI Model Box)
        # can verify the litellm TLS cert. The add-on reads this from the
        # pack folder (or the user sets the CA Bundle pref to this file).
        ca_src = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "ca", "ezaf-aie-root-ca.pem")
        ca_src = os.path.normpath(ca_src)
        if os.path.isfile(ca_src):
            with open(ca_src, "rb") as fh:
                zf.writestr("ca/ezaf-aie-root-ca.pem", fh.read())
        # Ship the Tier 2 API key (scoped LiteLLM virtual key) so the artist
        # has it right next to the CA. The add-on's API Key pref reads it from
        # here. (Internal-network PoC; the key is revocable — see README.)
        key_src = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "..", "ca", "key.txt")
        key_src = os.path.normpath(key_src)
        if os.path.isfile(key_src):
            with open(key_src, "rb") as fh:
                zf.writestr("ca/key.txt", fh.read())
    return buf.getvalue()
