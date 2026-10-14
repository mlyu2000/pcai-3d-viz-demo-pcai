# SPDX-License-Identifier: MIT
# PCAI 3D Viz PoC — Blender 4.x / 5.x add-on (single file, stdlib only + bpy).
#
# Tier 1: "Load Blender Pack" — pick the unzipped blender-pack folder
#          (scene.py + albedo.png + height.png), exec scene.py, save .blend.
# Tier 2: "AI Model Box" — prompt -> OpenAI-compatible LLM call (urllib only,
#          no openai/requests SDK) -> extract ```python block -> safe-check
#          (module blocklist) -> exec in Blender -> auto-retry up to 3 times.
#
# The LLM-box core (extract_python_block / safe_check / llm_chat_completion /
# run_with_retry) is plain Python with NO bpy dependency so it can be
# unit-tested outside Blender.

bl_info = {
    "name": "Viz Demo (PCAI 3D Viz PoC)",
    "version": (1, 0, 0),
    "blender": (4, 0, 0),
    "location": "3D View > sidebar (N-panel) > 'Viz Demo'",
    "description": "Tier 1: load a Blender-pack folder (scene.py + albedo/height maps) "
                   "and save a .blend. Tier 2: LLM modeling box — prompt -> generated "
                   "bpy code -> safe exec -> auto-retry x3 against an "
                   "OpenAI-compatible endpoint (CS1 LiteLLM or local Ollama).",
    "author": "PCAI 3D Viz PoC",
    "category": "3D View",
}

import ast
import json
import os
import re
import urllib.request

import bpy
from bpy.props import StringProperty
from bpy.types import AddonPreferences, Operator, Panel

# ---------------------------------------------------------------------------
# LLM-box core — pure Python (no bpy), unit-testable in isolation.
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are an expert Blender Python (bpy) coder for **Blender 5.x** "
    "(API-compatible with 4.x). Output ONLY code in a single ```python block. "
    "Never delete existing objects unless asked. "
    "Use image texture nodes for textures. "
    "STRICT Blender 5.x API rules:\n"
    "- NEVER use removed 2.8/3.x APIs: `links` (collections/scene), "
    "`bpy.ops.object.mode_set` with old names, `bpy.context.scene.links`, "
    "`bpy.data.collections[x].links`, `obj.copy` + `links.collection` patterns. "
    "These raise `NameError`/`AttributeError` in 4.x+.\n"
    "- To add objects to the active collection use: "
    "`bpy.context.collection.objects.link(obj)` (NOT `.links`).\n"
    "- To set an object's parent collection: "
    "`bpy.context.collection.objects.link(obj)` after creation, or "
    "`obj.users_collection[0].objects.unlink(obj)` to move.\n"
    "- Access object data via `obj.data` (NOT `obj.evaluated_get()` — removed).\n"
    "- `bpy.context.view_layer.objects.active` for the active object; "
    "`bpy.context.view_layer.objects.selected` for selected objects.\n"
    "- Set dimensions with `obj.scale` or `obj.dimensions` (both work in 5.x). "
    "Do NOT use `obj.dimensions` before the object is in the scene graph if it "
    "errors — prefer `obj.scale`.\n"
    "- Modifiers: `obj.modifiers.new(name=..., type=...)` with type strings "
    "like 'BEVEL', 'SUBSURF', 'MIRROR', 'ARRAY', 'SOLIDIFY', 'SIMPLIFY'.\n"
    "- Shading: `obj.data.shade_smooth()` (mesh) or "
    "`bpy.ops.object.shade_smooth()` with the object active + selected.\n"
    "- Keep code SHORT: use primitives (cube, UV sphere, cylinder, cone) + "
    "modifiers + transforms. No loops over collections, no complex math, "
    "no imports beyond `bpy` and `mathutils` (only if needed). "
    "Every line must be valid Blender 5.x bpy."
)

LLM_TIMEOUT_S = 300.0
LLM_TEMPERATURE = 0.1
# Reasoning model (qwen3-8-27b): it spends many tokens on its internal
# `reasoning_content` before the final `content`. 1024 was too small — the
# chain exhausted the budget and the final code block came back empty
# (finish_reason="length"). 10240 gives ample headroom for long reasoning +
# a full bpy code block (per artist request — the model uses a large
# reasoning budget).
LLM_MAX_TOKENS = 10240
MAX_ATTEMPTS = 3

# Modules the generated code must never import or reference.
BLOCKLIST_MODULES = (
    "os", "subprocess", "shutil", "socket", "ctypes", "pickle", "http", "urllib",
)
_BLOCKLIST = frozenset(BLOCKLIST_MODULES)

_FENCE_RE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)
_IMPORT_RE = re.compile(r"\b(?:import|from)\s+([A-Za-z_][\w.]*)")
_DOTTED_RE = re.compile(
    r"\b(" + "|".join(BLOCKLIST_MODULES) + r")\s*\.\s*\w+"
)


def extract_python_block(text):
    """Pull the ```python fenced block out of an LLM reply.

    Falls back to the raw text when no fence is present.
    """
    text = text or ""
    m = _FENCE_RE.search(text)
    return (m.group(1) if m else text).strip()


def _fallback_scan(code):
    """Regex-only scan for code that does not parse (still best-effort safe)."""
    for m in _IMPORT_RE.finditer(code):
        top = m.group(1).split(".")[0]
        if top in _BLOCKLIST:
            return "blocked import: '{}'".format(m.group(1))
    m = _DOTTED_RE.search(code)
    if m:
        return "blocked module reference: '{}'".format(m.group(1))
    return None


def safe_check(code):
    """Reject code that imports or references a blocklisted module.

    Returns (ok, reason). Scans AST import statements plus Name/Attribute
    references (catches bare calls like ``os.system(...)`` even without an
    explicit ``import os``); falls back to a regex scan if the code does not
    parse.
    """
    code = code or ""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        hit = _fallback_scan(code)
        return (hit is None), (hit or "")

    hits = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in _BLOCKLIST:
                    hits.add("'{}' (line {})".format(alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top = node.module.split(".")[0]
                if top in _BLOCKLIST:
                    hits.add("'{}' (line {})".format(node.module, node.lineno))
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in _BLOCKLIST:
                hits.add("'{}' (line {})".format(node.id, node.lineno))
        elif isinstance(node, ast.Attribute):
            base = node
            while isinstance(base, ast.Attribute):
                base = base.value
            if isinstance(base, ast.Name) and base.id in _BLOCKLIST:
                hits.add("'{}' (line {})".format(base.id, node.lineno))

    if hits:
        return False, "blocked module/name reference(s): " + ", ".join(sorted(hits))
    return True, ""


def llm_chat_completion(base_url, api_key, model, messages,
                        timeout=LLM_TIMEOUT_S, ca_bundle=""):
    """POST {base_url}/chat/completions via urllib (stdlib only).

    Returns the assistant message content as a string.

    ``ca_bundle``: optional path to a PEM file (or file contents string) with
    the CA(s) to trust. When set, a custom SSL context is built from it so the
    CS1 endpoint (signed by the private HPE Ezmeral AIE Root CA) verifies
    cleanly. Empty = use the system default trust store.
    """
    url = base_url.rstrip("/") + "/chat/completions"
    payload = json.dumps({
        "model": model,
        "temperature": LLM_TEMPERATURE,
        "max_tokens": LLM_MAX_TOKENS,
        "messages": messages,
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + (api_key or ""),
        },
    )
    ctx = _build_ssl_context(ca_bundle)
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def _build_ssl_context(ca_bundle):
    """Return an ssl.SSLContext for the LLM connection.

    - ca_bundle empty  -> default context (system trust store).
    - ca_bundle a path -> load CA(s) from that file (overrides default).
    - ca_bundle PEM text (starts with '-----BEGIN') -> load from string.
    """
    import ssl
    ca = (ca_bundle or "").strip()
    if not ca:
        return ssl.create_default_context()
    ctx = ssl.create_default_context(cafile=None)
    if ca.startswith("-----BEGIN"):
        # PEM text passed directly
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as tf:
            tf.write(ca)
            tf_path = tf.name
        try:
            ctx.load_verify_locations(cafile=tf_path)
        finally:
            os.unlink(tf_path)
    else:
        # treat as a file path
        if not os.path.isfile(ca):
            # Fall back to default trust store if the file is missing, rather
            # than crashing — a bad path shouldn't break a working connection.
            return ssl.create_default_context()
        ctx.load_verify_locations(cafile=ca)
    return ctx


def run_with_retry(call_llm, initial_prompt, max_attempts=MAX_ATTEMPTS):
    """Call the LLM, extract code, safe-check, exec — retrying on failure.

    ``call_llm(user_message)`` must return the raw LLM text. Up to
    ``max_attempts`` total attempts (initial + retries). On each failure the
    next user message is::

        Fix the error in this Blender code:
        {code}
        Error: {e}

    Returns (ok, attempts_used, last_error, last_code).
    """
    code = None
    last_error = None
    user_msg = initial_prompt
    for attempt in range(1, max_attempts + 1):
        # 1) LLM call
        try:
            raw = call_llm(user_msg)
        except Exception as e:
            last_error = "LLM call failed (attempt {}): {}".format(attempt, e)
            if attempt >= max_attempts:
                return False, attempt, last_error, code
            user_msg = "Fix the error in this Blender code:\n{}\nError: {}".format(
                code if code is not None else "(no code yet)", e)
            continue

        # 2) extract + safe-check
        code = extract_python_block(raw)
        # A reasoning model can exhaust its token budget on the internal
        # reasoning chain and return an EMPTY final content block. That used
        # to be reported as success (exec("") is a no-op). Treat empty as a
        # retryable failure with an explicit nudge so the model produces code.
        if not code or not code.strip():
            last_error = ("LLM returned no python code block (attempt {}): "
                          "reasoning likely exhausted the token budget — "
                          "retry with a shorter prompt / fewer details").format(attempt)
            if attempt >= max_attempts:
                return False, attempt, last_error, code
            user_msg = ("The previous reply contained NO python code. "
                        "Reply with ONLY a single ```python code block, no "
                        "explanation. Build it simply so it is short.")
            continue
        ok, reason = safe_check(code)
        if not ok:
            last_error = "Safe check rejected code (attempt {}): {}".format(
                attempt, reason)
            if attempt >= max_attempts:
                return False, attempt, last_error, code
            user_msg = "Fix the error in this Blender code:\n{}\nError: {}".format(
                code, reason)
            continue

        # 3) exec
        try:
            exec(compile(code, "<viz-demo>", "exec"))
        except Exception as e:
            last_error = "Exec failed (attempt {}): {}".format(attempt, e)
            if attempt >= max_attempts:
                return False, attempt, last_error, code
            user_msg = "Fix the error in this Blender code:\n{}\nError: {}".format(
                code, e)
            continue

        return True, attempt, None, code

    # Unreachable (loop always returns), kept for safety.
    return False, max_attempts, last_error, code


# ---------------------------------------------------------------------------
# Blender add-on classes
# ---------------------------------------------------------------------------

class VizDemo_OT_load_pack(Operator):
    """Load a Blender-pack folder: validate, exec scene.py, verify .blend"""
    bl_idname = "vizdemo.load_pack"
    bl_label = "Load Pack"
    bl_description = "Run the pack's scene.py (with __file__ set) and save the .blend"

    def execute(self, context):
        folder = (context.scene.vizdemo_pack_path or "").strip()
        if not folder:
            self.report({"ERROR"},
                        "No pack folder selected — pick the unzipped blender-pack "
                        "folder in the field above first.")
            context.scene.vizdemo_status = "ERROR: no pack folder selected"
            return {"CANCELLED"}
        folder = os.path.abspath(folder)
        if not os.path.isdir(folder):
            self.report({"ERROR"}, "Pack folder does not exist: " + folder)
            context.scene.vizdemo_status = "ERROR: folder does not exist"
            return {"CANCELLED"}

        required = ("scene.py", "albedo.png", "height.png")
        missing = [f for f in required
                   if not os.path.isfile(os.path.join(folder, f))]
        if missing:
            msg = "Pack folder is missing required file(s): " + ", ".join(missing)
            self.report({"ERROR"}, msg)
            context.scene.vizdemo_status = "ERROR: " + msg
            return {"CANCELLED"}

        scene_py = os.path.join(folder, "scene.py")
        try:
            with open(scene_py, "r", encoding="utf-8") as fh:
                code = fh.read()
            # __file__ must point at scene.py so the template's
            # os.path.dirname(os.path.abspath(__file__)) resolves the pack folder.
            exec(compile(code, scene_py, "exec"),
                 {"__file__": scene_py, "__name__": "__main__"})
        except Exception as e:
            msg = "scene.py failed: {}: {}".format(type(e).__name__, e)
            self.report({"ERROR"}, msg)
            context.scene.vizdemo_status = "ERROR: " + msg[:300]
            return {"CANCELLED"}

        blend_files = [f for f in os.listdir(folder) if f.lower().endswith(".blend")]
        if not blend_files:
            self.report({"ERROR"},
                        "scene.py finished but no .blend file was found in "
                        + folder)
            context.scene.vizdemo_status = "ERROR: no .blend produced"
            return {"CANCELLED"}
        newest = max(blend_files,
                     key=lambda f: os.path.getmtime(os.path.join(folder, f)))
        blend_path = os.path.join(folder, newest)
        if not os.path.isfile(blend_path):
            self.report({"ERROR"}, "Could not verify saved file: " + blend_path)
            context.scene.vizdemo_status = "ERROR: .blend not found after save"
            return {"CANCELLED"}

        self.report({"INFO"}, "Blender-pack loaded — saved " + blend_path)
        context.scene.vizdemo_status = "OK: pack loaded, saved " + blend_path
        return {"FINISHED"}


class VizDemo_OT_llm_generate(Operator):
    """Send the prompt to the LLM, safe-exec the generated bpy, retry x3"""
    bl_idname = "vizdemo.llm_generate"
    bl_label = "Generate (LLM)"
    bl_description = "Prompt -> LLM -> generated bpy code -> safe exec (auto-retry x3)"

    def execute(self, context):
        prompt = (context.scene.vizdemo_prompt or "").strip()
        if not prompt:
            self.report({"ERROR"}, "Prompt is empty — type a prompt first.")
            context.scene.vizdemo_status = "ERROR: prompt is empty"
            return {"CANCELLED"}

        try:
            prefs = context.preferences.addons[__name__].preferences
        except Exception:
            self.report({"ERROR"}, "Add-on preferences not available")
            context.scene.vizdemo_status = "ERROR: preferences not available"
            return {"CANCELLED"}

        base_url = (prefs.base_url or "").strip()
        model = (prefs.model or "").strip()
        api_key = prefs.api_key or ""
        if not base_url or not model:
            self.report({"ERROR"},
                        "LLM endpoint not configured — set Base URL and Model in "
                        "Edit > Preferences > Add-ons > Viz Demo (PCAI 3D Viz PoC).")
            context.scene.vizdemo_status = "ERROR: LLM endpoint not configured"
            return {"CANCELLED"}

        def call_llm(user_msg):
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ]
            return llm_chat_completion(base_url, api_key, model, messages,
                                       ca_bundle=prefs.ca_bundle)

        ok, attempts, err, _code = run_with_retry(call_llm, prompt,
                                                  max_attempts=MAX_ATTEMPTS)
        if ok:
            status = "OK ({} attempt{})".format(
                attempts, "" if attempts == 1 else "s")
            self.report({"INFO"}, "Generated code executed: " + status)
        else:
            status = "ERROR ({} attempt{}): {}".format(
                attempts, "" if attempts == 1 else "s",
                (err or "unknown")[:300])
            self.report({"ERROR"}, "LLM generation failed: " + (err or "unknown")[:200])

        context.scene.vizdemo_status = status
        return {"FINISHED"} if ok else {"CANCELLED"}


class VIEW3D_PT_vizdemo_pack(Panel):
    """Load Blender Pack — Tier 1 (deterministic, no LLM)"""
    bl_idname = "VIEW3D_PT_vizdemo_pack"
    bl_label = "Load Blender Pack"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Viz Demo"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        layout.prop(scene, "vizdemo_pack_path", text="Pack folder")
        layout.operator("vizdemo.load_pack")


class VIEW3D_PT_vizdemo_llm(Panel):
    """AI Model Box — Tier 2 (LLM-generated bpy, safe exec, retry x3)"""
    bl_idname = "VIEW3D_PT_vizdemo_llm"
    bl_label = "AI Model Box"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Viz Demo"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        layout.prop(scene, "vizdemo_prompt", text="Prompt")
        layout.operator("vizdemo.llm_generate")
        if scene.vizdemo_status:
            layout.label(text=scene.vizdemo_status)


class VizDemo_Preferences(AddonPreferences):
    """LLM endpoint config (CS1 LiteLLM or local Ollama)"""
    bl_idname = __name__

    base_url: StringProperty(
        name="Base URL",
        description="OpenAI-compatible base URL (no trailing '/chat/completions'). "
                    "CS1: http://<cs1-litellm-host>:4001/v1 — "
                    "local Ollama: http://localhost:11434/v1",
        default="http://localhost:11434/v1",
    )
    api_key: StringProperty(
        name="API Key",
        description="Bearer token. CS1: value of secret 'litellm-key'. "
                    "Ollama: any non-empty string (e.g. 'ollama')",
        default="ollama",
    )
    model: StringProperty(
        name="Model",
        description="CS1: 'qwen3-8-27b-int4-dflash2-r2' — "
                    "Ollama: e.g. 'qwen2.5-coder:7b'",
        default="qwen2.5-coder:7b",
    )
    ca_bundle: StringProperty(
        name="CA Bundle (TLS)",
        description="Path to a PEM file with the CA(s) to trust for the LLM "
                    "endpoint. For CS1: the 'ca/ezaf-aie-root-ca.pem' included "
                    "in the Blender-pack (the endpoint is signed by the private "
                    "'HPE Ezmeral AIE Root CA' — without this, the connection "
                    "fails with CERTIFICATE_VERIFY_FAILED). Leave empty to use "
                    "the system trust store (works for local Ollama / public "
                    "endpoints).",
        default="",
        subtype="FILE_PATH",
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "base_url")
        layout.prop(self, "api_key")
        layout.prop(self, "model")
        layout.prop(self, "ca_bundle")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_CLASSES = (
    VizDemo_OT_load_pack,
    VizDemo_OT_llm_generate,
    VIEW3D_PT_vizdemo_pack,
    VIEW3D_PT_vizdemo_llm,
    VizDemo_Preferences,
)


def register():
    # Idempotent: skip if the Scene props are already attached (re-enable /
    # a second register() call must not double-register).
    if hasattr(bpy.types.Scene, "vizdemo_status"):
        return
    bpy.types.Scene.vizdemo_pack_path = StringProperty(
        name="Pack Folder",
        description="Unzipped blender-pack folder (scene.py + albedo.png + height.png)",
        default="",
        subtype="DIR_PATH",
    )
    bpy.types.Scene.vizdemo_prompt = StringProperty(
        name="Prompt",
        description="What to build/modify in the scene (sent to the LLM)",
        default="",
        maxlen=2048,
    )
    bpy.types.Scene.vizdemo_status = StringProperty(
        name="Last Status",
        description="Result of the last Load Pack / Generate call",
        default="",
        maxlen=512,
    )

    for cls in _CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    # Idempotent: unregister_class() raises RuntimeError when a class is
    # already unregistered (e.g. a manual unregister() plus Blender's
    # auto-disable on exit), so swallow that per class.
    for cls in reversed(_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except (RuntimeError, AttributeError):
            pass
    for prop in ("vizdemo_pack_path", "vizdemo_prompt", "vizdemo_status"):
        try:
            delattr(bpy.types.Scene, prop)
        except AttributeError:
            pass


if __name__ == "__main__":
    register()
