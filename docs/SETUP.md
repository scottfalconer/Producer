# Set up Producer

Start with an offline rehearsal, then add OBS or live Twitch actions when you want them. The presenter screen is passive; configuration and Stop/Pause controls live outside it.

## 1. Install and start

The implementation has been exercised on macOS with Python 3.12, FFmpeg 7.1.1 and OBS 33. Other versions may work but are not established by that test. Native capture and secure file handling use POSIX primitives; Windows is not currently a validated target.

Install Python 3.12 and, for OBS or recorded video, FFmpeg using your normal package manager. Verify `python3.12 --version` and `ffmpeg -version`. Then, from the repository root:

```sh
python3.12 -m venv .venv-producer
. .venv-producer/bin/activate
python -m pip install -r producer/requirements.txt
python producer/server.py --port 8786 --key-file /absolute/path/to/openai-key.txt
```

Save your OpenAI Platform key in a private text file outside version control. Keep it out of notes, research, screenshots and logs. This command uses `--key-file`; setting `OPENAI_API_KEY` alone does not configure the helper. Your API account needs access to the configured models. The current director is `gpt-realtime-2.1`, with `gpt-4o-mini-transcribe` for transcript memory; `PRODUCER_MODEL` configures the optional Responses consulting editor.

Keep the terminal running. Open <http://127.0.0.1:8786/>. The main helper binds only to loopback. Starting it does not capture media, run inference or start OBS. **Start offline test** uses OpenAI processing but keeps Twitch operations simulated.

For a no-key preview, omit `--key-file` and open <http://127.0.0.1:8786/design>. Preview content is authored; it does not prove a model result.

## 2. Add your show notes

A sentence, rough bullets or ordinary paragraphs are enough. Paste them into **Show notes** or open a UTF-8 `.md`/`.txt` file. There is no required template.

Optionally open **Research packet**, then paste or open background material. Useful packets include concrete facts, links, dates, limitations and earlier-episode references. Producer can use relevant material; it does not fetch those URLs or independently verify the packet. Notes support up to 20,000 characters; research supports up to 32,000 characters / 128 KB.

To keep materials together, put them in a folder:

```text
my-show/
  notes.md
  research-packet.md
  diagram.png
```

Then add `--show-folder /absolute/path/to/my-show` to the helper command. Ordinary Markdown/text becomes private show context; one reserved `research-packet.md` or `.txt` becomes the research packet. Public still images are cataloged for the model to select. Supported limits are twelve images, 4 MB each, in PNG/JPEG/WebP formats. Subfolders, symlinks and unsupported files are excluded. The folder's `README.md` is ignored.

Pasted/imported notes and research override the folder selection. **Remove packet** explicitly selects no research, including after a page refresh. Start pins the selected materials; End before changing them for another show.

## 3. Try the screens without OBS

1. Under **Source and output**, choose **Microphone only**, **Camera and microphone**, or **Shared browser tab and its audio**. For tab input, select the intended tab and enable sharing its audio.
2. Keep **Let Producer on air** enabled. Leave **Use live Twitch actions** unchecked. Subtitles are off unless you choose them.
3. Press **Start offline test** and grant the selected browser device permission. Present normally; the private monitor shows one useful cue or stays quiet.
4. Open <http://127.0.0.1:8786/on-air> to see the audience graphics. It is a transparent overlay and can be blank until Producer chooses content. It contains no source video by itself.
5. Press **Escape** to return to controls. **Pause** releases input; **Resume** preserves show identity and transcript. **End show** ends the session.

Keep the Producer control/monitor page open. Switching focus is supported; closing or reloading that page currently ends the show. Model output is not guaranteed on every interval. An explicit source or provider error pauses/stops processing rather than silently choosing another feed or model.

## 4. Feed the complete OBS program directly

Start the helper with a separate local input port:

```sh
python producer/server.py --port 8786 --key-file /absolute/path/to/openai-key.txt --obs-input-port 8790
```

To save a local video too, add `--obs-recording-dir /absolute/path/to/private-recordings`. Without that option the native program is processed without retaining its raw media. With it, each connection is saved as a private `.ts` file. The browser remains at port 8786; the OBS receiver uses port 8790.

In a separate OBS rehearsal profile:

1. Open **Settings → Output → Advanced → Recording**.
2. Select **Custom Output (FFmpeg)** and **Output to URL**.
3. Set the URL to `tcp://127.0.0.1:8790` and container to `mpegts`.
4. The exercised setup used default MPEG-2 video/MP2 audio, 6000 Kbps video, 30-frame keyframe interval, 160 Kbps audio, 1280×720 output at 30 FPS, and audio track 1.
5. Select the audio track containing your microphone and the full computer/media mix. Check the source meters and track assignments. The helper decodes the first audio stream; it does not combine isolated tracks.

This replaces that OBS profile's ordinary file-recording destination. **To retain a local program file, use the helper's `--obs-recording-dir` option.** Use another profile when you want ordinary OBS file recording.

Keep your existing camera/shared content. Add transparent OBS Browser Sources at the scene canvas size:

- `http://127.0.0.1:8786/simulated-chat` for optional fictional demo comments.
- `http://127.0.0.1:8786/on-air` above it for Producer's graphics.

Do not capture the private confidence monitor into the program. Producer must receive the composition containing chat if you want it to read those comments. A separate unshared chat window is not visible to it.

In Producer select **OBS picture and audio**. Click **Start offline test**, then **Start Recording** in OBS. Decoded picture and audio start the Realtime session. This requires no virtual camera, browser device permission or virtual audio driver. Do not click **Start Streaming** for a local rehearsal.

Pause releases the feed. Stop any remaining OBS recording before resuming; click Resume in Producer, then Start Recording in OBS again. End the show and stop OBS recording when finished. The loopback listener waits up to two minutes for OBS after Start.

### Optional combined broadcast layout

Instead of separate `/on-air` and `/simulated-chat` Browser Sources, add `http://127.0.0.1:8786/demo-layout` at **1920×1080** above your native camera and shared-window sources. This transparent overlay places Producer's public graphics over the shared window and fictional chat below the camera. Hide the separate overlay sources to avoid duplicates.

For a 1920×1080 OBS canvas, use these transform bounds with centered **Fit** so each source keeps its aspect ratio:

| Source | X | Y | Width | Height |
| --- | ---: | ---: | ---: | ---: |
| Shared window | 24 | 24 | 1296 | 1032 |
| Camera | 1344 | 24 | 552 | 310.5 |

The chat region occupies (1344, 358) within 552×698. The overlay includes neither the camera nor the shared-window video itself. It reads the current show's public output; opening it does not start capture, inference, Twitch actions or broadcasting. `/demo-layout?preview=1` shows labeled fictional examples without starting a show. Remove `?preview=1` for the actual program.

Keep the private monitor and Producer controls outside any captured window or desktop. If the shared application contains an embedded browser, close private Producer pages inside that window before capturing it.

## 5. Optional phone confidence monitor

Connect your phone and computer to the same trusted local network. Start the helper with the computer's explicit private IPv4 address, a separate phone port and an optional private pairing-link file. Replace the example address below with your computer's address; retain the OBS, recording and show-material flags you use.

```sh
python producer/server.py \
  --port 8786 --key-file /absolute/path/to/openai-key.txt \
  --obs-input-port 8790 \
  --phone-bind 192.168.1.25 --phone-port 8787 \
  --phone-link-file /absolute/path/to/private/phone-link.json
```

Open the file's `pairing_url` on your phone, including everything after `#`. Anyone on that network with the link can read the current presenter cue, so keep it out of the broadcast, screenshots and shared logs. The file is written with owner-only permissions (`0600`). Without `--phone-link-file`, the helper prints the private link in its terminal. Restarting the helper creates a new link and invalidates the previous one.

The phone mirrors the current cue and input indicator. It shows **Waiting for Producer** before an active show and may be blank when Producer has no cue. Start, Pause and End stay on the computer; keep the computer's Producer control page open. The phone cannot start capture, change screens or keep an ended source running. Adjust its screen timeout as needed; automatic wake lock support depends on the browser.

The main helper and OBS receiver remain on loopback. Only the separate read-only phone listener binds to the chosen local address. It uses plain HTTP, with no public tunnel or TLS; use a trusted network. It exposes no show notes, research packet, transcript or control API. If the phone cannot connect, check the address, firewall permission for the phone port and whether the Wi-Fi isolates devices. See [SECURITY](SECURITY.md#optional-local-network-phone-display) for the access boundary.

## 6. Optional real Twitch production

Offline remains the default. Connecting Twitch alone does not enable model-directed public writes.

Register your own **Public** client application in the [Twitch developer console](https://dev.twitch.tv/console/apps). The connector uses Twitch's [Device Code Grant Flow](https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/#device-code-grant-flow); it does not require a client secret. Run:

```sh
python producer/connect_twitch.py --client-id YOUR_PUBLIC_APP_CLIENT_ID --login YOUR_TWITCH_LOGIN
```

Follow the displayed Twitch activation URL and code, inspect the account/scopes, and authorize it yourself. The default grant requests only `user:write:chat`. The resulting token stays in `.local/twitch-producer-token.json` with private file permissions; it is never printed. No public message is sent by connection.

For reviewed markers/title/category plans, explicitly request both scopes:

```sh
python producer/connect_twitch.py --client-id YOUR_PUBLIC_APP_CLIENT_ID --login YOUR_TWITCH_LOGIN --scope user:write:chat --scope channel:manage:broadcast
```

Native polls require `channel:manage:polls` and Twitch eligibility. A token scope does not prove a capability is available. Markers require a live stream and the applicable Twitch recording configuration. Do not request scopes for features you will not use.

To use your broadcaster account with attribution while keeping incoming chat fictional:

```sh
python producer/server.py --port 8786 --key-file /absolute/path/to/openai-key.txt --obs-input-port 8790 --enable-live-production --producer-chat-sender broadcaster_labelled --simulate-chat
```

Then explicitly check **Use live Twitch actions** under **Source and output**, and press **Start show**. Existing public plans still need review. For OBS, Start Recording supplies the feed. **No action here starts broadcasting.**

The live director can share current public output or execute reviewed plans. Dynamic model-composed polls, markers, title/category changes and API poll-result forwarding remain disabled in live mode. The literal `(From Producer)` prefix identifies the contribution, while Twitch still displays your actual username. The optional `separate` sender mode uses an independently authorized token selected with `TWITCH_PRODUCER_SENDER_TOKEN_FILE`; no account is created and there is no silent fallback between identities. `TWITCH_TOKEN_FILE` overrides the broadcaster credential location.

Incoming comments remain simulated with `--simulate-chat`; no real viewer chat is fetched into the director. Confirmed real Producer posts can join that layer using their actual sender and wording. End resets the next setup to offline. The chosen mode stays pinned through Pause/Resume.

## Recording and replay

A recorded-show test explicitly runs paid inference on your own selected file:

```sh
python producer/run-recorded-show.py --source /absolute/path/to/your-video.mp4 --key-file /absolute/path/to/openai-key.txt --notes-file /absolute/path/to/notes.md --show-folder /absolute/path/to/my-show --seconds 600 --bundle .local/producer/my-rehearsal
```

Choose a new bundle directory. The runner supports 1–600 seconds, keeps Twitch simulated, and generates fresh fictional chat unless given a seed or event file. Your recordings are not bundled with the project. The Office Hours browser recording preset is unavailable until its optional local clip is prepared; it is not needed for microphone/OBS use.

To repeat local tool validation/rendering without another model run:

```sh
python producer/replay-director-actions.py --bundle .local/producer/my-rehearsal
node producer/render-director-replay.cjs --bundle .local/producer/my-rehearsal --scenes replayed-scenes.jsonl --output .local/producer/my-rehearsal/review.mp4
```

The renderer requires the optional Node/Playwright setup in [CONTRIBUTING](CONTRIBUTING.md), plus FFmpeg. Saved-action replay tests how local code reacts to model output, not whether the model would make better decisions. The ordinary live OBS session retains transcript/action records and optional program video, but does not yet produce this complete replay bundle.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Empty audience page | The overlay is transparent. Check the selected source and Start state; Producer may choose quiet. `/on-air?preview=1` shows authored preview content. |
| Waiting for OBS | Start Producer first, then Start Recording in the matching OBS profile. Check URL/port, container and audio track. |
| Picture but no speech | Confirm the microphone meter, device and selected mix track in OBS. A disconnected device can remain listed. |
| Provider connection fails | Check the private key file and API account model access. Offline mode still needs OpenAI. There is no automatic provider fallback. |
| Chat is invisible to Producer | Include the chat layer in the actual captured program, with legible size and enough time across sampled frames. |
| Live actions unavailable | Check both helper enablement and per-show live selection, token identity/scopes, and Twitch channel capability. Do not infer success from an attempted call. |
| Port already used | End the old helper or choose another screen/input port and update the OBS URLs to match. |

See [SECURITY](SECURITY.md) before sharing logs, recordings or a public demonstration.
