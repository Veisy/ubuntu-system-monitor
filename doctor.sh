#!/usr/bin/env bash
# Detect what this machine can feed the monitor and validate the runtime.
# Read-only; exit 0 when the monitor can run, 1 when a hard requirement is missing.
# Usage: bash doctor.sh
set -uo pipefail
SRC=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
ok()   { printf '  [x] %s\n' "$1"; }
miss() { printf '  [ ] %s\n' "$1"; }
fail=0

echo "runtime:"
if command -v python3 >/dev/null && python3 -c 'import sys, curses; sys.exit(sys.version_info < (3, 8))' 2>/dev/null; then
  ok "python3 $(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))') with curses"
else miss "python3 >= 3.8 with the curses module (apt install python3)"; fail=1; fi
case "${LANG:-}${LC_ALL:-}" in *[Uu][Tt][Ff]-8*|*[Uu][Tt][Ff]8*) ok "UTF-8 locale";; *) miss "UTF-8 locale (block glyphs need it; set LANG=C.UTF-8)";; esac
if [[ -t 1 ]]; then
  read -r rows cols < <(stty size 2>/dev/null || echo 0 0)
  want=$(python3 "$SRC/temp_monitor.py" --geometry 2>/dev/null)   # what THIS machine needs
  if [[ $want =~ ^([0-9]+)x([0-9]+)$ ]]; then
    (( cols >= BASH_REMATCH[1] && rows >= BASH_REMATCH[2] )) \
      && ok "terminal ${cols}x${rows} (this machine needs $want)" \
      || miss "terminal ${cols}x${rows}: this machine needs $want for every row plus the legend; \
below that rows and columns drop out - 'temp_monitor.sh --window' opens a fitted one"
  fi
fi

echo "data sources:"
names=$(cat /sys/class/hwmon/hwmon*/name 2>/dev/null | sort -u | tr '\n' ' ')
if grep -qwE 'k10temp|zenpower|coretemp|x86_pkg_temp|cpu_thermal' <<<"$names"; then ok "CPU temperature (hwmon: $names)"
else miss "CPU temperature - no k10temp/zenpower/coretemp/x86_pkg_temp/cpu_thermal among hwmon: ${names:-none}"; fi
if compgen -G "/sys/class/hwmon/hwmon*/power1_input" >/dev/null; then ok "CPU power (hwmon power1_input)"
elif [[ -r $(compgen -G "/sys/class/powercap/*/energy_uj" | head -1) ]] 2>/dev/null; then ok "CPU power (RAPL energy counter readable)"
elif compgen -G "/sys/class/powercap/*/energy_uj" >/dev/null; then miss "CPU power - RAPL counters are root-only: sudo bash root_setup.sh"
else miss "CPU power - no RAPL or hwmon power sensor on this platform"; fi
if command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1; then ok "GPUs: $(nvidia-smi -L | wc -l) NVIDIA (nvidia-smi)"
elif command -v nvidia-smi >/dev/null; then miss "nvidia-smi present but no working driver (GPU rows hidden)"
else miss "GPU rows - nvidia-smi not found (only NVIDIA is read)"; fi
if compgen -G "/sys/class/nvme/nvme*/hwmon*" >/dev/null; then ok "NVMe temperatures: $(ls -d /sys/class/nvme/nvme* | wc -l) controller(s)"
else miss "NVMe temperatures - no NVMe controller with hwmon"; fi
if grep -qw drivetemp <<<"$names"; then ok "SATA/SAS temperatures (drivetemp)"
elif modinfo drivetemp >/dev/null 2>&1; then miss "SATA/SAS temperatures - drivetemp module not loaded: sudo bash root_setup.sh"
else miss "SATA/SAS temperatures - kernel has no drivetemp module"; fi
if command -v lsblk >/dev/null && lsblk -J -o NAME,MOUNTPOINTS >/dev/null 2>&1; then ok "drive load = filesystem usage (lsblk)"
else miss "drive load - lsblk with JSON MOUNTPOINTS needs util-linux >= 2.37"; fi
command -v zpool >/dev/null && ok "ZFS pool usage for member disks (zpool)" || miss "ZFS pool usage - zpool not installed (only matters with ZFS)"

exit $fail
