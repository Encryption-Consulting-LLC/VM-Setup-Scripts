# VM-Setup-Scripts

Automation for deploying ESXi VMs from a golden image: prepare a base image, generate per-VM
first-boot config, pack it into an ISO, clone the VM, and let it self-configure on first boot.

This repository is the **superproject** — an assembly of three reusable libraries (git
submodules) plus the thin CLIs that drive them. The libraries hold all the logic so the same
core can back the CLIs today and a REST API / GUI later.

## Layout

```
VM-Setup-Scripts/
├── vmkit/         submodule → ESXi/vCenter automation (connect, clone, datastore, VMX)
├── configgen/     submodule → generate per-VM hostname/network first-boot scripts
├── isokit/        submodule → pack first-boot scripts into a config ISO
├── cli/           thin command-line front-ends over the three libraries
└── base-vm-setup/ one-time golden-image prep (Linux firstboot runner, Windows sysprep)
```

The libraries are independent repos under `Arnesh-EC/{vmkit,configgen,isokit}`. Library code
takes parameters and raises exceptions; it never prompts, prints, or calls `sys.exit` — those
belong to the CLI (and future API) layer.

## Setup

```sh
git clone --recurse-submodules git@github-ec:Arnesh-EC/VM-Setup-Scripts.git
cd VM-Setup-Scripts
uv sync          # installs the three libraries (editable) + their deps
```

(Already cloned without `--recurse-submodules`? Run `git submodule update --init`.)

## End-to-end workflow

1. **Prepare the golden image (once).** On a fully configured VM, run the matching base-setup
   script, which installs the first-boot runner and generalizes the image:
   - Linux:   `sudo base-vm-setup/linux-server/firstboot-setup.sh`
   - Windows: `base-vm-setup/windows-server/firstboot-sysprep.ps1`

   Then snapshot / export it as the base.

2. **Generate per-VM config scripts:**
   ```sh
   gen-hostname -n web01 -o 10-hostname.sh
   gen-network --ip 192.168.1.50 --prefix 24 --gateway 192.168.1.1 --dns1 192.168.1.10 -o 20-network.sh
   ```

3. **Pack them into a config ISO:**
   ```sh
   pack-iso 10-hostname.sh 20-network.sh -o isos/web01-config.iso
   ```

4. **Clone and register the VM on ESXi:**
   ```sh
   clone-vm -n web01 -s esxi.example.com -u root --iso isos/web01-config.iso --power-on
   ```

5. **First boot:** the runner finds the config ISO by its `firstboot.manifest`, runs the scripts
   in order (hostname, network), then reboots once.

## CLIs

| Command       | Backed by   | Purpose                                      |
|---------------|-------------|----------------------------------------------|
| `gen-hostname`| configgen   | Generate a per-VM hostname first-boot script |
| `gen-network` | configgen   | Generate a per-VM network first-boot script  |
| `pack-iso`    | isokit      | Pack scripts into a config ISO               |
| `clone-vm`    | vmkit       | Clone the base VM and register it on ESXi    |
| `update-vm`   | vmkit       | Update an existing VM's hardware / config    |

> Status: `clone-vm`, `update-vm`, and `pack-iso` are wired. The unified `gen-hostname` /
> `gen-network` commands land with the API-ready refactor; until then the generators run as
> `python -m configgen.linux.hostname …` / `configgen.windows.network …`.
