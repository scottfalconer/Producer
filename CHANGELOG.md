# Changes

## 0.1.0 — November 2, 2026

- One Realtime director coordinates a private presenter monitor and public OBS overlay.
- Direct OBS program/audio input, optional browser input, show notes and research packets.
- Simulated incoming chat, offline-by-default production, and explicitly enabled Twitch actions.
- Timestamped local show memory, recording rehearsal and API-free action replay tools.
- Generic Twitch account connection, standalone credential loading, and a clean setup guide.
- Optional read-only phone confidence monitor and a separated OBS demo layout.
- Confirmed Producer posts appear in the visible chat; fictional incoming fixtures cannot impersonate Producer.
- Corrected late audio-acknowledgement handling so transcript timestamps cannot shift into a later window.
- Updated Requests, Pillow and websockets to reviewed security-fixed versions.

The first release is a macOS-tested prototype. Hosted CI checks local fixtures;
they do not establish live model or Twitch behavior. Personal recordings and
credentials are not included; optional recording tests skip without private media.
