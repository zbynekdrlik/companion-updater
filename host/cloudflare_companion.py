#!/usr/bin/env python3
"""Cloudflare side of the Companion web UIs (companion-updater#26).

Desired state:
  - an Access policy (reusable, allow) for the owner's e-mail, through the
    account's one-time-PIN login;
  - an Access app per managed hostname with that policy, 730h session like the
    owner's other apps;
  - a notification e-mailing the owner on every health change of either
    companion tunnel, so an outage is never again found only by chance (the
    tunnels sat dead for weeks after the presenter install replaced the rig's
    cloudflared.service);
  - tunnel ingress: each hostname -> its service on the rig (Companion UI :8000,
    companion-updater dashboard :8081), with cloudflared itself requiring a
    valid Access token for that app (originRequest.access), everything else 404;
  - a proxied DNS CNAME per hostname to its tunnel.

The older alias companion.newlevel.media is routed too, but its Access app
("newlevel") is the owner's own: it is only CHECKED (it must exist and have
allow policies only), never changed.

Writes happen in that order, after every check has passed, so a hostname is
never routed to the rig before its Access app exists.

Usage:
  cloudflare_companion.py plan       # what would change; exit 0 = nothing, 3 = changes pending
  cloudflare_companion.py apply      # make the changes, then re-plan (must be clean)
  cloudflare_companion.py status     # tunnel health; exit 1 when a tunnel is not healthy
  cloudflare_companion.py install-token <ssh-host>
      # write the run token of THAT rig's tunnel (chosen by the rig's hostname) to
      # /etc/cloudflared-companion.env (root 0600) and restart cloudflared-companion.
      # Never printed. SSH: $COMPANION_USER (default newlevel), password $COMPANION_PASS.

Environment:
  CF_TOKEN_FILE            API token file (default: the cloudflare-newlevel-full key file
                           in ~/.secrets); never printed
  COMPANION_ACCESS_EMAILS  comma-separated e-mails allowed in and alerted (the owner's;
                           kept out of this public repo)
"""
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.cloudflare.com/client/v4"
ZONE = "newlevel.media"
TEAM_NAME = "newlevelchurch"
COMPANION_UI = "http://localhost:8000"
UPDATER_UI = "http://localhost:8081"
# tunnel -> [(hostname, service on the rig)]
TUNNELS = {
    "companion-snv": [
        ("companion-snv.newlevel.media", COMPANION_UI),
        ("companion.newlevel.media", COMPANION_UI),
        ("companion-update-snv.newlevel.media", UPDATER_UI),
    ],
    "companion-pp": [
        ("companion-pp.newlevel.media", COMPANION_UI),
        ("companion-update-pp.newlevel.media", UPDATER_UI),
    ],
}
# The rig's `hostname` -> its tunnel. install-token picks the tunnel from this, so the
# snv token can never land on the pp rig (two rigs on one tunnel share its traffic).
RIG_TUNNEL = {"companion-snv": "companion-snv", "linux-pp": "companion-pp"}
MANAGED_APPS = {
    "companion-snv.newlevel.media": "Companion snv · claude",
    "companion-pp.newlevel.media": "Companion pp · claude",
    "companion-update-snv.newlevel.media": "Companion updater snv · claude",
    "companion-update-pp.newlevel.media": "Companion updater pp · claude",
}
CHECKED_ALIASES = ["companion.newlevel.media"]
POLICY_NAME = "Companion owner (OTP email) · claude"
# Owner 2026-10-21: 30 days like the other newlevel apps. A device logged in to any
# newlevel app gets in through the organisation's shared login without another PIN.
SESSION_DURATION = "730h"
DNS_COMMENT = "companion-updater#26 · claude"
ALERT_NAME = "Companion tunnels health · claude"
ENV_FILE = "/etc/cloudflared-companion.env"
UNIT = "cloudflared-companion"


class CloudflareError(Exception):
    pass


# ---------------------------------------------------------------- pure planning


def owner_emails():
    raw = os.environ.get("COMPANION_ACCESS_EMAILS", "")
    emails = sorted({e.strip() for e in raw.split(",") if e.strip()})
    if not emails:
        raise CloudflareError("set COMPANION_ACCESS_EMAILS (the owner's e-mail, comma-separated)")
    return emails


def _rules(rules):
    """A comparable form of an Access include/exclude/require list."""
    return sorted(json.dumps(r, sort_keys=True) for r in (rules or []))


def desired_policy(emails):
    return {
        "name": POLICY_NAME,
        "decision": "allow",
        "include": [{"email": {"email": e}} for e in emails],
        "exclude": [],
        "require": [],
    }


def policy_plan(policies, desired):
    """("ok"|"create"|"update", policy_id, reason) for the reusable owner policy."""
    mine = [p for p in policies if p.get("name") == desired["name"]]
    if not mine:
        return ("create", None, f"no Access policy '{desired['name']}'")
    pol = mine[0]
    diffs = []
    if pol.get("decision") != desired["decision"]:
        diffs.append("decision")
    for key in ("include", "exclude", "require"):
        if _rules(pol.get(key)) != _rules(desired[key]):
            diffs.append(key)
    if not diffs:
        return ("ok", pol.get("id"), f"Access policy '{desired['name']}' as wanted")
    return ("update", pol.get("id"), f"Access policy '{desired['name']}' differs in {', '.join(diffs)}")


def app_payload(domain, name, idp_id, policy_id):
    return {
        "name": name,
        "domain": domain,
        "type": "self_hosted",
        "session_duration": SESSION_DURATION,
        "allowed_idps": [idp_id],
        "auto_redirect_to_identity": True,
        "app_launcher_visible": True,
        "http_only_cookie_attribute": True,
        "policies": [{"id": policy_id, "precedence": 1}],
    }


def app_plan(apps, payload):
    """("ok"|"create"|"update"|"conflict", app_id, reason) for a managed app.

    The app must carry exactly the owner policy: any extra policy (a bypass, an
    'everyone') is drift. An app on the domain under another name is somebody
    else's; it is never overwritten.
    """
    domain = payload["domain"]
    mine = [a for a in apps if a.get("domain") == domain]
    if not mine:
        return ("create", None, f"no Access app for {domain}")
    if len(mine) > 1 or mine[0].get("name") != payload["name"]:
        names = ", ".join(repr(a.get("name")) for a in mine)
        return ("conflict", None, f"{domain} already has Access app(s) {names}; not overwriting a foreign app")
    app = mine[0]
    diffs = []
    for key in ("type", "session_duration", "allowed_idps", "auto_redirect_to_identity"):
        if app.get(key) != payload[key]:
            diffs.append(f"{key} {app.get(key)!r} -> {payload[key]!r}")
    have = [p.get("id") for p in app.get("policies") or []]
    want = [p["id"] for p in payload["policies"]]
    if have != want:
        diffs.append(f"policies {have} -> {want}")
    if not diffs:
        return ("ok", app.get("id"), f"{domain}: Access app '{app.get('name')}' as wanted")
    return ("update", app.get("id"), f"{domain}: " + "; ".join(diffs))


def alias_check(apps, domain):
    """None when `domain` is protected by an allow-only Access app, else the problem."""
    mine = [a for a in apps if a.get("domain") == domain]
    if not mine:
        return f"{domain} is routed to the rig but has NO Access app"
    for app in mine:
        policies = app.get("policies") or []
        if not policies:
            return f"{domain}: Access app '{app.get('name')}' has no policy"
        bad = [p.get("name") for p in policies if p.get("decision") != "allow" or any("everyone" in r for r in p.get("include") or [])]
        if bad:
            return f"{domain}: Access app '{app.get('name')}' has non-owner policies {bad} (bypass/everyone)"
    return None


def desired_ingress(rules):
    """rules: [(hostname, service, aud)] -> the tunnel ingress; cloudflared checks the Access token too."""
    out = []
    for hostname, service, aud in rules:
        out.append({
            "hostname": hostname,
            "service": service,
            "originRequest": {"access": {"required": True, "teamName": TEAM_NAME, "audTag": [aud]}},
        })
    return out + [{"service": "http_status:404"}]


def _ingress_key(rules):
    out = []
    for r in rules or []:
        access = (r.get("originRequest") or {}).get("access") or {}
        out.append((r.get("hostname"), r.get("service"), bool(access.get("required")), access.get("teamName"), sorted(access.get("audTag") or [])))
    return out


def ingress_matches(current, desired):
    return _ingress_key(current) == _ingress_key(desired)


def dns_plan(records, hostname, tunnel_id):
    """("ok"|"create"|"update"|"conflict", record_id, reason): `hostname` must be a proxied CNAME to the tunnel."""
    target = f"{tunnel_id}.cfargotunnel.com"
    mine = [r for r in records if r.get("name") == hostname]
    if not mine:
        return ("create", None, f"no record for {hostname}")
    if len(mine) > 1 or mine[0].get("type") != "CNAME":
        kinds = ", ".join(f"{r.get('type')} {r.get('content')}" for r in mine)
        return ("conflict", None, f"{hostname} already has {kinds}; not touching it")
    rec = mine[0]
    if rec.get("content") == target and rec.get("proxied") is True:
        return ("ok", rec.get("id"), f"{hostname} -> tunnel")
    return ("update", rec.get("id"), f"{hostname} -> {rec.get('content')} (proxied={rec.get('proxied')}), want the tunnel")


def alert_payload(tunnel_ids, emails):
    # No new_status filter: its value format is undocumented, and a wrong value would
    # give a valid alert that never fires. Every health change (down AND recovery) mails.
    return {
        "name": ALERT_NAME,
        "description": "companion-updater#26: a companion web UI tunnel changed health",
        "enabled": True,
        "alert_type": "tunnel_health_event",
        "filters": {"tunnel_id": sorted(tunnel_ids)},
        "mechanisms": {"email": [{"id": e} for e in emails]},
    }


def alert_plan(policies, payload):
    """("ok"|"create"|"update", policy_id, reason) for the tunnel health notification."""
    mine = [p for p in policies if p.get("name") == payload["name"]]
    if not mine:
        return ("create", None, f"no notification '{payload['name']}'")
    pol = mine[0]
    emails = sorted(m.get("id") for m in ((pol.get("mechanisms") or {}).get("email") or []))
    diffs = []
    if pol.get("alert_type") != payload["alert_type"]:
        diffs.append(f"alert_type {pol.get('alert_type')}")
    if not pol.get("enabled"):
        diffs.append("disabled")
    if (pol.get("filters") or {}) != payload["filters"]:
        diffs.append(f"filters {pol.get('filters')}")
    if emails != sorted(m["id"] for m in payload["mechanisms"]["email"]):
        diffs.append("e-mails differ")
    if not diffs:
        return ("ok", pol.get("id"), f"notification '{payload['name']}' as wanted")
    return ("update", pol.get("id"), f"notification '{payload['name']}': " + "; ".join(diffs))


def tunnel_id_from_token(token):
    """The tunnel id (`t`) inside a cloudflared run token (base64 JSON)."""
    padded = token + "=" * (-len(token) % 4)
    return json.loads(base64.b64decode(padded)).get("t")


# ---------------------------------------------------------------- HTTP layer


class Cloudflare:
    def __init__(self, token, opener=urllib.request.urlopen):
        self.token = token
        self.opener = opener

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{API}{path}", data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/json")
        try:
            with self.opener(req, timeout=30) as res:
                text = res.read().decode()
        except urllib.error.HTTPError as err:
            text = err.read().decode(errors="replace")
            try:
                payload = json.loads(text)
            except ValueError:
                raise CloudflareError(f"{method} {path}: HTTP {err.code}: {text[:200]}") from err
            raise CloudflareError(f"{method} {path}: HTTP {err.code}: {payload.get('errors')}") from err
        except urllib.error.URLError as err:
            raise CloudflareError(f"{method} {path}: {err.reason}") from err
        except TimeoutError as err:
            raise CloudflareError(f"{method} {path}: no answer within 30 s") from err
        try:
            payload = json.loads(text)
        except ValueError as err:
            raise CloudflareError(f"{method} {path}: answer is not JSON: {text[:200]}") from err
        if not payload.get("success"):
            raise CloudflareError(f"{method} {path}: {payload.get('errors')}")
        return payload["result"]


def read_token():
    path = os.environ.get("CF_TOKEN_FILE", os.path.expanduser("~/.secrets/cloudflare-newlevel-full"))
    with open(path) as f:
        token = "".join(f.read().split())
    if not token:
        raise CloudflareError(f"{path} is empty")
    return token


def q(value):
    return urllib.parse.quote(value, safe="")


def gather(cf):
    """Every GET the plan needs. No writes."""
    zones = cf.call("GET", f"/zones?name={ZONE}")
    if not zones:
        raise CloudflareError(f"zone {ZONE} not visible to this token")
    st = {"zone": zones[0]["id"], "account": zones[0]["account"]["id"], "tunnels": {}}
    acc = st["account"]
    for name in TUNNELS:
        found = cf.call("GET", f"/accounts/{acc}/cfd_tunnel?name={q(name)}&is_deleted=false")
        if not found:
            raise CloudflareError(f"tunnel {name} does not exist")
        t = found[0]
        if not t.get("remote_config"):
            raise CloudflareError(f"tunnel {name} is not remotely managed; its ingress lives on the rig")
        conf = cf.call("GET", f"/accounts/{acc}/cfd_tunnel/{t['id']}/configurations")
        st["tunnels"][name] = {"id": t["id"], "status": t.get("status"), "connections": len(t.get("connections") or []), "config": (conf or {}).get("config") or {}}
    idps = [i for i in cf.call("GET", f"/accounts/{acc}/access/identity_providers") if i.get("type") == "onetimepin"]
    if not idps:
        raise CloudflareError("no one-time-PIN identity provider in the account")
    st["idp"] = idps[0]["id"]
    st["policies"] = cf.call("GET", f"/accounts/{acc}/access/policies?per_page=1000")
    st["apps"] = {}
    for domain in list(MANAGED_APPS) + CHECKED_ALIASES:
        st["apps"][domain] = cf.call("GET", f"/accounts/{acc}/access/apps?domain={q(domain)}&exact=true")
    st["alerts"] = cf.call("GET", f"/accounts/{acc}/alerting/v3/policies")
    st["dns"] = {}
    for rules in TUNNELS.values():
        for host, _service in rules:
            st["dns"][host] = cf.call("GET", f"/zones/{st['zone']}/dns_records?name={q(host)}")
    return st


def run(cf, apply, out=print):
    """Plan, and with apply=True make, every change. Returns the number of changes."""
    emails = owner_emails()
    st = gather(cf)
    acc, zone = st["account"], st["zone"]

    # 1. every check first: any conflict stops the run before a single write.
    for alias in CHECKED_ALIASES:
        problem = alias_check(st["apps"][alias], alias)
        if problem:
            raise CloudflareError(problem)
    dns_steps = []
    for name, rules in TUNNELS.items():
        for host, _service in rules:
            action, rec_id, reason = dns_plan(st["dns"][host], host, st["tunnels"][name]["id"])
            if action == "conflict":
                raise CloudflareError(f"dns {reason}")
            dns_steps.append((name, host, action, rec_id, reason))
    want_policy = desired_policy(emails)
    pol_action, pol_id, pol_reason = policy_plan(st["policies"], want_policy)
    app_steps = []
    for domain, app_name in MANAGED_APPS.items():
        payload = app_payload(domain, app_name, st["idp"], pol_id or "<new policy>")
        action, app_id, reason = app_plan(st["apps"][domain], payload)
        if action == "conflict":
            raise CloudflareError(f"access {reason}")
        app_steps.append((domain, app_name, action, app_id, reason))

    changes = 0
    verb = "set  " if apply else "would"

    def note(action, what):
        nonlocal changes
        if action == "ok":
            out(f"ok      {what}")
        else:
            changes += 1
            out(f"{verb}   {what}")

    # 2. Access first: a hostname is never routed before its app exists.
    note(pol_action, f"policy {pol_reason}")
    if apply and pol_action == "create":
        pol_id = cf.call("POST", f"/accounts/{acc}/access/policies", want_policy)["id"]
    elif apply and pol_action == "update":
        cf.call("PUT", f"/accounts/{acc}/access/policies/{pol_id}", want_policy)
    for domain, app_name, action, app_id, reason in app_steps:
        note(action, f"access {reason}")
        payload = app_payload(domain, app_name, st["idp"], pol_id)
        if apply and action == "create":
            cf.call("POST", f"/accounts/{acc}/access/apps", payload)
        elif apply and action == "update":
            cf.call("PUT", f"/accounts/{acc}/access/apps/{app_id}", payload)

    # 3. the health notification.
    alert = alert_payload([t["id"] for t in st["tunnels"].values()], emails)
    action, alert_id, reason = alert_plan(st["alerts"], alert)
    note(action, f"alert {reason}")
    if apply and action == "create":
        cf.call("POST", f"/accounts/{acc}/alerting/v3/policies", alert)
    elif apply and action == "update":
        cf.call("PUT", f"/accounts/{acc}/alerting/v3/policies/{alert_id}", alert)

    # 4. ingress, with the apps' aud tags (re-read after a create).
    auds = {}
    for domain in list(MANAGED_APPS) + CHECKED_ALIASES:
        apps = st["apps"][domain]
        if apply and domain in MANAGED_APPS:
            apps = cf.call("GET", f"/accounts/{acc}/access/apps?domain={q(domain)}&exact=true")
        auds[domain] = apps[0].get("aud") if apps else "<aud of the new app>"
    for name, rules in TUNNELS.items():
        t = st["tunnels"][name]
        want = desired_ingress([(h, svc, auds[h]) for h, svc in rules])
        shown = ", ".join(f"{h} -> {svc}" for h, svc in rules)
        if ingress_matches(t["config"].get("ingress"), want):
            note("ok", f"tunnel {name}: ingress {shown}, Access token required")
        else:
            have = [(r.get("hostname") or "*", r.get("service")) for r in t["config"].get("ingress") or []]
            note("update", f"tunnel {name}: ingress {have} -> {shown}, Access token required, else 404")
            if apply:
                config = dict(t["config"], ingress=want)  # keep warp-routing / tunnel-level originRequest
                cf.call("PUT", f"/accounts/{acc}/cfd_tunnel/{t['id']}/configurations", {"config": config})

    # 5. DNS last.
    for name, host, action, rec_id, reason in dns_steps:
        note(action, f"dns {reason}")
        body = {"type": "CNAME", "name": host, "content": f"{st['tunnels'][name]['id']}.cfargotunnel.com", "proxied": True, "comment": DNS_COMMENT}
        if apply and action == "create":
            cf.call("POST", f"/zones/{zone}/dns_records", body)
        elif apply and action == "update":
            cf.call("PATCH", f"/zones/{zone}/dns_records/{rec_id}", body)
    return changes


def status(cf, out=print):
    """Report each companion tunnel's health; returns the number of unhealthy tunnels."""
    st = gather(cf)
    bad = 0
    for name, t in st["tunnels"].items():
        healthy = t["status"] == "healthy" and t["connections"] > 0
        bad += 0 if healthy else 1
        out(f"{'ok  ' if healthy else 'DOWN'}    tunnel {name}: status={t['status']} connections={t['connections']}")
    return bad


def ssh(host, remote, runner, stdin=None):
    user = os.environ.get("COMPANION_USER", "newlevel")
    if "COMPANION_PASS" not in os.environ:
        raise CloudflareError("set COMPANION_PASS (SSH password of the rig user)")
    env = dict(os.environ, SSHPASS=os.environ["COMPANION_PASS"])
    return runner(
        ["sshpass", "-e", "ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10", f"{user}@{host}", remote],
        input=stdin, text=True, capture_output=True, env=env,
    )


def install_token(cf, ssh_host, runner=subprocess.run):
    """Write TUNNEL_TOKEN for the rig's own tunnel to ENV_FILE (root 0600) and restart the unit."""
    who = ssh(ssh_host, "hostname", runner)
    if who.returncode != 0:
        raise CloudflareError(f"ssh {ssh_host} failed (exit {who.returncode}): {who.stderr.strip()[:300]}")
    rig = who.stdout.strip()
    if rig not in RIG_TUNNEL:
        raise CloudflareError(f"{ssh_host} is '{rig}', not a known rig ({', '.join(RIG_TUNNEL)})")
    tunnel = RIG_TUNNEL[rig]
    st = gather(cf)
    tid = st["tunnels"][tunnel]["id"]
    token = cf.call("GET", f"/accounts/{st['account']}/cfd_tunnel/{tid}/token")
    if not isinstance(token, str) or not token:
        raise CloudflareError(f"tunnel {tunnel}: the API returned no run token")
    if tunnel_id_from_token(token) != tid:
        raise CloudflareError(f"tunnel {tunnel}: the run token is for another tunnel")
    proc = ssh(ssh_host, f"sudo install -m 0600 -o root -g root /dev/stdin {ENV_FILE}", runner, stdin=f"TUNNEL_TOKEN={token}\n")
    if proc.returncode != 0:
        raise CloudflareError(f"writing {ENV_FILE} on {ssh_host} failed (exit {proc.returncode}): {proc.stderr.strip()[:300]}")
    start = ssh(ssh_host, f"if [ -f /etc/systemd/system/{UNIT}.service ]; then sudo systemctl enable {UNIT} && sudo systemctl restart {UNIT} && sleep 5 && systemctl is-active {UNIT}; else echo 'unit not installed (run deploy.sh)'; fi", runner)
    if start.returncode != 0:
        raise CloudflareError(f"{UNIT} on {ssh_host} did not start: {start.stdout.strip()} {start.stderr.strip()[:300]} (sudo journalctl -u {UNIT} -n 50)")
    return f"{ENV_FILE} written on {ssh_host} ({rig}) for tunnel {tunnel} (token length {len(token)}); {UNIT}: {start.stdout.strip()}"


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "plan"
    cf = Cloudflare(read_token())
    if cmd == "plan":
        changes = run(cf, apply=False)
        print(f"{changes} change(s) needed")
        return 3 if changes else 0
    if cmd == "apply":
        run(cf, apply=True)
        left = run(cf, apply=False, out=lambda _l: None)
        if left:
            raise CloudflareError(f"apply finished but {left} change(s) are still pending; run plan")
        print("applied; plan is clean")
        return 0
    if cmd == "status":
        return 1 if status(cf) else 0
    if cmd == "install-token" and len(argv) == 3:
        print(install_token(cf, argv[2]))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except CloudflareError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        sys.exit(1)
