"""Extract the structural topology of a PostgreSQL database.

    python3 -m topos.extract.postgres --dsn postgresql://... --target NAME --revision SHA \
        [--exclude 'function:public.helper(*)=environment=why it is left out'] --out topology.json

The database should hold the target's schema exactly as its revision applies it. The connection is
read-only: extraction never writes to its target.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from topos.extract.catalog import read_catalog
from topos.extract.topology import Exclusion, build_topology

BASES = ("environment", "scope", "tractability", "structure")


def _exclusion(text: str) -> Exclusion:
    selector, basis, reason = [*text.split("=", 2), "", ""][:3]
    if basis not in BASES or not reason or not selector:
        message = f"--exclude takes SELECTOR=BASIS=REASON with BASIS one of {', '.join(BASES)}"
        raise argparse.ArgumentTypeError(message)
    return Exclusion(selector, basis, reason)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--target", required=True, help="the target's name")
    parser.add_argument("--revision", required=True, help="the target revision applied")
    parser.add_argument("--exclude", action="append", default=[], type=_exclusion)
    parser.add_argument("--out", type=Path, help="write here instead of standard output")
    args = parser.parse_args(argv)
    document = build_topology(
        read_catalog(args.dsn),
        target_name=args.target,
        revision=args.revision,
        exclusions=args.exclude,
    )
    text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
