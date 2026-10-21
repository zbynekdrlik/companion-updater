#!/usr/bin/env python3
"""Cloudflare side of the Companion web UIs (companion-updater#26).

Desired state:
  - tunnel `companion-snv` serves companion-snv.newlevel.media (and the older
    alias companion.newlevel.media), tunnel `companion-pp` serves
    companion-pp.newlevel.media; both point at the rig's Companion UI on
    http://localhost:8000, everything else is a 404;
  - a proxied DNS CNAME per hostname to its tunnel;
  - a Cloudflare Access app per new hostname that lets the owner's e-mail in
    through the account's one-time-PIN login, with a long session so every
    device logs in once;
  - a Cloudflare notification that e-mails the owner when either companion
    tunnel goes down, so an outage is never again found only by chance
    (the tunnels sat dead for weeks after the presenter install replaced
    the rig's cloudflared.service).

Usage:
  cloudflare_companion.py plan                 # show what would change (default)
  cloudflare_companion.py apply                # make the changes
  cloudflare_companion.py install-token <tunnel> <ssh-host>
      # fetch the tunnel's run token and write it to /etc/cloudflared-companion.env
      # on the rig (root 0600) without ever printing it. SSH password: $COMPANION_PASS.

The Cloudflare API token is read from $CF_TOKEN_FILE
(default ~/.secrets/cloudflare-newlevel-full) and is never printed.
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

API = "https://api.cloudflare.com/client/v4"
ZONE = "newlevel.media"
ORIGIN = "http://localhost:8000"
TUNNELS = {
    "companion-snv": ["companion-snv.newlevel.media", "companion.newlevel.media"],
    "companion-pp": ["companion-pp.newlevel.media"],
}
# Only the new hostnames get an Access app from here; companion.newlevel.media
# keeps the app ("newlevel", policy "basic") that the owner set up by hand.
ACCESS_HOSTS = {
    "companion-snv.newlevel.media": "Companion snv · claude",
    "companion-pp.newlevel.media": "Companion pp · claude",
}
OWNER_EMAILS = ["drlik.zbynek@gmail.com"]
POLICY_NAME = "Companion owner (OTP email) · claude"
# Owner 2026-10-21: "raz prihlásim zariadenie" -- a device logged in once keeps the
# Companion UI for 120 days. The first login on a device is usually free anyway: the
# Access organisation's shared login (730h) from any other newlevel app covers it.
SESSION_DURATION = "2880h"
DNS_COMMENT = "companion-updater#26 · claude"
ALERT_NAME = "Companion tunnels down · claude"
ALERT_STATUSES = ["TUNNEL_STATUS_TYPE_DOWN"]
ENV_FILE = "/etc/cloudflared-companion.env"


class CloudflareError(Exception):
    pass


# ---------------------------------------------------------------- pure planning


def desired_ingress(hostnames):
    """The tunnel ingress list: every hostname to the Companion UI, then a 404."""
    return [{"hostname": h, "service": ORIGIN} for h in hostnames] + [{"service": "http_status:404"}]


def ingress_matches(current, desired):
    """True when the hostname/service sequence is the same (originRequest details ignored)."""
    norm = lambda rules: [(r.get("hostname"), r.get("service")) for r in (rules or [])]
    return norm(current) == norm(desired)


def dns_plan(records, hostname, tunnel_id):
    """What to do with the DNS records named `hostname` so it is a proxied CNAME to the tunnel.

    Returns ("ok"|"create"|"update"|"conflict", record_id_or_None, reason).
    """
    target = f"{tunnel_id}.cfargotunnel.com"
    mine = [r for r in records if r.get("name") == hostname]
    if not mine:
        return ("create", None, f"no record for {hostname}")
    if len(mine) > 1 or mine[0].get("type") != "CNAME":
        kinds = ", ".join(f"{r.get('type')} {r.get('content')}" for r in mine)
        return ("conflict", None, f"{hostname} already has {kinds}; not touching it")
    rec = mine[0]
    if rec.get("content") == target and rec.get("proxied") is True:
        return ("ok", rec.get("id"), f"{hostname} -> {target}")
    return ("update", rec.get("id"), f"{hostname} -> {rec.get('content')} (proxied={rec.get('proxied')}), want {target}")


def access_app_payload(domain, name, idp_id):
    return {
        "name": name,
        "domain": domain,
        "type": "self_hosted",
        "session_duration": SESSION_DURATION,
        "allowed_idps": [idp_id],
        "auto_redirect_to_identity": True,
        "app_launcher_visible": True,
        "http_only_cookie_attribute": True,
        "policies": [
            {
                "name": POLICY_NAME,
                "decision": "allow",
                "precedence": 1,
                "include": [{"email": {"email": e}} for e in OWNER_EMAILS],
            }
        ],
    }


def _policy_emails(app):
    emails = set()
    for policy in app.get("policies") or []:
        if policy.get("decision") != "allow":
            continue
        for rule in policy.get("include") or []:
            if "email" in rule:
                emails.add(rule["email"].get("email"))
    return emails


def access_plan(apps, domain, payload):
    """Returns ("ok"|"create"|"update", app_id_or_None, reason) for the app on `domain`."""
    mine = [a for a in apps if a.get("domain") == domain]
    if not mine:
        return ("create", None, f"no Access app for {domain}")
    app = mine[0]
    wanted_emails = {e for p in payload["policies"] for r in p["include"] for e in [r["email"]["email"]]}
    diffs = []
    for key in ("type", "session_duration", "allowed_idps", "auto_redirect_to_identity"):
        if app.get(key) != payload[key]:
            diffs.append(f"{key} {app.get(key)!r} -> {payload[key]!r}")
    if _policy_emails(app) != wanted_emails:
        diffs.append(f"allowed e-mails {sorted(_policy_emails(app))} -> {sorted(wanted_emails)}")
    if not diffs:
        return ("ok", app.get("id"), f"{domain}: Access app '{app.get('name')}' as wanted")
    return ("update", app.get("id"), f"{domain}: " + "; ".join(diffs))


def alert_payload(tunnel_ids):
    return {
        "name": ALERT_NAME,
        "description": "companion-updater#26: a companion web UI tunnel went down",
        "enabled": True,
        "alert_type": "tunnel_health_event",
        "filters": {"tunnel_id": sorted(tunnel_ids), "new_status": list(ALERT_STATUSES)},
        "mechanisms": {"email": [{"id": e} for e in OWNER_EMAILS]},
    }


def alert_plan(policies, payload):
    """Returns ("ok"|"create"|"update", policy_id_or_None, reason) for the tunnel-down alert."""
    mine = [p for p in policies if p.get("name") == payload["name"]]
    if not mine:
        return ("create", None, f"no notification '{payload['name']}'")
    pol = mine[0]
    filters = pol.get("filters") or {}
    emails = sorted(m.get("id") for m in ((pol.get("mechanisms") or {}).get("email") or []))
    diffs = []
    if pol.get("alert_type") != payload["alert_type"]:
        diffs.append(f"alert_type {pol.get('alert_type')}")
    if not pol.get("enabled"):
        diffs.append("disabled")
    if sorted(filters.get("tunnel_id") or []) != payload["filters"]["tunnel_id"]:
        diffs.append(f"tunnels {filters.get('tunnel_id')}")
    if sorted(filters.get("new_status") or []) != sorted(payload["filters"]["new_status"]):
        diffs.append(f"statuses {filters.get('new_status')}")
    if emails != sorted(e["id"] for e in payload["mechanisms"]["email"]):
        diffs.append(f"e-mails {emails}")
    if not diffs:
        return ("ok", pol.get("id"), f"notification '{payload['name']}' as wanted")
    return ("update", pol.get("id"), f"notification '{payload['name']}': " + "; ".join(diffs))


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
                payload = json.loads(res.read().decode())
        except urllib.error.HTTPError as err:
            try:
                payload = json.loads(err.read().decode())
            except ValueError:
                raise CloudflareError(f"{method} {path}: HTTP {err.code}") from err
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


def context(cf):
    zones = cf.call("GET", f"/zones?name={ZONE}")
    if not zones:
        raise CloudflareError(f"zone {ZONE} not visible to this token")
    zone_id, account_id = zones[0]["id"], zones[0]["account"]["id"]
    tunnels = {}
    for name in TUNNELS:
        found = cf.call("GET", f"/accounts/{account_id}/cfd_tunnel?name={name}&is_deleted=false")
        if not found:
            raise CloudflareError(f"tunnel {name} does not exist")
        tunnels[name] = found[0]["id"]
    idps = [i for i in cf.call("GET", f"/accounts/{account_id}/access/identity_providers") if i.get("type") == "onetimepin"]
    if not idps:
        raise CloudflareError("no one-time-PIN identity provider in the account")
    return zone_id, account_id, tunnels, idps[0]["id"]


def run(cf, apply, out=print):
    """Plan (and with apply=True, make) every change. Returns the number of changes needed/made."""
    zone_id, account_id, tunnels, idp_id = context(cf)
    changes = 0
    for name, hostnames in TUNNELS.items():
        tid = tunnels[name]
        current = cf.call("GET", f"/accounts/{account_id}/cfd_tunnel/{tid}/configurations")
        cur_ingress = ((current or {}).get("config") or {}).get("ingress")
        want = desired_ingress(hostnames)
        if ingress_matches(cur_ingress, want):
            out(f"ok      tunnel {name}: ingress {', '.join(hostnames)} -> {ORIGIN}")
        else:
            changes += 1
            have = [(r.get("hostname") or "*", r.get("service")) for r in (cur_ingress or [])]
            out(f"{'set' if apply else 'would'}     tunnel {name}: ingress {have} -> {[(r.get('hostname') or '*', r['service']) for r in want]}")
            if apply:
                cf.call("PUT", f"/accounts/{account_id}/cfd_tunnel/{tid}/configurations", {"config": {"ingress": want}})
        for host in hostnames:
            records = cf.call("GET", f"/zones/{zone_id}/dns_records?name={host}")
            action, rec_id, reason = dns_plan(records, host, tid)
            body = {"type": "CNAME", "name": host, "content": f"{tid}.cfargotunnel.com", "proxied": True, "comment": DNS_COMMENT}
            if action == "ok":
                out(f"ok      dns {reason}")
            elif action == "conflict":
                raise CloudflareError(f"dns {reason}")
            else:
                changes += 1
                out(f"{'set' if apply else 'would'}     dns {action}: {reason}")
                if apply and action == "create":
                    cf.call("POST", f"/zones/{zone_id}/dns_records", body)
                elif apply:
                    cf.call("PATCH", f"/zones/{zone_id}/dns_records/{rec_id}", body)
    apps = cf.call("GET", f"/accounts/{account_id}/access/apps?per_page=500")
    for domain, name in ACCESS_HOSTS.items():
        payload = access_app_payload(domain, name, idp_id)
        action, app_id, reason = access_plan(apps, domain, payload)
        if action == "ok":
            out(f"ok      access {reason}")
            continue
        changes += 1
        out(f"{'set' if apply else 'would'}     access {action}: {reason}")
        if apply and action == "create":
            cf.call("POST", f"/accounts/{account_id}/access/apps", payload)
        elif apply:
            cf.call("PUT", f"/accounts/{account_id}/access/apps/{app_id}", payload)
    payload = alert_payload(tunnels.values())
    action, pol_id, reason = alert_plan(cf.call("GET", f"/accounts/{account_id}/alerting/v3/policies"), payload)
    if action == "ok":
        out(f"ok      alert {reason}")
    else:
        changes += 1
        out(f"{'set' if apply else 'would'}     alert {action}: {reason}")
        if apply and action == "create":
            cf.call("POST", f"/accounts/{account_id}/alerting/v3/policies", payload)
        elif apply:
            cf.call("PUT", f"/accounts/{account_id}/alerting/v3/policies/{pol_id}", payload)
    return changes


def install_token(cf, tunnel, ssh_host, runner=subprocess.run):
    """Write TUNNEL_TOKEN=<token> to ENV_FILE on the rig (root, 0600). The token goes over stdin only."""
    _, account_id, tunnels, _ = context(cf)
    if tunnel not in tunnels:
        raise CloudflareError(f"unknown tunnel {tunnel}; known: {', '.join(tunnels)}")
    token = cf.call("GET", f"/accounts/{account_id}/cfd_tunnel/{tunnels[tunnel]}/token")
    if not isinstance(token, str) or not token:
        raise CloudflareError(f"tunnel {tunnel}: the API returned no run token")
    if "COMPANION_PASS" not in os.environ:
        raise CloudflareError("set COMPANION_PASS (SSH password of the rig user)")
    env = dict(os.environ, SSHPASS=os.environ["COMPANION_PASS"])
    remote = f"sudo install -m 0600 -o root -g root /dev/stdin {ENV_FILE}"
    proc = runner(
        ["sshpass", "-e", "ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10", f"newlevel@{ssh_host}", remote],
        input=f"TUNNEL_TOKEN={token}\n", text=True, capture_output=True, env=env,
    )
    if proc.returncode != 0:
        raise CloudflareError(f"writing {ENV_FILE} on {ssh_host} failed (exit {proc.returncode}): {proc.stderr.strip()[:300]}")
    return f"{ENV_FILE} written on {ssh_host} for tunnel {tunnel} (token length {len(token)})"


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "plan"
    cf = Cloudflare(read_token())
    if cmd in ("plan", "apply"):
        changes = run(cf, apply=(cmd == "apply"))
        print(f"{changes} change(s) {'made' if cmd == 'apply' else 'needed'}")
        return 0
    if cmd == "install-token" and len(argv) == 4:
        print(install_token(cf, argv[2], argv[3]))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except CloudflareError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        sys.exit(1)
