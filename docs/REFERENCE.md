# Reference

## Columns

| Column | Content |
|---|---|
| Component | `CPU · model`, `GPU n · name`, `nvmeN · model`, `sdX · model · capacity`; `[stale]` after 3 failed reads; long labels keep head + `…` + tail words |
| Temp | °C, coloured by threshold (below) |
| Pwr | watts. GPUs: gauge filled to power ÷ power limit. CPU: text only — no limit is published |
| Load | gauge 0–100 %. CPU: aggregate utilisation from `/proc/stat` deltas. GPU: `utilization.gpu`. Drives: share of capacity in use (mounted filesystems on that disk, or its ZFS pool) |
| VRAM | GPUs only: `used/totalGB`, gauge filled to used ÷ total |
| avg / max | since start or last `r` |
| Status | `Cool` < 50 °C · `OK` 50–69 · `Warm` 70–89 · `HOT!` ≥ 90 (`COOL_MAX`, `OK_MAX`, `WARM_MAX`) |

Gauges share one width: 8 columns, or the longest value on any gauge plus a one-column margin
each side. Fill has eighth-block resolution; any non-zero value shows a sliver and only a true
100 % reaches the end. Text over the filled part is drawn inverted. A row with no reading shows
`--`; a value with no full scale (CPU power) is text without a bar.

Rows are grouped CPU → GPU → NVMe → SATA with a rule between groups. A blank spacer is not used
inside a group.

## Layout

Everything is re-laid out every frame for the current terminal size. Column drop order as the
terminal narrows: avg/max → Load gauge (percentage stays as text) → VRAM → label shrinks to 8.
Below the minimum (48 columns × 8 rows, wider when values are long) a "too small" notice is shown.
Rows that do not fit are summarised as "… N more row(s)".

## Exit summary

`q`/`Esc`/`Ctrl+C` prints a table of current / power / VRAM / average / maximum / status per row,
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
