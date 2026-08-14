"""Narrow command-line entry point required by the WP-12 APEX importer."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from mirrorfirm.packs.apex_accounting import (
    APEX_ACCOUNTING_LABEL,
    ApexImportError,
    install_apex_accounting,
    installed_pack_path,
    show_gold_output,
)


def main(argv: Sequence[str] | None = None) -> int:
    """Run only the pack commands introduced before the full WP-13 CLI."""

    parser = argparse.ArgumentParser(prog="mirror-firm")
    commands = parser.add_subparsers(dest="command", required=True)
    packs = commands.add_parser("packs", help="manage external episode packs")
    pack_commands = packs.add_subparsers(dest="pack_command", required=True)

    install = pack_commands.add_parser("install", help="install a pinned external pack")
    install.add_argument("pack", choices=["apex-accounting"])
    install.add_argument("--revision", required=True)
    install.add_argument("--cache-root", type=Path)

    show_gold = pack_commands.add_parser(
        "show-gold", help="explicitly inspect one public development gold output"
    )
    show_gold.add_argument("pack", choices=["apex-accounting"])
    show_gold.add_argument("task_id")
    show_gold.add_argument("--revision", required=True)
    show_gold.add_argument("--cache-root", type=Path)

    arguments = parser.parse_args(argv)
    try:
        if arguments.pack_command == "install":
            pack = install_apex_accounting(
                revision=arguments.revision,
                cache_root=arguments.cache_root,
            )
            print(
                json.dumps(
                    {
                        "pack_id": "apex-accounting",
                        "revision": pack.revision,
                        "root": str(pack.root),
                        "contamination": pack.contamination,
                        "report_label": APEX_ACCOUNTING_LABEL,
                    },
                    sort_keys=True,
                )
            )
            return 0
        root = installed_pack_path(arguments.revision, cache_root=arguments.cache_root)
        print(show_gold_output(root, arguments.task_id))
        return 0
    except ApexImportError as error:
        parser.error(str(error))
    return 2


if __name__ == "__main__":  # pragma: no cover - console-script entry point
    raise SystemExit(main())
