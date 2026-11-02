"""Local Helix signal. The editorial producer never receives this object."""

import json
import os
import tempfile
import time
from pathlib import Path
import requests
from engine import ROOT


def authorization(path=None):
    production_token = ROOT / ".local/twitch-producer-token.json"
    path = Path(
        path
        or os.environ.get(
            "TWITCH_TOKEN_FILE",
            production_token
            if production_token.exists()
            else ROOT / ".local/twitch-token.json",
        )
    )
    token = json.loads(path.read_text())

    def validate():
        return requests.get(
            "https://id.twitch.tv/oauth2/validate",
            headers={"Authorization": "OAuth " + token["access_token"]},
            allow_redirects=False,
            timeout=8,
        )

    auth = validate()
    if auth.status_code == 401 and token.get("refresh_token"):
        response = requests.post(
            "https://id.twitch.tv/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "client_id": token["client_id"],
                "refresh_token": token["refresh_token"],
            },
            allow_redirects=False,
            timeout=8,
        )
        if response.status_code != 200:
            raise ValueError("Twitch sign-in needs renewal.")
        refreshed = response.json()
        token.update(
            access_token=refreshed["access_token"],
            refresh_token=refreshed.get("refresh_token", token["refresh_token"]),
        )
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
            json.dump(token, f)
        os.replace(f.name, path)
        auth = validate()
    if auth.status_code != 200:
        raise ValueError("Twitch sign-in needs renewal.")
    identity = auth.json()
    if (
        identity.get("client_id") != token["client_id"]
        or identity.get("user_id") != token["user_id"]
    ):
        raise ValueError("Twitch identity mismatch.")
    headers = {
        "Authorization": "Bearer " + token["access_token"],
        "Client-Id": identity["client_id"],
    }
    return token, identity, headers


def channel_status(path=None):
    token, identity, headers = authorization(path)

    def get(route, params):
        response = requests.get(
            "https://api.twitch.tv/helix/" + route,
            params=params,
            headers=headers,
            allow_redirects=False,
            timeout=8,
        )
        if response.status_code != 200:
            raise ValueError(f"Helix returned HTTP {response.status_code}.")
        return response.json()["data"]

    channels = get("channels", {"broadcaster_id": identity["user_id"]})
    streams = get("streams", {"user_id": identity["user_id"]})
    if not channels:
        raise ValueError("No broadcaster metadata returned.")
    return {
        "channel": identity["login"],
        "title": channels[0]["title"],
        "category": channels[0]["game_name"],
        "online": bool(streams),
        "viewers": streams[0]["viewer_count"] if streams else None,
        "checked": int(time.time()),
    }


if __name__ == "__main__":
    print(json.dumps(channel_status()))
