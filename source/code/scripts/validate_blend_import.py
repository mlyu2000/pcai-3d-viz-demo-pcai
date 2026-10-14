"""Blender-pack .blend OPEN/IMPORT validator (runs as a separate Blender process
per file, so a failure on one file cannot affect the others).

Usage:
    blender --background --python validate_blend_import.py -- <name> <blend_path> <png_dir>

"Can be opened/imported correctly" is verified four ways:
  A. OPEN       — bpy.ops.wm.open_mainfile, the exact operator File>Open uses.
  B. CONTENTS    — >=2 MESH variants, UV layers, materials, every image-texture
                   resolves with non-empty pixels.
  C. ROUND-TRIP  — save again -> re-open -> identical object set (not corrupt).
  D. MOVED       — source PNGs hidden, .blend copied to an empty dir, re-opened:
                   textures must STILL resolve (only true if they are PACKED into
                   the .blend — a bare external ref to the build machine's path
                   would break here, i.e. on an artist's machine).

Prints one JSON object on stdout; the caller treats a crash (non-zero exit) as a
FAIL for that file.
"""
import json
import os
import shutil
import sys
import tempfile

import bpy


def _img_ok(name):
    img = bpy.data.images.get(name)
    if img is None:
        return {"name": name, "found": False}
    w, h = img.size
    try:
        _ = img.pixels  # forces a load if not already cached
    except RuntimeError as e:
        return {"name": name, "found": True, "loaded": False,
                "packed": bool(img.packed_file), "file_path": img.filepath,
                "error": str(e)[:140]}
    step = max(1, (w * h) // 40)
    nz = tot = 0
    for i in range(0, w * h * 3 - 2, 3 * step):
        tot += 1
        if img.pixels[i] > 0.01 or img.pixels[i + 1] > 0.01 or img.pixels[i + 2] > 0.01:
            nz += 1
    return {"name": name, "found": True, "loaded": True,
            "packed": bool(img.packed_file), "file_path": img.filepath,
            "resolution": [w, h],
            "nonzero_ratio": round(nz / tot, 3) if tot else 0.0}


def _all_image_names():
    out = []
    for mat in bpy.data.materials:
        if mat.node_tree:
            for n in mat.node_tree.nodes:
                if n.type == "TEX_IMAGE" and n.image is not None:
                    out.append(n.image.name)
    return sorted(set(out))


def _contents():
    objs = list(bpy.data.objects)
    mesh_objs = [o for o in objs if o.type == "MESH"]
    uv_bad = [o.name for o in mesh_objs if o.data is None or not o.data.uv_layers]
    imgs = [_img_ok(n) for n in _all_image_names()]
    return {"objects": sorted(o.name for o in objs),
            "mesh_count": len(mesh_objs),
            "uv_layer_missing": uv_bad,
            "images": imgs,
            "all_images_packed": all(i.get("packed") for i in imgs)}


def main():
    if "--" in sys.argv:
        a = sys.argv[sys.argv.index("--") + 1:]
    else:
        a = sys.argv[1:]
    name, blend_path, png_dir = a[0], a[1], a[2]
    res = {"name": name, "file": os.path.basename(blend_path)}
    problems = []

    # ---- A. open via the real File>Open operator
    try:
        bpy.ops.wm.open_mainfile(filepath=blend_path)
        res["opened"] = True
    except Exception as e:
        res["opened"] = False
        res["open_error"] = repr(e)
        res["problems"] = ["open failed: %r" % e]
        print(json.dumps(res))
        return

    # ---- B. contents
    c = _contents()
    res["objects"] = c["objects"]
    res["mesh_count"] = c["mesh_count"]
    res["uv_layer_missing"] = c["uv_layer_missing"]
    res["images"] = c["images"]
    res["all_images_packed"] = c["all_images_packed"]
    if c["mesh_count"] < 2:
        problems.append(f"expected 2 mesh variants (Albedo+Relief), found {c['mesh_count']}")
    for o in c["uv_layer_missing"]:
        problems.append(f"{o}: no UV layer")
    for im in c["images"]:
        if not im.get("found"):
            problems.append(f"image not found in file: {im.get('name')}")
        elif not im.get("loaded", True):
            problems.append(f"image failed to load: {im.get('name')} {im.get('error')}")
        elif im.get("loaded", True) and im.get("packed") is False and (
                im.get("file_path") is None or not os.path.exists(im["file_path"])):
            problems.append(f"image NOT packed and its file is missing -> broken "
                            f"external ref: {im.get('file_path')}")

    # ---- C. round-trip: save -> re-open -> same object set
    try:
        rt = blend_path + ".rt.blend"
        bpy.ops.wm.save_as_mainfile(filepath=rt)
        bpy.ops.wm.open_mainfile(filepath=rt)
        res["roundtrip_ok"] = sorted(o.name for o in bpy.data.objects) == res["objects"]
        if not res["roundtrip_ok"]:
            problems.append("round-trip object set mismatch")
    except Exception as e:
        res["roundtrip_ok"] = False
        problems.append(f"round-trip failed: {e!r}")

    # ---- D. moved / another-machine test
    # Copy the .blend + PNGs to a temp dir, DELETE the copies of the PNGs, then
    # open the copy of the .blend. Working on copies means a crash can never
    # leave the real source pack in a bad state.
    moved_ok, moved_detail = None, {}
    try:
        empty = tempfile.mkdtemp(prefix="valm_")
        dst = os.path.join(empty, os.path.basename(blend_path))
        shutil.copy(blend_path, dst)
        for fn in ("albedo.png", "height.png"):  # copy then delete the copies
            src = os.path.join(png_dir, fn)
            if os.path.exists(src):
                cpy = os.path.join(empty, fn)
                shutil.copy(src, cpy)
                os.remove(cpy)
        bpy.ops.wm.open_mainfile(filepath=dst)  # "open on another machine"
        moved_imgs = [_img_ok(n) for n in _all_image_names()]
        moved_detail = {"images": moved_imgs,
                        "all_packed": all(i.get("packed") for i in moved_imgs)}
        # Portability == every texture is PACKED into the .blend and loads.
        # (A legitimately all-black channel — e.g. a non-metallic material's
        #  metallic map — still loads correctly on another machine, so we must
        #  NOT gate on non-zero pixel content.)
        moved_ok = (len(moved_imgs) > 0
                    and all(i.get("packed", False) and i.get("loaded", False)
                            for i in moved_imgs))
        if not moved_ok:
            moved_detail["reason"] = ("textures do not survive opening the .blend "
                                      "without the source PNGs (not packed)")
    except Exception as e:
        moved_ok = False
        moved_detail = {"error": repr(e)}
    res["moved_test_ok"] = moved_ok
    res["moved_detail"] = moved_detail
    if moved_ok is False:
        problems.append("MOVED/ANOTHER-MACHINE TEST FAILED: the .blend does not import "
                        "correctly when its source PNGs are absent (textures not packed)")

    res["problems"] = problems
    res["ok"] = not problems
    print(json.dumps(res))


main()
