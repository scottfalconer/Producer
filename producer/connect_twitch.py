"""Explicit Twitch public-client device authorization for the selected account.

Only activation instructions and sanitized status are printed. This command does
not post chat, read viewers, start Producer, or start an OBS broadcast.
"""

import argparse
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TOKEN_FILE = ROOT / ".local/twitch-producer-token.json"
SCOPES = frozenset({
    "user:read:chat", "user:write:chat", "channel:manage:broadcast",
    "channel:manage:polls",
})
DEFAULT_SCOPES = frozenset({"user:write:chat"})
OAUTH = "https://id.twitch.tv/oauth2/"


def validate_identity(identity, client_id, login, scopes, *, user_id=None):
    """Bind the new grant to the chosen app/account and only requested scopes."""
    if (
        not isinstance(identity, dict)
        or identity.get("client_id") != client_id
        or identity.get("login") != login
        or not isinstance(identity.get("user_id"), str)
        or not identity["user_id"]
        or (user_id is not None and identity["user_id"] != user_id)
        or not isinstance(identity.get("scopes"), list)
        or set(identity["scopes"]) != set(scopes)
        or type(identity.get("expires_in")) is not int
        or identity["expires_in"] < 120
    ):
        raise ValueError("Twitch account, application or requested scopes did not match.")


def _credential_path(path):
    path = Path(path).expanduser()
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Refusing a linked credential path.")
    return path


def _existing_user(path, client_id, login):
    """Renew the same identity; use another file when connecting another account."""
    if not path.exists():
        return None
    prior = json.loads(path.read_text())
    if (prior.get("client_id") != client_id or prior.get("login") != login
            or not isinstance(prior.get("user_id"), str) or not prior["user_id"]):
        raise ValueError("Credential file belongs to another account or app; choose another --token-file.")
    return prior["user_id"]


def _save(path, payload):
    path = _credential_path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as out:
            temporary = Path(out.name)
            os.fchmod(out.fileno(), 0o600)
            json.dump(payload, out)
            out.flush()
            os.fsync(out.fileno())
        _credential_path(path)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def connect(client_id, login, *, token_file=DEFAULT_TOKEN_FILE, scopes=DEFAULT_SCOPES):
    if not isinstance(client_id, str) or not re.fullmatch(r"[A-Za-z0-9]{1,128}", client_id):
        raise ValueError("Supply your Twitch public application's client ID.")
    if not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,25}", login):
        raise ValueError("Supply the Twitch login to authorize.")
    login = login.lower()
    selected = frozenset(scopes)
    if not selected or not selected <= SCOPES:
        raise ValueError("Choose only supported Twitch production scopes.")
    path = _credential_path(token_file)
    user_id = _existing_user(path, client_id, login)
    scope_text = " ".join(sorted(selected))
    response = requests.post(
        OAUTH + "device", data={"client_id": client_id, "scopes": scope_text},
        timeout=10, allow_redirects=False,
    )
    if response.status_code != 200:
        raise ValueError(f"Device setup returned HTTP {response.status_code}.")
    device = response.json()
    uri = urlparse(device["verification_uri"])
    if uri.scheme != "https" or uri.netloc not in ("www.twitch.tv", "twitch.tv") or uri.path != "/activate":
        raise ValueError("Unexpected Twitch activation destination.")
    print(json.dumps({
        "verification_url": device["verification_uri"],
        "user_code": device["user_code"], "account": login,
        "scopes": sorted(selected),
    }), flush=True)
    deadline = time.monotonic() + min(600, int(device["expires_in"]))
    interval = max(5, int(device.get("interval", 5)))
    while time.monotonic() + interval < deadline:
        time.sleep(interval)
        response = requests.post(
            OAUTH + "token",
            data={"client_id": client_id, "scopes": scope_text,
                  "device_code": device["device_code"],
                  "grant_type": "urn:ietf:params:oauth:grant-type:device_code"},
            timeout=10, allow_redirects=False,
        )
        token = response.json()
        if response.status_code == 200:
            check = requests.get(
                OAUTH + "validate",
                headers={"Authorization": "OAuth " + token["access_token"]},
                timeout=10, allow_redirects=False,
            )
            if check.status_code != 200:
                raise ValueError("New Twitch credential failed validation.")
            identity = check.json()
            validate_identity(identity, client_id, login, selected, user_id=user_id)
            _save(path, {
                "access_token": token["access_token"],
                "refresh_token": token.get("refresh_token"),
                "client_id": client_id, "user_id": identity["user_id"],
                "login": identity["login"], "scopes": identity["scopes"],
            })
            print(json.dumps({
                "connected": True, "account": identity["login"],
                "scopes": identity["scopes"], "credential_file": str(path),
                "credential_mode": "0600", "public_actions": 0,
            }), flush=True)
            return
        error = token.get("error") or token.get("message")
        if error == "authorization_pending":
            continue
        if error == "slow_down" or response.status_code == 429:
            interval += 5
            continue
        raise ValueError(f"Authorization stopped, HTTP {response.status_code}; no credential replaced.")
    raise ValueError("Device approval expired; no credential replaced.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-id", required=True, help="Client ID of your Public Twitch application; no client secret.")
    parser.add_argument("--login", required=True, help="Expected broadcaster or separate Producer account login.")
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE,
                        help="Private output file; choose a different file for a separate account.")
    parser.add_argument("--scope", action="append", choices=sorted(SCOPES),
                        help="Repeat for each requested scope; default is only user:write:chat.")
    args = parser.parse_args(argv)
    try:
        connect(args.client_id, args.login, token_file=args.token_file,
                scopes=args.scope or DEFAULT_SCOPES)
    except (OSError, TypeError, ValueError, KeyError, requests.RequestException):
        # Neither provider response bodies nor credential values belong in logs.
        parser.exit(1, "Twitch connection did not complete. Check the chosen account, public app, scopes and output path; no token content logged.\n")


if __name__ == "__main__":
    main()
