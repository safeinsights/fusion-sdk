#!/usr/bin/env python3
"""Run the examples/ templates under the simulator and against the fake tunnel pair (plan Phase 6).

    python3 tools/run_examples.py [--r-src r] [--py-installed] [--rscript Rscript]

Simulator: examples/simulate_two_party.{py,R}, examples/simulate_hub.{py,R}.
Fake pair: two-party (Python destination + Python source; R destination + R source) and the hub
with a Python destination, source A in R and source B in Python, then an R destination with
source A in Python and source B in R. Exit 1 if any run fails.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from fake_tunnel_pair import FakeTunnelPair, load_scenario  # noqa: E402

EX = ROOT / "examples"


def env_for(base: dict[str, str], *, r_src: str | None, py_src: bool) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("FUSION_")}
    env.update(base)
    if py_src:
        env["PYTHONPATH"] = str(ROOT / "python" / "src")
    if r_src:
        env["R_PROFILE_USER"] = str(ROOT / "tools" / "r_dev_profile.R")
        env["SIFUSION_SRC"] = str(Path(r_src).resolve())
    return env


def cmd(path: Path, rscript: str) -> list[str]:
    return [rscript, str(path)] if path.suffix == ".R" else [sys.executable, str(path)]


def run_simulators(rscript: str, r_src: str | None, py_src: bool) -> list[tuple[str, bool, str]]:
    out = []
    for name in ("simulate_two_party.py", "simulate_hub.py", "simulate_two_party.R", "simulate_hub.R"):
        proc = subprocess.run(
            cmd(EX / name, rscript),
            env=env_for({}, r_src=r_src, py_src=py_src),
            text=True,
            capture_output=True,
            timeout=300,
            check=False,
        )
        out.append(
            (
                f"simulator {name}",
                proc.returncode == 0,
                (proc.stdout + proc.stderr).strip().splitlines()[-1] if (proc.stdout + proc.stderr).strip() else "",
            )
        )
    return out


def run_fake(
    dest: Path, sources: list[Path], scenario: str, rscript: str, r_src: str | None, py_src: bool
) -> tuple[str, bool, str]:
    sc = replace(load_scenario(scenario), longpoll_ms=500)
    name = f"fake {dest.name} <- {', '.join(s.name for s in sources)} [{scenario}]"
    base = {
        "FUSION_READY_TIMEOUT_S": "60",
        "FUSION_POLL_HTTP_TIMEOUT_S": "10",
        "FUSION_ROUND_TIMEOUT_S": "20",
        "FUSION_LOG_LEVEL": "INFO",
    }
    with FakeTunnelPair(sc) as pair:
        procs = []
        for i, src in enumerate(sources):
            ep = pair.endpoints[i].source
            env = env_for(
                {
                    **base,
                    "FUSION_ROLE": "source",
                    "FUSION_TUNNEL_ENDPOINT": ep.endpoint,
                    "FUSION_TUNNEL_TOKEN": ep.token,
                },
                r_src=r_src,
                py_src=py_src,
            )
            procs.append(
                subprocess.Popen(
                    cmd(src, rscript), env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
                )
            )
        env = env_for({**base, **pair.env("destination")}, r_src=r_src, py_src=py_src)
        t0 = time.monotonic()
        d = subprocess.run(cmd(dest, rscript), env=env, text=True, capture_output=True, timeout=300, check=False)
        exits = []
        logs = ""
        for p in procs:
            try:
                p.wait(timeout=30)
            except subprocess.TimeoutExpired:
                p.kill()
            exits.append(p.returncode)
            logs += p.stdout.read() if p.stdout else ""
    ok = d.returncode == 0 and all(e == 0 for e in exits)
    detail = f"dest={d.returncode} src={exits} {time.monotonic() - t0:.1f}s"
    if not ok:
        detail += (
            " | "
            + ((d.stdout + d.stderr).strip().splitlines() or [""])[-1][:200]
            + " | "
            + (logs.strip().splitlines() or [""])[-1][:200]
        )
    return name, ok, detail


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rscript", default="Rscript")
    ap.add_argument("--r-src", default=None, help="load sifusion from this source dir via pkgload (dev)")
    ap.add_argument("--py-installed", action="store_true")
    ap.add_argument("--skip-simulator", action="store_true")
    args = ap.parse_args(argv)
    py_src = not args.py_installed
    results: list[tuple[str, bool, str]] = []
    if not args.skip_simulator:
        results += run_simulators(args.rscript, args.r_src, py_src)
    tp, hub = EX / "two-party", EX / "hub"
    results.append(run_fake(tp / "destination.py", [tp / "source.py"], "happy", args.rscript, args.r_src, py_src))
    results.append(run_fake(tp / "destination.R", [tp / "source.R"], "happy", args.rscript, args.r_src, py_src))
    results.append(
        run_fake(
            hub / "destination.py",
            [hub / "source-a.R", hub / "source-b.py"],
            "hub-two-legs-open",
            args.rscript,
            args.r_src,
            py_src,
        )
    )
    results.append(
        run_fake(
            hub / "destination.R",
            [hub / "source-a.py", hub / "source-b.R"],
            "hub-two-legs-open",
            args.rscript,
            args.r_src,
            py_src,
        )
    )
    failed = 0
    for name, ok, detail in results:
        print(f"[{'ok  ' if ok else 'FAIL'}] {name:<70} {detail}")
        failed += not ok
    print(f"\nexamples: {len(results) - failed}/{len(results)} ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
