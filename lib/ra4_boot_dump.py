#!/usr/bin/env python3
"""Read-only dumper for a Steam Deck controller board in Renesas boot mode.

Reads the Renesas RA4E1 through the MCU's own factory boot ROM, which serves a
general-purpose read command and therefore reaches the 0x0-0x8000 bootloader
region that Valve's bootloader refuses.

The board must already be in boot mode: a button combination held while the
controller board alone is power-cycled with BatCtrl. Nothing here can put it
there - this tool never touches BatCtrl and never power-cycles anything.

This tool can only read. The erase and write command constants are not defined
anywhere in the bundle, and self_audit() proves it before the device is opened.

Derived from raflash by Robin Krens - GNU GPL v2 or later. See raboot/packer.py.
"""

import argparse
import json
import os
import re
import struct
import sys
import time
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vendor'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SCRIPT_VERSION = '2026-09-11'

# The only command bytes this tool is permitted to transmit.
ALLOWED_COMMANDS = {'INQ_CMD', 'REA_CMD', 'SIG_CMD', 'ARE_CMD'}
FORBIDDEN_COMMANDS = {'ERA_CMD', 'WRI_CMD'}

TRANSCRIPT = []


def log_line(text):
    for line in str(text).rstrip('\n').split('\n'):
        TRANSCRIPT.append(f'{time.strftime("%H:%M:%S")}  {line}')


def say(msg=''):
    print(msg)
    log_line(msg)
    sys.stdout.flush()


def hr(ch='='):
    say(ch * 72)


def self_audit():
    """Prove the bundle cannot erase or write before touching the hardware.

    Two independent checks: the command constants must not exist, and every
    pack_pkt() call site must name a command from the allowed set.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    sources = [os.path.abspath(__file__)]
    pkg = os.path.join(here, 'raboot')
    for fn in sorted(os.listdir(pkg)):
        if fn.endswith('.py'):
            sources.append(os.path.join(pkg, fn))

    problems = []

    from raboot import packer
    for name in FORBIDDEN_COMMANDS:
        if hasattr(packer, name):
            problems.append(f'{name} is defined in raboot/packer.py')

    # Word-boundary guard so unpack_pkt() does not match, and a def guard so
    # the definition of pack_pkt itself does not match its own parameter name.
    call = re.compile(r'(?<![\w.])(?<!def )pack_pkt\(\s*([A-Za-z_][A-Za-z0-9_]*)')
    for path in sources:
        with open(path, 'r') as fh:
            text = fh.read()
        for m in call.finditer(text):
            cmd = m.group(1)
            if cmd not in ALLOWED_COMMANDS:
                line = text[:m.start()].count('\n') + 1
                problems.append(
                    f'{os.path.basename(path)}:{line} sends {cmd}, '
                    f'which is not in the allowed set')

    if problems:
        print('SELF-AUDIT FAILED - refusing to run:')
        for p in problems:
            print(f'  - {p}')
        sys.exit(1)

    say(f'Self-audit clean: {len(sources)} source files, '
        f'only {", ".join(sorted(ALLOWED_COMMANDS))} can be sent.')


def crc32_valve(data):
    """Valve's reflected CRC-32: poly 0x04C11DB7, init 0, no xorout."""
    return zlib.crc32(data, 0xFFFFFFFF) ^ 0xFFFFFFFF


def progress(done, total):
    """Redraws in place on a terminal; prints sparse milestones when piped or
    run over ssh, so a log file does not fill with thousands of near-identical
    lines. Never captured into the transcript either way."""
    pct = 100.0 * done / total if total else 100.0
    if sys.stdout.isatty():
        width = 40
        filled = int(width * done / total) if total else width
        sys.stdout.write(f'\r    [{"#" * filled}{"." * (width - filled)}] '
                         f'{pct:5.1f}%  {done:,}/{total:,} bytes')
        if done >= total:
            sys.stdout.write('\n')
        sys.stdout.flush()
        return
    step = max(total // 10, 1)
    if done >= total or done // step != (done - 1024) // step:
        print(f'    {pct:5.1f}%  {done:,}/{total:,} bytes', flush=True)


def cmd_probe(args):
    from raboot.connect import (list_candidate_ports, usb_device_present,
                                PRODUCT_ID, VENDOR_ID)
    hr()
    say('USB serial ports visible on this machine:')
    cands = list_candidate_ports()
    # A Deck exposes ~30 legacy /dev/ttyS* with no VID at all. Listing them buries
    # the one line that matters, so they are counted rather than named.
    usb = [c for c in cands if c['vid'] is not None]
    legacy = len(cands) - len(usb)
    if not usb:
        say('  (none)')
    for c in usb:
        mark = '  <-- RA BOOT MODE' if c['is_ra_boot'] else ''
        say(f"  {c['device']}  {c['vid']:04x}:{c['pid']:04x}  {c['description']}{mark}")
    if legacy:
        say(f'  ({legacy} legacy /dev/ttyS* ports omitted - never the boot ROM)')
    hr('-')
    if any(c['is_ra_boot'] for c in cands):
        say(f'Found a Renesas boot-mode device ({VENDOR_ID:04x}:{PRODUCT_ID:04x}).')
        say('Next: run   sudo python3 lib/ra4_boot_dump.py info')
        return 0
    if usb_device_present():
        # On the bus but no tty - cdc_acm has not bound (yet).
        say(f'{VENDOR_ID:04x}:{PRODUCT_ID:04x} IS on the USB bus, but has no')
        say('serial port. cdc_acm has not bound to it.')
        say('Try again in a moment; if it persists, check "lsmod | grep cdc_acm".')
        return 1

    say(f'No {VENDOR_ID:04x}:{PRODUCT_ID:04x} device - the board is not in boot mode.')
    say('')
    say('Note that the board only STAYS in boot mode for a few seconds. Running')
    say('probe after a power cycle will almost always miss it. Use:')
    say('')
    say('  sudo ./collect.sh')
    say('')
    say('which starts the dumper watching first, then power-cycles the board, so')
    say('it is caught inside the window.')
    return 1


def open_device(args):
    from raboot.connect import RABoot, wait_for_port, usb_device_present

    trace = getattr(args, 'trace', False)

    if args.port or not getattr(args, 'wait', 0):
        say(f'Opening {args.port or "auto-detected port"} ...')
        dev = RABoot(port=args.port, trace=trace)
        say(f'  connected on {dev.port}')
        return dev

    # The board holds boot mode for only ~3.3 s, so a single shot is a coin
    # flip. Keep retrying for the whole wait window: the operator's partner
    # process re-cycles power, and each cycle gives us another window. One
    # button-holding session therefore buys many attempts rather than one.
    say(f'Waiting up to {args.wait:g}s for the board to enter boot mode...')
    say('(power will be cycled repeatedly; keep holding the buttons)')
    deadline = time.monotonic() + args.wait
    t0 = time.monotonic()
    attempt = 0
    saw_board = False
    last_err = None

    while time.monotonic() < deadline:
        port = wait_for_port(timeout=max(0.1, deadline - time.monotonic()))
        if not port:
            break
        saw_board = True
        attempt += 1
        say(f'  [{time.monotonic() - t0:6.2f}s] attempt {attempt}: board on {port}')
        try:
            dev = RABoot(port=port, trace=trace)
            say(f'  connected on {dev.port}')
            return dev
        except Exception as err:
            last_err = err
            say(f'  attempt {attempt} failed: {str(err).splitlines()[0]}')
            if trace:
                say(str(err))
        # Let the dead node go away before looking again, otherwise we spin on
        # a stale port that no longer has a device behind it.
        gone = time.monotonic() + 8
        while time.monotonic() < gone and usb_device_present():
            time.sleep(0.1)

    if not saw_board:
        raise RuntimeError(
            'Board never entered boot mode within the wait window.\n'
            '  Nothing matching 045b:0261 appeared on the USB bus.\n'
            '  Check all three buttons are held for the whole power cycle.')
    raise RuntimeError(
        f'Board entered boot mode {attempt} time(s) but never completed the '
        f'handshake.\n  Last failure:\n{last_err}')


def show_identity(dev):
    """Signature + area info. Both are pure queries."""
    sig = dev.signature()
    hr('-')
    say('Device signature:')
    say(f"  product:           {sig['product']}")
    say(f"  chip:              {sig['chip']} (raw type 0x{sig['type_raw']:02X})")
    say(f"  boot firmware:     version {sig['boot_fw_version']}")
    say(f"  device ID:         {sig['device_id']}")
    say(f"  max baud:          {sig['max_baud']:,} bps")
    say(f"  accessible areas:  {sig['num_areas']}")

    # Ask for exactly as many areas as the device says it has. Assuming three
    # missed the config area entirely on a real RA4E1, which has four.
    areas = dev.area_info(num_areas=sig['num_areas'])
    hr('-')
    say('Area information, as reported by the device:')
    for i in sorted(areas):
        a = areas[i]
        if 'error' in a:
            say(f"  area {i}: {a['error']}")
            continue
        size = a['EAD'] - a['SAD'] + 1
        note = '' if a['readable'] else '  [NOT READABLE: read access unit is 0]'
        say(f"  area {i} ({a['kind']}, KOA 0x{a['KOA']:02X}): "
            f"0x{a['SAD']:08X}-0x{a['EAD']:08X} "
            f"({size:,} bytes, erase unit 0x{a['erase_unit']:X}){note}")
    return sig, areas


def cmd_info(args):
    hr()
    say('Query only. Nothing is read from flash and nothing is modified.')
    dev = open_device(args)
    try:
        show_identity(dev)
        hr()
        say('If the areas above list real addresses, the device is unlocked and')
        say('a dump will work. Run:  sudo ./collect.sh')
    finally:
        dev.close()
    return 0


def cmd_dump(args):
    from raboot.packer import DeviceError
    hr()
    say(f'ra4-boot-dump {SCRIPT_VERSION} - read-only RA4 boot-mode dump')
    dev = open_device(args)
    results = {}
    files = {}
    try:
        sig, areas = show_identity(dev)

        # Take the regions from the device rather than from a hardcoded map.
        # Code flash is reported as two areas that differ only in erase block
        # size, so asking area_for(0x0) and trusting its EAD read just the
        # first 64 KB of a 256 KB part and called it the whole code flash.
        spans = dev.regions_by_kind()
        label = {'user (code flash)': 'code-flash',
                 'data flash': 'data-flash',
                 'config': 'config-area'}
        wanted = []
        for k, lo, hi in spans:
            name = label.get(k, k.replace(' ', '-'))
            # A kind can yield more than one span if the device splits it across
            # an address gap. Keep the names unique so neither the results dict
            # nor the written files silently overwrite each other.
            if any(w[0] == name for w in wanted):
                name = f'{name}-0x{lo:08X}'
            wanted.append((name, lo, hi))
        hr('-')
        if not wanted:
            say('The device reported no readable areas at all.')
        for name, start, real_end in wanted:
            say(f'{name}: reading 0x{start:08X}-0x{real_end:08X} '
                f'({real_end - start + 1:,} bytes)')
            try:
                blob = dev.read_range(start, real_end, progress=progress)
                files[f'{name}.bin'] = blob
                results[name] = {
                    'status': 'read',
                    'start': start,
                    'end': real_end + 1,
                    'length': len(blob),
                    'crc32_valve': f'0x{crc32_valve(blob):08X}',
                }
                say(f'    {len(blob):,} bytes, '
                    f'CRC 0x{crc32_valve(blob):08X}')
            except DeviceError as e:
                say(f'    REFUSED: {e}')
                results[name] = {'status': 'refused', 'error': str(e),
                                 'code': f'0x{e.code:02X}'}
            except Exception as e:
                say(f'    FAILED: {e}')
                results[name] = {'status': 'failed', 'error': str(e)}

        # The prize: the region Valve's bootloader will not serve.
        if 'code-flash.bin' in files and len(files['code-flash.bin']) >= 0x8000:
            bl = files['code-flash.bin'][:0x8000]
            files['bootloader-0x0-0x8000.bin'] = bl
            blank = bl.count(0xFF)
            hr('-')
            say(f'Bootloader region extracted: {len(bl):,} bytes, '
                f'CRC 0x{crc32_valve(bl):08X}')
            say(f'  0xFF bytes: {blank:,}/{len(bl):,} '
                f'({100.0 * blank / len(bl):.1f}%)')
            if blank == len(bl):
                say('  WARNING: entirely 0xFF. That is blank flash, not a '
                    'bootloader.')
            results['bootloader'] = {
                'length': len(bl),
                'crc32_valve': f'0x{crc32_valve(bl):08X}',
                'ff_bytes': blank,
            }
    finally:
        dev.close()

    return write_results(args, sig, areas, results, files)


def write_results(args, sig, areas, results, files):
    """Write images and metadata straight into --outdir.

    Files are prefixed `rom-` so that, in an archive that also holds a USB
    dump of the same board, it is unambiguous which path each image came from.
    Packing is left to the caller.
    """
    stamp = time.strftime('%Y%m%d-%H%M%S')
    outdir = args.outdir
    os.makedirs(outdir, exist_ok=True)

    written = {}
    for fn, blob in files.items():
        name = f'rom-{fn}'
        with open(os.path.join(outdir, name), 'wb') as fh:
            fh.write(blob)
        written[name] = {'bytes': len(blob),
                         'crc32_valve': f'0x{crc32_valve(blob):08X}'}

    bootloader_build = None
    bl = files.get('bootloader-0x0-0x8000.bin')
    if bl:
        m = re.search(rb'BUILD_TIME_([0-9A-Fa-f]{8})', bl)
        if m:
            bootloader_build = m.group(1).decode()

    manifest = {
        'source': 'rom',
        'script_version': SCRIPT_VERSION,
        'created': stamp,
        'method': 'Renesas RA boot ROM, USB CDC, read command 0x15',
        'signature': sig,
        'areas': {str(k): v for k, v in areas.items()},
        'regions': results,
        'files': written,
        'bootloader_build': bootloader_build,
        'python': sys.version.split()[0],
    }
    with open(os.path.join(outdir, 'rom-dump.json'), 'w') as fh:
        json.dump(manifest, fh, indent=2)

    with open(os.path.join(outdir, 'rom-dump-log.txt'), 'w') as fh:
        fh.write('Console output of the ROM boot-mode dump.\n')
        fh.write('=' * 78 + '\n')
        fh.write('\n'.join(TRANSCRIPT) + '\n')

    hr()
    got_bootloader = 'bootloader-0x0-0x8000.bin' in files
    refused = [n for n, r in results.items()
               if r.get('status') in ('refused', 'failed', 'no-area')]

    if got_bootloader:
        say('Bootloader captured'
            + (f' - build {bootloader_build}' if bootloader_build else ''))
    elif files:
        say('PARTIAL - read something, but not the bootloader region')
    else:
        say('NOTHING CAPTURED - every region was refused')
        say('The part is almost certainly locked (DLM state, or an ID code).')
        say('No tool can get past that; the results record the refusals.')
    if refused:
        say(f'Refused or skipped: {", ".join(refused)}')
    return 0 if files else 1


def main():
    ap = argparse.ArgumentParser(
        description='Read-only RA4 boot-mode dumper for Steam Deck controller boards.')
    ap.add_argument('--port', help='Serial port, e.g. /dev/ttyACM0. '
                                   'Auto-detected if omitted.')
    ap.add_argument('--outdir', default=os.path.expanduser('~'),
                    help='Where to write the archive (default: home).')
    ap.add_argument('--wait', type=float, default=0, metavar='SECONDS',
                    help='Poll for the board to enter boot mode and grab it the '
                         'instant it appears. The board holds boot mode for only '
                         'a few seconds, so start this BEFORE power-cycling.')
    ap.add_argument('--trace', action='store_true',
                    help='Log every byte sent and received during the boot '
                         'handshake. Each real attempt costs a power cycle with '
                         'three buttons held, so make the failures talk.')
    sub = ap.add_subparsers(dest='command')
    sub.add_parser('probe', help='List serial ports, no device contact')
    sub.add_parser('info', help='Query chip identity and area map only')
    sub.add_parser('dump', help='Read code flash and data flash to an archive')
    args = ap.parse_args()

    if not args.command:
        ap.print_help()
        return 2

    self_audit()
    if args.command == 'probe':
        return cmd_probe(args)
    if args.command == 'info':
        return cmd_info(args)
    return cmd_dump(args)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\nInterrupted. Nothing was modified.')
        sys.exit(130)
    except Exception as exc:
        print(f'\nERROR: {exc}')
        print('Nothing was modified.')
        sys.exit(1)
