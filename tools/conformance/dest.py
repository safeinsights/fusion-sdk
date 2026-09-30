"""Conformance destination (Python). See README.md for the round script and exit codes."""

from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import date, datetime, timezone
from typing import Any

from safeinsights_fusion import Fusion, Peer, RemoteError, RoundTimeoutError, TerminalError

EXPECTED_ECHO: dict[str, Any] = {
    "s": "héllo ✓",
    "n": 3,
    "x": 0.25,
    "b": True,
    "z": None,
    "list": ["a", 1, None],
    "obj": {"k": "v"},
}


def out(**fields: Any) -> None:
    print(json.dumps(fields, ensure_ascii=False), flush=True)


def check(cond: bool, what: str) -> None:
    if not cond:
        raise AssertionError(what)


def round_counts(peer: Peer) -> None:
    r = peer.request("counts_by_group", {"person_ids": ["p1", "p2", "p3"], "group_by": "grade"})
    t = r.to_table()
    check([c.type for c in t.columns] == ["string", "integer"], f"counts_by_group column types {t.columns}")
    check(t.to_records() == [{"grade": "9", "n": 3}, {"grade": "10", "n": 7}], f"counts_by_group rows {t.rows}")


def round_total(peer: Peer) -> None:
    r = peer.request("total", {})
    check(
        r.body["total"] == 42 and r.body["correlation_id"] == r.correlation_id and r.body["peer"] == peer.name,
        f"total body {r.body}",
    )


def round_echo(peer: Peer) -> None:
    r = peer.request("echo", EXPECTED_ECHO)
    check(r.body == EXPECTED_ECHO, f"echo body {r.body}")


def round_table_types(peer: Peer) -> None:
    t = peer.request("table_types", {}).to_table()
    check(
        [c.type for c in t.columns] == ["string", "integer", "integer64", "number", "boolean", "date", "datetime"],
        f"types {t.columns}",
    )
    row = t.rows[0]
    check(
        row[0] == "héllo ✓" and row[1] == 412 and row[2] == 9007199254740993 and row[3] == 1.5 and row[4] is True,
        f"row0 {row}",
    )
    check(
        row[5] == date(2024, 2, 29) and row[6] == datetime(2026, 9, 18, 12, 0, 0, 250_000, tzinfo=timezone.utc),
        f"row0 dates {row}",
    )
    check(t.rows[1] == [None] * 7, f"row1 {t.rows[1]}")


def round_errors(peer: Peer) -> None:
    try:
        peer.request("boom", {})
        check(False, "boom did not raise")
    except RemoteError as exc:
        check(exc.code == "HANDLER_ERROR" and "conformance boom" in exc.message, f"boom {exc}")
    try:
        peer.request("nope", {})
        check(False, "nope did not raise")
    except RemoteError as exc:
        check(exc.code == "UNKNOWN_OPERATION", f"nope {exc}")


def two_party(fusion: Fusion, rounds: int) -> None:
    peer = fusion.peer()
    for i in range(rounds):
        round_counts(peer)
        round_total(peer)
        round_echo(peer)
        round_table_types(peer)
        out(event="round", i=i, ok=True)
    round_errors(peer)
    outcomes = fusion.complete()
    check(outcomes == {peer.name: "OK"}, f"complete {outcomes}")
    out(event="complete", outcomes=outcomes)


def hub(fusion: Fusion) -> None:
    a, b = fusion.peer("dp-a"), fusion.peer("dp-b")
    ra = a.request("total", {})
    rb = b.request("echo", {"derived_from_a": ra.body["total"]})
    check(rb.body == {"derived_from_a": 42}, f"hub echo {rb.body}")
    try:
        b.request("total", {})
        check(False, "leg B should be capped at one round")
    except TerminalError as exc:
        check(exc.code == "LIMIT_EXCEEDED", f"leg B terminal {exc}")
    round_counts(a)
    round_table_types(a)
    outcomes = fusion.complete()
    check(outcomes == {"dp-a": "OK", "dp-b": "LIMIT_EXCEEDED"}, f"complete {outcomes}")
    out(event="complete", outcomes=outcomes)


def chaos(fusion: Fusion, rounds: int) -> None:
    peer = fusion.peer()
    timeouts = 0
    for i in range(rounds):
        try:
            round_total(peer)
            out(event="round", i=i, ok=True)
        except RoundTimeoutError:
            timeouts += 1
            out(event="round", i=i, ok=False, timeout=True)
    outcomes = fusion.complete()
    out(event="complete", outcomes=outcomes, timeouts=timeouts)


def main() -> int:
    mode = os.environ.get("CONF_MODE", "two-party")
    rounds = int(os.environ.get("CONF_ROUNDS", "2"))
    try:
        fusion = Fusion.connect()
        if mode == "hub":
            hub(fusion)
        elif mode == "chaos":
            chaos(fusion, rounds)
        else:
            two_party(fusion, rounds)
        return 0
    except TerminalError as exc:
        out(event="terminal", code=exc.code, peer=exc.peer)
        return 3
    except Exception as exc:  # the driver reports any other failure as a parity break
        out(event="error", type=type(exc).__name__, message=str(exc))
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
