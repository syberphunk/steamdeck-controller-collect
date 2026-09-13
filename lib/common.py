"""Shared facts and helpers: USB detection, host identification, naming.

Imported by detect.py, dump_usb.py and pack.py so that all three agree on what
a given controller is and what the resulting files are called.
"""

import datetime
import hashlib
import os
import re
import subprocess
import sys
import time
import zlib

FW_DIR = '/usr/share/jupiter_controller_fw_updater'

VALVE_VID = 0x28de
PID_APP = 0x1205
PID_BOOTLOADER = 0x1004
PID_APP_LEGACY = 0x1204          # pre-release units, 8 KB bootloader
PID_BOOTLOADER_LEGACY = 0x1003

# Renesas Standard Boot Firmware, used by the Type 3 ROM path.
RA_BOOT_VID = 0x045b
RA_BOOT_PID = 0x0261

COLLECTOR_VERSION = '1.0.2'


# --------------------------------------------------------------- transcript
# Everything printed is also kept in memory so the archive carries a record of
# the run. Progress bars are excluded: they are redrawn in place and would
# otherwise fill the log with near-identical lines.
TRANSCRIPT = []


def log_line(text):
    for line in str(text).rstrip('\n').split('\n'):
        TRANSCRIPT.append(f'{time.strftime("%H:%M:%S")}  {line}')


def say(msg=''):
    print(msg)
    log_line(msg)
    sys.stdout.flush()


def hr(ch='='):
    say(ch * 78)


def write_transcript(path, header):
    with open(path, 'w') as fh:
        fh.write(header.rstrip('\n') + '\n')
        fh.write('=' * 78 + '\n')
        fh.write('\n'.join(TRANSCRIPT) + '\n')


# ------------------------------------------------------------------ helpers
def valve_crc(data):
    """Reflected CRC-32, poly 0x04C11DB7, init 0, no final xor.

    The form Valve's bootloader and data-flash records use.
    """
    return zlib.crc32(data, 0xFFFFFFFF) ^ 0xFFFFFFFF


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for blk in iter(lambda: fh.read(1 << 20), b''):
            h.update(blk)
    return h.hexdigest()


def strip_ff(b):
    """ASCII out of a fixed-width field padded with 0xFF or NUL."""
    try:
        return b.rstrip(b'\xff').rstrip(b'\x00').split(b'\x00')[0].decode('ascii')
    except (UnicodeDecodeError, AttributeError):
        return ''


def slug(text, maxlen=40, keep=''):
    """Reduce text to something safe to put in a filename.

    `keep` adds characters to the permitted set; version numbers pass '.' so
    they stay readable as 3.7.13 rather than 3-7-13.
    """
    allowed = 'A-Za-z0-9' + re.escape(keep)
    s = re.sub(f'[^{allowed}]+', '-', str(text or '')).strip('-' + keep)
    return s[:maxlen].strip('-' + keep)


def read_first_line(path):
    try:
        with open(path, 'r') as fh:
            return fh.read().strip()
    except OSError:
        return None


# ----------------------------------------------------------- variant catalog
TYPE_NAMES = {
    1: 'Type 1 - D21_D21, two SAMD21, 16 KB bootloader',
    2: 'Type 2 - D2x_D21, SAMD21 primary plus SAMD20 or SAMD21 secondary',
    3: 'Type 3 - RA4, single Renesas RA4E1',
}

# Hardware ID -> (board description, primary chip, secondary chip or None).
# IDs 26-32 are listed in /usr/bin/jupiter-controller-update and the HW_ID_*
# constants in d20bootloader.py. 41 and 46 are Renesas boards observed on
# hardware; Valve's table documents no RA4 IDs, and 41 shows the RA4 is not
# OLED-only - it also shipped in later LCD units.
HWID_BOARDS = {
    26: ('Steam Deck EV2 engineering unit (Jupiter)', 'samd21', 'samd21'),
    27: ('Steam Deck, original D21/D21 board (Jupiter)', 'samd21', 'samd21'),
    29: ('hybrid board, SAMD20 side (Jupiter)', 'samd20', None),
    30: ('Steam Deck LCD, hybrid SAMD21 + SAMD20 board (Jupiter)',
         'samd21', 'samd20'),
    31: ('Steam Deck LCD, homogeneous SAMD21 + SAMD21 board (Jupiter)',
         'samd21', 'samd21'),
    32: ('Steam Deck OLED, homogeneous SAMD21 + SAMD21 board (Galileo)',
         'samd21', 'samd21'),
    41: ('Steam Deck LCD, Renesas RA4 board (Jupiter)', 'ra4e1', None),
    46: ('Steam Deck OLED, Renesas RA4 board (Galileo)', 'ra4e1', None),
}


def layout_for(major, legacy_pid=False):
    """Flash layout per bootloader type.

    Tabulated here rather than read back from Valve's bootloader objects: the
    attributes are absent on some vintages of d20bootloader.py and unset on
    some constructor paths.
    """
    if major == 3:                                       # RA4, 32 KB bootloader
        return {
            'flash_start': 0x0,
            'flash_size': 256 * 1024,
            'app_start': 0x8000,
            'app_end': 0x40000,
            'info_offset': 0x0800_0000,
            'blob_offset': 0x0800_0100,
            # RA4E1 data flash is 8 KB. Valve's tooling uses only the first
            # 4 KB; scan the whole region and let the readability map find the
            # edge the device actually enforces.
            'data_flash': (0x0800_0000, 0x0800_2000),
        }
    app_start = 0x2000 if legacy_pid else 0x4000         # SAMD21, 8 or 16 KB BL
    return {
        'flash_start': 0x0,
        'flash_size': 256 * 1024,
        'app_start': app_start,
        'app_end': 0x3F000,
        'info_offset': 0x3F000,
        'blob_offset': 0x3F100,
        'data_flash': None,
    }


def identify(major, hwid, dmi_board):
    """(board description, primary chip, secondary chip). Never guesses silently.

    `hwid` is None before anything has read the device-info partition, which
    only comes back during the capture pass - detection on its own does not
    put the controller into a mode where it can be asked. Say so rather than
    printing a bare "None" at the operator.
    """
    known = HWID_BOARDS.get(hwid)
    hwid_note = (f'hardware ID {hwid}' if hwid is not None
                 else 'hardware ID not read yet')
    # Accept the table entry only if its chip family agrees with the USB type;
    # a mismatch means the ID has been reused and the table would mislead.
    if known and (major != 3) == (known[1] != 'ra4e1'):
        return known

    if major == 3:
        board = f'Renesas RA4 controller board, {hwid_note}'
        if dmi_board:
            board += f', in a {dmi_board}-class Deck'
        return board, 'ra4e1', None

    if known:
        return known

    generic = {1: 'SAMD21 + SAMD21', 2: 'SAMD21 + SAMD20 or SAMD21'}.get(
        major, 'unknown')
    if hwid is None:
        return (f'Type {major} board ({generic}), {hwid_note}',
                'samd21', 'samd21' if major == 1 else 'samdxx')
    return (f'unrecorded hardware ID {hwid} on a Type {major} board '
            f'({generic}) - please report this',
            'samd21', 'samd21' if major == 1 else 'samdxx')


def bootloader_source(major):
    """Where the bootloader region can be read from, for this type.

    Both answers are over USB - the controller is an internal USB device and
    the Renesas boot ROM enumerates as one too. The difference is which
    interface will serve 0x0 upward.

    SAMD parts serve it on Valve's debug-read path, so one pass captures
    bootloader and application together. The RA4 refuses 0x0-0x8000 there; its
    bootloader is only readable from the chip's own boot ROM, entered with a
    physical button combination, which is a second attended pass.
    """
    if major == 3:
        return 'rom'
    return 'usb'


# ------------------------------------------------------------ USB detection
def probe_usb():
    """Read the USB descriptors. Opens no device and changes nothing.

    The release number's major byte is what Valve's updater switches on to
    choose a tool, and it is readable while the controller runs normally.
    """
    out = {'error': None, 'interfaces': [], 'release_number': None,
           'major': None, 'in_bootloader': False, 'legacy_pid': False}
    try:
        import hid
    except Exception as exc:                                    # noqa: BLE001
        out['error'] = f'{type(exc).__name__}: {exc}'
        return out

    found = []
    for pid in (PID_APP, PID_BOOTLOADER, PID_APP_LEGACY, PID_BOOTLOADER_LEGACY):
        try:
            for d in hid.enumerate(VALVE_VID, pid):
                rec = {}
                for k, v in d.items():
                    if isinstance(v, bytes):
                        v = v.decode('utf-8', 'replace')
                    elif not isinstance(v, (int, float, str, type(None))):
                        v = str(v)
                    rec[k] = v
                found.append(rec)
        except Exception as exc:                                # noqa: BLE001
            out['error'] = f'{type(exc).__name__}: {exc}'

    out['interfaces'] = found
    if not found:
        return out

    pids = {r.get('product_id') for r in found}
    out['in_bootloader'] = bool(pids & {PID_BOOTLOADER, PID_BOOTLOADER_LEGACY})
    out['legacy_pid'] = bool(pids & {PID_APP_LEGACY, PID_BOOTLOADER_LEGACY})

    # Prefer the application interface: the bootloader reports the same major
    # byte, but the app is the interface Valve's updater reads.
    for want in (PID_APP, PID_BOOTLOADER, PID_APP_LEGACY, PID_BOOTLOADER_LEGACY):
        for r in found:
            if r.get('product_id') == want and r.get('release_number'):
                out['release_number'] = r['release_number']
                out['major'] = r['release_number'] >> 8
                return out
    return out


def usb_present(vid, pid):
    """True if vid:pid is on the bus, by sysfs walk.

    Reads sysfs rather than enumerating serial ports: pyserial's comports()
    parses idVendor unguarded and raises if a device disappears mid-scan,
    which happens routinely while the controller board is being power-cycled.
    """
    root = '/sys/bus/usb/devices'
    want_v, want_p = '%04x' % vid, '%04x' % pid
    try:
        names = os.listdir(root)
    except OSError:
        return False
    for name in names:
        dev = os.path.join(root, name)
        try:
            with open(os.path.join(dev, 'idVendor')) as f:
                if f.read().strip().lower() != want_v:
                    continue
            with open(os.path.join(dev, 'idProduct')) as f:
                if f.read().strip().lower() == want_p:
                    return True
        except OSError:
            continue
    return False


# --------------------------------------------------------------- host facts
def host_info():
    """Model, OS and firmware-package identification for the Deck itself."""
    dmi = {}
    for key in ('board_name', 'board_vendor', 'product_name', 'sys_vendor',
                'bios_version', 'product_version'):
        dmi[key] = read_first_line(f'/sys/class/dmi/id/{key}')

    # Full serial. It names the unit, and the point of this archive is to be
    # able to tie a controller's contents to the Deck it came out of. See
    # PRIVACY.md.
    dmi['product_serial'] = read_first_line('/sys/class/dmi/id/product_serial')
    dmi['board_serial'] = read_first_line('/sys/class/dmi/id/board_serial')

    os_release = {}
    try:
        with open('/etc/os-release') as fh:
            for line in fh:
                if '=' in line:
                    k, v = line.strip().split('=', 1)
                    os_release[k] = v.strip('"')
    except OSError:
        pass

    pkg = None
    try:
        pkg = subprocess.run(['pacman', '-Q', 'jupiter-hw-support'],
                             capture_output=True, text=True,
                             timeout=15).stdout.strip() or None
    except Exception:                                           # noqa: BLE001
        pass

    uname = os.uname()
    return {
        'dmi': dmi,
        'os_release': {k: os_release.get(k) for k in
                       ('NAME', 'VERSION_ID', 'BUILD_ID', 'VARIANT_ID')},
        'kernel': f'{uname.sysname} {uname.release}',
        'jupiter_hw_support': pkg,
        'python': sys.version.split()[0],
        'collector_version': COLLECTOR_VERSION,
        'collected': datetime.datetime.now().isoformat(timespec='seconds'),
    }


def fw_updater_inventory():
    """What firmware this Deck shipped with. Names, sizes and hashes only."""
    out = []
    try:
        for name in sorted(os.listdir(FW_DIR)):
            path = os.path.join(FW_DIR, name)
            if not os.path.isfile(path):
                out.append({'name': name, 'kind': 'directory'})
                continue
            try:
                out.append({'name': name, 'size': os.path.getsize(path),
                            'sha256': sha256(path)})
            except OSError:
                out.append({'name': name, 'sha256': None})
    except OSError:
        pass
    return out


def steamos_tag(host):
    """Short SteamOS identifier for filenames, e.g. 'steamos3.7.13'."""
    osr = host.get('os_release') or {}
    ver = osr.get('VERSION_ID') or osr.get('BUILD_ID')
    if not ver:
        return 'steamos-unknown'
    return 'steamos' + slug(ver, 20, keep='.')


def model_tag(host):
    """Deck model for filenames, from DMI board name (jupiter / galileo)."""
    dmi = host.get('dmi') or {}
    return slug(dmi.get('board_name') or dmi.get('product_name')
                or 'unknown-model', 24).lower()
