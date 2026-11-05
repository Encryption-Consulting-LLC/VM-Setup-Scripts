# VM-Setup-Scripts

Automation for deploying ESXi VMs from a golden image: prepare a base image, generate per-VM
first-boot config, pack it into an ISO, clone the VM, and let it self-configure on first boot.

This repository is the **scripts** deliverable — thin CLIs over three reusable libraries that
hold all the logic, so the same core backs these CLIs today and a REST API / GUI later. The
libraries are consumed as **versioned git dependencies** (not vendored), so this repo clones and
runs on its own.

## Layout

```
VM-Setup-Scripts/
├── cli/           thin command-line front-ends over the three libraries
└── base-vm-setup/ one-time golden-image prep (Linux firstboot runner, Windows sysprep)
```

The libraries are independent public repos, pinned in `pyproject.toml` (`[tool.uv.sources]`):
- `vmkit` — ESXi/vCenter automation (connect, clone, datastore, VMX)
- `configgen` — generate per-VM hostname/network first-boot scripts
- `isokit` — pack first-boot scripts into a config ISO

Library code takes parameters and raises exceptions; it never prompts, prints, or calls
`sys.exit` — those belong to the CLI (and future API) layer.

## Setup

```sh
git clone git@github-ec:Arnesh-EC/VM-Setup-Scripts.git
cd VM-Setup-Scripts
uv sync          # installs the CLIs + the three libraries (from their git tags) + deps
```

### Develop a library locally
The libraries install from pinned git tags, so this repo isn't an editable workspace over them.
To hack on a library and see changes here, clone it and install it editable into this venv:

```sh
git clone git@github-ec:Arnesh-EC/vmkit.git ../vmkit
uv pip install -e ../vmkit       # overrides the git-pinned vmkit until the next `uv sync`
```

## End-to-end workflow

1. **Prepare the golden image (once).** Copy the matching `base-vm-setup/<platform>/` folder
   to a fully configured VM (the setup script stages its sibling runner files —
   `FirstBoot.ps1` and `SetupComplete.cmd` on Windows, or `firstboot-runner.sh` on
   Linux — so the whole platform folder must travel together), then run
   the setup script, which installs the first-boot runner and generalizes the image:
   - Linux:   `sudo base-vm-setup/linux-server/firstboot-setup.sh`
   - Windows: `base-vm-setup/windows-server/firstboot-sysprep.ps1`

   Then snapshot / export it as the base.

   On Linux this step can run hands-off. See
   [Building the Linux base unattended](#building-the-linux-base-unattended).

2. **Generate per-VM config scripts:**
   ```sh
   gen-hostname --platform linux -n web01 -o 10-hostname.sh
   gen-network --platform linux --ip 192.168.1.50 --prefix 24 --gateway 192.168.1.1 --dns1 192.168.1.10 -o 20-network.sh
   ```

   On Linux, `gen-network --backend` picks the network stack the generated script
   configures: `netplan` (the default, and what the Ubuntu Server base image runs),
   `systemd-networkd`, or `networkmanager`. It must match the base image — a mismatch
   leaves the clone on its original address. The netplan backend also moves the image's
   shipped `/etc/netplan/*.yaml` aside before writing its own, so a stale interface name
   baked into `50-cloud-init.yaml` cannot fight the new config.

3. **Pack them into a config ISO:**
   ```sh
   pack-iso 10-hostname.sh 20-network.sh -o isos/web01-config.iso
   ```

4. **Clone and register the VM on ESXi:**
   ```sh
   clone-vm -n web01 -s esxi.example.com -u root --iso isos/web01-config.iso --power-on
   ```

5. **First boot:** the configuration flow finds the config ISO by its `firstboot.manifest`, stages any
   payload files a v2 manifest lists (`pack-iso --file`; scripts reach them via
   `$FIRSTBOOT_FILES_DIR`), runs the scripts in order (hostname, network, ...), then reboots
   once. On Windows, `SetupComplete.cmd` schedules that reboot only after the runner
   succeeds, then removes the transient runner, payload, and script files. A failed run
   retains those artifacts and does not reboot.

Runner tests (no VM needed): `pwsh -NoProfile -Command "Invoke-Pester -Path tests/FirstBoot.Tests.ps1"`
and `tests/test-linux-runner.sh`.

## Building the Linux base unattended

The Linux base is built in two stages. `<name>-base-pre` is a plain install with
the first-boot runner armed but not generalized. You keep it as the backup and
re-bake from it whenever `firstboot-setup.sh` changes. `<name>-base` is a clone
of it that has been generalized; every VM is cloned from that.
`base-vm-setup/linux-server/autoinstall/` automates both stages, so nobody has to
log in to either VM.

**One-time, per Ubuntu point release (optional):** remove the installer's
"Continue with autoinstall?" prompt:
```sh
base-vm-setup/linux-server/autoinstall/remaster-iso.sh \
    ubuntu-24.04.5-live-server-amd64.iso isos/ubuntu-24.04.5-autoinstall.iso
```
Without it, an install stops once at that prompt; type `yes` on the console.
The remastered ISO installs onto any VM it boots that has a seed disc attached,
with no confirmation.

**1. Install `-base-pre`.** Build the seed disc, then boot a new, empty VM (EFI
firmware, as vmkit's VMX template uses) with the Ubuntu ISO and the seed disc
attached as two CD-ROMs:
```sh
base-vm-setup/linux-server/autoinstall/build-seed.sh -u <user> \
    --customize my-customize.sh -o isos/ub-24.04-seed.iso
```
The install user's password comes from `$SEED_PASSWORD`, or you're prompted for
it. `--customize` is optional: a script that runs as root on the first boot after
the install, for packages, config, agents and anything else that should be in
every VM. Edit `autoinstall/user-data` to change disk layout, packages and the
rest of the install itself.

The VM installs, reboots, runs the bootstrap once, and **powers off**. It is then
`-base-pre`. Detach both ISOs. If it stays running, read
`/var/log/firstboot-bootstrap.log`.

**2. Bake `-base`.** The bake disc carries the setup script, with the runner as
a payload:
```sh
pack-iso base-vm-setup/linux-server/firstboot-setup.sh \
    --file base-vm-setup/linux-server/firstboot-runner.sh -o isos/ub-24.04-base-pre.iso
clone-vm -n ub-24.04-base -b ub-24.04-base-pre -s esxi.example.com -u root \
    --iso isos/ub-24.04-base-pre.iso --power-on
```
The runner on the clone runs `firstboot-setup.sh` from the disc. That script
re-installs the runner from the disc (so a re-bake picks up runner fixes),
generalizes, and **powers off**. That powered-off clone is the base. Detach its
ISO.

Pack the runner with `--file`, never as a script. Listed as a script, the runner
would execute itself and recurse.

## CLIs

| Command       | Backed by   | Purpose                                      |
|---------------|-------------|----------------------------------------------|
| `gen-hostname`| configgen   | Generate a per-VM hostname first-boot script |
| `gen-network` | configgen   | Generate a per-VM network first-boot script  |
| `pack-iso`    | isokit      | Pack scripts (+ `--file` payloads) into a config ISO |
| `clone-vm`    | vmkit       | Clone the base VM and register it on ESXi    |
| `update-vm`   | vmkit       | Update an existing VM's hardware / config    |

All five `--platform`-aware / ESXi commands are wired as `[project.scripts]` entry points.
