#!/usr/bin/env python3
"""Prove the vendored drops work the way `crane mutate` places files (plan Phase 7).

    python3 tools/test_vendored.py [--rscript Rscript]

Builds both drops into a scratch WORKDIR, copies the two-party example next to them (as the
containerizer appends researcher files), then runs the example destination and source against the
fake tunnel pair with *no* installed SDK: Python via the vendored package directory on the WORKDIR,
R via `source("safeinsights.fusion.R")`. Exit 1 on any failure.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from fake_tunnel_pair import FakeTunnelPair, load_scenario  # noqa: E402

R_SHIM = 'source(file.path(dirname(normalizePath(sub("--file=", "", grep("--file=", commandArgs(), value = TRUE)[1]))), "safeinsights.fusion.R"))\n'


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rscript", default="Rscript")
    args = ap.parse_args(argv)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "workdir"
        work.mkdir()
        subprocess.run(["sh", str(ROOT / "python" / "vendor.sh"), str(work)], check=True)
        subprocess.run([args.rscript, str(ROOT / "r" / "vendor.R"), str(work)], check=True)
        # Researcher files appended at WORKDIR; the R examples' library(safeinsights.fusion) becomes source("safeinsights.fusion.R").
        for name in ("destination.py", "source.py"):
            shutil.copy(ROOT / "examples" / "two-party" / name, work / name)
        for name in ("destination.R", "source.R"):
            text = (
                (ROOT / "examples" / "two-party" / name).read_text().replace("library(safeinsights.fusion)\n", R_SHIM)
            )
            (work / name).write_text(text)
        base = {k: v for k, v in os.environ.items() if not k.startswith(("FUSION_", "PYTHONPATH", "R_PROFILE_USER"))}
        base.update(FUSION_READY_TIMEOUT_S="60", FUSION_POLL_HTTP_TIMEOUT_S="10", FUSION_LOG_LEVEL="INFO")
        failed = 0
        for lang, dest, src in (
            ("python", [sys.executable, "destination.py"], [sys.executable, "source.py"]),
            ("r", [args.rscript, "destination.R"], [args.rscript, "source.R"]),
        ):
            with FakeTunnelPair(replace(load_scenario("happy"), longpoll_ms=500)) as pair:
                ep = pair.endpoints[0].source
                s = subprocess.Popen(
                    src,
                    cwd=work,
                    env={
                        **base,
                        "FUSION_ROLE": "source",
                        "FUSION_TUNNEL_ENDPOINT": ep.endpoint,
                        "FUSION_TUNNEL_TOKEN": ep.token,
                    },
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
                d = subprocess.run(
                    dest,
                    cwd=work,
                    env={**base, **pair.env("destination")},
                    text=True,
                    capture_output=True,
                    timeout=180,
                    check=False,
                )
                try:
                    s.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    s.kill()
                ok = d.returncode == 0 and s.returncode == 0
                failed += not ok
                print(f"[{'ok  ' if ok else 'FAIL'}] vendored {lang}: dest={d.returncode} src={s.returncode}")
                if not ok:
                    print((d.stdout + d.stderr)[-1500:])
                    print((s.stdout.read() if s.stdout else "")[-1500:])
        print(f"\nvendored: {2 - failed}/2 ok")
        return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
