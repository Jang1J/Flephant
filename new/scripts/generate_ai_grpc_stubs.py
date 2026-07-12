#!/usr/bin/env python
"""Generate Python gRPC stubs from new/proto/elephant_ai_bridge.proto."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROTO = ROOT / "new" / "proto" / "elephant_ai_bridge.proto"
DEFAULT_OUTPUT = ROOT / "new" / "src" / "integration" / "grpc" / "generated"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proto", default=str(DEFAULT_PROTO))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args(argv)

    try:
        from grpc_tools import protoc  # type: ignore[import-not-found]
    except ImportError:
        print(
            "grpc_tools is not installed. Install requirements.txt "
            "(grpcio + grpcio-tools) before generating stubs.",
            file=sys.stderr,
        )
        return 2

    proto_path = Path(str(args.proto)).resolve()
    output_dir = Path(str(args.output_dir)).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    init_path = output_dir / "__init__.py"
    if not init_path.exists():
        init_path.write_text(
            '"""Generated gRPC modules live here."""\n',
            encoding="utf-8",
        )

    with TemporaryDirectory(prefix="elephant_grpc_codegen_") as temp_dir:
        temp_output = Path(temp_dir)
        result = protoc.main(
            [
                "grpc_tools.protoc",
                f"-I{proto_path.parent}",
                f"--python_out={temp_output}",
                f"--grpc_python_out={temp_output}",
                str(proto_path),
            ]
        )
        if result != 0:
            return int(result)
        for generated_path in temp_output.glob("*_pb2*.py"):
            generated_path.replace(output_dir / generated_path.name)
    print(f"[ai_grpc] generated stubs: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
