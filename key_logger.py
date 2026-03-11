#!/usr/bin/env python3

# ######### ######### ######## ##########
# ######### Execution Overview ##########
# ######### ######### ######## ##########

# Listen for every key-down and key-up event
# For each key-press, check it against of list of ignored keys, and, if
#   needed, remap it prior to further processing (e.g. <ctrl-r> to
#   <ctrl>, since I don't care about which <ctrl> key was used)
# On each key-down event, if it's not a modifier, then log it. If it's
#   a modifier, then keep track of what's being held down so we can log
#   the key-combo later.
# On each key-up event, clear the key from the list of what's being
#   held down

import logging
import os
import signal
import sys


# ######### #### ######## ##########
# ######### User Settings ##########
# ######### #### ######## ##########

SEND_LOGS_TO_SQLITE = False
SEND_LOGS_TO_FILE = True

LOG_FILE_NAME = 'key_log.txt'
SQLITE_FILE_NAME = 'key_log.sqlite'

# ######### ####### ##### ##########
# ######### Logging Setup ##########
# ######### ####### ##### ##########

logging.basicConfig(
    # level=logging.DEBUG,
    level=logging.INFO,
    format='%(asctime)s.%(msecs)03d : %(levelname)-5s : %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

from datetime import datetime

if SEND_LOGS_TO_SQLITE:
  import sqlite3

if SEND_LOGS_TO_FILE:
  logging.info(f'File used for logging: {LOG_FILE_NAME}')

# ######### ###### ######### ##########
# ######### Global Variables ##########
# ######### ###### ######### ##########

# This can be changed, but I'm not sure there's much value to tinkering
# with it. You can read more about why it exists in the key_up()
# function.
LOCKED_IN_GARBAGE_COLLECTION_LIMIT = 5

if sys.platform == 'linux':
    # On Linux we use string-based key names from evdev instead of pynput
    # Key objects. These mirror the pynput names for compatibility.
    MODIFIER_KEYS = [
        'alt', 'alt_r', 'alt_l',
        'super', 'super_r', 'super_l',
        'ctrl', 'ctrl_r', 'ctrl_l',
        'shift', 'shift_r', 'shift_l',
    ]

    IGNORED_KEYS = []

    REMAP = {
        'alt_r': 'alt',
        'alt_l': 'alt',
        'ctrl_r': 'ctrl',
        'ctrl_l': 'ctrl',
        'super_r': 'super',
        'super_l': 'super',
        'shift_r': 'shift',
        'shift_l': 'shift',
    }
else:
    from pynput.keyboard import Key, Listener

    MODIFIER_KEYS = [
        Key.alt,
        Key.alt_r,
        Key.alt_l,
        Key.cmd,
        Key.cmd_r,
        Key.cmd_l,
        Key.ctrl,
        Key.ctrl_r,
        Key.ctrl_l,
        Key.shift,
        Key.shift_r,
        Key.shift_l,
    ]

    IGNORED_KEYS = []

    REMAP = {
        Key.alt_r: Key.alt,
        Key.alt_l: Key.alt,
        Key.ctrl_r: Key.ctrl,
        Key.ctrl_l: Key.ctrl,
        Key.cmd_r: Key.cmd,
        Key.cmd_l: Key.cmd,
        Key.shift_r: Key.shift,
        Key.shift_l: Key.shift,
    }

keys_currently_down = []

# ######### ####### ######### ##########
# ######### Logging Functions ##########
# ######### ####### ######### ##########


def setup_sqlite_database():
  """
  This creates the Python objects and the initial table and views in
  the SQLite database (file). It's long, but that's primarily just
  because some of the SQL for the views is long. At it's most basic, it
  is pretty straight forward: (1) connect to the SQLite file, (2) create
  the table for storing key presses, and (3) create the views for
  looking at usage statistics

  If you already have a SQLite key-log (a file with the name
  SQLITE_FILE_NAME), then the results of the session will be APPENDED to
  those. This is accomplished by running a CREATE TABLE IF NOT EXISTS
  statement, as opposed to the more simple CREATE TABLE statement. If
  you want to start from scratch you can (1) delete the existing file
  from your disk (the below will create a new one), (2) rename the
  existing file so you retain that prior sessions' data and a new file
  will be created, or (3) change the name of the file in the
  SQLITE_FILE_NAME variable above and a new one will be created.

  There's a few views that are created, simply as a convenience, that
  will list your usage by key, bi-gram, and tri-gram. The main table
  keeps a single row for every key-stroke, which doesn't do much for the
  ultimate goal of understanding your aggregate key usage.
  """
  global db_connection
  global db_cursor
  db_connection = sqlite3.connect(
      SQLITE_FILE_NAME,
      check_same_thread=False,
      # The same thread check is off since the keyboard listener works
      # in a spawned thread (a decision of the pynput library) separate
      # from this python script.
  )
  db_cursor = db_connection.cursor()
  logging.debug('SQLite connection and cursor created')

  db_cursor.execute("""
      CREATE TABLE IF NOT EXISTS key_log
      (time_utc TEXT, key_code TEXT)
  """)
  logging.debug('SQLite logging table created')

  db_cursor.execute('DROP VIEW IF EXISTS key_counts')
  db_cursor.execute("""
    CREATE VIEW IF NOT EXISTS key_counts AS
    WITH frequencies AS (
        SELECT key_code, count(*) AS count,
            (count(*) * 1.0) / (SELECT count(*) FROM key_log) AS frequency
        FROM key_log
        GROUP BY 1
    )
    SELECT *, SUM(frequency) OVER (
        ORDER BY frequency DESC ROWS UNBOUNDED PRECEDING
    ) AS cumulative_frequency
    FROM frequencies
    ORDER BY frequency DESC, key_code
  """)
  logging.debug('SQLite key_counts view created')

  db_cursor.execute('DROP VIEW IF EXISTS bigram_counts')
  db_cursor.execute("""
    CREATE VIEW IF NOT EXISTS bigram_counts AS
    WITH raw_bigram_data AS
    (
      SELECT key_code, lag(key_code) OVER (ORDER BY time_utc) AS key_code_lag_1
      FROM key_log
    )
    , bigram_counts AS
    (
      SELECT key_code_lag_1 || ' ' || key_code AS bigram, count(*) AS count
      FROM raw_bigram_data
      WHERE true
        AND key_code IS NOT NULL
        AND key_code_lag_1 IS NOT NULL
        AND key_code NOT LIKE '%+%'
        AND key_code_lag_1 NOT LIKE '%+%'
      GROUP BY 1
    )
    , bigram_frequencies AS
    (
      SELECT *,
        (1.0* count ) / (SELECT sum(count) FROM bigram_counts) AS frequency
      FROM bigram_counts
    )
    SELECT *, SUM(frequency) OVER (
        ORDER BY frequency DESC ROWS UNBOUNDED PRECEDING
    ) AS cumulative_frequency
    FROM bigram_frequencies
    GROUP BY bigram
    ORDER BY cumulative_frequency, count DESC, bigram
  """)
  logging.debug('SQLite bigram_counts view created')

  db_cursor.execute('DROP VIEW IF EXISTS trigram_counts')
  db_cursor.execute("""
    CREATE VIEW IF NOT EXISTS trigram_counts AS
    WITH raw_trigram_data AS
    (
      SELECT
        key_code,
        lag(key_code) OVER (ORDER BY time_utc) AS key_code_lag_1,
        lag(key_code, 2) OVER (ORDER BY time_utc) AS key_code_lag_2
      FROM key_log
    )
    , trigram_counts AS
    (
      SELECT
        key_code_lag_2 || ' ' || key_code_lag_1 || ' ' || key_code AS trigram,
        count(*) AS count
      FROM raw_trigram_data
      WHERE true
        AND key_code IS NOT NULL
        AND key_code_lag_1 IS NOT NULL
        AND key_code_lag_2 IS NOT NULL
        AND key_code NOT LIKE '%+%'
        AND key_code_lag_1 NOT LIKE '%+%'
        AND key_code_lag_2 NOT LIKE '%+%'
      GROUP BY 1
    )
    , trigram_frequencies AS
    (
      SELECT *,
        (1.0* count ) / (SELECT sum(count) FROM trigram_counts) AS frequency
      FROM trigram_counts
    )
    SELECT *, SUM(frequency) OVER (
        ORDER BY frequency DESC ROWS UNBOUNDED PRECEDING
    ) AS cumulative_frequency
    FROM trigram_frequencies
    GROUP BY trigram
    ORDER BY cumulative_frequency, count DESC, trigram
  """)
  logging.debug('SQLite trigram_counts view created')

  db_connection.commit()
  logging.info(f'SQLite database set up: {SQLITE_FILE_NAME}')


def log(key):
  """
  We're looking to see what modifiers are pressed, in addition to the
  key that triggered the log request, and then want to log that.

  If <shift> alone is the modifier pressed down we don't really need to
  track it when used with a symbol. For example, if you press 'a',
  you'll see that 'a' is logged, and when you press 'shift+a', 'A'
  is logged (similarly '1' and '!'). It's not important that shift
  was used to get there, only the final key, since, when setting up
  your own keyboard, it's possible that you choose to put a symbol,
  like '@' on a layer that doesn't require shift. The "alone" part here
  may look a bit more complicated than needed; however, it's to account
  for the case that you don't remap <shift_r> to <shift> and you then
  either do <shift_r> + a = A, or even <shift_r> + <shift_l> + a = A.
  The code there maps <shift>, <shift_r>, and <shift_l> to <shift>, and
  then removes duplicates (multi-shift key-downs) using the set(...)
  constructor.

  If you use shift with a non-symbol (like the right arrow), then
  you'd want to log it as normal, since this behavior (which is
  expanding the selection one character to the right usually)
  wouldn't be distinguishable from simple <right>.

  Additionally, when there are modifiers in addition to shift (like
  <ctrl> or <cmd>), you'll see that <ctrl> + <shift> + 'a' is logged
  like that and not like <ctrl> + A. So maintaining the <shift> as
  part of the combo, is important to distinguish between <ctrl> + a
  and <ctrl> + <shift> + a (and logging <ctrl> + A seems,
  conceptually, to miss the mark on logging combos).
  """
  modifiers_down = [k for k in keys_currently_down if k in MODIFIER_KEYS]
  if sys.platform == 'linux':
    shift_keys = ['shift', 'shift_l', 'shift_r']
    shift_only = list(set([
        'shift' if k in shift_keys else k
        for k in modifiers_down
    ])) == ['shift']
  else:
    shift_only = list(set([
        Key.shift if k in [Key.shift, Key.shift_l, Key.shift_r] else k
        for k in modifiers_down
    ])) == [Key.shift]
  if shift_only and key_is_a_symbol(key):
    modifiers_down = []
  log_entry = datetime.utcnow().isoformat() + ',' + ' + '.join(
      sorted([key_to_str(k) for k in modifiers_down])
      + [key_to_str(key)]
  )
  logging.info(f'key: {log_entry}')

  if SEND_LOGS_TO_SQLITE:
    row_values = (datetime.utcnow().isoformat(), log_entry)
    db_cursor.execute(
        'INSERT INTO key_log VALUES (?, ?)',
        row_values
    )
    db_connection.commit()
    logging.debug(f'logged to SQLite: {row_values}')

  if SEND_LOGS_TO_FILE:
    with open(LOG_FILE_NAME, 'a') as log_file:  # append mode
      log_file.write(f'{log_entry}\n')
      logging.debug(f'logged to file: {log_entry}')


# ######### ### ######### ##########
# ######### Key Functions ##########
# ######### ### ######### ##########


def key_is_a_symbol(key):
  if sys.platform == 'linux':
    # On Linux, keys are strings. Symbols are single characters;
    # named keys like 'ctrl', 'alt', 'space' are multi-char strings.
    return isinstance(key, str) and len(key) == 1
  return str(key)[0:4] != 'Key.'


def key_to_str(key):
  """
  The string representation pynput's Key.* objects isn't my preferred
  output, so this function standardizes how they're "stringified". The
  gist is that symbols (for example: a, b, Z, !, 3) are presented as-is,
  and other keys (for example: shift, control, command) are enclosed in
  brackets: '<' and '>'.

  There's a slightly more involved process for the symbols, only
  because the string representation includes the surrounding quotes of
  character (for example: "'a'") and it escapes backslashes, so that part
  undoes those two items.
  """
  if sys.platform == 'linux':
    if key_is_a_symbol(key):
      return key
    return f'<{key}>'
  s = str(key)
  if not key_is_a_symbol(key):
    s = f'<{s[4:]}>'
  else:
    s = s.encode('latin-1', 'backslashreplace').decode('unicode-escape')
    s = s[1:-1]  # trim the leading and trailing quotes
  return s


def key_down(key):
  """
  The goal here is to keep track of each key that's being pressed down
  and log when an action has taken place. By "action" I mean something
  that would be expected to send a keystroke to the computer (such as
  'a', as opposed to pressing just <shift>). If what's being pressed is
  only a modifier (the <cmd> in <cmd>-A), then we need to keep track
  that it's down, and wait until something else is pressed.

  First we only log the press if it's not already in the list, to avoid
  logging "sticky keys": pressing and holding a key and then seeing it
  typed many times. By exiting the function (stopping all processing)
  if it's in the keys_currently_down list (already being pressed), we
  ignore the repeats. This seems reasonable since in some places,
  holding the key will type the letter many times, and in others it
  will pop up a menu for selecting letters with diacritics. So it
  seems poorly defined as to what's actually happening on the screen
  anyways, during a hold-down. And the whole point of this program is
  to help you figure out what your fingers are doing, not necessarily
  what is going on in the computer.
  """
  if key in keys_currently_down:
    return

  keys_currently_down.append(key)
  logging.debug(
      f'key down : {key_to_str(key)} : '
      f'{[key_to_str(k) for k in keys_currently_down]}'
  )
  if key not in MODIFIER_KEYS:
    log(key)


def key_up(key):
  """
  The real action goes on when a key is pressed down, not up; however,
  this function needs to accomplish two key things: (1) registering that
  a key is no longer being pressed, which is especially important for
  the modifier keys, and (2) taking care of some routine clean up for
  keys that never get registered as having been released (up events,
  without a corresponding down event).

  It seems counter intuitive that a key could have been released, but
  never pressed; however, it seems to be the case. I don't know how
  general this case is, but it was consistent and repeatable for me.
  This seems to happen when you mix up the order of <shift> plus a
  symbol. For example, press down <shift>, then 'a', and what
  registers is a down-press of 'A' (which gets logged). If you then
  release the a-key, you're left with only <shift> being held down,
  which is an effectively "nothing" state. But instead, if you pick up
  <shift> first (while your finger is still holding down the a-key), the
  system registers <shift> up, and then 'a' (little a) down, but never
  'A' (big-A) up. And lastly when you pick up your finger from 'a', then
  the little-a gets cleared. In that sequence you'll see that big-A
  never got cleared (registered up). So now big-A is still in the
  keys_currently_down list, but you have no fingers on the keyboard.
  Another instance I've noticed is that as the system goes into and out
  of "secure input mode" (at least in macOS, the system locks down
  keyboard listening when you're in a password field), some key-up and
  key-down pairs don't match.

  In practice, this isn't actually that big of a deal because the
  logging event happens when a key is pressed down, and only the
  modifier keys (<ctrl>, <cmd>, etc.) plus the actual key that's being
  pressed, are being used to determine the combination. So a spurious
  'A' that's still in the keys_currently_down list, will have no effect
  when the 'w' is pressed in <cmd>-w (close a window).

  However, in the interest of good house-keeping, I like the idea of
  periodically clearing out these locked-in symbols from the
  keys_currently_down list. We accomplish this by waiting until two
  criteria have been met: (1) the length of the keys_currently_down list
  exceeds a threshold (LOCKED_IN_GARBAGE_COLLECTION_LIMIT; so it doesn't
  happen too often or prematurely), and (2) there are no modifier keys
  being pressed (so we don't clean things up when you're in the middle
  of a key-combo).
  """
  global keys_currently_down

  try:
    keys_currently_down.remove(key)
  except ValueError:
    logging.warning(f'{key_to_str(key)} up event without a paired down event')
    if len(keys_currently_down) >= LOCKED_IN_GARBAGE_COLLECTION_LIMIT:
      logging.debug('key-down count is above locked-in limit')
      number_of_modifiers_down = len([
          k for k in keys_currently_down if k in MODIFIER_KEYS
      ])
      if number_of_modifiers_down == 0:
        logging.debug(
            'clearing locked-in keys-down: '
            f'{[key_to_str(k) for k in keys_currently_down]}'
        )
        keys_currently_down = []

  logging.debug(
      f'key up  : {key_to_str(key)} : '
      f'{[key_to_str(k) for k in keys_currently_down]}'
  )


def preprocess(key, f):
  """
  A simple wrapper to to do some preprocessing on the key press prior to
  sending it off for normal key-up/down handling.

  The remapping step helps simplify the logging. For example, I don't
  care to log whether the left or right Control key was used in a combo,
  just that Control was used. So the CTRL_R is remapped simply to CTRL.

  Ignoring comes after remapping so that the ignore list can take
  advantage of the remapping, for brevity. For example, you can
  ignore left and right shift, by remapping shift_r and shift_l to
  shift, and then ignoring shift.
  """
  k = key
  if key in REMAP:
    k = REMAP[key]
    logging.debug(f'remapped key {key_to_str(key)} -> {key_to_str(k)}')

  if k in IGNORED_KEYS:
    logging.debug(f'ignoring key: {key_to_str(k)}')
    return

  return f(k)


# ######### ######### ##########
# ######### Execution ##########
# ######### ######### ##########


def _handle_sighup(signum, frame):
  """Re-exec the process on SIGHUP so `systemctl --user reload` picks up code changes."""
  logging.info('received SIGHUP, re-executing')
  os.execv(sys.executable, [sys.executable] + sys.argv)


def main():
  signal.signal(signal.SIGHUP, _handle_sighup)
  logging.info('getting set up')
  if SEND_LOGS_TO_SQLITE:
    setup_sqlite_database()

  if sys.platform == 'linux':
    main_linux()
  else:
    main_darwin()


def main_darwin():
  with Listener(
      on_press=(lambda key: preprocess(key, key_down)),
      on_release=(lambda key: preprocess(key, key_up)),
  ) as listener:
    logging.info('starting to listen for keyboard events')
    listener.join()


# ######### ############## ##########
# ######### Linux / evdev  ##########
# ######### ############## ##########

# Evdev keycodes are translated to characters using libxkbcommon, which
# respects the active keyboard layout (e.g. Russian).  Modifier and named
# keys (Enter, arrows, …) use a static map.  If XKB initialisation fails
# the logger falls back to a hardcoded US-QWERTY character map.

import ctypes
import ctypes.util
import re
import subprocess
import threading

# -- Static maps for modifiers and named keys (layout-independent) --------

EVDEV_MODIFIER_MAP = None   # lazily built
EVDEV_NAMED_KEY_MAP = None  # lazily built


def _build_evdev_maps():
  global EVDEV_MODIFIER_MAP, EVDEV_NAMED_KEY_MAP
  from evdev import ecodes

  EVDEV_MODIFIER_MAP = {}
  for ecode_name, name in {
      'LEFTSHIFT': 'shift_l', 'RIGHTSHIFT': 'shift_r',
      'LEFTCTRL': 'ctrl_l', 'RIGHTCTRL': 'ctrl_r',
      'LEFTALT': 'alt_l', 'RIGHTALT': 'alt_r',
      'LEFTMETA': 'super_l', 'RIGHTMETA': 'super_r',
  }.items():
    code = getattr(ecodes, f'KEY_{ecode_name}', None)
    if code is not None:
      EVDEV_MODIFIER_MAP[code] = name

  EVDEV_NAMED_KEY_MAP = {}
  for ecode_name, name in {
      'ENTER': 'enter', 'BACKSPACE': 'backspace', 'DELETE': 'delete',
      'ESC': 'esc', 'INSERT': 'insert', 'HOME': 'home', 'END': 'end',
      'PAGEUP': 'page_up', 'PAGEDOWN': 'page_down',
      'UP': 'up', 'DOWN': 'down', 'LEFT': 'left', 'RIGHT': 'right',
      'CAPSLOCK': 'caps_lock', 'NUMLOCK': 'num_lock',
      'SCROLLLOCK': 'scroll_lock', 'PRINT': 'print_screen',
      'PAUSE': 'pause', 'MENU': 'menu',
  }.items():
    code = getattr(ecodes, f'KEY_{ecode_name}', None)
    if code is not None:
      EVDEV_NAMED_KEY_MAP[code] = name
  for i in range(1, 21):
    code = getattr(ecodes, f'KEY_F{i}', None)
    if code is not None:
      EVDEV_NAMED_KEY_MAP[code] = f'f{i}'


# -- XKB layout-aware translation ----------------------------------------

class _XkbRuleNames(ctypes.Structure):
  _fields_ = [
      ('rules', ctypes.c_char_p),
      ('model', ctypes.c_char_p),
      ('layout', ctypes.c_char_p),
      ('variant', ctypes.c_char_p),
      ('options', ctypes.c_char_p),
  ]

_xkb_lib = None
_xkb_state = None
_xkb_keymap = None
_current_group = 0
_layout_to_group = {}  # layout name -> XKB group index
_XKB_MOD_SHIFT_MASK = 1  # Shift is modifier index 0


def _init_xkb():
  """Initialise libxkbcommon with the system keyboard layouts."""
  global _xkb_lib, _xkb_state, _xkb_keymap

  lib_path = ctypes.util.find_library('xkbcommon')
  if not lib_path:
    logging.warning('libxkbcommon not found, falling back to QWERTY map')
    return False
  _xkb_lib = ctypes.CDLL(lib_path)

  _xkb_lib.xkb_context_new.restype = ctypes.c_void_p
  _xkb_lib.xkb_keymap_new_from_names.restype = ctypes.c_void_p
  _xkb_lib.xkb_keymap_new_from_names.argtypes = [
      ctypes.c_void_p, ctypes.POINTER(_XkbRuleNames), ctypes.c_int]
  _xkb_lib.xkb_state_new.restype = ctypes.c_void_p
  _xkb_lib.xkb_state_new.argtypes = [ctypes.c_void_p]
  _xkb_lib.xkb_state_key_get_utf8.restype = ctypes.c_int
  _xkb_lib.xkb_state_key_get_utf8.argtypes = [
      ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_size_t]
  _xkb_lib.xkb_state_update_mask.restype = ctypes.c_int
  _xkb_lib.xkb_state_update_mask.argtypes = (
      [ctypes.c_void_p] + [ctypes.c_uint32] * 6)

  # Read layout configuration from GNOME
  try:
    sources = subprocess.check_output(
        ['gsettings', 'get', 'org.gnome.desktop.input-sources', 'sources'],
        text=True).strip()
    options_str = subprocess.check_output(
        ['gsettings', 'get', 'org.gnome.desktop.input-sources', 'xkb-options'],
        text=True).strip()
  except Exception:
    sources = "[('xkb', 'us')]"
    options_str = "[]"

  layouts, variants = [], []
  for m in re.finditer(r"\('xkb',\s*'([^']+)'\)", sources):
    s = m.group(1)
    if '+' in s:
      l, v = s.split('+', 1)
      layouts.append(l); variants.append(v)
    else:
      layouts.append(s); variants.append('')
  options = [m.group(1) for m in re.finditer(r"'([^']+)'", options_str)]

  ctx = _xkb_lib.xkb_context_new(0)
  names = _XkbRuleNames()
  names.rules = b'evdev'
  names.model = b'pc105'
  names.layout = ','.join(layouts).encode()
  names.variant = ','.join(variants).encode()
  names.options = ','.join(options).encode()

  _xkb_keymap = _xkb_lib.xkb_keymap_new_from_names(
      ctx, ctypes.byref(names), 0)
  if not _xkb_keymap:
    logging.warning('XKB keymap creation failed, falling back to QWERTY map')
    return False

  _xkb_state = _xkb_lib.xkb_state_new(_xkb_keymap)

  global _layout_to_group
  # Build name → group index map. Use the raw source name (e.g. 'isrt-rus')
  # which is what GNOME reports in mru-sources.
  source_names = [m.group(1) for m in
                  re.finditer(r"\('xkb',\s*'([^']+)'\)", sources)]
  _layout_to_group = {name: idx for idx, name in enumerate(source_names)}

  logging.info(f'XKB initialised: layouts={",".join(layouts)}')
  return True


def _start_layout_monitor():
  """Watch GNOME's active input source via D-Bus mru-sources signal."""
  global _current_group
  try:
    from gi.repository import Gio, GLib

    def on_dconf_changed(conn, sender, path, iface, signal, params):
      global _current_group
      # params is (path, keys, tag) from DConf.Writer.Notify
      # or (schema, key, value) from GSettings PropertiesChanged
      try:
        args = params.unpack()
        # DConf signal: args[0] is the path like '/org/gnome/desktop/input-sources/mru-sources'
        # We only care about mru-sources changes
        path_str = str(args[0]) if args else ''
        if 'mru-sources' not in path_str:
          return
      except Exception:
        pass
      # Read the current mru-sources value
      try:
        settings = Gio.Settings.new('org.gnome.desktop.input-sources')
        mru = settings.get_value('mru-sources')
        if mru.n_children() > 0:
          first = mru.get_child_value(0)
          active_layout = first.get_child_value(1).get_string()
          new_group = _layout_to_group.get(active_layout, 0)
          if new_group != _current_group:
            _current_group = new_group
            logging.info(f'keyboard layout changed to {active_layout} (group {new_group})')
      except Exception as e:
        logging.debug(f'mru-sources read failed: {e}')

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    bus.signal_subscribe(
        None,              # sender (any)
        'ca.desrt.dconf.Writer',  # DConf Writer interface
        'Notify',          # signal name
        None,              # path (any)
        None,              # arg0 (any)
        Gio.DBusSignalFlags.NONE,
        on_dconf_changed,
    )

    # Read initial value
    settings = Gio.Settings.new('org.gnome.desktop.input-sources')
    mru = settings.get_value('mru-sources')
    if mru.n_children() > 0:
      first = mru.get_child_value(0)
      active_layout = first.get_child_value(1).get_string()
      _current_group = _layout_to_group.get(active_layout, 0)

    loop = GLib.MainLoop()
    threading.Thread(target=loop.run, daemon=True).start()
    logging.info(f'layout monitor started (current group: {_current_group})')
  except Exception as e:
    logging.warning(f'layout monitor unavailable: {e}')


def _xkb_translate(evdev_code, shift_held):
  """Translate an evdev keycode to a character using XKB + active layout."""
  if _xkb_state is None:
    return None
  mods = _XKB_MOD_SHIFT_MASK if shift_held else 0
  _xkb_lib.xkb_state_update_mask(
      _xkb_state, mods, 0, 0, 0, 0, _current_group)
  buf = ctypes.create_string_buffer(8)
  size = _xkb_lib.xkb_state_key_get_utf8(
      _xkb_state, evdev_code + 8, buf, 8)
  if size > 0:
    char = buf.value.decode('utf-8')
    if len(char) == 1 and (char.isprintable() or char in (' ', '\t')):
      return char
  return None


# -- Hardcoded QWERTY fallback (used when XKB is unavailable) -------------

EVDEV_QWERTY_MAP = None


def _build_qwerty_fallback():
  global EVDEV_QWERTY_MAP
  from evdev import ecodes
  m = {}
  for c in 'abcdefghijklmnopqrstuvwxyz':
    m[getattr(ecodes, f'KEY_{c.upper()}')] = c
  for d in '1234567890':
    m[getattr(ecodes, f'KEY_{d}')] = d
  for ecode_name, char in {
      'MINUS': '-', 'EQUAL': '=', 'LEFTBRACE': '[', 'RIGHTBRACE': ']',
      'SEMICOLON': ';', 'APOSTROPHE': "'", 'GRAVE': '`', 'BACKSLASH': '\\',
      'COMMA': ',', 'DOT': '.', 'SLASH': '/', 'SPACE': ' ', 'TAB': '\t',
  }.items():
    code = getattr(ecodes, f'KEY_{ecode_name}', None)
    if code is not None:
      m[code] = char
  EVDEV_QWERTY_MAP = m


SHIFT_SYMBOL_MAP = {
    '1': '!', '2': '@', '3': '#', '4': '$', '5': '%',
    '6': '^', '7': '&', '8': '*', '9': '(', '0': ')',
    '-': '_', '=': '+', '[': '{', ']': '}', '\\': '|',
    ';': ':', "'": '"', '`': '~', ',': '<', '.': '>',
    '/': '?',
}


# -- Main translation entry point ----------------------------------------

def _evdev_translate(code, shift_held):
  """Translate an evdev key code to the string key name used by this script."""
  if EVDEV_MODIFIER_MAP is None:
    _build_evdev_maps()

  # Modifiers — always return named string
  if code in EVDEV_MODIFIER_MAP:
    return EVDEV_MODIFIER_MAP[code]

  # Named keys (enter, backspace, arrows, Fn, …)
  if code in EVDEV_NAMED_KEY_MAP:
    return EVDEV_NAMED_KEY_MAP[code]

  # Character keys — try XKB first (layout-aware)
  char = _xkb_translate(code, shift_held)
  if char is not None:
    return char

  # Fallback to hardcoded QWERTY map
  if EVDEV_QWERTY_MAP is None:
    _build_qwerty_fallback()
  name = EVDEV_QWERTY_MAP.get(code)
  if name is None:
    return None
  if shift_held and len(name) == 1 and name.isalpha():
    return name.upper()
  if shift_held and name in SHIFT_SYMBOL_MAP:
    return SHIFT_SYMBOL_MAP[name]
  return name


RESCAN_INTERVAL = 10  # seconds between device rescan checks


def main_linux():
  import selectors
  import time
  import evdev
  from evdev import categorize, ecodes

  # Initialise XKB for layout-aware key translation
  if _init_xkb():
    _start_layout_monitor()

  def scan_keyboards():
    """Return a dict of {path: InputDevice} for all EV_KEY-capable devices."""
    devices = [evdev.InputDevice(path) for path in evdev.list_devices()]
    return {
        dev.path: dev for dev in devices
        if ecodes.EV_KEY in dev.capabilities()
    }

  sel = selectors.DefaultSelector()
  monitored = {}  # path -> InputDevice

  def rescan():
    """Add new devices and remove stale ones."""
    current = scan_keyboards()
    # Remove devices that disappeared
    for path in list(monitored):
      if path not in current:
        logging.info(f'device removed: {path} ({monitored[path].name})')
        sel.unregister(monitored[path])
        monitored[path].close()
        del monitored[path]
    # Add new devices
    for path, dev in current.items():
      if path not in monitored:
        logging.info(f'monitoring keyboard: {path} ({dev.name})')
        sel.register(dev, selectors.EVENT_READ)
        monitored[path] = dev

  rescan()

  if not monitored:
    logging.error(
        'No keyboard devices found. '
        'Make sure your user is in the "input" group: '
        'sudo usermod -aG input $USER  (then re-login)'
    )
    sys.exit(1)

  logging.info('starting to listen for keyboard events')
  last_rescan = time.monotonic()
  # Track scancode → key name for reliable key-up matching across layout
  # switches (e.g. key pressed in Russian, released after switching to
  # English)
  down_scancodes = {}  # scancode -> key_name stored on key-down
  try:
    while True:
      # Rescan for new/removed devices periodically
      now = time.monotonic()
      if now - last_rescan >= RESCAN_INTERVAL:
        rescan()
        last_rescan = now

      ready = sel.select(timeout=RESCAN_INTERVAL)
      for selector_key, _ in ready:
        device = selector_key.fileobj
        try:
          events = device.read()
        except OSError:
          # Device was disconnected
          path = device.path
          logging.info(f'device disconnected: {path} ({device.name})')
          sel.unregister(device)
          device.close()
          monitored.pop(path, None)
          continue
        for event in events:
          if event.type != ecodes.EV_KEY:
            continue
          key_event = categorize(event)
          # value: 1 = down, 0 = up, 2 = hold/repeat
          if key_event.event.value == 2:
            continue  # ignore key repeats

          # Check if shift is currently held (for symbol translation)
          shift_held = any(
              k in ('shift', 'shift_l', 'shift_r')
              for k in keys_currently_down
          )

          if key_event.event.value == 1:  # key down
            key_name = _evdev_translate(
                key_event.scancode,
                shift_held,
            )
            if key_name is None:
              continue
            down_scancodes[key_event.scancode] = key_name
            preprocess(key_name, key_down)

          elif key_event.event.value == 0:  # key up
            # Use the stored name from key-down so layout switches
            # between press and release don't cause mismatches
            up_name = down_scancodes.pop(key_event.scancode, None)
            if up_name is None:
              up_name = _evdev_translate(key_event.scancode, False)
            if up_name is None:
              continue
            # If the stored name isn't in keys_currently_down (e.g.
            # because it was remapped), try the remapped version
            if up_name not in keys_currently_down:
              remapped = REMAP.get(up_name, up_name)
              if remapped in keys_currently_down:
                up_name = remapped
            preprocess(up_name, key_up)
  except KeyboardInterrupt:
    logging.info('stopping listener (keyboard interrupt)')
  finally:
    sel.close()


if __name__ == '__main__':
  main()
