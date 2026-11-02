# Producer

Producer is an AI live director for solo streamers. It follows your selected show audio and sampled images, uses your notes and optional research packet, and chooses what helps the show on two separate screens:

- **Presenter:** a quiet confidence monitor with timely reminders, audience questions and useful next steps.
- **Audience:** a transparent OBS overlay with relevant questions, references, facts and images.

One ongoing OpenAI Realtime session makes the editorial choices. Local Python code validates and routes its actions, maintains screen state, and handles source and session boundaries. Producer does not generate a speaking voice. Automatic subtitles and spoken commands are off by default; direct spoken commands are not part of the active flow.

## Try it

You need Python 3.12 and an OpenAI Platform API key with access to the configured Realtime model. The tested host is macOS. Direct OBS input and recorded-show rehearsals also need FFmpeg on your PATH. Node/Playwright are optional development and replay-rendering tools, not a prerequisite for the live screens.

```sh
python3.12 -m venv .venv-producer
. .venv-producer/bin/activate
python -m pip install -r producer/requirements.txt
python producer/server.py --port 8786 --key-file /absolute/path/to/openai-key.txt
```

Open [Producer](http://127.0.0.1:8786/), paste a few show notes, choose **Microphone only** under **Source and output**, and press **Start offline test**. Use [the setup guide](docs/SETUP.md) for direct OBS picture/audio, both screens, materials, and optional Twitch actions.

**Offline means simulated Twitch actions, not free or local AI.** Starting a show sends its selected feed, notes and research to OpenAI and uses your API account. You do not need a Twitch connection for an offline rehearsal. Without a key, you can start the helper and inspect the design previews; model processing requires the key.

## OBS and Twitch

The direct OBS route sends the finished programme and full selected audio mix to a loopback receiver. The browser displays the screens; it does not need webcam or microphone permissions for this route. Producer's audience overlay can sit above your existing OBS scene. The confidence monitor stays outside the programme.

Twitch production is optional and off by default. It requires an authorized account, an explicit helper flag, and **Use live Twitch actions** for that show. The current live path can share current public output in chat and execute reviewed production plans. A broadcaster account can use the literal `(From Producer)` attribution; a separately authorized account is also supported. Producer never starts OBS broadcasting.

For demonstrations, the optional simulated-chat layer provides fictional incoming comments. The selected programme images carry those comments to the model. They are not real Twitch API chat messages, and the opening overlay discloses the simulation. Confirmed Producer posts can appear in that feed too.

## Project map

- [Setup and troubleshooting](docs/SETUP.md)
- [Architecture and data flow](docs/ARCHITECTURE.md)
- [Development and tests](docs/CONTRIBUTING.md)
- [Privacy and security boundaries](docs/SECURITY.md)
- [Editable director prompt](producer/realtime_prompt.md)

This is a working prototype built with Codex for the Codex x TwitchCon hackathon. Native OBS/OpenAI screen output and one real attributed Twitch chat delivery have been exercised separately. Model timing and usefulness vary; a successful action or replay does not prove editorial quality. Long broadcasts, cross-platform installation and autonomous live production need further testing.

Original Producer code is released under the [MIT license](LICENSE). Bundled fonts retain their SIL Open Font License notices, and the country outline derives from public-domain Natural Earth data. See the accompanying asset notices. Recordings, credentials, personal show materials and development conversation archives are not included.
