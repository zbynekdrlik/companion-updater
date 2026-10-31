#!/usr/bin/env python3
"""Tests for host/cloudflare_companion.py (run: python3 host/cloudflare_companion_test.py).

companion-updater#26: the Companion UIs must be reachable at
companion-*.newlevel.media behind Cloudflare Access, and stay that way.
Cloudflare is an external network service, so it is replaced by an in-memory
fake answering the same paths with the same shapes as the real API, seeded
with the state found on 2026-10-21 (ids and e-mails are placeholders).
"""
import base64
import contextlib
import copy
import importlib.util
import io
import json
import os
import unittest
import urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("cfc", os.path.join(HERE, "cloudflare_companion.py"))
cfc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cfc)

SNV, PP = "tunnel-snv-0000", "tunnel-pp-0000"
ALERT = "alert@example.com"
ENV = mock.patch.dict(os.environ, {"COMPANION_ALERT_EMAILS": ALERT})
# The policy this tool created on 2026-10-21, before the apps moved to "basic".
OTHER = {"id": "pol-old", "name": "Companion owner (OTP email) · claude", "decision": "allow",
       "include": [{"email": {"email": "owner@example.com"}}], "exclude": [], "require": []}


def setUpModule():
    ENV.start()


def tearDownModule():
    ENV.stop()


def run_value(tunnel_id):
    """A cloudflared run value has the tunnel id in its base64 JSON."""
    return base64.b64encode(json.dumps({"a": "acc1", "t": tunnel_id, "s": "x"}).encode()).decode()


class FakeCloudflare:
    def __init__(self):
        self.calls = []
        self.tunnels = {
            SNV: {"name": "companion-snv", "status": "down", "connections": [], "config": {
                "warp-routing": {"enabled": False},
                "ingress": [
                    {"hostname": "companion.newlevel.media", "service": "http://companion.lan:8000"},
                    {"hostname": "n8n.newlevel.media", "service": "http://localhost:5678"},
                    {"service": "http_status:404"},
                ]}},
            PP: {"name": "companion-pp", "status": "down", "connections": [], "config": {"ingress": [
                {"hostname": "companion-pp.newlevel.media", "service": "http://localhost:8000"},
                {"service": "http_status:404"},
            ]}},
            # The presenter project's tunnels on the rigs (cloudflared.service).
            "tun-presenter-prod": {"name": "presenter-prod", "status": "healthy", "connections": [{}], "config": {"ingress": [
                {"hostname": "presenter-snv.newlevel.media", "service": "http://localhost:80"},
                {"service": "http_status:404"},
            ]}},
            "tun-presenter-pp": {"name": "presenter-pp", "status": "healthy", "connections": [{}], "config": {"ingress": [
                {"hostname": "presenter-pp.newlevel.media", "service": "http://localhost:3000"},
                {"service": "http_status:404"},
            ]}},
        }
        self.dns = [
            {"id": "r1", "type": "CNAME", "name": "companion.newlevel.media", "content": f"{SNV}.cfargotunnel.com", "proxied": True},
            {"id": "r2", "type": "CNAME", "name": "companion-pp.newlevel.media", "content": f"{PP}.cfargotunnel.com", "proxied": True},
        ]
        self.policies = [{"id": "pol-basic", "name": "basic", "decision": "allow", "include": [{"email": {"email": "someone@example.com"}}]}]
        # The owner's app covers companion-pp as an extra destination (found 2026-10-21).
        self.apps = [{"id": "a1", "aud": "aud-alias", "name": "newlevel", "domain": "companion.newlevel.media", "type": "self_hosted",
                      "self_hosted_domains": ["companion.newlevel.media", "companion-pp.newlevel.media"],
                      "destinations": [{"type": "public", "uri": "companion.newlevel.media"}, {"type": "public", "uri": "companion-pp.newlevel.media"}],
                      "policies": [{"id": "pol-basic", "name": "basic", "decision": "allow", "include": [{"email": {"email": "someone@example.com"}}]}]}]
        self.alerts = [{"id": "n1", "name": "Default budget alert (auto-created)", "alert_type": "billing_budget_alert", "enabled": True}]
        self.no_aud = False

    @staticmethod
    def page_of(items, params):
        """Cloudflare-style paging: page N of size per_page (both required)."""
        page, per_page = int(params["page"]), int(params["per_page"])
        return copy.deepcopy(items[(page - 1) * per_page:page * per_page])

    def writes(self):
        return [(m, p) for m, p, _b in self.calls if m != "GET"]

    def call(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        base, _, query = path.partition("?")
        params = dict(kv.split("=", 1) for kv in query.split("&") if kv)
        if base == "/zones" and params.get("name") == "newlevel.media":
            return [{"id": "zone1", "account": {"id": "acc1"}}]
        if base == "/accounts/acc1/cfd_tunnel":
            assert params.get("is_deleted") == "false", "deleted tunnels must be excluded"
            return [{"id": tid, "name": t["name"], "status": t["status"], "connections": t["connections"], "remote_config": t.get("remote_config", True)}
                    for tid, t in self.tunnels.items() if t["name"] == params.get("name")]
        for tid, t in self.tunnels.items():
            if base == f"/accounts/acc1/cfd_tunnel/{tid}/configurations":
                if method == "PUT":
                    t["config"] = body["config"]
                    return {}
                return {"config": copy.deepcopy(t["config"])}
            if base == f"/accounts/acc1/cfd_tunnel/{tid}/token":
                return run_value(tid)
        if base == "/accounts/acc1/access/identity_providers":
            return [{"id": "idp-otp", "type": "onetimepin"}]
        if base == "/accounts/acc1/access/policies" and method == "GET":
            return self.page_of(self.policies, params)
        if base.startswith("/accounts/acc1/access/policies/") and method == "DELETE":
            pid = base.rsplit("/", 1)[1]
            assert any(p["id"] == pid for p in self.policies), f"no policy {pid}"
            self.policies = [p for p in self.policies if p["id"] != pid]
            return {"id": pid}
        if base == "/accounts/acc1/access/apps":
            if method == "POST" and any(cfc.covers(a, body["domain"]) for a in self.apps):
                raise cfc.CloudflareError("HTTP 409: destination belongs to another application")
            if method == "POST":
                new = dict(body, id=f"a{len(self.apps) + 1}", aud=None if self.no_aud else f"aud-{body['domain']}")
                self.apps.append(new)
                return copy.deepcopy(new)
            return self.page_of(self.apps, params)
        if base.startswith("/accounts/acc1/access/apps/") and method == "PUT":
            aid = base.rsplit("/", 1)[1]
            for i, a in enumerate(self.apps):
                if a["id"] == aid:
                    self.apps[i] = dict(body, id=aid, aud=a["aud"])
                    return copy.deepcopy(self.apps[i])
            return {}
        if base == "/accounts/acc1/alerting/v3/policies":
            if method == "POST":
                self.alerts.append(dict(body, id=f"n{len(self.alerts) + 1}"))
                return {}
            return copy.deepcopy(self.alerts)
        if base.startswith("/accounts/acc1/alerting/v3/policies/") and method == "PUT":
            nid = base.rsplit("/", 1)[1]
            self.alerts = [dict(body, id=nid) if a["id"] == nid else a for a in self.alerts]
            return {}
        if base == "/zones/zone1/dns_records":
            if method == "POST":
                self.dns.append(dict(body, id=f"r{len(self.dns) + 1}"))
                return {}
            return [r for r in self.dns if r["name"] == params.get("name")]
        if base.startswith("/zones/zone1/dns_records/") and method == "PATCH":
            rid = base.rsplit("/", 1)[1]
            self.dns = [dict(body, id=rid) if r["id"] == rid else r for r in self.dns]
            return {}
        raise AssertionError(f"unexpected call {method} {path}")


def quiet(_line):
    pass


def apply_and_converge(test, cf):
    cfc.run(cf, apply=True, out=quiet)
    test.assertEqual(cfc.run(cf, apply=False, out=quiet), 0, "a second plan must be clean")


def managed_policy_ids(cf):
    return {a["domain"]: [p["id"] for p in a["policies"]] for a in cf.apps if a["domain"] in cfc.MANAGED_APPS}


def on_another_policy(test):
    """The state after the first apply of 2026-10-21: the managed apps on a policy of our own."""
    cf = FakeCloudflare()
    apply_and_converge(test, cf)
    cf.policies.append(copy.deepcopy(OTHER))
    for a in cf.apps:
        if a["domain"] in cfc.MANAGED_APPS:
            a["policies"] = [{"id": OTHER["id"], "precedence": 1}]
    cf.calls.clear()
    return cf


class PureFunctions(unittest.TestCase):
    def test_alert_emails_come_from_COMPANION_ALERT_EMAILS_only(self):
        with mock.patch.dict(os.environ, {"COMPANION_ALERT_EMAILS": " b@x.y, a@x.y ,"}):
            self.assertEqual(cfc.alert_emails(), ["a@x.y", "b@x.y"])
        for value in (None, "", " , "):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"COMPANION_ACCESS_EMAILS": "a@x.y"}):
                os.environ.pop("COMPANION_ALERT_EMAILS", None)
                if value is not None:
                    os.environ["COMPANION_ALERT_EMAILS"] = value
                with self.assertRaises(cfc.CloudflareError) as ctx:
                    cfc.alert_emails()
                self.assertIn("set COMPANION_ALERT_EMAILS", str(ctx.exception), "the old variable no longer counts")

    def test_access_policy_is_the_owners_basic_and_names_people_only(self):
        basic = {"id": "pb", "name": "basic", "decision": "allow", "include": [{"email": {"email": "x@y"}}, {"email_list": {"id": "l"}}]}
        other = {"id": "po", "name": "basic copy", "decision": "bypass", "include": [{"everyone": {}}]}
        self.assertIs(cfc.access_policy([other, basic]), basic)
        # require (AND) and exclude (NOT) can only narrow who gets in, so they are fine.
        for extra in ({"require": [], "exclude": []}, {"require": None, "exclude": None},
                      {"require": [{"geo": {"country_code": "SK"}}], "exclude": [{"email": {"email": "x@y"}}]}):
            self.assertEqual(cfc.access_policy([dict(basic, **extra)])["id"], "pb", extra)
        bad = {
            "missing": ([other], "no reusable Access policy 'basic'"),
            "two named basic": ([basic, dict(basic, id="pb2")], "2 reusable policies are named 'basic'"),
            "bypass": ([dict(basic, decision="bypass")], "is a bypass, not an allow"),
            "non_identity": ([dict(basic, decision="non_identity")], "is a non_identity, not an allow"),
            "no decision": ([dict(basic, decision=None)], "not an allow"),
            "empty include": ([dict(basic, include=[])], "names nobody"),
            "no include": ([{k: v for k, v in basic.items() if k != "include"}], "names nobody"),
            "everyone": ([dict(basic, include=basic["include"] + [{"everyone": {}}])], "lets in by ['everyone']"),
            "email_domain": ([dict(basic, include=[{"email_domain": {"domain": "gmail.com"}}])], "lets in by ['email_domain']"),
            "service token": ([dict(basic, include=[{"any_valid_service_token": {}}])], "lets in by ['any_valid_service_token']"),
            "group": ([dict(basic, include=[{"group": {"id": "g"}}])], "lets in by ['group']"),
        }
        for name, (policies, needle) in bad.items():
            with self.subTest(name):
                with self.assertRaises(cfc.CloudflareError) as ctx:
                    cfc.access_policy(policies)
                self.assertIn(needle, str(ctx.exception))

    def test_named_counts_people_without_their_e_mails(self):
        self.assertEqual(cfc.named([{"email": {"email": "x@y"}}]), "1 e-mail address")
        self.assertEqual(cfc.named([{"email": {"email": "x@y"}}, {"email": {"email": "X@y"}}]), "1 e-mail address", "one address in two rules")
        self.assertEqual(cfc.named([{"email": {"email": "x@y"}}, {"email": {"email": "z@y"}}, {"email_list": {"id": "l"}}]), "2 e-mail addresses and 1 e-mail list")
        self.assertEqual(cfc.named([{"email_list": {"id": "l"}}, {"email_list": {"id": "m"}}]), "0 e-mail addresses and 2 e-mail lists")
        self.assertEqual(cfc.named([{"email_list": {"id": "l"}}, {"email_list": {"id": "l"}}]), "0 e-mail addresses and 1 e-mail list", "one list in two rules")

    def test_covers_main_domain_and_extra_destinations(self):
        app = {"domain": "a.example", "self_hosted_domains": ["a.example", "b.example"],
               "destinations": [{"type": "public", "uri": "a.example"}, {"type": "public", "uri": "c.example"}]}
        for d in ("a.example", "b.example", "c.example"):
            self.assertTrue(cfc.covers(app, d), d)
        self.assertFalse(cfc.covers(app, "d.example"))

    def test_list_all_follows_pages_with_an_explicit_page_size(self):
        class Paged:
            def __init__(self):
                self.pages = []

            def call(self, method, path, body=None):
                base, query = path.split("?")
                params = dict(kv.split("=") for kv in query.split("&"))
                assert path.count("?") == 1 and base == "/x"
                assert params["per_page"] == str(cfc.PAGE), "the stop rule needs an explicit page size"
                self.pages.append(int(params["page"]))
                return list(range(cfc.PAGE)) if int(params["page"]) < 3 else [1]

        cf = Paged()
        self.assertEqual(len(cfc.list_all(cf, "/x")), 2 * cfc.PAGE + 1)
        self.assertEqual(cf.pages, [1, 2, 3])
        with self.assertRaises(cfc.CloudflareError):
            cfc.list_all(cf, "/x?y=1")

    def test_app_plan(self):
        payload = cfc.app_payload("d.example", "D · claude", "idp", "pol")
        self.assertEqual(cfc.app_plan([], payload)[0], "create")
        same = dict(payload, id="a9")
        self.assertEqual(cfc.app_plan([same], payload)[:2], ("ok", "a9"))
        self.assertEqual(cfc.app_plan([dict(same, session_duration="24h")], payload)[0], "update")
        bypass = dict(same, policies=[{"id": "pol"}, {"id": "bypass-everyone"}])
        self.assertEqual(cfc.app_plan([bypass], payload)[0], "update", "an extra policy is drift")
        self.assertEqual(cfc.app_plan([dict(same, name="hand-made")], payload)[0], "conflict")

    def test_app_payload_pins_the_cookie_and_login(self):
        p = cfc.app_payload("d.example", "D", "idp-otp", "pol")
        self.assertEqual(p["same_site_cookie_attribute"], "lax")
        self.assertTrue(p["http_only_cookie_attribute"])
        self.assertEqual(p["allowed_idps"], ["idp-otp"])
        self.assertEqual(p["session_duration"], "730h")
        self.assertEqual(p["policies"], [{"id": "pol", "precedence": 1}])

    def test_only_named_people_count_as_named(self):
        self.assertEqual(cfc.NAMED_SELECTORS, {"email", "email_list"})
        selectors = ["everyone", "email_domain", "ip", "ip_list", "geo", "country_code", "group", "service_token",
                     "any_valid_service_token", "login_method", "auth_method", "common_name", "certificate", "device_posture",
                     "external_evaluation", "saml", "gsuite", "github-organization", "azureAD", "okta", "auth_context"]
        for sel in selectors:
            with self.subTest(sel):
                self.assertEqual(cfc.loose_selectors([{"email": {"email": "x@y"}}, {sel: {}}]), [sel])
                self.assertIn("not by named people", cfc.policy_problem({"decision": "allow", "include": [{sel: {}}]}))

    def test_alias_check(self):
        good = {"name": "n", "domain": "c.example", "policies": [{"name": "basic", "decision": "allow", "include": [{"email": {"email": "x@y"}}]}]}
        self.assertIsNone(cfc.alias_check([good], "c.example"))
        self.assertIn("NO Access app", cfc.alias_check([], "c.example"))
        self.assertIn("no policy", cfc.alias_check([dict(good, policies=[])], "c.example"))
        bypass = dict(good, policies=good["policies"] + [{"name": "open", "decision": "bypass", "include": [{"email": {"email": "x@y"}}]}])
        self.assertIn("is a bypass", cfc.alias_check([bypass], "c.example"))
        for loose in ({"everyone": {}}, {"login_method": {"id": "otp"}}, {"email_domain": {"domain": "gmail.com"}}, {"ip": {"ip": "0.0.0.0/0"}}):
            app = dict(good, policies=[{"name": "p", "decision": "allow", "include": [{"email": {"email": "x@y"}}, loose]}])
            self.assertIn("not by named people", cfc.alias_check([app], "c.example"), loose)
        named = dict(good, policies=[{"name": "p", "decision": "allow", "include": [{"email_list": {"id": "l"}}]}])
        self.assertIsNone(cfc.alias_check([named], "c.example"))
        group = dict(good, policies=[{"name": "p", "decision": "allow", "include": [{"group": {"id": "g"}}]}])
        self.assertIn("not by named people", cfc.alias_check([group], "c.example"), "a group can hide 'everyone'")
        nobody = dict(good, policies=[{"name": "p", "decision": "allow", "include": []}])
        self.assertIn("names nobody", cfc.alias_check([nobody], "c.example"), "judged like 'basic'")

    def test_ingress_requires_the_access_token_and_compares_it(self):
        want = cfc.desired_ingress([("a.example", "http://localhost:8000", "aud-a")])
        self.assertEqual(want[-1], {"service": "http_status:404"})
        self.assertEqual(want[0]["originRequest"]["access"], {"required": True, "teamName": "newlevelchurch", "audTag": ["aud-a"]})
        self.assertTrue(cfc.ingress_matches(copy.deepcopy(want), want))
        loose = copy.deepcopy(want)
        loose[0]["originRequest"]["access"]["required"] = False
        self.assertFalse(cfc.ingress_matches(loose, want))
        other_aud = copy.deepcopy(want)
        other_aud[0]["originRequest"]["access"]["audTag"] = ["aud-b"]
        self.assertFalse(cfc.ingress_matches(other_aud, want))
        other_port = copy.deepcopy(want)
        other_port[0]["service"] = "http://localhost:8081"
        self.assertFalse(cfc.ingress_matches(other_port, want))
        other_team = copy.deepcopy(want)
        other_team[0]["originRequest"]["access"]["teamName"] = "someoneelse"
        self.assertFalse(cfc.ingress_matches(other_team, want))
        self.assertFalse(cfc.ingress_matches(None, want))

    def test_dns_plan(self):
        good = {"id": "x", "type": "CNAME", "name": "h", "content": "t1.cfargotunnel.com", "proxied": True}
        self.assertEqual(cfc.dns_plan([], "h", "t1")[0], "create")
        self.assertEqual(cfc.dns_plan([good], "h", "t1")[:2], ("ok", "x"))
        self.assertEqual(cfc.dns_plan([dict(good, proxied=False)], "h", "t1")[:2], ("update", "x"))
        self.assertEqual(cfc.dns_plan([dict(good, type="A", content="1.2.3.4")], "h", "t1")[0], "conflict")
        foreign = dict(good, content="someone.example.com")
        self.assertEqual(cfc.dns_plan([foreign], "h", "t1", {"t1.cfargotunnel.com"})[0], "conflict")
        self.assertEqual(cfc.dns_plan([dict(foreign, comment=cfc.DNS_COMMENT)], "h", "t1")[0], "update")
        other_tunnel = dict(good, content="t2.cfargotunnel.com")
        self.assertEqual(cfc.dns_plan([other_tunnel], "h", "t1", {"t2.cfargotunnel.com"})[0], "update")

    def test_alert_has_no_status_filter_so_it_cannot_be_silently_wrong(self):
        payload = cfc.alert_payload(["t2", "t1"], [ALERT])
        self.assertEqual(payload["filters"], {"tunnel_id": ["t1", "t2"]})
        self.assertEqual(payload["alert_type"], "tunnel_health_event")
        self.assertEqual(cfc.alert_plan([], payload)[0], "create")
        self.assertEqual(cfc.alert_plan([dict(payload, id="p1")], payload)[:2], ("ok", "p1"))
        self.assertEqual(cfc.alert_plan([dict(payload, id="p1", enabled=False)], payload)[0], "update")

    def test_tunnel_id_from_token(self):
        self.assertEqual(cfc.tunnel_id_from_token(run_value("abc").rstrip("=")), "abc")


class RunAgainstTheFoundState(unittest.TestCase):
    def test_plan_writes_nothing(self):
        cf = FakeCloudflare()
        changes = cfc.run(cf, apply=False, out=quiet)
        # 3 apps, alert, both tunnels' ingress, 3 new DNS records; no policy of ours
        self.assertEqual(changes, 9)
        self.assertEqual(cf.writes(), [])

    def test_apps_and_policies_on_later_pages_are_found(self):
        saved = cfc.PAGE
        cfc.PAGE = 1  # every app and policy on its own page; the owner's app is not on page 1
        try:
            cf = FakeCloudflare()
            cf.apps.insert(0, {"id": "a0", "aud": "x", "name": "other", "domain": "other.example", "policies": []})
            cf.policies.insert(0, {"id": "p0", "name": "other", "decision": "allow", "include": []})
            cf.policies.append(copy.deepcopy(OTHER))  # page 3
            apply_and_converge(self, cf)
            self.assertFalse([a for a in cf.apps if a["domain"] == "companion-pp.newlevel.media"],
                             "the owner's app on page 2 must be seen as covering companion-pp")
            self.assertEqual(set(map(tuple, managed_policy_ids(cf).values())), {("pol-basic",)}, "'basic' is on page 2")
            self.assertEqual([p["id"] for p in cf.policies], ["p0", "pol-basic", "pol-old"], "no policy is ever deleted")
        finally:
            cfc.PAGE = saved

    def test_apply_writes_access_before_any_routing_and_converges(self):
        cf = FakeCloudflare()
        seeded_policies = copy.deepcopy(cf.policies)
        apply_and_converge(self, cf)
        order = [p for _m, p in cf.writes()]
        first_routing = min(i for i, p in enumerate(order) if "/configurations" in p or "/dns_records" in p)
        last_access = max(i for i, p in enumerate(order) if "/access/" in p)
        self.assertLess(last_access, first_routing, "a hostname was routed before its Access app existed")
        managed = {a["domain"]: a for a in cf.apps}
        for domain in cfc.MANAGED_APPS:
            self.assertEqual(managed[domain]["session_duration"], "730h")
            self.assertEqual(managed[domain]["policies"], [{"id": "pol-basic", "precedence": 1}], domain)
        self.assertEqual(cf.policies, seeded_policies, "'basic' is kept exactly as it was")
        self.assertFalse([w for w in cf.writes() if w[1].startswith("/accounts/acc1/access/policies")],
                         "no policy is ever created, changed or deleted")
        self.assertEqual(managed["companion.newlevel.media"]["name"], "newlevel", "the owner's hand-made app stays as it is")

    def test_apps_on_another_policy_move_to_basic_and_no_policy_is_deleted(self):
        cf = on_another_policy(self)
        lines = []
        self.assertEqual(cfc.run(cf, apply=False, out=lines.append), 3, "3 apps back to 'basic'")
        apply_and_converge(self, cf)
        self.assertEqual(set(map(tuple, managed_policy_ids(cf).values())), {("pol-basic",)})
        self.assertEqual([p["id"] for p in cf.policies], ["pol-basic", "pol-old"])
        self.assertFalse([w for w in cf.writes() if w[0] == "DELETE"], "the tool never deletes anything")

    def test_a_basic_that_is_not_a_plain_allow_for_named_people_stops_before_any_write(self):
        def basic(cf):
            return [p for p in cf.policies if p["name"] == "basic"][0]

        cases = {
            "missing": (lambda cf: cf.policies.clear(), "no reusable Access policy 'basic'"),
            "two named basic": (lambda cf: cf.policies.append(dict(copy.deepcopy(basic(cf)), id="pol-basic-2")), "2 reusable policies are named 'basic'"),
            "not an allow": (lambda cf: basic(cf).update(decision="bypass"), "is a bypass, not an allow"),
            "everyone": (lambda cf: basic(cf)["include"].append({"everyone": {}}), "lets in by ['everyone']"),
            "email_domain": (lambda cf: basic(cf)["include"].append({"email_domain": {"domain": "example.com"}}), "lets in by ['email_domain']"),
            "service token": (lambda cf: basic(cf)["include"].append({"any_valid_service_token": {}}), "lets in by ['any_valid_service_token']"),
        }
        for name, (break_it, needle) in cases.items():
            with self.subTest(name):
                cf = FakeCloudflare()
                break_it(cf)
                self.assert_stops_without_writing(cf, needle)

    def test_a_basic_the_owner_narrowed_with_require_or_exclude_still_converges(self):
        cf = FakeCloudflare()
        cf.policies[0].update(require=[{"geo": {"country_code": "SK"}}], exclude=[{"email": {"email": "gone@example.com"}}])
        cfc.run(cf, apply=True, out=quiet)
        self.assertEqual(cfc.run(cf, apply=False, out=quiet), 0)
        self.assertFalse([p for _m, p in cf.writes() if "pol-basic" in p], "basic is never written")

    def test_plan_says_when_basic_sets_its_own_session(self):
        cf = FakeCloudflare()
        lines = []
        cfc.run(cf, apply=False, out=lines.append)
        self.assertFalse([line for line in lines if "overrides" in line])
        cf.policies[0]["session_duration"] = "24h"
        lines = []
        cfc.run(cf, apply=False, out=lines.append)
        self.assertIn("note    policy 'basic' sets a 24h session, which overrides the apps' 730h; "
                      "the team login renews it without a PIN until that login itself expires", lines)

    def test_plan_says_whether_the_owners_app_uses_basic_too(self):
        cf = FakeCloudflare()
        lines = []
        cfc.run(cf, apply=False, out=lines.append)
        self.assertIn("ok      alias companion-pp.newlevel.media: Access app 'newlevel' uses 'basic' too, so it lets in the same people", lines)
        cf.policies.append({"id": "pol-own", "name": "own", "decision": "allow", "include": [{"email": {"email": "o@example.com"}}]})
        cf.apps[0]["policies"] = [copy.deepcopy(cf.policies[-1])]
        lines = []
        self.assertEqual(cfc.run(cf, apply=False, out=lines.append) > 0, True)
        self.assertIn("note    alias companion-pp.newlevel.media: Access app 'newlevel' does not use 'basic', so it may let in other people", lines)
        cf.apps[0]["policies"] = [copy.deepcopy(cf.policies[0]), copy.deepcopy(cf.policies[-1])]
        lines = []
        cfc.run(cf, apply=False, out=lines.append)
        self.assertIn("note    alias companion-pp.newlevel.media: Access app 'newlevel' uses 'basic' plus 1 other policy, so it may let in other people", lines)
        self.assertFalse([line for line in lines if "lets in the same people" in line])

    def test_plan_names_basic_and_counts_its_people_without_their_e_mails(self):
        cf = FakeCloudflare()
        cf.policies[0]["include"] = [{"email": {"email": f"p{i}@example.com"}} for i in range(3)] + [{"email_list": {"id": "list1"}}]
        lines = []
        cfc.run(cf, apply=False, out=lines.append)
        self.assertIn("ok      policy 'basic' (the owner's, reusable): allow, names 3 e-mail addresses and 1 e-mail list; checked, never changed", lines)
        self.assertFalse([line for line in lines if "@" in line], "no e-mail address in the output")

    def test_the_alert_goes_to_COMPANION_ALERT_EMAILS_and_without_it_nothing_runs(self):
        with mock.patch.dict(os.environ, {"COMPANION_ALERT_EMAILS": "b@example.com,a@example.com"}):
            cf = FakeCloudflare()
            apply_and_converge(self, cf)
        alert = [a for a in cf.alerts if a["name"] == cfc.ALERT_NAME][0]
        self.assertEqual(alert["mechanisms"], {"email": [{"id": "a@example.com"}, {"id": "b@example.com"}]})
        with mock.patch.dict(os.environ, {"COMPANION_ACCESS_EMAILS": ALERT}):
            os.environ.pop("COMPANION_ALERT_EMAILS", None)
            cf = FakeCloudflare()
            with self.assertRaises(cfc.CloudflareError) as ctx:
                cfc.run(cf, apply=True, out=quiet)
        self.assertIn("COMPANION_ALERT_EMAILS", str(ctx.exception))
        self.assertEqual(cf.calls, [])

    def test_apply_routes_each_host_to_its_service_with_access_required(self):
        cf = FakeCloudflare()
        apply_and_converge(self, cf)
        snv = cf.tunnels[SNV]["config"]
        self.assertEqual(snv["warp-routing"], {"enabled": False}, "the rest of the tunnel config is kept")
        rules = {r.get("hostname"): r for r in snv["ingress"]}
        self.assertEqual(rules["companion-snv.newlevel.media"]["service"], "http://localhost:8000")
        self.assertEqual(rules["companion-update-snv.newlevel.media"]["service"], "http://localhost:8081")
        self.assertEqual(rules["companion.newlevel.media"]["originRequest"]["access"]["audTag"], ["aud-alias"])
        self.assertNotIn("n8n.newlevel.media", rules)
        self.assertEqual(snv["ingress"][-1], {"service": "http_status:404"})
        pp = {r.get("hostname"): r for r in cf.tunnels[PP]["config"]["ingress"]}
        self.assertEqual(pp["companion-update-pp.newlevel.media"]["service"], "http://localhost:8081")
        self.assertEqual(pp["companion-pp.newlevel.media"]["originRequest"]["access"]["audTag"], ["aud-alias"],
                         "companion-pp is protected by the owner's app, so its aud is that app's")
        self.assertFalse([a for a in cf.apps if a["domain"] == "companion-pp.newlevel.media"], "no second app for a covered host")
        for rule in [r for r in snv["ingress"] + cf.tunnels[PP]["config"]["ingress"] if r.get("hostname")]:
            self.assertTrue(rule["originRequest"]["access"]["required"], rule["hostname"])
        names = {r["name"]: r for r in cf.dns}
        for host in ("companion-snv.newlevel.media", "companion-update-snv.newlevel.media", "companion-update-pp.newlevel.media"):
            self.assertTrue(names[host]["proxied"])
        self.assertEqual(names["companion-update-pp.newlevel.media"]["content"], f"{PP}.cfargotunnel.com")
        alert = [a for a in cf.alerts if a["name"] == cfc.ALERT_NAME][0]
        self.assertEqual(alert["filters"], {"tunnel_id": sorted([SNV, PP])})
        self.assertEqual(alert["mechanisms"], {"email": [{"id": ALERT}]})

    def test_drift_is_repaired_through_the_update_paths(self):
        cf = FakeCloudflare()
        apply_and_converge(self, cf)
        for r in cf.dns:
            if r["name"] == "companion-snv.newlevel.media":
                r["proxied"] = False
        for a in cf.apps:
            if a["domain"] == "companion-update-pp.newlevel.media":
                a["session_duration"] = "24h"
                a["policies"].append({"id": "bypass-everyone"})
        for a in cf.alerts:
            if a["name"] == cfc.ALERT_NAME:
                a["enabled"] = False
        cf.calls.clear()
        self.assertEqual(cfc.run(cf, apply=False, out=quiet), 3)
        apply_and_converge(self, cf)
        for drift in (
            lambda: [a.update(filters={"tunnel_id": [SNV]}) for a in cf.alerts if a["name"] == cfc.ALERT_NAME],
            lambda: [a.update(mechanisms={"email": [{"id": "x@y"}]}) for a in cf.alerts if a["name"] == cfc.ALERT_NAME],
            lambda: [a.update(alert_type="other") for a in cf.alerts if a["name"] == cfc.ALERT_NAME],
            lambda: [a.update(type="ssh") for a in cf.apps if a["domain"] == "companion-snv.newlevel.media"],
            lambda: [a.update(allowed_idps=[]) for a in cf.apps if a["domain"] == "companion-snv.newlevel.media"],
            lambda: [a.update(auto_redirect_to_identity=False) for a in cf.apps if a["domain"] == "companion-snv.newlevel.media"],
            lambda: [a.update(same_site_cookie_attribute=None) for a in cf.apps if a["domain"] == "companion-snv.newlevel.media"],
            lambda: [a.update(policies=[{"id": "pol-other"}]) for a in cf.apps if a["domain"] == "companion-update-snv.newlevel.media"],
            lambda: [r.update(service="http://localhost:9999") for r in cf.tunnels[PP]["config"]["ingress"][:1]],
        ):
            drift()
            self.assertEqual(cfc.run(cf, apply=False, out=quiet), 1)
            apply_and_converge(self, cf)
        apply_and_converge(self, cf)
        writes = cf.writes()
        self.assertIn("PATCH", [m for m, _p in writes])
        self.assertTrue(any(m == "PUT" and "/access/apps/" in p for m, p in writes))
        self.assertFalse([p for _m, p in writes if "/access/policies" in p], "the owner's policy is never written")
        self.assertTrue(any(m == "PUT" and "/alerting/v3/policies/" in p for m, p in writes))
        self.assertFalse(any(m == "POST" for m, _p in writes), "drift must update, never duplicate")

    def assert_stops_without_writing(self, cf, needle):
        with self.assertRaises(cfc.CloudflareError) as ctx:
            cfc.run(cf, apply=True, out=quiet)
        self.assertIn(needle, str(ctx.exception))
        self.assertEqual(cf.writes(), [], "a conflict must stop the run before any write")

    def test_a_conflicting_dns_record_stops_before_any_write(self):
        cf = FakeCloudflare()
        cf.dns.append({"id": "r9", "type": "A", "name": "companion-update-pp.newlevel.media", "content": "1.2.3.4", "proxied": False})
        self.assert_stops_without_writing(cf, "already has A 1.2.3.4")

    def test_a_foreign_app_on_a_managed_domain_is_never_overwritten(self):
        cf = FakeCloudflare()
        cf.apps.append({"id": "a7", "aud": "x", "name": "hand-made", "domain": "other.example",
                        "destinations": [{"type": "public", "uri": "companion-update-pp.newlevel.media"}], "policies": []})
        self.assert_stops_without_writing(cf, "foreign app")

    def test_a_missing_aud_after_the_app_writes_routes_nothing(self):
        cf = FakeCloudflare()
        cf.no_aud = True
        with self.assertRaises(cfc.CloudflareError) as ctx:
            cfc.run(cf, apply=True, out=quiet)
        self.assertIn("no Access aud", str(ctx.exception))
        self.assertFalse([p for _m, p in cf.writes() if "/configurations" in p or "/dns_records" in p])

    def test_a_foreign_cname_stops_before_any_write(self):
        cf = FakeCloudflare()
        cf.dns.append({"id": "r9", "type": "CNAME", "name": "companion-snv.newlevel.media", "content": "elsewhere.example.com", "proxied": True})
        self.assert_stops_without_writing(cf, "not ours")

    def test_another_tunnel_on_a_rig_reaching_companion_or_the_updater_stops_the_run(self):
        for service in ("http://localhost:8081", "http://companion-pp.lan:8000"):
            with self.subTest(service=service):
                cf = FakeCloudflare()
                cf.tunnels["tun-presenter-pp"]["config"]["ingress"].insert(0, {"hostname": "x.newlevel.media", "service": service})
                self.assert_stops_without_writing(cf, f"tunnel presenter-pp (not ours; it runs on a Companion rig) routes x.newlevel.media to {service}")

    def test_a_neighbour_rule_that_can_reach_any_port_stops_the_run(self):
        rules = {
            "bastion": {"hostname": "x.newlevel.media", "service": "bastion"},
            "socks-proxy": {"hostname": "x.newlevel.media", "service": "socks-proxy"},
            "bastionMode": {"hostname": "x.newlevel.media", "service": "ssh://localhost:22", "originRequest": {"bastionMode": True}},
            "proxyType socks": {"hostname": "x.newlevel.media", "service": "http://localhost:80", "originRequest": {"proxyType": "socks"}},
        }
        for name, rule in rules.items():
            with self.subTest(rule=name):
                cf = FakeCloudflare()
                cf.tunnels["tun-presenter-prod"]["config"]["ingress"].insert(0, rule)
                self.assert_stops_without_writing(cf, "tunnel presenter-prod (not ours; it runs on a Companion rig) can reach any port")

    def test_tunnel_wide_any_port_settings_and_warp_routing_stop_the_run(self):
        shapes = {
            "tunnel-wide bastionMode": ("originRequest", {"bastionMode": True}, "can reach any port"),
            "tunnel-wide socks": ("originRequest", {"proxyType": "socks"}, "can reach any port"),
            "warp routing": ("warp-routing", {"enabled": True}, "WARP routing"),
        }
        for name, (key, value, needle) in shapes.items():
            with self.subTest(shape=name):
                cf = FakeCloudflare()
                cf.tunnels["tun-presenter-pp"]["config"][key] = value
                self.assert_stops_without_writing(cf, needle)

    def test_a_neighbour_service_the_check_does_not_know_stops_the_run(self):
        cf = FakeCloudflare()
        cf.tunnels["tun-presenter-pp"]["config"]["ingress"].insert(0, {"hostname": "x.newlevel.media", "service": "new-thing"})
        self.assert_stops_without_writing(cf, "does not know")

    def test_harmless_neighbour_services_pass(self):
        cf = FakeCloudflare()
        cf.tunnels["tun-presenter-pp"]["config"]["ingress"][:0] = [
            {"hostname": "a.newlevel.media", "service": "hello_world"},
            {"hostname": "b.newlevel.media", "service": "unix:/run/presenter.sock"},
            {"hostname": "c.newlevel.media", "service": "https://localhost"},
            {"hostname": "d.newlevel.media", "service": "tcp://localhost:5900"},
        ]
        cf.tunnels["tun-presenter-pp"]["config"]["warp-routing"] = {"enabled": False}
        self.assertEqual(cfc.run(cf, apply=False, out=quiet) > 0, True)

    def test_an_unreadable_neighbour_service_stops_the_run(self):
        cf = FakeCloudflare()
        cf.tunnels["tun-presenter-pp"]["config"]["ingress"].insert(0, {"hostname": "x.newlevel.media", "service": "http://localhost:99999"})
        self.assert_stops_without_writing(cf, "cannot read")

    def test_a_missing_or_locally_managed_neighbour_stops_the_run(self):
        cf = FakeCloudflare()
        del cf.tunnels["tun-presenter-prod"]
        self.assert_stops_without_writing(cf, "neighbour tunnel presenter-prod not found")
        cf = FakeCloudflare()
        cf.tunnels["tun-presenter-pp"]["remote_config"] = False
        self.assert_stops_without_writing(cf, "presenter-pp is managed on the rig")

    def test_the_neighbour_tunnels_are_read_and_never_written(self):
        cf = FakeCloudflare()
        cfc.run(cf, apply=True, out=quiet)
        self.assertIn(("GET", "/accounts/acc1/cfd_tunnel/tun-presenter-pp/configurations", None), cf.calls)
        self.assertFalse([p for _m, p in cf.writes() if "tun-presenter-pp" in p])

    def test_an_unprotected_alias_stops_the_run(self):
        cf = FakeCloudflare()
        cf.apps = []
        self.assert_stops_without_writing(cf, "NO Access app")


class Status(unittest.TestCase):
    def test_status_reads_only_the_companion_tunnels(self):
        cf = FakeCloudflare()
        del cf.tunnels["tun-presenter-prod"]  # a neighbour problem is plan's business, not status's
        cfc.status(cf, out=lambda _l: None)
        self.assertFalse([p for _m, p, _b in cf.calls if "presenter" in p], "status must not read the neighbour tunnels")

    def test_counts_unhealthy_tunnels(self):
        cf = FakeCloudflare()
        lines = []
        self.assertEqual(cfc.status(cf, out=lines.append), 2)
        cf.tunnels[SNV]["status"], cf.tunnels[SNV]["connections"] = "healthy", [{}, {}]
        self.assertEqual(cfc.status(cf, out=lines.append), 1)
        cf.tunnels[PP]["status"] = "healthy"  # healthy but no connection is still down
        self.assertEqual(cfc.status(cf, out=lines.append), 1)


class HttpLayer(unittest.TestCase):
    def call_with(self, opener):
        return cfc.Cloudflare("tok", opener=opener).call("GET", "/x")

    def test_success_and_failures_all_end_in_cloudflare_error(self):
        ok = lambda req, timeout: io.BytesIO(b'{"success": true, "result": [1]}')
        self.assertEqual(self.call_with(ok), [1])

        def answer(body):
            return lambda req, timeout: io.BytesIO(body)

        def http_json(req, timeout):
            raise urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(b'{"success": false, "errors": [{"code": 9109}]}'))

        def http_text(req, timeout):
            raise urllib.error.HTTPError("u", 502, "Bad Gateway", {}, io.BytesIO(b"<html>bad gateway"))

        def url_error(req, timeout):
            raise urllib.error.URLError("name resolution failed")

        def timeout_error(req, timeout):
            raise TimeoutError()

        cases = {
            "success false": answer(b'{"success": false, "errors": [{"code": 10000}]}'),
            "not JSON": answer(b"<html>"),
            "HTTP JSON": http_json,
            "HTTP text": http_text,
            "URLError": url_error,
            "timeout": timeout_error,
        }
        for name, opener in cases.items():
            with self.subTest(name):
                with self.assertRaises(cfc.CloudflareError):
                    self.call_with(opener)

    def test_the_api_token_goes_in_the_header_only(self):
        seen = {}

        def opener(req, timeout):
            seen["auth"], seen["url"] = req.get_header("Authorization"), req.full_url
            return io.BytesIO(b'{"success": true, "result": {}}')

        cfc.Cloudflare("tok123", opener=opener).call("GET", "/zones")
        self.assertEqual(seen["auth"], "Bearer tok123")
        self.assertNotIn("tok123", seen["url"])


class Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class InstallToken(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"COMPANION_PASS": "pw"})
        env.start()
        self.addCleanup(env.stop)
        self.calls = []

    def runner_for(self, rig, fail_on=None):
        def runner(cmd, **kw):
            remote = cmd[-1]
            self.calls.append((cmd, kw))
            if fail_on and fail_on in remote:
                return Proc(255, err="Permission denied")
            if remote == "hostname":
                return Proc(out=f"{rig}\n")
            if "systemctl" in remote:
                return Proc(out="active\n")
            return Proc()
        return runner

    def stdin_write(self):
        return [c for c in self.calls if "/dev/stdin" in c[0][-1]]

    def converged(self):
        cf = FakeCloudflare()
        cfc.run(cf, apply=True, out=quiet)
        return cf

    def test_refused_while_cloudflare_is_not_set_up(self):
        with self.assertRaises(cfc.CloudflareError) as ctx:
            cfc.install_token(FakeCloudflare(), "companion-pp.lan", runner=self.runner_for("linux-pp"))
        self.assertIn("run `apply` first", str(ctx.exception))
        self.assertEqual(self.stdin_write(), [], "no token may be installed before Access is in place")

    def test_needs_the_alert_e_mails_because_it_refuses_unless_plan_is_clean(self):
        cf = self.converged()
        with mock.patch.dict(os.environ):
            os.environ.pop("COMPANION_ALERT_EMAILS", None)
            with self.assertRaises(cfc.CloudflareError) as ctx:
                cfc.install_token(cf, "companion-pp.lan", runner=self.runner_for("linux-pp"))
        self.assertIn("COMPANION_ALERT_EMAILS", str(ctx.exception))
        self.assertEqual(self.stdin_write(), [])

    def test_user_override_restart_recheck_and_missing_unit(self):
        with mock.patch.dict(os.environ, {"COMPANION_USER": "admin"}):
            cfc.install_token(self.converged(), "companion-pp.lan", runner=self.runner_for("linux-pp"))
        self.assertTrue(all("admin@companion-pp.lan" in c[0] for c in self.calls))
        start = [c for c in self.calls if "systemctl" in c[0][-1]][0][0][-1]
        self.assertIn("sudo systemctl enable cloudflared-companion && sudo systemctl restart cloudflared-companion && sleep 5 && systemctl is-active cloudflared-companion", start)
        self.assertIn("unit not installed (run deploy.sh)", start)

    def test_the_rigs_own_tunnel_goes_over_stdin_and_the_unit_is_restarted(self):
        msg = cfc.install_token(self.converged(), "companion-pp.lan", runner=self.runner_for("linux-pp"))
        write = self.stdin_write()[0]
        self.assertEqual(write[1]["input"], f"TUNNEL_TOKEN={run_value(PP)}\n")
        self.assertIn("install -m 0600 -o root -g root /dev/stdin /etc/cloudflared-companion.env", write[0][-1])
        self.assertTrue(all(run_value(PP) not in " ".join(c[0]) for c in self.calls), "the value must never be on a command line")
        self.assertEqual(write[1]["env"]["SSHPASS"], "pw")
        self.assertTrue(any("systemctl restart cloudflared-companion" in c[0][-1] for c in self.calls))
        self.assertIn("companion-pp", msg)
        self.assertNotIn(run_value(PP), msg)

    def test_snv_rig_gets_the_snv_tunnel(self):
        cfc.install_token(self.converged(), "companion.lan", runner=self.runner_for("companion-snv"))
        self.assertEqual(self.stdin_write()[0][1]["input"], f"TUNNEL_TOKEN={run_value(SNV)}\n")

    def test_unknown_rig_failed_ssh_and_a_failed_start_are_errors(self):
        with self.assertRaises(cfc.CloudflareError):
            cfc.install_token(self.converged(), "stream.lan", runner=self.runner_for("stream-pp"))
        with self.assertRaises(cfc.CloudflareError):
            cfc.install_token(self.converged(), "h", runner=self.runner_for("linux-pp", fail_on="hostname"))
        with self.assertRaises(cfc.CloudflareError) as ctx:
            cfc.install_token(self.converged(), "h", runner=self.runner_for("linux-pp", fail_on="systemctl"))
        self.assertIn("did not start", str(ctx.exception))

    def test_a_token_for_another_tunnel_is_refused(self):
        cf = self.converged()
        real = cf.call
        cf.call = lambda m, p, b=None: run_value(SNV) if p.endswith(f"{PP}/token") else real(m, p, b)
        with self.assertRaises(cfc.CloudflareError) as ctx:
            cfc.install_token(cf, "h", runner=self.runner_for("linux-pp"))
        self.assertIn("another tunnel", str(ctx.exception))
        self.assertEqual(self.stdin_write(), [])


class Main(unittest.TestCase):
    def test_exit_codes(self):
        cf = FakeCloudflare()
        self.assertEqual(cfc.main(["x", "plan"], cf=cf), 3, "pending changes")
        self.assertEqual(cfc.main(["x", "status"], cf=cf), 1, "tunnels down")
        self.assertEqual(cfc.main(["x", "apply"], cf=cf), 0)
        self.assertEqual(cfc.main(["x", "plan"], cf=cf), 0, "clean after apply")
        for t in cf.tunnels.values():
            t["status"], t["connections"] = "healthy", [{}]
        self.assertEqual(cfc.main(["x", "status"], cf=cf), 0)
        self.assertEqual(cfc.main(["x", "bogus"], cf=cf), 2)

    def test_hosts_lists_a_rigs_hostnames_from_the_tunnel_map_without_any_token(self):
        with mock.patch.dict(os.environ, {"CF_TOKEN_FILE": "/nonexistent/companion-test-token"}):
            cases = {
                ("hosts", "companion-snv"): "companion-snv.newlevel.media companion.newlevel.media companion-update-snv.newlevel.media",
                ("hosts", "companion-snv", "--ui"): "companion-snv.newlevel.media companion.newlevel.media",
                ("hosts", "linux-pp"): "companion-pp.newlevel.media companion-update-pp.newlevel.media",
                ("hosts", "linux-pp", "--ui"): "companion-pp.newlevel.media",
                ("hosts", "some-other-box"): "",
            }
            for args, want in cases.items():
                with self.subTest(args=args):
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out):
                        self.assertEqual(cfc.main(["x", *args]), 0)
                    self.assertEqual(out.getvalue(), want + "\n")
            for args in (("hosts",), ("hosts", "linux-pp", "--UI"), ("hosts", "a", "b", "c")):
                with self.subTest(args=args):
                    err = io.StringIO()
                    with contextlib.redirect_stderr(err):
                        self.assertEqual(cfc.main(["x", *args]), 2, "bad arguments print the usage")
                    self.assertIn("Usage:", err.getvalue())

    def test_a_missing_token_file_is_a_clear_error(self):
        with mock.patch.dict(os.environ, {"CF_TOKEN_FILE": "/nonexistent/companion-test-token"}):
            with self.assertRaises(cfc.CloudflareError) as ctx:
                cfc.main(["x", "status"])
        self.assertIn("/nonexistent/companion-test-token", str(ctx.exception))

    def test_apply_that_does_not_converge_is_an_error(self):
        cf = FakeCloudflare()
        real = cf.call
        # a PUT of the ingress that the API silently ignores
        cf.call = lambda m, p, b=None: {} if (m == "PUT" and "/configurations" in p) else real(m, p, b)
        with self.assertRaises(cfc.CloudflareError) as ctx:
            cfc.main(["x", "apply"], cf=cf)
        self.assertIn("still pending", str(ctx.exception))


class TunnelConfig(unittest.TestCase):
    def test_the_tunnel_is_pinned_to_http2_not_quic(self):
        # QUIC is cloudflared's default. On the rigs its UDP path stalls ("timeout: no
        # recent network activity"): every stall flips the tunnel's health and mails the
        # owner, while the presenter tunnel on http2 beside it has none (#33).
        with open(os.path.join(HERE, "cloudflared-companion.yml")) as f:
            settings = [line.split("#")[0].strip() for line in f]
        self.assertIn("protocol: http2", settings)


if __name__ == "__main__":
    unittest.main()
