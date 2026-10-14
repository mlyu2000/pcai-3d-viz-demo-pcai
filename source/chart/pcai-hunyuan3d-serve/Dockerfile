# Hunyuan3D-2.1 serving image (v14) — PCAI 3D Viz PoC real-3D stage.
# Serves the OFFICIAL api_server.py (FastAPI, port 8080): shape DiT + VAE + PBR paint.
#
# Base = pytorch 2.4.1 / CUDA 12.4 / py3.10 WITH the CUDA TOOLKIT (nvcc + headers) so the
# two custom extensions (custom_rasterizer GPU kernel + mesh_inpaint_processor C++/pybind11)
# COMPILE at build time; the runtime image then needs no GPU.
FROM pytorch/pytorch:2.4.1-cuda12.4-cudnn9-devel

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_DATASETS_OFFLINE=1 \
    OMP_NUM_THREADS=8 \
    PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
    TORCH_CUDA_ARCH_LIST=8.0

# Build toolchain: g++ for the C++/pybind11 mesh painter; git to clone the repo.
# (nvcc + CUDA headers come from the -devel base image.)
# libgl1 (libGL.so.1) + libglib2.0-0: RUNTIME deps of pymeshlab, which hy3dshape
# imports at startup (postprocessors -> pymeshlab). Without libGL the container
# crash-loops on `ImportError: libGL.so.1: cannot open shared object file`.
RUN apt-get update && apt-get install -y --no-install-recommends \
        g++ git curl ca-certificates \
        libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt
# Official source (api_server.py, model_worker.py, hy3dshape/, hy3dpaint/ incl. the CUDA exts
# + the RealESRGAN checkpoint at hy3dpaint/ckpt/RealESRGAN_x4plus.pth).
RUN git clone --depth 1 https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1.git /opt/Hunyuan3D-2.1
WORKDIR /opt/Hunyuan3D-2.1

# v14.8: forward /send request params (octree_resolution, num_inference_steps)
# into the shape pipeline call. The official model_worker.py ignores every
# request param except 'image' (mesh = self.pipeline(image=image)[0]) so the
# API's octree/steps fields are dead — detail is always 384/50. The patcher
# keeps those as defaults, so callers that don't send params are unchanged.
COPY patch_worker_params.py /opt/patch_worker_params.py
RUN python /opt/patch_worker_params.py

# requirements: strip the two Chinese mirror index URLs (unreachable from the bastion) and the
# demo/training-only packages api_server.py never imports:
#   bpy        — Blender Python wheel; only an OPTIONAL mesh_io path the renderer guards with
#                try/except (convert_utils builds the PBR GLB with pure-Python pygltflib)
#   deepspeed  — training-only
#   gradio     — demo UI (the API server uses FastAPI)
#   pythreejs  — demo viz
#   tb_nightly — tensorboard (demo only)
# torch is NOT pinned here (it comes from the base image); the CUDA exts compile against it.
RUN sed -e '/^--extra-index-url/d' requirements.txt \
    | grep -v -E '^[[:space:]]*(bpy|deepspeed|gradio|pythreejs|tb_nightly)([=[:space:]]|$)' \
    > /tmp/requirements.txt \
 && pip install --no-cache-dir -r /tmp/requirements.txt

# Custom CUDA rasterizer (rasterizer.cpp + rasterizer_gpu.cu) — needs nvcc from the base.
# Must be installed BEFORE the C++ mesh painter (which links against it).
RUN pip install --no-cache-dir ./hy3dpaint/custom_rasterizer

# mesh_inpaint_processor (C++ / pybind11) — the official compile script.
# NOTE: the script compiles `mesh_inpaint_processor.cpp` by RELATIVE path, so it
# MUST be run from its own directory (cd into it first) or cc1plus fails with
# "mesh_inpaint_processor.cpp: No such file or directory".
RUN cd hy3dpaint/DifferentiableRenderer \
 && bash compile_mesh_painter.sh \
 && ls -la mesh_inpaint_processor*.so

# rembg (bg removal in the paint pipeline) downloads u2net.onnx (~176MB) to /root/.u2net/
# on FIRST RUN. The cluster's egress to github.com is slow (~50-300 kB/s => ~15min), so
# pre-bake it at build time (bastion has good egress) and the predictor starts clean.
RUN mkdir -p /root/.u2net \
 && curl -fSL --retry 3 -o /root/.u2net/u2net.onnx \
      https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net.onnx \
 && ls -la /root/.u2net/u2net.onnx

# v14.10: the paint pipeline's imageSuperNet loads hy3dpaint/ckpt/RealESRGAN_x4plus.pth
# at startup. Upstream's git clone --depth 1 no longer ships it (git-lfs blob not fetched),
# so the pod crashes at load_models with FileNotFoundError. Fetch the official checkpoint
# at build time. (Placed after the compile steps so a rebuild here stays cheap.)
RUN mkdir -p hy3dpaint/ckpt \
 && curl -fSL --retry 3 -o hy3dpaint/ckpt/RealESRGAN_x4plus.pth \
      https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth \
 && ls -la hy3dpaint/ckpt/RealESRGAN_x4plus.pth

COPY entrypoint.sh /opt/entrypoint.sh
RUN chmod +x /opt/entrypoint.sh

# Kserve (MLIS PackagedModel) runs the image ENTRYPOINT with the model `arguments` as args.
# The entrypoint is authoritative: it sets cwd (the code uses RELATIVE imports: ./hy3dshape,
# ./hy3dpaint, hy3dpaint/ckpt/...), points the paint pipeline at the seeded HF cache, and
# runs the official api_server on port 8080 (the Kserve route target).
EXPOSE 8080
ENTRYPOINT ["/opt/entrypoint.sh"]
