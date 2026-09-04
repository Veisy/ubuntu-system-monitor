#!/usr/bin/env bash
# SYSTEM TEMPERATURE MONITOR - entry point.
# Implementation lives in temp_monitor.py next to this script.
# Usage: temp_monitor.sh [interval_seconds]    run here (keys: q quit, r reset stats)
#        temp_monitor.sh --window [interval]   open a terminal window fitted to this machine
# How the window is sized, and the per-emulator flags: docs/REFERENCE.md.
set -uo pipefail
DIR=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
PY=$DIR/temp_monitor.py
[[ ${1:-} == --window ]] || exec python3 "$PY" "$@"
shift
INTERVAL=${1:-1}
# Same spellings python accepts ('.5', '1e0'), refused here rather than in a
# window that opens only to print an error.
[[ $INTERVAL =~ ^([0-9]+|[0-9]*\.[0-9]+)([eE][-+]?[0-9]+)?$ ]] || {
  echo "temp_monitor: invalid interval: $INTERVAL" >&2; exit 2; }

# --- what the monitor needs -----------------------------------------------------
cols= rows=
if ideal=$(python3 "$PY" --geometry "$INTERVAL" 2>/dev/null); then
  [[ $ideal =~ ^([0-9]+)x([0-9]+)$ ]] && { cols=${BASH_REMATCH[1]}; rows=${BASH_REMATCH[2]}; }
fi

# --- what the screen can show ---------------------------------------------------
screen_px() {                     # "<width> <height>" of ONE monitor, or nothing
  local d
  # Per-monitor only.  xdpyinfo's `dimensions` is the union of all screens, so
  # on a dual-head desktop it would license a window wider than either monitor;
  # with no per-monitor mode we print nothing and the caller maximizes instead.
  d=$(xrandr --current 2>/dev/null | awk '/ connected primary /{print $4; exit}')
  d=${d%%+*}
  [[ $d =~ ^([0-9]+)x([0-9]+)$ ]] || \
    d=$(xrandr --current 2>/dev/null | awk '/\*/{print $1; exit}')      # active mode
  [[ $d =~ ^([0-9]+)x([0-9]+)$ ]] && echo "${BASH_REMATCH[1]} ${BASH_REMATCH[2]}"
}

font_pt() {                       # point size of the desktop's monospace font
  local f=${TEMP_MONITOR_FONT_PT:-}
  [[ -z $f ]] && f=$(gsettings get org.gnome.desktop.interface monospace-font-name 2>/dev/null)
  [[ -z $f ]] && f=$(kreadconfig6 --group General --key fixed 2>/dev/null)
  [[ -z $f ]] && f=$(kreadconfig5 --group General --key fixed 2>/dev/null)
  [[ -z $f ]] && f=$(xfconf-query -c xfce4-terminal -p /font-name 2>/dev/null)
  f=${f//\'/}
  [[ $f == *,* ]] && f=$(cut -d, -f2 <<<"$f")   # KDE: "Hack,10,-1,5,50,..."
  f=${f##* }                                    # else: "Ubuntu Mono 13"
  [[ $f =~ ^[0-9]+$ ]] && echo "$f"
}

ceiling() {                       # "<cols> <rows>" the screen can show, or nothing
  local sw sh pt dpi ts
  read -r sw sh < <(screen_px) || return
  [[ -n ${sw:-} && -n ${sh:-} ]] || return
  pt=$(font_pt) || return
  [[ -n $pt ]] || return
  dpi=$(xdpyinfo 2>/dev/null | awk '/resolution:/{split($2, r, "x"); print r[1]; exit}')
  [[ $dpi =~ ^[0-9]+$ ]] || dpi=96               # the standard default
  ts=$(gsettings get org.gnome.desktop.interface text-scaling-factor 2>/dev/null)
  [[ $ts =~ ^[0-9]+([.][0-9]+)?$ ]] || ts=1      # a scaled desktop has bigger cells
  # em = pt * scale * dpi / 72 px; a monospace cell is about 0.60 em wide and
  # 1.25 em tall.  Cells round UP and the screen counts for 95 % (window
  # chrome), so the ceiling can only under-state what really fits.  awk, not
  # shell arithmetic: the scaling factor is fractional.
  awk -v sw="$sw" -v sh="$sh" -v pt="$pt" -v dpi="$dpi" -v ts="$ts" 'BEGIN {
    em = pt * ts * dpi / 72
    cw = int(0.60 * em); if (cw < 0.60 * em) cw++
    ch = int(1.25 * em); if (ch < 1.25 * em) ch++
    if (cw < 1 || ch < 1) exit 1
    printf "%d %d\n", int(sw * 0.95 / cw), int(sh * 0.95 / ch)
  }'
}

fitted=0
if [[ -n $cols && -n $rows ]]; then
  read -r maxc maxr < <(ceiling)
  if [[ ${maxc:-} =~ ^[0-9]+$ && ${maxr:-} =~ ^[0-9]+$ ]] && (( maxc > 0 && maxr > 0 )); then
    (( cols > maxc )) && cols=$maxc              # never larger than the screen
    (( rows > maxr )) && rows=$maxr
    fitted=1
  fi
fi

# --- open it ---------------------------------------------------------------------
inner=$(printf '%q %q; exec %q' "$DIR/temp_monitor.sh" "$INTERVAL" "${SHELL:-/bin/bash}")

desktop_term() {
  case ${XDG_CURRENT_DESKTOP:-}${DESKTOP_SESSION:-} in
    *GNOME*|*gnome*|*Unity*|*Cinnamon*) echo gnome-terminal ;;
    *KDE*|*plasma*)                     echo konsole ;;
    *XFCE*|*xfce*)                      echo xfce4-terminal ;;
    *MATE*|*mate*)                      echo mate-terminal ;;
  esac
}

term=
if [[ -n ${TERMINAL:-} ]] && command -v "$TERMINAL" >/dev/null; then
  term=$(command -v "$TERMINAL")   # quoted on its own: the value may hold spaces
else
  for c in $(desktop_term) x-terminal-emulator gnome-terminal konsole \
           xfce4-terminal mate-terminal kitty alacritty xterm; do
    command -v "$c" >/dev/null && { term=$(command -v "$c"); break; }
  done
fi
if [[ -z $term ]]; then
  echo "temp_monitor: no known terminal emulator; run it here: $DIR/temp_monitor.sh $INTERVAL" >&2
  exit 1
fi

# A fitted window uses the emulator's own CHARACTER geometry; without a ceiling
# we open maximized instead, which is never larger than the screen either, and
# the renderer adapts to whatever size it gets.
case $(basename "$(readlink -f "$term")") in
  *.wrapper)                       # x-terminal-emulator -> gnome-terminal.wrapper et al:
    # a Perl option translator that knows xterm spellings and silently drops
    # everything else, so the native --geometry=CxR form would launch nothing.
    (( fitted )) && exec "$term" -geometry "${cols}x${rows}" -e bash -c "$inner"
    exec "$term" -e bash -c "$inner" ;;          # no maximize spelling exists here
  gnome-terminal*)
    (( fitted )) && exec "$term" --geometry="${cols}x${rows}" -- bash -c "$inner"
    exec "$term" --maximize -- bash -c "$inner" ;;
  xfce4-terminal*|mate-terminal*)
    (( fitted )) && exec "$term" --geometry="${cols}x${rows}" -x bash -c "$inner"
    exec "$term" --maximize -x bash -c "$inner" ;;
  konsole*)
    (( fitted )) && exec "$term" -p "TerminalColumns=$cols" -p "TerminalRows=$rows" \
                                 -e bash -c "$inner"
    exec "$term" --fullscreen -e bash -c "$inner" ;;
  kitty*)
    (( fitted )) && exec "$term" -o "initial_window_width=${cols}c" \
                                 -o "initial_window_height=${rows}c" bash -c "$inner"
    exec "$term" --start-as=maximized bash -c "$inner" ;;
  alacritty*)
    (( fitted )) && exec "$term" -o "window.dimensions.columns=$cols" \
                                 -o "window.dimensions.lines=$rows" -e bash -c "$inner"
    exec "$term" -o 'window.startup_mode="Maximized"' -e bash -c "$inner" ;;
  xterm*)
    (( fitted )) && exec "$term" -geometry "${cols}x${rows}" -e bash -c "$inner"
    exec "$term" -maximized -e bash -c "$inner" ;;
  *)
    exec "$term" -e bash -c "$inner" ;;          # unknown flags: let it size itself
esac
