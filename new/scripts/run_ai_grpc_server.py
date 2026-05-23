#!/usr/bin/env python
"""Run the read-only AI-BE gRPC bridge server."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from src.integration.grpc.server import serve  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=50051)
    parser.add_argument("--bundle-id", default="")
    parser.add_argument("--max-workers", type=int, default=4)
    args = parser.parse_args(argv)

    server = serve(
        host=str(args.host),
        port=int(args.port),
        bundle_id=str(args.bundle_id),
        root=ROOT,
        max_workers=int(args.max_workers),
    )
    print(
        f"[ai_grpc] serving on {args.host}:{args.port} "
        f"bundle_id={args.bundle_id or '<request-required>'}"
    )
    try:
        while True:
            time.sleep(86400)
    except KeyboardInterrupt:
        server.stop(grace=2)
        print("[ai_grpc] stopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
