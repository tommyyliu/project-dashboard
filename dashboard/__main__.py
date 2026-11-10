import argparse
import json
from pathlib import Path

from .scanner import Config, scan_all
from .server import CONFIG, serve


def main() -> None:
    parser = argparse.ArgumentParser(prog="dashboard", description="Dashboard of the projects in a folder.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--json", action="store_true", help="print one scan as JSON and exit")
    parser.add_argument("--snapshot", metavar="PATH", help="write a read-only copy of the page with a fresh scan and exit")
    args = parser.parse_args()
    if args.snapshot:
        from .snapshot import build
        Path(args.snapshot).write_text(build(Config.load(CONFIG)))
        print(f"Wrote {args.snapshot}")
    elif args.json:
        print(json.dumps(scan_all(Config.load(CONFIG)), indent=2))
    else:
        serve(args.port)


if __name__ == "__main__":
    main()
