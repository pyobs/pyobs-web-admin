import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml
from django.test import override_settings

from modules import ejabberd, services
from modules.views import _tag_host
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


# ── services.resolve_and_validate_acl ────────────────────────────────────────────

class ResolveAndValidateAclTests(unittest.TestCase):
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

    def test_valid_local_acl(self):
        self._write("cam1", "class: pyobs.modules.camera.BaseCamera\nacl:\n  allow:\n    scheduler: '*'\n")
        acl, source, error = services.resolve_and_validate_acl("cam1")
        self.assertEqual(acl, {"allow": {"scheduler": "*"}})
        self.assertIsNone(source)
        self.assertIsNone(error)

    def test_open_module_has_no_error(self):
        self._write("cam1", "class: pyobs.modules.camera.BaseCamera\n")
        acl, source, error = services.resolve_and_validate_acl("cam1")
        self.assertIsNone(acl)
        self.assertIsNone(source)
        self.assertIsNone(error)

    def test_malformed_allow_reports_error_not_raise(self):
        self._write("cam1", "acl:\n  allow: [this, is, not, a, mapping]\n")
        acl, source, error = services.resolve_and_validate_acl("cam1")
        self.assertIsNone(acl)
        self.assertIsNone(source)
        self.assertIsNotNone(error)


# ── services.get_comm_user ────────────────────────────────────────────────────

class GetCommUserTests(unittest.TestCase):
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
        self.assertIsNone(services.get_comm_user("nope"))

    def test_no_comm_block_returns_none(self):
        """Confirmed real example: HttpFileCache has no comm: block at all -- this is the
        signal DEV_EJABBERD_INTEGRATION.md uses to skip modules that were never expected to
        have an XMPP identity, not an error."""
        self._write("filecache", "class: pyobs.modules.utils.HttpFileCache\n")
        self.assertIsNone(services.get_comm_user("filecache"))

    def test_comm_block_without_user_key_returns_none(self):
        self._write("cam1", "comm:\n  password: pyobs\n")
        self.assertIsNone(services.get_comm_user("cam1"))

    def test_comm_user_defined_locally(self):
        self._write("cam1", "class: pyobs.modules.camera.BaseCamera\ncomm:\n  user: camera\n  password: pyobs\n")
        self.assertEqual(services.get_comm_user("cam1"), "camera")

    def test_comm_user_via_anchor_merge_key(self):
        """Matches a real config's shape: comm: {<<: *comm, user: camera, password: pyobs}."""
        self._write(
            "cam1",
            "_comm_defaults: &comm\n  class: pyobs.comm.xmpp.XmppComm\n"
            "  jid: pyobs\n"
            "class: pyobs.modules.camera.BaseCamera\n"
            "comm:\n  <<: *comm\n  user: camera\n  password: pyobs\n",
        )
        self.assertEqual(services.get_comm_user("cam1"), "camera")

    def test_comm_user_via_include(self):
        """comm: pulled in from a shared fragment via {include} -- get_comm_user reuses
        get_resolved_acl's exact resolution pipeline, so this works the same way."""
        self._write("comm.shared", "comm:\n  user: camera\n  password: pyobs\n")
        self._write(
            "cam1",
            "class: pyobs.modules.camera.BaseCamera\n{include comm.shared.yaml}\n",
        )
        self.assertEqual(services.get_comm_user("cam1"), "camera")

    def test_resolved_missing_module_returns_none_triple(self):
        self.assertEqual(services.get_resolved_comm("nope"), (None, None, None))

    def test_resolved_comm_defined_locally_has_no_source(self):
        self._write("cam1", "class: pyobs.modules.camera.BaseCamera\ncomm:\n  user: camera\n  password: pyobs\n")
        user, password, source = services.get_resolved_comm("cam1")
        self.assertEqual(user, "camera")
        self.assertEqual(password, "pyobs")
        self.assertIsNone(source)

    def test_resolved_comm_via_bare_top_level_include_has_source(self):
        """comm: key itself doesn't appear in the module's own file -- the whole block, key
        included, comes from a bare top-level {include} -- DEV_EJABBERD_USER_MANAGEMENT.md's
        config write-back must refuse to edit comm.password: in this case, the same way
        save_local_acl already refuses for acl:."""
        self._write("comm.shared", "comm:\n  user: camera\n  password: pyobs\n")
        self._write(
            "cam1",
            "class: pyobs.modules.camera.BaseCamera\n{include comm.shared.yaml}\n",
        )
        user, password, source = services.get_resolved_comm("cam1")
        self.assertEqual(user, "camera")
        self.assertEqual(password, "pyobs")
        self.assertEqual(source, "comm.shared")

    def test_resolved_comm_via_inline_include_value_has_source(self):
        """comm: key present in the module's own file, but its value is {include}'d."""
        self._write("comm.shared", "user: camera\npassword: pyobs\n")
        self._write(
            "cam1",
            "class: pyobs.modules.camera.BaseCamera\ncomm:\n  {include comm.shared.yaml}\n",
        )
        user, password, source = services.get_resolved_comm("cam1")
        self.assertEqual(user, "camera")
        self.assertEqual(password, "pyobs")
        self.assertEqual(source, "comm.shared")


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
        # "rogue-client" only ever appears in a deny list -- all three must still be columns,
        # alongside every actual module ("cam1", "telescope") which is always a column too.
        self.assertEqual(
            set(matrix["callers"]),
            {"scheduler", "external-script", "rogue-client", "cam1", "telescope"}
        )

    def test_every_module_is_always_a_column_even_with_no_acl_at_all(self):
        # "always show all modules in both headers" -- a module must appear as a column
        # (and get a real cell computed against every other module's acl:) even if it's
        # never referenced as a caller anywhere and has no acl: block of its own.
        self._write("cam1", "class: pyobs.modules.camera.BaseCamera\nacl:\n  allow:\n    scheduler: '*'\n")
        self._write("telescope", "class: pyobs.modules.telescope.BaseTelescope\n")  # open, never a caller
        matrix = services.build_acl_matrix()
        self.assertEqual(set(matrix["callers"]), {"scheduler", "cam1", "telescope"})
        # cam1's acl: only mentions "scheduler" -- but "telescope" and "cam1" (itself) must
        # still each get a real, correctly-denied cell rather than being missing entirely.
        cam1_cells = self._row(matrix, "cam1")["cells"]
        self.assertEqual(cam1_cells["telescope"]["kind"], "denied")
        self.assertEqual(cam1_cells["cam1"]["kind"], "denied")

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


# ── services.create_module ───────────────────────────────────────────────────────

class CreateModuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self._settings = override_settings(PYOBS_CONFIG_DIR=str(self.tmp_path))
        self._settings.enable()

    def tearDown(self):
        self._settings.disable()
        self.tmp.cleanup()

    def test_creates_minimal_starter_yaml(self):
        services.create_module("camera2")
        self.assertIn("camera2", services.list_modules())
        content = (self.tmp_path / "camera2.yaml").read_text()
        self.assertIn("class:", content)

    def test_refuses_invalid_name(self):
        with self.assertRaises(ValueError):
            services.create_module("bad name!")
        self.assertEqual(services.list_modules(), [])

    def test_refuses_if_already_exists(self):
        (self.tmp_path / "camera2.yaml").write_text("class: pyobs.modules.camera.BaseCamera\n")
        with self.assertRaises(FileExistsError):
            services.create_module("camera2")
        # the existing file must survive untouched, not get clobbered with the starter template
        self.assertEqual((self.tmp_path / "camera2.yaml").read_text(), "class: pyobs.modules.camera.BaseCamera\n")

    def test_creates_config_dir_if_missing(self):
        missing_dir = self.tmp_path / "does-not-exist-yet"
        with override_settings(PYOBS_CONFIG_DIR=str(missing_dir)):
            services.create_module("camera2")
            self.assertTrue((missing_dir / "camera2.yaml").exists())


# ── services.save_comm_password / find_modules_sharing_comm_user ────────────────

class SaveCommPasswordTests(unittest.TestCase):
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

    def test_replaces_password_preserving_anchor_merge_key(self):
        # Matches this box's own real telescope.yaml shape exactly.
        self._write(
            "telescope",
            "_comm_defaults: &comm\n  class: pyobs.comm.xmpp.XmppComm\n  domain: localhost\n"
            "class: pyobs.modules.telescope.BaseTelescope\n"
            "comm:\n  <<: *comm\n  user: telescope\n  password: pyobs\n",
        )
        updated = services.save_comm_password("telescope", "newpass123")
        self.assertEqual(updated, ["telescope"])
        raw = self._read("telescope")
        self.assertIn("password: newpass123", raw)
        self.assertIn("<<: *comm", raw)  # anchor merge key survives, not flattened
        self.assertIn("user: telescope", raw)

    def test_updates_every_module_sharing_the_same_comm_user(self):
        """DEV_EJABBERD_INTEGRATION.md's own real-world case: a _test copy reusing a real
        module's identity. A password change must not leave one of them stale."""
        self._write("camera", "class: pyobs.modules.camera.BaseCamera\ncomm:\n  user: shared_id\n  password: old\n")
        self._write("_test", "class: pyobs.modules.camera.BaseCamera\ncomm:\n  user: shared_id\n  password: old\n")
        updated = services.save_comm_password("shared_id", "newpass")
        self.assertEqual(sorted(updated), ["_test", "camera"])
        self.assertIn("password: newpass", self._read("camera"))
        self.assertIn("password: newpass", self._read("_test"))

    def test_unrelated_lines_and_other_modules_untouched(self):
        self._write("camera", "class: pyobs.modules.camera.BaseCamera\ncomm:\n  user: camera\n  password: old\n")
        self._write("telescope", "class: pyobs.modules.telescope.BaseTelescope\ncomm:\n  user: telescope\n  password: untouched\n")
        services.save_comm_password("camera", "newpass")
        self.assertIn("class: pyobs.modules.camera.BaseCamera", self._read("camera"))
        self.assertIn("password: untouched", self._read("telescope"))

    def test_password_value_is_yaml_quoted_safely(self):
        self._write("camera", "class: pyobs.modules.camera.BaseCamera\ncomm:\n  user: camera\n  password: old\n")
        services.save_comm_password("camera", "a: weird, value")
        _, password, _ = services.get_resolved_comm("camera")
        self.assertEqual(password, "a: weird, value")

    def test_no_module_has_this_comm_user_raises(self):
        self._write("camera", "class: pyobs.modules.camera.BaseCamera\ncomm:\n  user: camera\n  password: old\n")
        with self.assertRaises(ValueError):
            services.save_comm_password("nonexistent_identity", "newpass")

    def test_refuses_when_comm_comes_from_shared_fragment(self):
        self._write("comm.shared", "comm:\n  user: camera\n  password: old\n")
        self._write("camera", "class: pyobs.modules.camera.BaseCamera\n{include comm.shared.yaml}\n")
        with self.assertRaises(ValueError):
            services.save_comm_password("camera", "newpass")
        # nothing written -- shared fragment untouched
        self.assertIn("password: old", self._read("comm.shared"))

    def test_all_or_nothing_across_shared_identity_when_one_source_is_shared(self):
        """One of two modules sharing an identity has comm: from a shared fragment -- must
        refuse before writing to *either* module, not just the one that's actually shared."""
        self._write("comm.shared", "comm:\n  user: shared_id\n  password: old\n")
        self._write("camera", "class: pyobs.modules.camera.BaseCamera\ncomm:\n  user: shared_id\n  password: old\n")
        self._write("_test", "class: pyobs.modules.camera.BaseCamera\n{include comm.shared.yaml}\n")
        with self.assertRaises(ValueError):
            services.save_comm_password("shared_id", "newpass")
        self.assertIn("password: old", self._read("camera"))
        self.assertIn("password: old", self._read("comm.shared"))

    def test_find_modules_sharing_comm_user(self):
        self._write("camera", "class: pyobs.modules.camera.BaseCamera\ncomm:\n  user: shared_id\n  password: old\n")
        self._write("_test", "class: pyobs.modules.camera.BaseCamera\ncomm:\n  user: shared_id\n  password: old\n")
        self._write("telescope", "class: pyobs.modules.telescope.BaseTelescope\ncomm:\n  user: telescope\n  password: old\n")
        self._write("filecache", "class: pyobs.modules.utils.HttpFileCache\n")
        self.assertEqual(sorted(services.find_modules_sharing_comm_user("shared_id")), ["_test", "camera"])
        self.assertEqual(services.find_modules_sharing_comm_user("nonexistent_identity"), [])

    def test_build_comm_user_map(self):
        self._write("camera", "class: pyobs.modules.camera.BaseCamera\ncomm:\n  user: shared_id\n  password: old\n")
        self._write("_test", "class: pyobs.modules.camera.BaseCamera\ncomm:\n  user: shared_id\n  password: old\n")
        self._write("telescope", "class: pyobs.modules.telescope.BaseTelescope\ncomm:\n  user: telescope\n  password: old\n")
        self._write("filecache", "class: pyobs.modules.utils.HttpFileCache\n")
        mapping = services.build_comm_user_map()
        self.assertEqual(sorted(mapping["shared_id"]), ["_test", "camera"])
        self.assertEqual(mapping["telescope"], ["telescope"])
        self.assertNotIn("filecache", mapping)  # no comm.user at all -- not a key, not a value


# ── services.merge_acl_matrices ─────────────────────────────────────────────────

class MergeAclMatricesTests(unittest.TestCase):
    def _row(self, matrix: dict, host: str, name: str) -> dict:
        return next(r for r in matrix["targets"] if r["host"] == host and r["name"] == name)

    def test_tags_rows_with_their_host(self):
        local = {"targets": [{"name": "cam1", "acl": None, "source": None, "open": True, "error": None, "cells": {}}], "callers": []}
        remote = {"targets": [{"name": "telescope", "acl": None, "source": None, "open": True, "error": None, "cells": {}}], "callers": []}
        merged = services.merge_acl_matrices([("localhost", local), ("MONETS", remote)])
        self.assertEqual(self._row(merged, "localhost", "cam1")["host"], "localhost")
        self.assertEqual(self._row(merged, "MONETS", "telescope")["host"], "MONETS")

    def test_caller_union_spans_hosts(self):
        local = {
            "targets": [{"name": "cam1", "acl": {"allow": {"scheduler": "*"}}, "source": None, "open": False, "error": None, "cells": {}}],
            "callers": ["scheduler"],
        }
        remote = {
            "targets": [{"name": "telescope", "acl": {"deny": ["rogue-client"]}, "source": None, "open": False, "error": None, "cells": {}}],
            "callers": ["rogue-client"],
        }
        merged = services.merge_acl_matrices([("localhost", local), ("MONETS", remote)])
        self.assertEqual(set(merged["callers"]), {"scheduler", "rogue-client"})

    def test_cells_recomputed_against_global_caller_union(self):
        """A row from one host must still get a cell for a caller that only appears on a
        different host -- the host that resolved this row's acl: never saw that caller."""
        local = {
            "targets": [{"name": "cam1", "acl": {"allow": {"scheduler": "*"}}, "source": None, "open": False, "error": None, "cells": {}}],
            "callers": ["scheduler"],
        }
        remote = {
            "targets": [{"name": "telescope", "acl": {"deny": ["rogue-client"]}, "source": None, "open": False, "error": None, "cells": {}}],
            "callers": ["rogue-client"],
        }
        merged = services.merge_acl_matrices([("localhost", local), ("MONETS", remote)])
        cam1_cells = self._row(merged, "localhost", "cam1")["cells"]
        self.assertEqual(cam1_cells["scheduler"]["kind"], "all")
        self.assertEqual(cam1_cells["rogue-client"]["kind"], "denied")  # allow-listed, not mentioned -> denied


# ── ejabberd ──────────────────────────────────────────────────────────────────
#
# Fixtures below are the exact responses captured against a real, running ejabberd 24.12-4
# instance during DEV_EJABBERD_INTEGRATION.md's design phase (see that doc's Data layer), not
# invented shapes -- both the HTTP (mod_http_api) and ejabberdctl paths are covered since
# ejabberdctl is a real fallback, not dead code (see modules/ejabberd.py, _use_http).

class EjabberdHttpTests(unittest.TestCase):
    """EJABBERD_API_URL set -> HTTP path. Mocks requests.post's response only; the URL/JSON
    body construction itself is exercised for real (not mocked) via the assertion on what
    _http.post was called with."""

    def setUp(self):
        self._settings = override_settings(
            EJABBERD_API_URL="http://127.0.0.1:5281/api", EJABBERD_DOMAIN="localhost",
        )
        self._settings.enable()

    def tearDown(self):
        self._settings.disable()

    def _mock_response(self, json_body):
        resp = MagicMock()
        resp.json.return_value = json_body
        resp.raise_for_status.return_value = None
        return resp

    @patch("modules.ejabberd._http.post")
    def test_status(self, mock_post):
        mock_post.return_value = self._mock_response(
            "The node ejabberd@localhost is started. Status: started  ejabberd 24.12-4 is running in that node"
        )
        self.assertEqual(ejabberd.status(), "The node ejabberd@localhost is started. Status: started  ejabberd 24.12-4 is running in that node")
        mock_post.assert_called_once_with("http://127.0.0.1:5281/api/status", json={}, timeout=5)

    @patch("modules.ejabberd._http.post")
    def test_stats(self, mock_post):
        mock_post.return_value = self._mock_response(6)
        self.assertEqual(ejabberd.stats("registeredusers"), 6)
        mock_post.assert_called_once_with(
            "http://127.0.0.1:5281/api/stats", json={"name": "registeredusers"}, timeout=5
        )

    @patch("modules.ejabberd._http.post")
    def test_connected_users_info(self, mock_post):
        mock_post.return_value = self._mock_response([{
            "jid": "camera@localhost/pyobs", "connection": "c2s_tls", "ip": "::1", "port": 51918,
            "priority": 0, "node": "ejabberd@localhost", "uptime": 5, "status": "available",
            "resource": "pyobs", "statustext": "",
        }])
        result = ejabberd.connected_users_info()
        self.assertEqual(result[0]["jid"], "camera@localhost/pyobs")
        self.assertEqual(result[0]["resource"], "pyobs")

    @patch("modules.ejabberd._http.post")
    def test_registered_users(self, mock_post):
        mock_post.return_value = self._mock_response(
            ["admin", "camera", "mastermind", "observer", "scheduler", "telescope"]
        )
        self.assertEqual(
            ejabberd.registered_users(),
            ["admin", "camera", "mastermind", "observer", "scheduler", "telescope"],
        )
        mock_post.assert_called_once_with(
            "http://127.0.0.1:5281/api/registered_users", json={"host": "localhost"}, timeout=5
        )

    @patch("modules.ejabberd._http.post")
    def test_check_account_true_and_false(self, mock_post):
        mock_post.return_value = self._mock_response(0)
        self.assertTrue(ejabberd.check_account("camera"))
        mock_post.return_value = self._mock_response(1)
        self.assertFalse(ejabberd.check_account("nonexistent-user-xyz"))

    @patch("modules.ejabberd._http.post")
    def test_get_last_online(self, mock_post):
        mock_post.return_value = self._mock_response(
            {"timestamp": "2026-07-03T17:15:25.464942Z", "status": "ONLINE"}
        )
        self.assertEqual(ejabberd.get_last("camera"), {"timestamp": "2026-07-03T17:15:25.464942Z", "status": "ONLINE"})


class EjabberdCtlFallbackTests(unittest.TestCase):
    """EJABBERD_API_URL empty -> ejabberdctl subprocess path. Raw stdout fixtures are the
    exact text captured from the live instance, including the trailing-tab empty
    statustext field confirmed via `cat -A` (see DEV_EJABBERD_INTEGRATION.md, Data layer)."""

    def setUp(self):
        self._settings = override_settings(EJABBERD_API_URL="", EJABBERDCTL="ejabberdctl", EJABBERD_DOMAIN="localhost")
        self._settings.enable()

    def tearDown(self):
        self._settings.disable()

    def _mock_result(self, stdout="", returncode=0):
        result = MagicMock()
        result.stdout = stdout
        result.returncode = returncode
        return result

    @patch("modules.ejabberd.subprocess.run")
    def test_status(self, mock_run):
        mock_run.return_value = self._mock_result(
            "The node ejabberd@localhost is started with status: started\nejabberd 24.12-4 is running in that node\n"
        )
        self.assertIn("started", ejabberd.status())
        mock_run.assert_called_once_with(
            ["ejabberdctl", "status"], capture_output=True, text=True, timeout=10
        )

    @patch("modules.ejabberd.subprocess.run")
    def test_stats(self, mock_run):
        mock_run.return_value = self._mock_result("6\n")
        self.assertEqual(ejabberd.stats("registeredusers"), 6)

    @patch("modules.ejabberd.subprocess.run")
    def test_connected_users_info_parses_tab_separated_line_with_jid(self, mock_run):
        mock_run.return_value = self._mock_result(
            "camera@localhost/pyobs\tc2s_tls\t::1\t55368\t0\tejabberd@localhost\t23\tavailable\tpyobs\t\n"
        )
        result = ejabberd.connected_users_info()
        self.assertEqual(result, [{
            "jid": "camera@localhost/pyobs", "connection": "c2s_tls", "ip": "::1", "port": 55368,
            "priority": 0, "node": "ejabberd@localhost", "uptime": 23, "status": "available",
            "resource": "pyobs", "statustext": "",
        }])

    @patch("modules.ejabberd.subprocess.run")
    def test_connected_users_info_empty_result_is_not_an_error(self, mock_run):
        mock_run.return_value = self._mock_result("")
        self.assertEqual(ejabberd.connected_users_info(), [])

    @patch("modules.ejabberd.subprocess.run")
    def test_user_sessions_info_parses_tab_separated_line_without_jid(self, mock_run):
        mock_run.return_value = self._mock_result("c2s_tls\t::1\t55368\t0\tejabberd@localhost\t44\tavailable\tpyobs\t\n")
        result = ejabberd.user_sessions_info("camera")
        self.assertEqual(result, [{
            "connection": "c2s_tls", "ip": "::1", "port": 55368, "priority": 0,
            "node": "ejabberd@localhost", "uptime": 44, "status": "available",
            "resource": "pyobs", "statustext": "",
        }])

    @patch("modules.ejabberd.subprocess.run")
    def test_registered_users(self, mock_run):
        mock_run.return_value = self._mock_result("admin\ncamera\nmastermind\nobserver\nscheduler\ntelescope\n")
        self.assertEqual(
            ejabberd.registered_users(),
            ["admin", "camera", "mastermind", "observer", "scheduler", "telescope"],
        )
        mock_run.assert_called_once_with(
            ["ejabberdctl", "registered_users", "localhost"], capture_output=True, text=True, timeout=10
        )

    @patch("modules.ejabberd.subprocess.run")
    def test_get_last_while_online(self, mock_run):
        mock_run.return_value = self._mock_result("2026-07-03T17:15:25.464942Z\tONLINE\n")
        self.assertEqual(ejabberd.get_last("camera"), {"timestamp": "2026-07-03T17:15:25.464942Z", "status": "ONLINE"})

    @patch("modules.ejabberd.subprocess.run")
    def test_get_last_freeform_disconnect_reason_not_a_fixed_enum(self, mock_run):
        mock_run.return_value = self._mock_result("2026-06-16T18:14:02Z\tStream reset by peer\n")
        self.assertEqual(
            ejabberd.get_last("scheduler"),
            {"timestamp": "2026-06-16T18:14:02Z", "status": "Stream reset by peer"},
        )

    @patch("modules.ejabberd.subprocess.run")
    def test_check_account_true(self, mock_run):
        mock_run.return_value = self._mock_result(returncode=0)
        self.assertTrue(ejabberd.check_account("camera"))

    @patch("modules.ejabberd.subprocess.run")
    def test_check_account_false(self, mock_run):
        mock_run.return_value = self._mock_result(stdout="Error: false\n", returncode=1)
        self.assertFalse(ejabberd.check_account("nonexistent-user-xyz"))


# ── ejabberd.py write commands ────────────────────────────────────────────────
#
# Fixtures are the exact stdout/returncode captured live against a real ejabberd 24.12-4
# instance, using a disposable test account created and fully removed afterward -- see
# DEV_EJABBERD_USER_MANAGEMENT.md's "Verified live" table. Not mod_http_api -- these commands
# are ejabberdctl-only by design (see that doc's Transport decision), so EJABBERD_API_URL is
# irrelevant here; still set to "" to make that explicit rather than rely on the default.

class EjabberdWriteCommandTests(unittest.TestCase):
    def setUp(self):
        self._settings = override_settings(EJABBERD_API_URL="", EJABBERDCTL="ejabberdctl", EJABBERD_DOMAIN="localhost")
        self._settings.enable()

    def tearDown(self):
        self._settings.disable()

    def _mock_result(self, stdout="", returncode=0):
        result = MagicMock()
        result.stdout = stdout
        result.returncode = returncode
        return result

    @patch("modules.ejabberd.subprocess.run")
    def test_register_success(self, mock_run):
        mock_run.return_value = self._mock_result("User newuser@localhost successfully registered\n", 0)
        ejabberd.register("newuser", "somepassword")
        mock_run.assert_called_once_with(
            ["ejabberdctl", "register", "newuser", "localhost", "somepassword"],
            capture_output=True, text=True, timeout=10,
        )

    @patch("modules.ejabberd.subprocess.run")
    def test_register_conflict_raises_with_ejabberds_own_message(self, mock_run):
        mock_run.return_value = self._mock_result(
            "Error: conflict: User newuser@localhost already registered\n", 1
        )
        with self.assertRaises(ValueError) as ctx:
            ejabberd.register("newuser", "somepassword")
        self.assertIn("already registered", str(ctx.exception))

    @patch("modules.ejabberd.subprocess.run")
    def test_change_password_success_has_empty_stdout(self, mock_run):
        # Verified live: unlike ejabberdctl help's own example (which shows a printed 'ok'),
        # this ejabberd version prints nothing on success -- empty stdout is the success case,
        # not a sign the call didn't go through.
        mock_run.return_value = self._mock_result("", 0)
        ejabberd.change_password("newuser", "newpassword")

    @patch("modules.ejabberd.subprocess.run")
    def test_change_password_nonexistent_user_raises_with_erlang_tuple_message(self, mock_run):
        mock_run.return_value = self._mock_result('{not_found,"unknown_user"}\n', 1)
        with self.assertRaises(ValueError) as ctx:
            ejabberd.change_password("nonexistent-user-xyz", "newpassword")
        self.assertIn("not_found", str(ctx.exception))

    @patch("modules.ejabberd.subprocess.run")
    def test_ban_account_success(self, mock_run):
        mock_run.return_value = self._mock_result("", 0)
        ejabberd.ban_account("newuser", "policy violation")
        mock_run.assert_called_once_with(
            ["ejabberdctl", "ban_account", "newuser", "localhost", "policy violation"],
            capture_output=True, text=True, timeout=10,
        )

    @patch("modules.ejabberd.subprocess.run")
    def test_unban_account_success(self, mock_run):
        mock_run.return_value = self._mock_result("", 0)
        ejabberd.unban_account("newuser")

    @patch("modules.ejabberd.subprocess.run")
    def test_unregister_success(self, mock_run):
        mock_run.return_value = self._mock_result("", 0)
        ejabberd.unregister("newuser")

    @patch("modules.ejabberd.subprocess.run")
    def test_unregister_nonexistent_user_is_silently_idempotent_not_an_error(self, mock_run):
        """Verified live: ejabberd itself doesn't distinguish "removed" from "was never
        there" -- exit 0, empty output either way. Callers needing that distinction must
        check_account first; unregister's own result can't tell them."""
        mock_run.return_value = self._mock_result("", 0)
        ejabberd.unregister("never-existed-xyz")  # must not raise

    @patch("modules.ejabberd.subprocess.run")
    def test_get_ban_details_when_banned_parses_tab_separated_fields(self, mock_run):
        mock_run.return_value = self._mock_result(
            "reason\tsecond verification ban\n"
            "bandate\t2026-07-04T09:27:15.202186Z\n"
            "lastdate\t2026-07-04T09:24:35Z\n"
            "lastreason\tRegistered but didn't login\n"
        )
        self.assertEqual(ejabberd.get_ban_details("newuser"), {
            "reason": "second verification ban",
            "bandate": "2026-07-04T09:27:15.202186Z",
            "lastdate": "2026-07-04T09:24:35Z",
            "lastreason": "Registered but didn't login",
        })

    @patch("modules.ejabberd.subprocess.run")
    def test_get_ban_details_when_not_banned_returns_none(self, mock_run):
        mock_run.return_value = self._mock_result("")
        self.assertIsNone(ejabberd.get_ban_details("newuser"))

    @patch("modules.ejabberd.subprocess.run")
    def test_kick_session_success_has_empty_stdout(self, mock_run):
        # Verified live against a real connected session: empty stdout/stderr, exit 0 on
        # success -- same silent-rescode pattern as change_password, despite ejabberdctl's
        # own help text example showing a printed 'ok'.
        mock_run.return_value = self._mock_result("", 0)
        ejabberd.kick_session("newuser", "pyobs", "Kicked via pyobs-web-admin")
        mock_run.assert_called_once_with(
            ["ejabberdctl", "kick_session", "newuser", "localhost", "pyobs", "Kicked via pyobs-web-admin"],
            capture_output=True, text=True, timeout=10,
        )

    @patch("modules.ejabberd.subprocess.run")
    def test_kick_session_failure_raises(self, mock_run):
        # The failure path itself wasn't exercised live (only success was, against a real
        # session) -- this just confirms the generic raise-on-nonzero-exit wiring works,
        # not a specific verified error message shape.
        mock_run.return_value = self._mock_result("error", 1)
        with self.assertRaises(ValueError):
            ejabberd.kick_session("never-connected-xyz", "pyobs", "test")


class EjabberdPathSelectionTests(unittest.TestCase):
    """Which transport gets used is decided purely by whether EJABBERD_API_URL is set, not
    by probing/falling back on HTTP failure -- see modules.ejabberd._use_http's docstring
    for why (ejabberdctl is a fallback for un-configured hosts, not for real HTTP errors,
    which should surface rather than be silently masked)."""

    @override_settings(EJABBERD_API_URL="http://127.0.0.1:5281/api")
    def test_http_used_when_api_url_set(self):
        self.assertTrue(ejabberd._use_http())

    @override_settings(EJABBERD_API_URL="")
    def test_ctl_used_when_api_url_empty(self):
        self.assertFalse(ejabberd._use_http())


# ── pyobsd config auto-detection (see DEV_JOURNALD_LOGS.md) ──────────────────────────

class PyobsdAutoDetectTests(unittest.TestCase):
    """_log_backend()'s auto-detection reads the same global config file pyobsd itself
    reads (pyobs-core/pyobs/cli/_cli.py's CLI._load_config) -- these tests point
    services._PYOBSD_CONFIG_CANDIDATES at a controlled temp path instead of the real
    ~/.config/pyobs.yaml /etc/pyobs.yaml /opt/pyobs/storage/pyobs.yaml locations, so results
    don't depend on whatever happens to exist on the machine running the tests."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.candidate = str(self.tmp_path / "pyobs.yaml")
        self._patch = patch.object(services, "_PYOBSD_CONFIG_CANDIDATES", [self.candidate])
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmp.cleanup()

    def _write(self, content: str) -> None:
        Path(self.candidate).write_text(content)

    def test_no_candidate_file_returns_empty(self):
        self.assertEqual(services._pyobsd_config(), {})

    def test_reads_pyobsd_section(self):
        self._write("pyobsd:\n  syslog: true\n  log_level: debug\n")
        self.assertEqual(services._pyobsd_config(), {"syslog": True, "log_level": "debug"})

    def test_missing_pyobsd_section_returns_empty(self):
        self._write("some_other_section:\n  key: value\n")
        self.assertEqual(services._pyobsd_config(), {})

    def test_malformed_yaml_returns_empty_not_crash(self):
        self._write("pyobsd: [this is not: valid yaml structure\n")
        self.assertEqual(services._pyobsd_config(), {})

    def test_first_existing_candidate_wins(self):
        second = str(self.tmp_path / "second.yaml")
        Path(second).write_text("pyobsd:\n  syslog: true\n")
        with patch.object(services, "_PYOBSD_CONFIG_CANDIDATES", [self.candidate, second]):
            self._write("pyobsd:\n  syslog: false\n")
            self.assertEqual(services._pyobsd_config(), {"syslog": False})

    @override_settings(PYOBS_LOG_BACKEND=None)
    def test_log_backend_defaults_to_file_when_no_config_and_no_override(self):
        self.assertEqual(services._log_backend(), "file")

    @override_settings(PYOBS_LOG_BACKEND=None)
    def test_log_backend_auto_detects_journald(self):
        self._write("pyobsd:\n  syslog: true\n")
        self.assertEqual(services._log_backend(), "journald")

    @override_settings(PYOBS_LOG_BACKEND=None)
    def test_log_backend_auto_detects_file_when_syslog_false(self):
        self._write("pyobsd:\n  syslog: false\n")
        self.assertEqual(services._log_backend(), "file")

    @override_settings(PYOBS_LOG_BACKEND="file")
    def test_explicit_setting_overrides_auto_detected_journald(self):
        self._write("pyobsd:\n  syslog: true\n")
        self.assertEqual(services._log_backend(), "file")

    @override_settings(PYOBS_LOG_BACKEND="journald")
    def test_explicit_setting_overrides_auto_detected_file(self):
        self._write("pyobsd:\n  syslog: false\n")
        self.assertEqual(services._log_backend(), "journald")


# ── journald log backend (see DEV_JOURNALD_LOGS.md) ─────────────────────────────────

class StartModuleLogBackendTests(unittest.TestCase):
    """start_module()'s only journald-related job is choosing --syslog vs --log-file --
    everything else (pid file, --log-level, config arg) is unchanged either way, per
    DEV_JOURNALD_LOGS.md's Design, "What doesn't change"."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        (self.tmp_path / "config").mkdir()
        (self.tmp_path / "run").mkdir()
        (self.tmp_path / "log").mkdir()
        (self.tmp_path / "config" / "camera.yaml").write_text("class: pyobs.modules.camera.BaseCamera\n")
        self._settings = override_settings(
            PYOBS_CONFIG_DIR=str(self.tmp_path / "config"),
            PYOBS_RUN_DIR=str(self.tmp_path / "run"),
            PYOBS_LOG_DIR=str(self.tmp_path / "log"),
            PYOBS_EXEC="pyobs",
            PYOBS_LOG_LEVEL="info",
        )
        self._settings.enable()

    def tearDown(self):
        self._settings.disable()
        self.tmp.cleanup()

    def _run_side_effect(self, pid_file: Path, pid: int = 4242):
        def _run(args, **kwargs):
            pid_file.write_text(str(pid))
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result
        return _run

    @override_settings(PYOBS_LOG_BACKEND="file")
    @patch("modules.services._is_alive", return_value=True)
    @patch("modules.services.subprocess.run")
    def test_file_backend_passes_log_file_not_syslog(self, mock_run, _mock_alive):
        pid_file = self.tmp_path / "run" / "camera.pid"
        mock_run.side_effect = self._run_side_effect(pid_file)
        ok, msg = services.start_module("camera")
        self.assertTrue(ok)
        args = mock_run.call_args[0][0]
        self.assertIn("--log-file", args)
        self.assertNotIn("--syslog", args)
        self.assertEqual(args[-1], str(self.tmp_path / "config" / "camera.yaml"))

    @override_settings(PYOBS_LOG_BACKEND="journald")
    @patch("modules.services._is_alive", return_value=True)
    @patch("modules.services.subprocess.run")
    def test_journald_backend_passes_syslog_not_log_file(self, mock_run, _mock_alive):
        pid_file = self.tmp_path / "run" / "camera.pid"
        mock_run.side_effect = self._run_side_effect(pid_file)
        ok, msg = services.start_module("camera")
        self.assertTrue(ok)
        args = mock_run.call_args[0][0]
        self.assertIn("--syslog", args)
        self.assertNotIn("--log-file", args)
        self.assertEqual(args[-1], str(self.tmp_path / "config" / "camera.yaml"))
        self.assertEqual(list((self.tmp_path / "log").glob("*.log")), [])


class LogBackendJournaldTests(unittest.TestCase):
    """Fixtures are real `journalctl -o json` lines, captured by instantiating the exact
    handler class pyobs/application.py builds and emitting real records through it (see
    DEV_JOURNALD_LOGS.md, Design) -- an invented JSON shape would have missed the real surprise
    these caught: pyobs journals CRITICAL as PRIORITY 0, not the naively-expected 2."""

    _DEBUG_ENTRY = (
        '{"_GID":"1000","_BOOT_ID":"c64abca0faac4631899bda2bedcdc028","_RUNTIME_SCOPE":"system",'
        '"_SYSTEMD_UNIT":"user@1000.service","MESSAGE_ID":"bfa904f2902437eb8e3a2d4fa47e0f39",'
        '"RELATIVE_USEC":"360920240","_COMM":"python3","CODE_FILE":"camera.py","CODE":"camera.None:42",'
        '"CODE_LINE":"42","_EXE":"/usr/bin/python3.14","__SEQNUM":"803506",'
        '"_MACHINE_ID":"36f4b5eac84c44deae61f9b10c0b5dbd","PROCESS_NAME":"MainProcess",'
        '"_SYSTEMD_USER_UNIT":"app-pycharm@bc5ad7f66bbe4bcb9767b927877e084f.service",'
        '"_SYSTEMD_USER_SLICE":"app.slice","_CAP_EFFECTIVE":"0","SYSLOG_IDENTIFIER":"pyobs",'
        '"_HOSTNAME":"husserLaptop","__REALTIME_TIMESTAMP":"1783144498389717",'
        '"_CMDLINE":"/opt/pyobs/venv/bin/python3 -","_AUDIT_SESSION":"4","_SYSTEMD_SLICE":"user-1000.slice",'
        '"_AUDIT_LOGINUID":"1000","_TRANSPORT":"journal","PID":"535522","_PID":"535522",'
        '"MESSAGE":"camera_verify_test camera.py:42 debug line",'
        '"__MONOTONIC_TIMESTAMP":"101244893036",'
        '"_SYSTEMD_CGROUP":"/user.slice/user-1000.slice/user@1000.service/app.slice/app-pycharm@bc5ad7f66bbe4bcb9767b927877e084f.service",'
        '"CREATED_USEC":"1783144498389476",'
        '"__CURSOR":"s=ca9321337fb04d6f8365c542905d8539;i=c42b2;b=c64abca0faac4631899bda2bedcdc028;m=1792aa776c;t=655c2ae68e2d5;x=53f39c8c4dc5a4c8",'
        '"THREAD_NAME":"MainThread","EXTRA_PYOBS_MODULE":"camera_verify_test",'
        '"_SYSTEMD_INVOCATION_ID":"3ff770e2105a45a0885b7d8dcf16679c","SYSLOG_FACILITY":"23",'
        '"_SYSTEMD_OWNER_UID":"1000","_SOURCE_REALTIME_TIMESTAMP":"1783144498389601",'
        '"LOGGER_NAME":"journald_verify_test","MESSAGE_RAW":"debug line","THREAD_ID":"138519519175168",'
        '"PRIORITY":"7","_UID":"1000","__SEQNUM_ID":"ca9321337fb04d6f8365c542905d8539",'
        '"CODE_MODULE":"camera","PYOBS_MODULE":"camera_verify_test"}'
    )

    _CRITICAL_ENTRY = (
        '{"_MACHINE_ID":"36f4b5eac84c44deae61f9b10c0b5dbd","PID":"535522","_AUDIT_SESSION":"4",'
        '"_UID":"1000","_AUDIT_LOGINUID":"1000","CODE_LINE":"42","__MONOTONIC_TIMESTAMP":"101244894241",'
        '"EXTRA_PYOBS_MODULE":"camera_verify_test","CREATED_USEC":"1783144498389720",'
        '"SYSLOG_IDENTIFIER":"pyobs","MESSAGE_ID":"8bea426d6424387bab814e125313660d",'
        '"__SEQNUM":"803510","PRIORITY":"0","__REALTIME_TIMESTAMP":"1783144498390922",'
        '"__CURSOR":"s=ca9321337fb04d6f8365c542905d8539;i=c42b6;b=c64abca0faac4631899bda2bedcdc028;m=1792aa7c21;t=655c2ae68e78a;x=342c1a819294758",'
        '"_HOSTNAME":"husserLaptop","_CAP_EFFECTIVE":"0","_SYSTEMD_OWNER_UID":"1000",'
        '"MESSAGE_RAW":"critical line","THREAD_NAME":"MainThread","_SYSTEMD_SLICE":"user-1000.slice",'
        '"_SYSTEMD_USER_SLICE":"app.slice","CODE_FILE":"camera.py",'
        '"_SYSTEMD_INVOCATION_ID":"3ff770e2105a45a0885b7d8dcf16679c","_SYSTEMD_UNIT":"user@1000.service",'
        '"_BOOT_ID":"c64abca0faac4631899bda2bedcdc028","_CMDLINE":"/opt/pyobs/venv/bin/python3 -",'
        '"_PID":"535522","MESSAGE":"camera_verify_test camera.py:42 critical line",'
        '"_RUNTIME_SCOPE":"system","_SOURCE_REALTIME_TIMESTAMP":"1783144498389742",'
        '"THREAD_ID":"138519519175168","PROCESS_NAME":"MainProcess","CODE_MODULE":"camera",'
        '"_TRANSPORT":"journal","LOGGER_NAME":"journald_verify_test","PYOBS_MODULE":"camera_verify_test",'
        '"CODE":"camera.None:42","SYSLOG_FACILITY":"23","_COMM":"python3",'
        '"_SYSTEMD_USER_UNIT":"app-pycharm@bc5ad7f66bbe4bcb9767b927877e084f.service",'
        '"__SEQNUM_ID":"ca9321337fb04d6f8365c542905d8539","_GID":"1000"}'
    )

    def _mock_result(self, stdout):
        result = MagicMock()
        result.stdout = stdout
        return result

    @override_settings(PYOBS_LOG_BACKEND="journald")
    @patch("modules.services.subprocess.run")
    def test_get_logs_reconstructs_file_shaped_lines_from_real_captured_json(self, mock_run):
        mock_run.return_value = self._mock_result(self._DEBUG_ENTRY + "\n" + self._CRITICAL_ENTRY + "\n")
        lines = services.get_logs("camera_verify_test", lines=300)
        # Derived the same way the code does (datetime.fromtimestamp is local-TZ-dependent,
        # matching the file backend's own asctime-based lines) rather than a hardcoded wall
        # clock string, which would be wrong under a different process TZ (e.g. Django's test
        # runner forces TZ=UTC regardless of the machine's own timezone).
        debug_ts = datetime.fromtimestamp(1783144498389717 / 1_000_000)
        critical_ts = datetime.fromtimestamp(1783144498390922 / 1_000_000)
        self.assertEqual(lines, [
            f"{debug_ts:%Y-%m-%d %H:%M:%S} [DEBUG] (camera_verify_test) camera.py:42 debug line",
            f"{critical_ts:%Y-%m-%d %H:%M:%S} [CRITICAL] (camera_verify_test) camera.py:42 critical line",
        ])
        mock_run.assert_called_once_with(
            ["journalctl", "SYSLOG_IDENTIFIER=pyobs", "PYOBS_MODULE=camera_verify_test",
             "-n", "300", "-o", "json", "--no-pager"],
            capture_output=True, text=True,
        )

    @override_settings(PYOBS_LOG_BACKEND="journald")
    @patch("modules.services.subprocess.run")
    def test_get_logs_filter_str_applies_after_reconstruction(self, mock_run):
        mock_run.return_value = self._mock_result(self._DEBUG_ENTRY + "\n" + self._CRITICAL_ENTRY + "\n")
        lines = services.get_logs("camera_verify_test", filter_str="critical")
        self.assertEqual(len(lines), 1)
        self.assertIn("critical line", lines[0])

    @override_settings(PYOBS_LOG_BACKEND="journald")
    @patch("modules.services.subprocess.run")
    def test_get_logs_empty_journal_returns_empty_list(self, mock_run):
        mock_run.return_value = self._mock_result("")
        self.assertEqual(services.get_logs("nonexistent_module"), [])

    @override_settings(PYOBS_LOG_BACKEND="journald")
    @patch("modules.services.subprocess.run")
    def test_get_log_stats_counts_by_priority_not_by_reparsing_text(self, mock_run):
        mock_run.return_value = self._mock_result(self._DEBUG_ENTRY + "\n" + self._CRITICAL_ENTRY + "\n")
        counts = services.get_log_stats("camera_verify_test")
        self.assertEqual(counts, {"DEBUG": 1, "INFO": 0, "WARNING": 0, "ERROR": 0, "CRITICAL": 1})
        mock_run.assert_called_once_with(
            ["journalctl", "SYSLOG_IDENTIFIER=pyobs", "PYOBS_MODULE=camera_verify_test",
             "--since", "-24h", "-o", "json", "--no-pager"],
            capture_output=True, text=True,
        )

    @override_settings(PYOBS_LOG_BACKEND="journald")
    @patch("modules.services.subprocess.run")
    def test_get_logs_strips_prefix_when_code_file_is_a_full_path(self, mock_run):
        """Regression test for a real bug caught by live testing, not by the other fixtures
        here: logging_journald's CODE_FILE is record.pathname (a full path), but pyobs's own
        journal formatter builds MESSAGE's "<module> <file>:<line> " prefix from
        %(filename)s (just the basename) -- the earlier fixtures above used a bare
        "camera.py" for CODE_FILE, which accidentally already equaled its own basename and
        so didn't exercise this mismatch. A real running module's CODE_FILE is a full path,
        which caught the bug live: the prefix was never stripped, so lines came out with
        the file:line info doubled."""
        entry = json.loads(self._DEBUG_ENTRY)
        entry["CODE_FILE"] = "/home/husser/code/pyobs/pyobs-core/pyobs/application.py"
        entry["MESSAGE"] = "camera_verify_test application.py:42 debug line"
        mock_run.return_value = self._mock_result(json.dumps(entry) + "\n")
        lines = services.get_logs("camera_verify_test")
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].count("application.py:42"), 1)
        self.assertTrue(lines[0].endswith("application.py:42 debug line"))

    @patch("modules.services.subprocess.run")
    def test_file_backend_uses_tail_not_journalctl(self, mock_run):
        """PYOBS_LOG_BACKEND="file" (the default) must keep routing to `tail`, not
        `journalctl` -- confirms the new branch didn't disturb the existing path."""
        with tempfile.TemporaryDirectory() as tmp:
            log_file = Path(tmp) / "camera.log"
            log_file.write_text("2026-07-04 08:00:00 [INFO] (camera) x.py:1 hello\n")
            mock_run.return_value = MagicMock(stdout="2026-07-04 08:00:00 [INFO] (camera) x.py:1 hello\n")
            with override_settings(PYOBS_LOG_DIR=tmp, PYOBS_LOG_BACKEND="file"):
                services.get_logs("camera")
            mock_run.assert_called_once_with(
                ["tail", "-n", "300", str(log_file)], capture_output=True, text=True
            )


# ── get_all_logs ──────────────────────────────────────────────────────────────

class GetAllLogsTests(unittest.TestCase):
    _CAMERA_ENTRY = (
        '{"SYSLOG_IDENTIFIER":"pyobs","PYOBS_MODULE":"camera","PRIORITY":"6",'
        '"__REALTIME_TIMESTAMP":"1783144498000000","CODE_FILE":"camera.py","CODE_LINE":"1",'
        '"MESSAGE":"camera camera.py:1 from camera"}'
    )
    _TELESCOPE_ENTRY = (
        '{"SYSLOG_IDENTIFIER":"pyobs","PYOBS_MODULE":"telescope","PRIORITY":"6",'
        '"__REALTIME_TIMESTAMP":"1783144499000000","CODE_FILE":"telescope.py","CODE_LINE":"2",'
        '"MESSAGE":"telescope telescope.py:2 from telescope"}'
    )

    def _mock_result(self, stdout):
        result = MagicMock()
        result.stdout = stdout
        return result

    @override_settings(PYOBS_LOG_BACKEND="journald")
    @patch("modules.services.subprocess.run")
    def test_journald_no_names_omits_module_filter(self, mock_run):
        mock_run.return_value = self._mock_result(self._CAMERA_ENTRY + "\n" + self._TELESCOPE_ENTRY + "\n")
        lines = services.get_all_logs(lines=300)
        self.assertEqual(len(lines), 2)
        mock_run.assert_called_once_with(
            ["journalctl", "SYSLOG_IDENTIFIER=pyobs", "-n", "300", "-o", "json", "--no-pager"],
            capture_output=True, text=True,
        )

    @override_settings(PYOBS_LOG_BACKEND="journald")
    @patch("modules.services.subprocess.run")
    def test_journald_names_are_ored_via_repeated_field(self, mock_run):
        mock_run.return_value = self._mock_result(self._CAMERA_ENTRY + "\n" + self._TELESCOPE_ENTRY + "\n")
        services.get_all_logs(names=["camera", "telescope"], lines=50)
        mock_run.assert_called_once_with(
            ["journalctl", "SYSLOG_IDENTIFIER=pyobs", "PYOBS_MODULE=camera", "PYOBS_MODULE=telescope",
             "-n", "50", "-o", "json", "--no-pager"],
            capture_output=True, text=True,
        )

    @override_settings(PYOBS_LOG_BACKEND="journald")
    @patch("modules.services.subprocess.run")
    def test_journald_empty_names_list_means_none_selected_not_all(self, mock_run):
        lines = services.get_all_logs(names=[], lines=300)
        self.assertEqual(lines, [])
        mock_run.assert_not_called()

    @patch("modules.services.subprocess.run")
    def test_file_backend_empty_names_list_returns_nothing(self, mock_run):
        lines = services.get_all_logs(names=[], lines=300)
        self.assertEqual(lines, [])
        mock_run.assert_not_called()

    def test_file_backend_merges_and_sorts_across_modules_by_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "camera.log").write_text(
                "2026-07-04 08:00:00 [INFO] (camera) x.py:1 hello camera\n"
                "2026-07-04 08:00:02 [INFO] (camera) x.py:2 world camera\n"
            )
            (Path(tmp) / "telescope.log").write_text(
                "2026-07-04 08:00:01 [INFO] (telescope) y.py:1 hello telescope\n"
            )
            with override_settings(PYOBS_LOG_DIR=tmp, PYOBS_LOG_BACKEND="file"):
                lines = services.get_all_logs(names=["camera", "telescope"], lines=300)
            self.assertEqual(lines, [
                "2026-07-04 08:00:00 [INFO] (camera) x.py:1 hello camera",
                "2026-07-04 08:00:01 [INFO] (telescope) y.py:1 hello telescope",
                "2026-07-04 08:00:02 [INFO] (camera) x.py:2 world camera",
            ])

    def test_merge_log_lines_combines_and_trims_multiple_already_ordered_lists(self):
        # Exercises the same helper views.api_all_logs uses to combine each hub host's own
        # already-fetched result into one fleet-wide view -- one list per "host" here, though
        # the function itself has no notion of hosts, just ordered line lists.
        host_a = [
            "2026-07-04 08:00:00 [INFO] (camera) x.py:1 hello",
            "2026-07-04 08:00:03 [INFO] (camera) x.py:2 world",
        ]
        host_b = ["2026-07-04 08:00:01 [INFO] (telescope) y.py:1 hi"]
        merged = services.merge_log_lines([host_a, host_b], lines=300)
        self.assertEqual(merged, [
            "2026-07-04 08:00:00 [INFO] (camera) x.py:1 hello",
            "2026-07-04 08:00:01 [INFO] (telescope) y.py:1 hi",
            "2026-07-04 08:00:03 [INFO] (camera) x.py:2 world",
        ])

    def test_merge_log_lines_trims_to_overall_last_n(self):
        merged = services.merge_log_lines([
            [f"2026-07-04 08:00:{i:02d} [INFO] (a) x.py:1 line{i}" for i in range(5)],
        ], lines=2)
        self.assertEqual(merged, [
            "2026-07-04 08:00:03 [INFO] (a) x.py:1 line3",
            "2026-07-04 08:00:04 [INFO] (a) x.py:1 line4",
        ])

    def test_file_backend_no_names_defaults_to_list_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "camera.yaml").write_text("class: pyobs.modules.Module\n")
            (Path(tmp) / "camera.log").write_text("2026-07-04 08:00:00 [INFO] (camera) x.py:1 hello camera\n")
            with override_settings(PYOBS_CONFIG_DIR=tmp, PYOBS_LOG_DIR=tmp, PYOBS_LOG_BACKEND="file"):
                lines = services.get_all_logs(lines=300)
            self.assertEqual(lines, ["2026-07-04 08:00:00 [INFO] (camera) x.py:1 hello camera"])

    def test_filter_str_applies_after_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "camera.log").write_text(
                "2026-07-04 08:00:00 [INFO] (camera) x.py:1 hello camera\n"
            )
            (Path(tmp) / "telescope.log").write_text(
                "2026-07-04 08:00:01 [INFO] (telescope) y.py:1 hello telescope\n"
            )
            with override_settings(PYOBS_LOG_DIR=tmp, PYOBS_LOG_BACKEND="file"):
                lines = services.get_all_logs(names=["camera", "telescope"], lines=300, filter_str="telescope")
            self.assertEqual(lines, ["2026-07-04 08:00:01 [INFO] (telescope) y.py:1 hello telescope"])


# ── _tag_host (fleet-wide All Logs cross-host tagging) ────────────────────────

class TagHostTests(unittest.TestCase):
    def test_inserts_host_tag_right_after_leading_timestamp(self):
        # Regression test for a real bug caught by live cross-host testing: an earlier
        # version of api_all_logs forwarded bare module names ("dome2") to a remote host's
        # own api_all_logs, which now expects "host:module" tokens -- the remote silently
        # dropped anything without a colon, so a selected remote module's logs vanished
        # entirely. That bug was in the *forwarding* params, not this tagging helper, but
        # this test locks in the tag's own placement so a client-side timestamp parse
        # (which requires the timestamp to lead the line) keeps working once lines from
        # multiple hosts are merged into one view.
        line = "2026-07-04 09:00:00 [INFO] (camera1) x.py:1 hello"
        self.assertEqual(
            _tag_host(line, "spoke1"),
            "2026-07-04 09:00:00 [spoke1] [INFO] (camera1) x.py:1 hello",
        )

    def test_falls_back_to_prefix_when_no_leading_timestamp(self):
        self.assertEqual(_tag_host("no timestamp here", "spoke1"), "[spoke1] no timestamp here")


# ── Package version selection (Packages page) ──────────────────────────────────

class SelectLatestVersionTests(unittest.TestCase):
    # Regression coverage for a real production case: a host had pyobs-core installed as
    # "2.0.0.dev11" (an in-progress pre-release of an unreleased 2.0.0), while PyPI's
    # info.version -- "the latest stable release" -- was "1.54.0". The original
    # implementation compared against info.version directly, which (1) never surfaces a
    # newer prerelease like "2.0.0.dev13" that's actually available, and would have (2)
    # flagged "1.54.0" as an "update" even though it's older than the installed dev build,
    # were it not for _is_update_available's separate PEP 440 comparison. Confirmed live
    # against a real installation with `pip install --upgrade --dry-run --report`: pip's own
    # resolver leaves an already-installed pre-release alone entirely (offers nothing at
    # all, not even "1.54.0") unless --pre is passed -- so "what counts as latest" here must
    # mirror pip's own pre-release policy, not just PyPI's info.version field.

    def test_installed_prerelease_sees_newer_prerelease(self):
        available = ["1.54.0", "2.0.0.dev10", "2.0.0.dev11", "2.0.0.dev13"]
        self.assertEqual(services._select_latest_version(available, "2.0.0.dev11"), "2.0.0.dev13")

    def test_installed_stable_ignores_prereleases(self):
        available = ["1.50.0", "1.54.0", "2.0.0.dev13"]
        self.assertEqual(services._select_latest_version(available, "1.50.0"), "1.54.0")

    def test_installed_stable_already_latest_ignores_newer_prerelease(self):
        available = ["1.54.0", "2.0.0.dev13"]
        self.assertEqual(services._select_latest_version(available, "1.54.0"), "1.54.0")

    def test_no_versions_available_returns_none(self):
        self.assertIsNone(services._select_latest_version([], "1.0.0"))

    def test_unparseable_version_strings_are_skipped(self):
        self.assertEqual(services._select_latest_version(["not-a-version", "1.2.3"], "1.0.0"), "1.2.3")


class IsUpdateAvailableTests(unittest.TestCase):
    def test_installed_prerelease_ahead_of_stable_latest_is_not_flagged(self):
        # Same production case as SelectLatestVersionTests -- even if "latest" somehow ended
        # up as an older stable release, this must never say an "update" is available for a
        # dev build that's already ahead of it.
        self.assertFalse(services._is_update_available("2.0.0.dev11", "1.54.0"))

    def test_genuinely_newer_version_is_flagged(self):
        self.assertTrue(services._is_update_available("1.50.0", "1.54.0"))

    def test_same_version_is_not_flagged(self):
        self.assertFalse(services._is_update_available("1.54.0", "1.54.0"))

    def test_none_latest_is_not_flagged(self):
        self.assertFalse(services._is_update_available("1.54.0", None))


# ── PYOBS_MANAGED_PACKAGES (extras + non-pyobs packages on the Packages page) ───

class NormalizePackageNameTests(unittest.TestCase):
    def test_hyphen_underscore_dot_all_equivalent(self):
        self.assertEqual(services._normalize_package_name("pyobs-core"), "pyobs-core")
        self.assertEqual(services._normalize_package_name("pyobs_core"), "pyobs-core")
        self.assertEqual(services._normalize_package_name("Pyobs.Core"), "pyobs-core")


class ManagedPackageSpecsTests(unittest.TestCase):
    def test_extras_spec_parsed_by_bare_name(self):
        with override_settings(PYOBS_MANAGED_PACKAGES=["pyobs-core[full]"]):
            self.assertEqual(services._managed_package_specs(), {"pyobs-core": "pyobs-core[full]"})

    def test_bare_non_pyobs_name_is_its_own_spec(self):
        with override_settings(PYOBS_MANAGED_PACKAGES=["my-custom-driver"]):
            self.assertEqual(services._managed_package_specs(), {"my-custom-driver": "my-custom-driver"})

    def test_lookup_key_is_normalized(self):
        # An operator listing "pyobs_core[full]" (underscore) must still match pip's own
        # "pyobs-core" (hyphen) spelling of the installed package's name.
        with override_settings(PYOBS_MANAGED_PACKAGES=["pyobs_core[full]"]):
            self.assertIn("pyobs-core", services._managed_package_specs())

    def test_malformed_entry_is_skipped_not_raised(self):
        with override_settings(PYOBS_MANAGED_PACKAGES=["not a valid spec!!"]):
            self.assertEqual(services._managed_package_specs(), {})

    def test_empty_by_default(self):
        with override_settings(PYOBS_MANAGED_PACKAGES=[]):
            self.assertEqual(services._managed_package_specs(), {})


class InstallSpecForTests(unittest.TestCase):
    def test_uses_configured_extras_spec(self):
        with override_settings(PYOBS_MANAGED_PACKAGES=["pyobs-core[full]"]):
            self.assertEqual(services._install_spec_for("pyobs-core"), "pyobs-core[full]")

    def test_falls_back_to_bare_name_when_unmanaged(self):
        with override_settings(PYOBS_MANAGED_PACKAGES=[]):
            self.assertEqual(services._install_spec_for("pyobs-core"), "pyobs-core")


class ListPyobsPackagesManagedTests(unittest.TestCase):
    """list_pyobs_packages must still only report what's *actually installed* -- the
    PYOBS_MANAGED_PACKAGES setting only ever widens the filter over pip's own report, never
    invents an entry pip didn't return."""

    def _mock_pip_list(self, packages):
        result = MagicMock()
        result.returncode = 0
        result.stdout = json.dumps(packages)
        return result

    @patch("modules.services.subprocess.run")
    def test_non_pyobs_managed_package_included_if_installed(self, mock_run):
        mock_run.return_value = self._mock_pip_list([
            {"name": "pyobs-core", "version": "2.0.0.dev11"},
            {"name": "my-custom-driver", "version": "1.0.0"},
            {"name": "numpy", "version": "2.4.6"},
        ])
        with override_settings(PYOBS_MANAGED_PACKAGES=["my-custom-driver"]):
            names = {p["name"] for p in services.list_pyobs_packages()}
        self.assertEqual(names, {"pyobs-core", "my-custom-driver"})

    @patch("modules.services.subprocess.run")
    def test_managed_but_not_installed_is_not_invented(self, mock_run):
        mock_run.return_value = self._mock_pip_list([
            {"name": "pyobs-core", "version": "2.0.0.dev11"},
        ])
        with override_settings(PYOBS_MANAGED_PACKAGES=["my-custom-driver"]):
            names = {p["name"] for p in services.list_pyobs_packages()}
        self.assertEqual(names, {"pyobs-core"})


class UpdatePackageManagedTests(unittest.TestCase):
    def _mock_result(self, returncode=0, stdout="ok"):
        result = MagicMock()
        result.returncode = returncode
        result.stdout = stdout
        result.stderr = ""
        return result

    @patch("modules.services.subprocess.run")
    def test_uses_configured_extras_spec_in_pip_args(self, mock_run):
        mock_run.return_value = self._mock_result()
        with override_settings(PYOBS_MANAGED_PACKAGES=["pyobs-core[full]"]):
            services.update_package("pyobs-core", "1.54.0")
        args = mock_run.call_args[0][0]
        self.assertEqual(args[-1], "pyobs-core[full]")

    @patch("modules.services.subprocess.run")
    def test_non_pyobs_managed_package_is_allowed(self, mock_run):
        mock_run.return_value = self._mock_result()
        with override_settings(PYOBS_MANAGED_PACKAGES=["my-custom-driver"]):
            ok, _ = services.update_package("my-custom-driver", "1.0.0")
        self.assertTrue(ok)
        mock_run.assert_called_once()

    @patch("modules.services.subprocess.run")
    def test_unmanaged_non_pyobs_package_is_refused(self, mock_run):
        with override_settings(PYOBS_MANAGED_PACKAGES=[]):
            ok, message = services.update_package("some-random-package", "1.0.0")
        self.assertFalse(ok)
        self.assertIn("unmanaged", message)
        mock_run.assert_not_called()