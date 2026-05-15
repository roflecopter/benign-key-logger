# benign-key-logger — Fedora Setup Guide

Evdev-based keystroke logger for keyboard layout optimization. Runs as a systemd user service on Fedora/Wayland.

## Full Setup on a Fresh Fedora Install

### 1. Install python-evdev

```bash
sudo dnf install python3-evdev
```

Or via pip (if using a venv):
```bash
pip3 install evdev
```

### 2. Add user to `input` group

Required to read `/dev/input/event*` devices:

```bash
sudo usermod -aG input $USER
```

**Must re-login (or reboot) after this** for the group change to take effect. Verify with:

```bash
groups  # should show 'input'
ls -la /dev/input/event0  # should show group 'input' with rw permissions
```

### 3. Clone the repo

```bash
cd ~/Storage/Dev
git clone <repo-url> benign-key-logger
```

### 4. Create systemd user service

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/key-logger.service << 'EOF'
[Unit]
Description=benign key logger (evdev)

[Service]
ExecStart=/usr/bin/python3 -u %h/Storage/Dev/benign-key-logger/key_logger.py
WorkingDirectory=%h/Storage/Dev/benign-key-logger
ExecReload=/bin/kill -HUP $MAINPID
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
EOF
```

### 5. Enable and start

```bash
systemctl --user daemon-reload
systemctl --user enable --now key-logger
```

### 6. Verify it's running

```bash
systemctl --user status key-logger
journalctl --user -u key-logger -f  # should show key events
```

## Troubleshooting

### Service starts but no key events logged

- **Not in `input` group**: Run `groups` — if `input` is missing, re-login after `usermod`
- **No keyboard devices found**: Check `ls -la /dev/input/event*` — all should have group `input` with `rw` for group
- **SELinux blocking**: Check `ausearch -m avc -ts recent` — if evdev access is denied, either set permissive or add a policy

### Permission denied on /dev/input/event*

```bash
# Verify device permissions
ls -la /dev/input/event*
# Should show: crw-rw----. 1 root input ...

# If group is not 'input', check udev rules:
cat /usr/lib/udev/rules.d/50-udev-default.rules | grep input
```

### Service crashes on startup

```bash
# Test manually first:
python3 -u ~/Storage/Dev/benign-key-logger/key_logger.py
# If ImportError: install evdev
sudo dnf install python3-evdev
```

## Configuration

In `key_logger.py`:
- `SEND_LOGS_TO_FILE = True` — writes to `key_log.txt` (one keystroke per line)
- `SEND_LOGS_TO_SQLITE = False` — set True for SQLite DB with query views
- Modifier remapping: left/right variants merged (e.g. `ctrl_l` → `ctrl`)

## Management

```bash
systemctl --user status key-logger     # check status
systemctl --user restart key-logger    # restart
systemctl --user reload key-logger     # reload (SIGHUP, re-execs)
journalctl --user -u key-logger -f     # follow logs
```

## Output

- `key_log.txt` — plain text, one keystroke per line with timestamps
- `key_log.sqlite` — SQLite DB with predefined views (if enabled)

## Dependencies

- `python3-evdev` (Fedora package) or `evdev` (pip)
- User in `input` group
- systemd user service (for auto-start)

---

# benign-key-logger — macOS Setup

`window_focus_logger_macos.py` is the macOS port of `window_focus_logger.py`. Same JSONL schema (`wm_class` / `wm_class_instance` / `title` / `pid` / `window_id` / `workspace` / `start` / `end` / `duration_s`), same env vars, same WindowCoalescer + AFKTracker semantics — so a single downstream analyzer can consume both Linux and macOS streams.

`key_logger.py` runs unchanged via `pynput` (no evdev needed on macOS).

## Substitutions vs Linux

| Linux | macOS |
|-------|-------|
| `gdbus` → GNOME `window-calls` extension | `CGWindowListCopyWindowInfo` + `NSWorkspace.frontmostApplication` (pyobjc) |
| `evdev /dev/input/event*` reader thread | `CGEventSourceSecondsSinceLastEventType` polled inline |
| `systemd --user` | launchd `~/Library/LaunchAgents/*.plist` |
| `input` group | TCC permissions (see below) |

CDP / browser URL detection from the Linux side is not ported. macOS browser URL access needs per-browser AppleScript (Firefox in particular has no AppleScript URL dictionary). The MonkeyType exclusion case is satisfied by substring-matching the page title that appears inside each browser's OS-level window title.

## Setup

### 1. Python with pyobjc

Requires pyobjc (`Quartz`, `AppKit`) and `pynput`. Anaconda's macOS distribution ships pyobjc; a venv works too:

```bash
pip3 install pyobjc-framework-Cocoa pyobjc-framework-Quartz pynput
```

### 2. TCC permissions

System Settings → Privacy & Security → grant the **Python interpreter** binary path (not the wrapper shell) to:

| Service | Reason |
|---------|--------|
| **Input Monitoring** | `pynput.keyboard.Listener` in `key_logger.py` |
| **Screen Recording** | `kCGWindowName` for window titles in `window_focus_logger_macos.py` |

Without Screen Recording, the focus logger keeps running and logs `wm_class` / `pid` / `window_id` / AFK transitions fine, but every `title` row is `None`.

**Gotcha**: TCC decisions are cached per process launch. After granting, restart the agent with `launchctl kickstart -k`. Don't trust manual shell-runs from Terminal as proof the daemon will work — Terminal's grants propagate to child processes via the "responsible PID" mechanism; launchd's don't.

### 3. LaunchAgents

Two agents, both `KeepAlive=true`:

`~/Library/LaunchAgents/com.qs.keylogger.plist`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.qs.keylogger</string>
    <key>ProgramArguments</key>
    <array>
        <string>/path/to/python</string>
        <string>/path/to/repo/key_logger.py</string>
    </array>
    <key>KeepAlive</key><true/>
    <key>WorkingDirectory</key><string>/path/to/repo</string>
</dict>
</plist>
```

`~/Library/LaunchAgents/com.qs.window-focus.plist`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.qs.window-focus</string>
    <key>ProgramArguments</key>
    <array>
        <string>/path/to/python</string>
        <string>-u</string>
        <string>/path/to/repo/window_focus_logger_macos.py</string>
    </array>
    <key>KeepAlive</key><true/>
    <key>WorkingDirectory</key><string>/path/to/repo</string>
    <key>StandardOutPath</key><string>/tmp/com.qs.window-focus.out.log</string>
    <key>StandardErrorPath</key><string>/tmp/com.qs.window-focus.err.log</string>
</dict>
</plist>
```

Load:
```bash
UID=$(id -u)
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.qs.keylogger.plist
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.qs.window-focus.plist
```

### 4. Environment variables (same as Linux side)

| Var | Default | Meaning |
|-----|---------|---------|
| `WFL_LOG_DIR`           | `/Volumes/Data/Storage/Self/!raw-Keylog` (m1max default) | dir holding `window_focus.log` + `afk.log` |
| `WFL_INTERVAL`          | `0.5`  | poll interval seconds |
| `WFL_MAX_EVENT_SECONDS` | `600`  | force-flush after this long in one state (crash bound) |
| `WFL_AFK_TIMEOUT`       | `180`  | seconds of idle before AFK starts |

CLI flags mirror the env vars (`--once`, `--interval`, etc.); `--once` prints a snapshot and exits.

## Management

```bash
launchctl print          gui/$(id -u)/com.qs.window-focus | grep -E 'state|pid|last exit'
launchctl kickstart -k   gui/$(id -u)/com.qs.window-focus   # restart, picks up new code, NOT plist edits
launchctl bootout        gui/$(id -u)/com.qs.window-focus   # then bootstrap again, for plist changes
```

stdout/stderr go to `/tmp/com.qs.window-focus.{out,err}.log` per the plist above.

## Output

- `key_log.sqlite` (or `key_log.txt`) — same as Linux.
- `window_focus.log` — JSONL, one row per coalesced focus session (`wm_class`/`wm_class_instance`/`title`/`pid`/`window_id`/`workspace` + `start`/`end`/`duration_s`).
- `afk.log` — JSONL, one row per AFK ↔ active transition (`status`/`start`/`end`/`duration_s`).

Both `window_focus.log` and `afk.log` are written with mode `0600` (owner-only) and `buffering=1` (line-buffered).
