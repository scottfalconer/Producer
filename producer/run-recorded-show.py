"""Explicit real-time owner-recording run with local tool/scene replay evidence.

This invokes paid inference only when explicitly run. It starts an isolated
loopback helper, never the owner's active helper or a real Twitch connection.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
import time
import uuid
from http.server import ThreadingHTTPServer

import requests

import server
from action_trace import ActionTrace, capture_scene
from recorded_source import RecordingSource
from recorded_inputs import select_inputs, select_research_packet
from simulated_chat import generate_chat, parse_seed
from screen_chat_overlay import STYLE as CHAT_STYLE, with_producer_posts


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)


def telemetry(value):
    """Retain routing diagnostics without the director's private working memory."""
    if isinstance(value, dict):
        return {key: telemetry(item) for key, item in value.items()
                if key not in {"memory", "working_notes", "director_notes", "deeper_advice"}}
    if isinstance(value, list):
        return [telemetry(item) for item in value]
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--source-cache", type=Path)
    parser.add_argument("--notes-file", type=Path, help="Explicit topical show notes; defaults to Office Hours.")
    parser.add_argument("--events-file", type=Path, help="Use this exact fictional timeline instead of generating new chat.")
    parser.add_argument("--chat-theme", choices=("office-hours", "halloween"), default="office-hours")
    parser.add_argument("--chat-seed", type=parse_seed, help="Repeat a generated timeline; omit for fresh random chat on each run.")
    parser.add_argument("--show-folder", type=Path, help="Pinned topical images; use an empty folder for no images.")
    parser.add_argument("--research-packet", type=Path, help="UTF-8 Markdown/text reference packet; overrides the reserved file in the show folder.")
    parser.add_argument("--audience-input", choices=("screen", "legacy-fixture"), default="screen",
                        help="Read visible simulated chat from programme pixels (default); legacy-fixture only reproduces historical direct-message tests.")
    parser.add_argument("--seconds", type=int, default=600)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 600:
        parser.error("Choose 1–600 seconds of the explicitly selected recording.")
    if args.events_file and args.chat_seed is not None:
        parser.error("Choose an exact events file or a generated chat seed, not both.")
    try:
        notes, events, show_folder = select_inputs(
            notes_file=args.notes_file, events_file=args.events_file, show_folder=args.show_folder,
            default_notes=server.rehearsal.config()["notes"], default_events=server.rehearsal.DATA,
            default_show_folder=server.DEFAULT_SHOW_FOLDER,
        )
        research_packet, research_source = select_research_packet(
            research_packet_file=args.research_packet, show_folder=show_folder,
        )
        if args.audience_input == "screen" and args.events_file is None:
            events = generate_chat(args.chat_theme, duration=args.seconds, seed=args.chat_seed)
    except (ValueError, OSError, UnicodeError) as exc:
        parser.error(str(exc))
    os.umask(0o077)
    bundle = args.bundle.resolve()
    bundle.mkdir(parents=True, exist_ok=False)
    source = RecordingSource(args.source, duration=args.seconds, cache_dir=args.source_cache or bundle / "source",
                             chat_events=events if args.audience_input == "screen" else None)
    source_report = source.prepare()
    show_id = "recorded-show-" + uuid.uuid4().hex
    # This script owns an isolated process/helper. Do not change the main helper's
    # fixture or add a live API route for arbitrary event/source substitution.
    server.rehearsal.DATA = events
    server.rehearsal.CLIP = source.clip_path
    (bundle / "show-notes.md").write_text(notes)
    (bundle / "research-packet.md").write_text(research_packet, encoding="utf-8")
    (bundle / "producer-prompt.md").write_bytes(Path(__file__).with_name("realtime_prompt.md").read_bytes())
    write_json(bundle / "mock-events.json", events)
    http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    http.key_file, http.show_folder, http.audience_openai = args.key_file, show_folder, False
    http.chat_fixture_override = events
    threading.Thread(target=http.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{http.server_port}"
    headers = {"Origin": base, "X-Desk": server.TOKEN}
    session = None
    sid = None
    trace = None
    feed_started = [None]
    manifest = {
        "schema": "producer.recorded-run.v1", "show_id": show_id,
        "status": "prepared", "duration_s": args.seconds,
        "playback_rate": 1, "model": server.REALTIME_MODEL,
        "context_policy": "model-managed",
        "audience_text_policy": "verbatim",
        "audience_input": args.audience_input,
        "chat_overlay": {"events": "mock-events.json", "width": 1280, "height": 720, "style": CHAT_STYLE}
                        if args.audience_input == "screen" else None,
        "card_attribution_policy": "model",
        "offline": True, "synthetic_audience": True,
        "fixture_id": events["id"],
        "input_files": {"notes": str(args.notes_file.resolve()) if args.notes_file else None,
                        "events": str(args.events_file.resolve()) if args.events_file else None,
                        "research_packet": research_source,
                        "show_folder": str(show_folder)},
        "research_packet": {"snapshot": "research-packet.md", "characters": len(research_packet),
                            "sha256": hashlib.sha256(research_packet.encode("utf-8")).hexdigest()},
        "source": {"path": str(source.clip_path), "start_s": 0, "original": source_report},
        "assets": {}, "limits": ["Recorded host cannot react to simulated audience or new cues.",
                                  "This video reconstructs the saved real-time screen state; it is not a new model run."]}
    write_json(bundle / "manifest.json", manifest)

    def post(route, data):
        response = requests.post(base + route, json=data, headers=headers, timeout=30)
        if not response.ok:
            raise RuntimeError(f"{route}: HTTP {response.status_code}: {response.text[:180]}")
        return response.json()

    def elapsed():
        return max(0, time.monotonic() - feed_started[0]) if feed_started[0] is not None else 0

    def scene():
        return capture_scene(session["director"], trace)

    try:
        prepared = post("/api/prepare", {"notes": notes, "runtime": "realtime"})
        started = post("/api/start", {"brief": prepared["brief"], "notes": notes,
                       "research_packet": research_packet,
                       "audience_input": args.audience_input,
                       "runtime": "realtime", "show_id": show_id,
                       "input_type": "rehearsal", "offline": True, "on_air": True, "captions": False})
        sid = started["id"]
        session = server.LIVE[sid]
        session["audience_text_policy"] = manifest["audience_text_policy"]
        session["card_attribution_policy"] = manifest["card_attribution_policy"]
        manifest["production_policy"] = session["production_policy"]
        manifest["production_replay"] = {
            "platform": "fixture", "channel_id": session["production"].channel,
            "brief": {"production": session["production"].plans,
                      "intentions": [{"id": item["id"]} for item in session["brief"].get("intentions", [])]},
        }
        session["rehearsal"] = source
        trace = ActionTrace(bundle / "actions.jsonl", clock=elapsed,
                            source_clock=lambda: session["director"].audio_end if session.get("director") else 0)
        session["action_trace"] = trace
        assets_dir = bundle / "assets"
        assets_dir.mkdir()
        manifest["asset_catalog"] = session["materials"].catalog
        for asset in session["materials"].catalog:
            content, _ = session["materials"].get(asset["id"])
            asset_path = assets_dir / (asset["id"] + ".jpg")
            asset_path.write_bytes(content)
            manifest["assets"][asset["id"]] = str(asset_path)
        manifest["ready"] = telemetry(post("/api/director/start", {"id": sid}))
        feed_started[0], manifest["wall_epoch_s"] = time.monotonic(), time.time()
        manifest["status"] = "running"
        manifest["initial_state"] = telemetry(session["director"].state())
        write_json(bundle / "manifest.json", manifest)
        with (bundle / "scenes.jsonl").open("w") as scenes, (bundle / "metrics.jsonl").open("w") as metrics:
            scenes.write(json.dumps(scene(), ensure_ascii=False) + "\n")
            for sequence in range(args.seconds * 2):
                remaining = (sequence + 1) * 0.5 - elapsed()
                if remaining > 0:
                    time.sleep(remaining)
                accepted = post("/api/director/audio", {"id": sid, "sequence": sequence})
                status = post("/api/director/status", {"id": sid})
                if status["status"] == "error":
                    raise RuntimeError("The actual Realtime connection stopped: " + str(status.get("error")))
                row = scene()
                row["packet_lag_s"] = round(elapsed() - (sequence + 1) * 0.5, 5)
                scenes.write(json.dumps(row, ensure_ascii=False) + "\n")
                scenes.flush()
                manifest["accepted_through"] = accepted["through"]
                if sequence % 60 == 59:
                    metrics.write(json.dumps({"wall_s": elapsed(), "status": telemetry(status)}, ensure_ascii=False) + "\n")
                    metrics.flush()
                    print(json.dumps({"progress_s": accepted["through"], "lag_s": row["packet_lag_s"],
                                      "responses": status.get("responses"), "tool_calls": status.get("tool_calls")}), flush=True)
                manifest["last_status"] = telemetry(status)
        manifest["status"] = "completed"
    except Exception as exc:
        manifest["status"], manifest["error"] = "failed", str(exc)[:300]
        raise
    finally:
        if sid:
            manifest["stop"] = telemetry(post("/api/stop", {"id": sid, "reason": "ended",
                                      "through": manifest.get("accepted_through", 0)}))
            transcript = session["transcript"].path
            if transcript.is_file():
                shutil.copy2(transcript, bundle / "transcript.jsonl")
            if session.get("producer_posts") and args.audience_input == "screen":
                write_json(bundle / "input-mock-events.json", events)
                write_json(bundle / "producer-posts.json", session["producer_posts"])
                write_json(bundle / "mock-events.json", {**events, "events": with_producer_posts(events, session["producer_posts"])})
        if trace:
            trace.close()
        server.audience.stop()
        http.shutdown()
        http.server_close()
        manifest["wall_duration_s"] = elapsed()
        manifest["files"] = {str(p.relative_to(bundle)): hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in bundle.rglob("*") if p.is_file() and p.name != "manifest.json"}
        write_json(bundle / "manifest.json", manifest)
        print(json.dumps({"bundle": str(bundle), "status": manifest["status"],
                          "accepted_through": manifest.get("accepted_through", 0)}), flush=True)


if __name__ == "__main__":
    main()
