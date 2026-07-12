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
   to a fully configured VM (the setup script stages the runner from its sibling file —
   `FirstBoot.ps1` / `firstboot-runner.sh` — so both files must travel together), then run
   the setup script, which installs the first-boot runner and generalizes the image:
   - Linux:   `sudo base-vm-setup/linux-server/firstboot-setup.sh`
   - Windows: `base-vm-setup/windows-server/firstboot-sysprep.ps1`

   Then snapshot / export it as the base.

2. **Generate per-VM config scripts:**
   ```sh
   gen-hostname --platform linux -n web01 -o 10-hostname.sh
   gen-network --platform linux --ip 192.168.1.50 --prefix 24 --gateway 192.168.1.1 --dns1 192.168.1.10 -o 20-network.sh
   ```

3. **Pack them into a config ISO:**
   ```sh
   pack-iso 10-hostname.sh 20-network.sh -o isos/web01-config.iso
   ```

4. **Clone and register the VM on ESXi:**
   ```sh
   clone-vm -n web01 -s esxi.example.com -u root --iso isos/web01-config.iso --power-on
   ```

5. **First boot:** the runner finds the config ISO by its `firstboot.manifest`, stages any
   payload files a v2 manifest lists (`pack-iso --file`; scripts reach them via
   `$FIRSTBOOT_FILES_DIR`), runs the scripts in order (hostname, network, ...), then reboots
   once.

Runner tests (no VM needed): `pwsh -NoProfile -Command "Invoke-Pester -Path tests/FirstBoot.Tests.ps1"`
and `tests/test-linux-runner.sh`.

## CLIs

| Command       | Backed by   | Purpose                                      |
|---------------|-------------|----------------------------------------------|
| `gen-hostname`| configgen   | Generate a per-VM hostname first-boot script |
| `gen-network` | configgen   | Generate a per-VM network first-boot script  |
| `pack-iso`    | isokit      | Pack scripts (+ `--file` payloads) into a config ISO |
| `clone-vm`    | vmkit       | Clone the base VM and register it on ESXi    |
| `update-vm`   | vmkit       | Update an existing VM's hardware / config    |

All five `--platform`-aware / ESXi commands are wired as `[project.scripts]` entry points.
