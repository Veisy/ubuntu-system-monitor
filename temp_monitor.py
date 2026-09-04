#!/usr/bin/env python3
"""
SYSTEM TEMPERATURE MONITOR
==========================
Curses dashboard for CPU / GPU / NVMe / SATA temperatures, power draw, load
and GPU VRAM.  Pwr / Load / VRAM are gauges: the value is centred on a bar
filled to its share of full scale (power / power limit, utilisation, VRAM
used / total).  For drives, "load" is the share of capacity in use (mounted
filesystems or the ZFS pool they belong to).

Hardware is discovered every round from /sys, nvidia-smi and lsblk - nothing
is hardcoded: new disks or GPUs appear, swapped hardware re-labels its row
and resets its statistics, removed hardware goes [stale].  Every frame is
re-laid out for the current terminal size.

Usage:
    temp_monitor.py [interval_seconds]     (default 1.0, minimum 0.1)
    temp_monitor.py --geometry [interval]  print COLSxROWS this machine needs

Keys:
    q / Esc / Ctrl+C    quit and print a summary
    r                   reset statistics

Debug: TEMP_MONITOR_DEBUG_LOG=/path/file logs each round; SIGUSR1 dumps a
stack trace there.
"""

import collections
import curses
import faulthandler
import functools
import glob
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time

# --- thresholds & layout --------------------------------------------------------
COOL_MAX, OK_MAX, WARM_MAX = 50, 70, 90
STALE_AFTER = 3              # consecutive failed reads before [stale] shows

MIN_ROWS = 8                 # below this a "too small" notice is drawn (width: min_cols)
HEADER_ROWS = 6              # box, column headers and rule: the first row a component may use
PAD, GAP = 2, 3              # GAP leaves room for ' │ ' between columns
LABEL_W, MIN_LABEL_W = 36, 8
TEMP_W, AVG_W, MAX_W, STATUS_W = 5, 4, 4, 6
GAUGE_PAD = 2                # gauge margin when there is room: one column either side
# Every gauge reserves what its formatter can produce at most, never what the
# current reading happens to be, so a value gaining a digit cannot re-widen the
# table.  fmt_pwr / fmt_load / fmt_vram each guarantee their own width.
PWR_W, LOAD_W, VRAM_W = 6, 4, 9
GAUGE_CAP = {"pwr": PWR_W, "load": LOAD_W, "vram": VRAM_W}
# Counter allowance for the meta line's width: five-digit sample, 99-hour uptime.
META_SAMPLE, META_ELAPSED = 99999, 99 * 3600

CPU_HWMON_NAMES = ("k10temp", "zenpower", "coretemp", "x86_pkg_temp", "cpu_thermal")
NVIDIA_QUERY = ["nvidia-smi", "--format=csv,noheader",
                "--query-gpu=index,name,temperature.gpu,utilization.gpu,power.draw,"
                "power.limit,memory.used,memory.total"]

# --- debug log (optional) --------------------------------------------------------
_DBG_LOG = os.environ.get("TEMP_MONITOR_DEBUG_LOG")


def _dlog(msg):
    if _DBG_LOG:
        try:
            with open(_DBG_LOG, "a") as f:
                f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
        except OSError:
            pass


def _install_stack_dump():
    def dump(signum, frame):
        try:
            with open(_DBG_LOG, "a") as f:
                f.write(f"{time.strftime('%H:%M:%S')} SIGUSR1 stack:\n")
                faulthandler.dump_traceback(file=f)
        except OSError:
            pass
    if _DBG_LOG:
        signal.signal(signal.SIGUSR1, dump)

# --- sysfs helpers ----------------------------------------------------------------


def _read_str(path):
    try:
        with open(path) as f:
            return f.read().strip() or None
    except OSError:
        return None


def _read_int(path):
    try:
        return int(_read_str(path))
    except (TypeError, ValueError):
        return None


def hwmon_dirs(names):
    return [d for d in sorted(glob.glob("/sys/class/hwmon/hwmon*"))
            if _read_str(os.path.join(d, "name")) in names]

# --- collectors -------------------------------------------------------------------

# hwmon temperature reads run in a short-lived child (SensorReader): drivetemp
# and NVMe log pages issue real block I/O and can block uninterruptibly
# (D state) when device firmware stalls - common with QLC drives during
# reclamation.  Signals cannot wake a D-state read, so it happens somewhere
# we can abandon.
_SENSOR_HELPER = r"""
import glob, json, os

def _ri(p):
    try:
        with open(p) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None

out = {"cpu": None, "nvme": {}, "sata": {}}
for d in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
    try:
        with open(os.path.join(d, "name")) as f:
            name = f.read().strip()
    except OSError:
        continue
    if name in __CPU_HWMON_NAMES__:
        if out["cpu"] is None:
            v = _ri(os.path.join(d, "temp1_input"))
            if v is not None:
                out["cpu"] = v // 1000
    elif name == "drivetemp":
        try:
            blk = sorted(os.listdir(os.path.join(d, "device", "block")))[0]
        except (OSError, IndexError):
            continue
        v = _ri(os.path.join(d, "temp1_input"))
        if v is not None:
            out["sata"][blk] = v // 1000
for d in sorted(glob.glob("/sys/class/nvme/nvme*")):
    for hw in sorted(glob.glob(os.path.join(d, "hwmon*"))):
        v = _ri(os.path.join(hw, "temp1_input"))
        if v is not None:
            out["nvme"][os.path.basename(d)] = v // 1000
            break
print(json.dumps(out))
""".replace("__CPU_HWMON_NAMES__", repr(CPU_HWMON_NAMES))


class SensorReader:
    """hwmon temperatures via a child process; returns
    {"cpu": C|None, "nvme": {dev: C}, "sata": {blk: C}} or None this round.

    A child still blocked in an uninterruptible read is kept and reaped on a
    later round; while it lives no new child is spawned, so at most one stuck
    helper can ever exist."""

    def __init__(self, timeout=2.0):
        self.timeout = timeout
        self._stuck = None

    def read(self):
        p = self._stuck
        if p is None:
            try:
                p = subprocess.Popen([sys.executable, "-c", _SENSOR_HELPER],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True)
            except OSError:
                return None
        try:
            out, _ = p.communicate(timeout=0.1 if self._stuck else self.timeout)
        except subprocess.TimeoutExpired:
            self._stuck = p  # kill only takes effect once the I/O completes
            try:
                p.kill()
            except OSError:
                pass
            return None
        self._stuck = None
        try:
            return json.loads(out) if p.returncode == 0 else None
        except ValueError:
            return None


class CpuPower:
    """Package power in watts, or None when no sensor is readable.

    Prefers an hwmon power reading; otherwise differentiates the RAPL energy
    counter between rounds (energy_uj is root-only by default - see
    root_setup.sh next to this script)."""

    def __init__(self):
        self._prev = None  # (monotonic seconds, energy_uj)

    def read(self):
        for d in hwmon_dirs(CPU_HWMON_NAMES):
            v = _read_int(os.path.join(d, "power1_input"))  # micro-watts
            if v is not None:
                return v / 1e6
        for d in sorted(glob.glob("/sys/class/powercap/*")):
            if not (_read_str(os.path.join(d, "name")) or "").startswith("package"):
                continue
            e = _read_int(os.path.join(d, "energy_uj"))
            if e is None:
                return None
            now = time.monotonic()
            prev, self._prev = self._prev, (now, e)
            if prev is None or now <= prev[0]:
                return None
            de = e - prev[1]
            if de < 0:  # counter wrapped
                de += _read_int(os.path.join(d, "max_energy_range_uj")) or 0
            return de / (now - prev[0]) / 1e6
        return None


def _run(cmd, timeout):
    """stdout of a finished command, or None when it is missing or fails."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def zpool_usage():
    """{pool: (alloc_bytes, size_bytes)}; {} without ZFS."""
    out = {}
    for line in (_run(["zpool", "list", "-Hp", "-o", "name,alloc,size"], 2) or "").splitlines():
        try:
            name, alloc, size = line.split()
            out[name] = (int(alloc), int(size))
        except ValueError:
            continue
    return out


def collect_disk_usage():
    """{block_device: used_pct} over every mounted filesystem or ZFS pool the
    device carries; devices with nothing mounted are absent."""
    try:
        disks = json.loads(_run(["lsblk", "-J", "-o", "NAME,TYPE,FSTYPE,LABEL,MOUNTPOINTS"], 2)
                           or "")["blockdevices"]
    except (ValueError, KeyError, TypeError):
        return {}
    pools = zpool_usage()
    out = {}
    for disk in disks:
        if disk.get("type") != "disk":  # skips loop devices etc.
            continue
        used = total = 0
        nodes = [disk]
        while nodes:
            n = nodes.pop()
            nodes += n.get("children") or []
            if n.get("fstype") == "zfs_member" and n.get("label") in pools:
                a, sz = pools[n["label"]]
                used, total = used + a, total + sz
            for mp in n.get("mountpoints") or []:
                if not mp or mp.startswith("["):  # [SWAP]
                    continue
                try:
                    st = os.statvfs(mp)
                except OSError:
                    continue
                total += st.f_blocks * st.f_frsize
                used += (st.f_blocks - st.f_bfree) * st.f_frsize
        if total:
            out[disk["name"]] = round(100 * used / total)
    return out


def read_cpu_jiffies():
    """(total, idle) jiffies of the aggregate 'cpu' line, or None."""
    try:
        with open("/proc/stat") as f:
            vals = [int(x) for x in f.readline().split()[1:]]
        return sum(vals), vals[3] + vals[4]  # idle + iowait
    except (OSError, ValueError, IndexError):
        return None


@functools.lru_cache(maxsize=None)
def cpu_model_name():
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.lower().startswith("model name"):
                    m = line.split(":", 1)[1].strip()
                    m = re.sub(r"^(AMD|Intel|GenuineIntel)\s+", "", m)
                    return re.sub(r"\s+\d+\s*-\s*Cores?\s*$", "", m) or "CPU"
    except OSError:
        pass
    return "CPU"


def _num(field):
    """Leading number of an nvidia-smi field, or None for '[N/A]' and friends."""
    try:
        return float(field.split()[0])
    except (ValueError, IndexError):
        return None


def collect_gpus():
    """{idx: (name, temp_c, power_w, power_limit_w, util_pct, (used_mib, total_mib)|None)}."""
    out = {}
    for line in (_run(NVIDIA_QUERY, 5) or "").splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) < 8:
            continue
        try:
            idx = int(parts[0])
        except ValueError:
            continue
        temp, util = _num(parts[2]), _num(parts[3])
        used, total = _num(parts[6]), _num(parts[7])
        out[idx] = (parts[1], None if temp is None else int(temp),
                    _num(parts[4]), _num(parts[5]),
                    None if util is None else int(util),
                    (used, total) if used is not None and total else None)
    return out


def nvme_label(dev):
    m = _read_str(os.path.join("/sys/class/nvme", dev, "model"))
    return f"{dev} · {m}" if m else dev


def drive_capacity(blk):
    """lsblk-style capacity from /sys/block/<blk>/size, or None."""
    n = _read_int(os.path.join("/sys/block", blk, "size"))  # 512-byte sectors
    if n is None:
        return None
    b = n * 512.0
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if b < 999.5 or unit == "PB":
            break
        b /= 1000.0
    v = f"{b:.0f}" if unit == "B" or b >= 100 else f"{b:.1f}".rstrip("0").rstrip(".")
    return f"{v}{unit}"


def sata_label(blk):
    m = _read_str(os.path.join("/sys/block", blk, "device", "model"))
    return " · ".join(x for x in (blk, m, drive_capacity(blk)) if x)


def gpu_label(idx, name):
    label = f"GPU {idx} · {name}"
    if len(label) > LABEL_W and name.startswith("NVIDIA "):
        label = f"GPU {idx} · {name[7:]}"
    return label

# --- state ------------------------------------------------------------------------


class Component:
    __slots__ = ("key", "label", "temp", "pwr", "pwr_max", "util", "vram",
                 "samples", "total", "max", "last", "misses", "stale")

    def __init__(self, key, label):
        self.key, self.label = key, label
        self.reset()

    def reset(self):
        self.temp = self.pwr = self.pwr_max = self.util = self.vram = None
        self.max = self.last = None
        self.samples = self.total = self.misses = 0
        self.stale = False

    def observe(self, temp, pwr=None, util=None, vram=None, pwr_max=None):
        self._seen(temp is not None)
        self.pwr, self.pwr_max, self.util, self.vram = pwr, pwr_max, util, vram
        if temp is None:
            return
        self.temp = self.last = temp
        self.samples += 1
        self.total += temp
        self.max = temp if self.max is None else max(self.max, temp)

    def miss(self):
        self._seen(False)

    def _seen(self, ok):
        # [stale] only after STALE_AFTER consecutive misses - one transient
        # EIO must not flash the tag
        self.misses = 0 if ok else self.misses + 1
        self.stale = self.misses >= STALE_AFTER and self.last is not None

    @property
    def avg(self):
        return self.total // self.samples if self.samples else None


class State:
    def __init__(self):
        self.comps = {}
        self.order = []
        self.reset_stats()

    def ensure(self, key, label):
        c = self.comps.get(key)
        if c is None:
            c = self.comps[key] = Component(key, label)
            self.order.append(key)
        elif c.label != label:
            # same slot, different hardware (e.g. SSD swap): start fresh
            c.label = label
            c.reset()
        return c

    def sync_group(self, prefix, rows):
        """rows = {suffix: (label, observe args)}; group members not in rows
        register a miss."""
        for suffix, (label, args) in sorted(rows.items()):
            self.ensure(prefix + suffix, label).observe(*args)
        for c in self.comps.values():
            if c.key.startswith(prefix) and c.key[len(prefix):] not in rows:
                c.miss()

    def reset_stats(self):
        for c in self.comps.values():
            c.reset()
        self.sample = 0
        self.start = time.time()
        self.cpu_jiffies = None  # (total, idle) for utilisation deltas


def collect_round(state, reader, cpu_power):
    s = reader.read() or {"cpu": None, "nvme": {}, "sata": {}}
    usage = collect_disk_usage()

    cpu_util = None
    j = read_cpu_jiffies()
    if j is not None:
        if state.cpu_jiffies is not None:
            dt, di = j[0] - state.cpu_jiffies[0], j[1] - state.cpu_jiffies[1]
            if dt > 0:
                cpu_util = round((1.0 - di / dt) * 100)
        state.cpu_jiffies = j
    state.ensure("cpu", f"CPU · {cpu_model_name()}").observe(
        s["cpu"], cpu_power.read(), cpu_util)

    state.sync_group("gpu", {
        str(i): (gpu_label(i, name), (temp, pwr, util, vram, pmax))
        for i, (name, temp, pwr, pmax, util, vram) in collect_gpus().items()})
    # disk usage is per block device (nvme0n1); the sensor is per controller
    state.sync_group("nvme-", {
        dev: (nvme_label(dev), (t, None, next(
            (v for k, v in usage.items() if k.startswith(dev + "n")), None)))
        for dev, t in s["nvme"].items()})
    state.sync_group("sata-", {
        blk: (sata_label(blk), (t, None, usage.get(blk)))
        for blk, t in s["sata"].items()})

# --- rendering --------------------------------------------------------------------


def fmt_dur(s):
    h, r = divmod(int(max(0, s)), 3600)
    m, sec = divmod(r, 60)
    return f"{h}h {m:02d}m {sec:02d}s" if h else f"{m}m {sec:02d}s"


def finite(v):
    """True when v is a real, finite number.  A sensor that hands back inf, nan
    or an absurd integer must not reach a format string: it would raise inside
    a draw and take the frame down."""
    try:
        return math.isfinite(float(v))
    except (TypeError, ValueError, OverflowError):
        return False


def frac_of(value, full):
    """value's share of full scale in 0..1, or None when there is no usable
    scale - a gauge with no scale is drawn as plain text."""
    if not full or not finite(value) or not finite(full):
        return None
    return max(0.0, min(1.0, float(value) / float(full)))


def fmt_pwr(watts):
    """Watts in at most PWR_W columns: '19.6W', '999.9W', '1234W', '123kW'.
    Bounded, because the gauge reserves PWR_W for it."""
    if not finite(watts):
        return None
    watts = float(watts)
    for cell in (f"{watts:.1f}W", f"{watts:.0f}W",
                 f"{watts / 1000:.1f}kW", f"{watts / 1000:.0f}kW"):
        if len(cell) <= PWR_W:
            return cell
    return None                   # off any real scale: no reading, not digits


def fmt_load(util):
    """Utilisation as '%0'..'%100' - clamped, so LOAD_W is a real ceiling."""
    if not finite(util):
        return None
    return f"%{max(0, min(100, int(round(float(util)))))}"


def fmt_vram(vram):
    """(used_mib, total_mib) -> '20.6/24GB'; precision, then the unit, then a
    bare percentage drop so the text never exceeds VRAM_W - a 2 TB card would
    otherwise overflow the width the gauge reserved."""
    if not vram or not all(finite(v) for v in vram):
        return "--"
    used, total = (float(v) / 1024.0 for v in vram)
    pct = 100 * used / total if total else 0
    for cell in (f"{used:.1f}/{total:.0f}GB", f"{used:.0f}/{total:.0f}GB",
                 f"{used:.0f}/{total:.0f}G", f"{used / 1024:.1f}/{total / 1024:.1f}T",
                 f"%{pct:.0f}"):
        if len(cell) <= VRAM_W:
            return cell
    return "--"                   # nothing bounded fits: show no reading


def status_for(temp):
    if temp is None:
        return "N/A"
    return ("Cool" if temp < COOL_MAX else "OK" if temp < OK_MAX
            else "Warm" if temp < WARM_MAX else "HOT!")


STATUS_KEY = {"Cool": "cool", "OK": "ok", "Warm": "warm", "HOT!": "hot"}
STATUS_FG = {"cool": curses.COLOR_GREEN, "ok": curses.COLOR_CYAN,
             "warm": curses.COLOR_YELLOW, "hot": curses.COLOR_RED}
COLORS = {}


def setup_colors():
    if not curses.has_colors():
        return
    try:
        curses.start_color()
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    track = 240 if curses.COLORS >= 256 else curses.COLOR_WHITE  # gauge track
    pairs = [("title", curses.COLOR_WHITE, bg), ("track_bar", bg, track),
             ("track_inv", curses.COLOR_BLACK, track)]
    for k, fg in STATUS_FG.items():
        pairs += [(k, fg, bg),                                  # text
                  (f"{k}_bg", curses.COLOR_BLACK if k != "hot" else curses.COLOR_WHITE, fg),
                  (f"{k}_bar", fg, track),                      # gauge fill / text on track
                  (f"{k}_inv", curses.COLOR_BLACK, fg)]         # text over the fill
    for i, (k, fg, bgc) in enumerate(pairs, start=1):
        curses.init_pair(i, fg, bgc)
        COLORS[k] = i


def _pair(key, fallback):
    return curses.color_pair(COLORS[key]) if key in COLORS else fallback


def temp_attr(temp):
    if temp is None:
        return curses.A_DIM
    return curses.A_BOLD | _pair(STATUS_KEY[status_for(temp)], 0)


def bar_attr(temp, inv=False):
    """Gauge attribute: temperature colour on the grey track, or (inv) black
    on the temperature colour for characters over the fill."""
    key = "track" if temp is None else STATUS_KEY[status_for(temp)]
    return _pair(f"{key}_{'inv' if inv else 'bar'}", curses.A_REVERSE)


def gauge(stdscr, y, x, w, frac, text, temp):
    """w-wide bar filled to frac (0..1) with text centred over it.  Eighth-
    block resolution; any non-zero frac shows a sliver, only frac >= 1
    reaches the end; characters over the fill are inverted."""
    steps = w * 8
    units = (0 if not frac else steps if frac >= 1
             else min(steps - 1, max(1, round(frac * steps))))
    full, part = divmod(units, 8)
    fill = bar_attr(temp)
    for i, ch in enumerate(text.center(w)[:w]):
        if ch != " ":
            covered = i < full or (i == full and part >= 4)
            put(stdscr, y, x + i, ch, bar_attr(temp, inv=True) if covered else fill)
        elif i < full:
            put(stdscr, y, x + i, "█", fill)
        elif i == full and part:
            put(stdscr, y, x + i, " ▏▎▍▌▋▊▉"[part], fill)
        else:
            put(stdscr, y, x + i, " ", bar_attr(None))


def trunc_tail(s, n):
    """Truncate to n columns keeping the tail; for 'head · rest' labels keep
    the head + '…' + as many trailing words as fit, so 'GPU 0' / 'sda' stay
    identifiable in a narrow column."""
    if len(s) <= n:
        return s
    if n < 2:
        return s[:n]
    head, sep, rest = s.partition(" · ")
    if sep and len(head) + 2 <= n:
        keep = []
        for w in reversed(rest.split()):
            if len(" ".join([w] + keep)) > n - len(head) - 1:
                break
            keep.insert(0, w)
        if keep:
            return head + "…" + " ".join(keep)
    return "…" + s[-(n - 1):]


Layout = collections.namedtuple("Layout", "lw off right gw")


def gauge_cells(c):
    """(col, frac, text) for a row's gauges; text None = no reading, frac
    None = no full scale (text only)."""
    return [
        ("pwr", frac_of(c.pwr, c.pwr_max), fmt_pwr(c.pwr)),
        ("load", frac_of(c.util, 100), fmt_load(c.util)),
        ("vram", frac_of(c.vram[0], c.vram[1]) if c.vram else None,
         fmt_vram(c.vram) if c.vram else None),
    ]


def gauge_text_w(vram):
    """Narrowest gauge that can still show any text its column may produce:
    the widest reserved capacity among the visible gauges.  Derived from the
    formatters, never from a sample, so columns hold still while values move.
    vram=False leaves out the VRAM column, whose texts are the widest."""
    cols = ("pwr", "load", "vram") if vram else ("pwr", "load")
    return max(GAUGE_CAP[c] for c in cols)


def has_vram(state):
    """Whether a VRAM column is offered at all: true when this machine has a
    GPU, not when a sample happened to read VRAM - a capability, so a driver
    hiccup cannot make the column appear and vanish."""
    return any(cat(k) == "gpu" for k in state.order)


def _layout(lw, vram, gw, stats=False):
    x, off = PAD, {}
    for name, w in (("label", lw), ("temp", TEMP_W), ("pwr", gw),
                    ("load", gw), ("vram", gw if vram else 0),
                    ("avg", AVG_W if stats else 0), ("max", MAX_W if stats else 0),
                    ("status", STATUS_W)):
        if w:
            off[name] = x
            x += w + GAP
    return Layout(lw, off, x - GAP, gw)


def min_cols(state=None):
    """Narrowest terminal that fits the narrowest layout: shortest label, every
    column this machine offers (VRAM whenever it has a GPU), every gauge at the
    width its formatters reserve.  Constant for given hardware, so the "too
    small" threshold cannot move while values move."""
    vram = has_vram(state) if state is not None else False
    return _layout(MIN_LABEL_W, vram, gauge_text_w(vram)).right + PAD


def compute_layout(iw, state):
    """Column geometry for inner width iw.  Order: label, temp, pwr, load,
    [vram], [avg, max], status.  VRAM is present exactly when the machine has
    a GPU and is never dropped.  avg/max are extras: they appear only once the
    terminal is wide enough for the full label AND every gauge at its full
    margin AND the two stat columns, and never at the opening width (the
    legend row's, which the window is sized to) - widening the terminal adds
    them on every machine, GPU or not.  Every
    gauge is a bar and all of them share one width, which is the background of
    the value it shows: gauges never narrow past the width their formatters
    reserve, and any spare width goes to their side margins (up to GAUGE_PAD)
    before it is left unused.  When even bare-text gauges do not fit, the
    label shrinks towards MIN_LABEL_W."""
    def spare(lw, vram, gw, stats=False):
        return iw - PAD - _layout(lw, vram, gw, stats).right

    vram = has_vram(state)
    tw = gauge_text_w(vram)
    if iw > legend_cols() and spare(LABEL_W, vram, tw + GAUGE_PAD, True) >= 0:
        return _layout(LABEL_W, vram, tw + GAUGE_PAD, True)   # wider than opening
    room = spare(LABEL_W, vram, tw)
    if room >= 0:
        gauges = 3 if vram else 2
        return _layout(LABEL_W, vram, tw + min(GAUGE_PAD, room // gauges))
    lw = MIN_LABEL_W + spare(MIN_LABEL_W, vram, tw)
    return _layout(min(LABEL_W, max(MIN_LABEL_W, lw)), vram, tw)


TITLE = "SYSTEM TEMPERATURE MONITOR"
LEGEND_LABEL = "Legend:"
LEGEND_HINT = "[q] quit + summary    [r] reset stats"
LEGEND = (("Cool <50C", "cool"), ("OK 50–69C", "ok"),
          ("Warm 70–89C", "warm"), ("HOT ≥90C", "hot"))


def legend_layout():
    """([(x, text, colour_key)], hint_x): where the legend row's cells go.  The
    renderer and the width calculation both read it, so neither can drift."""
    x, cells = PAD + len(LEGEND_LABEL) + 2, []
    for text, key in LEGEND:
        cells.append((x, text, key))
        x += len(text) + 2
    return cells, x + 2


def legend_cols():
    """Columns the legend row needs.  It carries fixed strings, so unlike the
    table it cannot shrink - which makes it a floor for the window width."""
    _, hint_x = legend_layout()
    return hint_x + len(LEGEND_HINT)


def meta_text(stamp, sample, elapsed, interval):
    return (f"{stamp}  ·  sample {sample}  ·  running {fmt_dur(elapsed)}"
            f"  ·  refresh {interval:g}s")


def meta_cols(interval):
    """Columns the header's meta line needs once the counters have grown."""
    return PAD + len(meta_text(time.strftime("%Y-%m-%d %H:%M:%S"), META_SAMPLE,
                               META_ELAPSED, interval)) + PAD


def group_rules(keys):
    """Rules drawn between hardware groups: one per change of group."""
    cats = [cat(k) for k in keys]
    return sum(1 for a, b in zip(cats, cats[1:]) if a != b)


def pci_gpu_count():
    """NVIDIA display devices the kernel sees in sysfs.  Read instead of
    trusting one nvidia-smi round: the driver can be busy or briefly
    unavailable, and a window sized during that outage would be short by a row
    per GPU once the rows come back."""
    n = 0
    for dev in glob.glob("/sys/bus/pci/devices/*"):
        if _read_str(f"{dev}/vendor") == "0x10de" and \
                (_read_str(f"{dev}/class") or "")[:6] in ("0x0300", "0x0302"):
            n += 1
    return n


def geometry_keys(state):
    """The rows a window must plan for: what this sample found, plus a row for
    every GPU sysfs knows about that the sample missed, so the height is a
    property of the hardware rather than of one round's luck."""
    keys = list(state.order)
    missing = pci_gpu_count() - sum(1 for k in keys if cat(k) == "gpu")
    if missing > 0:
        gpus = [k for k in keys if cat(k) == "gpu"]
        at = keys.index(gpus[-1]) + 1 if gpus else len(keys)
        keys[at:at] = [f"gpu-absent{i}" for i in range(missing)]
    return keys


def ideal_geometry(state, interval):
    """(cols, rows) for a window that shows everything this machine reports.
    Width is the widest line that cannot shrink - the legend, the meta line, or
    the narrowest table - so the table adapts inside it instead of the window
    growing to the table's full width.  Height covers the header, every sensor
    row, its group rules, the closing rule, the legend, and the bottom row
    put() keeps free."""
    keys = geometry_keys(state)
    cols = max(legend_cols(), meta_cols(interval), min_cols(state), len(TITLE) + 2 * PAD)
    return cols, max(HEADER_ROWS + len(keys) + group_rules(keys) + 3, MIN_ROWS)


def put(stdscr, y, x, text, attr=0):
    H, W = stdscr.getmaxyx()
    if text and 0 <= y < H - 1 and x < W:
        try:
            stdscr.addnstr(y, x, text, W - x, attr)
        except curses.error:
            pass


def cat(key):
    """Hardware group of a component key (for group separators)."""
    return key.split("-")[0] if "-" in key else ("gpu" if key.startswith("gpu") else key)


def draw_seps(stdscr, y, L):
    for col, x in L.off.items():
        if col not in ("label", "temp"):
            put(stdscr, y, x - 2, "│", curses.A_DIM)


def render_row(stdscr, y, c, L):
    o = L.off
    draw_seps(stdscr, y, L)
    label = c.label + ("  [stale]" if c.stale and len(c.label) + 9 <= L.lw else "")
    put(stdscr, y, o["label"], trunc_tail(label, L.lw), curses.A_DIM if c.stale else 0)
    a = temp_attr(c.temp)
    put(stdscr, y, o["temp"], f"{c.temp:3d}C" if c.temp is not None else "   --", a)
    for col, frac, text in gauge_cells(c):
        if col not in o:
            continue
        if text is None:
            put(stdscr, y, o[col], "--".center(L.gw), curses.A_DIM)
        elif frac is None:
            put(stdscr, y, o[col], text.center(L.gw), curses.A_DIM)  # no scale: text only
        else:
            gauge(stdscr, y, o[col], L.gw, frac, text, c.temp)
    for col, v in (("avg", c.avg), ("max", c.max)):
        if col in o:
            put(stdscr, y, o[col], f"{v:3d}" if v is not None else "  --", curses.A_DIM)
    put(stdscr, y, o["status"], status_for(c.temp), a)


def draw(stdscr, state, interval):
    stdscr.erase()
    H, W = stdscr.getmaxyx()
    if W < min_cols(state) or H < MIN_ROWS:
        y = max(1, H // 2 - 1)
        put(stdscr, y, 0, f"Terminal too small: {W}x{H}  "
            f"(need at least {min_cols(state)}x{MIN_ROWS})", curses.A_BOLD)
        put(stdscr, y + 1, 0, "Resize the terminal window to continue.")
        return

    # header box
    put(stdscr, 0, 0, "┌" + "─" * (W - 2) + "┐")
    for y in (1, 2):
        put(stdscr, y, 0, "│")
        put(stdscr, y, W - 1, "│")
    put(stdscr, 1, PAD, TITLE, curses.A_BOLD | _pair("title", 0))
    put(stdscr, 2, PAD, meta_text(time.strftime("%Y-%m-%d %H:%M:%S"), state.sample,
                                  time.time() - state.start, interval), curses.A_DIM)
    put(stdscr, 3, 0, "└" + "─" * (W - 2) + "┘")

    # column headers, then a rule
    L = compute_layout(W, state)
    o, right = L.off, L.right
    for col, hdr, w in (("label", "Component", 0), ("temp", "Temp", 0),
                        ("pwr", "Pwr", L.gw), ("load", "Load", L.gw),
                        ("vram", "VRAM", L.gw), ("avg", "avg", -AVG_W),
                        ("max", "max", -MAX_W), ("status", "Status", 0)):
        if col in o:
            put(stdscr, 4, o[col], hdr.center(w) if w > 0 else hdr.rjust(-w), curses.A_DIM)
    draw_seps(stdscr, 4, L)
    put(stdscr, 5, PAD, "─" * (right - PAD), curses.A_DIM)

    def rule(y):
        put(stdscr, y, PAD, "─" * (right - PAD), curses.A_DIM)

    y, prev = HEADER_ROWS, None
    for shown, key in enumerate(state.order):
        c = state.comps[key]
        if prev is not None and cat(key) != prev:  # rule between hardware groups
            if y > H - 3:
                break
            rule(y)
            y += 1
        if y > H - 3:
            break
        render_row(stdscr, y, c, L)
        y += 1
        prev = cat(key)
    else:
        if y + 1 < H - 1:  # rule + legend only when both fit
            rule(y)
            cells, hint_x = legend_layout()
            put(stdscr, y + 1, PAD, LEGEND_LABEL, curses.A_DIM)
            for x, text, key in cells:
                put(stdscr, y + 1, x, text, _pair(f"{key}_bg", 0))
            put(stdscr, y + 1, hint_x, LEGEND_HINT, curses.A_DIM)
        return
    put(stdscr, min(y, H - 2), PAD,
        f"... {len(state.order) - shown} more row(s) — enlarge the terminal", curses.A_DIM)


def request_fit():
    """Request the launcher-computed size when the terminal opened differently."""
    m = re.fullmatch(r"(\d+)x(\d+)", os.environ.get("TEMP_MONITOR_FIT", ""))
    if not m:
        return
    cols, rows = int(m.group(1)), int(m.group(2))
    s = shutil.get_terminal_size()
    _dlog(f"fit request {cols}x{rows}, terminal is {s.columns}x{s.lines}")
    if (s.columns, s.lines) != (cols, rows):
        sys.__stdout__.write(f"\033[8;{rows};{cols}t")
        sys.__stdout__.flush()


class TermSync:
    """Call resizeterm() only when the terminal size actually changed: every
    call arms a KEY_RESIZE, so doing it each round would starve real keys;
    never doing it leaves stdscr at the old size and frames garble."""

    def __init__(self):
        s = shutil.get_terminal_size()
        self._last = (s.lines, s.columns)

    def sync(self):
        s = shutil.get_terminal_size()
        if (s.lines, s.columns) != self._last:
            self._last = (s.lines, s.columns)
            try:
                curses.resizeterm(s.lines, s.columns)
            except curses.error:
                pass

# --- main loop --------------------------------------------------------------------


def run(stdscr, state, interval):
    _install_stack_dump()
    curses.noecho()
    curses.cbreak()
    setup_colors()
    stdscr.keypad(True)
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    # No SIGWINCH handler of our own: ncurses flags the resize and wakes
    # getch() with KEY_RESIZE; TermSync then resizes stdscr next round.
    reader, cpu_power, term = SensorReader(), CpuPower(), TermSync()
    request_fit()
    while True:
        term.sync()
        t0 = time.monotonic()
        collect_round(state, reader, cpu_power)
        state.sample += 1
        _dlog(f"round {state.sample} collected in {time.monotonic() - t0:.2f}s")
        draw(stdscr, state, interval)
        try:
            stdscr.refresh()
        except curses.error:
            return
        stdscr.timeout(int(max(0.05, interval - (time.monotonic() - t0)) * 1000))
        ch = stdscr.getch()
        if ch in (ord("q"), ord("Q"), 27, 3):
            return
        if ch in (ord("r"), ord("R")):
            state.reset_stats()

# --- exit summary -----------------------------------------------------------------


def print_summary(state, interval):
    cols = shutil.get_terminal_size((80, 24)).columns
    table = [
        ("Current", 7, lambda c: f"{c.last}C" if c.last is not None else "n/a"),
        ("Pwr", 6, lambda c: f"{c.pwr:5.1f}W" if c.pwr is not None else "--"),
        ("VRAM", 9, lambda c: fmt_vram(c.vram)),
        ("Average", 7, lambda c: f"{c.avg}C" if c.avg is not None else "n/a"),
        ("Maximum", 7, lambda c: f"{c.max}C" if c.max is not None else "n/a"),
        ("Status", 6, lambda c: status_for(c.last)),
    ]
    for drop in ("VRAM", "Pwr", "Average", "Maximum"):  # shed columns before the label
        if cols - sum(w + 2 for _, w, _ in table) >= MIN_LABEL_W:
            break
        table = [col for col in table if col[0] != drop]
    fixed = sum(w + 2 for _, w, _ in table)
    lw = min(LABEL_W, max(MIN_LABEL_W, cols - fixed))
    width = lw + fixed
    print("=" * width, "MONITORING SUMMARY".center(width), "=" * width, sep="\n")
    print(f"{'Component':<{lw}}" + "".join(f"  {h:>{w}}" for h, w, _ in table))
    print("-" * width)
    for key in state.order:
        c = state.comps[key]
        print(f"{trunc_tail(c.label, lw):<{lw}}" + "".join(f"  {get(c):>{w}}" for _, w, get in table))
    print("-" * width)
    if state.sample:
        print(f"Duration: {fmt_dur(time.time() - state.start)}  "
              f"samples: {state.sample}  interval: {interval:g}s")
    else:
        print("No samples collected.")
    for key in state.order:
        c = state.comps[key]
        if c.max is not None and c.max >= WARM_MAX:
            print(f"[!] {trunc_tail(c.label, lw)} reached {c.max}C (>= {WARM_MAX}C)")
    print()


def report_geometry(interval):
    """Print the terminal size this machine's sensors need, for a launcher to
    open a fitted window.  One sensor round is enough: GPUs missing from it are
    still counted from sysfs (see geometry_keys)."""
    state = State()
    collect_round(state, SensorReader(), CpuPower())
    cols, rows = ideal_geometry(state, interval)
    print(f"{cols}x{rows}")
    return 0


def main():
    interval = 1.0
    geometry = False
    for a in sys.argv[1:]:
        if a in ("-h", "--help"):
            print(__doc__.strip())
            return 0
        if a == "--geometry":
            geometry = True
            continue
        try:
            interval = float(a)
            if not math.isfinite(interval):
                raise ValueError
        except ValueError:
            print(f"temp_monitor: invalid interval: {a!r}", file=sys.stderr)
            return 2
        interval = max(0.1, interval)
    if geometry:                      # no curses, no TTY: a launcher asks this
        return report_geometry(interval)
    if not (sys.stdout.isatty() and sys.stdin.isatty()):
        print("temp_monitor: requires an interactive terminal (TTY)", file=sys.stderr)
        return 2

    state = State()
    try:
        curses.wrapper(run, state, interval)
    except KeyboardInterrupt:
        pass
    except curses.error as e:
        print(f"temp_monitor: terminal error: {e}", file=sys.stderr)
        return 2
    sys.stdout.write("\033[H\033[2J")
    print_summary(state, interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
