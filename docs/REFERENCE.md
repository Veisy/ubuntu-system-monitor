# Reference

## Columns

| Column | Content |
|---|---|
| Component | `CPU · model`, `GPU n · name`, `nvmeN · model`, `sdX · model · capacity`; `[stale]` after 3 failed reads; long labels keep head + `…` + tail words |
| Temp | °C, coloured by threshold (below) |
| Pwr | watts. GPUs: gauge filled to power ÷ power limit. CPU: text only — no limit is published |
| Load | gauge 0–100 %. CPU: aggregate utilisation from `/proc/stat` deltas. GPU: `utilization.gpu`. Drives: share of capacity in use (mounted filesystems on that disk, or its ZFS pool) |
| VRAM | GPUs only: `used/totalGB`, gauge filled to used ÷ total |
| avg / max | since start or last `r`; extras shown only when the terminal is wider than the full table needs — never in the opening window |
| Status | `Cool` < 50 °C · `OK` 50–69 · `Warm` 70–89 · `HOT!` ≥ 90 (`COOL_MAX`, `OK_MAX`, `WARM_MAX`) |

Gauges share one width: the widest text the visible gauge columns can EVER produce — the
formatters' own ceilings (`PWR_W` 6, `LOAD_W` 4 for `%100`, `VRAM_W` 9), never the current
reading — plus up to one column of margin each side when the terminal has room (`GAUGE_PAD`).
Reserving capacity is what keeps the table still: `99.9W` growing to `100.1W` cannot re-widen
every column. Fill has eighth-block resolution; any non-zero value shows a sliver and only a true
100 % reaches the end. Text over the filled part is drawn inverted. A row with no reading shows
`--`; a value with no full scale (CPU power) is text without a bar.

Rows are grouped CPU → GPU → NVMe → SATA with a rule between groups. A blank spacer is not used
inside a group.

## Layout

Everything is re-laid out every frame for the current terminal size. As the terminal narrows the
gauges give up their side margins first — every gauge stays a bar, never narrower than its
reserved text — then the label shrinks to 8. VRAM is present exactly when the machine has a GPU
and is never dropped. avg/max appear only above the full table's width (label, gauges with
margins, plus both stat columns). Below the minimum
(`temp_monitor.py --geometry` reports what this machine needs; the floor is `min_cols` × 8 rows,
constant for given hardware) a "too small" notice is shown.
Rows that do not fit are summarised as "… N more row(s)".

## Opening geometry

`temp_monitor.py --geometry [interval]` prints `COLSxROWS` for one sensor round — no curses, no
TTY needed. GPUs the round missed are still counted from `/sys/bus/pci/devices` (NVIDIA display
class), so a driver that is briefly unavailable cannot cost the window a row per GPU. Columns are the widest line that *cannot* shrink (the legend row, the header's meta
line, or `min_cols`), so the table adapts inside the window instead of the window growing to the
table's full width: that is 95 on every machine (the legend row's own width), and avg/max
start hidden. Rows cover the
header, every sensor row, the rules between hardware groups, the closing rule, the legend, and
the bottom line curses keeps free.

`temp_monitor.sh --window [interval]` opens it, always passing that size in the emulator's own
character-geometry spelling (`--geometry=CxR`, konsole profile properties, kitty / alacritty
options). When the screen can be measured — pixels of one monitor (`xrandr` primary, else its
active mode; never `xdpyinfo`'s multi-head union), cell size from the desktop monospace font's
point size, the desktop text-scaling factor and DPI (0.60 em wide, 1.25 em tall, rounded up, 95 %
of the screen) — the request is capped by it; without a probe the uncapped size is sent, since a
window manager clamps an oversized window to the work area anyway. `TEMP_MONITOR_FONT_PT`
overrides the font probe. The window is never opened maximized: a maximized gnome-terminal
starts at 80×24 and then jumps to the whole screen.

The launcher also exports `TEMP_MONITOR_FIT=CxR` to the monitor, which — only in that window,
never in a terminal opened by hand — asks the emulator for that size once at start (XTWINOPS
`CSI 8 t`) when the window came up a different size, covering emulators that ignore or lack a
launch flag. gnome-terminal (VTE), xterm and konsole all honour it — an 80×24 window is 95×23
one frame later.

Ptyxis supports neither route, so the launcher supplies its computed size through a verified,
read-only dconf overlay in the runtime directory. The overlay applies only to the new Ptyxis
process and leaves the user's settings unchanged. If dconf, the expected schema, or verification
is unavailable, the launcher warns and lets Ptyxis choose its own size.

`temp_monitor.sh --window --dry-run` prints the emulator, the size and the exact command without
opening anything; `doctor.sh` shows the same line.

## Exit summary

`q`/`Esc`/`Ctrl+C` prints a table of current / power / VRAM / average / maximum (since start or
last `r`) / status per row,
duration and sample count, plus a warning line for anything that reached ≥ 90 °C. Optional
columns are shed on narrow terminals so lines never wrap.

## Data sources

| Value | Source | Notes |
|---|---|---|
| CPU temp | hwmon `k10temp`/`zenpower`/`coretemp`/`x86_pkg_temp`/`cpu_thermal` `temp1_input` (m°C) | read in a child process |
| CPU power | hwmon `power1_input` (µW) or RAPL `/sys/class/powercap/*/energy_uj` Δ ÷ Δt, wrap-safe via `max_energy_range_uj` | RAPL needs `root_setup.sh` |
| CPU load | `/proc/stat` first line, 1 − Δidle/Δtotal | |
| GPU | `nvidia-smi --query-gpu=index,name,temperature.gpu,utilization.gpu,power.draw,power.limit,memory.used,memory.total` | 5 s timeout; `[N/A]` fields become `--` |
| NVMe temp | `/sys/class/nvme/*/hwmon*/temp1_input` | child process |
| SATA temp | hwmon `drivetemp` `temp1_input`, mapped to its block device | child process |
| Drive load | `lsblk -J -o NAME,TYPE,FSTYPE,LABEL,MOUNTPOINTS` → `statvfs` per mountpoint; `zpool list -Hp` for `zfs_member` partitions | swap and loop devices ignored |

The child process for hwmon reads exists because `drivetemp` and NVMe log-page reads issue real
I/O that can block uninterruptibly (D state) when drive firmware stalls; a stuck child is kept and
reaped later, and at most one can exist.

## Options and environment

| | |
|---|---|
| `temp_monitor.sh [interval]` | refresh interval in seconds, default 1.0, minimum 0.1; non-numeric or infinite → exit 2 |
| `-h`, `--help` | usage |
| `TEMP_MONITOR_DEBUG_LOG=/path` | per-round timing log; `SIGUSR1` appends a Python stack trace |

Exit codes: 0 normal, 2 invalid interval / no TTY / terminal error.
