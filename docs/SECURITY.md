# Privacy and security

Producer is a local prototype, not a hardened multi-user or hosted service. Run it on a computer you control. The main control server and OBS media receiver bind to loopback. The optional read-only phone display uses a separate listener on an explicitly selected private IPv4 address. Do not expose any of these listeners through a public tunnel or public interface.

## Optional local-network phone display

`--phone-bind` enables the separate phone listener; `--phone-port` selects its port. It accepts an explicit private IPv4 address, not every interface. The phone page reads only the current presenter cue and input presence. It has no control API and receives no show notes, research packet, transcript, provider credential or Twitch token.

Each helper start creates a fresh pairing link. Its token authorizes access to the current cue; anyone who obtains it and can reach the listener can read that cue. The link is printed in the terminal unless `--phone-link-file` selects a local JSON file, which is written with owner-only permissions (`0600`). Keep both the link and file private. Stopping the helper revokes the link, and restarting requires the new one.

Phone access uses plain HTTP rather than TLS. The pairing token and cue travel over the selected local network, so use a trusted network. A private address and pairing link do not provide protection against a network observer. Keep the phone and its pairing link outside OBS and any shared screen capture. See [SETUP](SETUP.md#5-optional-phone-confidence-monitor) for configuration.

## What leaves the computer

When you start a show, the selected audio, sampled images, notes, selected research packet and relevant show context are processed by your configured OpenAI account. Offline mode simulates Twitch actions; it does not disable paid OpenAI processing. The optional consulting editor also uses OpenAI.

The active demo reads fictional incoming comments from program images. It does not fetch real Twitch chat into the Realtime director. If actual viewer content appears in a source you select, consider the rights and permissions for sending that source to your provider. Do not assume public visibility alone authorizes every use.

Live Twitch actions are separately enabled. Only confirmed public deliveries count as success. A broadcaster-labeled post retains the broadcaster's real Twitch username with `(From Producer)` attribution; it is not a separate identity.

## Local storage

- The API key stays in the supplied private file on the helper host; it is not sent to browser pages or included in prompts.
- OAuth access/refresh tokens stay in private local files and must not be committed.
- Show-scoped transcript text, action receipts and simulated timelines are retained under `.local/producer/`.
- Browser setup can remember your notes and selected research packet in that browser profile.
- Raw browser media is not retained by the active live route. Native OBS program video is saved only with `--obs-recording-dir`.
- Recorded-show test bundles intentionally retain selected media derivatives, prompt/context snapshots, transcript and action/scene evidence for local replay.

Treat recordings, debug bundles and screenshots as private show material. Inspect them before sharing. The application does not promise automatic retention expiry; manage local files deliberately after ending the show. Do not delete credentials or active-session data while a show is using them.

## Enforcement boundaries

The main helper binds to loopback, serves allowlisted assets and validates browser mutation origin/token. The separate phone listener allowlists its assets and cue fields, validates its expected host and pairing token, and rejects write methods. Materials and tool payloads are bounded. Selected source/session identity, current output identity and Stop checks reject stale actions. Transcript retrieval is show-scoped and past-only. Public writes use identity/capability checks and a durable ledger rather than success assumptions or blind retries.

Program speech, visible chat, notes and research are untrusted context. They must not expand the model's available tools or override privacy/permission boundaries. Direct spoken-command execution is inactive. The model can still make factual or editorial mistakes, including misreading a sampled image; those are not prevented by a structurally valid tool call.

## Reporting

For a possible credential exposure or security vulnerability, do not paste tokens, private recordings or personal transcripts into a public issue. Describe the affected version and general impact without exploit details or secrets, and arrange private contact with the repository maintainer before transferring sensitive evidence. If a credential has been exposed, revoke it with its issuing provider rather than relying on removal from a file or commit alone.
