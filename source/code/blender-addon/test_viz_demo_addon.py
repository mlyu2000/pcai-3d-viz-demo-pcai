# SPDX-License-Identifier: MIT
# Tests for viz_demo_addon.py — runnable OUTSIDE Blender (no GUI, no bpy install).
#
# Two layers:
#   1. Unit tests of the bpy-independent LLM core
#      (extract_python_block / safe_check / run_with_retry).
#   2. A mock-bpy smoke test: a fake `bpy` package is injected into sys.modules
#      so the real addon module can be imported and register()/unregister() run,
#      proving the class/property wiring is coherent (this is what Blender would
#      execute on install).
#
# Run:  python -m pytest blender-addon/test_viz_demo_addon.py -v
# or:   python -m unittest blender.addon.test_viz_demo_addon -v
# (stdlib unittest works too: python blender-addon/test_viz_demo_addon.py)

import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# Mock-bpy: just enough for `import viz_demo_addon` + register()/unregister().
# Injected into sys.modules BEFORE importing the addon so its top-level
# `import bpy` / `from bpy.props import ...` resolve to our fakes.
# ---------------------------------------------------------------------------
_REGISTERED = []
_UNREGISTERED = []


def _make_fake_bpy():
    bpy = types.ModuleType("bpy")

    # bpy.props — property factories (return sentinel objects; the addon stores
    # them as class/scene attributes, we only need them to be settable).
    props = types.ModuleType("bpy.props")

    def _prop_factory(name):
        def factory(*a, **k):
            return ("PROP", name)
        factory.__name__ = name
        return factory

    props.StringProperty = _prop_factory("StringProperty")
    props.IntProperty = _prop_factory("IntProperty")
    props.FloatProperty = _prop_factory("FloatProperty")
    props.BoolProperty = _prop_factory("BoolProperty")
    props.EnumProperty = _prop_factory("EnumProperty")
    bpy.props = props

    # bpy.types — base classes the addon subclasses.
    class_types = types.ModuleType("bpy.types")

    class Operator:
        def __init_subclass__(cls, **kw):
            super().__init_subclass__(**kw)

    class Panel:
        pass

    class AddonPreferences:
        pass

    class Scene:
        # plain object; the addon attaches StringProperty sentinels to it and
        # deletes them in unregister() — this exercises that round-trip.
        pass

    class_types.Operator = Operator
    class_types.Panel = Panel
    class_types.AddonPreferences = AddonPreferences
    class_types.Scene = Scene
    bpy.types = class_types

    # bpy.utils — record register/unregister calls.
    utils = types.ModuleType("bpy.utils")
    utils.register_class = lambda cls: _REGISTERED.append(cls)
    utils.unregister_class = lambda cls: _UNREGISTERED.append(cls)
    bpy.utils = utils

    # bpy.data / bpy.ops / bpy.context — present so any import resolves; the
    # tested code paths don't call them.
    bpy.data = types.SimpleNamespace()
    bpy.ops = types.SimpleNamespace()
    bpy.context = types.SimpleNamespace(scene=Scene())

    return bpy


def _install_fake_bpy():
    fake = _make_fake_bpy()
    sys.modules["bpy"] = fake
    sys.modules["bpy.props"] = fake.props
    sys.modules["bpy.types"] = fake.types
    sys.modules["bpy.utils"] = fake.utils
    return fake


def _load_addon():
    """Import viz_demo_addon under the fake bpy (once)."""
    sys.path.insert(0, HERE)
    if "viz_demo_addon" in sys.modules:
        return sys.modules["viz_demo_addon"]
    _install_fake_bpy()
    import viz_demo_addon  # noqa: 419
    return viz_demo_addon


# ---------------------------------------------------------------------------
# 1. LLM-core unit tests (pure Python, no bpy needed)
# ---------------------------------------------------------------------------
class ExtractPythonBlockTests(unittest.TestCase):
    def _f(self):
        return _load_addon().extract_python_block

    def test_fenced_python(self):
        text = "Here you go:\n```python\nimport bpy\nbpy.ops.mesh.primitive_cube_add()\n```\ndone"
        self.assertEqual(self._f()(text),
                         "import bpy\nbpy.ops.mesh.primitive_cube_add()")

    def test_fenced_bare(self):
        text = "```python\nx = 1\n```"
        self.assertEqual(self._f()(text), "x = 1")

    def test_no_fence_returns_raw_stripped(self):
        text = "   x = 1\ny = 2   "
        self.assertEqual(self._f()(text), "x = 1\ny = 2")

    def test_empty(self):
        self.assertEqual(self._f()(""), "")
        self.assertEqual(self._f()(None), "")


class SafeCheckTests(unittest.TestCase):
    def _f(self):
        return _load_addon().safe_check

    def test_clean_code_passes(self):
        ok, reason = self._f()("import bpy\nbpy.ops.mesh.primitive_cube_add()")
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_blocked_import_os(self):
        ok, reason = self._f()("import os\nos.system('rm -rf /')")
        self.assertFalse(ok)
        self.assertIn("os", reason)

    def test_blocked_from_import(self):
        ok, reason = self._f()("from subprocess import run\nrun(['ls'])")
        self.assertFalse(ok)
        self.assertIn("subprocess", reason)

    def test_blocked_pickle(self):
        ok, reason = self._f()("import pickle\npickle.dumps(1)")
        self.assertFalse(ok)
        self.assertIn("pickle", reason)

    def test_blocked_urllib_submodule(self):
        # `import urllib.request` -> top module "urllib" must be blocked.
        ok, reason = self._f()("import urllib.request\nurllib.request.urlopen('x')")
        self.assertFalse(ok)
        self.assertIn("urllib", reason)

    def test_bare_name_without_import(self):
        # No `import os`, but a bare `os.system` Name ref must still be caught.
        ok, reason = self._f()("os.system('id')")
        self.assertFalse(ok)
        self.assertIn("os", reason)

    def test_dotted_attribute_of_blocked_name(self):
        ok, reason = self._f()("x = socket.getaddrinfo\nx('a', 'b')")
        self.assertFalse(ok)
        self.assertIn("socket", reason)

    def test_word_boundary_no_false_positive(self):
        # "bpy" and a var named "my_os" must NOT trip the "os" blocklist.
        ok, reason = self._f()("import bpy\nmy_os = 5\nprint(my_os)")
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_syntax_error_falls_back_to_regex(self):
        # Unparseable code containing a blocked import is still rejected.
        ok, reason = self._f()("import os\nif (")
        self.assertFalse(ok)
        self.assertIn("os", reason)


class RunWithRetryTests(unittest.TestCase):
    def _r(self):
        return _load_addon().run_with_retry

    def test_succeeds_first_attempt(self):
        def call_llm(msg):
            return "```python\nx = 1\n```"
        ok, attempts, err, code = self._r()(call_llm, "build a cube")
        self.assertTrue(ok)
        self.assertEqual(attempts, 1)
        self.assertIsNone(err)
        self.assertEqual(code, "x = 1")

    def test_fails_twice_then_succeeds(self):
        calls = []

        def call_llm(msg):
            calls.append(msg)
            if len(calls) == 1:
                raise ValueError("boom-1")
            if len(calls) == 2:
                return "```python\nraise RuntimeError('boom-2')\n```"
            return "```python\nx = 2\n```"
        ok, attempts, err, code = self._r()(call_llm, "build a cube")
        self.assertTrue(ok)
        self.assertEqual(attempts, 3)
        self.assertEqual(len(calls), 3)
        # Retry messages must carry the previous error.
        self.assertIn("boom-1", calls[1])
        self.assertIn("boom-2", calls[2])
        # Final code is the one that succeeded.
        self.assertEqual(code, "x = 2")

    def test_blocked_code_retries_with_reason(self):
        calls = []

        def call_llm(msg):
            calls.append(msg)
            if len(calls) == 1:
                return "```python\nimport os\nos.system('id')\n```"
            return "```python\nx = 3\n```"
        ok, attempts, err, code = self._r()(call_llm, "build a cube")
        self.assertTrue(ok)
        self.assertEqual(attempts, 2)
        self.assertIn("os", calls[1])  # reason propagated to retry prompt

    def test_all_attempts_fail(self):
        def call_llm(msg):
            return "```python\nraise ValueError('always')\n```"
        ok, attempts, err, code = self._r()(call_llm, "build a cube")
        self.assertFalse(ok)
        self.assertEqual(attempts, 3)
        self.assertIsNotNone(err)
        self.assertIn("always", err)

    def test_empty_code_retried_then_succeeds(self):
        # Reasoning model returns an empty content block (token budget
        # exhausted) on the first attempt, then a real code block. Must NOT
        # report success on the empty attempt — it must retry and succeed.
        calls = []

        def call_llm(msg):
            calls.append(msg)
            if len(calls) == 1:
                return "   "  # empty / whitespace-only code block
            return "```python\nx = 5\n```"
        ok, attempts, err, code = self._r()(call_llm, "build a cube")
        self.assertTrue(ok)
        self.assertEqual(attempts, 2)  # did NOT pass on attempt 1
        self.assertEqual(len(calls), 2)
        # Retry prompt explicitly tells the model it got no code.
        self.assertIn("NO python code", calls[1])
        self.assertEqual(code, "x = 5")

    def test_empty_code_all_attempts_fail(self):
        # Every attempt returns an empty block -> must FAIL (not a silent
        # no-op success) after exhausting retries.
        def call_llm(msg):
            return ""
        ok, attempts, err, code = self._r()(call_llm, "build a cube")
        self.assertFalse(ok)
        self.assertEqual(attempts, 3)
        self.assertIsNotNone(err)
        self.assertIn("no python code", err)

    def test_llm_call_exception_retried(self):
        calls = []

        def call_llm(msg):
            calls.append(msg)
            if len(calls) == 1:
                raise ConnectionError("conn")
            return "```python\nx = 4\n```"
        ok, attempts, err, code = self._r()(call_llm, "build a cube")
        self.assertTrue(ok)
        self.assertEqual(attempts, 2)
        self.assertEqual(code, "x = 4")


# ---------------------------------------------------------------------------
# 2. Mock-bpy smoke test: import the real addon, register + unregister.
# ---------------------------------------------------------------------------
class MockBpySmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # fresh registered/unregistered lists for this test class
        _REGISTERED.clear()
        _UNREGISTERED.clear()
        cls.addon = _load_addon()

    def test_bl_info(self):
        bi = self.addon.bl_info
        self.assertEqual(bi["name"], "Viz Demo (PCAI 3D Viz PoC)")
        self.assertEqual(bi["blender"], (4, 0, 0))
        self.assertEqual(bi["category"], "3D View")

    def test_register_and_unregister(self):
        self.addon.register()
        self.assertEqual(len(_REGISTERED), 5,
                         "expected 5 classes registered: "
                         + repr([c.__name__ for c in _REGISTERED]))
        names = {c.__name__ for c in _REGISTERED}
        self.assertEqual(names, {
            "VizDemo_OT_load_pack", "VizDemo_OT_llm_generate",
            "VIEW3D_PT_vizdemo_pack", "VIEW3D_PT_vizdemo_llm",
            "VizDemo_Preferences",
        })
        # Scene properties attached during register()
        Scene = self.addon.bpy.types.Scene
        for attr in ("vizdemo_pack_path", "vizdemo_prompt", "vizdemo_status"):
            self.assertTrue(hasattr(Scene, attr), attr + " not attached")

        self.addon.unregister()
        self.assertEqual(len(_UNREGISTERED), 5)
        # Scene properties removed
        for attr in ("vizdemo_pack_path", "vizdemo_prompt", "vizdemo_status"):
            self.assertFalse(hasattr(Scene, attr), attr + " not removed")

    def test_panel_ids_and_space(self):
        for p in (self.addon.VIEW3D_PT_vizdemo_pack, self.addon.VIEW3D_PT_vizdemo_llm):
            self.assertEqual(p.bl_space_type, "VIEW_3D")
            self.assertEqual(p.bl_region_type, "UI")
            self.assertEqual(p.bl_category, "Viz Demo")

    def test_operator_ids(self):
        self.assertEqual(self.addon.VizDemo_OT_load_pack.bl_idname, "vizdemo.load_pack")
        self.assertEqual(self.addon.VizDemo_OT_llm_generate.bl_idname, "vizdemo.llm_generate")

    def test_preferences_idname_is_module_name(self):
        self.assertEqual(self.addon.VizDemo_Preferences.bl_idname, "viz_demo_addon")


if __name__ == "__main__":
    unittest.main(verbosity=2)
