#!/usr/bin/env python3
"""Tests for host/companion-backup-push.sh (run: python3 host/companion_backup_push_test.py).

companion-updater#8: the hourly pusher only globbed `v4.*/backups`, so after the
Companion 5 upgrade it kept re-finding an old v4.2 file and never pushed a v5
backup. And `find | sort -nr | head -n1` under `set -o pipefail` died with
`sort: write error` (SIGPIPE) once a backups dir held enough files.

The real script runs against temp dirs: a Companion config dir with versioned
backup folders, and a local bare git repo standing in for the backup repo.
"""
import os
import subprocess
import tempfile
import time
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "companion-backup-push.sh")


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


class Rig:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.config = os.path.join(root, "companion-nodejs")
        self.state = os.path.join(root, "state")
        origin = os.path.join(root, "origin.git")
        self.repo = os.path.join(self.state, "repo")
        os.makedirs(self.config)
        os.makedirs(self.state)
        git("init", "-q", "--bare", "-b", "main", origin, cwd=root)
        seed = os.path.join(root, "seed")
        git("init", "-q", "-b", "main", seed, cwd=root)
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "init", cwd=seed)
        git("push", "-q", origin, "main", cwd=seed)
        git("clone", "-q", origin, self.repo, cwd=root)
        git("config", "user.name", "t", cwd=self.repo)
        git("config", "user.email", "t@example.invalid", cwd=self.repo)

    def backup(self, version, name, content, age_s):
        d = os.path.join(self.config, version, "backups")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, name)
        with open(p, "w") as f:
            f.write(content)
        t = time.time() - age_s
        os.utime(p, (t, t))
        return p

    def run(self):
        env = dict(
            os.environ,
            MACHINE="companion-test",
            COMPANION_CONFIG_DIR=self.config,
            BACKUP_STATE_DIR=self.state,
        )
        return subprocess.run(["bash", SCRIPT], capture_output=True, text=True, env=env)

    def pushed_latest(self):
        p = os.path.join(self.repo, "companion-test", "latest.companionconfig")
        with open(p) as f:
            return f.read()


class CompanionBackupPush(unittest.TestCase):
    def setUp(self):
        self.rig = Rig()
        self.addCleanup(self.rig.tmp.cleanup)

    def test_pushes_the_newest_backup_across_companion_versions(self):
        self.rig.backup("v4.2", "old.companionconfig", "v4 config", age_s=86400 * 200)
        self.rig.backup("v5.1", "new.companionconfig", "v5 config", age_s=60)
        proc = self.rig.run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.rig.pushed_latest(), "v5 config")

    def test_many_backups_do_not_kill_the_run_with_sigpipe(self):
        newest = None
        for i in range(4000):
            newest = self.rig.backup("v5.1", f"companion-{i:05d}-long-backup-file-name.companionconfig", f"c{i}", age_s=4000 - i)
        proc = self.rig.run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("write error", proc.stderr)
        self.assertEqual(self.rig.pushed_latest(), "c3999", newest)

    def test_unchanged_backup_is_not_pushed_twice(self):
        self.rig.backup("v5.1", "a.companionconfig", "same", age_s=60)
        self.assertEqual(self.rig.run().returncode, 0)
        proc = self.rig.run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Backup unchanged", proc.stdout)

    def test_fails_loudly_when_no_backup_exists(self):
        proc = self.rig.run()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("no .companionconfig found", proc.stderr)


if __name__ == "__main__":
    unittest.main()
