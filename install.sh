#!/usr/bin/env bash
# Per-user install. No root. Idempotent.
#   - copies the program to $DEST (default ~/.local/bin)
#   - on GNOME binds $SHORTCUT (default Ctrl+Shift+Alt+T) to open it in a fitted terminal
#   - runs doctor.sh so you see what this machine will show
# Usage: bash install.sh      [DEST=/path] [SHORTCUT='<Super>t']
set -euo pipefail
SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DEST=${DEST:-$HOME/.local/bin}
SHORTCUT=${SHORTCUT:-<Ctrl><Shift><Alt>t}
GEOMETRY=113x23

bash "$SRC/doctor.sh" >/dev/null || { bash "$SRC/doctor.sh"; echo "fix the [ ] runtime items above first"; exit 1; }

mkdir -p "$DEST"
for f in temp_monitor.py temp_monitor.sh root_setup.sh doctor.sh; do
  install -m 0755 "$SRC/$f" "$DEST/$f"
done
echo "installed to $DEST"
case ":$PATH:" in *":$DEST:"*) ;; *) echo "note: $DEST is not on PATH - run $DEST/temp_monitor.sh";; esac

CMD="gnome-terminal --geometry=$GEOMETRY -- bash -c '$DEST/temp_monitor.sh 1; exec bash'"
if command -v gsettings >/dev/null && command -v gnome-terminal >/dev/null \
   && [[ ${XDG_CURRENT_DESKTOP:-} == *GNOME* ]]; then
  BASE=org.gnome.settings-daemon.plugins.media-keys
  PFX=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings
  list=$(gsettings get $BASE custom-keybindings)
  slot=""
  for p in $(tr -d "[]'," <<<"$list"); do   # reuse an existing temp_monitor binding
    gsettings get $BASE.custom-keybinding:"$p" command | grep -q temp_monitor && { slot=$p; break; }
  done
  if [[ -z $slot ]]; then                    # else the first free slot
    for i in $(seq 0 99); do grep -q "$PFX/custom$i/" <<<"$list" || { slot="$PFX/custom$i/"; break; }; done
    new=$(python3 -c 'import ast,sys; l=ast.literal_eval(sys.argv[1]) if sys.argv[1] not in ("[]","@as []") else []; l.append(sys.argv[2]); print(repr(l))' "$list" "$slot")
    gsettings set $BASE custom-keybindings "$new"
  fi
  gsettings set $BASE.custom-keybinding:"$slot" name "System monitor"
  gsettings set $BASE.custom-keybinding:"$slot" command "$CMD"
  gsettings set $BASE.custom-keybinding:"$slot" binding "$SHORTCUT"
  echo "GNOME shortcut $SHORTCUT -> $CMD"
else
  echo "not a GNOME session - bind a key yourself to: $CMD  (see docs/ADOPTING.md for other terminals)"
fi

echo
bash "$SRC/doctor.sh" || true
echo
echo "run: $DEST/temp_monitor.sh    (q quits + summary, r resets stats)"
