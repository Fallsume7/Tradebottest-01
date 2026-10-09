"""Run the mock trading site: ``python -m mwbot.mocksite [--port 8765]``."""

import argparse

import uvicorn

from mwbot.mocksite.app import create_mock_app


def main() -> None:
    ap = argparse.ArgumentParser(description="Mock virtual trading site (for adapter tests)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    uvicorn.run(create_mock_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
