import argparse
import json

from .scanner import Config, scan_all
from .server import CONFIG, serve


def main() -> None:
    parser = argparse.ArgumentParser(prog="dashboard", description="Dashboard of the projects in a folder.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--json", action="store_true", help="print one scan as JSON and exit")
    args = parser.parse_args()
    if args.json:
        print(json.dumps(scan_all(Config.load(CONFIG)), indent=2))
    else:
        serve(args.port)


if __name__ == "__main__":
    main()
