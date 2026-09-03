import fcntl
import json
import os
import re
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import psutil
import requests
import yaml
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import AnonymousUser, User
from django.test import Client as DjangoClient
from django.test import RequestFactory, override_settings
from django.test import TestCase as DjangoTestCase
from packaging.version import Version

from modules import ejabberd, services, views
from modules.middleware import HubTokenMiddleware
from modules.pyobs_config import include_parts, pre_process_yaml, reload_anchors
from modules.views import _tag_host
from pyobs_web_admin.authentication.admin_sync import sync_admin_user

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
        self._settings = override_settings(PYOBS_CONFIG_DIR=str(self.tmp_path), PYOBS_CONFIG_GIT_ENABLED=False)
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
        self._settings = override_settings(PYOBS_CONFIG_DIR=str(self.tmp_path), PYOBS_CONFIG_GIT_ENABLED=False)
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


# ── services.get_module_class / build_module_classes (issue #65) ────────────────

class GetModuleClassTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self._settings = override_settings(PYOBS_CONFIG_DIR=str(self.tmp_path), PYOBS_CONFIG_GIT_ENABLED=False)
        self._settings.enable()

    def tearDown(self):
        self._settings.disable()
        self.tmp.cleanup()

    def _write(self, name: str, content: str) -> None:
        (self.tmp_path / f"{name}.yaml").write_text(content)

    def test_missing_module_returns_none(self):
        self.assertIsNone(services.get_module_class("nope"))

    def test_no_class_key_returns_none(self):
        self._write("cam1", "comm:\n  user: camera\n")
        self.assertIsNone(services.get_module_class("cam1"))

    def test_class_defined_locally(self):
        self._write("cam1", "class: pyobs.modules.camera.BaseCamera\n")
        self.assertEqual(services.get_module_class("cam1"), "pyobs.modules.camera.BaseCamera")

    def test_class_via_include(self):
        self._write("base.shared", "class: pyobs.modules.camera.BaseCamera\n")
        self._write("cam1", "{include base.shared.yaml}\n")
        self.assertEqual(services.get_module_class("cam1"), "pyobs.modules.camera.BaseCamera")

    def test_broken_config_returns_none_not_raise(self):
        self._write("cam1", "class: [unterminated\n")
        self.assertIsNone(services.get_module_class("cam1"))


class BuildModuleClassesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self._settings = override_settings(PYOBS_CONFIG_DIR=str(self.tmp_path), PYOBS_CONFIG_GIT_ENABLED=False)
        self._settings.enable()

    def tearDown(self):
        self._settings.disable()
        self.tmp.cleanup()

    def _write(self, name: str, content: str) -> None:
        (self.tmp_path / f"{name}.yaml").write_text(content)

    def test_maps_every_module_with_a_class(self):
        self._write("cam1", "class: pyobs.modules.camera.BaseCamera\n")
        self._write("tel1", "class: pyobs.modules.telescope.BaseTelescope\n")
        self.assertEqual(
            services.build_module_classes(),
            {"cam1": "pyobs.modules.camera.BaseCamera", "tel1": "pyobs.modules.telescope.BaseTelescope"},
        )

    def test_omits_modules_with_no_resolvable_class(self):
        self._write("cam1", "class: pyobs.modules.camera.BaseCamera\n")
        self._write("broken", "class: [unterminated\n")
        self.assertEqual(services.build_module_classes(), {"cam1": "pyobs.modules.camera.BaseCamera"})

    def test_no_modules_returns_empty_dict(self):
        self.assertEqual(services.build_module_classes(), {})


# ── services.merge_module_classes ───────────────────────────────────────────────

class MergeModuleClassesTests(unittest.TestCase):
    def test_tags_rows_with_their_host(self):
        merged = services.merge_module_classes([
            ("localhost", {"cam1": "pyobs.modules.camera.BaseCamera"}),
            ("MONETS", {"telescope": "pyobs.modules.telescope.BaseTelescope"}),
        ])
        self.assertIn({"name": "cam1", "class": "pyobs.modules.camera.BaseCamera", "host": "localhost"}, merged)
        self.assertIn(
            {"name": "telescope", "class": "pyobs.modules.telescope.BaseTelescope", "host": "MONETS"}, merged
        )

    def test_same_named_module_on_two_hosts_becomes_two_rows(self):
        """No collision arbitration -- disambiguated by host, same choice merge_acl_matrices
        makes for ACL rows, rather than one host's entry silently overwriting the other's."""
        merged = services.merge_module_classes([
            ("localhost", {"cam1": "pyobs.modules.camera.BaseCamera"}),
            ("MONETS", {"cam1": "pyobs.modules.camera.Sbig"}),
        ])
        self.assertEqual(len(merged), 2)
        self.assertIn({"name": "cam1", "class": "pyobs.modules.camera.BaseCamera", "host": "localhost"}, merged)
        self.assertIn({"name": "cam1", "class": "pyobs.modules.camera.Sbig", "host": "MONETS"}, merged)

    def test_empty_input_returns_empty_list(self):
        self.assertEqual(services.merge_module_classes([]), [])

    def test_host_with_no_modules_contributes_nothing(self):
        merged = services.merge_module_classes([("localhost", {}), ("MONETS", {"cam1": "pyobs.modules.camera.Sbig"})])
        self.assertEqual(merged, [{"name": "cam1", "class": "pyobs.modules.camera.Sbig", "host": "MONETS"}])


# ── services.get_comm_user ────────────────────────────────────────────────────

class GetCommUserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self._settings = override_settings(PYOBS_CONFIG_DIR=str(self.tmp_path), PYOBS_CONFIG_GIT_ENABLED=False)
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
        signal ejabberd-integration.md uses to skip modules that were never expected to
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

    def test_comm_user_via_include_of_missing_file_returns_none(self):
        """A dangling {include} (fragment deleted/renamed after a module's config referenced
        it) must not crash -- previously pre_process_yaml's bare open() raised
        FileNotFoundError straight out of get_resolved_comm, which is called directly from
        views like the dashboard's status list with no try/except, so one module with a
        broken include used to take down the whole fleet view."""
        self._write(
            "cam1",
            "class: pyobs.modules.camera.BaseCamera\n{include comm.shared.yaml}\n",
        )
        self.assertIsNone(services.get_comm_user("cam1"))
        self.assertEqual(services.get_resolved_comm("cam1"), (None, None, None))

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
        included, comes from a bare top-level {include} -- ejabberd-user-management.md's
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
        self._settings = override_settings(PYOBS_CONFIG_DIR=str(self.tmp_path), PYOBS_CONFIG_GIT_ENABLED=False)
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
        self._settings = override_settings(PYOBS_CONFIG_DIR=str(self.tmp_path), PYOBS_CONFIG_GIT_ENABLED=False)
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
        self._settings = override_settings(PYOBS_CONFIG_DIR=str(self.tmp_path), PYOBS_CONFIG_GIT_ENABLED=False)
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
        self._settings = override_settings(PYOBS_CONFIG_DIR=str(self.tmp_path), PYOBS_CONFIG_GIT_ENABLED=False)
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
        """ejabberd-integration.md's own real-world case: a _test copy reusing a real
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
# instance during ejabberd-integration.md's design phase (see that doc's Data layer), not
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
    statustext field confirmed via `cat -A` (see ejabberd-integration.md, Data layer)."""

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
# ejabberd-user-management.md's "Verified live" table. Not mod_http_api -- these commands
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


# ── pyobsd config auto-detection (see journald-logs.md) ──────────────────────────

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


# ── journald log backend (see journald-logs.md) ─────────────────────────────────

class StartModuleLogBackendTests(unittest.TestCase):
    """start_module()'s only journald-related job is choosing --syslog vs --log-file --
    everything else (pid file, --log-level, config arg) is unchanged either way, per
    journald-logs.md's Design, "What doesn't change"."""

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
            PYOBS_CONFIG_GIT_ENABLED=False,
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
    journald-logs.md, Design) -- an invented JSON shape would have missed the real surprise
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

    def setUp(self):
        # Seed the version-detection cache so _journald_module_tag() doesn't shell out to
        # `pip list` on every call (which would both slow these tests down and add an
        # unmocked-shaped extra subprocess.run call, breaking assert_called_once_with below) --
        # see JournaldModuleTagVersionGateTests for coverage of the version-gating itself.
        services._pyobs_core_version_cache = (time.time(), Version("2.0.0.dev41"))
        self.addCleanup(self._clear_version_cache)

    def _clear_version_cache(self):
        services._pyobs_core_version_cache = None

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
    def test_get_logs_deactivated_module_queries_active_name(self, mock_run):
        # pyobs-core strips leading underscores off the config filename stem before stamping
        # PYOBS_MODULE (f3b20627, "log _test.yaml configs as test"), so a deactivated module
        # started manually for testing ("_startup.yaml") is tagged "startup" in the journal --
        # the query must match that, not the raw "_startup" name.
        mock_run.return_value = self._mock_result("")
        services.get_logs("_startup", lines=300)
        mock_run.assert_called_once_with(
            ["journalctl", "SYSLOG_IDENTIFIER=pyobs", "PYOBS_MODULE=startup",
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
    def test_get_log_stats_deactivated_module_queries_active_name(self, mock_run):
        mock_run.return_value = self._mock_result("")
        services.get_log_stats("_startup")
        mock_run.assert_called_once_with(
            ["journalctl", "SYSLOG_IDENTIFIER=pyobs", "PYOBS_MODULE=startup",
             "--since", "-24h", "-o", "json", "--no-pager"],
            capture_output=True, text=True,
        )

    @override_settings(PYOBS_LOG_BACKEND="journald")
    @patch("modules.services.subprocess.run")
    def test_get_log_stats_since_narrows_window_when_more_recent_than_24h(self, mock_run):
        # A dashboard-supplied "last acknowledged" instant more recent than the standard 24h
        # rollup should become the actual --since cutoff, not just widen/ignore it.
        mock_run.return_value = self._mock_result("")
        since = datetime.now(UTC) - timedelta(hours=1)
        services.get_log_stats("camera_verify_test", since=since)
        mock_run.assert_called_once_with(
            ["journalctl", "SYSLOG_IDENTIFIER=pyobs", "PYOBS_MODULE=camera_verify_test",
             "--since", f"{since:%Y-%m-%d %H:%M:%S} UTC", "-o", "json", "--no-pager"],
            capture_output=True, text=True,
        )

    @override_settings(PYOBS_LOG_BACKEND="journald")
    @patch("modules.services.subprocess.run")
    def test_get_log_stats_since_older_than_24h_falls_back_to_24h(self, mock_run):
        # An ack from days ago shouldn't pull that whole history back into the "unacknowledged"
        # count -- the window is still capped at the standard 24h rollup.
        mock_run.return_value = self._mock_result("")
        since = datetime.now(UTC) - timedelta(days=3)
        services.get_log_stats("camera_verify_test", since=since)
        args = mock_run.call_args[0][0]
        since_arg = args[args.index("--since") + 1]
        self.assertTrue(since_arg.endswith(" UTC"))
        cutoff = datetime.strptime(since_arg[:-len(" UTC")], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        expected = datetime.now(UTC) - timedelta(hours=24)
        self.assertLess(abs((cutoff - expected).total_seconds()), 5)

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

    @override_settings(PYOBS_LOG_BACKEND="journald")
    @patch("modules.services.subprocess.run")
    def test_get_logs_before_adds_until_arg_ahead_of_lines_flag(self, mock_run):
        """The log window's "load older logs" (scroll-to-top) fetch passes the oldest
        currently-loaded line's own timestamp as `before` -- this must become journalctl's
        --until, mirroring get_log_stats' existing --since usage, so "-n" then returns the
        last N entries *at or before* that instant instead of the last N overall."""
        mock_run.return_value = self._mock_result("")
        before = datetime(2026, 7, 15, 10, 0, 0, tzinfo=UTC)
        services.get_logs("camera_verify_test", lines=300, before=before)
        mock_run.assert_called_once_with(
            ["journalctl", "SYSLOG_IDENTIFIER=pyobs", "PYOBS_MODULE=camera_verify_test",
             "--until", "2026-07-15 10:00:00 UTC", "-n", "300", "-o", "json", "--no-pager"],
            capture_output=True, text=True,
        )

    @override_settings(PYOBS_LOG_BACKEND="journald")
    @patch("modules.services.subprocess.run")
    def test_get_logs_since_adds_since_arg_ahead_of_lines_flag(self, mock_run):
        """The time-range start date becomes journalctl's --since, so `-n` returns the last
        N entries *at or after* that instant instead of the last N overall."""
        mock_run.return_value = self._mock_result("")
        since = datetime(2026, 7, 15, 10, 0, 0, tzinfo=UTC)
        services.get_logs("camera_verify_test", lines=300, since=since)
        mock_run.assert_called_once_with(
            ["journalctl", "SYSLOG_IDENTIFIER=pyobs", "PYOBS_MODULE=camera_verify_test",
             "--since", "2026-07-15 10:00:00 UTC", "-n", "300", "-o", "json", "--no-pager"],
            capture_output=True, text=True,
        )

    @override_settings(PYOBS_LOG_BACKEND="journald")
    @patch("modules.services.subprocess.run")
    def test_get_logs_since_and_before_combine_both_bounds(self, mock_run):
        mock_run.return_value = self._mock_result("")
        since = datetime(2026, 7, 15, 9, 0, 0, tzinfo=UTC)
        before = datetime(2026, 7, 15, 10, 0, 0, tzinfo=UTC)
        services.get_logs("camera_verify_test", lines=300, since=since, before=before)
        mock_run.assert_called_once_with(
            ["journalctl", "SYSLOG_IDENTIFIER=pyobs", "PYOBS_MODULE=camera_verify_test",
             "--since", "2026-07-15 09:00:00 UTC", "--until", "2026-07-15 10:00:00 UTC",
             "-n", "300", "-o", "json", "--no-pager"],
            capture_output=True, text=True,
        )

    @override_settings(PYOBS_LOG_BACKEND="journald")
    @patch("modules.services.subprocess.run")
    def test_get_logs_until_adds_until_arg(self, mock_run):
        """The time-range end date (`until`) becomes journalctl's --until, so `-n` returns the
        last N entries *at or before* the end date instead of the last N overall -- without
        this the server kept returning the newest lines and the client-side end filter wiped
        out a past window entirely."""
        mock_run.return_value = self._mock_result("")
        since = datetime(2026, 7, 15, 9, 0, 0, tzinfo=UTC)
        until = datetime(2026, 7, 15, 10, 0, 0, tzinfo=UTC)
        services.get_logs("camera_verify_test", lines=300, since=since, until=until)
        mock_run.assert_called_once_with(
            ["journalctl", "SYSLOG_IDENTIFIER=pyobs", "PYOBS_MODULE=camera_verify_test",
             "--since", "2026-07-15 09:00:00 UTC", "--until", "2026-07-15 10:00:00 UTC",
             "-n", "300", "-o", "json", "--no-pager"],
            capture_output=True, text=True,
        )

    @override_settings(PYOBS_LOG_BACKEND="journald")
    @patch("modules.services.subprocess.run")
    def test_get_logs_before_and_until_tighter_bound_wins(self, mock_run):
        """A page-back cursor (`before`) and an end date (`until`) are both upper bounds --
        journald takes a single --until, so the earlier of the two is used."""
        mock_run.return_value = self._mock_result("")
        since = datetime(2026, 7, 15, 9, 0, 0, tzinfo=UTC)
        before = datetime(2026, 7, 15, 9, 30, 0, tzinfo=UTC)
        until = datetime(2026, 7, 15, 10, 0, 0, tzinfo=UTC)
        services.get_logs("camera_verify_test", lines=300, since=since, before=before, until=until)
        mock_run.assert_called_once_with(
            ["journalctl", "SYSLOG_IDENTIFIER=pyobs", "PYOBS_MODULE=camera_verify_test",
             "--since", "2026-07-15 09:00:00 UTC", "--until", "2026-07-15 09:30:00 UTC",
             "-n", "300", "-o", "json", "--no-pager"],
            capture_output=True, text=True,
        )

    @override_settings(PYOBS_LOG_BACKEND="journald")
    @patch("modules.services.subprocess.run")
    def test_get_logs_until_newer_than_cursor_cursor_wins(self, mock_run):
        mock_run.return_value = self._mock_result("")
        since = datetime(2026, 7, 15, 9, 0, 0, tzinfo=UTC)
        before = datetime(2026, 7, 15, 11, 0, 0, tzinfo=UTC)
        until = datetime(2026, 7, 15, 10, 0, 0, tzinfo=UTC)
        services.get_logs("camera_verify_test", lines=300, since=since, before=before, until=until)
        mock_run.assert_called_once_with(
            ["journalctl", "SYSLOG_IDENTIFIER=pyobs", "PYOBS_MODULE=camera_verify_test",
             "--since", "2026-07-15 09:00:00 UTC", "--until", "2026-07-15 10:00:00 UTC",
             "-n", "300", "-o", "json", "--no-pager"],
            capture_output=True, text=True,
        )

    @override_settings(PYOBS_LOG_BACKEND="journald")
    @patch("modules.services.subprocess.run")
    def test_get_logs_merges_comm_user_identity(self, mock_run):
        """Issue #59: pyobs-core tags PYOBS_MODULE two ways -- most logging uses the config
        file stem, but execute()/BackgroundTask logging uses the module's own comm-derived
        name (the comm user). A module whose comm user differs from its config name therefore
        has lines filed under both identities, and get_logs must query both and merge."""
        with tempfile.TemporaryDirectory() as config_dir:
            Path(config_dir, "cam1.yaml").write_text(
                "class: pyobs.modules.camera.BaseCamera\ncomm:\n  user: camera\n"
            )
            config_stem_entry = (
                '{"SYSLOG_IDENTIFIER":"pyobs","PYOBS_MODULE":"cam1","PRIORITY":"6",'
                '"__REALTIME_TIMESTAMP":"1783144498000000","CODE_FILE":"cam1.py","CODE_LINE":"1",'
                '"MESSAGE":"cam1 cam1.py:1 from config stem"}'
            )
            comm_entry = (
                '{"SYSLOG_IDENTIFIER":"pyobs","PYOBS_MODULE":"camera","PRIORITY":"6",'
                '"__REALTIME_TIMESTAMP":"1783144498500000","CODE_FILE":"camera.py","CODE_LINE":"2",'
                '"MESSAGE":"camera camera.py:2 from comm user"}'
            )
            mock_run.side_effect = [self._mock_result(config_stem_entry + "\n"),
                                    self._mock_result(comm_entry + "\n")]
            with override_settings(PYOBS_CONFIG_DIR=config_dir, PYOBS_CONFIG_GIT_ENABLED=False):
                lines = services.get_logs("cam1", lines=300)
        # Derived the same way the code does (datetime.fromtimestamp is local-TZ-dependent,
        # matching the file backend's own asctime-based lines) -- see the reconstruction test
        # above for why a hardcoded wall-clock string would be wrong.
        config_ts = datetime.fromtimestamp(1783144498000000 / 1_000_000)
        comm_ts = datetime.fromtimestamp(1783144498500000 / 1_000_000)
        self.assertEqual(lines, [
            f"{config_ts:%Y-%m-%d %H:%M:%S} [INFO] (cam1) cam1.py:1 from config stem",
            f"{comm_ts:%Y-%m-%d %H:%M:%S} [INFO] (camera) camera.py:2 from comm user",
        ])
        self.assertEqual(mock_run.call_count, 2)
        self.assertEqual(
            mock_run.call_args_list[0][0][0],
            ["journalctl", "SYSLOG_IDENTIFIER=pyobs", "PYOBS_MODULE=cam1",
             "-n", "300", "-o", "json", "--no-pager"],
        )
        self.assertEqual(
            mock_run.call_args_list[1][0][0],
            ["journalctl", "SYSLOG_IDENTIFIER=pyobs", "PYOBS_MODULE=camera",
             "-n", "300", "-o", "json", "--no-pager"],
        )

    @override_settings(PYOBS_LOG_BACKEND="journald")
    @patch("modules.services.subprocess.run")
    def test_get_logs_comm_user_matching_config_name_queries_once(self, mock_run):
        """The common case -- comm user equals the config stem -- collapses both taggings
        onto one identity and must not double the query."""
        with tempfile.TemporaryDirectory() as config_dir:
            Path(config_dir, "camera.yaml").write_text(
                "class: pyobs.modules.camera.BaseCamera\ncomm:\n  user: camera\n"
            )
            mock_run.return_value = self._mock_result("")
            with override_settings(PYOBS_CONFIG_DIR=config_dir, PYOBS_CONFIG_GIT_ENABLED=False):
                services.get_logs("camera", lines=300)
        mock_run.assert_called_once_with(
            ["journalctl", "SYSLOG_IDENTIFIER=pyobs", "PYOBS_MODULE=camera",
             "-n", "300", "-o", "json", "--no-pager"],
            capture_output=True, text=True,
        )

    @override_settings(PYOBS_LOG_BACKEND="journald")
    @patch("modules.services.subprocess.run")
    def test_get_log_stats_merges_comm_user_identity(self, mock_run):
        """Log stats must sum the comm-user identity's counts too, or a module whose comm
        user differs from its config name undercounts its execute()/BackgroundTask lines."""
        with tempfile.TemporaryDirectory() as config_dir:
            Path(config_dir, "cam1.yaml").write_text(
                "class: pyobs.modules.camera.BaseCamera\ncomm:\n  user: camera\n"
            )
            debug_under_config = self._DEBUG_ENTRY.replace(
                '"PYOBS_MODULE":"camera_verify_test"', '"PYOBS_MODULE":"cam1"'
            )
            critical_under_comm = self._CRITICAL_ENTRY.replace(
                '"PYOBS_MODULE":"camera_verify_test"', '"PYOBS_MODULE":"camera"'
            )
            mock_run.side_effect = [self._mock_result(debug_under_config + "\n"),
                                    self._mock_result(critical_under_comm + "\n")]
            with override_settings(PYOBS_CONFIG_DIR=config_dir, PYOBS_CONFIG_GIT_ENABLED=False):
                counts = services.get_log_stats("cam1")
        self.assertEqual(counts, {"DEBUG": 1, "INFO": 0, "WARNING": 0, "ERROR": 0, "CRITICAL": 1})
        self.assertEqual(mock_run.call_count, 2)
        self.assertEqual(
            mock_run.call_args_list[0][0][0][2], "PYOBS_MODULE=cam1"
        )
        self.assertEqual(
            mock_run.call_args_list[1][0][0][2], "PYOBS_MODULE=camera"
        )

    def test_file_backend_merges_comm_user_log_file(self):
        """File backend: a module whose comm user differs from its config name may also have
        a log file under the comm name (e.g. started under that identity) -- get_logs must
        merge it in, not just read the config-stem file."""
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "cam1.yaml").write_text(
                "class: pyobs.modules.camera.BaseCamera\ncomm:\n  user: camera\n"
            )
            Path(tmp, "cam1.log").write_text(
                "2026-07-04 08:00:00 [INFO] (cam1) x.py:1 from config stem\n"
            )
            Path(tmp, "camera.log").write_text(
                "2026-07-04 08:00:01 [INFO] (camera) y.py:2 from comm user\n"
            )
            with override_settings(PYOBS_CONFIG_DIR=tmp, PYOBS_LOG_DIR=tmp,
                                   PYOBS_LOG_BACKEND="file", PYOBS_CONFIG_GIT_ENABLED=False):
                lines = services.get_logs("cam1", lines=300)
        self.assertEqual(lines, [
            "2026-07-04 08:00:00 [INFO] (cam1) x.py:1 from config stem",
            "2026-07-04 08:00:01 [INFO] (camera) y.py:2 from comm user",
        ])

    def test_file_backend_no_comm_user_queries_only_config_stem_file(self):
        """Without a comm block (confirmed real case: HttpFileCache), the file backend must
        read exactly one file -- the existing behavior, unchanged by the identity merge."""
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "filecache.yaml").write_text("class: pyobs.modules.files.HttpFileCache\n")
            Path(tmp, "filecache.log").write_text(
                "2026-07-04 08:00:00 [INFO] (filecache) x.py:1 hello\n"
            )
            with override_settings(PYOBS_CONFIG_DIR=tmp, PYOBS_LOG_DIR=tmp,
                                   PYOBS_LOG_BACKEND="file", PYOBS_CONFIG_GIT_ENABLED=False):
                lines = services.get_logs("filecache", lines=300)
        self.assertEqual(lines, ["2026-07-04 08:00:00 [INFO] (filecache) x.py:1 hello"])

    @patch("modules.services.subprocess.run")
    def test_file_backend_before_returns_empty_list_not_a_tail(self, mock_run):
        """Without a `since` the file backend still can't page back -- `tail -n` has no
        seek/offset concept to page further back with -- so a bare `before` request reports
        "nothing older available" (empty list) rather than silently re-running the same tail,
        which would look like an infinite scrollback of duplicate lines. With a start date the
        window is bounded by `since`, so `before` paging works (see the since tests below)."""
        with tempfile.TemporaryDirectory() as tmp:
            log_file = Path(tmp) / "camera.log"
            log_file.write_text("2026-07-04 08:00:00 [INFO] (camera) x.py:1 hello\n")
            with override_settings(PYOBS_LOG_DIR=tmp, PYOBS_LOG_BACKEND="file"):
                lines = services.get_logs("camera", before=datetime(2026, 7, 4, tzinfo=UTC))
            self.assertEqual(lines, [])
            mock_run.assert_not_called()

    @patch("modules.services.subprocess.run")
    def test_file_backend_since_filters_tail_to_window(self, mock_run):
        with tempfile.TemporaryDirectory() as tmp:
            log_file = Path(tmp) / "camera.log"
            log_file.write_text(
                "2026-07-04 08:00:00 [INFO] (camera) x.py:1 old\n"
                "2026-07-04 09:00:00 [INFO] (camera) x.py:2 new\n"
            )
            mock_run.return_value = MagicMock(stdout=log_file.read_text())
            since = datetime(2026, 7, 4, 8, 30, 0, tzinfo=UTC)
            with override_settings(PYOBS_LOG_DIR=tmp, PYOBS_LOG_BACKEND="file"):
                lines = services.get_logs("camera", lines=300, since=since)
            self.assertEqual(lines, ["2026-07-04 09:00:00 [INFO] (camera) x.py:2 new"])
            mock_run.assert_called_once_with(
                ["tail", "-n", "300", str(log_file)], capture_output=True, text=True
            )

    def test_file_backend_since_and_before_pages_within_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "camera.log").write_text(
                "2026-07-04 08:00:00 [INFO] (camera) x.py:1 old\n"
                "2026-07-04 09:00:00 [INFO] (camera) x.py:2 mid\n"
                "2026-07-04 10:00:00 [INFO] (camera) x.py:3 new\n"
            )
            since = datetime(2026, 7, 4, 8, 30, 0, tzinfo=UTC)
            before = datetime(2026, 7, 4, 9, 30, 0, tzinfo=UTC)
            with override_settings(PYOBS_LOG_DIR=tmp, PYOBS_LOG_BACKEND="file"):
                lines = services.get_logs("camera", lines=300, since=since, before=before)
            self.assertEqual(lines, ["2026-07-04 09:00:00 [INFO] (camera) x.py:2 mid"])

    @patch("modules.services.subprocess.run")
    def test_file_backend_until_caps_the_tail(self, mock_run):
        """The time-range end date (`until`) bounds the plain tail from above, so the bug
        scenario -- user picks a [since, until] window that ends before the newest activity --
        returns exactly the window's lines instead of the newest N lines (which the old
        client-only end filter then wiped out entirely)."""
        with tempfile.TemporaryDirectory() as tmp:
            log_file = Path(tmp) / "camera.log"
            log_file.write_text(
                "2026-07-04 08:00:00 [INFO] (camera) x.py:1 before window\n"
                "2026-07-04 09:00:00 [INFO] (camera) x.py:2 in window\n"
                "2026-07-04 09:30:00 [INFO] (camera) x.py:3 in window\n"
                "2026-07-04 14:00:00 [INFO] (camera) x.py:4 after window\n"
            )
            mock_run.return_value = MagicMock(stdout=log_file.read_text())
            since = datetime(2026, 7, 4, 8, 30, 0, tzinfo=UTC)
            until = datetime(2026, 7, 4, 10, 0, 0, tzinfo=UTC)
            with override_settings(PYOBS_LOG_DIR=tmp, PYOBS_LOG_BACKEND="file"):
                lines = services.get_logs("camera", lines=300, since=since, until=until)
            self.assertEqual(lines, [
                "2026-07-04 09:00:00 [INFO] (camera) x.py:2 in window",
                "2026-07-04 09:30:00 [INFO] (camera) x.py:3 in window",
            ])
            mock_run.assert_called_once_with(
                ["tail", "-n", "300", str(log_file)], capture_output=True, text=True
            )

    def test_file_backend_until_without_since_filters_tail_to_end_date(self):
        """An end date alone is a plain upper bound -- the last N lines at or before it, no
        page-back semantics involved (unlike a bare `before`, which needs a `since`)."""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "camera.log").write_text(
                "2026-07-04 08:00:00 [INFO] (camera) x.py:1 in window\n"
                "2026-07-04 09:00:00 [INFO] (camera) x.py:2 in window\n"
                "2026-07-04 14:00:00 [INFO] (camera) x.py:3 after window\n"
            )
            until = datetime(2026, 7, 4, 10, 0, 0, tzinfo=UTC)
            with override_settings(PYOBS_LOG_DIR=tmp, PYOBS_LOG_BACKEND="file"):
                lines = services.get_logs("camera", lines=300, until=until)
            self.assertEqual(lines, [
                "2026-07-04 08:00:00 [INFO] (camera) x.py:1 in window",
                "2026-07-04 09:00:00 [INFO] (camera) x.py:2 in window",
            ])

    def test_file_backend_before_and_until_tighter_bound_wins(self):
        """A scroll-to-top page-back (`before`) combined with an end date (`until`): the
        earlier of the two caps the window, so paging back never resurfaces lines newer than
        the end date."""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "camera.log").write_text(
                "2026-07-04 08:00:00 [INFO] (camera) x.py:1 old\n"
                "2026-07-04 09:00:00 [INFO] (camera) x.py:2 mid\n"
                "2026-07-04 09:30:00 [INFO] (camera) x.py:3 late mid\n"
                "2026-07-04 14:00:00 [INFO] (camera) x.py:4 after window\n"
            )
            since = datetime(2026, 7, 4, 8, 30, 0, tzinfo=UTC)
            before = datetime(2026, 7, 4, 9, 20, 0, tzinfo=UTC)   # cursor older than until
            until = datetime(2026, 7, 4, 10, 0, 0, tzinfo=UTC)    # end date
            with override_settings(PYOBS_LOG_DIR=tmp, PYOBS_LOG_BACKEND="file"):
                lines = services.get_logs("camera", lines=300, since=since, before=before, until=until)
            self.assertEqual(lines, ["2026-07-04 09:00:00 [INFO] (camera) x.py:2 mid"])

    def test_file_backend_until_caps_page_back_when_newer_than_cursor(self):
        """The other ordering: until (end date) is newer than the page-back cursor -- the
        cursor still wins as the tighter upper bound."""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "camera.log").write_text(
                "2026-07-04 08:00:00 [INFO] (camera) x.py:1 old\n"
                "2026-07-04 09:00:00 [INFO] (camera) x.py:2 mid\n"
                "2026-07-04 14:00:00 [INFO] (camera) x.py:3 after window\n"
            )
            since = datetime(2026, 7, 4, 8, 30, 0, tzinfo=UTC)
            before = datetime(2026, 7, 4, 15, 0, 0, tzinfo=UTC)   # cursor newer than until
            until = datetime(2026, 7, 4, 10, 0, 0, tzinfo=UTC)    # end date
            with override_settings(PYOBS_LOG_DIR=tmp, PYOBS_LOG_BACKEND="file"):
                lines = services.get_logs("camera", lines=300, since=since, before=before, until=until)
            self.assertEqual(lines, [
                "2026-07-04 09:00:00 [INFO] (camera) x.py:2 mid",
            ])


# ── journald PYOBS_MODULE version gating ─────────────────────────────────────

class JournaldModuleTagVersionGateTests(unittest.TestCase):
    """_journald_module_tag() picks the pre- or post-f3b20627 PYOBS_MODULE tagging convention
    based on the installed pyobs-core version, so a fleet running a mix of old and new
    pyobs-core across hosts still matches each host's own journal correctly."""

    def tearDown(self):
        services._pyobs_core_version_cache = None

    def test_old_version_keeps_raw_name(self):
        services._pyobs_core_version_cache = (time.time(), Version("2.0.0.dev40"))
        self.assertEqual(services._journald_module_tag("_startup"), "_startup")

    def test_new_version_strips_underscore(self):
        services._pyobs_core_version_cache = (time.time(), Version("2.0.0.dev41"))
        self.assertEqual(services._journald_module_tag("_startup"), "startup")

    def test_version_above_cutoff_strips_underscore(self):
        services._pyobs_core_version_cache = (time.time(), Version("2.0.0"))
        self.assertEqual(services._journald_module_tag("_startup"), "startup")

    def test_unknown_version_defaults_to_new_behavior(self):
        # pip lookup failed, or pyobs-core isn't listed at all -- assume current pyobs-core
        # behavior rather than the old one, since that's what any fresh install has.
        services._pyobs_core_version_cache = (time.time(), None)
        self.assertEqual(services._journald_module_tag("_startup"), "startup")

    def test_name_without_leading_underscore_is_unaffected_either_way(self):
        services._pyobs_core_version_cache = (time.time(), Version("2.0.0.dev40"))
        self.assertEqual(services._journald_module_tag("camera"), "camera")
        services._pyobs_core_version_cache = (time.time(), Version("2.0.0.dev41"))
        self.assertEqual(services._journald_module_tag("camera"), "camera")


class PyobsCoreVersionTests(unittest.TestCase):
    """pyobs_core_version() reads the installed pyobs-core version from the same environment
    PYOBS_EXEC runs pyobs in (via list_pyobs_packages' `pip list --format=json`), cached so
    it's not re-shelled-out-to on every journald request."""

    def setUp(self):
        services._pyobs_core_version_cache = None
        self.addCleanup(self._clear_version_cache)

    def _clear_version_cache(self):
        services._pyobs_core_version_cache = None

    def _mock_pip_list(self, packages):
        result = MagicMock()
        result.returncode = 0
        result.stdout = json.dumps(packages)
        return result

    @patch("modules.services.subprocess.run")
    def test_parses_installed_pyobs_core_version(self, mock_run):
        mock_run.return_value = self._mock_pip_list(
            [{"name": "pyobs-core", "version": "2.0.0.dev41"}, {"name": "pyobs-iagvt", "version": "1.0.0"}]
        )
        self.assertEqual(services.pyobs_core_version(), Version("2.0.0.dev41"))

    @patch("modules.services.subprocess.run")
    def test_name_matched_pep503_normalized(self, mock_run):
        mock_run.return_value = self._mock_pip_list([{"name": "pyobs_core", "version": "1.9.0"}])
        self.assertEqual(services.pyobs_core_version(), Version("1.9.0"))

    @patch("modules.services.subprocess.run")
    def test_pyobs_core_not_installed_returns_none(self, mock_run):
        mock_run.return_value = self._mock_pip_list([{"name": "pyobs-iagvt", "version": "1.0.0"}])
        self.assertIsNone(services.pyobs_core_version())

    @patch("modules.services.subprocess.run")
    def test_unparseable_version_string_returns_none(self, mock_run):
        mock_run.return_value = self._mock_pip_list([{"name": "pyobs-core", "version": "not-a-version"}])
        self.assertIsNone(services.pyobs_core_version())

    @patch("modules.services.subprocess.run")
    def test_result_is_cached_not_requeried_within_ttl(self, mock_run):
        mock_run.return_value = self._mock_pip_list([{"name": "pyobs-core", "version": "2.0.0.dev41"}])
        first = services.pyobs_core_version()
        second = services.pyobs_core_version()
        self.assertEqual(first, second)
        mock_run.assert_called_once()

    @patch("modules.services.subprocess.run")
    def test_stale_cache_entry_is_requeried(self, mock_run):
        services._pyobs_core_version_cache = (
            time.time() - services._PYOBS_CORE_VERSION_CACHE_TTL - 1,
            Version("1.0.0"),
        )
        mock_run.return_value = self._mock_pip_list([{"name": "pyobs-core", "version": "2.0.0.dev41"}])
        self.assertEqual(services.pyobs_core_version(), Version("2.0.0.dev41"))


# ── get_module_versions / stale_packages ───────────────────────────────────────

class GetModuleVersionsFileTests(unittest.TestCase):
    """get_module_versions() file backend: `tac | grep -m1` over a real temp log file, same
    unmocked-subprocess convention as GetAllLogsTests' file-backend cases."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        (self.tmp_path / "run").mkdir()
        (self.tmp_path / "log").mkdir()
        self._settings = override_settings(
            PYOBS_RUN_DIR=str(self.tmp_path / "run"),
            PYOBS_LOG_DIR=str(self.tmp_path / "log"),
            PYOBS_LOG_BACKEND="file",
        )
        self._settings.enable()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self._settings.disable()
        self.tmp.cleanup()
        services._module_versions_cache.clear()

    def _write_pid(self, name: str, pid: int) -> None:
        (self.tmp_path / "run" / f"{name}.pid").write_text(str(pid))

    def _write_log(self, name: str, lines: list[str]) -> None:
        (self.tmp_path / "log" / f"{name}.log").write_text("\n".join(lines) + "\n")

    @patch("modules.services._is_alive", return_value=True)
    def test_parses_newest_of_several_lines(self, _mock_alive):
        self._write_pid("camera", 4242)
        self._write_log("camera", [
            "2026-08-15 12:00:00 [INFO] (camera) application.py:379 Loaded pyobs packages: pyobs-core=2.0.0.dev70",
            "2026-08-15 12:00:01 [INFO] (camera) application.py:10 startup",
            "2026-08-15 12:05:00 [INFO] (camera) application.py:379 Loaded pyobs packages: pyobs-core=2.0.0.dev76, pyobs-fli=2.0.0.dev7",
        ])
        self.assertEqual(
            services.get_module_versions("camera"),
            {"pyobs-core": "2.0.0.dev76", "pyobs-fli": "2.0.0.dev7"},
        )

    @patch("modules.services._is_alive", return_value=True)
    def test_no_line_returns_none(self, _mock_alive):
        self._write_pid("camera", 4242)
        self._write_log("camera", ["2026-08-15 12:00:00 [INFO] (camera) application.py:10 startup"])
        self.assertIsNone(services.get_module_versions("camera"))

    @patch("modules.services._is_alive", return_value=True)
    def test_no_log_file_returns_none(self, _mock_alive):
        self._write_pid("camera", 4242)
        self.assertIsNone(services.get_module_versions("camera"))

    def test_not_running_returns_none(self):
        self.assertIsNone(services.get_module_versions("camera"))


class GetModuleVersionsJournaldTests(unittest.TestCase):
    """get_module_versions() journald backend: same substring parse applied to the newest
    matching journal entry's MESSAGE, bounded to the module's own lifetime via --since
    @<create_time>."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        (self.tmp_path / "run").mkdir()
        self._settings = override_settings(
            PYOBS_RUN_DIR=str(self.tmp_path / "run"),
            PYOBS_LOG_BACKEND="journald",
        )
        self._settings.enable()
        # See LogBackendJournaldTests.setUp -- same reasoning: seed the version-detection
        # cache so _journald_module_tag() doesn't shell out to `pip list` too.
        services._pyobs_core_version_cache = (time.time(), Version("2.0.0.dev41"))
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self._settings.disable()
        self.tmp.cleanup()
        services._module_versions_cache.clear()
        services._pyobs_core_version_cache = None

    def _write_pid(self, name: str, pid: int) -> None:
        (self.tmp_path / "run" / f"{name}.pid").write_text(str(pid))

    def _mock_journalctl_result(self, message: str | None):
        result = MagicMock()
        result.stdout = (
            json.dumps({"MESSAGE": message, "__REALTIME_TIMESTAMP": "1783144498389717"}) + "\n" if message else ""
        )
        return result

    @patch("modules.services._is_alive", return_value=True)
    @patch("modules.services.psutil.Process")
    @patch("modules.services.subprocess.run")
    def test_parses_newest_matching_entry(self, mock_run, mock_process_cls, _mock_alive):
        self._write_pid("camera", 4242)
        mock_process_cls.return_value.create_time.return_value = 1700000000.0
        mock_run.return_value = self._mock_journalctl_result(
            "camera application.py:379 Loaded pyobs packages: pyobs-core=2.0.0.dev76, pyobs-fli=2.0.0.dev7"
        )
        self.assertEqual(
            services.get_module_versions("camera"),
            {"pyobs-core": "2.0.0.dev76", "pyobs-fli": "2.0.0.dev7"},
        )
        since = datetime.fromtimestamp(1700000000.0, tz=UTC)
        mock_run.assert_called_once_with(
            ["journalctl", "SYSLOG_IDENTIFIER=pyobs", "PYOBS_MODULE=camera",
             "--since", f"{since:%Y-%m-%d %H:%M:%S} UTC", "--grep", "Loaded pyobs packages: ",
             "-o", "json", "--no-pager"],
            capture_output=True, text=True,
        )

    @patch("modules.services._is_alive", return_value=True)
    @patch("modules.services.psutil.Process")
    @patch("modules.services.subprocess.run")
    def test_no_matching_entry_returns_none(self, mock_run, mock_process_cls, _mock_alive):
        self._write_pid("camera", 4242)
        mock_process_cls.return_value.create_time.return_value = 1700000000.0
        mock_run.return_value = self._mock_journalctl_result(None)
        self.assertIsNone(services.get_module_versions("camera"))

    @patch("modules.services._is_alive", return_value=True)
    @patch("modules.services.psutil.Process")
    def test_process_gone_returns_none_and_clears_cache(self, mock_process_cls, _mock_alive):
        # A different PID than the cached entry forces a cache miss, so the read path
        # actually reaches psutil.Process (a same-PID hit would short-circuit before that).
        self._write_pid("camera", 5000)
        services._module_versions_cache["camera"] = (4242, {"pyobs-core": "2.0.0.dev76"})
        mock_process_cls.side_effect = psutil.NoSuchProcess(5000)
        self.assertIsNone(services.get_module_versions("camera"))
        self.assertNotIn("camera", services._module_versions_cache)


class GetModuleVersionsCacheTests(unittest.TestCase):
    """The version set is fixed for a process's lifetime, so get_module_versions must not
    re-shell-out on every call while the PID is unchanged -- mirrors _process_cache's own
    PID-keyed invalidation (get_module_stats)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        (self.tmp_path / "run").mkdir()
        self._settings = override_settings(PYOBS_RUN_DIR=str(self.tmp_path / "run"), PYOBS_LOG_BACKEND="file")
        self._settings.enable()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self._settings.disable()
        self.tmp.cleanup()
        services._module_versions_cache.clear()

    def _write_pid(self, name: str, pid: int) -> None:
        (self.tmp_path / "run" / f"{name}.pid").write_text(str(pid))

    @patch("modules.services._is_alive", return_value=True)
    @patch("modules.services._get_module_versions_file")
    def test_same_pid_reuses_cached_result_without_requerying(self, mock_get, _mock_alive):
        self._write_pid("camera", 4242)
        mock_get.return_value = {"pyobs-core": "2.0.0.dev76"}
        first = services.get_module_versions("camera")
        second = services.get_module_versions("camera")
        self.assertEqual(first, second)
        mock_get.assert_called_once()

    @patch("modules.services._is_alive", return_value=True)
    @patch("modules.services._get_module_versions_file")
    def test_pid_change_recomputes(self, mock_get, _mock_alive):
        self._write_pid("camera", 4242)
        mock_get.return_value = {"pyobs-core": "2.0.0.dev76"}
        services.get_module_versions("camera")
        self._write_pid("camera", 4300)
        mock_get.return_value = {"pyobs-core": "2.0.0.dev80"}
        second = services.get_module_versions("camera")
        self.assertEqual(second, {"pyobs-core": "2.0.0.dev80"})
        self.assertEqual(mock_get.call_count, 2)

    def test_stopped_module_clears_cache_entry(self):
        services._module_versions_cache["camera"] = (4242, {"pyobs-core": "2.0.0.dev76"})
        self.assertIsNone(services.get_module_versions("camera"))
        self.assertNotIn("camera", services._module_versions_cache)


class StalePackagesTests(unittest.TestCase):
    def test_flags_only_differing_packages(self):
        running = {"pyobs-core": "2.0.0.dev41", "pyobs-fli": "2.0.0.dev7"}
        installed = {"pyobs-core": "2.0.0.dev76", "pyobs-fli": "2.0.0.dev7"}
        self.assertEqual(services.stale_packages(running, installed), ["pyobs-core"])

    def test_empty_list_when_all_match(self):
        versions = {"pyobs-core": "2.0.0.dev76"}
        self.assertEqual(services.stale_packages(versions, versions), [])

    def test_running_only_package_missing_from_installed_counts_as_differing(self):
        # e.g. installed has since dropped a driver the running process still has loaded.
        running = {"pyobs-core": "2.0.0.dev76", "pyobs-fli": "2.0.0.dev7"}
        installed = {"pyobs-core": "2.0.0.dev76"}
        self.assertEqual(services.stale_packages(running, installed), ["pyobs-fli"])

    def test_installed_only_package_the_module_never_loaded_is_not_flagged(self):
        # `installed` is the full host-wide pip list, `running` only what this module actually
        # imported (pyobs-core's loaded_pyobs_packages()) -- a camera module loading
        # pyobs-core+pyobs-fli must not be flagged outdated just because pyobs-telescope also
        # happens to be installed on the same host. Regression test for a real review finding:
        # unioning running/installed names instead of iterating running only.
        running = {"pyobs-core": "2.0.0.dev76"}
        installed = {"pyobs-core": "2.0.0.dev76", "pyobs-telescope": "2.0.0.dev1"}
        self.assertEqual(services.stale_packages(running, installed), [])


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

    def setUp(self):
        # See LogBackendJournaldTests.setUp -- same reasoning.
        services._pyobs_core_version_cache = (time.time(), Version("2.0.0.dev41"))
        self.addCleanup(self._clear_version_cache)

    def _clear_version_cache(self):
        services._pyobs_core_version_cache = None

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
    def test_journald_names_normalizes_deactivated_names_to_active_form(self, mock_run):
        mock_run.return_value = self._mock_result("")
        services.get_all_logs(names=["camera", "_startup"], lines=50)
        mock_run.assert_called_once_with(
            ["journalctl", "SYSLOG_IDENTIFIER=pyobs", "PYOBS_MODULE=camera", "PYOBS_MODULE=startup",
             "-n", "50", "-o", "json", "--no-pager"],
            capture_output=True, text=True,
        )

    @override_settings(PYOBS_LOG_BACKEND="journald")
    @patch("modules.services.subprocess.run")
    def test_journald_expands_selected_module_to_its_comm_identity(self, mock_run):
        """Issue #59: selecting a module in the fleet-wide All Logs view must also fetch its
        comm-user identity, or the module's execute()/BackgroundTask lines (filed under the
        comm user, not the config stem) are missing from its stream."""
        with tempfile.TemporaryDirectory() as config_dir:
            Path(config_dir, "cam1.yaml").write_text(
                "class: pyobs.modules.camera.BaseCamera\ncomm:\n  user: camera\n"
            )
            mock_run.return_value = self._mock_result("")
            with override_settings(PYOBS_CONFIG_DIR=config_dir, PYOBS_CONFIG_GIT_ENABLED=False):
                services.get_all_logs(names=["cam1"], lines=50)
        mock_run.assert_called_once_with(
            ["journalctl", "SYSLOG_IDENTIFIER=pyobs", "PYOBS_MODULE=cam1", "PYOBS_MODULE=camera",
             "-n", "50", "-o", "json", "--no-pager"],
            capture_output=True, text=True,
        )

    @override_settings(PYOBS_LOG_BACKEND="journald")
    @patch("modules.services.subprocess.run")
    def test_journald_shared_comm_user_is_added_once(self, mock_run):
        """Two modules sharing one comm user (a documented real case: _test and camera share
        an identity) must add that identity to the OR once, not once per module."""
        with tempfile.TemporaryDirectory() as config_dir:
            Path(config_dir, "camera.yaml").write_text(
                "class: pyobs.modules.camera.BaseCamera\ncomm:\n  user: shared_id\n"
            )
            Path(config_dir, "_test.yaml").write_text(
                "class: pyobs.modules.camera.BaseCamera\ncomm:\n  user: shared_id\n"
            )
            mock_run.return_value = self._mock_result("")
            with override_settings(PYOBS_CONFIG_DIR=config_dir, PYOBS_CONFIG_GIT_ENABLED=False):
                services.get_all_logs(names=["camera", "_test"], lines=50)
        mock_run.assert_called_once_with(
            ["journalctl", "SYSLOG_IDENTIFIER=pyobs", "PYOBS_MODULE=camera", "PYOBS_MODULE=shared_id",
             "PYOBS_MODULE=test", "-n", "50", "-o", "json", "--no-pager"],
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

    def test_file_backend_get_log_stats_since_narrows_the_24h_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime.now(UTC)
            old_ts = (now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
            recent_ts = (now - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
            (Path(tmp) / "camera.log").write_text(
                f"{old_ts} [WARNING] (camera) x.py:1 old warning\n"
                f"{recent_ts} [WARNING] (camera) x.py:2 recent warning\n"
            )
            with override_settings(PYOBS_LOG_DIR=tmp, PYOBS_LOG_BACKEND="file"):
                self.assertEqual(services.get_log_stats("camera")["WARNING"], 2)
                since = now - timedelta(hours=1)
                self.assertEqual(services.get_log_stats("camera", since=since)["WARNING"], 1)

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
            with override_settings(PYOBS_CONFIG_DIR=tmp, PYOBS_LOG_DIR=tmp, PYOBS_LOG_BACKEND="file", PYOBS_CONFIG_GIT_ENABLED=False):
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

    @override_settings(PYOBS_LOG_BACKEND="journald")
    @patch("modules.services.subprocess.run")
    def test_journald_before_adds_until_arg_ahead_of_lines_flag(self, mock_run):
        mock_run.return_value = self._mock_result("")
        before = datetime(2026, 7, 15, 10, 0, 0, tzinfo=UTC)
        services.get_all_logs(names=["camera"], lines=300, before=before)
        mock_run.assert_called_once_with(
            ["journalctl", "SYSLOG_IDENTIFIER=pyobs", "PYOBS_MODULE=camera",
             "--until", "2026-07-15 10:00:00 UTC", "-n", "300", "-o", "json", "--no-pager"],
            capture_output=True, text=True,
        )

    @patch("modules.services.subprocess.run")
    def test_file_backend_before_returns_empty_list(self, mock_run):
        # Same "no seek/offset to page further back with without a since" limitation as
        # get_logs' own file backend -- see that test's docstring.
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "camera.log").write_text("2026-07-04 08:00:00 [INFO] (camera) x.py:1 hello\n")
            with override_settings(PYOBS_LOG_DIR=tmp, PYOBS_LOG_BACKEND="file"):
                lines = services.get_all_logs(names=["camera"], before=datetime(2026, 7, 4, tzinfo=UTC))
            self.assertEqual(lines, [])
            mock_run.assert_not_called()

    @override_settings(PYOBS_LOG_BACKEND="journald")
    @patch("modules.services.subprocess.run")
    def test_journald_since_adds_since_arg_ahead_of_lines_flag(self, mock_run):
        mock_run.return_value = self._mock_result("")
        since = datetime(2026, 7, 15, 10, 0, 0, tzinfo=UTC)
        services.get_all_logs(names=["camera"], lines=300, since=since)
        mock_run.assert_called_once_with(
            ["journalctl", "SYSLOG_IDENTIFIER=pyobs", "PYOBS_MODULE=camera",
             "--since", "2026-07-15 10:00:00 UTC", "-n", "300", "-o", "json", "--no-pager"],
            capture_output=True, text=True,
        )

    def test_file_backend_since_filters_across_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "camera.log").write_text(
                "2026-07-04 08:00:00 [INFO] (camera) x.py:1 old camera\n"
                "2026-07-04 09:00:00 [INFO] (camera) x.py:2 new camera\n"
            )
            (Path(tmp) / "telescope.log").write_text(
                "2026-07-04 09:30:00 [INFO] (telescope) y.py:1 new telescope\n"
            )
            since = datetime(2026, 7, 4, 8, 30, 0, tzinfo=UTC)
            with override_settings(PYOBS_LOG_DIR=tmp, PYOBS_LOG_BACKEND="file"):
                lines = services.get_all_logs(names=["camera", "telescope"], lines=300, since=since)
            self.assertEqual(lines, [
                "2026-07-04 09:00:00 [INFO] (camera) x.py:2 new camera",
                "2026-07-04 09:30:00 [INFO] (telescope) y.py:1 new telescope",
            ])


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
            "2026-07-04 09:00:00 [INFO] [spoke1] (camera1) x.py:1 hello",
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
            self.assertEqual(
                services._managed_package_specs(),
                {"pyobs-core": services._ManagedSpec("pyobs-core[full]", is_vcs=False)},
            )

    def test_bare_non_pyobs_name_is_its_own_spec(self):
        with override_settings(PYOBS_MANAGED_PACKAGES=["my-custom-driver"]):
            self.assertEqual(
                services._managed_package_specs(),
                {"my-custom-driver": services._ManagedSpec("my-custom-driver", is_vcs=False)},
            )

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

    def test_git_url_spec_is_parsed_and_flagged_vcs(self):
        entry = "pyobs-iagvt[gui] @ git+https://gitlab.gwdg.de/iagvt/pyobs-iagvt.git"
        with override_settings(PYOBS_MANAGED_PACKAGES=[entry]):
            self.assertEqual(
                services._managed_package_specs(),
                {"pyobs-iagvt": services._ManagedSpec(entry, is_vcs=True)},
            )

    def test_bare_and_extras_specs_are_not_flagged_vcs(self):
        with override_settings(PYOBS_MANAGED_PACKAGES=["pyobs-core[full]"]):
            self.assertFalse(services._managed_package_specs()["pyobs-core"].is_vcs)


class InstallSpecForTests(unittest.TestCase):
    def test_uses_configured_extras_spec(self):
        with override_settings(PYOBS_MANAGED_PACKAGES=["pyobs-core[full]"]):
            self.assertEqual(services._install_spec_for("pyobs-core"), "pyobs-core[full]")

    def test_falls_back_to_bare_name_when_unmanaged(self):
        with override_settings(PYOBS_MANAGED_PACKAGES=[]):
            self.assertEqual(services._install_spec_for("pyobs-core"), "pyobs-core")

    def test_uses_configured_git_url_spec(self):
        entry = "pyobs-iagvt[gui] @ git+https://gitlab.gwdg.de/iagvt/pyobs-iagvt.git"
        with override_settings(PYOBS_MANAGED_PACKAGES=[entry]):
            self.assertEqual(services._install_spec_for("pyobs-iagvt"), entry)


class ConfiguredVcsRefTests(unittest.TestCase):
    def test_extracts_ref_pinned_on_git_url(self):
        entry = "pyobs-iagvt[gui] @ git+https://gitlab.gwdg.de/iagvt/pyobs-iagvt.git@develop"
        with override_settings(PYOBS_MANAGED_PACKAGES=[entry]):
            self.assertEqual(services._configured_vcs_ref("pyobs-iagvt"), "develop")

    def test_none_when_url_pins_no_ref(self):
        entry = "pyobs-iagvt[gui] @ git+https://gitlab.gwdg.de/iagvt/pyobs-iagvt.git"
        with override_settings(PYOBS_MANAGED_PACKAGES=[entry]):
            self.assertIsNone(services._configured_vcs_ref("pyobs-iagvt"))

    def test_none_for_ssh_url_with_user_at_host_but_no_pinned_ref(self):
        # Regression: an ssh:// URL's "user@host" auth component was misparsed as a ref
        # delimiter, so a no-ref entry like this returned the repo path as the "ref" and
        # the git remote lookup silently matched nothing (empty ls-remote) -- the Packages
        # page then showed no latest version for the package.
        entry = "pyobs-monet @ git+ssh://git@gitlab.gwdg.de/monet/pyobs-monet.git"
        with override_settings(PYOBS_MANAGED_PACKAGES=[entry]):
            self.assertIsNone(services._configured_vcs_ref("pyobs-monet"))

    def test_extracts_ref_pinned_after_user_at_host_on_ssh_url(self):
        entry = "pyobs-monet @ git+ssh://git@gitlab.gwdg.de/monet/pyobs-monet.git@develop"
        with override_settings(PYOBS_MANAGED_PACKAGES=[entry]):
            self.assertEqual(services._configured_vcs_ref("pyobs-monet"), "develop")

    def test_none_for_non_vcs_spec(self):
        with override_settings(PYOBS_MANAGED_PACKAGES=["pyobs-core[full]"]):
            self.assertIsNone(services._configured_vcs_ref("pyobs-core"))

    def test_none_for_unmanaged_package(self):
        with override_settings(PYOBS_MANAGED_PACKAGES=[]):
            self.assertIsNone(services._configured_vcs_ref("pyobs-iagvt"))


class VcsUpdateStatusTests(unittest.TestCase):
    """Regression coverage for a real prod report: switching pyobs-iagvt's branch in
    PYOBS_MANAGED_PACKAGES showed no update available and left "Reinstall" disabled, because
    the remote lookup used the ref recorded at install time (the *old* branch) instead of the
    newly configured one -- so it kept comparing the old branch against itself."""

    @patch("modules.services._git_remote_commit")
    @patch("modules.services._vcs_direct_url_info")
    def test_branch_switch_in_config_is_detected_even_if_old_ref_unchanged(self, mock_info, mock_remote):
        mock_info.return_value = {
            "url": "https://gitlab.gwdg.de/iagvt/pyobs-iagvt.git",
            "ref": "main",
            "commit_id": "008dae0c" * 5,
        }
        mock_remote.return_value = "abc12345" * 5
        entry = "pyobs-iagvt[gui] @ git+https://gitlab.gwdg.de/iagvt/pyobs-iagvt.git@develop"
        with override_settings(PYOBS_MANAGED_PACKAGES=[entry]):
            status = services._vcs_update_status("pyobs-iagvt")
        mock_remote.assert_called_once_with("https://gitlab.gwdg.de/iagvt/pyobs-iagvt.git", "develop")
        self.assertEqual(status["ref"], "develop")
        self.assertTrue(status["update_available"])

    @patch("modules.services._git_remote_commit")
    @patch("modules.services._vcs_direct_url_info")
    def test_falls_back_to_installed_ref_when_config_pins_none(self, mock_info, mock_remote):
        mock_info.return_value = {
            "url": "https://gitlab.gwdg.de/iagvt/pyobs-iagvt.git",
            "ref": "main",
            "commit_id": "008dae0c" * 5,
        }
        mock_remote.return_value = "008dae0c" * 5
        entry = "pyobs-iagvt[gui] @ git+https://gitlab.gwdg.de/iagvt/pyobs-iagvt.git"
        with override_settings(PYOBS_MANAGED_PACKAGES=[entry]):
            status = services._vcs_update_status("pyobs-iagvt")
        mock_remote.assert_called_once_with("https://gitlab.gwdg.de/iagvt/pyobs-iagvt.git", "main")
        self.assertEqual(status["ref"], "main")
        self.assertFalse(status["update_available"])


class IsVcsManagedTests(unittest.TestCase):
    def test_true_for_git_url_spec(self):
        entry = "pyobs-iagvt[gui] @ git+https://gitlab.gwdg.de/iagvt/pyobs-iagvt.git"
        with override_settings(PYOBS_MANAGED_PACKAGES=[entry]):
            self.assertTrue(services._is_vcs_managed("pyobs-iagvt"))

    def test_false_for_plain_extras_spec(self):
        with override_settings(PYOBS_MANAGED_PACKAGES=["pyobs-core[full]"]):
            self.assertFalse(services._is_vcs_managed("pyobs-core"))

    def test_false_for_unmanaged_package(self):
        with override_settings(PYOBS_MANAGED_PACKAGES=[]):
            self.assertFalse(services._is_vcs_managed("pyobs-core"))


class GetPackageOverviewVcsTests(unittest.TestCase):
    """A git/URL-installed managed package has no PyPI release history, so its overview
    entry must skip the PyPI lookup rather than report a spurious/misleading result."""

    @patch("modules.services._pypi_latest_version")
    @patch("modules.services.list_pyobs_packages")
    def test_pypi_lookup_skipped_for_vcs_package(self, mock_list, mock_latest):
        mock_list.return_value = [{"name": "pyobs-iagvt", "version": "1.0.0"}]
        entry = "pyobs-iagvt[gui] @ git+https://gitlab.gwdg.de/iagvt/pyobs-iagvt.git"
        with override_settings(PYOBS_MANAGED_PACKAGES=[entry]):
            overview = services.get_package_overview()
        mock_latest.assert_not_called()
        self.assertEqual(overview, [{
            "name": "pyobs-iagvt",
            "installed_version": "1.0.0",
            "version": "1.0.0",
            "latest_version": None,
            "update_available": False,
            "vcs": True,
        }])

    @patch("modules.services._pypi_latest_version")
    @patch("modules.services.list_pyobs_packages")
    def test_pypi_lookup_still_used_for_regular_package(self, mock_list, mock_latest):
        mock_list.return_value = [{"name": "pyobs-core", "version": "1.0.0"}]
        mock_latest.return_value = "2.0.0"
        with override_settings(PYOBS_MANAGED_PACKAGES=[]):
            overview = services.get_package_overview()
        mock_latest.assert_called_once_with("pyobs-core", "1.0.0")
        self.assertEqual(overview[0]["vcs"], False)
        self.assertEqual(overview[0]["latest_version"], "2.0.0")
        self.assertTrue(overview[0]["update_available"])


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


class BuildUpdateArgsManagedTests(unittest.TestCase):
    def test_uses_configured_extras_spec_in_pip_args(self):
        with override_settings(PYOBS_MANAGED_PACKAGES=["pyobs-core[full]"]):
            args = services._build_update_args("pyobs-core", "1.54.0")
        self.assertEqual(args[-1], "pyobs-core[full]")

    def test_non_pyobs_managed_package_is_allowed(self):
        with override_settings(PYOBS_MANAGED_PACKAGES=["my-custom-driver"]):
            args = services._build_update_args("my-custom-driver", "1.0.0")
        self.assertEqual(args[-1], "my-custom-driver")

    def test_unmanaged_non_pyobs_package_is_refused(self):
        with override_settings(PYOBS_MANAGED_PACKAGES=[]):
            with self.assertRaises(ValueError) as ctx:
                services._build_update_args("some-random-package", "1.0.0")
        self.assertIn("unmanaged", str(ctx.exception))


class UpdatePackageStartTests(unittest.TestCase):
    """update_package_start spawns detached and returns immediately -- these tests never let a
    real pip run; PYOBS_PIP_EXEC (via PYOBS_EXEC) is stubbed to a fast shell one-liner so the
    background job actually completes within the test, exercising the real file-based state
    machine end to end rather than mocking subprocess.Popen itself."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        (self.tmp_path / "run").mkdir()
        self._settings = override_settings(
            PYOBS_RUN_DIR=str(self.tmp_path / "run"),
            PYOBS_EXEC="/opt/pyobs/venv/bin/pyobs",
            PYOBS_MANAGED_PACKAGES=[],
        )
        self._settings.enable()

    def tearDown(self):
        self._settings.disable()
        self.tmp.cleanup()

    def _wait_for_exit_file(self, timeout=5):
        exit_file = services._pkg_update_exit_file()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if exit_file.exists():
                return
            time.sleep(0.05)
        self.fail("background job did not finish in time")

    @patch("modules.services._pip_exec", return_value="/bin/echo")
    def test_returns_immediately_and_reports_running_then_success(self, _mock_pip):
        ok, message = services.update_package_start("pyobs-core", "1.54.0")
        self.assertTrue(ok)
        self.assertIn("pyobs-core", message)
        self._wait_for_exit_file()
        status = services.get_package_update_status()
        self.assertEqual(status["state"], "success")
        self.assertEqual(status["name"], "pyobs-core")
        self.assertFalse(status["active"])
        self.assertIn("pyobs-core", status["log"])  # /bin/echo prints its argv, incl. the spec

    @patch("modules.services._pip_exec", return_value="/bin/false")
    def test_nonzero_exit_reported_as_failed(self, _mock_pip):
        services.update_package_start("pyobs-core", "1.54.0")
        self._wait_for_exit_file()
        status = services.get_package_update_status()
        self.assertEqual(status["state"], "failed")

    @patch("modules.services._build_update_args", return_value=["/bin/sleep", "1"])
    def test_second_start_refused_while_first_still_running(self, _mock_args):
        ok1, _ = services.update_package_start("pyobs-core", "1.54.0")
        self.assertTrue(ok1)
        status = services.get_package_update_status()
        self.assertTrue(status["active"])
        ok2, message = services.update_package_start("pyobs-fli", "1.0.0")
        self.assertFalse(ok2)
        self.assertIn("pyobs-core", message)
        self._wait_for_exit_file()  # let sleep finish so it doesn't outlive the test

    def test_unmanaged_package_refused_without_spawning(self):
        with override_settings(PYOBS_MANAGED_PACKAGES=[]):
            ok, message = services.update_package_start("some-random-package", "1.0.0")
        self.assertFalse(ok)
        self.assertIn("unmanaged", message)
        self.assertFalse(services._pkg_update_lock_file().exists())

    @patch("modules.services._pip_exec", return_value="/bin/echo")
    def test_lock_records_matching_pid_create_time(self, _mock_pip):
        services.update_package_start("pyobs-core", "1.54.0")
        lock = json.loads(services._pkg_update_lock_file().read_text())
        actual = psutil.Process(lock["pid"]).create_time()
        self.assertAlmostEqual(lock["pid_create_time"], actual, delta=1.0)
        self._wait_for_exit_file()

    @patch("modules.services._build_update_args", return_value=["/bin/sleep", "1"])
    def test_second_start_refused_while_first_holds_the_flock(self, _mock_args):
        # Simulates two requests landing on different gunicorn workers close enough together
        # that the second one's flock() call contends with the first's, rather than finding a
        # fully-written lock file -- the race PR #50 review comment #2 flagged.
        services._run_dir().mkdir(parents=True, exist_ok=True)
        services._pkg_update_lock_file().write_text(json.dumps({"name": "pyobs-core", "pid": 1, "pid_create_time": None}))
        flock_fd = os.open(services._pkg_update_lock_flock_file(), os.O_CREAT | os.O_RDWR)
        fcntl.flock(flock_fd, fcntl.LOCK_EX)
        try:
            ok, message = services.update_package_start("pyobs-fli", "1.0.0")
        finally:
            fcntl.flock(flock_fd, fcntl.LOCK_UN)
            os.close(flock_fd)
        self.assertFalse(ok)
        self.assertIn("pyobs-core", message)


class GetPackageUpdateStatusTests(unittest.TestCase):
    """Exercises the on-disk state machine directly (no real spawn) for states that
    UpdatePackageStartTests can't easily force: no job ever run, interrupted, and reused-pid."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        (self.tmp_path / "run").mkdir()
        self._settings = override_settings(PYOBS_RUN_DIR=str(self.tmp_path / "run"))
        self._settings.enable()

    def tearDown(self):
        self._settings.disable()
        self.tmp.cleanup()

    def test_no_job_ever_run(self):
        status = services.get_package_update_status()
        self.assertEqual(status, {"active": False})

    def test_interrupted_when_pid_gone_and_no_exit_file(self):
        services._pkg_update_lock_file().write_text(json.dumps({"name": "pyobs-core", "pid": 999999999, "started_at": 0}))
        services._pkg_update_log_file().write_text("partial output\n")
        status = services.get_package_update_status()
        self.assertEqual(status["state"], "interrupted")
        self.assertFalse(status["active"])
        self.assertIn("partial output", status["log"])

    def test_interrupted_when_pid_alive_but_create_time_mismatches(self):
        # A live pid whose actual start time doesn't match what was recorded means the tracked
        # process is gone and the OS has since reused its pid for something unrelated (e.g. after
        # a host reboot) -- must not be reported as "running" just because *some* process with
        # that number exists. Use this test process's own pid, which is guaranteed alive, with a
        # deliberately wrong recorded create_time.
        real_create_time = psutil.Process(os.getpid()).create_time()
        services._pkg_update_lock_file().write_text(
            json.dumps({"name": "pyobs-core", "pid": os.getpid(), "pid_create_time": real_create_time - 1000, "started_at": 0})
        )
        status = services.get_package_update_status()
        self.assertEqual(status["state"], "interrupted")
        self.assertFalse(status["active"])


class HubTokenMiddlewareTests(unittest.TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.middleware = HubTokenMiddleware(lambda request: request)

    @override_settings(HUB_CLIENTS=[{"name": "hub-a", "token": "secret-a"}, {"name": "hub-b", "token": "secret-b"}])
    def test_matching_token_authenticates_and_identifies_client(self):
        request = self.factory.get("/api/status", HTTP_X_HUB_TOKEN="secret-b")
        self.middleware(request)
        self.assertTrue(request._hub_authenticated)
        self.assertEqual(request._hub_client, "hub-b")
        self.assertTrue(request._dont_enforce_csrf_checks)

    @override_settings(HUB_CLIENTS=[{"name": "hub-a", "token": "secret-a"}])
    def test_unknown_token_is_not_authenticated(self):
        request = self.factory.get("/api/status", HTTP_X_HUB_TOKEN="wrong-token")
        self.middleware(request)
        self.assertFalse(getattr(request, "_hub_authenticated", False))

    @override_settings(HUB_CLIENTS=[{"name": "hub-a", "token": "secret-a"}])
    def test_missing_token_is_not_authenticated(self):
        request = self.factory.get("/api/status")
        self.middleware(request)
        self.assertFalse(getattr(request, "_hub_authenticated", False))

    @override_settings(HUB_CLIENTS=[], HUB_TOKEN="legacy-secret")
    def test_legacy_hub_token_still_works_as_default_client(self):
        request = self.factory.get("/api/status", HTTP_X_HUB_TOKEN="legacy-secret")
        self.middleware(request)
        self.assertTrue(request._hub_authenticated)
        self.assertEqual(request._hub_client, "default")

    @override_settings(HUB_CLIENTS=[{"name": "hub-a", "token": "secret-a"}], HUB_TOKEN="legacy-secret")
    def test_named_clients_and_legacy_token_coexist(self):
        request = self.factory.get("/api/status", HTTP_X_HUB_TOKEN="secret-a")
        self.middleware(request)
        self.assertEqual(request._hub_client, "hub-a")

        request = self.factory.get("/api/status", HTTP_X_HUB_TOKEN="legacy-secret")
        self.middleware(request)
        self.assertEqual(request._hub_client, "default")


# ── api_module_classes ───────────────────────────────────────────────────────

class ApiModuleClassesTests(unittest.TestCase):
    """Issue #68: api_module_classes went from "always local" (flat {name: class} dict) to
    fleet-aggregating (loops ["localhost"] + HUB_HOSTS, merges, reports unreachable hosts),
    matching api_all_logs' pattern. See module-classes-fleet-aggregation.md."""

    def setUp(self):
        self.factory = RequestFactory()
        self.hosts = [{"name": "MONETS", "url": "http://monets", "token": "tok"}]

    def _request(self):
        return self.factory.get("/api/modules/classes/")

    @patch("modules.services.build_module_classes")
    def test_single_host_no_hub_hosts_returns_local_modules_in_new_shape(self, mock_build):
        mock_build.return_value = {"cam1": "pyobs.modules.camera.BaseCamera"}
        response = views.api_module_classes(self._request())
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(
            data, {"modules": [{"name": "cam1", "class": "pyobs.modules.camera.BaseCamera", "host": "localhost"}],
                    "unreachable_hosts": []},
        )

    @override_settings(HUB_HOSTS=[{"name": "MONETS", "url": "http://monets", "token": "tok"}])
    @patch("modules.proxy.call")
    @patch("modules.services.build_module_classes")
    def test_multi_host_merges_local_and_remote(self, mock_build, mock_call):
        mock_build.return_value = {"cam1": "pyobs.modules.camera.BaseCamera"}
        mock_call.return_value = {
            "modules": [{"name": "telescope", "class": "pyobs.modules.telescope.BaseTelescope", "host": "localhost"}],
            "unreachable_hosts": [],
        }
        response = views.api_module_classes(self._request())
        data = json.loads(response.content)
        self.assertCountEqual(
            data["modules"],
            [
                {"name": "cam1", "class": "pyobs.modules.camera.BaseCamera", "host": "localhost"},
                {"name": "telescope", "class": "pyobs.modules.telescope.BaseTelescope", "host": "MONETS"},
            ],
        )
        self.assertEqual(data["unreachable_hosts"], [])
        mock_call.assert_called_once_with(self.hosts[0], "GET", "/api/modules/classes/")

    @override_settings(HUB_HOSTS=[{"name": "MONETS", "url": "http://monets", "token": "tok"}])
    @patch("modules.proxy.call")
    @patch("modules.services.build_module_classes")
    def test_unreachable_host_is_reported_not_fatal(self, mock_build, mock_call):
        mock_build.return_value = {"cam1": "pyobs.modules.camera.BaseCamera"}
        mock_call.side_effect = Exception("connection refused")
        response = views.api_module_classes(self._request())
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(
            data["modules"], [{"name": "cam1", "class": "pyobs.modules.camera.BaseCamera", "host": "localhost"}]
        )
        self.assertEqual(len(data["unreachable_hosts"]), 1)
        self.assertEqual(data["unreachable_hosts"][0]["name"], "MONETS")

    @override_settings(HUB_HOSTS=[{"name": "MONETS", "url": "http://monets", "token": "tok"}])
    @patch("modules.proxy.call")
    @patch("modules.services.build_module_classes")
    def test_nested_hub_preserves_sub_host_tags_instead_of_collapsing_them(self, mock_build, mock_call):
        """MONETS is itself a hub with its own sub-host "south" -- its response is already
        host-tagged (it went through this same view), including a "cam1" on both its own
        localhost and "south". Re-flattening that into one {name: class} dict per remote
        would silently drop one of the two "cam1" entries; each inner host must survive,
        with only MONETS's own "localhost" rows re-tagged to "MONETS"."""
        mock_build.return_value = {}
        mock_call.return_value = {
            "modules": [
                {"name": "cam1", "class": "pyobs.modules.camera.BaseCamera", "host": "localhost"},
                {"name": "cam1", "class": "pyobs.modules.camera.Sbig", "host": "south"},
            ],
            "unreachable_hosts": [],
        }
        response = views.api_module_classes(self._request())
        data = json.loads(response.content)
        self.assertCountEqual(
            data["modules"],
            [
                {"name": "cam1", "class": "pyobs.modules.camera.BaseCamera", "host": "MONETS"},
                {"name": "cam1", "class": "pyobs.modules.camera.Sbig", "host": "south"},
            ],
        )

    @override_settings(HUB_HOSTS=[{"name": "MONETS", "url": "http://monets", "token": "tok"}])
    @patch("modules.proxy.call")
    @patch("modules.services.build_module_classes")
    def test_nested_hubs_unreachable_sub_host_is_propagated(self, mock_build, mock_call):
        mock_build.return_value = {}
        mock_call.return_value = {
            "modules": [],
            "unreachable_hosts": [{"name": "south", "error": "connection refused"}],
        }
        response = views.api_module_classes(self._request())
        data = json.loads(response.content)
        self.assertEqual(data["unreachable_hosts"], [{"name": "south", "error": "connection refused"}])

    @override_settings(HUB_HOSTS=[{"name": "MONETS", "url": "http://monets", "token": "tok"}])
    @patch("modules.proxy.call")
    @patch("modules.services.build_module_classes")
    def test_remote_on_old_flat_dict_shape_is_reported_not_silently_dropped(self, mock_build, mock_call):
        """A HUB_HOSTS remote not yet upgraded past #68 still answers with the pre-existing
        flat {name: class} shape (no "modules" key) -- during a rolling deployment this must
        surface as unreachable, not silently contribute zero modules with no explanation."""
        mock_build.return_value = {"cam1": "pyobs.modules.camera.BaseCamera"}
        mock_call.return_value = {"telescope": "pyobs.modules.telescope.BaseTelescope"}
        response = views.api_module_classes(self._request())
        data = json.loads(response.content)
        self.assertEqual(
            data["modules"], [{"name": "cam1", "class": "pyobs.modules.camera.BaseCamera", "host": "localhost"}]
        )
        self.assertEqual(len(data["unreachable_hosts"]), 1)
        self.assertEqual(data["unreachable_hosts"][0]["name"], "MONETS")


class ApiAllLogStatsAcksTests(unittest.TestCase):
    """The dashboard sends its own localStorage "log-ack-<module>" timestamps as an `acks`
    query param so its WARNING/ERROR/CRITICAL badges reflect unacknowledged issues rather than
    an acknowledge-blind rolling 24h count -- see api_all_log_stats/collectAckTimes."""

    def setUp(self):
        self.factory = RequestFactory()

    def _request(self, acks=None):
        params = {"acks": json.dumps(acks)} if acks is not None else {}
        request = self.factory.get("/api/log-stats/", params)
        request.session = {}  # _active_host reads request.session -- "localhost" (no proxy)
        return request

    @patch("modules.services.get_log_stats")
    @patch("modules.services.list_modules")
    def test_acks_entry_is_parsed_into_a_since_datetime(self, mock_list_modules, mock_get_log_stats):
        mock_list_modules.return_value = ["camera"]
        mock_get_log_stats.return_value = {"DEBUG": 0, "INFO": 0, "WARNING": 0, "ERROR": 0, "CRITICAL": 0}
        response = views.api_all_log_stats(self._request({"camera": "2026-07-15T10:00:00.000Z"}))
        self.assertEqual(response.status_code, 200)
        mock_get_log_stats.assert_called_once_with("camera", since=datetime(2026, 7, 15, 10, 0, 0, tzinfo=UTC))

    @patch("modules.services.get_log_stats")
    @patch("modules.services.list_modules")
    def test_module_with_no_ack_entry_gets_no_since(self, mock_list_modules, mock_get_log_stats):
        mock_list_modules.return_value = ["camera", "telescope"]
        mock_get_log_stats.return_value = {"DEBUG": 0, "INFO": 0, "WARNING": 0, "ERROR": 0, "CRITICAL": 0}
        views.api_all_log_stats(self._request({"camera": "2026-07-15T10:00:00.000Z"}))
        calls = {c.args[0]: c.kwargs.get("since") for c in mock_get_log_stats.call_args_list}
        self.assertIsNotNone(calls["camera"])
        self.assertIsNone(calls["telescope"])

    @patch("modules.services.get_log_stats")
    @patch("modules.services.list_modules")
    def test_no_acks_param_at_all_behaves_as_before(self, mock_list_modules, mock_get_log_stats):
        mock_list_modules.return_value = ["camera"]
        mock_get_log_stats.return_value = {"DEBUG": 0, "INFO": 0, "WARNING": 0, "ERROR": 0, "CRITICAL": 0}
        views.api_all_log_stats(self._request())
        mock_get_log_stats.assert_called_once_with("camera", since=None)

    @patch("modules.services.get_log_stats")
    @patch("modules.services.list_modules")
    def test_malformed_acks_json_is_ignored_not_a_500(self, mock_list_modules, mock_get_log_stats):
        mock_list_modules.return_value = ["camera"]
        mock_get_log_stats.return_value = {"DEBUG": 0, "INFO": 0, "WARNING": 0, "ERROR": 0, "CRITICAL": 0}
        request = self.factory.get("/api/log-stats/", {"acks": "{not json"})
        request.session = {}
        response = views.api_all_log_stats(request)
        self.assertEqual(response.status_code, 200)
        mock_get_log_stats.assert_called_once_with("camera", since=None)

    @patch("modules.services.get_log_stats")
    @patch("modules.services.list_modules")
    def test_malformed_ack_value_for_one_module_is_skipped_not_fatal(self, mock_list_modules, mock_get_log_stats):
        mock_list_modules.return_value = ["camera"]
        mock_get_log_stats.return_value = {"DEBUG": 0, "INFO": 0, "WARNING": 0, "ERROR": 0, "CRITICAL": 0}
        response = views.api_all_log_stats(self._request({"camera": "not-a-timestamp"}))
        self.assertEqual(response.status_code, 200)
        mock_get_log_stats.assert_called_once_with("camera", since=None)


class ApiLogsBeforeParamTests(unittest.TestCase):
    """The log windows' scroll-to-top "load older logs" fetch sends `before` (the oldest
    currently-loaded line's own ISO-8601 timestamp) to api_logs/api_all_logs, which must
    parse it the same tolerant way api_all_log_stats' acks param already is (see
    ApiAllLogStatsAcksTests) and forward it through to services.get_logs/get_all_logs."""

    def setUp(self):
        self.factory = RequestFactory()

    def test_parse_before_accepts_iso_with_z_suffix(self):
        parsed = views._parse_ts("2026-07-15T10:00:00.000Z")
        self.assertEqual(parsed, datetime(2026, 7, 15, 10, 0, 0, tzinfo=UTC))

    def test_parse_before_missing_is_none(self):
        self.assertIsNone(views._parse_ts(None))
        self.assertIsNone(views._parse_ts(""))

    def test_parse_before_malformed_is_none_not_raising(self):
        self.assertIsNone(views._parse_ts("not-a-timestamp"))

    @patch("modules.services.get_logs")
    @patch("modules.services.list_modules")
    def test_api_logs_forwards_before_to_get_logs(self, mock_list_modules, mock_get_logs):
        mock_list_modules.return_value = ["camera"]
        mock_get_logs.return_value = []
        request = self.factory.get("/api/modules/camera/logs/", {"lines": 300, "before": "2026-07-15T10:00:00.000Z"})
        request.session = {}
        response = views.api_logs(request, "camera")
        self.assertEqual(response.status_code, 200)
        mock_get_logs.assert_called_once_with(
            "camera", lines=300, filter_str="", before=datetime(2026, 7, 15, 10, 0, 0, tzinfo=UTC), since=None, until=None
        )

    @patch("modules.services.get_logs")
    @patch("modules.services.list_modules")
    def test_api_logs_no_before_param_behaves_as_before(self, mock_list_modules, mock_get_logs):
        mock_list_modules.return_value = ["camera"]
        mock_get_logs.return_value = []
        request = self.factory.get("/api/modules/camera/logs/", {"lines": 300})
        request.session = {}
        views.api_logs(request, "camera")
        mock_get_logs.assert_called_once_with("camera", lines=300, filter_str="", before=None, since=None, until=None)

    @patch("modules.services.get_all_logs")
    @patch("modules.services.list_modules")
    def test_api_all_logs_forwards_before_to_get_all_logs(self, mock_list_modules, mock_get_all_logs):
        mock_list_modules.return_value = ["camera"]
        mock_get_all_logs.return_value = []
        request = self.factory.get(
            "/api/logs/", {"lines": 300, "modules": "localhost:camera", "before": "2026-07-15T10:00:00.000Z"}
        )
        request.session = {}
        response = views.api_all_logs(request)
        self.assertEqual(response.status_code, 200)
        mock_get_all_logs.assert_called_once_with(
            ["camera"], lines=300, filter_str="", before=datetime(2026, 7, 15, 10, 0, 0, tzinfo=UTC), since=None, until=None
        )

    @patch("modules.services.get_logs")
    @patch("modules.services.list_modules")
    def test_api_logs_forwards_since_to_get_logs(self, mock_list_modules, mock_get_logs):
        mock_list_modules.return_value = ["camera"]
        mock_get_logs.return_value = []
        request = self.factory.get("/api/modules/camera/logs/", {"lines": 300, "since": "2026-07-15T10:00:00.000Z"})
        request.session = {}
        response = views.api_logs(request, "camera")
        self.assertEqual(response.status_code, 200)
        mock_get_logs.assert_called_once_with(
            "camera", lines=300, filter_str="", before=None, since=datetime(2026, 7, 15, 10, 0, 0, tzinfo=UTC), until=None
        )

    @patch("modules.services.get_all_logs")
    @patch("modules.services.list_modules")
    def test_api_all_logs_forwards_since_to_get_all_logs(self, mock_list_modules, mock_get_all_logs):
        mock_list_modules.return_value = ["camera"]
        mock_get_all_logs.return_value = []
        request = self.factory.get(
            "/api/logs/", {"lines": 300, "modules": "localhost:camera", "since": "2026-07-15T10:00:00.000Z"}
        )
        request.session = {}
        response = views.api_all_logs(request)
        self.assertEqual(response.status_code, 200)
        mock_get_all_logs.assert_called_once_with(
            ["camera"], lines=300, filter_str="", before=None, since=datetime(2026, 7, 15, 10, 0, 0, tzinfo=UTC), until=None
        )

    @patch("modules.services.get_logs")
    @patch("modules.services.list_modules")
    def test_api_logs_forwards_until_to_get_logs(self, mock_list_modules, mock_get_logs):
        """The time-range end date arrives as `until` (an ISO-8601 instant) and must be
        forwarded to services.get_logs -- before this, the end date never reached the server,
        so a range ending before the newest activity was wiped by the client-side filter even
        though lines existed in the window."""
        mock_list_modules.return_value = ["camera"]
        mock_get_logs.return_value = []
        request = self.factory.get("/api/modules/camera/logs/", {"lines": 300, "until": "2026-07-15T10:00:00.000Z"})
        request.session = {}
        response = views.api_logs(request, "camera")
        self.assertEqual(response.status_code, 200)
        mock_get_logs.assert_called_once_with(
            "camera", lines=300, filter_str="", before=None, since=None, until=datetime(2026, 7, 15, 10, 0, 0, tzinfo=UTC)
        )

    @patch("modules.services.get_all_logs")
    @patch("modules.services.list_modules")
    def test_api_all_logs_forwards_until_to_get_all_logs(self, mock_list_modules, mock_get_all_logs):
        mock_list_modules.return_value = ["camera"]
        mock_get_all_logs.return_value = []
        request = self.factory.get(
            "/api/logs/", {"lines": 300, "modules": "localhost:camera", "until": "2026-07-15T10:00:00.000Z"}
        )
        request.session = {}
        response = views.api_all_logs(request)
        self.assertEqual(response.status_code, 200)
        mock_get_all_logs.assert_called_once_with(
            ["camera"], lines=300, filter_str="", before=None, since=None, until=datetime(2026, 7, 15, 10, 0, 0, tzinfo=UTC)
        )


# ── Running module versions API ─────────────────────────────────────────────────

class ApiStatusVersionsTests(unittest.TestCase):
    """api_status/api_all_statuses surface get_module_versions()/stale_packages() as
    versions/outdated (plus the installed set), None for a stopped module or one with no
    version line yet."""

    def setUp(self):
        self.factory = RequestFactory()

    @patch("modules.services.list_pyobs_packages")
    @patch("modules.services.get_module_versions")
    @patch("modules.services.get_module_stats")
    @patch("modules.services.get_module_status")
    @patch("modules.services.list_modules")
    def test_api_status_running_and_outdated(
        self, mock_list_modules, mock_status, mock_stats, mock_versions, mock_pkgs
    ):
        mock_list_modules.return_value = ["camera"]
        mock_status.return_value = "running"
        mock_stats.return_value = {"pid": 1, "cpu_percent": 0.0, "memory_mb": 1.0, "uptime_seconds": 1}
        mock_versions.return_value = {"pyobs-core": "2.0.0.dev41", "pyobs-fli": "2.0.0.dev7"}
        mock_pkgs.return_value = [{"name": "pyobs-core", "version": "2.0.0.dev76"}, {"name": "pyobs-fli", "version": "2.0.0.dev7"}]
        request = self.factory.get("/api/modules/camera/status/")
        request.session = {}
        response = views.api_status(request, "camera")
        data = json.loads(response.content)
        self.assertEqual(data["versions"], {"pyobs-core": "2.0.0.dev41", "pyobs-fli": "2.0.0.dev7"})
        self.assertEqual(data["outdated"], ["pyobs-core"])
        self.assertEqual(data["installed"], {"pyobs-core": "2.0.0.dev76", "pyobs-fli": "2.0.0.dev7"})

    @patch("modules.services.list_pyobs_packages")
    @patch("modules.services.get_module_versions")
    @patch("modules.services.get_module_stats")
    @patch("modules.services.get_module_status")
    @patch("modules.services.list_modules")
    def test_api_status_stopped_module_has_no_versions(
        self, mock_list_modules, mock_status, mock_stats, mock_versions, mock_pkgs
    ):
        mock_list_modules.return_value = ["camera"]
        mock_status.return_value = "stopped"
        mock_pkgs.return_value = []
        request = self.factory.get("/api/modules/camera/status/")
        request.session = {}
        response = views.api_status(request, "camera")
        data = json.loads(response.content)
        self.assertIsNone(data["versions"])
        self.assertIsNone(data["outdated"])
        mock_versions.assert_not_called()
        mock_stats.assert_not_called()

    @patch("modules.services.list_pyobs_packages")
    @patch("modules.services.get_module_versions")
    @patch("modules.services.get_module_stats")
    @patch("modules.services.get_module_status")
    @patch("modules.services.list_modules")
    def test_api_status_running_with_no_version_line_yet(
        self, mock_list_modules, mock_status, mock_stats, mock_versions, mock_pkgs
    ):
        mock_list_modules.return_value = ["camera"]
        mock_status.return_value = "running"
        mock_stats.return_value = {"pid": 1, "cpu_percent": 0.0, "memory_mb": 1.0, "uptime_seconds": 1}
        mock_versions.return_value = None
        mock_pkgs.return_value = [{"name": "pyobs-core", "version": "2.0.0.dev76"}]
        request = self.factory.get("/api/modules/camera/status/")
        request.session = {}
        response = views.api_status(request, "camera")
        data = json.loads(response.content)
        self.assertIsNone(data["versions"])
        self.assertIsNone(data["outdated"])

    @patch("modules.services.get_comm_user", return_value=None)
    @patch("modules.services.list_pyobs_packages")
    @patch("modules.services.get_module_versions")
    @patch("modules.services.get_module_stats")
    @patch("modules.services.get_module_status")
    @patch("modules.services.list_modules")
    def test_api_all_statuses_fetches_installed_once_not_per_module(
        self, mock_list_modules, mock_status, mock_stats, mock_versions, mock_pkgs, _mock_comm_user
    ):
        mock_list_modules.return_value = ["camera", "telescope"]
        mock_status.return_value = "running"
        mock_stats.return_value = {"pid": 1, "cpu_percent": 0.0, "memory_mb": 1.0, "uptime_seconds": 1}
        mock_versions.return_value = {"pyobs-core": "2.0.0.dev76"}
        mock_pkgs.return_value = [{"name": "pyobs-core", "version": "2.0.0.dev76"}]
        request = self.factory.get("/api/statuses/")
        request.session = {}
        response = views.api_all_statuses(request)
        data = json.loads(response.content)
        self.assertEqual(len(data["modules"]), 2)
        for m in data["modules"]:
            self.assertEqual(m["versions"], {"pyobs-core": "2.0.0.dev76"})
            self.assertEqual(m["outdated"], [])
        self.assertEqual(data["installed"], {"pyobs-core": "2.0.0.dev76"})
        mock_pkgs.assert_called_once()


# ── Async package update views ─────────────────────────────────────────────────────

class ApiPackageUpdateViewsTests(unittest.TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    @patch("modules.services.update_package_start")
    @patch("modules.services.list_pyobs_packages")
    def test_api_package_update_returns_immediately(self, mock_list, mock_start):
        mock_list.return_value = [{"name": "pyobs-core", "version": "1.54.0"}]
        mock_start.return_value = (True, "Started updating pyobs-core")
        request = self.factory.post("/api/packages/pyobs-core/update/")
        request.session = {}
        response = views.api_package_update(request, "pyobs-core")
        self.assertEqual(response.status_code, 200)
        mock_start.assert_called_once_with("pyobs-core", "1.54.0")
        self.assertEqual(json.loads(response.content), {"ok": True, "message": "Started updating pyobs-core"})

    @patch("modules.services.get_package_update_status")
    def test_api_package_update_status_returns_service_dict(self, mock_status):
        mock_status.return_value = {"active": True, "name": "pyobs-core", "state": "running", "log": "..."}
        request = self.factory.get("/api/packages/update/status/")
        request.session = {}
        response = views.api_package_update_status(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content), mock_status.return_value)


# ── Git-backed config ────────────────────────────────────────────────────────────────

class GitConfigTests(unittest.TestCase):
    """Tests for Git-backed config helpers."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _mock_result(self, stdout="", stderr="", returncode=0):
        result = MagicMock()
        result.stdout = stdout
        result.stderr = stderr
        result.returncode = returncode
        return result

    # --- configuration discovery ---

    @patch("modules.services.settings")
    def test_git_disabled_returns_false(self, mock_settings):
        mock_settings.PYOBS_CONFIG_GIT_ENABLED = False
        mock_settings.PYOBS_CONFIG_GIT_ROOT = ""
        self.assertFalse(services._git_enabled())

    @patch("modules.services.settings")
    def test_git_enabled_returns_true(self, mock_settings):
        mock_settings.PYOBS_CONFIG_GIT_ENABLED = True
        mock_settings.PYOBS_CONFIG_GIT_SUBPATH = ""
        mock_settings.PYOBS_CONFIG_GIT_ROOT = ""
        self.assertTrue(services._git_enabled())

    @patch("modules.services.settings")
    def test_git_repo_dir_without_subpath(self, mock_settings):
        mock_settings.PYOBS_CONFIG_GIT_ENABLED = True
        mock_settings.PYOBS_CONFIG_GIT_SUBPATH = ""
        mock_settings.PYOBS_CONFIG_GIT_ROOT = ""
        mock_settings.PYOBS_CONFIG_GIT_SOURCE_DIR = ""
        mock_settings.PYOBS_CONFIG_GIT_REPO = "https://example.com/repo.git"
        mock_settings.PYOBS_CONFIG_DIR = "/opt/pyobs/config"
        self.assertEqual(services._git_repo_dir(), Path("/opt/pyobs/config"))

    @patch("modules.services.settings")
    def test_git_repo_dir_with_subpath(self, mock_settings):
        mock_settings.PYOBS_CONFIG_GIT_ENABLED = True
        mock_settings.PYOBS_CONFIG_GIT_SUBPATH = "sites/obs1"
        mock_settings.PYOBS_CONFIG_GIT_ROOT = ""
        mock_settings.PYOBS_CONFIG_GIT_SOURCE_DIR = ""
        mock_settings.PYOBS_CONFIG_GIT_REPO = "https://example.com/repo.git"
        mock_settings.PYOBS_CONFIG_DIR = "/opt/pyobs/config/sites/obs1"
        self.assertEqual(services._git_repo_dir(), Path("/opt/pyobs/config"))

    @patch("modules.services.settings")
    def test_git_repo_dir_with_nested_subpath(self, mock_settings):
        mock_settings.PYOBS_CONFIG_GIT_ENABLED = True
        mock_settings.PYOBS_CONFIG_GIT_SUBPATH = "cluster/phase/obs1/config"
        mock_settings.PYOBS_CONFIG_GIT_ROOT = ""
        mock_settings.PYOBS_CONFIG_GIT_SOURCE_DIR = ""
        mock_settings.PYOBS_CONFIG_GIT_REPO = "https://example.com/repo.git"
        mock_settings.PYOBS_CONFIG_DIR = "/opt/pyobs/cluster/phase/obs1/config"
        self.assertEqual(services._git_repo_dir(), Path("/opt/pyobs"))

    @patch("modules.services.settings")
    def test_git_repo_dir_uses_explicit_root(self, mock_settings):
        mock_settings.PYOBS_CONFIG_GIT_ENABLED = True
        mock_settings.PYOBS_CONFIG_GIT_SUBPATH = "sites/obs1"
        mock_settings.PYOBS_CONFIG_GIT_ROOT = "/opt/pyobs/config"
        mock_settings.PYOBS_CONFIG_DIR = "/opt/pyobs/config/sites/obs1"
        self.assertEqual(services._git_repo_dir(), Path("/opt/pyobs/config"))

    # --- auto-stage ---

    @patch("modules.services.subprocess.run")
    @patch("modules.services._git_enabled", return_value=False)
    def test_git_auto_stage_does_nothing_when_disabled(self, mock_enabled, mock_run):
        services._git_auto_stage()
        mock_run.assert_not_called()

    # --- git_run ---

    @patch("modules.services.subprocess.run")
    @patch("modules.services._git_enabled", return_value=False)
    def test_git_run_disabled_returns_true(self, mock_enabled, mock_run):
        ok, out = services._git_run(["status"])
        self.assertTrue(ok)
        self.assertEqual(out, "")
        mock_run.assert_not_called()

    @patch("modules.services.subprocess.run")
    @patch("modules.services._git_enabled", return_value=True)
    def test_git_run_passes_env(self, mock_enabled, mock_run):
        mock_run.return_value = self._mock_result("master\n")
        services._git_run(["rev-parse", "--abbrev-ref", "HEAD"])
        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args[1]
        self.assertEqual(call_kwargs["capture_output"], True)
        self.assertEqual(call_kwargs["text"], True)
        self.assertEqual(call_kwargs["timeout"], 60)
        self.assertIn("GIT_TERMINAL_PROMPT", call_kwargs["env"])

    # --- git_repo_exists ---

    @patch("modules.services._git_enabled", return_value=False)
    def test_git_repo_exists_false_when_disabled(self, mock_enabled):
        self.assertFalse(services.git_repo_exists())

    @patch("modules.services._git_repo_dir")
    @patch("modules.services._git_enabled", return_value=True)
    def test_git_repo_exists_no_git(self, mock_enabled, mock_repo_dir):
        mock_repo_dir.return_value = self.tmp_path / "not-a-repo"
        self.assertFalse(services.git_repo_exists())

    @patch("modules.services._git_repo_dir")
    @patch("modules.services._git_enabled", return_value=True)
    def test_git_repo_exists_has_git(self, mock_enabled, mock_repo_dir):
        repo = self.tmp_path / "test-repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        mock_repo_dir.return_value = repo
        self.assertTrue(services.git_repo_exists())

    # --- git_clone ---

    @patch("modules.services.settings")
    @patch("modules.services.subprocess.run")
    @patch("modules.services._git_repo_dir")
    def test_git_clone_missing_settings(self, mock_repo_dir, mock_run, mock_settings):
        mock_repo_dir.return_value = self.tmp_path / "clone-target"
        mock_settings.PYOBS_CONFIG_GIT_REPO = ""
        mock_settings.PYOBS_CONFIG_GIT_BRANCH = "main"
        mock_settings.PYOBS_CONFIG_GIT_SUBPATH = ""
        mock_settings.PYOBS_CONFIG_GIT_ROOT = ""
        with patch("modules.services._git_enabled", return_value=True):
            ok, msg = services.git_clone()
        self.assertFalse(ok)
        self.assertIn("is not set", msg)

    @patch("modules.services.settings")
    @patch("modules.services.subprocess.run")
    @patch("modules.services._git_repo_dir")
    def test_git_clone_already_exists(self, mock_repo_dir, mock_run, mock_settings):
        clone_target = self.tmp_path / "clone-target"
        mock_repo_dir.return_value = clone_target
        (clone_target / ".git").mkdir(parents=True, exist_ok=True)
        mock_settings.PYOBS_CONFIG_GIT_REPO = "https://example.com/repo.git"
        mock_settings.PYOBS_CONFIG_GIT_BRANCH = "main"
        mock_settings.PYOBS_CONFIG_GIT_SUBPATH = ""
        mock_settings.PYOBS_CONFIG_GIT_ROOT = ""
        with patch("modules.services._git_enabled", return_value=True):
            ok, msg = services.git_clone()
        self.assertFalse(ok)
        self.assertIn("already exists", msg)

    @patch("modules.services.settings")
    @patch("modules.services.subprocess.run")
    @patch("modules.services._git_repo_dir")
    def test_git_clone_success(self, mock_repo_dir, mock_run, mock_settings):
        clone_target = self.tmp_path / "clone-target"
        mock_repo_dir.return_value = clone_target
        mock_run.return_value = self._mock_result()
        mock_settings.PYOBS_CONFIG_GIT_REPO = "https://example.com/repo.git"
        mock_settings.PYOBS_CONFIG_GIT_BRANCH = "main"
        mock_settings.PYOBS_CONFIG_GIT_SUBPATH = ""
        mock_settings.PYOBS_CONFIG_GIT_ROOT = ""
        with patch("modules.services._config_dir", return_value=clone_target):
            with patch("modules.services._git_enabled", return_value=False):
                ok, msg = services.git_clone()
        self.assertTrue(ok)

    @patch("modules.services.settings")
    @patch("modules.services.subprocess.run")
    @patch("modules.services._git_repo_dir")
    def test_git_clone_config_dir_outside_repo_allowed(self, mock_repo_dir, mock_run, mock_settings):
        """Pre-symlink state: config dir outside repo is allowed through the check."""
        clone_target = self.tmp_path / "repo-root"
        config_target = clone_target / "configs" / "obs1"
        config_link = self.tmp_path / "config-link"
        mock_repo_dir.return_value = clone_target
        mock_settings.PYOBS_CONFIG_GIT_REPO = "https://example.com/repo.git"
        mock_settings.PYOBS_CONFIG_GIT_BRANCH = "main"
        mock_settings.PYOBS_CONFIG_GIT_SUBPATH = "configs/obs1"
        mock_settings.PYOBS_CONFIG_GIT_ROOT = ""
        mock_settings.PYOBS_CONFIG_GIT_SOURCE_DIR = str(self.tmp_path / "src")
        mock_settings.PYOBS_CONFIG_DIR = str(config_link)
        def side_effect(args, **kwargs):
            if "sparse-checkout" in args and "set" in args:
                config_target.mkdir(parents=True)
            return self._mock_result()
        mock_run.side_effect = side_effect
        with patch("modules.services._git_enabled", return_value=True):
            ok, msg = services.git_clone()
        self.assertTrue(ok)

    @patch("modules.services.subprocess.run")
    @patch("modules.services._git_enabled", return_value=True)
    @patch("modules.services._git_repo_dir")
    @patch("modules.services._config_dir")
    def test_git_run_config_dir_regular_dir_allows_through(
        self, mock_config_dir, mock_repo_dir, mock_enabled, mock_run
    ):
        """A regular PYOBS_CONFIG_DIR outside the repo is allowed (pre-symlink state)."""
        config_dir = self.tmp_path / "other" / "config"
        config_dir.mkdir(parents=True)
        repo_dir = self.tmp_path / "repo-root"
        mock_repo_dir.return_value = repo_dir
        mock_config_dir.return_value = config_dir
        mock_settings = MagicMock()
        mock_settings.PYOBS_CONFIG_DIR = str(config_dir)
        mock_run.return_value = self._mock_result("ok\n")
        with patch("modules.services.settings", mock_settings):
            ok, msg = services._git_run(["status"])
        self.assertTrue(ok)
        mock_run.assert_called_once()

    # --- _repo_name ---

    @patch("modules.services.settings")
    def test_repo_name_https(self, mock_settings):
        mock_settings.PYOBS_CONFIG_GIT_REPO = "https://github.com/pyobs/pyobs-config.git"
        self.assertEqual(services._repo_name(), "pyobs-config")

    @patch("modules.services.settings")
    def test_repo_name_ssh(self, mock_settings):
        mock_settings.PYOBS_CONFIG_GIT_REPO = "git@github.com:pyobs/pyobs-config.git"
        self.assertEqual(services._repo_name(), "pyobs-config")

    @patch("modules.services.settings")
    def test_repo_name_absolute_path(self, mock_settings):
        mock_settings.PYOBS_CONFIG_GIT_REPO = "/opt/repo/my-config"
        self.assertEqual(services._repo_name(), "my-config")

    @patch("modules.services.settings")
    def test_repo_name_no_git_suffix(self, mock_settings):
        mock_settings.PYOBS_CONFIG_GIT_REPO = "https://example.com/config"
        self.assertEqual(services._repo_name(), "config")

    # --- _ensure_symlink ---

    def test_ensure_symlink_creates_when_missing(self):
        with patch.object(services.settings, "PYOBS_CONFIG_DIR", str(self.tmp_path / "config")):
            with patch("modules.services._git_enabled", return_value=True):
                with patch("modules.services._git_subpath", return_value="configs/obs1"):
                    repo_dir = self.tmp_path / "src" / "pyobs-config"
                    target = repo_dir / "configs" / "obs1"
                    target.mkdir(parents=True)
                    (target / "test.yaml").write_text("ok")
                    with patch("modules.services._git_repo_dir", return_value=repo_dir):
                        ok, msg = services._ensure_symlink()
                    self.assertTrue(ok)
                    self.assertTrue((self.tmp_path / "config").is_symlink())
                    self.assertEqual(
                        (self.tmp_path / "config").resolve(),
                        target.resolve()
                    )

    def test_ensure_symlink_noop_when_correct(self):
        with patch.object(services.settings, "PYOBS_CONFIG_DIR", str(self.tmp_path / "config")):
            with patch("modules.services._git_enabled", return_value=True):
                with patch("modules.services._git_subpath", return_value="configs/obs1"):
                    repo_dir = self.tmp_path / "src" / "pyobs-config"
                    target = repo_dir / "configs" / "obs1"
                    target.mkdir(parents=True)
                    link = self.tmp_path / "config"
                    link.symlink_to(target)
                    with patch("modules.services._git_repo_dir", return_value=repo_dir):
                        ok, msg = services._ensure_symlink()
                    self.assertTrue(ok)
                    self.assertIn("already correct", msg)

    def test_ensure_symlink_errors_when_wrong_target(self):
        repo_dir = self.tmp_path / "src" / "pyobs-config"
        target = repo_dir / "configs" / "obs1"
        target.mkdir(parents=True)
        wrong = self.tmp_path / "wrong"
        wrong.mkdir()
        with patch.object(services.settings, "PYOBS_CONFIG_DIR", str(self.tmp_path / "config")):
            with patch("modules.services._git_enabled", return_value=True):
                with patch("modules.services._git_subpath", return_value="configs/obs1"):
                    with patch("modules.services._git_repo_dir", return_value=repo_dir):
                        link = self.tmp_path / "config"
                        link.symlink_to(wrong)
                        ok, msg = services._ensure_symlink()
                    self.assertFalse(ok)
                    self.assertIn("points to", msg)

    def test_ensure_symlink_errors_when_regular_dir(self):
        repo_dir = self.tmp_path / "src" / "pyobs-config"
        target = repo_dir / "configs" / "obs1"
        target.mkdir(parents=True)
        with patch.object(services.settings, "PYOBS_CONFIG_DIR", str(self.tmp_path / "config")):
            with patch("modules.services._git_enabled", return_value=True):
                with patch("modules.services._git_subpath", return_value="configs/obs1"):
                    with patch("modules.services._git_repo_dir", return_value=repo_dir):
                        (self.tmp_path / "config").mkdir()
                        ok, msg = services._ensure_symlink()
                    self.assertFalse(ok)
                    self.assertIn("not a symlink", msg)

    # --- _git_repo_dir with new layout ---

    @patch("modules.services.settings")
    def test_git_repo_dir_derives_from_source_dir(self, mock_settings):
        mock_settings.PYOBS_CONFIG_GIT_SOURCE_DIR = "/opt/pyobs/src"
        mock_settings.PYOBS_CONFIG_GIT_ROOT = ""
        mock_settings.PYOBS_CONFIG_GIT_REPO = "https://example.com/repo/pyobs-config.git"
        with patch("modules.services._git_enabled", return_value=True):
            self.assertEqual(
                services._git_repo_dir(),
                Path("/opt/pyobs/src/pyobs-config")
            )

    @patch("modules.services.settings")
    def test_git_repo_dir_explicit_root_wins(self, mock_settings):
        mock_settings.PYOBS_CONFIG_GIT_SOURCE_DIR = "/opt/pyobs/src"
        mock_settings.PYOBS_CONFIG_GIT_ROOT = "/explicit/path"
        mock_settings.PYOBS_CONFIG_GIT_REPO = "https://example.com/repo/pyobs-config.git"
        with patch("modules.services._git_enabled", return_value=True):
            self.assertEqual(services._git_repo_dir(), Path("/explicit/path"))

    # --- git_clone with symlink ---

    @patch("modules.services.settings")
    @patch("modules.services.subprocess.run")
    @patch("modules.services._git_repo_dir")
    def test_git_clone_creates_symlink(self, mock_repo_dir, mock_run, mock_settings):
        clone_target = self.tmp_path / "clone-target"
        config_target = clone_target / "configs" / "obs1"
        config_link = self.tmp_path / "config-link"
        mock_repo_dir.return_value = clone_target
        mock_settings.PYOBS_CONFIG_GIT_REPO = "https://example.com/repo.git"
        mock_settings.PYOBS_CONFIG_GIT_BRANCH = "main"
        mock_settings.PYOBS_CONFIG_GIT_SUBPATH = "configs/obs1"
        mock_settings.PYOBS_CONFIG_GIT_ROOT = ""
        mock_settings.PYOBS_CONFIG_GIT_SOURCE_DIR = str(self.tmp_path / "src")
        mock_settings.PYOBS_CONFIG_DIR = str(config_link)
        # Subprocess mock: the sparse-checkout set call creates the config_target dir
        def side_effect(args, **kwargs):
            if "sparse-checkout" in args and "set" in args:
                config_target.mkdir(parents=True)
            return self._mock_result()
        mock_run.side_effect = side_effect
        with patch("modules.services._git_enabled", return_value=True):
            with patch("modules.services._git_config_ok", return_value=(True, "")):
                ok, msg = services.git_clone()
        self.assertTrue(ok)
        self.assertTrue(config_link.is_symlink())
        self.assertEqual(config_link.resolve(), config_target.resolve())

    @patch("modules.services.settings")
    @patch("modules.services.subprocess.run")
    @patch("modules.services._git_repo_dir")
    def test_git_clone_rolls_back_on_symlink_failure(
        self, mock_repo_dir, mock_run, mock_settings
    ):
        clone_target = self.tmp_path / "clone-target"
        config_target = clone_target / "configs" / "obs1"
        config_link = self.tmp_path / "config-link"
        (config_link / "existing").mkdir(parents=True)
        mock_repo_dir.return_value = clone_target
        mock_settings.PYOBS_CONFIG_GIT_REPO = "https://example.com/repo.git"
        mock_settings.PYOBS_CONFIG_GIT_BRANCH = "main"
        mock_settings.PYOBS_CONFIG_GIT_SUBPATH = "configs/obs1"
        mock_settings.PYOBS_CONFIG_GIT_ROOT = ""
        mock_settings.PYOBS_CONFIG_GIT_SOURCE_DIR = str(self.tmp_path / "src")
        mock_settings.PYOBS_CONFIG_DIR = str(config_link)
        def side_effect(args, **kwargs):
            if "sparse-checkout" in args and "set" in args:
                config_target.mkdir(parents=True)
            return self._mock_result()
        mock_run.side_effect = side_effect
        with patch("modules.services._git_enabled", return_value=True):
            with patch("modules.services._git_config_ok", return_value=(True, "")):
                ok, msg = services.git_clone()
        self.assertFalse(ok)
        self.assertIn("symlink", msg)
        self.assertFalse(clone_target.exists())

    # --- git_fetch ---

    @patch("modules.services.subprocess.run")
    @patch("modules.services._git_enabled", return_value=True)
    def test_git_fetch_success(self, mock_enabled, mock_run):
        mock_run.return_value = self._mock_result()
        ok, msg = services.git_fetch()
        self.assertTrue(ok)
        self.assertIn("fetch", mock_run.call_args[0][0])

    @patch("modules.services.subprocess.run")
    @patch("modules.services._git_enabled", return_value=True)
    def test_git_fetch_forces_tags(self, mock_enabled, mock_run):
        """Regression test: a deployment checkout's local tag can diverge from origin's (e.g.
        a re-cut release), and plain `git fetch --tags` rejects that tag with "would clobber
        existing tag" -- which fails the *entire* fetch, not just the one tag (seen live on
        astro159 for a stale v2.0.1). --force makes a diverging local tag always lose to
        origin, since these checkouts never create their own tags."""
        mock_run.return_value = self._mock_result()
        services.git_fetch()
        called_args = mock_run.call_args[0][0]
        self.assertIn("--tags", called_args)
        self.assertIn("--force", called_args)

    # --- git_pull ---

    @patch("modules.services.subprocess.run")
    @patch("modules.services._git_enabled", return_value=True)
    def test_git_pull(self, mock_enabled, mock_run):
        ok = self._mock_result()
        mock_run.side_effect = [ok]
        ok, _ = services.git_pull()
        self.assertTrue(ok)
        self.assertEqual(mock_run.call_count, 1)
        called_args = mock_run.call_args_list[0][0][0]
        self.assertIn("pull", called_args)
        self.assertIn("pull.rebase=false", called_args)
        self.assertTrue(any(a.startswith("user.name=") for a in called_args))
        self.assertTrue(any(a.startswith("user.email=") for a in called_args))

    @patch("modules.services.subprocess.run")
    @patch("modules.services._git_enabled", return_value=True)
    def test_git_pull_conflict_aborts_merge(self, mock_enabled, mock_run):
        conflict = self._mock_result(
            stdout="CONFLICT (modify/delete): config/iagvtsrv/fibercamera.yaml deleted in "
            "91175106 and modified in HEAD.",
            returncode=1,
        )
        abort = self._mock_result()
        mock_run.side_effect = [conflict, abort]
        ok, msg = services.git_pull()
        self.assertFalse(ok)
        self.assertEqual(mock_run.call_count, 2)
        self.assertIn("merge", mock_run.call_args_list[1][0][0])
        self.assertIn("--abort", mock_run.call_args_list[1][0][0])
        self.assertIn("Resolve the conflict manually via SSH", msg)

    # --- git_status ---

    @patch("modules.services.subprocess.run")
    @patch("modules.services._git_enabled", return_value=True)
    def test_git_status_branch_and_commit(self, mock_enabled, mock_run):
        def side_effect(args, **kwargs):
            if "rev-parse" in args and any("abbrev-ref" in a for a in args):
                return self._mock_result(stdout="main\n")
            if "rev-parse" in args and any("short" in a for a in args):
                return self._mock_result(stdout="abc1234\n")
            if "rev-list" in args:
                return self._mock_result(stdout="0\n")
            if "log" in args:
                return self._mock_result(stdout="2025-01-01 00:00:00\n")
            if "status" in args:
                return self._mock_result(stdout="## main...origin/main\n")
            return self._mock_result()

        mock_run.side_effect = side_effect
        status = services.git_status()
        self.assertEqual(status["branch"], "main")
        self.assertEqual(status["last_commit"], "abc1234")

    @patch("modules.services.subprocess.run")
    @patch("modules.services._git_enabled", return_value=True)
    def test_git_status_unstaged_changes(self, mock_enabled, mock_run):
        def side_effect(args, **kwargs):
            if "status" in args:
                return self._mock_result(stdout="## main\n1 .M N... 100644 100644 100644 e69de29bb2d1d6434b8b29ae775ad8c2e48c5391 e69de29bb2d1d6434b8b29ae775ad8c2e48c5391 camera.yaml\n")
            if "rev-parse" in args and any("abbrev-ref" in a for a in args):
                return self._mock_result(stdout="main\n")
            if "rev-parse" in args and any("short" in a for a in args):
                return self._mock_result(stdout="abc1234\n")
            if "rev-list" in args:
                return self._mock_result(stdout="0\n")
            if "log" in args:
                return self._mock_result(stdout="2025-01-01 00:00:00\n")
            return self._mock_result()

        mock_run.side_effect = side_effect
        status = services.git_status()
        self.assertTrue(status["dirty"])
        self.assertIn("camera.yaml", status["modified_files"])

    @patch("modules.services.subprocess.run")
    @patch("modules.services._git_enabled", return_value=True)
    def test_git_status_staged_changes(self, mock_enabled, mock_run):
        def side_effect(args, **kwargs):
            if "status" in args:
                return self._mock_result(stdout="## main\n1 A. N... 000000 100644 100644 0000000000000000000000000000000000000000 3e757656cf36eca53338e520d134963a44f793f8 telescope.yaml\n")
            if "rev-parse" in args and any("abbrev-ref" in a for a in args):
                return self._mock_result(stdout="main\n")
            if "rev-parse" in args and any("short" in a for a in args):
                return self._mock_result(stdout="abc1234\n")
            if "rev-list" in args:
                return self._mock_result(stdout="0\n")
            if "log" in args:
                return self._mock_result(stdout="2025-01-01 00:00:00\n")
            return self._mock_result()

        mock_run.side_effect = side_effect
        status = services.git_status()
        self.assertTrue(status["dirty"])
        self.assertIn("telescope.yaml", status["new_files"])

    @patch("modules.services.subprocess.run")
    @patch("modules.services._git_enabled", return_value=True)
    def test_git_status_ahead_and_behind_are_not_swapped(self, mock_enabled, mock_run):
        """Regression test: ahead/behind used to be computed with the rev-list ranges
        swapped, so status["behind"] actually held local's own unpushed-commit count (usually
        0), which left the Pull button on the git config page permanently disabled even when
        the remote genuinely had commits the local repo didn't."""

        def side_effect(args, **kwargs):
            if "rev-list" in args:
                range_arg = args[-1]
                if range_arg == "origin/main..main":
                    return self._mock_result(stdout="2\n")  # local's own unpushed commits
                if range_arg == "main..origin/main":
                    return self._mock_result(stdout="5\n")  # commits only on the remote
                return self._mock_result(stdout="0\n")
            if "rev-parse" in args and any("abbrev-ref" in a for a in args):
                return self._mock_result(stdout="main\n")
            if "rev-parse" in args and any("short" in a for a in args):
                return self._mock_result(stdout="abc1234\n")
            if "log" in args:
                return self._mock_result(stdout="2025-01-01 00:00:00\n")
            if "status" in args:
                return self._mock_result(stdout="## main...origin/main\n")
            return self._mock_result()

        mock_run.side_effect = side_effect
        status = services.git_status()
        self.assertEqual(status["ahead"], 2)
        self.assertEqual(status["behind"], 5)

    # --- auto-stage on save ---

    @patch("modules.services.subprocess.run")
    @patch("modules.services._git_enabled", return_value=True)
    def test_save_config_stages_when_git_enabled(self, mock_enabled, mock_run):
        config_file = self.tmp_path / "test.yaml"
        config_file.write_text("---\n")
        with patch("modules.services._config_dir", return_value=self.tmp_path):
            services.save_config("test", "---\nupdated\n")
        mock_run.assert_called_once()


class GitConfigPagePushDisabledTests(unittest.TestCase):
    """Regression test: git_push() (modules/services.py) is a plain `git push`, no
    force/rebase, so pushing while behind the remote is a guaranteed non-fast-forward
    rejection. The Push button must stay disabled whenever Pull is (behind > 0), even with
    local unpushed commits or uncommitted changes that would otherwise enable it."""

    def setUp(self):
        self.factory = RequestFactory()

    def _push_disabled(self, **status_overrides):
        status = {
            "branch": "develop",
            "ahead": 0,
            "behind": 0,
            "clean": True,
            "dirty": False,
            "modified_files": [],
            "new_files": [],
            "deleted_files": [],
            "last_commit": "",
            "last_commit_time": "",
        }
        status.update(status_overrides)
        request = self.factory.get("/git-config/")
        request.session = {}
        request.user = AnonymousUser()
        with patch("modules.services.git_status", return_value=status):
            response = views.git_config_page(request)
        content = response.content.decode()
        return bool(re.search(r'id="btn-git-push"[^>]*\bdisabled\b', content))

    def test_disabled_while_behind_despite_unpushed_local_commits(self):
        self.assertTrue(self._push_disabled(ahead=2, behind=66))

    def test_disabled_while_behind_despite_dirty_uncommitted_changes(self):
        self.assertTrue(self._push_disabled(behind=1, clean=False, dirty=True, modified_files=["a.yaml"]))

    def test_enabled_when_ahead_and_not_behind(self):
        self.assertFalse(self._push_disabled(ahead=2, behind=0))

    def test_disabled_when_nothing_to_push(self):
        self.assertTrue(self._push_disabled())


class ProxyRemoteErrorMessageTests(unittest.TestCase):
    """Regression test: _proxy() (modules/views.py) used to stringify any proxy.call()
    exception into a bare {"error": str(e)}, discarding the remote host's own JSON body --
    e.g. astro159's api_git_fetch returning {"success": false, "message": "<real git
    error>"} on a 502 became just "Operation failed" in the UI, with the actual cause lost.
    _proxy() should now surface the remote's own body when it's JSON."""

    def setUp(self):
        self.factory = RequestFactory()

    def _request(self):
        request = self.factory.post("/api/git/fetch/")
        request.session = {"active_host": "astro159"}
        return request

    def _http_error(self, status_code, json_body=None, text_body=None):
        response = MagicMock()
        response.status_code = status_code
        if json_body is not None:
            response.json.return_value = json_body
        else:
            response.json.side_effect = ValueError("not JSON")
        response.text = text_body or ""
        error = requests.exceptions.HTTPError(response=response)
        return error

    @override_settings(HUB_HOSTS=[{"name": "astro159", "url": "http://astro159:8765", "token": "tok"}])
    @patch("modules.proxy.call")
    def test_surfaces_remote_json_error_message(self, mock_call):
        mock_call.side_effect = self._http_error(502, {"success": False, "message": "fetch failed: rejected tag"})
        response = views.api_git_fetch(self._request())
        self.assertEqual(response.status_code, 502)
        data = json.loads(response.content)
        self.assertEqual(data, {"success": False, "message": "fetch failed: rejected tag"})

    @override_settings(HUB_HOSTS=[{"name": "astro159", "url": "http://astro159:8765", "token": "tok"}])
    @patch("modules.proxy.call")
    def test_falls_back_to_generic_error_when_remote_body_is_not_json(self, mock_call):
        mock_call.side_effect = self._http_error(502, json_body=None, text_body="<html>Bad Gateway</html>")
        response = views.api_git_fetch(self._request())
        self.assertEqual(response.status_code, 502)
        data = json.loads(response.content)
        self.assertIn("error", data)

    @override_settings(HUB_HOSTS=[{"name": "astro159", "url": "http://astro159:8765", "token": "tok"}])
    @patch("modules.proxy.call")
    def test_non_http_connection_error_still_falls_back_generically(self, mock_call):
        mock_call.side_effect = requests.exceptions.ConnectionError("connection refused")
        response = views.api_git_fetch(self._request())
        self.assertEqual(response.status_code, 502)
        data = json.loads(response.content)
        self.assertIn("connection refused", data["error"])


class SetHostNextRedirectTests(unittest.TestCase):
    """Issue #46: switching hub host while on a module page (e.g. astro159's "fts") used to
    blindly redirect to the same path on the new host, 404ing when that module doesn't exist
    there. set_host() must now only carry the "next" path over if the target module actually
    exists on the newly active host, falling back to "/" otherwise."""

    def setUp(self):
        self.factory = RequestFactory()
        self.hosts = [{"name": "astro159", "url": "http://astro159", "token": "tok"}]

    def _request(self, next_url: str | None):
        params = {"next": next_url} if next_url is not None else {}
        request = self.factory.get("/set-host/dummy/", params)
        request.session = {}
        return request

    @override_settings(HUB_HOSTS=[{"name": "astro159", "url": "http://astro159", "token": "tok"}])
    @patch("modules.services.list_modules")
    def test_switching_to_host_without_the_module_falls_back_to_dashboard(self, mock_list_modules):
        mock_list_modules.return_value = ["camera"]  # "fts" not present locally
        response = views.set_host(self._request("/modules/fts/"), "localhost")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/")

    @override_settings(HUB_HOSTS=[{"name": "astro159", "url": "http://astro159", "token": "tok"}])
    @patch("modules.services.list_modules")
    def test_switching_to_host_with_the_module_preserves_next(self, mock_list_modules):
        mock_list_modules.return_value = ["camera", "fts"]
        response = views.set_host(self._request("/modules/fts/"), "localhost")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/modules/fts/")

    @override_settings(HUB_HOSTS=[{"name": "astro159", "url": "http://astro159", "token": "tok"}])
    @patch("modules.proxy.call")
    def test_switching_to_remote_host_checks_its_status_list(self, mock_call):
        mock_call.return_value = {"modules": [{"name": "camera"}]}  # no "fts" on astro159
        response = views.set_host(self._request("/modules/fts/"), "astro159")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/")
        mock_call.assert_called_once_with(self.hosts[0], "GET", "/api/statuses/")

    @override_settings(HUB_HOSTS=[{"name": "astro159", "url": "http://astro159", "token": "tok"}])
    @patch("modules.proxy.call")
    def test_remote_host_error_falls_back_to_dashboard(self, mock_call):
        mock_call.side_effect = Exception("connection refused")
        response = views.set_host(self._request("/modules/fts/"), "astro159")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/")

    @patch("modules.services.list_modules")
    def test_non_module_next_paths_are_unaffected(self, mock_list_modules):
        mock_list_modules.return_value = []
        response = views.set_host(self._request("/shared/acl.shared/"), "localhost")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/shared/acl.shared/")

    def test_missing_next_defaults_to_dashboard(self):
        response = views.set_host(self._request(None), "localhost")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/")

    def test_unsafe_next_is_rejected(self):
        response = views.set_host(self._request("https://evil.example/"), "localhost")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/")


# A distinct, unlikely-to-collide username -- not "admin", since a real local_settings.py
# (e.g. a developer's own ADMIN_USERNAME="admin") would already have synced an "admin" User via
# admin_sync's post_migrate hook by the time the test database exists, before any of this
# module's own override_settings is active.
_TEST_ADMIN_USERNAME = "test-sync-admin"


@override_settings(ADMIN_USERNAME=_TEST_ADMIN_USERNAME, ADMIN_PASSWORD_HASH=make_password("admin"))
class LoginViewSyncsDjangoUserTests(DjangoTestCase):
    """The shared admin/password login (session["authenticated"]) also logs in a real
    django.contrib.auth User, so the same credential works for /admin/ too - see login_view's
    own comment for why. The primary sync path is admin_sync.sync_admin_user (post_migrate
    signal, see AdminSyncTests below); this covers login_view's fallback get_or_create for a
    fresh install that hasn't run `migrate` since ADMIN_PASSWORD_HASH was set."""

    def test_login_creates_a_staff_superuser_with_a_working_password_if_not_already_synced(self):
        response = self.client.post("/login/", {"username": _TEST_ADMIN_USERNAME, "password": "admin"})
        self.assertEqual(response.status_code, 302)

        user = User.objects.get(username=_TEST_ADMIN_USERNAME)
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.is_active)
        self.assertTrue(user.check_password("admin"))

    def test_synced_user_can_then_log_into_django_admin_directly(self):
        self.client.post("/login/", {"username": _TEST_ADMIN_USERNAME, "password": "admin"})

        fresh_client = DjangoClient()
        response = fresh_client.post(
            "/admin/login/", {"username": _TEST_ADMIN_USERNAME, "password": "admin", "next": "/admin/"}
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/admin/")

    def test_wrong_password_does_not_create_or_touch_any_user(self):
        response = self.client.post("/login/", {"username": _TEST_ADMIN_USERNAME, "password": "wrong"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username=_TEST_ADMIN_USERNAME).exists())


@override_settings(ADMIN_USERNAME=_TEST_ADMIN_USERNAME, ADMIN_PASSWORD_HASH=make_password("admin"))
class AdminSyncTests(DjangoTestCase):
    """admin_sync.sync_admin_user is the primary way the settings-configured admin account gets
    created/kept in sync - wired to run after every `manage.py migrate` via the post_migrate
    signal (AuthenticationConfig.ready()), same mechanism as pyobs-archive/pyobs-portal,
    so a fresh deployment doesn't need an interactive `createsuperuser` step."""

    def test_sync_creates_a_staff_superuser_with_a_working_password(self):
        sync_admin_user(sender=None)

        user = User.objects.get(username=_TEST_ADMIN_USERNAME)
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.is_active)
        self.assertTrue(user.check_password("admin"))

    def test_sync_updates_an_existing_user_that_drifted(self):
        User.objects.create(username=_TEST_ADMIN_USERNAME, is_staff=False, is_superuser=False)

        sync_admin_user(sender=None)

        user = User.objects.get(username=_TEST_ADMIN_USERNAME)
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.check_password("admin"))

    @override_settings(ADMIN_USERNAME="", ADMIN_PASSWORD_HASH="")
    def test_sync_does_nothing_when_unconfigured(self):
        sync_admin_user(sender=None)
        self.assertFalse(User.objects.filter(username=_TEST_ADMIN_USERNAME).exists())
