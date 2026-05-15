#!/usr/bin/env python3
"""window-focus-logger (macOS) — focused-window + AFK tracking, ActivityWatch-style.

macOS port of window_focus_logger.py. Same JSONL schema, same env vars
(WFL_LOG_DIR / WFL_INTERVAL / WFL_MAX_EVENT_SECONDS / WFL_AFK_TIMEOUT),
same start/end/duration_s coalescing semantics, same AFK transition rules
("active ends at last input"), same SIGHUP re-exec. Drop-in replacement
for downstream analysis.

macOS-specific substitutions:
- list_windows():   CGWindowListCopyWindowInfo + NSWorkspace
                    (Linux: gdbus → GNOME `window-calls` extension)
- AFK source:       CGEventSourceSecondsSinceLastEventType, polled inline
                    (Linux: evdev /dev/input/event* in a reader thread)
- Service:          launchd ~/Library/LaunchAgents/com.qs.window-focus.plist
                    (Linux: systemd --user)

Field-name mapping for the `window_focus.log` row schema (kept identical
to Linux so a single analyzer can consume both):
- wm_class           = bundle ID (e.g. com.apple.Terminal)
- wm_class_instance  = localized app name (e.g. Terminal)
- title              = kCGWindowName (requires Screen Recording TCC grant)
- pid                = process ID
- window_id          = kCGWindowNumber
- workspace          = null (macOS Spaces aren't exposed via public API)

CDP / browser URL detection from the Linux side is intentionally not
ported. macOS browser URL access requires per-browser AppleScript and
the MonkeyType exclusion case is satisfied by substring-matching the
page title that appears in each browser's OS-level window title.

Title gating: `kCGWindowName` returns None under launchd unless the
Python interpreter has Screen Recording TCC permission. All other
fields (app name, bundle, pid, window_id, AFK) work regardless.
"""

import atexit
import json
import logging
import os
import re
import signal
import sys
import threading
import time
from datetime import datetime

from AppKit import NSWorkspace
from Quartz import (
    CGWindowListCopyWindowInfo,
    CGEventSourceSecondsSinceLastEventType,
    kCGWindowListOptionOnScreenOnly,
    kCGWindowListExcludeDesktopElements,
    kCGNullWindowID,
    kCGAnyInputEventType,
    kCGEventSourceStateHIDSystemState,
)


LOG_DIR = os.environ.get(
    'WFL_LOG_DIR', '/Volumes/Data/Storage/Self/!raw-Keylog')
WINDOW_LOG = os.path.join(LOG_DIR, 'window_focus.log')
AFK_LOG = os.path.join(LOG_DIR, 'afk.log')
POLL_INTERVAL = float(os.environ.get('WFL_INTERVAL', '0.5'))
MAX_EVENT_SECONDS = float(os.environ.get('WFL_MAX_EVENT_SECONDS', '600'))
AFK_TIMEOUT = float(os.environ.get('WFL_AFK_TIMEOUT', '180'))


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-5s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)


def _iso(wall):
    return datetime.fromtimestamp(wall).astimezone().isoformat(timespec='milliseconds')


# ---- Focused-window snapshot via CG/NSWorkspace ---------------------------

def list_windows():
    """Return a list of window-state dicts; the focused entry has focus=True.

    Mirrors the GNOME `window-calls` extension's List output shape so that
    `focused_window()` and `build_state()` are platform-agnostic.
    """
    front = NSWorkspace.sharedWorkspace().frontmostApplication()
    if front is None:
        return []
    front_pid = int(front.processIdentifier())
    bundle_id = front.bundleIdentifier() or ''
    app_name = front.localizedName() or ''

    raw = CGWindowListCopyWindowInfo(
        kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements,
        kCGNullWindowID,
    )

    result = []
    front_window_picked = False
    for w in (raw or ()):
        owner_pid = int(w.get('kCGWindowOwnerPID') or 0)
        layer = int(w.get('kCGWindowLayer') or 0)
        win_id = int(w.get('kCGWindowNumber') or 0)
        title = w.get('kCGWindowName')
        owner_name = w.get('kCGWindowOwnerName') or ''
        is_front_app = (owner_pid == front_pid)
        # CGWindowList returns windows in front-to-back z-order. The first
        # layer-0 window of the frontmost app is the focused one.
        focus = False
        if is_front_app and layer == 0 and not front_window_picked:
            focus = True
            front_window_picked = True
        result.append({
            'wm_class': bundle_id if is_front_app else owner_name,
            'wm_class_instance': app_name if is_front_app else owner_name,
            'title': title,
            'pid': owner_pid,
            'id': win_id,
            'workspace': None,
            'focus': focus,
        })
    return result


def focused_window(wins):
    for w in wins:
        if w.get('focus'):
            return w
    return None


# ---- Title normalization for dedupe ---------------------------------------

# Identical to Linux side so analysis sees the same keys for cross-platform rows.
# U+2700–U+27BF Dingbats, U+2800–U+28FF Braille (spinners), U+25A0–U+25FF
# Geometric Shapes (prompt indicators / animation glyphs).
_STATUS_GLYPHS = re.compile(r'[■-◿✀-⣿]+')
_UNREAD_PREFIX = re.compile(r'^\s*\(\d+\)\s*|^\s*•\s*')


def _normalize_title(t):
    if not t:
        return ''
    t = _STATUS_GLYPHS.sub('', t)
    t = _UNREAD_PREFIX.sub('', t)
    return ' '.join(t.split())


# ---- Event construction ---------------------------------------------------

def build_state(win, _now_mono):
    """Return a dict (no timestamps) describing the focused window state."""
    return {
        'wm_class': win.get('wm_class') or '',
        'wm_class_instance': win.get('wm_class_instance') or '',
        'title': win.get('title') or '',
        'pid': win.get('pid'),
        'window_id': win.get('id'),
        'workspace': win.get('workspace'),
    }


def state_key(state):
    # URL is intentionally NOT part of the identity (and not collected on
    # macOS) — matches Linux to keep dedupe behaviour identical for SPAs
    # that rewrite the URL inside a focused-tab session without changing
    # the page title.
    return (state.get('window_id'),
            _normalize_title(state.get('title', '')))


# ---- Window event coalescer ----------------------------------------------

class WindowCoalescer:
    """Coalesce consecutive identical focus states into one row.

    Durations use monotonic (suspend-safe); emitted ISO timestamps use
    wall clock. Long sessions roll over contiguously at MAX_EVENT_SECONDS.
    """

    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.fh = open(path, 'a', buffering=1, encoding='utf-8')
        self.lock = threading.RLock()
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
                if (now_mono - self.current['start_mono']) >= MAX_EVENT_SECONDS:
                    # Contiguous roll-over at `now` on both clocks.
                    self._flush_locked(now_mono, now_wall)
                    self._begin(state, key, now_mono, now_wall)
                else:
                    self.current['last_mono'] = now_mono
                    self.current['last_wall'] = now_wall
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
        with self.lock:
            if self._closed:
                return
            self._flush_locked(time.monotonic(), time.time())

    def close(self):
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
        rec = dict(c['state'])
        rec['start'] = _iso(c['start_wall'])
        rec['end'] = _iso(c['last_wall'])
        rec['duration_s'] = round(c['last_mono'] - c['start_mono'], 3)
        self.fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
        self.current = None


# ---- AFK tracker ----------------------------------------------------------

class AFKTracker:
    """ActivityWatch-style AFK ↔ active transitions.

    Initial state is 'afk' — if the agent starts while the user is away,
    no false active interval is recorded; if they're typing, the first
    touch() flips to active immediately (resulting 'afk' shard ≤ ~poll).
    """

    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.fh = open(path, 'a', buffering=1, encoding='utf-8')
        self.lock = threading.RLock()
        self._closed = False
        now_mono = time.monotonic()
        now_wall = time.time()
        self.state = 'afk'
        self.state_start_mono = now_mono
        self.state_start_wall = now_wall
        self.last_input_mono = now_mono
        self.last_input_wall = now_wall

    def touch(self, input_mono=None, input_wall=None):
        """Register that input occurred at (input_mono, input_wall).

        Defaults to "now" if no timestamps are provided. The macOS caller
        supplies the precise idle-derived timestamp so AFK boundaries
        match the actual last-input event (mirrors aw-watcher-afk).
        """
        now_mono = time.monotonic()
        now_wall = time.time()
        if input_mono is None:
            input_mono = now_mono
        if input_wall is None:
            input_wall = now_wall
        with self.lock:
            if self._closed:
                return
            # Never let last_input go backwards (poll jitter / clock skew).
            if input_mono > self.last_input_mono:
                self.last_input_mono = input_mono
                self.last_input_wall = input_wall
            if self.state == 'afk':
                # Close the AFK shard at the input boundary itself.
                self._write_locked(
                    'afk',
                    self.state_start_mono, self.state_start_wall,
                    self.last_input_mono, self.last_input_wall,
                )
                self.state = 'active'
                self.state_start_mono = self.last_input_mono
                self.state_start_wall = self.last_input_wall

    def tick(self):
        now_mono = time.monotonic()
        now_wall = time.time()
        with self.lock:
            if self._closed:
                return
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
            if self.state == 'active':
                ref_mono, ref_wall = self.last_input_mono, self.last_input_wall
            else:
                ref_mono, ref_wall = now_mono, now_wall
            if (ref_mono - self.state_start_mono) >= MAX_EVENT_SECONDS:
                self._write_locked(
                    self.state,
                    self.state_start_mono, self.state_start_wall,
                    ref_mono, ref_wall,
                )
                self.state_start_mono = ref_mono
                self.state_start_wall = ref_wall

    def flush(self):
        now_mono = time.monotonic()
        now_wall = time.time()
        with self.lock:
            if self._closed:
                return
            if self.state == 'active':
                end_mono, end_wall = self.last_input_mono, self.last_input_wall
            else:
                end_mono, end_wall = now_mono, now_wall
            if end_mono > self.state_start_mono:
                self._write_locked(
                    self.state,
                    self.state_start_mono, self.state_start_wall,
                    end_mono, end_wall,
                )
                self.state_start_mono = end_mono
                self.state_start_wall = end_wall

    def close(self):
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


# ---- macOS idle poller ----------------------------------------------------

def _idle_seconds():
    return float(CGEventSourceSecondsSinceLastEventType(
        kCGEventSourceStateHIDSystemState, kCGAnyInputEventType,
    ))


# ---- Main -----------------------------------------------------------------

def main():
    if '--once' in sys.argv:
        wins = list_windows()
        focused = focused_window(wins)
        if focused is None:
            print('no focused window'); return
        snap = build_state(focused, time.monotonic())
        snap['idle_seconds'] = round(_idle_seconds(), 3)
        print(json.dumps(snap, ensure_ascii=False, indent=2))
        return

    window_coal = WindowCoalescer(WINDOW_LOG)
    afk = AFKTracker(AFK_LOG)
    stop = threading.Event()
    shutdown_signal = [None]

    def on_signal(signum, _frame):
        logging.info(f'signal {signum} received')
        shutdown_signal[0] = signum
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGHUP, on_signal)
    # Belt-and-suspenders for unexpected exits. close() is idempotent.
    atexit.register(window_coal.close)
    atexit.register(afk.close)

    logging.info(
        f'window={WINDOW_LOG} afk={AFK_LOG} '
        f'poll={POLL_INTERVAL}s afk_timeout={AFK_TIMEOUT}s '
        f'max_event={MAX_EVENT_SECONDS}s'
    )

    # AFK touch threshold: if idle < poll + jitter, input happened in the last
    # tick. Translate that to a precise last-input timestamp by subtracting
    # idle from now.
    touch_threshold = POLL_INTERVAL + 0.25

    try:
        while not stop.is_set():
            now_mono = time.monotonic()
            now_wall = time.time()
            idle = _idle_seconds()

            if idle < touch_threshold:
                last_in_mono = now_mono - idle
                last_in_wall = now_wall - idle
                afk.touch(last_in_mono, last_in_wall)

            wins = list_windows()
            focused = focused_window(wins)
            state = build_state(focused, now_mono) if focused else None
            window_coal.observe(state, now_mono, now_wall)
            afk.tick()
            stop.wait(POLL_INTERVAL)
    finally:
        window_coal.close()
        afk.close()

    if shutdown_signal[0] == signal.SIGHUP:
        os.execv(sys.executable, [sys.executable] + sys.argv)


if __name__ == '__main__':
    main()
