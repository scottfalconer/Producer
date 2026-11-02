"""API-free saved screen-tool replay. Does not load a credential or media feed."""

import argparse
import json
from pathlib import Path

from action_replay import replay_bundle, write_private


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args()
    report, scenes = replay_bundle(args.bundle)
    write_private(args.bundle / "replay-report.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    write_private(args.bundle / "replayed-scenes.jsonl", "".join(json.dumps(scene, ensure_ascii=False) + "\n" for scene in scenes))
    print(json.dumps({k: v for k, v in report.items() if k not in ("calls", "scene_mismatches")}, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
