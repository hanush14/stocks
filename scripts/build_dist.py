#!/usr/bin/env python3
"""Build the portable distribution.

Produces `dist/ledger.pyz` - a single-file Python zipapp. The `ledger` package
depends only on the standard library, so this one file runs anywhere Python 3.10+
is installed, with nothing to pip install.

A true Windows .exe cannot be cross-built from Linux; PyInstaller has to run on
the target OS. `packaging/ledger.spec` is provided so that one command on Windows
turns this into a real .exe.

Usage:  python3 scripts/build_dist.py
"""
from __future__ import annotations

import shutil
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
STAGE = DIST / "_stage"

MAIN = '''\
import sys
from ledger.cli import main
sys.exit(main())
'''


def build() -> Path:
    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)

    shutil.copytree(ROOT / "ledger", STAGE / "ledger",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (STAGE / "__main__.py").write_text(MAIN)

    target = DIST / "ledger.pyz"
    zipapp.create_archive(STAGE, target=target, interpreter="/usr/bin/env python3")
    shutil.rmtree(STAGE)
    return target


if __name__ == "__main__":
    out = build()
    print(f"built {out.relative_to(ROOT)}  ({out.stat().st_size:,} bytes)")
    print("run it with:  python ledger.pyz status")
