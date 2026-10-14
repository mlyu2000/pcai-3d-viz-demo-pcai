#!/usr/bin/env bash
# Hunyuan3D-2.1 predictor entrypoint (port 8080, official api_server.py).
#
# Kserve (MLIS PackagedModel) runs this ENTRYPOINT with the model `arguments` as $@. We are
# authoritative and IGNORE the passed args: we set the working dir (the code uses RELATIVE
# imports: ./hy3dshape, ./hy3dpaint, hy3dpaint/ckpt/RealESRGAN_x4plus.pth, hy3dpaint/cfgs/...),
# point the PAINT pipeline's runtime snapshot_download at the seeded HF cache, and run the
# official api_server with the fixed shape model path + port 8080 (the Kserve route target).
#
# Weight layout (seeded by seed-job.yaml onto the pcai-wan-store PVC):
#   /models/hunyuan3d-2.1/hunyuan3d-dit-v2-1/{config.yaml, model.fp16.ckpt}
#       -> shape pipeline (from_pretrained -> smart_load_model loads this plain dir); the
#          ShapeVAE and the DINOv2-Large conditioner are EMBEDDED in model.fp16.ckpt.
#   /models/hf-cache/hub/models--tencent--Hunyuan3D-2.1/snapshots/<rev>/hunyuan3d-paintpbr-v2-1/
#       -> paint pipeline (multiview_utils.snapshot_download with HF_HOME=/models/hf-cache).
set -euo pipefail

CODE_DIR="/opt/Hunyuan3D-2.1"
SHAPE_DIR="/models/hunyuan3d-2.1"
HF_HOME_DIR="/models/hf-cache"

cd "$CODE_DIR"
export HF_HOME="$HF_HOME_DIR"
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_DATASETS_OFFLINE=1
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-8}"
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
export TOKENIZERS_PARALLELISM=false

echo "[entrypoint] cwd: $CODE_DIR"
echo "[entrypoint] shape dir: $( [ -d "$SHAPE_DIR/hunyuan3d-dit-v2-1" ] && echo OK || echo MISSING )"
echo "[entrypoint] paint cache: $( [ -d "$HF_HOME_DIR/hub/models--tencent--Hunyuan3D-2.1" ] && echo OK || echo MISSING )"

# v14.9: the api_server logger writes to $CODE_DIR/gradio_cache/controller.log
# (SAVE_DIR default). Upstream creates gradio_cache at runtime via gradio,
# which this image strips — without the dir the server crashes at logger init.
mkdir -p "$CODE_DIR/gradio_cache"

# v14.8: guard the optional `bpy` import in the paint pipeline's mesh_utils.py.
# Upstream's mesh_utils.py has a BARE `import bpy` at module scope; the Dockerfile
# deliberately strips the bpy wheel (it is only an OPTIONAL mesh_io path — the
# textured-GLB conversion uses pure-Python pygltflib). Older builds patched this
# with try/except; a later upstream commit reverted it, crash-looping the pod on
# `ModuleNotFoundError: No module named 'bpy'`. Patch at startup (idempotent).
MESH_UTILS="$CODE_DIR/hy3dpaint/DifferentiableRenderer/mesh_utils.py"
if [ -f "$MESH_UTILS" ] && grep -qE '^import bpy$' "$MESH_UTILS"; then
  python - <<'PY'
import re
p = "/opt/Hunyuan3D-2.1/hy3dpaint/DifferentiableRenderer/mesh_utils.py"
s = open(p).read()
s2, n = re.subn(r'^import bpy$', 'try:\n    import bpy\nexcept ImportError:\n    bpy = None', s, flags=re.M, count=1)
if n == 1:
    open(p, "w").write(s2)
    print("[entrypoint] patched mesh_utils.py bpy import guard")
else:
    print("[entrypoint] mesh_utils.py bpy import already guarded / not found")
PY
fi

exec python api_server.py \
  --model_path "$SHAPE_DIR" \
  --host 0.0.0.0 \
  --port 8080 \
  --device cuda \
  --limit-model-concurrency 1
