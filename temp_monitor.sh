#!/usr/bin/env bash
# SYSTEM TEMPERATURE MONITOR - entry point.
# Implementation lives in temp_monitor.py next to this script.
# Usage: temp_monitor.sh [interval_seconds]   (keys: q quit, r reset stats)
exec python3 "$(cd "$(dirname "$(readlink -f "$0")")" && pwd)/temp_monitor.py" "$@"
