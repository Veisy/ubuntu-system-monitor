# Adopting on any Ubuntu machine

Nothing in the program is tied to a particular box: every row is discovered at run time from
`/sys`, `nvidia-smi` and `lsblk`. Adopting = checking which sources the new machine has, enabling
the two that need root, and binding a key.

## 1. Detect and validate

```bash
bash doctor.sh
```

```
runtime:
  [x] python3 3.12 with curses
  [x] UTF-8 locale
  [x] terminal 113x23 (full table needs 113x23)
data sources:
  [x] CPU temperature (hwmon: k10temp nvme drivetemp ...)
  [ ] CPU power - RAPL counters are root-only: sudo bash root_setup.sh
  [x] GPUs: 2 NVIDIA (nvidia-smi)
  [x] NVMe temperatures: 1 controller(s)
  [ ] SATA/SAS temperatures - drivetemp module not loaded: sudo bash root_setup.sh
  [x] drive load = filesystem usage (lsblk)
  [ ] ZFS pool usage - zpool not installed (only matters with ZFS)
```

`[x]` runtime lines are hard requirements; `[ ]` data-source lines only mean that row/column will
be absent or `--`. Each `[ ]` line names the fix when one exists.

Validate by running it: `bash temp_monitor.sh 0.5`. Every device that reports should have a row
within one round; a row that goes `[stale]` has failed three consecutive reads.

## 2. Per-source notes

| Source | How it is read | If missing |
|---|---|---|
| CPU temperature | first hwmon named `k10temp` / `zenpower` (AMD), `coretemp` / `x86_pkg_temp` (Intel), `cpu_thermal` (ARM SBCs), `temp1_input` | check `cat /sys/class/hwmon/hwmon*/name`; add the name to `CPU_HWMON_NAMES` in `temp_monitor.py` if your platform uses another driver |
| CPU power | hwmon `power1_input` (µW) if present, else the RAPL `energy_uj` counter differentiated between rounds | RAPL is root-only by default (CVE-2020-8694 hardening); `root_setup.sh` installs a udev rule making it world-readable — fine on a personal machine, decide for shared ones. No bar is drawn: the kernel publishes no CPU power limit |
| GPU rows | `nvidia-smi --query-gpu=…` (temp, util, power, power limit, memory) | only NVIDIA's proprietary driver; AMD/Intel GPUs are not read. On a GPU that drives the display, a few % idle utilisation is normal (compositor) |
| NVMe temperature | `/sys/class/nvme/nvme*/hwmon*/temp1_input` | present on every current kernel with an NVMe controller |
| SATA/SAS temperature | hwmon `drivetemp` | `root_setup.sh` runs `modprobe drivetemp` and persists it in `/etc/modules-load.d/`; kernel ≥ 5.6 |
| Drive load | `lsblk -J` + `statvfs` over the mounted filesystems on each disk; ZFS members use `zpool list` | needs util-linux ≥ 2.37 (JSON `MOUNTPOINTS`); an unmounted disk shows `--` |

Reads that can block on stalled drive firmware (drivetemp, NVMe log pages) run in a child process
with a timeout, so a sick disk cannot freeze the display.

## 3. Install and bind a key

```bash
bash install.sh                     # ~/.local/bin, GNOME: Ctrl+Shift+Alt+T
DEST=/opt/monitor SHORTCUT='<Super>m' bash install.sh   # custom target / key
```

The shortcut opens `gnome-terminal --geometry=113x23` — exactly the size of the full table with
avg/max columns; smaller terminals drop columns in this order: avg/max, load gauge, VRAM, then
label width. Minimum 48×8.

Other desktops (bind manually):

```
xfce4-terminal --geometry=113x23 -e "bash -c '$HOME/.local/bin/temp_monitor.sh 1; exec bash'"
konsole --geometry 113x23 -e bash -c "$HOME/.local/bin/temp_monitor.sh 1; exec bash"
kitty --override initial_window_width=113c --override initial_window_height=23c $HOME/.local/bin/temp_monitor.sh 1
```

Headless / SSH: just run `temp_monitor.sh`; it needs a TTY, so use `ssh -t`.

## 4. Terminal requirements

UTF-8 locale and a monospace font with block glyphs (`█ ▏▎▍▌▋▊▉ │ ─ …`) — DejaVu Sans Mono,
Noto Mono, any Nerd Font. 256 colours give a grey gauge track; 8-colour terminals get a white one.

## 5. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `requires an interactive terminal (TTY)` | launched without a terminal; use a terminal emulator or `ssh -t` |
| `Terminal too small: WxH` | enlarge; the message states the minimum for the current values |
| bars render as `?` or boxes | font lacks block glyphs (see §4) |
| CPU `Pwr` is `--` | `doctor.sh` line "CPU power" tells you whether RAPL needs `root_setup.sh` or the platform has no sensor |
| no GPU rows | `nvidia-smi -L` must work for the current user |
| a row is `[stale]` | 3 consecutive failed reads: device removed, or its firmware stalled |
| debugging | `TEMP_MONITOR_DEBUG_LOG=/tmp/tm.log temp_monitor.sh` logs each round; `kill -USR1 <pid>` appends a stack trace |

## 6. Uninstall

```bash
rm ~/.local/bin/{temp_monitor.py,temp_monitor.sh,doctor.sh,root_setup.sh}
sudo rm -f /etc/udev/rules.d/90-rapl-energy-readable.rules /etc/modules-load.d/drivetemp.conf
```

Remove the GNOME shortcut in Settings → Keyboard → Custom Shortcuts ("System monitor").
