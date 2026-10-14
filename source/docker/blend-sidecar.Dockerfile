# PCAI 3D Viz PoC — .blend sidecar (headless Blender, official 5.2.2 tarball).
#
# Why a separate image: the main app is python:3.11-slim + a heavy ML stack.
# The bpy PyPI wheel is cp313-only AND ships the Python module WITHOUT the
# `blender` binary (bpy.app.binary_path is empty) — verified on the bastion.
# The reliable path is the OFFICIAL Blender 5.2.2 linux tarball, which is
# self-contained: `blender` binary + bundled Python 3.13 + all bundled libs.
# It is the SAME build as the artist's desktop Blender (hash d13f752e3b9c),
# so the .blend it produces is bit-identical to what the add-on's Load Pack
# would produce.
#
# The sidecar is a tiny stdlib HTTP server (port 8100) that takes an asset id,
# downloads that asset's Blender-pack zip from the main app (same pod,
# localhost), runs the pack's proven scene.py under `blender --background`,
# and writes the resulting <id>.blend to the SHARED PVC — which the main app
# then streams to the browser as a direct download.
#
# Verified on the bastion (docker run, no GPU, no X):
#   blender --background --python <scene> -> rc 0, "Saved as t.blend", objects: 4.
#   The tarball binary needs a small set of system X11/GL shared libs (RUN 1).
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PATH="/opt/blender:/opt/blender/5.2/python/bin:${PATH}" \
    PYTHONUNBUFFERED=1

# Headless Blender runtime deps (X11/GL stack; software-render only, no GPU).
# Minimal set confirmed sufficient for `blender --background --python` on noble.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates xz-utils curl \
    libx11-6 libx11-xcb1 libxcb1 libxext6 libxrender1 libxfixes3 libxi6 \
    libxrandr2 libxcursor1 libxdamage1 libxinerama1 libxss1 libxt6 libxmu6 \
    libsm6 libice6 libfontconfig1 libfreetype6 \
    libxkbcommon0 libxkbcommon-x11-0 \
    libgl1 libglx0 libegl1 libopengl0 \
    libglib2.0-0t64 libdbus-1-3 \
    libwayland-client0 libwayland-cursor0 libwayland-egl1 \
    libnss3 libcups2 libasound2t64 libpulse0 \
    libva-drm2 libva-x11-2 libharfbuzz0b liblcms2-2 \
    libpng16-16 libjpeg-turbo8 libtiff6 libwebp7 \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Official Blender 5.2.2 (self-contained: binary + bundled Python 3.13).
# Pinned to match the artist's desktop Blender 5.2.2 LTS exactly.
ARG BLENDER_VERSION=5.2.2
RUN mkdir -p /opt/blender && \
    curl -fsSL "https://download.blender.org/release/Blender5.2/blender-${BLENDER_VERSION}-linux-x64.tar.xz" \
        -o /tmp/bl.tar.xz && \
    tar -xJf /tmp/bl.tar.xz -C /opt/blender --strip-components=1 && \
    rm -f /tmp/bl.tar.xz

# Build-time smoke test: headless Blender must run a scene and save a .blend
# (catches missing shared libs at build time, not at request time).
RUN printf 'import bpy\nbpy.ops.mesh.primitive_cube_add(location=(0,0,0))\nbpy.ops.wm.save_as_mainfile(filepath="/tmp/smoke.blend")\nprint("SMOKE_OK", len(bpy.data.objects))\n' > /tmp/smoke.py \
    && blender --background --python /tmp/smoke.py 2>&1 | tail -3 \
    && test -f /tmp/smoke.blend && echo "blend-smoke: saved" \
    && rm -f /tmp/smoke.py /tmp/smoke.blend

WORKDIR /app
COPY sidecar/build_blend.py /app/build_blend.py

# Run as the same uid as the main app (1000) so both containers can read/write
# the shared PVC (fsGroup 1000 chowns the mount).
RUN useradd -m -u 1000 appuser 2>/dev/null || true \
    && mkdir -p /data && chown -R 1000:1000 /app /data
USER 1000

EXPOSE 8100
# Run the stdlib HTTP server as a PLAIN long-running Python process using
# Blender's bundled Python 3.13 (already in the image, full stdlib, verified).
# A plain process gives clean SIGTERM handling on pod restarts and no Blender
# main-loop/signal quirks. build_blend.py spawns the `blender` binary (the
# self-contained 5.2.2 binary at /opt/blender/blender, on PATH) as a SUBPROCESS
# for each actual .blend build — one Blender process per build, not a permanent
# one hosting the server.
CMD ["/opt/blender/5.2/python/bin/python3.13", "build_blend.py"]
