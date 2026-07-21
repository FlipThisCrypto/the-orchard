# SPDX-License-Identifier: Apache-2.0
"""Entry points:

  python -m orchard_chia.datalayer              # Season attest writer (legacy default)
  python -m orchard_chia.datalayer attest       # same as default
  python -m orchard_chia.datalayer publish      # hot-path readings publisher (ADR-0003)
  python -m orchard_chia.datalayer publish --dry-run
  python -m orchard_chia.datalayer preflight    # config + connectivity checks
  python -m orchard_chia.datalayer preflight --skip-chia
  python -m orchard_chia.datalayer reconcile [--season N]  # oracle vs DL honesty
"""
from __future__ import annotations

import sys


def _dispatch(argv: list[str]) -> int:
    if not argv or argv[0] in ("attest", "attestation", "season"):
        from .main import main
        return int(main() or 0)

    cmd = argv[0]
    if cmd in ("publish", "hot", "readings"):
        from .publish import main as publish_main
        return int(publish_main(argv[1:]) or 0)

    if cmd in ("preflight", "check", "doctor"):
        from .preflight import main as preflight_main
        return int(preflight_main(argv[1:]) or 0)

    if cmd in ("reconcile", "honesty", "audit"):
        from .reconcile import main as reconcile_main
        return int(reconcile_main(argv[1:]) or 0)

    if cmd in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0

    print(
        f"Unknown subcommand {cmd!r}. "
        f"Use: attest | publish | preflight | reconcile | --help",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(_dispatch(sys.argv[1:]))
