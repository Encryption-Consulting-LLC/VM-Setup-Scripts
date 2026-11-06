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
├── recipes/       multi-step workflows that chain the libraries (deploy, bake)
└── base-vm-setup/ one-time golden-image prep (Linux firstboot runner, Windows sysprep)
```

The libraries are independent public repos, pinned in `pyproject.toml` (`[tool.uv.sources]`):
- `vmkit` — ESXi/vCenter automation (connect, clone, datastore, VMX)
- `configgen` — generate per-VM hostname/network first-boot scripts
- `isokit` — pack first-boot scripts into a config ISO

Library code takes parameters and raises exceptions; it never prompts, prints, or calls
`sys.exit` — those belong to the CLI (and future API) layer. `recipes/` follows the same
rule: it holds the workflows that need more than one library (vmkit must not depend on
configgen), so `deploy-vm`, `bake-base` and a future API share one implementation.

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

## Quick start

Put the settings that don't change between VMs in a config file once
(see [Config file](#config-file)):

```sh
mkdir -p ~/.config/vm-setup
cp vm-setup.example.toml ~/.config/vm-setup/config.toml   # then edit it
```

Then each operation is one command:

```sh
install-base ub-24.04-base --seed ISO/ub-24.04-seed.iso  # autoinstall a new -base-pre
bake-base ub-24.04-base                                  # -base-pre -> generalized base
deploy-vm web01 --profile ubuntu --ip 192.168.1.50       # scripts -> ISO -> clone -> power on
deploy-vm web02 --profile ubuntu --ip 192.168.1.51/24 --reset-password root
deploy-vm web03 --profile ubuntu --ip 192.168.1.52 --dry-run   # show the long-form commands
deploy-vm 'web-{n}' --range 61-66 --ip '192.168.1.{n}' --profile ubuntu -j 3   # six VMs, 3 at a time
```

`deploy-vm` does steps 2–4 below in one go; `install-base` and `bake-base` do
steps 1 and 2 of [Building the Linux base unattended](#building-the-linux-base-unattended).
With `--dry-run`, `deploy-vm` and `bake-base` print the single-step commands they
stand for, `install-base` prints what it would create; none of them change anything.
The single-step commands remain for anything the shortcuts don't cover.

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

Steps 2–4 in one command: `deploy-vm web01 --platform linux --ip 192.168.1.50/24
--gateway 192.168.1.1 --dns1 192.168.1.10 -s esxi.example.com -u root -b ub-24.04-base`
(shorter with a profile). It also takes `--reset-password USER[:PASS],...` (as
`gen-password`), `--script FILE` for extra first-boot scripts run after the generated
ones, `--file FILE` for payloads, `--keep-iso FILE` to save the ISO, and `--no-power-on`.

Tests (no VM needed): `uv run pytest` (config and recipes),
`pwsh -NoProfile -Command "Invoke-Pester -Path tests/FirstBoot.Tests.ps1"`
and `tests/test-linux-runner.sh` (runners).

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

**1. Install `-base-pre`.** Build the seed disc and upload it and the installer
to a datastore folder:
```sh
base-vm-setup/linux-server/autoinstall/build-seed.sh -u <user> \
    --customize my-customize.sh -o isos/ub-24.04-seed.iso
upload-file isos/ubuntu-24.04.5-autoinstall.iso --dir ISO
upload-file isos/ub-24.04-seed.iso --dir ISO
```
The install user's password comes from `$SEED_PASSWORD`, or you're prompted for
it. `--customize` is optional: a script that runs as root on the first boot after
the install, for packages, config, agents and anything else that should be in
every VM. Edit `autoinstall/user-data` to change disk layout, packages and the
rest of the install itself.

Then create and install the VM:
```sh
install-base ub-24.04-base --installer ISO/ubuntu-24.04.5-autoinstall.iso \
    --seed ISO/ub-24.04-seed.iso          # creates ub-24.04-base-pre; --as PRE to override
```
ISO paths are relative to `-d`, or `"[datastore] path"` for another datastore;
both are checked before anything is created. It creates the VM with a blank disk
(`--disk-gb`, default 40, thin unless `--thick`), EFI, and the installer on the
first CD-ROM so it boots, then powers it on. The VM installs, reboots, runs the
bootstrap once, and **powers off**; `install-base` waits for that (`--timeout`,
default 60 min) and then detaches both discs. That powered-off VM is
`-base-pre`. The port group (`--network`) needs DHCP for the install.

Detaching matters: the remastered installer installs without asking, so a VM
that boots it again wipes its disk. If the install times out, both discs are still
attached; on the console, an installer screen means the install failed, a login
prompt means the bootstrap did (read `/var/log/firstboot-bootstrap.log`).

The VM is built from the same VMX template as every clone, so its NIC sits at the
same PCI slot and has the same interface name as its clones. A `-base-pre` made
by hand in the Host Client doesn't, and `bake-base` warns about it.

**2. Bake `-base`.**
```sh
bake-base ub-24.04-base          # clones ub-24.04-base-pre; --from PRE to override
```
It packs the bake disc, clones `-base-pre` with it, powers it on, waits for the
clone to generalize and power itself off (`--timeout`, default 30 min), then
detaches the disc. The VM must not exist yet. `--no-wait` returns once the clone
is powered on. If the bake times out, the VM is still running and the disc is
still attached; read `/var/log/firstboot.log` and `/var/log/firstboot-error.log`
on its console.

By hand, the same thing: the bake disc carries the setup script, with the runner
as a payload:
```sh
pack-iso base-vm-setup/linux-server/firstboot-setup.sh \
    --file base-vm-setup/linux-server/firstboot-runner.sh -o isos/ub-24.04-base-pre.iso
clone-vm -n ub-24.04-base -b ub-24.04-base-pre -s esxi.example.com -u root \
    --iso isos/ub-24.04-base-pre.iso --power-on
```
The runner on the clone runs `firstboot-setup.sh` from the disc. That script
re-installs the runner from the disc (so a re-bake picks up runner fixes),
generalizes, and **powers off**. That powered-off clone is the base. Detach its
ISO (`update-vm -n ub-24.04-base -s ... -u root --remove-iso`).

Pack the runner with `--file`, never as a script. Listed as a script, the runner
would execute itself and recurse.

## Many VMs at once

`deploy-vm` and `update-vm` take a `{n}` placeholder and `--range`, and do one VM
per number, `-j` at a time (default 3):

```sh
deploy-vm 'cbom-ha-{n}' --range 91-96 --ip '192.168.4.{n}/16' --profile k8s -j 3
update-vm -n 'cbom-ha-{n}' --range 91-96 -c 4 -r 8192 --remove-iso -o -j 6
```

- `--range` takes `91-96`, `91,92,95` or `1-3,7`; a leading zero pads (`01-06`).
  `{n}` works in `NAME`, `--ip` and `--keep-iso` (deploy-vm), and in `-n` and
  `--iso` (update-vm).
- **Checked as a whole before anything starts.** For a deploy, that means names
  are unique and not already on the host, static IPs are unique, every VM's
  scripts render, and the datastore has room for *all* the clones together
  (separate commands would each see the same free space and each "fit"). Memory
  overcommit is reported. An update checks that every VM exists. Problems are
  listed together and nothing is created.
- **One failure doesn't stop the rest.** Every VM is attempted, then a per-VM
  summary is printed. A failed clone's datastore files are deleted, unless the VM
  had already registered; then it is left for inspection.
- MACs are drawn up front so none repeat within the batch. One ESXi session per
  worker; one password prompt. Console lines are prefixed with the VM's name.
- Ctrl-C stops new VMs from starting and cancels running vSphere tasks; a second
  Ctrl-C aborts. `--dry-run` prints each VM's equivalent commands.
- `update-vm` powers a running VM off **hard** before updating it. Shut the
  guests down cleanly first when that matters.

## CLIs

| Command       | Backed by   | Purpose                                      |
|---------------|-------------|----------------------------------------------|
| `deploy-vm`   | recipes     | Generate config, pack the ISO, clone and power on — in one step |
| `install-base`| recipes     | Create and autoinstall a Linux `-base-pre` VM from scratch |
| `bake-base`   | recipes     | Generalize a Linux `-base-pre` clone into the base VM |
| `gen-hostname`| configgen   | Generate a per-VM hostname first-boot script |
| `gen-network` | configgen   | Generate a per-VM network first-boot script  |
| `pack-iso`    | isokit      | Pack scripts (+ `--file` payloads) into a config ISO |
| `clone-vm`    | vmkit       | Clone the base VM and register it on ESXi    |
| `update-vm`   | vmkit       | Update an existing VM's hardware / config    |
| `gen-password`| configgen   | Generate per-VM password-reset first-boot scripts |
| `upload-file` | vmkit       | Copy a local file to a datastore folder      |

All are wired as `[project.scripts]` entry points.

The ESXi password comes from `-p`, else `$VMSETUP_PASSWORD`, else a prompt.

Exit codes for the ESXi commands: `2` connection or login failed, `3` VM already
exists / not found, `4` other failure, `5` config ISO could not be built, `6` not
enough datastore space, `7` a bake or install did not finish within `--timeout`,
`8` one or more VMs in a `--range` batch failed (see the summary), `130` a batch
was cancelled with Ctrl-C.

## Config file

Every command reads defaults from TOML, so per-site values (ESXi host, datastore,
base, gateway, DNS) aren't retyped. [`vm-setup.example.toml`](vm-setup.example.toml)
is a starting point.

```toml
[defaults]                 # every command
server = "esxi.example.com"
user = "root"

[profile.ubuntu]           # with --profile ubuntu (or $VMSETUP_PROFILE=ubuntu)
platform = "linux"
base = "ub-24.04-base"
prefix = 24
gateway = "192.168.1.1"
dns1 = "192.168.1.10"
```

- **Files**, lowest precedence first: `~/.config/vm-setup/config.toml` (honours
  `$XDG_CONFIG_HOME`), then `./vm-setup.toml`. `--config FILE` or `$VMSETUP_CONFIG`
  uses that one file instead.
- **Precedence:** flag > profile > `[defaults]` > built-in default.
- **Keys** are the long option names: `server user port datastore base network
  guest-os cpus ram max-usage platform backend dhcp prefix gateway dns1 dns2
  dns-suffix`, plus `installer seed disk-gb` for `install-base`. Each command uses
  the ones it has an option for; `update-vm` and
  `upload-file` take only the connection keys (`server user port datastore`), so a
  profile's `cpus`/`ram` can never resize an existing VM.
- **Not configurable:** anything naming one VM (name, IP, MAC), passwords, and
  on/off switches like `--power-on`. An unknown key or invalid value is an error
  that names the file.
- `--no-dhcp` overrides `dhcp = true`, and so does passing `--ip` to `deploy-vm`.
  With `deploy-vm --dhcp`, a profile's `prefix` and `gateway` are dropped. On
  Windows, a `backend` from the config is ignored.
