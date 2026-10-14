#!/usr/bin/env python3
"""Regression tests for module/verify-deploy.py (run: python3 module/verify_deploy_test.py).

Bug (companion-updater#10): deploy.sh kept its fallback copy as
/opt/companion-module-dev/resolume-simple.old, inside Companion's
--extra-module-path. Companion loads every module directory there, so the
old copy (same id) replaced the new one and the rig ran the old version
while the deploy reported success. verify-deploy.py must refuse a second
copy of the module id in the extra-module-path.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify-deploy.py")


def make_module(root, dirname, module_id):
    os.makedirs(os.path.join(root, dirname, "companion"))
    with open(os.path.join(root, dirname, "companion", "manifest.json"), "w") as f:
        json.dump({"id": module_id}, f)
    return os.path.join(root, dirname)


def run(module_id, installed_dir):
    return subprocess.run(
        [sys.executable, SCRIPT, module_id, "2026-01-01 00:00:00 UTC", installed_dir],
        capture_output=True, text=True,
    )


class DuplicateModuleCopies(unittest.TestCase):
    def test_a_second_copy_of_the_id_in_the_extra_module_path_fails(self):
        with tempfile.TemporaryDirectory() as extra:
            dest = make_module(extra, "resolume-simple", "resolume-simple")
            make_module(extra, "resolume-simple.old", "resolume-simple")
            make_module(extra, "presenter", "presenter")
            result = run("resolume-simple", dest)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("expected exactly one resolume-simple", result.stdout)
            self.assertIn("resolume-simple.old", result.stdout)

    def test_a_single_copy_passes_the_duplicate_check(self):
        with tempfile.TemporaryDirectory() as extra:
            dest = make_module(extra, "resolume-simple", "resolume-simple")
            make_module(extra, "presenter", "presenter")
            result = run("resolume-simple", dest)
            # Past the duplicate check it looks for Companion's DB, which a CI box does not have.
            self.assertNotIn("expected exactly one", result.stdout)


def load_verify():
    import importlib.util
    spec = importlib.util.spec_from_file_location("verify_deploy", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class HealthOutcomes(unittest.TestCase):
    """companion-updater#22: the deploy also verifies bkshading-simple, whose
    first health line is "Connected to bkshading ..." or "bkshading not
    reachable: ..."; a resolume-only regex would time out every such deploy."""

    def setUp(self):
        self.verify = load_verify()

    def test_resolume_and_bkshading_outcomes_count(self):
        lines = [
            "Instance/Connection/resolume-bridge Connected to Arena 7.22.0 at http://resolume-pp.lan:8090",
            "Instance/Connection/resolume-songs Resolume not reachable: GET http://songs-snv.lan:8091/api/v1/product failed",
            "Instance/Connection/bkshading Connected to bkshading 1.7.0-dev.785 at http://strih.lan:8770: cameras cam6",
            "Instance/Connection/bkshading-off bkshading not reachable: GET http://strih.lan:8770/api/cameras failed",
        ]
        self.assertEqual(self.verify.health_outcomes("resolume-bridge", lines), [lines[0].split(" ", 1)[1]])
        self.assertEqual(self.verify.health_outcomes("resolume-songs", lines), [lines[1].split(" ", 1)[1]])
        self.assertEqual(self.verify.health_outcomes("bkshading", lines), [lines[2].split(" ", 1)[1]])
        self.assertEqual(self.verify.health_outcomes("bkshading-off", lines), [lines[3].split(" ", 1)[1]])

    def test_other_log_lines_are_not_a_health_outcome(self):
        lines = [
            "Instance/Connection/bkshading Selected camera cam4 (Cam 4): relay not reachable",
            "Instance/Connection/bkshading cam6 iso + failed: the relay of cam1 is not reachable",
            "Instance/Connection/bkshading Poll missed once (timeout); not reporting yet",
            "Instance/Connection/other Connected to Arena 7.22.0",
        ]
        self.assertEqual(self.verify.health_outcomes("bkshading", lines), [])


if __name__ == "__main__":
    unittest.main()
