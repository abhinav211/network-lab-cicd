import argparse
import logging
from pathlib import Path

from lab.automation import build, live, prepare
from lab.model import load_hosts


def main() -> None:
    parser = argparse.ArgumentParser(description="Build and verify an ephemeral cEOS BGP lab")
    parser.add_argument(
        "command", choices=["validate", "build", "prepare", "wait", "configure", "verify"]
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, default=Path("build"))
    parser.add_argument("--lab-name")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    root = args.root.resolve()
    output = args.output.resolve()
    if args.command == "validate":
        load_hosts(root)
    elif args.command == "build":
        build(root, output)
    elif args.command == "prepare":
        if not args.lab_name:
            parser.error("prepare requires --lab-name")
        prepare(root, output, args.lab_name)
    else:
        live(root, output, args.command, args.timeout)
    logging.info("%s completed", args.command)


if __name__ == "__main__":
    main()
