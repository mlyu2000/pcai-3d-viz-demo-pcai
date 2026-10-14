# PCAI 3D Viz PoC — single image (FastAPI backend + static frontend + samples
# + CPU-only ML geometry layer + offline model caches).
# Layout in image: /app = repo root (backend/ + frontend/ + sample_data/ + models/),
# so main.py's REPO_ROOT-relative lookups (frontend, sample_data) just work.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# --- Core API deps (lean layer, cached) ------------------------------------
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# --- ML geometry layer (CPU-only, lazy-imported by backend/geometry.py) ----
# torch + transformers (Depth-Anything V2 Small) + rembg (U2NetP) give the
# image->3D path (silhouette + monocular depth -> textured 2.5D OBJ). The core
# API never imports these at startup (fast boot; core flow unaffected if this
# layer is absent). torch/torchvision are the +cpu wheels.
COPY backend/requirements-3d.txt backend/requirements-3d.txt
RUN pip install --no-cache-dir -r backend/requirements-3d.txt

# --- Offline model caches (baked into the image) ---------------------------
# The cluster is offline/internal, so the geometry models ship INSIDE the image
# and are read from these env-var locations at runtime (no network needed):
#   rembg U2NetP      -> /app/models/rembg  (REMBG_HOME)
#   Depth-Anything V2 -> /app/models/hf     (HF_HUB_CACHE)
COPY models/ ./models/
ENV REMBG_HOME=/app/models/rembg \
    HF_HUB_CACHE=/app/models/hf \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

# --- app code + assets (frontend/ is served by the same pod at '/') --------
COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY sample_data/ ./sample_data/
# CA bundle for Tier 2 (AI Model Box) TLS: blender_pack.py ships this into the
# pack zip so the add-on can verify the CS1 LLM cert (private HPE Ezmeral CA).
COPY ca/ ./ca/

# /data = asset store (NFS PVC mount); pre-create for the non-root user
RUN useradd -m -u 1000 app \
    && mkdir -p /data && chown -R app:app /app /data
USER 1000

EXPOSE 8000
CMD ["uvicorn", "main:app", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
