"""`python -m fake_tunnel_pair --scenario happy [--legs-override N]`.

Prints one JSON line with endpoints and tokens (see FakeTunnelPair.announce), optionally
writes it to --announce FILE, then serves until SIGINT/SIGTERM or until stdin reaches EOF
(so a parent process that spawned it with a pipe takes it down by exiting).
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from dataclasses import replace
from pathlib import Path

from .scenarios import DEFAULT_SPEC_DIR, load_scenario
from .server import FakeTunnelPair


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fake-tunnel-pair", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--scenario", default="happy", help="scenario name under --spec-dir, or a path to a JSON file")
    p.add_argument("--spec-dir", type=Path, default=DEFAULT_SPEC_DIR)
    p.add_argument("--host", default="127.0.0.1", help="bind address (0.0.0.0 only for the docker smoke)")
    p.add_argument("--advertise-host", default=None, help="host name to put in announced endpoints (default: --host)")
    p.add_argument("--port-base", type=int, default=0, help="first port; legs take consecutive pairs. 0 = ephemeral")
    p.add_argument("--longpoll-ms", type=int, default=None, help="override the scenario's long-poll hold")
    p.add_argument("--ready-delay-ms", type=int, default=None, help="override the scenario's readiness delay")
    p.add_argument("--fixed-tokens", action="store_true", help="tokens fake-<legId>-<role> (test/smoke only)")
    p.add_argument("--api-version", default="1.0.0", help="apiVersion reported on /v1/info (test knob)")
    p.add_argument("--announce", type=Path, default=None, help="also write the announce JSON to this file")
    p.add_argument("--no-stdin-watch", action="store_true", help="do not exit on stdin EOF")
    p.add_argument("--verbose", action="store_true", help="log every HTTP request to stderr")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    scenario = load_scenario(args.scenario, args.spec_dir)
    if args.longpoll_ms is not None or args.ready_delay_ms is not None:
        scenario = replace(
            scenario,
            longpoll_ms=args.longpoll_ms if args.longpoll_ms is not None else scenario.longpoll_ms,
            ready_delay_ms=args.ready_delay_ms if args.ready_delay_ms is not None else scenario.ready_delay_ms,
        )
    pair = FakeTunnelPair(
        scenario,
        host=args.host,
        port_base=args.port_base,
        fixed_tokens=args.fixed_tokens,
        verbose=args.verbose,
        advertise_host=args.advertise_host,
        api_version=args.api_version,
    )
    pair.start()
    announce = pair.announce()
    line = json.dumps(announce)
    if args.announce is not None:
        tmp = args.announce.with_suffix(args.announce.suffix + ".tmp")
        tmp.write_text(line + "\n", encoding="utf-8")
        tmp.replace(args.announce)
    print(line, flush=True)

    stop = threading.Event()

    def _stop(*_: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    if not args.no_stdin_watch:

        def _watch_stdin() -> None:
            try:
                while sys.stdin.readline():
                    pass
            except (OSError, ValueError):
                pass
            stop.set()

        threading.Thread(target=_watch_stdin, daemon=True, name="stdin-watch").start()

    try:
        while not stop.wait(0.5):
            pass
    finally:
        pair.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
