import tempfile
import unittest
from pathlib import Path

import yaml
from django.test import override_settings

from modules import services
from modules.pyobs_config import include_parts, pre_process_yaml, reload_anchors


# ── include_parts ─────────────────────────────────────────────────────────────

class IncludePartsTests(unittest.TestCase):
    def test_empty_key_returns_full(self):
        d = {"a": {"b": 1}}
        self.assertEqual(include_parts(d, ""), d)

    def test_none_key_returns_full(self):
        d = {"a": 1}
        self.assertEqual(include_parts(d, None), d)

    def test_single_key(self):
        d = {"a": {"b": 1}, "c": 2}
        self.assertEqual(include_parts(d, "a"), {"b": 1})

    def test_nested_key(self):
        d = {"a": {"b": {"c": 42}}}
        self.assertEqual(include_parts(d, "a.b"), {"c": 42})

    def test_deep_nested_key(self):
        d = {"a": {"b": {"c": {"d": "value"}}}}
        self.assertEqual(include_parts(d, "a.b.c"), {"d": "value"})

    def test_strips_whitespace(self):
        d = {"a": 1}
        self.assertEqual(include_parts(d, " a "), 1)

    def test_missing_key_raises(self):
        d = {"a": 1}
        with self.assertRaises(KeyError):
            include_parts(d, "b")


# ── reload_anchors ────────────────────────────────────────────────────────────

class ReloadAnchorsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_finds_anchors(self):
        f = self.tmp_path / "anchors.yaml"
        f.write_text("camera: &cam_anchor\n  type: DummyCamera\n")
        matches = reload_anchors(str(f))
        self.assertIn(("camera", "cam_anchor"), matches)

    def test_empty_file(self):
        f = self.tmp_path / "empty.yaml"
        f.write_text("no_anchor: value\n")
        self.assertEqual(reload_anchors(str(f)), [])

    def test_multiple_anchors(self):
        f = self.tmp_path / "multi.yaml"
        f.write_text("a: &anchor_a\n  x: 1\nb: &anchor_b\n  y: 2\n")
        self.assertEqual(len(reload_anchors(str(f))), 2)


# ── pre_process_yaml ──────────────────────────────────────────────────────────

class PreProcessYamlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_simple(self):
        """A plain YAML file with no includes is returned unchanged."""
        f = self.tmp_path / "config.yaml"
        f.write_text("camera:\n  type: DummyCamera\n")
        result = pre_process_yaml(str(f))
        self.assertIn("DummyCamera", result)

    def test_include(self):
        """Include block is replaced by the contents of the included file."""
        (self.tmp_path / "camera.yaml").write_text("type: DummyCamera\nexposure_time: 1.0\n")
        main = self.tmp_path / "config.yaml"
        main.write_text("camera:\n  {include camera.yaml}\n")

        parsed = yaml.safe_load(pre_process_yaml(str(main)))
        self.assertEqual(parsed["camera"]["type"], "DummyCamera")
        self.assertEqual(parsed["camera"]["exposure_time"], 1.0)

    def test_include_with_key(self):
        """Include block with key extracts only the specified section."""
        (self.tmp_path / "modules.yaml").write_text(
            "camera:\n  type: DummyCamera\ntelescope:\n  type: DummyTelescope\n"
        )
        main = self.tmp_path / "config.yaml"
        main.write_text("cam:\n  {include modules.yaml camera}\n")

        parsed = yaml.safe_load(pre_process_yaml(str(main)))
        self.assertEqual(parsed["cam"]["type"], "DummyCamera")
        self.assertNotIn("telescope", str(parsed.get("cam", {})))

    def test_include_nested_key(self):
        """Include with dotted key traverses nested dict."""
        (self.tmp_path / "nested.yaml").write_text("a:\n  b:\n    value: 42\n")
        main = self.tmp_path / "config.yaml"
        main.write_text("result:\n  {include nested.yaml a.b}\n")

        parsed = yaml.safe_load(pre_process_yaml(str(main)))
        self.assertEqual(parsed["result"]["value"], 42)

    def test_recursive_include(self):
        """Included files can themselves include other files."""
        (self.tmp_path / "deep.yaml").write_text("value: deep\n")
        (self.tmp_path / "mid.yaml").write_text("mid_val: 1\ndeep:\n  {include deep.yaml}\n")
        main = self.tmp_path / "config.yaml"
        main.write_text("root:\n  {include mid.yaml}\n")

        parsed = yaml.safe_load(pre_process_yaml(str(main)))
        self.assertEqual(parsed["root"]["deep"]["value"], "deep")

    def test_preserves_indentation(self):
        """Included content is properly indented."""
        (self.tmp_path / "sub.yaml").write_text("x: 1\ny: 2\n")
        main = self.tmp_path / "config.yaml"
        main.write_text("outer:\n  inner:\n    {include sub.yaml}\n")

        parsed = yaml.safe_load(pre_process_yaml(str(main)))
        self.assertEqual(parsed["outer"]["inner"]["x"], 1)
        self.assertEqual(parsed["outer"]["inner"]["y"], 2)

    def test_acl_block_via_include(self):
        """The motivating case: an acl: block pulled in from a shared fragment."""
        (self.tmp_path / "acl.shared.yaml").write_text(
            "acl:\n  allow:\n    scheduler: '*'\n    gui: [expose]\n"
        )
        main = self.tmp_path / "camera1.yaml"
        main.write_text("class: pyobs.modules.camera.BaseCamera\n{include acl.shared.yaml}\n")

        parsed = yaml.safe_load(pre_process_yaml(str(main)))
        self.assertEqual(parsed["acl"]["allow"]["scheduler"], "*")
        self.assertEqual(parsed["acl"]["allow"]["gui"], ["expose"])


# ── services.get_resolved_acl ─────────────────────────────────────────────────

class GetResolvedAclTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self._settings = override_settings(PYOBS_CONFIG_DIR=str(self.tmp_path))
        self._settings.enable()

    def tearDown(self):
        self._settings.disable()
        self.tmp.cleanup()

    def _write(self, name: str, content: str) -> None:
        (self.tmp_path / f"{name}.yaml").write_text(content)

    def test_missing_module_returns_none(self):
        self.assertEqual(services.get_resolved_acl("nope"), (None, None))

    def test_no_acl_key_returns_none(self):
        self._write("cam1", "class: pyobs.modules.camera.BaseCamera\n")
        self.assertEqual(services.get_resolved_acl("cam1"), (None, None))

    def test_acl_defined_locally(self):
        self._write(
            "cam1",
            "class: pyobs.modules.camera.BaseCamera\n"
            "acl:\n  allow:\n    scheduler: '*'\n",
        )
        acl, source = services.get_resolved_acl("cam1")
        self.assertEqual(acl["allow"]["scheduler"], "*")
        self.assertIsNone(source)

    def test_acl_value_via_include(self):
        """acl: key present in the module's own file, but its value is {include}'d."""
        self._write("rules.shared", "allow:\n  scheduler: '*'\n")
        self._write(
            "cam1",
            "class: pyobs.modules.camera.BaseCamera\nacl:\n  {include rules.shared.yaml}\n",
        )
        acl, source = services.get_resolved_acl("cam1")
        self.assertEqual(acl["allow"]["scheduler"], "*")
        self.assertEqual(source, "rules.shared")

    def test_acl_via_bare_top_level_include(self):
        """acl: key itself doesn't appear in the module's own file -- the whole block,
        key included, comes from a bare top-level {include}."""
        self._write("acl.shared", "acl:\n  allow:\n    scheduler: '*'\n")
        self._write(
            "cam1",
            "class: pyobs.modules.camera.BaseCamera\n{include acl.shared.yaml}\n",
        )
        acl, source = services.get_resolved_acl("cam1")
        self.assertEqual(acl["allow"]["scheduler"], "*")
        self.assertEqual(source, "acl.shared")


# ── services.build_acl_matrix ──────────────────────────────────────────────────

class BuildAclMatrixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self._settings = override_settings(PYOBS_CONFIG_DIR=str(self.tmp_path))
        self._settings.enable()

    def tearDown(self):
        self._settings.disable()
        self.tmp.cleanup()

    def _write(self, name: str, content: str) -> None:
        (self.tmp_path / f"{name}.yaml").write_text(content)

    def _row(self, matrix: dict, name: str) -> dict:
        return next(r for r in matrix["targets"] if r["name"] == name)

    def test_open_target_has_no_acl_and_open_flag(self):
        self._write("telescope", "class: pyobs.modules.telescope.BaseTelescope\n")
        matrix = services.build_acl_matrix()
        row = self._row(matrix, "telescope")
        self.assertTrue(row["open"])
        self.assertIsNone(row["error"])

    def test_caller_union_includes_non_module_and_deny_only_callers(self):
        self._write(
            "cam1",
            "class: pyobs.modules.camera.BaseCamera\n"
            "acl:\n  allow:\n    scheduler: '*'\n    external-script: [expose]\n",
        )
        self._write(
            "telescope",
            "class: pyobs.modules.telescope.BaseTelescope\n"
            "acl:\n  deny: [rogue-client]\n",
        )
        matrix = services.build_acl_matrix()
        # "scheduler" and "external-script" aren't modules this installation manages, and
        # "rogue-client" only ever appears in a deny list -- all three must still be columns.
        self.assertEqual(
            set(matrix["callers"]), {"scheduler", "external-script", "rogue-client"}
        )

    def test_allow_all_vs_allow_methods_vs_not_listed(self):
        self._write(
            "cam1",
            "class: pyobs.modules.camera.BaseCamera\n"
            "acl:\n  allow:\n    scheduler: '*'\n    gui: [expose, ICamera]\n",
        )
        matrix = services.build_acl_matrix()
        cells = self._row(matrix, "cam1")["cells"]
        self.assertEqual(cells["scheduler"]["kind"], "all")
        self.assertEqual(cells["gui"]["kind"], "methods")
        self.assertEqual(
            cells["gui"]["methods"],
            [{"name": "expose", "is_interface": False}, {"name": "ICamera", "is_interface": True}],
        )
        # a caller that exists as a column (via another target) but isn't in *this*
        # target's allow list is denied here.
        self._write(
            "telescope",
            "class: pyobs.modules.telescope.BaseTelescope\nacl:\n  deny: [gui]\n",
        )
        matrix = services.build_acl_matrix()
        self.assertEqual(self._row(matrix, "telescope")["cells"]["scheduler"]["kind"], "all")
        self.assertEqual(self._row(matrix, "telescope")["cells"]["gui"]["kind"], "denied")

    def test_deny_list_semantics(self):
        self._write(
            "telescope",
            "class: pyobs.modules.telescope.BaseTelescope\nacl:\n  deny: [rogue-client]\n",
        )
        matrix = services.build_acl_matrix()
        cells = self._row(matrix, "telescope")["cells"]
        self.assertEqual(cells["rogue-client"]["kind"], "denied")

    def test_mode_log_is_surfaced(self):
        self._write(
            "cam1",
            "class: pyobs.modules.camera.BaseCamera\n"
            "acl:\n  mode: log\n  allow:\n    scheduler: '*'\n",
        )
        matrix = services.build_acl_matrix()
        cell = self._row(matrix, "cam1")["cells"]["scheduler"]
        self.assertEqual(cell["mode"], "log")

    def test_broken_config_reported_as_error_not_crash(self):
        self._write("cam1", "acl:\n  allow: [this, is, not, a, mapping]\n")
        matrix = services.build_acl_matrix()
        row = self._row(matrix, "cam1")
        self.assertIsNotNone(row["error"])
        self.assertFalse(row["open"])


# ── services.save_local_acl ─────────────────────────────────────────────────────

class SaveLocalAclTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self._settings = override_settings(PYOBS_CONFIG_DIR=str(self.tmp_path))
        self._settings.enable()

    def tearDown(self):
        self._settings.disable()
        self.tmp.cleanup()

    def _write(self, name: str, content: str) -> None:
        (self.tmp_path / f"{name}.yaml").write_text(content)

    def _read(self, name: str) -> str:
        return (self.tmp_path / f"{name}.yaml").read_text()

    def test_adds_acl_to_module_with_none(self):
        self._write("telescope", "class: pyobs.modules.telescope.BaseTelescope\n")
        services.save_local_acl("telescope", {"allow": {"scheduler": "*"}})
        acl, source = services.get_resolved_acl("telescope")
        self.assertEqual(acl, {"allow": {"scheduler": "*"}})
        self.assertIsNone(source)
        # the rest of the file must survive untouched
        self.assertIn("class: pyobs.modules.telescope.BaseTelescope", self._read("telescope"))

    def test_replaces_existing_local_acl_block(self):
        self._write(
            "cam1",
            "class: pyobs.modules.camera.BaseCamera\n"
            "acl:\n  allow:\n    scheduler: '*'\n"
            "world:\n  class: pyobs.utils.simulation.world.SimWorld\n",
        )
        services.save_local_acl("cam1", {"mode": "log", "deny": ["rogue-client"]})
        acl, source = services.get_resolved_acl("cam1")
        self.assertEqual(acl, {"mode": "log", "deny": ["rogue-client"]})
        self.assertIsNone(source)
        # unrelated keys before and after the acl: block must survive untouched
        raw = self._read("cam1")
        self.assertIn("class: pyobs.modules.camera.BaseCamera", raw)
        self.assertIn("world:\n  class: pyobs.utils.simulation.world.SimWorld", raw)

    def test_preserves_unrelated_include_lines(self):
        self._write("comm.shared", "comm:\n  class: pyobs.comm.xmpp.XmppComm\n")
        self._write(
            "cam1",
            "{include comm.shared.yaml}\n"
            "class: pyobs.modules.camera.BaseCamera\n"
            "acl:\n  allow:\n    scheduler: '*'\n",
        )
        services.save_local_acl("cam1", {"allow": {"gui": ["expose"]}})
        raw = self._read("cam1")
        self.assertIn("{include comm.shared.yaml}", raw)
        acl, _ = services.get_resolved_acl("cam1")
        self.assertEqual(acl, {"allow": {"gui": ["expose"]}})

    def test_removes_acl_entirely(self):
        self._write(
            "cam1",
            "class: pyobs.modules.camera.BaseCamera\nacl:\n  allow:\n    scheduler: '*'\n",
        )
        services.save_local_acl("cam1", None)
        acl, source = services.get_resolved_acl("cam1")
        self.assertIsNone(acl)
        self.assertIsNone(source)
        self.assertIn("class: pyobs.modules.camera.BaseCamera", self._read("cam1"))

    def test_refuses_to_write_through_shared_fragment(self):
        self._write("acl.shared", "acl:\n  allow:\n    scheduler: '*'\n")
        self._write(
            "cam1",
            "class: pyobs.modules.camera.BaseCamera\n{include acl.shared.yaml}\n",
        )
        with self.assertRaises(ValueError):
            services.save_local_acl("cam1", {"allow": {"gui": ["expose"]}})
        # nothing written -- the module's own file and the shared fragment are untouched
        acl, source = services.get_resolved_acl("cam1")
        self.assertEqual(acl, {"allow": {"scheduler": "*"}})
        self.assertEqual(source, "acl.shared")