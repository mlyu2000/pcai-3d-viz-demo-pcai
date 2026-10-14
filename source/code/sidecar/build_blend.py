"""PCAI 3D Viz PoC — .blend sidecar (headless Blender via the `bpy` wheel).

A tiny stdlib HTTP server (port 8100) that turns an asset id into a
self-contained .blend file:

    GET /build?asset=<id>
      1. If /data/blends/<id>.blend already exists -> 200 (the main app
         streams it straight to the browser; no sidecar round-trip).
      2. Otherwise: fetch the asset's Blender-pack zip from the main app
         (same pod, http://127.0.0.1:8000/api/assets/<id>/blender-pack),
         unzip it, run `blender --background --python scene.py`, which
         builds the object, wires the full PBR set, PACKS the textures into
         the file, and saves <id>.blend. Move it to /data/blends/ -> 200.
      3. While a build is in flight (concurrent request) -> 503 "building".

The produced .blend is byte-equivalent to the one the artist gets via the
add-on's Load Pack: same scene.py, same maps, textures packed.

Concurrency: ThreadingHTTPServer + a per-asset lock file on the PVC
(<id>.blend.building) so one asset builds at a time; stale locks (>15 min,
e.g. after a pod restart mid-build) are reclaimed.

Env:
  APP_URL        main app (same pod) — default http://127.0.0.1:8000
  BLEND_DIR      output dir on the shared PVC — default /data/blends
  PORT           listen port — default 8100
"""
from __future__ import annotations

import io
import json as _json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

APP_URL = os.environ.get("APP_URL", "http://127.0.0.1:8000").rstrip("/")
BLEND_DIR = os.environ.get("BLEND_DIR", "/data/blends")
PORT = int(os.environ.get("PORT", "8100"))
ASSET_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
BLEND_TIMEOUT_S = 280  # below the VirtualService 300s route timeout
BUILDING_STALE_S = 900  # a lock older than this is a dead build

os.makedirs(BLEND_DIR, exist_ok=True)

_log_lock = threading.Lock()


def _log(msg: str) -> None:
    with _log_lock:
        print(f"[blend-sidecar] {msg}", flush=True)


def _blender_bin() -> str:
    """Resolve the headless Blender binary.

    The sidecar image installs the official Blender tarball at /opt/blender
    (self-contained binary + bundled Python 3.13). PATH is set to include
    /opt/blender, so `shutil.which` usually wins; the explicit candidates and
    the `bpy`-wheel fallback make this robust to image layout changes.
    """
    p = shutil.which("blender")
    if p:
        return p
    for cand in ("/opt/blender/blender", "/blender/blender"):
        if os.path.isfile(cand):
            return cand
    # Fallback: a `bpy`-wheel-based image (binary path reported by the wheel).
    try:
        out = subprocess.check_output(
            [sys.executable, "-c", "import bpy; print(bpy.app.binary_path)"],
            text=True, timeout=60,
        ).strip()
        if out and os.path.isfile(out):
            return out
    except Exception as e:  # noqa: BLE001
        _log(f"could not resolve bpy.app.binary_path: {type(e).__name__}: {e}")
    raise BuildError("no Blender binary found (PATH or /opt/blender/blender)")


class BuildError(Exception):
    pass


def _run_blender(pack_dir: str, asset_id: str) -> str:
    """Run the pack's scene.py under headless Blender; return the .blend path.

    scene.py saves <OUT_NAME>.blend next to itself (HERE = dirname(__file__)),
    so we run it in place inside the pack dir and then move the result.
    """
    script = os.path.join(pack_dir, "scene.py")
    if not os.path.isfile(script):
        raise BuildError("pack has no scene.py")
    _log(f"{asset_id}: running headless Blender on scene.py")
    blender = _blender_bin()
    try:
        proc = subprocess.run(
            [blender, "--background", "--python", script],
            capture_output=True, text=True, timeout=BLEND_TIMEOUT_S, cwd=pack_dir,
        )
    except subprocess.TimeoutExpired as e:
        raise BuildError(f"blender timed out after {BLEND_TIMEOUT_S}s") from e
    out = (proc.stdout or "") + ("\n" if (proc.stdout or "") and (proc.stderr or "") else "")
    err = proc.stderr or ""
    tail = (out + err)[-4000:]
    if proc.returncode != 0:
        raise BuildError(f"blender exited rc={proc.returncode}: {tail}")
    blend = os.path.join(pack_dir, f"{asset_id}.blend")
    if not os.path.isfile(blend):
        raise BuildError("scene.py finished but did not save a .blend: " + tail)
    return blend


def build_blend(asset_id: str) -> None:
    """Full build: pack zip from the app -> scene.py under Blender -> /data/blends."""
    lock = os.path.join(BLEND_DIR, f"{asset_id}.blend.building")
    try:
        # Non-blocking lock on the PVC (O_EXCL is atomic across processes).
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, os.urandom(16))
        os.close(fd)

        # 1) fetch the pack zip from the main app (same pod).
        url = f"{APP_URL}/api/assets/{asset_id}/blender-pack"
        with urllib.request.urlopen(url, timeout=120) as r:
            if r.status != 200:
                raise BuildError(f"app returned HTTP {r.status} for {asset_id}")
            zip_bytes = r.read()

        # 2) unpack into a fresh temp dir and run scene.py under Blender.
        tmp = tempfile.mkdtemp(prefix=f"blendsrc_{asset_id}_")
        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                zf.extractall(tmp)
            blend_src = _run_blender(tmp, asset_id)
            dest = os.path.join(BLEND_DIR, f"{asset_id}.blend")
            tmp_dest = dest + f".tmp.{os.getpid()}"
            shutil.move(blend_src, tmp_dest)
            os.replace(tmp_dest, dest)  # atomic on POSIX/NFS
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        _log(f"{asset_id}: .blend built ({os.path.getsize(dest)} bytes)")
    finally:
        try:
            os.unlink(lock)
        except FileNotFoundError:
            pass


def _build_or_error(asset_id: str) -> bytes:
    lock = os.path.join(BLEND_DIR, f"{asset_id}.blend.building")
    blend = os.path.join(BLEND_DIR, f"{asset_id}.blend")
    if os.path.isfile(blend):
        return open(blend, "rb").read()
    if os.path.isfile(lock):
        # A build is already in flight for this asset (or a stale one).
        age = time.time() - os.path.getmtime(lock)
        if age > BUILDING_STALE_S:
            try:
                os.unlink(lock)
            except FileNotFoundError:
                pass
        else:
            raise _Building()
    try:
        build_blend(asset_id)
    except _Building:
        raise
    if not os.path.isfile(blend):
        raise BuildError("build reported success but no .blend on the PVC")
    return open(blend, "rb").read()


class _Building(Exception):
    pass


import time  # noqa: E402  (used by _build_or_error)


class Handler(BaseHTTPRequestHandler):
    server_version = "BlendSidecar/1.0"

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self._send(200, b'{"ok":true}')
            return
        if parsed.path != "/build":
            self._send(404, b'{"error":"not found"}')
            return
        qs = parse_qs(parsed.query)
        asset_id = (qs.get("asset") or [""])[0]
        if not ASSET_RE.match(asset_id):
            self._send(400, b'{"error":"bad asset id"}')
            return
        try:
            data = _build_or_error(asset_id)
        except _Building:
            self._send(503, b'{"error":"build in progress","status":"building"}')
            return
        except BuildError as e:
            _log(f"{asset_id}: BUILD FAILED: {e}")
            import json as _json
            self._send(500, _json.dumps({"error": str(e)[:2000]}).encode())
            return
        except Exception as e:  # noqa: BLE001
            _log(f"{asset_id}: UNEXPECTED: {type(e).__name__}: {e}")
            import json as _json
            self._send(500, _json.dumps({"error": f"{type(e).__name__}: {e}"}).encode())
            return
        self._send(200, data, "application/octet-stream")

    def log_message(self, fmt, *args):  # quieter
        _log(self.address_string() + " " + fmt % args)


def main() -> None:
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    _log(f"listening on :{PORT} (APP_URL={APP_URL}, BLEND_DIR={BLEND_DIR})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
