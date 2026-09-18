#!/usr/bin/env python3
"""Cross-language matrix (plan Phase 5): every {Python, R} destination x source pair talks through the fake.

    python3 tools/matrix.py                      # 4 two-party pairs + the mixed-source hub, scenario happy
    python3 tools/matrix.py --pairs py-r r-py    # a subset
    python3 tools/matrix.py --scenario backpressure --rounds 3
    python3 tools/matrix.py --hub-only           # hub: destination py then r; source A in R, source B in Python
    python3 tools/matrix.py --fixtures           # fixture parity: decode every spec fixture in both languages
    python3 tools/matrix.py --r-src r            # load the R package from source (dev) instead of library(sifusion)

Exit status 1 when any cell fails. Prints one table row per cell. Uses tools/conformance/*.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from fake_tunnel_pair import FakeTunnelPair, LegEndpoints, TunnelEndpoint, load_scenario  # noqa: E402

CONF = ROOT / "tools" / "conformance"
LANGS = ("py", "r")


@dataclass(frozen=True)
class Toolchain:
    python: str
    rscript: str
    r_src: str | None  # path to r/ to load from source; None = library(sifusion)
    py_src: bool  # put python/src on PYTHONPATH (dev) instead of relying on an install

    def env(self, extra: dict[str, str]) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("FUSION_")}
        env.update(extra)
        if self.py_src:
            env["PYTHONPATH"] = str(ROOT / "python" / "src") + (
                os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
            )
        if self.r_src:
            env["SIFUSION_SRC"] = str(Path(self.r_src).resolve())
        return env

    def command(self, lang: str, role: str) -> list[str]:
        if lang == "py":
            return [self.python, str(CONF / f"{role}.py")]
        return [self.rscript, str(CONF / f"{role}.R")]


@dataclass
class Cell:
    name: str
    ok: bool
    dest_exit: int | None
    src_exits: list[int | None]
    seconds: float
    detail: str = ""


class AnnouncedTunnels:
    """Tunnels provided from outside (the real tunnel harness, Phase 8) in the fake pair's announce format."""

    def __init__(self, announce: dict[str, object]) -> None:
        legs = announce["legs"]
        assert isinstance(legs, list)
        self.endpoints = [
            LegEndpoints(
                leg_id=str(leg["legId"]),
                peer_org_slug=str(leg["peerOrgSlug"]),
                destination=TunnelEndpoint(str(leg["destination"]["endpoint"]), str(leg["destination"]["token"])),
                source=TunnelEndpoint(str(leg["source"]["endpoint"]), str(leg["source"]["token"])),
            )
            for leg in legs
        ]

    def env(self, role: str) -> dict[str, str]:
        if len(self.endpoints) == 1:
            ep = getattr(self.endpoints[0], role)
            return {"FUSION_ROLE": role, "FUSION_TUNNEL_ENDPOINT": ep.endpoint, "FUSION_TUNNEL_TOKEN": ep.token}
        return {
            "FUSION_ROLE": role,
            "FUSION_TUNNEL_ENDPOINTS": json.dumps({e.leg_id: getattr(e, role).endpoint for e in self.endpoints}),
            "FUSION_TUNNEL_TOKENS": json.dumps({e.leg_id: getattr(e, role).token for e in self.endpoints}),
        }

    def __enter__(self) -> AnnouncedTunnels:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def run(
    tc: Toolchain,
    dest_lang: str,
    src_langs: list[str],
    scenario_name: str,
    *,
    mode: str,
    rounds: int,
    timeout: float,
    longpoll_ms: int,
    announce: dict[str, object] | None = None,
) -> Cell:
    tunnels: FakeTunnelPair | AnnouncedTunnels
    if announce is None:
        sc = replace(load_scenario(scenario_name), longpoll_ms=longpoll_ms)
        if len(sc.legs) != len(src_langs):
            raise SystemExit(
                f"scenario {scenario_name} has {len(sc.legs)} legs; {len(src_langs)} source languages given"
            )
        tunnels = FakeTunnelPair(sc)
        where = scenario_name
    else:
        tunnels = AnnouncedTunnels(announce)
        if len(tunnels.endpoints) != len(src_langs):
            raise SystemExit(
                f"announce describes {len(tunnels.endpoints)} legs; {len(src_langs)} source languages given"
            )
        where = "real tunnel"
    name = f"{dest_lang}->{'+'.join(src_langs)} [{where}/{mode}]"
    t0 = time.monotonic()
    real = announce is not None
    base = {
        "FUSION_READY_TIMEOUT_S": "900" if real else "60",
        "FUSION_POLL_HTTP_TIMEOUT_S": "40" if real else "10",
        "FUSION_ROUND_TIMEOUT_S": "60" if real else "8",
        "FUSION_ROUND_MAX_REISSUES": "2",
        "FUSION_LOG_LEVEL": "INFO",
    }
    with tunnels as pair:
        sources: list[subprocess.Popen[str]] = []
        for i, lang in enumerate(src_langs):
            ep = pair.endpoints[i].source
            env = tc.env(
                {
                    **base,
                    "FUSION_ROLE": "source",
                    "FUSION_TUNNEL_ENDPOINT": ep.endpoint,
                    "FUSION_TUNNEL_TOKEN": ep.token,
                }
            )
            sources.append(
                subprocess.Popen(
                    tc.command(lang, "source"), env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
                )
            )
        env = tc.env({**base, **pair.env("destination"), "CONF_MODE": mode, "CONF_ROUNDS": str(rounds)})
        try:
            dest = subprocess.run(
                tc.command(dest_lang, "dest"), env=env, text=True, capture_output=True, timeout=timeout, check=False
            )
            dest_exit: int | None = dest.returncode
            dest_out = dest.stdout + dest.stderr
        except subprocess.TimeoutExpired as exc:
            dest_exit = None
            dest_out = (exc.stdout or "") + (exc.stderr or "") if isinstance(exc.stdout, str) else "timeout"
        src_exits: list[int | None] = []
        src_out = ""
        for proc in sources:
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
            src_exits.append(proc.returncode)
            if proc.stdout is not None:
                src_out += proc.stdout.read()
    ok = dest_exit == 0 and (
        (mode == "hub" and src_exits[0] == 0 and src_exits[1] != 0)
        or (mode != "hub" and all(e == 0 for e in src_exits))
    )
    if mode == "chaos":
        ok = dest_exit in (0, 3) and all(e is not None for e in src_exits)
    detail = (
        ""
        if ok
        else (dest_out.strip().splitlines() or ["(no destination output)"])[-1][:300]
        + " | src: "
        + (src_out.strip().splitlines() or [""])[-1][:200]
    )
    return Cell(name, ok, dest_exit, src_exits, time.monotonic() - t0, detail)


def fixtures_parity(tc: Toolchain) -> list[Cell]:
    """Decode every spec fixture in both languages and compare verdicts."""
    fixtures = sorted((ROOT / "spec" / "fixtures").glob("*.json"))
    py_code = r"""
import json, sys
sys.path.insert(0, %r)
from safeinsights_fusion._envelope import EnvelopeError, Table, decode
out = {}
for path in sys.argv[1:]:
    fx = json.load(open(path, encoding="utf-8"))
    try:
        (Table.from_json if fx["kind"] == "table" else decode)(fx["input"])
        out[fx["id"]] = "valid"
    except EnvelopeError:
        out[fx["id"]] = "invalid"
print(json.dumps(out))
""".replace("%r", repr(str(ROOT / "python" / "src")))
    r_code = r"""
src <- Sys.getenv("SIFUSION_SRC", "")
if (nzchar(src)) suppressMessages(pkgload::load_all(src, quiet = TRUE)) else suppressPackageStartupMessages(library(sifusion))
args <- commandArgs(trailingOnly = TRUE)
out <- list()
for (path in args) {
  fx <- jsonlite::fromJSON(path, simplifyVector = FALSE)
  fn <- if (fx$kind == "table") sifusion:::table_from_json else sifusion:::decode_envelope
  out[[fx$id]] <- tryCatch({ fn(fx$input); "valid" }, fusion_envelope_error = function(e) "invalid")
}
cat(jsonlite::toJSON(out, auto_unbox = TRUE))
"""
    paths = [str(p) for p in fixtures]
    py = subprocess.run([tc.python, "-c", py_code, *paths], text=True, capture_output=True, check=False, env=tc.env({}))
    r = subprocess.run([tc.rscript, "-e", r_code, *paths], text=True, capture_output=True, check=False, env=tc.env({}))
    if py.returncode != 0 or r.returncode != 0:
        return [Cell("fixtures", False, py.returncode, [r.returncode], 0.0, (py.stderr + r.stderr).strip()[-300:])]
    py_v, r_v = json.loads(py.stdout), json.loads(r.stdout.strip().splitlines()[-1])
    cells: list[Cell] = []
    for p in fixtures:
        fx = json.loads(p.read_text(encoding="utf-8"))
        expected = "valid" if fx["valid"] else "invalid"
        ok = py_v.get(fx["id"]) == expected == r_v.get(fx["id"])
        if not ok:
            cells.append(
                Cell(
                    f"fixture {fx['id']}",
                    False,
                    None,
                    [],
                    0.0,
                    f"expected {expected}, python {py_v.get(fx['id'])}, r {r_v.get(fx['id'])}",
                )
            )
    cells.append(
        Cell(
            f"fixtures ({len(fixtures)} files, both languages agree on {len(fixtures) - len(cells)})",
            not cells,
            0,
            [0],
            0.0,
        )
    )
    return cells


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pairs", nargs="*", default=None, help="subset like py-py py-r r-py r-r (default: all)")
    ap.add_argument("--scenario", default="happy")
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--hub-only", action="store_true")
    ap.add_argument("--no-hub", action="store_true")
    ap.add_argument("--fixtures", action="store_true", help="only the fixture parity gate")
    ap.add_argument("--chaos", action="store_true", help="CONF_MODE=chaos (used by tools/chaos.py)")
    ap.add_argument("--timeout", type=float, default=240.0)
    ap.add_argument("--longpoll-ms", type=int, default=500)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--rscript", default="Rscript")
    ap.add_argument("--r-src", default=None, help="load the R package from this source dir (dev)")
    ap.add_argument("--py-installed", action="store_true", help="use the installed safeinsights_fusion, not python/src")
    ap.add_argument(
        "--announce",
        default=None,
        help="Phase 8: JSON (fake announce format) describing REAL tunnels to use instead of the fake",
    )
    args = ap.parse_args(argv)
    announce = json.loads(Path(args.announce).read_text(encoding="utf-8")) if args.announce else None
    if announce is not None and (args.fixtures or args.hub_only or not args.pairs or len(args.pairs) != 1):
        # A real tunnel pair serves exactly one study: complete() closes it, so one cell per announce file.
        ap.error(
            "--announce runs exactly one cell: pass a single --pairs d-s (e.g. --pairs py-r) and no --fixtures/--hub-only"
        )
    tc = Toolchain(args.python, args.rscript, args.r_src, not args.py_installed)
    cells: list[Cell] = []
    if args.fixtures:
        cells += fixtures_parity(tc)
    else:
        mode = "chaos" if args.chaos else "two-party"
        pairs = [tuple(p.split("-")) for p in args.pairs] if args.pairs else [(d, s) for d in LANGS for s in LANGS]
        if not args.hub_only:
            for d, s in pairs:
                cells.append(
                    run(
                        tc,
                        d,
                        [s],
                        args.scenario,
                        mode=mode,
                        rounds=args.rounds,
                        timeout=args.timeout,
                        longpoll_ms=args.longpoll_ms,
                        announce=announce,
                    )
                )
                print(fmt(cells[-1]), flush=True)
        if not args.no_hub and not args.chaos and announce is None:
            for d in LANGS:
                cells.append(
                    run(
                        tc,
                        d,
                        ["r", "py"],
                        "hub-two-legs",
                        mode="hub",
                        rounds=1,
                        timeout=args.timeout,
                        longpoll_ms=args.longpoll_ms,
                    )
                )
                print(fmt(cells[-1]), flush=True)
    if args.fixtures:
        for c in cells:
            print(fmt(c))
    failed = [c for c in cells if not c.ok]
    print(f"\nmatrix: {len(cells) - len(failed)}/{len(cells)} cells ok")
    return 1 if failed else 0


def fmt(c: Cell) -> str:
    status = "ok  " if c.ok else "FAIL"
    exits = f"dest={c.dest_exit} src={c.src_exits}"
    return f"[{status}] {c.name:<40} {exits:<28} {c.seconds:5.1f}s {c.detail}"


if __name__ == "__main__":
    sys.exit(main())
