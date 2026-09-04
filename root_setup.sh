#!/usr/bin/env bash
# Optional root-level setup for temp_monitor. Idempotent; safe to re-run.
#   1. RAPL energy counters world-readable -> CPU power column (Intel/AMD RAPL).
#      The kernel makes energy_uj root-only (CVE-2020-8694 side-channel hardening);
#      on a single-user workstation that is an acceptable trade - your call on shared boxes.
#   2. drivetemp kernel module loaded now and at boot -> SATA/SAS drive temperatures.
# Usage: sudo bash root_setup.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }

# 1. RAPL ---------------------------------------------------------------------
if compgen -G "/sys/class/powercap/*/energy_uj" >/dev/null; then
  RULE=/etc/udev/rules.d/90-rapl-energy-readable.rules
  cat >"$RULE" <<'EOF'
# Allow unprivileged reads of RAPL energy counters (temp_monitor CPU power).
SUBSYSTEM=="powercap", ACTION=="add", TEST=="energy_uj", RUN+="/bin/chmod 0444 /sys%p/energy_uj"
EOF
  udevadm control --reload
  chmod 0444 /sys/class/powercap/*/energy_uj
  echo "RAPL: $(ls /sys/class/powercap/*/energy_uj | wc -l) energy counter(s) readable; rule at $RULE"
else
  echo "RAPL: no /sys/class/powercap energy counters on this machine - skipped"
fi

# 2. drivetemp ----------------------------------------------------------------
if modprobe drivetemp 2>/dev/null; then
  echo drivetemp >/etc/modules-load.d/drivetemp.conf
  echo "drivetemp: loaded and persisted in /etc/modules-load.d/drivetemp.conf"
else
  echo "drivetemp: module not available in this kernel - SATA temperatures will not show"
fi
