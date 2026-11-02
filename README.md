# Producer

Producer is an AI live director that helps streamers make a better show while they keep presenting. It follows your audio and sampled video frames, using your show notes and optional research to decide what deserves attention.

- **For you:** a private confidence monitor with audience questions, useful reminders, and a steady reference to what comes next. View it on your computer or an optionally connected phone.
- **For viewers:** an OBS overlay with relevant questions, facts, links, and images from your show materials.

OpenAI’s Realtime model decides what to show and when. Built with OpenAI Codex for the Codex x TwitchCon Hackathon.

## Try it

The live setup has been tested on **macOS**. You need **Python 3.12**, a browser, and an **OpenAI Platform API key** with access to the configured models. OBS and recorded-video input also require **FFmpeg**.

Save your API key in a private text file outside the repository, then run:

```sh
git clone https://github.com/scottfalconer/Producer.git
cd Producer
python3.12 -m venv .venv-producer
. .venv-producer/bin/activate
python -m pip install -r producer/requirements.txt
python producer/server.py --port 8786 --key-file /absolute/path/to/openai-key.txt
```

Replace the key-file path with your own. Keep the terminal running, then:

1. Open [Producer](http://127.0.0.1:8786/) and paste your show notes.
2. Under **Source and output**, choose **Microphone only**.
3. Press **Start offline test**, allow microphone access, and start presenting.
4. Open [the audience overlay](http://127.0.0.1:8786/on-air) separately to see public graphics. Press **Escape** in the presenter view to reach **Pause** and **End show**.

This trial hears your microphone; it cannot see your screen or chat. **Offline keeps Twitch actions simulated.** Your selected audio, images, notes, and research still go to OpenAI and incur API charges. A Twitch connection is optional.

## Use it with OBS and Twitch

Follow the [OBS setup](docs/SETUP.md#4-feed-the-complete-obs-program-directly) to send your finished video and full audio mix directly to Producer. Add the transparent audience overlay above your existing OBS scene. Keep the private confidence monitor outside the broadcast; the [phone setup](docs/SETUP.md#5-optional-phone-confidence-monitor) gives you a separate display.

With Twitch authorization and live actions explicitly enabled, Producer can share current on-air content in chat and execute production plans you’ve reviewed. You control broadcasting in OBS. See [Twitch setup](docs/SETUP.md#6-optional-real-twitch-production).

The optional demo chat contains fictional comments, disclosed on screen. Producer reads those comments from the video frames; it does not fetch incoming Twitch chat through the API.

## Documentation

- [Setup, model requirements, previews, and troubleshooting](docs/SETUP.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Development and tests](docs/CONTRIBUTING.md)
- [Privacy and security](docs/SECURITY.md)
- [Director prompt](producer/realtime_prompt.md)

Producer’s original code is [MIT licensed](LICENSE). Bundled assets retain their [third-party licenses and notices](THIRD_PARTY_NOTICES.md).
