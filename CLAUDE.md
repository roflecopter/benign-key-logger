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
