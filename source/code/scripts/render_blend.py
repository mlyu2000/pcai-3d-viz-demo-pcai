"""Headless Blender render of a generated viz-demo .blend (visual test helper).

Usage:
  blender --background render_blend.py -- <blend> --out <png> \
      [--width 640] [--height 420] [--samples 16]

Loads the .blend (which contains the two object variants <SHAPE>_Albedo and
<SHAPE>_Relief at x=-1.3/+1.3), adds a camera framing both + two lights on a
black world, and renders to PNG (Cycles, CPU, low samples). Exit code 0 on
success; the PNG is the visual ground truth for comparing the Blender output
against the original sample reference.
"""
import sys

import bpy


def parse_args(argv):
    i = argv.index("--") if "--" in argv else 0
    args = argv[i + 1:]
    opts = {"width": 640, "height": 420, "samples": 16, "out": None}
    blend = None
    while args:
        a = args[0]
        if a == "--out":
            args.pop(0)
            opts["out"] = args.pop(0)
        elif a == "--width":
            args.pop(0)
            opts["width"] = int(args.pop(0))
        elif a == "--height":
            args.pop(0)
            opts["height"] = int(args.pop(0))
        elif a == "--samples":
            args.pop(0)
            opts["samples"] = int(args.pop(0))
        else:
            blend = a
            args.pop(0)
    return blend, opts


def main():
    blend, opts = parse_args(sys.argv)
    if not blend or not opts["out"]:
        print("[render_blend] need: blend --out png")
        return 2

    bpy.ops.wm.open_mainfile(filepath=blend)

    # Black world (easy object masking: non-black pixels are the object).
    world = bpy.data.worlds.get("World") or bpy.data.worlds.new("World")
    bpy.context.scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs["Color"].default_value = (0.0, 0.0, 0.0, 1.0)
        bg.inputs["Strength"].default_value = 0.15

    # Camera framing both variants (they sit at x=-1.3 and x=+1.3).
    cam_data = bpy.data.cameras.new("TestCam")
    cam = bpy.data.objects.new("TestCam", cam_data)
    bpy.context.collection.objects.link(cam)
    cam.location = (0.0, -4.6, 2.2)
    tgt = bpy.data.objects.new("CamTarget", None)
    tgt.location = (0.0, 0.0, 0.5)
    bpy.context.collection.objects.link(tgt)
    con = cam.constraints.new("TRACK_TO")
    con.target = tgt
    con.track_axis = "TRACK_NEGATIVE_Z"
    con.up_axis = "UP_Y"
    cam_data.lens = 50
    bpy.context.scene.camera = cam

    # Two lights: key + fill (matches the web preview's lighting feel).
    key_d = bpy.data.lights.new("KeyLight", type="SUN")
    key_d.energy = 3.0
    key = bpy.data.objects.new("KeyLight", key_d)
    key.rotation_euler = (0.7, 0.2, 0.4)
    bpy.context.collection.objects.link(key)
    fill_d = bpy.data.lights.new("FillLight", type="SUN")
    fill_d.energy = 1.0
    fill = bpy.data.objects.new("FillLight", fill_d)
    fill.rotation_euler = (-0.5, -0.6, -0.8)
    bpy.context.collection.objects.link(fill)

    # Render settings: Cycles CPU, low samples (this is a test render).
    scene = bpy.context.scene
    try:
        scene.render.engine = "CYCLES"
    except Exception:
        scene.render.engine = "BLENDER_EEVEE"
    scene.cycles.device = "CPU"
    scene.cycles.samples = opts["samples"]
    scene.render.resolution_x = opts["width"]
    scene.render.resolution_y = opts["height"]
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = False
    scene.render.filepath = opts["out"]
    scene.render.image_settings.file_format = "PNG"

    bpy.ops.render.render(write_still=True)
    print(f"[render_blend] rendered {blend} -> {opts['out']} ({opts['width']}x{opts['height']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
