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


if __name__ == "__main__":
    unittest.main()
