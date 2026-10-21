#!/usr/bin/env python3
"""Tests for host/cloudflare_companion.py (run: python3 host/cloudflare_companion_test.py).

companion-updater#26: the Companion UIs must be reachable at
companion-snv/companion-pp.newlevel.media behind Cloudflare Access. Cloudflare
is an external network service, so it is replaced by an in-memory fake that
answers the same paths with the same shapes as the real API.
"""
import copy
import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("cfc", os.path.join(HERE, "cloudflare_companion.py"))
cfc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cfc)

SNV, PP = "c5dbd8d0-1d45-475a-8658-a4f83020b33b", "ddb2684d-9306-4465-9699-bfe8f1f27ae1"
FIXTURE_RUN = "fixture-run-value"


class FakeCloudflare:
    """The Cloudflare state as found on 2026-10-21 (companion-updater#26)."""

    def __init__(self):
        self.calls = []
        self.ingress = {
            SNV: [
                {"hostname": "companion.newlevel.media", "service": "http://companion.lan:8000"},
                {"hostname": "n8n.newlevel.media", "service": "http://localhost:5678"},
                {"service": "http_status:404"},
            ],
            PP: [{"hostname": "companion-pp.newlevel.media", "service": "http://localhost:8000"}, {"service": "http_status:404"}],
        }
        self.dns = [
            {"id": "r1", "type": "CNAME", "name": "companion.newlevel.media", "content": f"{SNV}.cfargotunnel.com", "proxied": True},
            {"id": "r2", "type": "CNAME", "name": "companion-pp.newlevel.media", "content": f"{PP}.cfargotunnel.com", "proxied": True},
        ]
        self.alerts = [
            {"id": "n1", "name": "Default budget alert (auto-created)", "alert_type": "billing_budget_alert", "enabled": True},
        ]
        self.apps = [
            {"id": "a1", "name": "newlevel", "domain": "companion.newlevel.media", "type": "self_hosted",
             "policies": [{"decision": "allow", "include": [{"email": {"email": "drlik.marek@gmail.com"}}]}]},
        ]

    def call(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if path == "/zones?name=newlevel.media":
            return [{"id": "zone1", "account": {"id": "acc1"}}]
        if path.startswith("/accounts/acc1/cfd_tunnel?name="):
            name = path.split("name=")[1].split("&")[0]
            return [{"id": {"companion-snv": SNV, "companion-pp": PP}[name]}] if name in ("companion-snv", "companion-pp") else []
        if path == "/accounts/acc1/access/identity_providers":
            return [{"id": "idp-otp", "type": "onetimepin"}]
        for tid in (SNV, PP):
            if path == f"/accounts/acc1/cfd_tunnel/{tid}/configurations":
                if method == "PUT":
                    self.ingress[tid] = body["config"]["ingress"]
                    return {}
                return {"config": {"ingress": self.ingress[tid]}}
            if path == f"/accounts/acc1/cfd_tunnel/{tid}/token":
                return FIXTURE_RUN
        if path.startswith("/zones/zone1/dns_records?name="):
            name = path.split("name=")[1]
            return [r for r in self.dns if r["name"] == name]
        if path == "/zones/zone1/dns_records" and method == "POST":
            self.dns.append(dict(body, id=f"r{len(self.dns) + 1}"))
            return {}
        if path == "/accounts/acc1/alerting/v3/policies":
            if method == "GET":
                return self.alerts
            self.alerts.append(dict(body, id=f"n{len(self.alerts) + 1}"))
            return {}
        if path.startswith("/accounts/acc1/access/apps"):
            if method == "GET":
                return self.apps
            if method == "POST":
                self.apps.append(dict(body, id=f"a{len(self.apps) + 1}"))
                return {}
        raise AssertionError(f"unexpected call {method} {path}")


class PureFunctions(unittest.TestCase):
    def test_desired_ingress_ends_with_404(self):
        rules = cfc.desired_ingress(["a.example", "b.example"])
        self.assertEqual(rules[-1], {"service": "http_status:404"})
        self.assertEqual([r.get("hostname") for r in rules[:-1]], ["a.example", "b.example"])
        self.assertTrue(all(r["service"] == "http://localhost:8000" for r in rules[:-1]))

    def test_ingress_matches_ignores_origin_request(self):
        want = cfc.desired_ingress(["a.example"])
        have = [dict(want[0], originRequest={"access": {"required": False}}), want[1]]
        self.assertTrue(cfc.ingress_matches(have, want))
        self.assertFalse(cfc.ingress_matches([want[1]], want))
        self.assertFalse(cfc.ingress_matches(None, want))

    def test_dns_plan(self):
        tid = "t1"
        good = {"id": "x", "type": "CNAME", "name": "h", "content": "t1.cfargotunnel.com", "proxied": True}
        self.assertEqual(cfc.dns_plan([], "h", tid)[0], "create")
        self.assertEqual(cfc.dns_plan([good], "h", tid)[:2], ("ok", "x"))
        self.assertEqual(cfc.dns_plan([dict(good, proxied=False)], "h", tid)[:2], ("update", "x"))
        self.assertEqual(cfc.dns_plan([dict(good, content="other.cfargotunnel.com")], "h", tid)[0], "update")
        self.assertEqual(cfc.dns_plan([dict(good, type="A", content="1.2.3.4")], "h", tid)[0], "conflict")

    def test_access_plan_detects_missing_email_and_session(self):
        payload = cfc.access_app_payload("d.example", "D", "idp")
        self.assertEqual(cfc.access_plan([], "d.example", payload)[0], "create")
        same = dict(payload, id="a9")
        self.assertEqual(cfc.access_plan([same], "d.example", payload)[:2], ("ok", "a9"))
        other = dict(same, policies=[{"decision": "allow", "include": [{"email": {"email": "x@y"}}]}])
        self.assertEqual(cfc.access_plan([other], "d.example", payload)[0], "update")
        short = dict(same, session_duration="24h")
        action, _, reason = cfc.access_plan([short], "d.example", payload)
        self.assertEqual(action, "update")
        self.assertIn("session_duration", reason)

    def test_access_payload_lets_the_owner_in_by_otp_with_a_long_session(self):
        p = cfc.access_app_payload("companion-pp.newlevel.media", "Companion pp", "idp-otp")
        self.assertEqual(p["allowed_idps"], ["idp-otp"])
        self.assertEqual(p["session_duration"], "2880h")  # 120 days, owner 2026-10-21
        self.assertEqual(p["policies"][0]["include"], [{"email": {"email": "drlik.zbynek@gmail.com"}}])
        self.assertEqual(p["policies"][0]["decision"], "allow")


class AlertPlan(unittest.TestCase):
    def test_alert_plan(self):
        payload = cfc.alert_payload(["t2", "t1"])
        self.assertEqual(payload["filters"]["tunnel_id"], ["t1", "t2"])
        self.assertEqual(cfc.alert_plan([], payload)[0], "create")
        self.assertEqual(cfc.alert_plan([dict(payload, id="p1")], payload)[:2], ("ok", "p1"))
        self.assertEqual(cfc.alert_plan([dict(payload, id="p1", enabled=False)], payload)[0], "update")
        one = dict(payload, id="p1", filters={"tunnel_id": ["t1"], "new_status": ["TUNNEL_STATUS_TYPE_DOWN"]})
        self.assertEqual(cfc.alert_plan([one], payload)[0], "update")


class RunAgainstTheFoundState(unittest.TestCase):
    def test_plan_changes_nothing(self):
        cf = FakeCloudflare()
        before = (copy.deepcopy(cf.ingress), copy.deepcopy(cf.dns), copy.deepcopy(cf.apps))
        lines = []
        changes = cfc.run(cf, apply=False, out=lines.append)
        self.assertEqual(changes, 5)  # snv ingress, companion-snv DNS, two Access apps, the down alert
        self.assertEqual((cf.ingress, cf.dns, cf.apps), before)
        self.assertFalse([c for c in cf.calls if c[0] != "GET"])

    def test_apply_reaches_the_desired_state_and_a_second_run_is_a_no_op(self):
        cf = FakeCloudflare()
        cfc.run(cf, apply=True, out=lambda _l: None)
        self.assertEqual(
            [(r.get("hostname"), r["service"]) for r in cf.ingress[SNV]],
            [("companion-snv.newlevel.media", "http://localhost:8000"), ("companion.newlevel.media", "http://localhost:8000"), (None, "http_status:404")],
        )
        self.assertNotIn("n8n.newlevel.media", str(cf.ingress[SNV]))
        new = [r for r in cf.dns if r["name"] == "companion-snv.newlevel.media"]
        self.assertEqual(len(new), 1)
        self.assertEqual((new[0]["content"], new[0]["proxied"]), (f"{SNV}.cfargotunnel.com", True))
        self.assertEqual(sorted(a["domain"] for a in cf.apps), ["companion-pp.newlevel.media", "companion-snv.newlevel.media", "companion.newlevel.media"])
        alert = [a for a in cf.alerts if a["name"] == "Companion tunnels down · claude"]
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert[0]["alert_type"], "tunnel_health_event")
        self.assertEqual(alert[0]["filters"], {"tunnel_id": sorted([SNV, PP]), "new_status": ["TUNNEL_STATUS_TYPE_DOWN"]})
        self.assertEqual(alert[0]["mechanisms"], {"email": [{"id": "drlik.zbynek@gmail.com"}]})
        old = [a for a in cf.apps if a["domain"] == "companion.newlevel.media"][0]
        self.assertEqual(old["name"], "newlevel", "the owner's hand-made app must stay as it is")
        self.assertEqual(cfc.run(cf, apply=False, out=lambda _l: None), 0)

    def test_a_conflicting_dns_record_stops_the_run(self):
        cf = FakeCloudflare()
        cf.dns.append({"id": "r9", "type": "A", "name": "companion-snv.newlevel.media", "content": "1.2.3.4", "proxied": False})
        with self.assertRaises(cfc.CloudflareError):
            cfc.run(cf, apply=True, out=lambda _l: None)


class InstallToken(unittest.TestCase):
    def test_token_goes_over_stdin_into_a_root_0600_file_and_is_never_returned(self):
        seen = {}

        class Proc:
            returncode, stderr = 0, ""

        def runner(cmd, **kw):
            seen["cmd"], seen["input"], seen["env"] = cmd, kw["input"], kw["env"]
            return Proc()

        os.environ["COMPANION_PASS"] = "x"
        msg = cfc.install_token(FakeCloudflare(), "companion-pp", "100.101.72.101", runner=runner)
        self.assertEqual(seen["input"], f"TUNNEL_TOKEN={FIXTURE_RUN}\n")
        self.assertNotIn(FIXTURE_RUN, " ".join(seen["cmd"]))
        self.assertIn("install -m 0600 -o root -g root /dev/stdin /etc/cloudflared-companion.env", seen["cmd"][-1])
        self.assertEqual(seen["env"]["SSHPASS"], "x")
        self.assertNotIn(FIXTURE_RUN, msg)

    def test_unknown_tunnel_and_failed_ssh_are_errors(self):
        os.environ["COMPANION_PASS"] = "x"
        with self.assertRaises(cfc.CloudflareError):
            cfc.install_token(FakeCloudflare(), "presenter-pp", "h", runner=lambda *a, **k: None)

        class Bad:
            returncode, stderr = 255, "Permission denied"

        with self.assertRaises(cfc.CloudflareError) as ctx:
            cfc.install_token(FakeCloudflare(), "companion-snv", "h", runner=lambda *a, **k: Bad())
        self.assertIn("exit 255", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
