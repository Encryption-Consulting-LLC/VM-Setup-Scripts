#!/usr/bin/env python3
"""deploy-vm — Generate a VM's first-boot config, pack it, and clone the VM, in one step.

Thin CLI over ``recipes.deploy``. Equivalent to running gen-hostname,
gen-network, (gen-password,) pack-iso and clone-vm in turn, but the scripts are
rendered in memory and the ISO lives in a temp directory unless --keep-iso is
given. --dry-run prints those equivalent commands instead of running anything.

Settings that do not change from VM to VM (ESXi host, base, gateway, DNS, ...)
belong in a config profile; see cli/_config.py or the README.

Many VMs at once: put {n} in NAME (and --ip, --keep-iso) and give --range; the
batch is checked as a whole, then deployed -j at a time (recipes.deploy_batch).
"""

import argparse
import logging
import shlex
import sys
from pathlib import Path

import configgen
import vmkit
from configgen import NetworkConfig, ValidationError
from vmkit.vmx import DEFAULT_NETWORK
from vmkit.validate import validate_cpus, validate_mac, validate_memory

import recipes
from cli._common import (
    DEFAULT_LINUX_BACKEND,
    add_connection_args,
    arg_validator,
    batch_connector,
    batch_exit_code,
    batch_range,
    exit_on_error,
    log_batch_summary,
    parse_accounts,
    resolve_accounts,
    resolve_password,
    setup_cli_logging,
    tag_batch_log_lines,
)
from cli._config import CLONE_KEYS, NETWORK_KEYS, add_config_args, apply_config

log = logging.getLogger("deploy-vm")

DEFAULT_BASE = "ws-2025-base"


def _ip_or_cidr(value: str) -> tuple[str, int | None]:
    """``ADDR`` or ``ADDR/LEN`` -> ``(addr, len | None)``."""
    addr, sep, prefix = value.partition("/")
    addr = configgen.validate_ipv4(addr)
    return addr, configgen.validate_prefix(prefix) if sep else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deploy-vm",
        description=(
            "Deploy a VM in one step: generate its hostname and network first-boot "
            "scripts (and optional password resets), pack them into a config ISO, "
            "clone the base VM with that ISO attached, and power it on."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s web01 --profile ubuntu --ip 192.168.1.50\n"
            "  %(prog)s web01 --profile ubuntu --ip 192.168.1.50/24 --reset-password root\n"
            "  %(prog)s web02 --profile ubuntu --dhcp --dry-run\n"
            "  %(prog)s 'cbom-ha-{n}' --range 91-96 --ip '192.168.4.{n}/16' --profile k8s -j 3\n"
            "  %(prog)s dc01 --platform windows --ip 10.0.0.10/24 --gateway 10.0.0.1 \\\n"
            "      --dns1 10.0.0.10 -s esxi.example.com -u root -b ws-2025-base\n\n"
            "Any option except NAME, --ip, --mac-address, the passwords and the\n"
            "on/off switches can come from a config profile; a flag overrides it."
        ),
    )
    parser.add_argument(
        "name", metavar="NAME",
        help="VM name and hostname (RFC 1123). With --range, a template containing {n}.",
    )
    parser.add_argument(
        "--platform", required=True, choices=configgen.PLATFORMS,
        help="Guest OS family of the base image.",
    )

    net = parser.add_argument_group("guest network (the generated network script)")
    net.add_argument(
        "--ip", metavar="ADDR[/LEN]",
        help="Static IPv4 address, optionally with its prefix length. Required unless --dhcp. "
             "With --range, a template containing {n}.",
    )
    net.add_argument(
        "--prefix", type=arg_validator(configgen.validate_prefix), metavar="LEN",
        help="Subnet prefix length (1-32), if --ip has no /LEN.",
    )
    net.add_argument(
        "--gateway", type=arg_validator(configgen.validate_ipv4), metavar="ADDR",
        help="Default gateway. Required unless --dhcp.",
    )
    net.add_argument(
        "--dns1", type=arg_validator(configgen.validate_ipv4), metavar="ADDR",
        help="Primary DNS server. Required for static; optional for DHCP.",
    )
    net.add_argument(
        "--dns2", type=arg_validator(configgen.validate_ipv4), metavar="ADDR",
        help="Secondary DNS server.",
    )
    net.add_argument("--dns-suffix", metavar="SUFFIX", help="DNS search suffix.")
    net.add_argument(
        "--dhcp", action=argparse.BooleanOptionalAction, default=False,
        help="Use DHCP instead of a static IP. --no-dhcp overrides a DHCP profile.",
    )
    net.add_argument(
        "--backend", choices=configgen.LINUX_BACKENDS,
        help=f"Linux network stack (default: {DEFAULT_LINUX_BACKEND}); must match the "
             "base image. Invalid for windows.",
    )

    extra = parser.add_argument_group("extra first-boot content")
    extra.add_argument(
        "--reset-password", type=arg_validator(parse_accounts), metavar="USER[:PASS][,...]",
        help="Reset these local accounts' passwords on first boot (as gen-password -u). "
             "A bare USER is prompted for. Passwords are stored in plain text on the ISO.",
    )
    extra.add_argument(
        "--script", action="append", default=[], metavar="FILE",
        help="Extra first-boot script to run after the generated ones (repeatable).",
    )
    extra.add_argument(
        "--file", action="append", default=[], dest="payload", metavar="FILE",
        help="Payload file to stage on the guest, never executed (repeatable).",
    )

    vm = parser.add_argument_group("VM and ESXi")
    add_connection_args(vm)
    vm.add_argument("-d", "--datastore", default="datastore1", metavar="DS",
                    help="Datastore name (default: datastore1).")
    vm.add_argument("-b", "--base", default=DEFAULT_BASE, metavar="BASE",
                    help=f"Base VM to clone (default: {DEFAULT_BASE}).")
    vm.add_argument("--network", default=DEFAULT_NETWORK, metavar="PORTGROUP",
                    help=f"Port group for ethernet0 (default: {DEFAULT_NETWORK!r}).")
    vm.add_argument("-c", "--cpus", default=2, type=arg_validator(validate_cpus), metavar="N",
                    help="vCPUs (power of 2). Default: 2.")
    vm.add_argument("-r", "--ram", default=4096, type=arg_validator(validate_memory),
                    metavar="MB", help="RAM in MB. Default: 4096.")
    vm.add_argument("-m", "--mac-address", type=arg_validator(validate_mac), metavar="MAC",
                    help="Static MAC for ethernet0. Defaults to a random one.")
    vm.add_argument("--guest-os", metavar="ID",
                    help="VMware guestOS id. Defaults to the base VM's.")
    vm.add_argument("--max-usage", type=float, default=80.0, metavar="PCT",
                    help="Abort if the datastore would pass this %% full (default: 80).")
    vm.add_argument("--skip-disk-check", action="store_true",
                    help="Skip the datastore free-space check.")

    batch = parser.add_argument_group("many VMs at once")
    batch.add_argument(
        "--range", metavar="SPEC",
        help="Deploy one VM per number, substituted for {n} in NAME, --ip and --keep-iso: "
             "91-96, 91,92,95 or 1-3,7. A leading zero pads: 01-06. The batch is checked "
             "as a whole (names, IPs, datastore space) before any VM starts.",
    )
    batch.add_argument(
        "-j", "--jobs", type=int, default=recipes.DEFAULT_JOBS, metavar="N",
        help=f"With --range: VMs deployed at a time (default: {recipes.DEFAULT_JOBS}). "
             "A failed VM does not stop the others.",
    )

    run = parser.add_argument_group("run")
    run.add_argument("--no-power-on", dest="power_on", action="store_false",
                     help="Register the VM but leave it powered off.")
    run.add_argument("--keep-iso", type=Path, metavar="FILE",
                     help="Also save the config ISO here (default: temporary, deleted). "
                          "With --range, must contain {n}.")
    run.add_argument("--dry-run", action="store_true",
                     help="Validate and print the equivalent single-step commands; "
                          "change nothing.")
    run.add_argument("-v", "--verbose", action="store_true",
                     help="Verbose (DEBUG) console output.")

    add_config_args(parser)
    return parser


def _network_config(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    ip_value: tuple[str, int | None] | None,
) -> NetworkConfig:
    ip, cidr_prefix = ip_value or (None, None)
    dhcp, prefix, gateway = args.dhcp, args.prefix, args.gateway
    # Profile values must not fight what was typed: --ip beats a DHCP profile,
    # and --dhcp drops a static profile's prefix and gateway.
    if ip is not None and "dhcp" in args.from_config:
        dhcp = False
    if dhcp:
        prefix = None if "prefix" in args.from_config else prefix
        gateway = None if "gateway" in args.from_config else gateway
    if cidr_prefix is not None:
        if prefix is not None and prefix != cidr_prefix and "prefix" not in args.from_config:
            parser.error(f"--ip has /{cidr_prefix} but --prefix is {prefix}.")
        prefix = cidr_prefix
    try:
        return NetworkConfig(
            mode="dhcp" if dhcp else "static",
            ip=ip, prefix=prefix, gateway=gateway,
            dns1=args.dns1, dns2=args.dns2, dns_suffix=args.dns_suffix,
        )
    except ValidationError as exc:
        parser.error(str(exc))


def _backend(args: argparse.Namespace, parser: argparse.ArgumentParser) -> str | None:
    if args.platform == "windows":
        if args.backend is not None and "backend" not in args.from_config:
            parser.error("--backend is a Linux-only option; Windows has a single network stack.")
        return None
    return args.backend or DEFAULT_LINUX_BACKEND


def _equivalent_commands(
    args: argparse.Namespace,
    spec: recipes.DeploySpec,
    usernames: list[str],
    keep_iso: Path | None,
) -> list[list[str]]:
    """The single-step commands this deploy stands for (no passwords)."""
    plat, cfg = spec.platform, spec.network
    sfx = recipes.SCRIPT_SUFFIX[plat]
    iso = str(keep_iso or f"isos/{spec.name}-config.iso")

    cmds = [["gen-hostname", "--platform", plat, "-n", spec.name, "-o", f"10-hostname{sfx}"]]

    net = ["gen-network", "--platform", plat]
    if spec.backend:
        net += ["--backend", spec.backend]
    if cfg.mode == "dhcp":
        net.append("--dhcp")
    else:
        net += ["--ip", cfg.ip, "--prefix", str(cfg.prefix), "--gateway", cfg.gateway]
    for flag, value in (("--dns1", cfg.dns1), ("--dns2", cfg.dns2),
                        ("--dns-suffix", cfg.dns_suffix)):
        if value is not None:
            net += [flag, value]
    cmds.append(net + ["-o", f"20-network{sfx}"])

    scripts = [f"10-hostname{sfx}", f"20-network{sfx}"]
    for user in usernames:
        out = f"30-password-{user}{sfx}"
        cmds.append(["gen-password", "--platform", plat, "-u", user, "-o", out])
        scripts.append(out)

    pack = ["pack-iso", *scripts, *args.script]
    for payload in args.payload:
        pack += ["--file", payload]
    cmds.append(pack + ["-o", iso])

    clone = ["clone-vm", "-n", spec.name, "-s", args.server, "-u", args.user]
    if args.port != 443:
        clone += ["-P", str(args.port)]
    clone += ["-d", args.datastore, "-b", args.base, "--network", args.network,
              "-c", str(args.cpus), "-r", str(args.ram)]
    if args.mac_address:
        clone += ["-m", args.mac_address]
    if args.guest_os:
        clone += ["--guest-os", args.guest_os]
    if args.max_usage != 80.0:
        clone += ["--max-usage", f"{args.max_usage:g}"]
    if args.skip_disk_check:
        clone.append("--skip-disk-check")
    clone += ["--iso", iso]
    if args.power_on:
        clone.append("--power-on")
    cmds.append(clone)
    return cmds


def _targets(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> list[tuple[str, str | None, Path | None]]:
    """``(name, ip_text, keep_iso)`` per VM: one, or one per --range number."""
    placeholder = recipes.PLACEHOLDER
    keep = str(args.keep_iso) if args.keep_iso else None
    if args.range is None:
        for flag, value in (("NAME", args.name), ("--ip", args.ip), ("--keep-iso", keep)):
            if value and placeholder in value:
                parser.error(f"{flag} contains {placeholder} but --range was not given.")
        return [(args.name, args.ip, args.keep_iso)]

    if placeholder not in args.name:
        parser.error(f"--range needs {placeholder} in NAME, e.g. 'web-{placeholder}'.")
    if args.ip and placeholder not in args.ip:
        parser.error(f"--ip has no {placeholder}: every VM in the batch would get the same "
                     "address. Use e.g. '192.168.4.{n}/24', or --dhcp.")
    if args.mac_address:
        parser.error("--mac-address cannot be used with --range: VMs would share it.")
    if keep and placeholder not in keep:
        parser.error(f"--keep-iso needs {placeholder} with --range, or every VM's ISO "
                     "would overwrite the last.")
    if args.jobs < 1:
        parser.error("--jobs must be at least 1.")
    return [
        (recipes.expand(args.name, n),
         recipes.expand(args.ip, n) if args.ip else None,
         Path(recipes.expand(keep, n)) if keep else None)
        for n in batch_range(args, parser)
    ]


def _spec(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    name: str,
    ip_text: str | None,
    backend: str | None,
    accounts: tuple[tuple[str, str], ...],
) -> recipes.DeploySpec:
    """Validate one VM's name and address and build its spec."""
    try:
        configgen.validate_hostname_rfc(name)
        ip_value = _ip_or_cidr(ip_text) if ip_text else None
    except ValueError as exc:
        parser.error(f"{name}: {exc}" if args.range else str(exc))
    return recipes.DeploySpec(
        name=name, platform=args.platform, network=_network_config(args, parser, ip_value),
        backend=backend, accounts=accounts,
        scripts=tuple(Path(p) for p in args.script),
        files=tuple(Path(p) for p in args.payload),
    )


def main() -> None:
    parser = build_parser()
    from_config = apply_config(parser, CLONE_KEYS | NETWORK_KEYS)
    args = parser.parse_args()
    args.from_config = {k for k, v in from_config.items() if getattr(args, k) == v}

    for path in [*args.script, *args.payload]:
        if not Path(path).is_file():
            parser.error(f"file not found: {path}")

    targets = _targets(args, parser)
    backend = _backend(args, parser)
    accounts = args.reset_password or []

    # Validate everything renderable before prompting or connecting. Passwords
    # are checked as they are parsed or prompted for, so a placeholder stands in.
    placeholder_accounts = tuple((user, "placeholder") for user, _ in accounts)
    previews = [_spec(args, parser, name, ip, backend, placeholder_accounts)
                for name, ip, _ in targets]
    for preview in previews:
        try:
            recipes.render_scripts(preview)
        except ValidationError as exc:
            parser.error(f"{preview.name}: {exc}" if args.range else str(exc))

    if args.dry_run:
        usernames = [user for user, _ in accounts]
        if args.range:
            print(f"Dry run: deploy {len(targets)} VMs from base '{args.base}' on "
                  f"{args.server}, {min(args.jobs, len(targets))} at a time. Each is "
                  "equivalent to:")
        for preview, (_, _, keep_iso) in zip(previews, targets):
            if args.range:
                print(f"\n  # {preview.name}")
            else:
                print(f"Dry run: deploy '{preview.name}' from base '{args.base}' on "
                      f"{args.server}.")
                print("Equivalent commands:")
            for cmd in _equivalent_commands(args, preview, usernames, keep_iso):
                print("  " + shlex.join(cmd))
        return

    resolved = tuple(resolve_accounts(accounts, parser))
    specs = [_spec(args, parser, name, ip, backend, resolved) for name, ip, _ in targets]
    clone_options = dict(
        base=args.base, datastore=args.datastore, cpus=args.cpus, mem_mb=args.ram,
        guest_os=args.guest_os, network=args.network, max_usage_pct=args.max_usage,
        skip_disk_check=args.skip_disk_check, power_on=args.power_on,
    )

    if args.range:
        _deploy_batch(args, specs, targets, clone_options)
        return

    (spec,) = specs
    cfg = spec.network
    setup_cli_logging(spec.name, args.verbose)
    log.info("=" * 60)
    log.info("Deploy VM: %s  (base: %s, datastore: %s, network: %s)",
             spec.name, args.base, args.datastore, args.network)
    if cfg.mode == "static":
        log.info("Guest network: %s/%s via %s (%s)", cfg.ip, cfg.prefix, cfg.gateway,
                 backend or args.platform)
    else:
        log.info("Guest network: DHCP (%s)", backend or args.platform)
    log.info("=" * 60)

    password = resolve_password(args)

    with exit_on_error(log, "Deploy"):
        conn = vmkit.open_connection(args.server, args.user, password, args.port)
        result = recipes.deploy(
            conn, spec, keep_iso=args.keep_iso, mac=args.mac_address, **clone_options,
        )

    clone = result.clone
    log.info("Done. VM '%s' registered (MAC %s)%s.", clone.name, clone.mac,
             ", powering through first boot" if clone.powered_on else ", powered off")
    if result.iso_path:
        log.info("Config ISO kept at %s.", result.iso_path)


def _deploy_batch(
    args: argparse.Namespace,
    specs: list[recipes.DeploySpec],
    targets: list[tuple[str, str | None, Path | None]],
    clone_options: dict,
) -> None:
    label = args.name.replace(recipes.PLACEHOLDER, "N")
    setup_cli_logging(label, args.verbose)
    tag_batch_log_lines()
    jobs = min(args.jobs, len(specs))
    log.info("=" * 60)
    log.info("Deploy %d VMs: %s .. %s  (base: %s, datastore: %s, %d at a time)",
             len(specs), specs[0].name, specs[-1].name, args.base, args.datastore, jobs)
    log.info("=" * 60)

    password = resolve_password(args)

    def on_plan(plan: vmkit.CapacityPlan) -> None:
        log.info("Batch fits: datastore %.1f%% -> %.1f%% after %d clones.",
                 plan.pct_now, plan.pct_after, plan.vm_count)
        for warning in plan.warnings:
            log.warning("%s", warning)

    def on_outcome(outcome: recipes.Outcome) -> None:
        if outcome.ok:
            log.info("Deployed (MAC %s).", outcome.result.clone.mac)

    with exit_on_error(log, "Deploy batch"):
        result = recipes.deploy_batch(
            batch_connector(args, password), specs,
            jobs=jobs,
            keep_isos={name: keep for name, _, keep in targets if keep},
            on_plan=on_plan, on_outcome=on_outcome,
            **clone_options,
        )

    log_batch_summary(log, result, lambda r: f"MAC {r.clone.mac}"
                      + (", powering through first boot" if r.clone.powered_on else ""))
    sys.exit(batch_exit_code(result))


if __name__ == "__main__":
    main()
