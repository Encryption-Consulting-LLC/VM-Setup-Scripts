#!/usr/bin/env python3
"""upload-file — Copy a local file into a datastore folder on ESXi.

Thin CLI over ``vmkit.datastore.upload_file``: parse args, prompt for the
password, open a connection, ensure the destination folder exists, and PUT the
file to ``[datastore] <dir>/<name>`` over HTTPS. The remote filename defaults to
the local basename.
"""

import argparse
import logging
import sys
from pathlib import Path

import vmkit
from vmkit.datastore import make_directory, upload_file
from vmkit.esxi import get_datacenter
from vmkit.progress import setup_logging

from cli._common import add_connection_args, resolve_password

log = logging.getLogger("deploy-vm")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="upload-file",
        description=(
            "Copy a local file into a folder on an ESXi datastore over HTTPS. "
            "The destination folder is created if it does not exist; the remote "
            "filename defaults to the local basename."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s ws-base-pre.iso -d datastore1 --dir isos -s esxi7.example.com -u root\n"
            "  %(prog)s agent.exe --dir payloads --name installer.exe "
            "-s esxi7.example.com -u root"
        ),
    )
    parser.add_argument(
        "file", metavar="FILE",
        help="Local file to upload.",
    )
    parser.add_argument(
        "--dir", required=True, metavar="DIR",
        help="Destination folder within the datastore (created if missing).",
    )
    parser.add_argument(
        "--name", default=None, metavar="NAME",
        help="Remote filename. Defaults to the local file's basename.",
    )

    add_connection_args(parser)

    parser.add_argument(
        "-d", "--datastore", default="datastore1", metavar="DS",
        help="Datastore name (default: datastore1).",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Verbose (DEBUG) console output.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()

    local = Path(args.file)
    if not local.is_file():
        raise SystemExit(f"Local file not found: {local}")
    remote_name = args.name or local.name

    setup_logging(local.stem, args.verbose)

    log.info("=" * 60)
    log.info("Upload file: %s -> [%s] %s/%s",
             local, args.datastore, args.dir, remote_name)
    log.info("=" * 60)

    password = resolve_password(args)

    try:
        conn = vmkit.open_connection(args.server, args.user, password, args.port)
        dc = get_datacenter(conn.content)
        make_directory(conn.content, dc, args.datastore, args.dir)
        upload_file(
            conn.host, conn.user, conn.password, conn.port,
            args.datastore, dc.name, args.dir, str(local),
            remote_filename=remote_name,
        )
    except (vmkit.AuthenticationError, vmkit.ConnectionFailedError) as exc:
        log.error("%s", exc)
        sys.exit(2)
    except vmkit.VmkitError as exc:
        log.error("Upload failed: %s", exc)
        sys.exit(4)
    except Exception as exc:
        log.error("Upload failed: %s", exc)
        log.debug("Traceback:", exc_info=True)
        sys.exit(4)

    log.info("Done. Uploaded to [%s] %s/%s", args.datastore, args.dir, remote_name)


if __name__ == "__main__":
    main()
