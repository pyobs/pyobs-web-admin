import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml
from django.test import override_settings

from modules import ejabberd, services
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
        signal EJABBERD_INTEGRATION.md uses to skip modules that were never expected to
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
# instance during EJABBERD_INTEGRATION.md's design phase (see that doc's Data layer), not
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
    statustext field confirmed via `cat -A` (see EJABBERD_INTEGRATION.md, Data layer)."""

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


# ── journald log backend (see JOURNALD_LOGS.md) ─────────────────────────────────

class StartModuleLogBackendTests(unittest.TestCase):
    """start_module()'s only journald-related job is choosing --syslog vs --log-file --
    everything else (pid file, --log-level, config arg) is unchanged either way, per
    JOURNALD_LOGS.md's Design, "What doesn't change"."""

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
    JOURNALD_LOGS.md, Design) -- an invented JSON shape would have missed the real surprise
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