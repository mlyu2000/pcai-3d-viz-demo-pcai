#!/usr/bin/env python3
"""Mock MLIS i2i image server for local testing (stdlib only).

POST /v1/images/edits  — multipart 'image' file -> deterministic "edited"
                          result: horizontal mirror + 10% brightness boost
                          + colored border, returned as OpenAI-style
                          {"data":[{"b64_json": ...}]}.
GET  /v1/models        — {"data":[{"id":"mock"}]}

Run:  python scripts/mock_image_server.py  (listens on 127.0.0.1:8099)
"""

import base64
import io
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image, ImageDraw, ImageEnhance, ImageOps

HOST = "127.0.0.1"
PORT = 8099


def _parse_multipart(body: bytes, content_type: str) -> bytes:
    """Extract the file bytes for the 'image' part from a multipart body."""
    m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type)
    if not m:
        raise ValueError("no boundary")
    boundary = ("--" + (m.group(1) or m.group(2))).encode()

    # Prefer the cgi module when available (cleaner than hand-parsing)
    try:
        import cgi  # noqa: F401  (removed in 3.13; fallback below)

        environ = {
            "REQUEST_METHOD": "POST",
            "CONTENT_TYPE": content_type,
            "CONTENT_LENGTH": str(len(body)),
        }
        fs = cgi.FieldStorage(
            fp=io.BytesIO(body), environ=environ, keep_blank_values=True
        )
        item = fs["image"]
        if item is None or not getattr(item, "file", None):
            raise ValueError("no 'image' field")
        return item.file.read()
    except ImportError:
        pass

    # Manual boundary parser
    parts = body.split(boundary)
    for part in parts:
        if b'name="image"' not in part:
            continue
        sep = part.find(b"\r\n\r\n")
        if sep == -1:
            continue
        data = part[sep + 4:]
        if data.endswith(b"\r\n"):
            data = data[:-2]
        return data
    raise ValueError("no 'image' file part")


def edit_image(png_bytes: bytes) -> bytes:
    """Deterministic 'edit': mirror + 10% brightness + colored border."""
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    img = ImageOps.mirror(img)
    img = ImageEnhance.Brightness(img).enhance(1.10)
    bordered = Image.new("RGB", (img.width + 24, img.height + 24), (200, 40, 120))
    bordered.paste(img, (12, 12))
    ImageDraw.Draw(bordered).rectangle([0, 0, bordered.width - 1, bordered.height - 1], outline=(120, 0, 180), width=3)
    buf = io.BytesIO()
    bordered.save(buf, format="PNG")
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, obj: dict, status: int = 200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/v1/models":
            self._send_json({"data": [{"id": "mock"}]})
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path != "/v1/images/edits":
            self._send_json({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            png = _parse_multipart(body, self.headers.get("Content-Type", ""))
        except Exception as e:
            self._send_json({"error": f"bad multipart: {e}"}, 400)
            return
        try:
            out = edit_image(png)
        except Exception as e:
            self._send_json({"error": f"edit failed: {e}"}, 500)
            return
        self._send_json({"data": [{"b64_json": base64.b64encode(out).decode(), "revised_prompt": "mock"}]})

    def log_message(self, format, *args):
        print("[mock-image-server]", format % args, flush=True)


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"mock MLIS i2i server on http://{HOST}:{PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
