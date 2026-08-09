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
        # attest takes no flags, and main() accepts none — so ANY flag given
        # here used to be silently discarded while the job ran for real.
        # `attest --dry-run` therefore performed a live batch_update, spent a
        # fee, and wrote 185 permanent records to a public store (2026-08-08,
        # tx 0x583aa051…). The operator had every reason to believe it was a
        # rehearsal: --dry-run is real on `publish`.
        #
        # A tool must refuse a flag it does not understand rather than proceed
        # with its default, irreversible action. Silently ignoring input is the
        # worst option available: it is indistinguishable from honouring it.
        extra = argv[1:] if argv else []
        if extra:
            print(
                f"attest takes no options, but got: {' '.join(extra)}\n"
                f"Refusing to run rather than ignore them — attest writes to the "
                f"blockchain and cannot be undone.\n"
                f"There is no --dry-run for attest. To preview without writing, "
                f"use:  python -m orchard_chia.datalayer reconcile",
                file=sys.stderr,
            )
            return 2
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
