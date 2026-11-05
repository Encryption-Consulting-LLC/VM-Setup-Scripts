#!/usr/bin/env python3
"""bake-base — Generalize a clone of a Linux ``-base-pre`` VM into the base VM.

Thin CLI over ``recipes.bake``. Replaces the manual pack-iso + clone-vm +
wait + detach-ISO sequence from the README's "Bake -base" step, and always
packs the runner as a payload (packing it as a script makes it recurse).
--dry-run prints the equivalent single-step commands instead.

Linux only: a Windows base is generalized by firstboot-sysprep.ps1 on the VM.
"""

import argparse
import logging
import shlex
from pathlib import Path

import configgen
import vmkit
from vmkit.vmx import DEFAULT_NETWORK
from vmkit.validate import validate_cpus, validate_memory

import recipes
from recipes import DEFAULT_BAKE_TIMEOUT_S
from cli._common import (
    add_connection_args,
    arg_validator,
    exit_on_error,
    resolve_password,
    setup_cli_logging,
)
from cli._config import CLONE_KEYS, add_config_args, apply_config

log = logging.getLogger("deploy-vm")

#: The repo's Linux setup folder. Found relative to this file, which holds for the
#: editable install `uv sync` makes; --setup-dir covers anything else.
DEFAULT_SETUP_DIR = Path(__file__).resolve().parents[1] / "base-vm-setup" / "linux-server"

#: ``base`` names what deploy-vm clones from; a bake clones from ``--from`` instead.
BAKE_KEYS = CLONE_KEYS - {"base"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bake-base",
        description=(
            "Bake a Linux base VM: clone NAME-pre as NAME with a bake disc carrying "
            "firstboot-setup.sh, power it on, wait for it to generalize and power "
            "itself off, then detach the disc. The result is the base VM that "
            "deploy-vm / clone-vm clone from."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s ub-24.04-base\n"
            "  %(prog)s ub-24.04-base --from ub-24.04-base-pre -s esxi.example.com -u root\n"
            "  %(prog)s ub-24.04-base --no-wait\n"
            "  %(prog)s ub-24.04-base --dry-run\n\n"
            "NAME must not exist yet. To re-bake, delete the old base first."
        ),
    )
    parser.add_argument(
        "name", type=arg_validator(configgen.validate_hostname_rfc), metavar="NAME",
        help="Base VM to create, e.g. ub-24.04-base.",
    )
    parser.add_argument(
        "--from", dest="pre", type=arg_validator(configgen.validate_hostname_rfc),
        metavar="PRE", help="Installed, not-yet-generalized VM to clone (default: NAME-pre).",
    )
    parser.add_argument(
        "--setup-dir", type=Path, default=DEFAULT_SETUP_DIR, metavar="DIR",
        help="Folder holding firstboot-setup.sh and firstboot-runner.sh "
             "(default: this repo's base-vm-setup/linux-server).",
    )

    vm = parser.add_argument_group("VM and ESXi")
    add_connection_args(vm)
    vm.add_argument("-d", "--datastore", default="datastore1", metavar="DS",
                    help="Datastore name (default: datastore1).")
    vm.add_argument("--network", default=DEFAULT_NETWORK, metavar="PORTGROUP",
                    help=f"Port group for ethernet0 (default: {DEFAULT_NETWORK!r}).")
    vm.add_argument("-c", "--cpus", default=2, type=arg_validator(validate_cpus), metavar="N",
                    help="vCPUs (power of 2). Default: 2.")
    vm.add_argument("-r", "--ram", default=4096, type=arg_validator(validate_memory),
                    metavar="MB", help="RAM in MB. Default: 4096.")
    vm.add_argument("--guest-os", metavar="ID",
                    help="VMware guestOS id. Defaults to the -pre VM's.")
    vm.add_argument("--max-usage", type=float, default=80.0, metavar="PCT",
                    help="Abort if the datastore would pass this %% full (default: 80).")
    vm.add_argument("--skip-disk-check", action="store_true",
                    help="Skip the datastore free-space check.")

    run = parser.add_argument_group("run")
    run.add_argument("--no-wait", dest="wait", action="store_false",
                     help="Return once the bake VM is powered on; do not wait or detach.")
    run.add_argument("--timeout", type=float, default=DEFAULT_BAKE_TIMEOUT_S / 60, metavar="MIN",
                     help=f"How long to wait for the bake to finish "
                          f"(default: {DEFAULT_BAKE_TIMEOUT_S // 60} min).")
    run.add_argument("--keep-disc", dest="detach", action="store_false",
                     help="Leave the bake disc attached after the bake finishes.")
    run.add_argument("--keep-iso", type=Path, metavar="FILE",
                     help="Also save the bake disc here (default: temporary, deleted).")
    run.add_argument("--dry-run", action="store_true",
                     help="Print the equivalent single-step commands; change nothing.")
    run.add_argument("-v", "--verbose", action="store_true",
                     help="Verbose (DEBUG) console output.")

    add_config_args(parser)
    return parser


def _display(path: Path) -> str:
    """``path`` relative to the working directory when it is beneath it."""
    try:
        return str(path.resolve().relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def _equivalent_commands(args: argparse.Namespace) -> list[list[str]]:
    iso = str(args.keep_iso or f"isos/{args.pre}.iso")
    setup = args.setup_dir
    conn = ["-s", args.server, "-u", args.user]
    if args.port != 443:
        conn += ["-P", str(args.port)]

    clone = ["clone-vm", "-n", args.name, "-b", args.pre, *conn, "-d", args.datastore,
             "--network", args.network, "-c", str(args.cpus), "-r", str(args.ram)]
    if args.guest_os:
        clone += ["--guest-os", args.guest_os]
    if args.max_usage != 80.0:
        clone += ["--max-usage", f"{args.max_usage:g}"]
    if args.skip_disk_check:
        clone.append("--skip-disk-check")
    clone += ["--iso", iso, "--power-on"]

    cmds = [
        ["pack-iso", _display(setup / recipes.SETUP_SCRIPT),
         "--file", _display(setup / recipes.RUNNER_SCRIPT), "-o", iso],
        clone,
    ]
    if args.wait and args.detach:
        cmds.append(["update-vm", "-n", args.name, *conn, "-d", args.datastore, "--remove-iso"])
    return cmds


def main() -> None:
    parser = build_parser()
    apply_config(parser, BAKE_KEYS)
    args = parser.parse_args()
    args.pre = args.pre or f"{args.name}-pre"
    if args.pre == args.name:
        parser.error("--from must differ from NAME.")
    if args.timeout <= 0:
        parser.error("--timeout must be positive.")
    for script in (recipes.SETUP_SCRIPT, recipes.RUNNER_SCRIPT):
        if not (args.setup_dir / script).is_file():
            parser.error(f"{script} not found in {args.setup_dir}; pass --setup-dir.")

    if args.dry_run:
        print(f"Dry run: bake '{args.name}' from '{args.pre}' on {args.server}.")
        print("Equivalent commands:")
        for i, cmd in enumerate(_equivalent_commands(args)):
            if i == 2:
                print("  # once the VM has powered itself off:")
            print("  " + shlex.join(cmd))
        return

    setup_cli_logging(args.name, args.verbose)
    log.info("=" * 60)
    log.info("Bake base: %s  (from: %s, datastore: %s)", args.name, args.pre, args.datastore)
    log.info("=" * 60)

    password = resolve_password(args)

    with exit_on_error(log, "Bake"):
        conn = vmkit.open_connection(args.server, args.user, password, args.port)
        result = recipes.bake(
            conn,
            name=args.name,
            pre=args.pre,
            setup_dir=args.setup_dir,
            datastore=args.datastore,
            wait=args.wait,
            detach_iso=args.detach,
            timeout_s=args.timeout * 60,
            keep_iso=args.keep_iso,
            cpus=args.cpus,
            mem_mb=args.ram,
            guest_os=args.guest_os,
            network=args.network,
            max_usage_pct=args.max_usage,
            skip_disk_check=args.skip_disk_check,
        )

    if not result.finished:
        log.info("Bake started on '%s'. It powers itself off when done; then detach "
                 "its disc (update-vm -n %s --remove-iso).", args.name, args.name)
    elif result.iso_detached:
        log.info("Done. '%s' is generalized, powered off, and ready to clone.", args.name)
    else:
        log.info("Done. '%s' is generalized and powered off; its bake disc is still "
                 "attached.", args.name)


if __name__ == "__main__":
    main()
