#!/usr/bin/env python3
"""update-vm — Update a registered VM's VMX and/or config ISO.

Thin CLI over ``vmkit.update_workflow``. Updates the VMX (CPU, RAM, MAC) and
optionally swaps the config ISO for an existing, powered-off VM. Does not touch
the disk.

KNOWN LIMITATION — swapping the config ISO does NOT re-run first-boot config on an
already-deployed VM. FirstBoot.ps1 is launched by SetupComplete.cmd, which Windows
fires only ONCE, during the post-Sysprep specialize/OOBE pass. After a VM has booted
past first boot, attaching a new ISO and powering on re-applies nothing. Use a new
ISO at clone time (clone-vm) for per-VM config.
"""

import argparse
import logging
import sys

import configgen
import vmkit
from vmkit.validate import validate_cpus, validate_iso_path, validate_mac, validate_memory

import recipes
from cli._common import (
    batch_connector,
    batch_exit_code,
    batch_range,
    log_batch_summary,
    tag_batch_log_lines,
    add_connection_args,
    arg_validator,
    exit_on_error,
    resolve_password,
    setup_cli_logging,
)
from cli._config import CONNECTION_KEYS, add_config_args, apply_config

log = logging.getLogger("deploy-vm")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="update-vm",
        description=(
            "Update a registered VM's VMX (CPU, RAM, MAC) and optionally swap its "
            "config ISO. The VM must exist; it is powered off before the update."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s -n dc01 -s esxi7.example.com -u root -c 4 -r 8192\n"
            "  %(prog)s -n dc01 -s esxi7.example.com -u root "
            "--iso isos/dc01-config.iso --power-on\n"
            "  %(prog)s -n 'cbom-ha-{n}' --range 91-96 -s esxi7.example.com -u root "
            "-c 4 -r 8192 --remove-iso -o -j 6\n\n"
            "A running VM is powered off hard before its update; shut guests down\n"
            "cleanly first when that matters."
        ),
    )
    parser.add_argument(
        "-n", "--name", required=True, metavar="NAME",
        help="VM name / hostname (RFC 1123). With --range, a template containing {n}.",
    )
    parser.add_argument(
        "-m", "--mac-address", default=None, type=arg_validator(validate_mac),
        metavar="MAC", help="Static MAC for ethernet0. If omitted, keeps the existing MAC.",
    )
    parser.add_argument(
        "-c", "--cpus", default=None, type=arg_validator(validate_cpus),
        metavar="N", help="Number of vCPUs (power of 2, 1–128). If omitted, keeps current.",
    )
    parser.add_argument(
        "-r", "--ram", default=None, type=arg_validator(validate_memory),
        metavar="MB", help="RAM in MB (multiple of 4, min 512). If omitted, keeps current.",
    )
    parser.add_argument(
        "--iso", default=None, metavar="FILE",
        help="Local .iso to upload as {name}-config.iso and attach. With --range it may "
             "contain {n}, giving each VM its own.",
    )
    parser.add_argument(
        "--remove-iso", action="store_true",
        help="Remove the CD-ROM attachment (do not upload or attach any ISO).",
    )

    add_connection_args(parser)

    parser.add_argument(
        "-d", "--datastore", default="datastore1", metavar="DS",
        help="Datastore name (default: datastore1).",
    )
    parser.add_argument(
        "-o", "--power-on", action="store_true", help="Power on the VM after updating.",
    )
    parser.add_argument(
        "--range", metavar="SPEC",
        help="Update one VM per number, substituted for {n} in -n (and --iso): 91-96, "
             "91,92,95 or 1-3,7. Every VM must exist; that is checked before any starts.",
    )
    parser.add_argument(
        "-j", "--jobs", type=int, default=recipes.DEFAULT_JOBS, metavar="N",
        help=f"With --range: VMs updated at a time (default: {recipes.DEFAULT_JOBS}).",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Verbose (DEBUG) console output.",
    )
    add_config_args(parser)
    return parser


def _targets(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> list[tuple[str, str | None]]:
    """``(name, iso)`` per VM, each validated: one, or one per --range number."""
    placeholder = recipes.PLACEHOLDER
    if args.range is None:
        for flag, value in (("-n", args.name), ("--iso", args.iso)):
            if value and placeholder in value:
                parser.error(f"{flag} contains {placeholder} but --range was not given.")
        pairs = [(args.name, args.iso)]
    else:
        if placeholder not in args.name:
            parser.error(f"--range needs {placeholder} in -n, e.g. 'web-{placeholder}'.")
        if args.mac_address:
            parser.error("--mac-address cannot be used with --range: VMs would share it.")
        if args.jobs < 1:
            parser.error("--jobs must be at least 1.")
        pairs = [(recipes.expand(args.name, n),
                  recipes.expand(args.iso, n) if args.iso else None)
                 for n in batch_range(args, parser)]
    out = []
    for name, iso in pairs:
        try:
            configgen.validate_hostname_rfc(name)
            if iso is not None:
                iso = validate_iso_path(iso)
        except ValueError as exc:
            parser.error(f"{name}: {exc}" if args.range else str(exc))
        out.append((name, iso))
    return out


def main() -> None:
    parser = build_parser()
    # Connection settings only: a profile's cpus/ram must not silently resize the VM.
    apply_config(parser, CONNECTION_KEYS)
    args = parser.parse_args()
    targets = _targets(args, parser)
    update_options = dict(
        datastore=args.datastore, cpus=args.cpus, mem_mb=args.ram,
        remove_iso=args.remove_iso, power_on=args.power_on,
    )

    if args.range:
        _update_batch(args, targets, update_options)
        return

    ((name, iso),) = targets
    setup_cli_logging(name, args.verbose)

    log.info("=" * 60)
    log.info("Update VM: %s  (datastore: %s)", name, args.datastore)
    log.info("=" * 60)

    password = resolve_password(args)

    with exit_on_error(log, "Update"):
        conn = vmkit.open_connection(args.server, args.user, password, args.port)
        result = vmkit.update_workflow(
            conn, name=name, mac=args.mac_address, iso_path=iso, **update_options,
        )

    log.info("Done. VM '%s' updated: %d CPUs, %d MB RAM, ISO %s.",
             result.name, result.cpus, result.mem_mb, result.iso_action)


def _update_batch(
    args: argparse.Namespace, targets: list[tuple[str, str | None]], update_options: dict
) -> None:
    names = [name for name, _ in targets]
    setup_cli_logging(args.name.replace(recipes.PLACEHOLDER, "N"), args.verbose)
    tag_batch_log_lines()
    jobs = min(args.jobs, len(names))
    log.info("=" * 60)
    log.info("Update %d VMs: %s .. %s  (datastore: %s, %d at a time)",
             len(names), names[0], names[-1], args.datastore, jobs)
    log.info("=" * 60)

    password = resolve_password(args)

    with exit_on_error(log, "Update batch"):
        result = recipes.update_batch(
            batch_connector(args, password), names,
            jobs=jobs,
            iso_paths={name: iso for name, iso in targets if iso},
            on_outcome=lambda o: o.ok and log.info(
                "Updated: %d CPUs, %d MB RAM, ISO %s.",
                o.result.cpus, o.result.mem_mb, o.result.iso_action),
            **update_options,
        )

    log_batch_summary(log, result, lambda r: f"{r.cpus} CPUs, {r.mem_mb} MB RAM, ISO {r.iso_action}"
                      + (", powered on" if r.powered_on else ""))
    sys.exit(batch_exit_code(result))


if __name__ == "__main__":
    main()
