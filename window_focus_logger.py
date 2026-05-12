#!/usr/bin/env python3
"""window-focus-logger — focused-window + AFK tracking, ActivityWatch-style.

Two JSONL output streams in $WFL_LOG_DIR:
- window_focus.log: focused-window events with start/end/duration_s.
  Coalesced by (window_id, normalized_title, url) — a single sustained
  focus produces one row. Long sessions are split at MAX_EVENT_SECONDS
  with contiguous boundaries (no gaps).
- afk.log: AFK ↔ active intervals with start/end/duration_s. "active"
  ends at the last input event (not "now") so AFK begins exactly when
  the user stopped typing/moving, mirroring aw-watcher-afk.

Captured per window event:
  wm_class, wm_class_instance, title, pid, window_id, workspace,
  and for Chromium-class windows: url, tab_title, tab_count, cdp_port
  (active tab matched against CDP /json by title; cached WFL_CDP_CACHE_TTL).

AFK detection reads any keyboard / mouse / abs-input evdev device. Needs
the user in the `input` group (same requirement as key-logger).

Timing uses time.monotonic() for AFK timeout / duration math so wall-clock
jumps (NTP, suspend/resume) don't produce phantom intervals. Wall-clock
time.time() is used only for emitted ISO timestamps.
"""

import ast
import atexit
import json
import logging
import os
import re
import selectors
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from urllib.error import URLError
from urllib.request import urlopen


LOG_DIR = os.environ.get('WFL_LOG_DIR', '/home/lst/Storage/Self/!raw-Keylog')
WINDOW_LOG = os.path.join(LOG_DIR, 'window_focus.log')
AFK_LOG = os.path.join(LOG_DIR, 'afk.log')
POLL_INTERVAL = float(os.environ.get('WFL_INTERVAL', '0.5'))
MAX_EVENT_SECONDS = float(os.environ.get('WFL_MAX_EVENT_SECONDS', '600'))
AFK_TIMEOUT = float(os.environ.get('WFL_AFK_TIMEOUT', '180'))
CDP_CACHE_TTL = float(os.environ.get('WFL_CDP_CACHE_TTL', '2.0'))
CDP_PORTS = [
    int(p) for p in os.environ.get('WFL_CDP_PORTS', '9222').split(',') if p.strip()
]

CHROMIUM_WM_CLASSES = {
    'io.github.ungoogled_software.ungoogled_chromium',
    'org.chromium.Chromium',
    'chromium-browser',
    'Chromium',
    'Google-chrome',
    'google-chrome',
}

try:
    from evdev import InputDevice, ecodes, list_devices
    EVDEV_AVAILABLE = True
except ImportError:
    EVDEV_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-5s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)


def _iso(wall):
    return datetime.fromtimestamp(wall).astimezone().isoformat(timespec='milliseconds')


# ---- window-calls (GNOME Shell extension) ---------------------------------

def list_windows():
    try:
        r = subprocess.run(
            ['gdbus', 'call', '--session',
             '--dest', 'org.gnome.Shell',
             '--object-path', '/org/gnome/Shell/Extensions/Windows',
             '--method', 'org.gnome.Shell.Extensions.Windows.List'],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode != 0:
            return []
        parsed = ast.literal_eval(r.stdout.strip())
        if not (isinstance(parsed, tuple) and parsed
                and isinstance(parsed[0], str)):
            logging.debug(f'unexpected gdbus output shape: {type(parsed)}')
            return []
        return json.loads(parsed[0])
    except Exception as e:
        logging.debug(f'window-calls List failed: {e}')
        return []


def focused_window(wins):
    for w in wins:
        if w.get('focus'):
            return w
    return None


# ---- CDP active-tab matching with TTL cache ------------------------------

class CDPCache:
    def __init__(self, ttl):
        self.ttl = ttl
        self.entries = {}  # port -> (mono_ts, tabs)
        self.lock = threading.Lock()

    def get(self, port, now_mono):
        with self.lock:
            ent = self.entries.get(port)
            if ent and (now_mono - ent[0]) < self.ttl:
                return ent[1]
        tabs = self._fetch(port)
        with self.lock:
            self.entries[port] = (now_mono, tabs)
        return tabs

    @staticmethod
    def _fetch(port):
        try:
            with urlopen(f'http://127.0.0.1:{port}/json', timeout=1) as r:
                data = json.loads(r.read().decode())
            return [t for t in data if t.get('type') == 'page']
        except (URLError, OSError, json.JSONDecodeError, TimeoutError):
            return []


_cdp_cache = CDPCache(CDP_CACHE_TTL)

_BROWSER_SUFFIX = re.compile(
    r' [—–\-] (Chromium|Google Chrome)( \(Incognito\))?$'
)
_ELLIPSIS_TAIL = re.compile(r'[…\.]+\s*$')


def match_active_tab(title, tabs):
    if not tabs or not title:
        return None
    base = _BROWSER_SUFFIX.sub('', title).strip()
    base = _ELLIPSIS_TAIL.sub('', base).strip()
    if not base:
        return None
    for t in tabs:
        if t.get('title', '').strip() == base:
            return t
    # GNOME may truncate long titles with '…'; CDP has the full string.
    # Match by longest-common-prefix; require a non-trivial overlap.
    prefix = base[:max(20, int(len(base) * 0.6))]
    for t in tabs:
        if t.get('title', '').strip().startswith(prefix):
            return t
    return None


# ---- Title normalization for dedupe ---------------------------------------

# U+2700–U+27BF Dingbats (zellij ✳ glyph), U+2800–U+28FF Braille (spinners),
# U+25A0–U+25FF Geometric Shapes (some prompt indicators).
_STATUS_GLYPHS = re.compile(r'[■-◿✀-⣿]+')
_UNREAD_PREFIX = re.compile(r'^\s*\(\d+\)\s*|^\s*•\s*')


def _normalize_title(t):
    if not t:
        return ''
    t = _STATUS_GLYPHS.sub('', t)
    t = _UNREAD_PREFIX.sub('', t)
    return ' '.join(t.split())


# ---- Event construction ---------------------------------------------------

def build_state(win, now_mono):
    """Return a dict (no timestamps) describing the focused window state."""
    wm_class = win.get('wm_class') or ''
    title = win.get('title') or ''
    rec = {
        'wm_class': wm_class,
        'wm_class_instance': win.get('wm_class_instance') or '',
        'title': title,
        'pid': win.get('pid'),
        'window_id': win.get('id'),
        'workspace': win.get('workspace'),
    }
    if wm_class in CHROMIUM_WM_CLASSES:
        for port in CDP_PORTS:
            tabs = _cdp_cache.get(port, now_mono)
            active = match_active_tab(title, tabs)
            if active is not None:
                rec['cdp_port'] = port
                rec['url'] = active.get('url')
                rec['tab_title'] = active.get('title')
                rec['tab_count'] = len(tabs)
                break
    return rec


def state_key(state):
    # URL is intentionally NOT part of the identity. SPAs and Google Search
    # rewrite the URL (tracking params, history.pushState) within a single
    # focused-tab session without changing document.title. Title is in the
    # GNOME window title, so a real navigation to a different page will
    # change the key naturally; same-page URL churn won't.
    return (state.get('window_id'),
            _normalize_title(state.get('title', '')))


# ---- Window event coalescer ----------------------------------------------

class WindowCoalescer:
    """Coalesce consecutive identical focus states into one row.

    Internally tracks both monotonic and wall-clock anchors per event;
    durations use monotonic (suspend-safe), emitted ISO timestamps use wall.
    """

    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.fh = open(path, 'a', buffering=1, encoding='utf-8')
        self.lock = threading.RLock()  # signal handler may re-enter
        self.current = None
        self._closed = False

    def observe(self, state, now_mono, now_wall):
        with self.lock:
            if self._closed:
                return
            if state is None:
                self._flush_locked(now_mono, now_wall)
                return
            key = state_key(state)
            if self.current is not None and self.current['key'] == key:
                # Same focus — extend OR roll over a long session.
                if (now_mono - self.current['start_mono']) >= MAX_EVENT_SECONDS:
                    # Roll over at `now` on BOTH sides so chunks are contiguous.
                    self._flush_locked(now_mono, now_wall)
                    self._begin(state, key, now_mono, now_wall)
                else:
                    self.current['last_mono'] = now_mono
                    self.current['last_wall'] = now_wall
                    # Refresh state — URL/tab_title may have changed within
                    # the same coalesced window (e.g. SPA navigation).
                    self.current['state'] = state
            else:
                self._flush_locked(now_mono, now_wall)
                self._begin(state, key, now_mono, now_wall)

    def _begin(self, state, key, now_mono, now_wall):
        self.current = {
            'start_mono': now_mono, 'start_wall': now_wall,
            'last_mono': now_mono, 'last_wall': now_wall,
            'key': key, 'state': state,
        }

    def flush(self):
        """Public flush — uses current time as the close boundary."""
        with self.lock:
            if self._closed:
                return
            self._flush_locked(time.monotonic(), time.time())

    def close(self):
        """Final flush; subsequent flush()/observe() are no-ops."""
        self.flush()
        with self.lock:
            self._closed = True
            try:
                self.fh.close()
            except Exception:
                pass

    def _flush_locked(self, _now_mono, _now_wall):
        if self.current is None:
            return
        c = self.current
        # Always emit duration from the monotonic delta (suspend-safe).
        # Always emit end-timestamps from the LAST observed wall time —
        # the focus may have ended seconds before this flush call.
        rec = dict(c['state'])
        rec['start'] = _iso(c['start_wall'])
        rec['end'] = _iso(c['last_wall'])
        rec['duration_s'] = round(c['last_mono'] - c['start_mono'], 3)
        self.fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
        self.current = None


# ---- AFK tracker ----------------------------------------------------------

class AFKTracker:
    """ActivityWatch-style AFK ↔ active transitions.

    Initial state is 'afk' — if the service starts while the user is
    actually away, no fake active interval is emitted; if the user is
    typing, the first touch() flips to active immediately (the resulting
    'afk' shard is at most ~POLL_INTERVAL long).
    """

    def __init__(self, path, enabled=True):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.fh = open(path, 'a', buffering=1, encoding='utf-8')
        self.lock = threading.RLock()
        self.enabled = enabled
        self._closed = False
        now_mono = time.monotonic()
        now_wall = time.time()
        self.state = 'afk'
        self.state_start_mono = now_mono
        self.state_start_wall = now_wall
        self.last_input_mono = now_mono
        self.last_input_wall = now_wall
        if not enabled:
            # Mark the stream so analysis can tell "evdev disabled" apart
            # from "user was active forever".
            rec = {
                'status': 'evdev_disabled',
                'start': _iso(now_wall),
                'end': _iso(now_wall),
                'duration_s': 0.0,
            }
            self.fh.write(json.dumps(rec) + '\n')

    def touch(self):
        if not self.enabled:
            return
        now_mono = time.monotonic()
        now_wall = time.time()
        with self.lock:
            if self._closed:
                return
            self.last_input_mono = now_mono
            self.last_input_wall = now_wall
            if self.state == 'afk':
                self._write_locked(
                    'afk',
                    self.state_start_mono, self.state_start_wall,
                    now_mono, now_wall,
                )
                self.state = 'active'
                self.state_start_mono = now_mono
                self.state_start_wall = now_wall

    def tick(self):
        if not self.enabled:
            return
        now_mono = time.monotonic()
        now_wall = time.time()
        with self.lock:
            if self._closed:
                return
            # active → afk transition
            if (self.state == 'active'
                    and (now_mono - self.last_input_mono) >= AFK_TIMEOUT):
                self._write_locked(
                    'active',
                    self.state_start_mono, self.state_start_wall,
                    self.last_input_mono, self.last_input_wall,
                )
                self.state = 'afk'
                self.state_start_mono = self.last_input_mono
                self.state_start_wall = self.last_input_wall
                return
            # Cap long single-state events so a crash loses ≤ MAX_EVENT_SECONDS.
            # For 'active', use last_input as the boundary so durations match
            # aw-watcher-afk's "active ends at last input" convention even
            # mid-session. For 'afk', use now.
            if self.state == 'active':
                ref_mono = self.last_input_mono
                ref_wall = self.last_input_wall
            else:
                ref_mono = now_mono
                ref_wall = now_wall
            if (ref_mono - self.state_start_mono) >= MAX_EVENT_SECONDS:
                self._write_locked(
                    self.state,
                    self.state_start_mono, self.state_start_wall,
                    ref_mono, ref_wall,
                )
                self.state_start_mono = ref_mono
                self.state_start_wall = ref_wall

    def flush(self):
        if not self.enabled:
            return
        now_mono = time.monotonic()
        now_wall = time.time()
        with self.lock:
            if self._closed:
                return
            if self.state == 'active':
                end_mono = self.last_input_mono
                end_wall = self.last_input_wall
            else:
                end_mono = now_mono
                end_wall = now_wall
            if end_mono > self.state_start_mono:
                self._write_locked(
                    self.state,
                    self.state_start_mono, self.state_start_wall,
                    end_mono, end_wall,
                )
                self.state_start_mono = end_mono
                self.state_start_wall = end_wall

    def close(self):
        """Final flush; subsequent calls are no-ops."""
        self.flush()
        with self.lock:
            self._closed = True
            try:
                self.fh.close()
            except Exception:
                pass

    def _write_locked(self, status, start_mono, start_wall, end_mono, end_wall):
        duration = round(end_mono - start_mono, 3)
        if duration <= 0:
            return
        rec = {
            'status': status,
            'start': _iso(start_wall),
            'end': _iso(end_wall),
            'duration_s': duration,
        }
        self.fh.write(json.dumps(rec, ensure_ascii=False) + '\n')


# ---- AFK input thread -----------------------------------------------------

def afk_input_reader(tracker, stop_event):
    if not EVDEV_AVAILABLE:
        logging.warning('python-evdev not installed — AFK detection disabled')
        return

    sel = selectors.DefaultSelector()
    monitored = {}  # path -> InputDevice

    def rescan():
        try:
            paths = set(list_devices())
        except Exception as e:
            logging.warning(f'list_devices failed: {e}')
            return
        for p in list(monitored):
            if p not in paths:
                try:
                    sel.unregister(monitored[p])
                    monitored[p].close()
                except Exception:
                    pass
                del monitored[p]
                logging.info(f'afk: device gone {p}')
        for p in paths - set(monitored):
            try:
                d = InputDevice(p)
            except OSError:
                continue
            caps = d.capabilities()
            if (ecodes.EV_KEY in caps
                    or ecodes.EV_REL in caps
                    or ecodes.EV_ABS in caps):
                try:
                    sel.register(d, selectors.EVENT_READ)
                    monitored[p] = d
                    logging.info(f'afk: monitoring {p} ({d.name})')
                except Exception as e:
                    logging.warning(f'afk: register {p} failed: {e}')

    rescan()
    if not monitored:
        # Keep trying — devices may show up later (USB hot-plug).
        logging.warning('afk: no input devices accessible yet, will keep rescanning')

    last_rescan = time.monotonic()
    while not stop_event.is_set():
        if not monitored:
            stop_event.wait(2.0)
            rescan()
            last_rescan = time.monotonic()
            continue
        try:
            ready = sel.select(timeout=1.0)
        except OSError:
            time.sleep(0.1)
            continue
        for sk, _ in ready:
            dev = sk.fileobj
            try:
                # Drain every available event but only touch the tracker once
                # per wakeup (touch is idempotent within a tick anyway).
                got_any = False
                while True:
                    try:
                        for _ in dev.read():
                            got_any = True
                    except BlockingIOError:
                        # No more events buffered. Normal end of drain.
                        break
                if got_any:
                    tracker.touch()
            except OSError:
                # Genuine device error (ENODEV etc).
                path = getattr(dev, 'path', None)
                try:
                    sel.unregister(dev)
                except Exception:
                    pass
                try:
                    dev.close()
                except Exception:
                    pass
                if path:
                    monitored.pop(path, None)
                    logging.info(f'afk: device gone {path}')
        if time.monotonic() - last_rescan > 10:
            rescan()
            last_rescan = time.monotonic()


# ---- Main -----------------------------------------------------------------

def main():
    if '--once' in sys.argv:
        wins = list_windows()
        focused = focused_window(wins)
        if focused is None:
            print('no focused window'); return
        print(json.dumps(build_state(focused, time.monotonic()),
                         ensure_ascii=False, indent=2))
        return

    window_coal = WindowCoalescer(WINDOW_LOG)
    afk = AFKTracker(AFK_LOG, enabled=EVDEV_AVAILABLE)
    stop = threading.Event()
    shutdown_signal = [None]

    def on_signal(signum, _frame):
        # Don't flush from the signal handler — main thread may hold a lock.
        # Just request shutdown and let the main loop tear down cleanly.
        logging.info(f'signal {signum} received')
        shutdown_signal[0] = signum
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGHUP, on_signal)
    # atexit is a belt-and-suspenders for unexpected exits; close() is
    # idempotent so this won't double-emit even after normal teardown.
    atexit.register(window_coal.close)
    atexit.register(afk.close)

    input_thread = threading.Thread(
        target=afk_input_reader, args=(afk, stop), daemon=False, name='afk-reader')
    input_thread.start()

    logging.info(
        f'window={WINDOW_LOG} afk={AFK_LOG} '
        f'poll={POLL_INTERVAL}s afk_timeout={AFK_TIMEOUT}s '
        f'max_event={MAX_EVENT_SECONDS}s cdp={CDP_PORTS} '
        f'cdp_cache_ttl={CDP_CACHE_TTL}s evdev={EVDEV_AVAILABLE}'
    )

    try:
        while not stop.is_set():
            now_mono = time.monotonic()
            now_wall = time.time()
            wins = list_windows()
            focused = focused_window(wins)
            state = build_state(focused, now_mono) if focused else None
            window_coal.observe(state, now_mono, now_wall)
            afk.tick()
            stop.wait(POLL_INTERVAL)
    finally:
        input_thread.join(timeout=2.0)
        window_coal.close()
        afk.close()

    if shutdown_signal[0] == signal.SIGHUP:
        os.execv(sys.executable, [sys.executable] + sys.argv)


if __name__ == '__main__':
    main()
