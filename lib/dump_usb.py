#!/usr/bin/env python3
"""Read the controller's flash over USB, through Valve's own bootloader scripts.

Read-only. The only state change made to the controller is entering bootloader
mode in order to read, and returning to the application afterwards. No flash
contents are altered.

How read-only is enforced:

 1. Valve's bootloader object is wrapped in ReadOnlyDevice, a proxy with an
    explicit allow-list. Any attempt to reach a mutating method, or to set any
    attribute, raises DestructiveCallBlocked instead of reaching the hardware.

 2. At startup the script audits its own source, and the rest of lib/, for
    calls to known-destructive APIs and refuses to run if it finds any.

 3. Output files are created with exclusive-create mode, so an existing file is
    never overwritten.

Reads that fail are recorded as gaps and reported, never silently padded: a
fabricated 0xFF byte in a firmware image is indistinguishable from erased
flash and is worse than a short file.

Tool selection follows Valve's updater, which switches on the major byte of the
USB release number:

    major 1  ->  Type 1  ->  d21bootloader16.py   D21 + D21, 16 KB bootloader
    major 2  ->  Type 2  ->  d20bootloader.py     D21 primary + D20 or D21
    major 3  ->  Type 3  ->  d20bootloader.py     Renesas RA4, single MCU

The release number is read from hid.enumerate() rather than from an attribute
of Valve's objects: copies of d20bootloader.py predating 2022-07 have neither
DeviceType nor self.device_type, and touching them raises AttributeError.
"""

import argparse
import datetime
import io
import json
import os
import re
import struct
import sys
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import (COLLECTOR_VERSION, FW_DIR, TYPE_NAMES,  # noqa: E402
                    HWID_BOARDS, fw_updater_inventory, host_info, hr,
                    identify, layout_for, probe_usb, read_first_line, say,
                    sha256, strip_ff, valve_crc, write_transcript)

# d20bootloader.read_32b() is fixed at 32 bytes.
#
# d21bootloader16._read_debug_data() does not stream either. Its `size`
# argument only caps how much of the reply it accepts; the firmware serves
# exactly one 32-byte report per request (the command is
# DEBUG_READ_32B_THIS/_OTHER), then answers the next _get_feature_report with
# len_ == 0 and the loop breaks. Valve's own read_blob() therefore loops
# `for off in range(0, size, 32)`. Asking for more than 32 silently returns 32,
# and the surplus would be invented bytes.
BLOCK = 32

# A patch of unreadable flash must not throw away the rest of the dump - the
# regions of interest are at both ends of the address space. Only give up if
# the device has clearly stopped answering, or the run is taking absurdly long.
MAX_CONSECUTIVE_FAILS = 256
TIME_BUDGET_S = 30 * 60

# A refused read is a stalled control transfer, not a timeout, so it returns
# immediately and retrying is cheap. On the RA4 a single refused transaction
# can disturb the following one, so one stray failure must not condemn an
# address.
READ_RETRIES = 3

# Coarse readability scan before committing to a long dump: one read per 4 KB
# covers 256 KB in 64 transactions.
SCAN_STEP = 4096

# How many offsets to re-read afterwards and compare against the dump. The D20
# read path returns a fixed-size slice with no error signalling, so a silent
# mis-read cannot be caught at read time - only by reading the same address
# twice.
VERIFY_SAMPLES = 64


def bail(msg, code=1):
    say()
    hr()
    say('  COULD NOT CONTINUE')
    hr()
    say()
    say(msg)
    say()
    say('Nothing was changed on your Deck.')
    sys.exit(code)


# ------------------------------------------------------------------ safeguard
class DestructiveCallBlocked(RuntimeError):
    """Raised if anything tries to reach a mutating API on the controller."""


# Anything on Valve's objects that can change the device. The proxy is
# allow-list based, so this set is a second line of defence rather than the
# only one.
FORBIDDEN = frozenset({
    'erase', 'erase_row', 'erase_partition', 'erase_all', 'chip_erase',
    'write_32b', 'write', 'upload', 'upload_blob', 'upload_firmware',
    'upload_mte_blob', 'do_crc_fixup', 'addcrc', 'set_singleton_mode',
    'set_force_crc_check', 'program', 'flash', 'update', 'set_hwid',
    'set_board_serial', 'set_unit_serial', 'reset_to_factory',
    'update_row', 'update_partition', 'update_crc', 'write_row',
    'write_partition', 'set_mte_blob', 'info', 'mte_blob', 'send',
    '_send_data', '_send_feature_report', '_complete_update',
})

# Names distinctive enough to grep the source for. Generic words such as
# write/update/flash are excluded: `fh.write(...)` on an output file is not a
# device call, and an audit that cries wolf is an audit nobody trusts.
AUDIT_NAMES = frozenset({
    'erase', 'erase_row', 'erase_partition', 'erase_all', 'chip_erase',
    'write_32b', 'upload_blob', 'upload_firmware', 'upload_mte_blob',
    'do_crc_fixup', 'addcrc', 'set_singleton_mode', 'set_force_crc_check',
    'set_hwid', 'set_board_serial', 'set_unit_serial', 'reset_to_factory',
    'update_row', 'update_partition', 'update_crc', 'write_row',
    'write_partition', 'set_mte_blob', '_send_data', '_complete_update',
})


class ReadOnlyDevice:
    """Proxy exposing only the handful of harmless calls this tool needs.

    Everything else - including anything added to Valve's scripts in future -
    is refused before it can reach the hardware. Attribute assignment is
    refused outright, which blocks the property setters (hardware_id,
    board_serial, ...) that would rewrite device identity.
    """

    ALLOWED = frozenset({
        # reading flash
        'read_32b', '_read_debug_data',
        # lifecycle: bootloader entry is how reading works, reboot returns the
        # controller to its normal application afterwards
        'reboot', 'close',
        # read-only descriptive attributes
        'device_type', 'mcu',
        'FLASH_SIZE', 'FLASH_END', 'APP_FW_START', 'APP_FW_END',
        'APP_FW_INFO', 'APP_FW_LENGTH', 'INFO_OFFSET', 'BLOB_OFFSET',
        'DATA_FLASH_START', 'DATA_FLASH_END',
        'hardware_id', 'board_serial', 'unit_serial', 'bootloader_reason',
        'bl_firmware_build_time', 'firmware_build_time',
        'unique_id', 'user_row', 'state',
    })

    def __init__(self, obj):
        object.__setattr__(self, '_obj', obj)

    # __getattribute__, not __getattr__: the latter is consulted only when
    # normal lookup fails, which would leave `proxy.__dict__` handing out the
    # raw unguarded device object.
    def __getattribute__(self, name):
        if name == '__class__':                 # keep isinstance/repr sane
            return object.__getattribute__(self, name)
        if name in FORBIDDEN or name not in ReadOnlyDevice.ALLOWED:
            raise DestructiveCallBlocked(
                f'refused access to {name!r}: this tool is read-only')
        return getattr(object.__getattribute__(self, '_obj'), name)

    def __setattr__(self, name, value):
        raise DestructiveCallBlocked(
            f'refused to set {name!r}: this tool never modifies the device')

    def __delattr__(self, name):
        raise DestructiveCallBlocked('refused: this tool never modifies the device')


def self_audit():
    """Refuse to run if any script in lib/ calls a destructive API.

    Covers the whole directory, not just this file, so that splitting code out
    into a helper module cannot route around the check. vendor/ is excluded:
    it is an unmodified third-party serial library.
    """
    libdir = os.path.dirname(os.path.abspath(__file__))
    checked = 0
    for root, dirs, files in os.walk(libdir):
        dirs[:] = [d for d in dirs if d not in ('vendor', '__pycache__')]
        for fn in sorted(files):
            if not fn.endswith('.py'):
                continue
            try:
                with open(os.path.join(root, fn), 'r') as fh:
                    src = fh.read()
            except OSError:
                continue        # an unreadable file is not a reason to stop
            checked += 1
            for name in sorted(AUDIT_NAMES):
                if re.search(r'\.\s*' + re.escape(name) + r'\s*\(', src):
                    bail('SAFETY CHECK FAILED.\n\n'
                         f'{fn} appears to contain a call to "{name}", which '
                         'could modify\nyour controller. Nothing has been '
                         'done.\n\nPlease report this to whoever supplied '
                         'these scripts.')
    say(f'Self-audit clean: {checked} source files, none can erase or write.')


# --------------------------------------------------------------------- loading
def load_module(path, name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class Backend:
    """One MCU, reachable through whichever of Valve's scripts suits its type."""

    def __init__(self, kind, mod, obj, label, side, code=None):
        self.kind, self.mod, self.obj = kind, mod, obj
        self.label, self.side = label, side
        self.code = code                       # THIS/OTHER read selector
        self.chip = None                       # filled in once the HWID is known

    def read(self, offset, length=BLOCK):
        """Read `length` bytes at `offset`. Raises on failure, never fabricates."""
        last = None
        for attempt in range(READ_RETRIES):
            try:
                return self._read_once(offset, length)
            except Exception as exc:                            # noqa: BLE001
                last = exc
                # Let the device finish resynchronising before deciding the
                # address itself is at fault.
                time.sleep(0.02 * (attempt + 1))
        raise last

    def _read_once(self, offset, length):
        if self.kind == 'd20':
            data = self.obj.read_32b(offset)
        else:
            # verbose=False suppresses the progress bar, which matters because
            # there are thousands of these calls per MCU. The redirect guards
            # against a noisier build of the Valve script.
            sink = io.StringIO()
            with redirect_stderr(sink), redirect_stdout(sink):
                data = self.obj._read_debug_data(self.code, size=length,
                                                 offset=offset, verbose=False)
        if not data:
            raise IOError(f'empty read at 0x{offset:08X}')
        data = bytes(data[:length])
        # A short read is a failed read, not a partial success. Padding it
        # would manufacture bytes indistinguishable from erased flash.
        if len(data) != length:
            raise IOError(f'short read at 0x{offset:08X}: asked {length} bytes, '
                          f'got {len(data)}')
        return data

    def close(self):
        for fn in ('reboot', 'close'):
            try:
                getattr(self.obj, fn)()
            except Exception:                                   # noqa: BLE001
                pass


def open_type1(d21_path, notes, legacy_pid):
    """D21_D21. One USB endpoint, two MCUs addressed as THIS and OTHER."""
    if legacy_pid:
        # d21bootloader16.py reads these at import time, so they must be set
        # before the module is loaded.
        os.environ.setdefault('APP_FW_START', '0x2000')
        os.environ.setdefault('JUPITER_USB_PID', '0x1204')
        os.environ.setdefault('JUPITER_BOOTLOADER_USB_PID', '0x1003')
        notes.append('pre-release USB IDs detected: using the 8 KB bootloader '
                     'layout (app at 0x2000)')
    mod = load_module(d21_path, 'valve_d21')
    try:
        obj = ReadOnlyDevice(mod.DogBootloader(verbose=False))
    except TypeError:
        obj = ReadOnlyDevice(mod.DogBootloader())
    targets = [
        Backend('d21', mod, obj, 'primary-right', 'primary',
                mod.DEBUG_READ_32B_THIS),
        Backend('d21', mod, obj, 'secondary-left', 'secondary',
                mod.DEBUG_READ_32B_OTHER),
    ]
    say('  [ok] both MCUs opened via d21bootloader16.py')
    notes.append('read through d21bootloader16.py (Type 1 path)')
    return targets


def open_type23(d20_path, major, notes):
    """D2x_D21 and RA4. One USB interface per MCU."""
    mod = load_module(d20_path, 'valve_d20')
    mcu_enum = getattr(mod, 'DogBootloaderMCU', None)
    if mcu_enum is None:
        raise RuntimeError('this copy of d20bootloader.py has no '
                           'DogBootloaderMCU')

    primary = ReadOnlyDevice(mod.DogBootloader(mcu=mcu_enum.PRIMARY))
    label = 'primary-single' if major == 3 else 'primary-right'
    targets = [Backend('d20', mod, primary, label, 'primary')]
    say(f'  [ok] {label} opened via d20bootloader.py')

    if major == 3:
        notes.append('read through d20bootloader.py (Type 3 path); this '
                     'generation has a single MCU')
        return targets

    # A secondary exists on D2x_D21 only. Valve raises NotSupported otherwise,
    # and opening it anyway yields a bogus duplicate of the primary.
    try:
        sec = ReadOnlyDevice(mod.DogBootloader(mcu=mcu_enum.SECONDARY))
        targets.append(Backend('d20', mod, sec, 'secondary-left', 'secondary'))
        say('  [ok] secondary-left opened via d20bootloader.py')
    except Exception as exc:                                    # noqa: BLE001
        say(f'  [--] secondary unavailable ({type(exc).__name__})')
        notes.append(f'secondary MCU could not be opened: {type(exc).__name__}')
    notes.append('read through d20bootloader.py (Type 2 path)')
    return targets


def open_targets(usb, notes):
    """Open every MCU that can legitimately be opened, choosing tool by type."""
    d20_path = os.path.join(FW_DIR, 'd20bootloader.py')
    d21_path = os.path.join(FW_DIR, 'd21bootloader16.py')
    major = usb.get('major')

    if major in (1, 2, 3):
        say(f'  [ii] {TYPE_NAMES[major]}')
    elif usb.get('release_number'):
        say(f'  [--] unrecognised USB release number '
            f'0x{usb["release_number"]:04X}; trying both tools')
        notes.append(f'unrecognised USB release number '
                     f'0x{usb["release_number"]:04X}')
    else:
        say('  [--] could not read the USB release number; trying both tools')
        notes.append('USB release number unreadable: '
                     + (usb.get('error') or 'no controller interfaces found'))

    # Valve's rule first, then the other tool as a fallback.
    attempts = []
    if major == 1 and os.path.exists(d21_path):
        attempts.append(('d21', d21_path))
    if major in (2, 3) and os.path.exists(d20_path):
        attempts.append(('d20', d20_path))
    if not attempts:
        if os.path.exists(d21_path):
            attempts.append(('d21', d21_path))
        if os.path.exists(d20_path):
            attempts.append(('d20', d20_path))
    if os.path.exists(d21_path) and not any(k == 'd21' for k, _ in attempts):
        attempts.append(('d21', d21_path))
    if os.path.exists(d20_path) and not any(k == 'd20' for k, _ in attempts):
        attempts.append(('d20', d20_path))

    if not attempts:
        notes.append(f'neither bootloader script is present in {FW_DIR}')
        return [], major

    for kind, path in attempts:
        try:
            if kind == 'd21':
                return open_type1(path, notes, usb.get('legacy_pid', False)), \
                    (major or 1)
            return open_type23(path, major or 2, notes), (major or 2)
        except Exception as exc:                                # noqa: BLE001
            script = os.path.basename(path)
            say(f'  [--] {script} could not open the controller '
                f'({type(exc).__name__}: {exc})')
            notes.append(f'{script} failed: {type(exc).__name__}: {exc}')

    return [], major


# --------------------------------------------------- known-unserved regions
# Ranges a board family is known in advance not to serve on the interface its
# own firmware provides. Presentation only - nothing here changes what is read
# or what the manifest records. It exists so a run that goes exactly to plan
# does not report itself in the language of failure.
EXPECTED_UNSERVED = []


def set_expected_unserved(major, lay):
    EXPECTED_UNSERVED.clear()
    if major != 3:
        return
    EXPECTED_UNSERVED.append((
        lay['flash_start'], lay['app_start'],
        'the bootloader, which only the boot ROM hands over'))
    if lay['data_flash']:
        start, end = lay['data_flash']
        EXPECTED_UNSERVED.append((
            start + 0x1000, end,
            'the upper half of data flash, which this interface does not expose'))


def expected_refusal(start, end):
    """Why this range was always going to be refused, or None if unexpected."""
    for s, e, why in EXPECTED_UNSERVED:
        if start >= s and end <= e:
            return why
    return None


# ---------------------------------------------------------- pre-flight probing
def readable(backend, off):
    try:
        backend.read(off)
        return True
    except Exception:                                           # noqa: BLE001
        return False


def find_edge(backend, lo, hi, lo_ok):
    """Bisect to the exact 32-byte boundary between two addresses of opposite
    readability. `lo_ok` is the readability at `lo`; `hi` is the other kind.
    Returns the lowest address that behaves like `hi`."""
    while hi - lo > BLOCK:
        mid = ((lo + hi) // 2) // BLOCK * BLOCK
        if mid <= lo:
            mid = lo + BLOCK
        if mid >= hi:
            break
        if readable(backend, mid) == lo_ok:
            lo = mid
        else:
            hi = mid
    return hi


def map_regions(backend, start, end, label):
    """Coarse-scan the address space, then bisect the edges.

    Some controllers refuse whole ranges - on the RA4 the bootloader area is
    refused as a matter of course - and reading a refused range 32 bytes at a
    time wastes minutes and produces nothing. Sampling every 4 KB finds the
    shape in seconds, and bisecting each transition pins the boundary exactly,
    so the dump reads what is readable and reports the rest precisely.
    """
    samples = []
    off = start
    while off < end:
        samples.append((off, readable(backend, off)))
        off += SCAN_STEP
    # Always test the final block: a range ending mid-step would otherwise be
    # judged by a sample that is not inside it.
    tail = end - BLOCK
    if samples and tail > samples[-1][0]:
        samples.append((tail, readable(backend, tail)))

    spans = []
    for off, ok in samples:
        if spans and spans[-1][2] == ok:
            spans[-1][1] = min(off + SCAN_STEP, end)
        else:
            spans.append([off, min(off + SCAN_STEP, end), ok])
    if spans:
        spans[-1][1] = end

    for i in range(1, len(spans)):
        edge = find_edge(backend, spans[i - 1][0], spans[i][0], spans[i - 1][2])
        spans[i - 1][1] = edge
        spans[i][0] = edge

    for s, e, ok in spans:
        if ok:
            note = 'readable'
        else:
            # A refusal that was predicted from the board type is the normal
            # answer, not a fault. Shouting REFUSED at every one of them makes
            # a textbook run look like a failing one.
            why = expected_refusal(s, e)
            note = f'not served here - {why}' if why else 'REFUSED by the controller'
        say(f'   {label} 0x{s:08X}-0x{e:08X}  {note}')
    return [tuple(s) for s in spans]


# --------------------------------------------------------------------- the dump
def merge_gaps(gaps):
    out = []
    for off, n in sorted(gaps):
        if out and out[-1][0] + out[-1][1] >= off:
            out[-1] = (out[-1][0],
                       max(out[-1][0] + out[-1][1], off + n) - out[-1][0])
        else:
            out.append((off, n))
    return out


def dump_region(backend, start, end, label, depth=0):
    """Map the region, then read every part of it the controller will serve."""
    spans = map_regions(backend, start, end, label)
    buf = bytearray(b'\xff' * (end - start))
    gaps = []
    result = []
    for s, e, ok in spans:
        if not ok:
            gaps.append((s, e - s))
            result.append((s, e, False))
            continue
        data, g = dump_range(backend, s, e, label)
        buf[s - start:s - start + len(data)] = data
        gaps.extend(g)
        if len(data) >= e - s:
            result.append((s, e, True))
            continue
        # The span stopped early. A coarse scan can miss a refused patch that
        # starts mid-step, so re-map what is left rather than writing off the
        # remainder: on a controller that refuses one region, the regions after
        # it are usually fine.
        rest = s + len(data)
        if rest > s:
            result.append((s, rest, True))
        if depth >= 3:
            gaps.append((rest, e - rest))
            result.append((rest, e, False))
            continue
        say(f'   re-checking 0x{rest:08X}-0x{e:08X} after a run of refusals...')
        sub, sub_gaps, sub_spans = dump_region(backend, rest, e, label, depth + 1)
        buf[rest - start:e - start] = sub
        gaps.extend(sub_gaps)
        result.extend(sub_spans)
    return bytes(buf), merge_gaps(gaps), sorted(result)


def dump_range(backend, start, end, description):
    """Returns (data, gaps). Failed reads become recorded gaps, not fake 0xFF."""
    size = end - start
    data = bytearray()
    gaps = []
    consecutive = 0
    t0 = time.time()
    off = start
    last_draw = 0.0

    while off < end:
        n = min(BLOCK, end - off)
        try:
            chunk = backend.read(off, n)
            consecutive = 0
        except Exception:                                       # noqa: BLE001
            consecutive += 1
            # Merge with the previous gap if adjacent, so a long bad patch is
            # one entry rather than hundreds.
            if gaps and gaps[-1][0] + gaps[-1][1] == off:
                gaps[-1] = (gaps[-1][0], gaps[-1][1] + n)
            else:
                gaps.append((off, n))
            chunk = b'\xff' * n            # placeholder, but recorded in gaps
        # backend.read() guarantees exactly n bytes or raises, and the failure
        # placeholder above is exactly n. Anything else is a bug here, and
        # padding it would forge flash contents.
        if len(chunk) != n:
            raise AssertionError(f'internal error: {len(chunk)} bytes for a '
                                 f'{n}-byte read at 0x{off:08X}')
        data += chunk
        off += n

        if consecutive > MAX_CONSECUTIVE_FAILS:
            say(f'\n   long run of refused reads from 0x{off:08X}.')
            break
        if time.time() - t0 > TIME_BUDGET_S:
            say(f'\n   taking too long; stopping at 0x{off:08X} and keeping '
                f'what we have.')
            break

        now = time.time()
        if now - last_draw > 0.5 or off >= end:
            last_draw = now
            done = off - start
            pct = 100.0 * done / max(size, 1)
            rate = done / max(now - t0, 0.001)
            eta = (size - done) / max(rate, 1)
            bars = int(pct / 2.5)
            sys.stdout.write(f'\r   {description} [{"#" * bars}{"." * (40 - bars)}]'
                             f' {pct:5.1f}%  about {eta / 60:4.1f} min left   ')
            sys.stdout.flush()
    print()
    return bytes(data), gaps


def verify_sample(backend, data, start, gaps):
    """Re-read scattered offsets and compare, to catch silent mis-reads.

    The D20 read path hands back a fixed-size slice whether or not the device
    understood the request, so a bad read cannot be detected at read time. It
    can be detected by reading the same address twice.
    """
    if len(data) < BLOCK:
        return {'checked': 0, 'mismatched': 0, 'errors': 0, 'offsets': []}
    bad, err, checked, examples = 0, 0, 0, []
    span = (len(data) // BLOCK) - 1
    stride = max(1, span // max(VERIFY_SAMPLES - 1, 1))
    for i in range(0, span + 1, stride):
        off = start + i * BLOCK
        if any(o <= off < o + n for o, n in gaps):
            continue
        try:
            again = backend.read(off)
        except Exception:                                       # noqa: BLE001
            err += 1
            continue
        checked += 1
        if again != data[i * BLOCK:i * BLOCK + BLOCK]:
            bad += 1
            if len(examples) < 8:
                examples.append(f'0x{off:08X}')
    return {'checked': checked, 'mismatched': bad, 'errors': err,
            'offsets': examples}


# --------------------------------------------------------------------- analysis
def app_crc_check(data, lay):
    """Recompute the stored application CRC exactly as the bootloader does."""
    info_off = lay['app_end'] - 4
    if len(data) < info_off + 4:
        return None
    stored = struct.unpack_from('<I', data, info_off)[0]
    body = data[lay['app_start']:info_off]
    calc = valve_crc(body + b'\xff' * ((info_off - lay['app_start']) - len(body)))
    return {'stored': f'0x{stored:08X}', 'computed': f'0x{calc:08X}',
            'valid': stored == calc}


def parse_devinfo(block):
    """The 256-byte identity partition. Same layout on every variant seen."""
    if len(block) < 76:
        return None
    crc, magic, ver, hw_id = struct.unpack_from('<IIII', block, 0)
    out = {'crc': f'0x{crc:08X}', 'magic': f'0x{magic:08X}', 'version': ver,
           'hw_id': hw_id,
           'board_serial': strip_ff(block[0x10:0x2E]),
           'unit_serial': strip_ff(block[0x2E:0x4C]),
           'crc_valid': crc == valve_crc(block[4:256].ljust(252, b'\xff'))}
    if magic != 0xBEEFFACE or ver != 1:
        out['hw_id'] = None
        out['board_serial'] = ''
        out['unit_serial'] = ''
        out['note'] = 'magic/version mismatch - block not populated'
    return out


def parse_mte_blob(block):
    """The per-unit factory blob that sits directly after the identity block."""
    if len(block) < 8:
        return None
    crc = struct.unpack_from('<I', block, 0)[0]
    present = block[4] == 0
    text = strip_ff(block[5:256])
    return {'crc': f'0x{crc:08X}', 'present': present, 'text': text,
            'nonblank_bytes': sum(1 for b in block if b != 0xFF)}


def build_times(data, lay):
    """Valve stamps BUILD_TIME_<hex> into both the bootloader and the app."""
    found = {'bootloader': None, 'app': None, 'all': []}
    for m in re.finditer(rb'BUILD_TIME_([0-9A-Fa-f]{8})', data):
        stamp = m.group(1).decode()
        found['all'].append({'offset': f'0x{m.start():08X}', 'value': stamp})
        where = 'bootloader' if m.start() < lay['app_start'] else 'app'
        if found[where] is None:
            found[where] = stamp
    return found


def vectors_ok(data, at=0):
    """True if the words at `at` look like a Cortex-M vector table."""
    if len(data) < at + 8:
        return False
    sp, reset = struct.unpack_from('<II', data, at)
    return 0x20000000 <= sp <= 0x20100000 and bool(reset & 1)


# -------------------------------------------------------------------- API facts
def collect_api_info(backend, major=None):
    """Everything the bootloader will state about itself. All best-effort.

    Nothing here is load-bearing: the identity used for naming is parsed out of
    the dump itself. This records what the device claims over USB.
    """
    idx = 0 if backend.side == 'primary' else 1
    fields = ['device_type', 'hardware_id', 'board_serial', 'unit_serial',
              'bootloader_reason', 'bl_firmware_build_time',
              'firmware_build_time', 'unique_id', 'user_row', 'state']
    # user_row reads NVMCTRL_AUX0_ADDRESS, a SAMD-only register. Valve's code
    # skips it for DeviceType.RA4; asking an RA4 for 0x00804000 gets the
    # transfer refused and can disturb the following read.
    if major == 3:
        fields.remove('user_row')
    out = {}
    for attr in fields:
        try:
            val = getattr(backend.obj, attr, None)
        except Exception as exc:                                # noqa: BLE001
            out[attr] = f'<unavailable: {type(exc).__name__}>'
            continue
        if val is None or callable(val):
            continue
        try:
            # d21bootloader16 exposes genuinely per-side values as
            # (this, other) tuples: hardware_id, board_serial,
            # bootloader_reason, unique_id, user_row, state. The scalars it
            # returns (unit_serial, firmware_build_time) come from THIS only,
            # so attributing them to the secondary would be wrong.
            if isinstance(val, tuple) and len(val) == 2 and backend.kind == 'd21':
                val = val[idx]
            elif backend.kind == 'd21' and backend.side == 'secondary':
                continue
            if attr in ('unique_id', 'user_row') and val is not None:
                if isinstance(val, (bytes, bytearray)):
                    val = val.hex().upper()
                elif isinstance(val, (list, tuple)):
                    val = ' '.join(f'{int(x):08X}' for x in val)
            if isinstance(val, int) and attr in ('bl_firmware_build_time',
                                                 'firmware_build_time'):
                out[attr] = val
                out[attr + '_hex'] = f'{val:08X}'
                out[attr + '_utc'] = datetime.datetime.fromtimestamp(
                    val, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
                continue
            out[attr] = val if isinstance(val, (int, str)) else str(val)
        except Exception as exc:                                # noqa: BLE001
            out[attr] = f'<unreadable: {type(exc).__name__}>'
    return out


# ------------------------------------------------------------------- reporting
def assessment(records):
    """State what was captured, without judging whether it is wanted."""
    if any(r['verdict'].startswith('suspect') for r in records):
        return ('WARNING: the readable part of the flash is almost entirely '
                'blank, which cannot be true of a controller that works. '
                'Something went wrong during this run.')
    if any(r['verify']['mismatched'] for r in records):
        return ('WARNING: some addresses read back differently the second '
                'time, so parts of this dump may be wrong.')

    got = [r for r in records if r['bytes']]
    if not got:
        return ('Nothing was captured - the controller refused every read. '
                'The archive still records what this controller is.')

    bits = [f'{len(got)} MCU(s) read']
    if any(r['verdict'] == 'bootloader captured' for r in got):
        bits.append('bootloader region included')
    # This line is printed at the end of the USB pass, which on an RA4 is the
    # first of two. Worded as a final tally it reads as a failed run when the
    # bootloader is about to be collected by the pass that follows.
    all_gaps = [g for r in got for g in r.get('gaps', [])]
    missing = sum(n for _, n in all_gaps)
    if any(r['verdict'].startswith('app only') for r in got):
        bits.append('bootloader left for the boot-ROM pass')
    if missing:
        if all(expected_refusal(o, o + n) for o, n in all_gaps):
            bits.append(f'{missing:,} bytes this interface does not serve')
        else:
            bits.append(f'{missing:,} bytes not captured')
    return 'Completed: ' + ', '.join(bits) + '.'


def rename_if_free(src, dst):
    if src == dst or os.path.exists(dst):
        return src
    os.rename(src, dst)
    return dst


# ------------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description='Read controller flash over USB.')
    ap.add_argument('--outdir', required=True,
                    help='directory to write firmware images and metadata into')
    args = ap.parse_args()

    self_audit()

    if os.geteuid() != 0:
        bail('This must run as root to talk to the controller over USB.\n'
             'Run collect.sh, which handles that.')

    outdir = os.path.abspath(args.outdir)
    os.makedirs(outdir, exist_ok=True)

    if not os.path.isdir(FW_DIR):
        bail('This does not look like a Steam Deck.\n'
             f'Expected to find: {FW_DIR}')

    notes = []
    say('Looking at the controller over USB...')
    usb = probe_usb()
    if usb['error']:
        notes.append(f'USB enumeration problem: {usb["error"]}')

    say()
    say('Connecting to the controller...')
    say('(The controller goes quiet while it is read. It comes back at the end.)')
    say()

    try:
        targets, major = open_targets(usb, notes)
    except Exception:                                           # noqa: BLE001
        traceback.print_exc()
        targets, major = [], usb.get('major')
        notes.append('unexpected error opening the controller')

    if not targets:
        bail('Could not talk to the controller boards.\n\n'
             'Things to check:\n'
             "  * Is this Deck's controller working normally? This tool needs\n"
             '    a healthy controller - it cannot dump a broken one.\n'
             '  * Close Steam completely, then try again (Steam can hold the\n'
             '    controller open).\n'
             '  * Reboot the Deck and try once more.')

    lay = layout_for(major, usb.get('legacy_pid', False))
    set_expected_unserved(major, lay)

    records = []
    for b in targets:
        say()
        hr('-')
        say(f'  Reading {b.label}   ({lay["flash_size"] // 1024} KB)')
        hr('-')

        api = collect_api_info(b, major)
        for k, v in sorted(api.items()):
            say(f'   {k}: {v}')

        say('   checking which parts of the flash the controller will serve...')
        data, gaps, spans = dump_region(
            b, lay['flash_start'], lay['flash_start'] + lay['flash_size'],
            'flash')

        if not any(ok for _, _, ok in spans):
            say('   This MCU refused every read. Nothing to save for it.')
            records.append({'label': b.label, 'backend': b.kind, 'side': b.side,
                            'bytes': 0, 'verdict': 'refused all reads',
                            'blank_pct': 0.0, 'gaps': gaps, 'files': [],
                            'api': api, 'spans': spans,
                            'verify': {'checked': 0, 'mismatched': 0,
                                       'errors': 0, 'offsets': []}})
            continue

        raw = os.path.join(outdir, f'{b.label}-full-flash.bin')
        with open(raw, 'xb') as fh:
            fh.write(data)

        df = None
        if lay['data_flash']:
            dfs, dfe = lay['data_flash']
            df, dgaps, dspans = dump_region(b, dfs, dfe, 'dataflash')
            gaps = gaps + dgaps
            spans = spans + dspans
            with open(os.path.join(outdir, f'{b.label}-dataflash.bin'), 'xb') as fh:
                fh.write(df)

        say('   verifying by re-reading a sample of the flash...')
        ver = verify_sample(b, data, lay['flash_start'], gaps)
        if ver['mismatched']:
            say(f'   !! {ver["mismatched"]} of {ver["checked"]} sampled '
                f'addresses read back differently. This dump is UNRELIABLE.')
        else:
            say(f'   >> {ver["checked"]} sampled addresses re-read identically.')

        # Count blankness over what was actually read. Including a refused
        # region would report placeholder 0xFF as erased flash and trip the
        # suspect-dump check below on a perfectly good dump.
        got = sum(e - s for s, e, ok in spans
                  if ok and s < lay['flash_start'] + lay['flash_size']) \
            - sum(n for _, n in gaps)
        got = max(got, 0)
        blank = (data.count(0xFF) - (len(data) - got)) / max(got, 1) * 100
        blank = min(max(blank, 0.0), 100.0)
        bl_readable = any(ok and s <= lay['flash_start'] < e for s, e, ok in spans)

        # A vector table at 0x0 attests to the first eight bytes only. If that
        # signal disagrees with an almost entirely blank flash, a mis-read is
        # far likelier than a working controller running from erased flash, so
        # say so rather than reporting a confident wrong verdict.
        vok = vectors_ok(data) and bl_readable
        if vok and blank > 90:
            verdict = 'suspect - implausibly blank'
            say('   !! SUSPECT DUMP: real-looking vectors but the readable part')
            say(f'   !! of the flash is {blank:.1f}% blank. That combination is')
            say('   !! not physically plausible on a controller that works.')
        elif vok:
            verdict = 'bootloader captured'
            say('   >> bootloader region read, vector table looks valid.')
        elif not bl_readable:
            verdict = 'app only - bootloader region refused'
            say('   >> this interface does not serve the bootloader region.')
        elif blank > 99:
            verdict = 'blank'
        else:
            verdict = 'data, no clear vector table'

        if lay['data_flash']:
            info_src = df or b''
            info_base = lay['data_flash'][0]
        else:
            info_src = data
            info_base = 0
        devinfo = parse_devinfo(
            info_src[lay['info_offset'] - info_base:
                     lay['info_offset'] - info_base + 256])
        mte = parse_mte_blob(
            info_src[lay['blob_offset'] - info_base:
                     lay['blob_offset'] - info_base + 256])

        records.append({
            'label': b.label, 'backend': b.kind, 'side': b.side,
            'bytes': len(data), 'verdict': verdict, 'blank_pct': blank,
            'gaps': gaps, 'api': api, 'verify': ver, 'spans': spans,
            'device_info': devinfo, 'mte_blob': mte,
            'build_times': build_times(data, lay),
            'app_crc': app_crc_check(data, lay),
            'vector_table_ok': vok,
            '_data': data, '_dataflash': df, '_raw_path': raw, 'files': [],
        })

        if gaps:
            missing = sum(n for _, n in gaps)
            unexpected = [(o, n) for o, n in gaps
                          if not expected_refusal(o, o + n)]
            say(f'   -- {missing:,} bytes in {len(gaps)} range(s) were not read '
                f'by this pass.')
            if not unexpected:
                say('   -- All of that is region this board was never going to')
                say('   -- serve here, so nothing has gone wrong.')
            say('   -- It is held as 0xFF and listed, range by range, in the')
            say('   -- manifest, so a short read is never taken for erased flash.')

    # ---- hand the controller back ------------------------------------------
    say()
    say('Returning the controller to normal...')
    seen = set()
    for b in targets:
        if id(b.obj) not in seen:
            seen.add(id(b.obj))
            b.close()
    time.sleep(2)

    # ---- establish what this board is --------------------------------------
    hwid = None
    for r in records:
        di = r.get('device_info') or {}
        if r.get('side') == 'primary' and di.get('hw_id') is not None:
            hwid = di['hw_id']
            break
    if hwid is None:
        for r in records:
            di = r.get('device_info') or {}
            if di.get('hw_id') is not None:
                hwid = di['hw_id']
                break
    if hwid is None:
        for r in records:
            v = r['api'].get('hardware_id')
            if isinstance(v, int):
                hwid = v
                break

    dmi_board = read_first_line('/sys/class/dmi/id/board_name') or ''
    board, chip_primary, chip_secondary = identify(major, hwid, dmi_board)
    for r in records:
        r['chip'] = chip_primary if r.get('side') == 'primary' else \
            (chip_secondary or chip_primary)
        # If the secondary reports its own hardware ID, believe it over the
        # table lookup - that is the point of a hybrid board.
        di = r.get('device_info') or {}
        own = HWID_BOARDS.get(di.get('hw_id'))
        if r.get('side') == 'secondary' and own:
            r['chip'] = own[1]

    # ---- name and write the carved regions ---------------------------------
    def add_file(rec, suffix, payload):
        name = f'{rec["label"]}-{rec["chip"]}-{suffix}.bin'
        path = os.path.join(outdir, name)
        if os.path.exists(path):
            return
        with open(path, 'xb') as fh:
            fh.write(payload)
        rec['files'].append({'name': name, 'bytes': len(payload),
                             'sha256': sha256(path)})

    for r in records:
        data = r.pop('_data', None)
        df = r.pop('_dataflash', None)
        raw = r.pop('_raw_path', None)
        if data is None:
            continue
        final_raw = os.path.join(outdir,
                                 f'{r["label"]}-{r["chip"]}-full-flash.bin')
        raw = rename_if_free(raw, final_raw)
        r['files'].append({'name': os.path.basename(raw), 'bytes': len(data),
                           'sha256': sha256(raw)})
        if df is not None:
            src = os.path.join(outdir, f'{r["label"]}-dataflash.bin')
            dst = os.path.join(outdir, f'{r["label"]}-{r["chip"]}-dataflash.bin')
            dst = rename_if_free(src, dst)
            r['files'].append({'name': os.path.basename(dst), 'bytes': len(df),
                               'sha256': sha256(dst)})

        bl = data[lay['flash_start']:lay['app_start']]
        if bl and bl.count(0xFF) != len(bl):
            add_file(r, f'bootloader-{len(bl) // 1024}k', bl)
        app = data[lay['app_start']:lay['app_end']]
        if app and app.count(0xFF) != len(app):
            add_file(r, 'app', app)
        if lay['data_flash']:
            if df:
                base = lay['data_flash'][0]
                add_file(r, 'devinfo', df[lay['info_offset'] - base:
                                          lay['info_offset'] - base + 256])
                # One logical partition. The rest of the region is already in
                # the dataflash image.
                add_file(r, 'unit-blob', df[lay['blob_offset'] - base:
                                            lay['blob_offset'] - base + 256])
        else:
            add_file(r, 'devinfo', data[lay['info_offset']:
                                        lay['info_offset'] + 256])
            add_file(r, 'unit-blob', data[lay['blob_offset']:
                                          lay['blob_offset'] + 256])

    # ---- identity used for naming the archive ------------------------------
    primary = next((r for r in records if r.get('side') == 'primary'), None)
    di = (primary or {}).get('device_info') or {}
    bt = (primary or {}).get('build_times') or {}

    result = {
        'source': 'usb',
        'collector_version': COLLECTOR_VERSION,
        'created': datetime.datetime.now().isoformat(timespec='seconds'),
        'identity': {
            'major': major,
            'type_name': TYPE_NAMES.get(major, f'unrecognised type {major}'),
            'release_number': usb.get('release_number'),
            'release_number_hex': (f'0x{usb["release_number"]:04X}'
                                   if usb.get('release_number') else None),
            'legacy_usb_ids': usb.get('legacy_pid'),
            'found_in_bootloader_mode': usb.get('in_bootloader'),
            'hw_id': hwid,
            'board': board,
            'primary_chip': chip_primary,
            'secondary_chip': chip_secondary,
            'board_serial': di.get('board_serial') or None,
            'unit_serial': di.get('unit_serial') or None,
            'app_build': bt.get('app'),
            'bootloader_build': bt.get('bootloader'),
        },
        'flash_layout': {k: (v if not isinstance(v, int) else f'0x{v:08X}')
                         for k, v in lay.items() if k != 'data_flash'},
        'usb_interfaces': usb.get('interfaces'),
        'host': host_info(),
        'shipped_firmware': fw_updater_inventory(),
        'mcus': records,
        'notes': notes,
    }
    if lay['data_flash']:
        result['flash_layout']['data_flash'] = \
            f'0x{lay["data_flash"][0]:08X}-0x{lay["data_flash"][1]:08X}'
    result['assessment'] = assessment(records)

    with open(os.path.join(outdir, 'usb-dump.json'), 'w') as fh:
        json.dump(result, fh, indent=2, default=str)

    write_transcript(os.path.join(outdir, 'usb-dump-log.txt'),
                     'Console output of the USB dump.')

    say()
    say(f'  {result["assessment"]}')
    for r in records:
        extra = '' if not r.get('gaps') else f"  [{len(r['gaps'])} unread gap(s)]"
        say(f'    - {r["label"]} ({r.get("chip")}): {r["verdict"]}{extra}')
    say()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        say('\nCancelled. Nothing was changed on the controller.')
        sys.exit(130)
    except Exception:                                           # noqa: BLE001
        traceback.print_exc()
        say()
        say('Something unexpected went wrong. Nothing was written to the')
        say('controller. Restart the Deck if it does not respond.')
        sys.exit(1)
