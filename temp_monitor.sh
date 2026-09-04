#!/usr/bin/env bash
# SYSTEM TEMPERATURE MONITOR - entry point.
# Implementation lives in temp_monitor.py next to this script.
# Usage: temp_monitor.sh [interval_seconds]    run here (keys: q quit, r reset stats)
#        temp_monitor.sh --window [interval]   open a terminal window fitted to this machine
#        temp_monitor.sh --window --dry-run     print the emulator and size it would use
# How the window is sized, the per-emulator flags, and why ptyxis needs a
# dconf overlay instead of a flag: docs/REFERENCE.md.
set -uo pipefail
DIR=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
PY=$DIR/temp_monitor.py
[[ ${1:-} == --window ]] || exec python3 "$PY" "$@"
shift
dry=0 INTERVAL=1
for a in "$@"; do
  [[ $a == --dry-run ]] && { dry=1; continue; }
  INTERVAL=$a
done
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

# The ideal is always what gets sent: a window manager clamps an oversized
# window to the work area anyway, and the renderer adapts to what it gets.  A
# screen ceiling, when one can be measured, only shrinks the request further.
ceiling_note="no screen probe"
if [[ -n $cols && -n $rows ]]; then
  read -r maxc maxr < <(ceiling)
  if [[ ${maxc:-} =~ ^[0-9]+$ && ${maxr:-} =~ ^[0-9]+$ ]] && (( maxc > 0 && maxr > 0 )); then
    (( cols > maxc )) && cols=$maxc              # never larger than the screen
    (( rows > maxr )) && rows=$maxr
    ceiling_note="screen fits ${maxc}x${maxr}"
  fi
else
  echo "temp_monitor: --geometry failed; the emulator picks its own size" >&2
fi

# --- open it ---------------------------------------------------------------------
# TEMP_MONITOR_FIT lets the monitor ask the emulator for this size once it is
# running (XTWINOPS), for emulators whose launch flags are ignored or missing.
fit=${cols:+${cols}x${rows}}
inner=$(printf 'TEMP_MONITOR_FIT=%q %q %q; exec %q' "$fit" "$DIR/temp_monitor.sh" \
        "$INTERVAL" "${SHELL:-/bin/bash}")

# Ptyxis has no character-geometry flag and ignores XTWINOPS resizing. Overlay
# only its window-size settings for this process; never write the user's dconf.
ptyxis_build() {                  # compile the overlay for ${cols}x${rows}
  local db=$1 prof=$2 kf
  kf=$(mktemp -d "$(dirname "$db")/kf.XXXXXX") || return 1
  printf '[org/gnome/Ptyxis]\nrestore-window-size=false\nrestore-session=false\ndefault-columns=uint32 %s\ndefault-rows=uint32 %s\n' \
         "$cols" "$rows" >"$kf/fit"
  if dconf compile "$kf.db" "$kf" 2>/dev/null && [[ -s $kf.db ]] &&
     printf 'file-db:%s\nuser-db:user\n' "$db" >"$kf.profile" &&
     mv -f "$kf.db" "$db" && mv -f "$kf.profile" "$prof"; then
    rm -rf "$kf"; return 0
  fi
  rm -rf "$kf" "$kf.db" "$kf.profile"; return 1
}

ptyxis_verifies() {               # does ptyxis really read ${cols}x${rows} here?
  local prof=$1 c r window session
  c=$(DCONF_PROFILE=$prof gsettings get org.gnome.Ptyxis default-columns 2>/dev/null)
  r=$(DCONF_PROFILE=$prof gsettings get org.gnome.Ptyxis default-rows 2>/dev/null)
  window=$(DCONF_PROFILE=$prof gsettings get org.gnome.Ptyxis restore-window-size 2>/dev/null)
  session=$(DCONF_PROFILE=$prof gsettings get org.gnome.Ptyxis restore-session 2>/dev/null)
  [[ ${c##* } == "$cols" && ${r##* } == "$rows" && $window == false && $session == false ]]
}

ptyxis_profile() {                # print a verified DCONF_PROFILE, or fail
  local dir=${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/temp_monitor.$UID
  local db=$dir/$1.db prof=$dir/$1.profile attempt
  command -v dconf >/dev/null && command -v gsettings >/dev/null || return 1
  if [[ -e $dir || -L $dir ]]; then
    [[ -d $dir && ! -L $dir && -O $dir ]] || return 1
  else
    (umask 077; mkdir "$dir") || return 1
  fi
  chmod 700 "$dir" || return 1
  for attempt in 1 2; do          # a cached db that stops verifying is rebuilt once
    [[ -s $db && -s $prof ]] || ptyxis_build "$db" "$prof" || return 1
    ptyxis_verifies "$prof" && { echo "$prof"; return 0; }
    rm -f "$db" "$prof"
  done
  return 1
}

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

# Each emulator gets the size in its own CHARACTER-geometry spelling; with no
# size at all it opens however it likes and the renderer adapts.
kind=$(basename "$(readlink -f "$term")")
cmd=("$term")
case $kind in
  *.wrapper)                       # x-terminal-emulator -> gnome-terminal.wrapper et al:
    # a Perl option translator that knows xterm spellings and silently drops
    # everything else, so the native --geometry=CxR form would launch nothing.
    [[ -n $fit ]] && cmd+=(-geometry "$fit"); cmd+=(-e) ;;
  gnome-terminal*)
    [[ -n $fit ]] && cmd+=(--geometry="$fit"); cmd+=(--) ;;
  xfce4-terminal*|mate-terminal*)
    [[ -n $fit ]] && cmd+=(--geometry="$fit"); cmd+=(-x) ;;
  konsole*)
    [[ -n $fit ]] && cmd+=(-p "TerminalColumns=$cols" -p "TerminalRows=$rows"); cmd+=(-e) ;;
  kitty*)
    [[ -n $fit ]] && cmd+=(-o "initial_window_width=${cols}c" -o "initial_window_height=${rows}c") ;;
  alacritty*)
    [[ -n $fit ]] && cmd+=(-o "window.dimensions.columns=$cols" -o "window.dimensions.lines=$rows")
    cmd+=(-e) ;;
  xterm*)
    [[ -n $fit ]] && cmd+=(-geometry "$fit"); cmd+=(-e) ;;
  ptyxis*)
    # -s is what makes the profile bite: without it ptyxis hands the command to
    # the instance already running, a process that never sees our environment.
    if [[ -n $fit ]] && prof=$(ptyxis_profile "fit-$fit"); then
      cmd=(env "DCONF_PROFILE=$prof" "$term" -s --)
    else
      [[ -n $fit ]] && echo "temp_monitor: could not size ptyxis through dconf;" \
        "the window opens at whatever size it remembers" >&2
      cmd+=(--)                    # -e is undocumented here; -- is not
    fi ;;
  *)
    cmd+=(-e) ;;                   # unknown flags: size only via TEMP_MONITOR_FIT
esac
cmd+=(bash -c "$inner")

if (( dry )); then
  echo "emulator: $term ($kind)"
  echo "size:     ${fit:-none} (${ceiling_note})"
  printf 'command: '; printf '%q ' "${cmd[@]}"; echo
  exit 0
fi
exec "${cmd[@]}"
