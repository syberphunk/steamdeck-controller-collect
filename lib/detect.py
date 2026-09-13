#!/usr/bin/env python3
"""Identify the controller board without touching its flash.

Reads USB descriptors and the host's own identification only. The controller
is not put into bootloader mode and keeps working throughout, so this is safe
to run at any time.

Writes detect.json next to the given output directory and prints a summary.

Exit status:
    0  a controller was identified
    2  no Valve controller interface is present
    3  this is not a Steam Deck
"""

import argparse
import importlib.util
import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import (FW_DIR, PID_APP, TYPE_NAMES, VALVE_VID,  # noqa: E402
                    bootloader_source, host_info, identify, layout_for,
                    probe_usb, read_first_line, say)


def app_build_timestamps():
    """Application build stamps, readable while the controller runs normally.

    Uses Valve's own helper, which talks to the application over HID feature
    reports. Returns None if the helper is unavailable or declines.
    """
    path = os.path.join(FW_DIR, 'd21bootloader16.py')
    if not os.path.exists(path):
        return None
    try:
        spec = importlib.util.spec_from_file_location('valve_d21_ts', path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules['valve_d21_ts'] = mod
        spec.loader.exec_module(mod)
        import hid
        # Interface 2 is the control interface the helper expects; fall back to
        # whatever enumerates if this build numbers them differently.
        devs = [d for d in hid.enumerate(VALVE_VID, PID_APP)
                if d.get('interface_number') == 2] or \
            hid.enumerate(VALVE_VID, PID_APP)
        if not devs:
            return None
        sink = io.StringIO()
        with redirect_stderr(sink), redirect_stdout(sink):
            primary, secondary = mod.get_dev_build_timestamp(devs[0])
        return {'primary': f'{primary:08X}' if primary else None,
                'secondary': f'{secondary:08X}' if secondary else None}
    except Exception:                                           # noqa: BLE001
        return None


def plan_for(major):
    """What can be captured for this type, and what it costs the operator."""
    if major == 3:
        return {
            'usb_dump': True,
            'rom_dump': True,
            'bootloader_from': 'rom',
            'summary': [
                'Application firmware and data flash come off over USB, '
                'unattended.',
                'The bootloader region (0x0-0x8000) is refused over USB on '
                'this board.',
                'Capturing it needs the Renesas ROM boot mode, which means '
                'holding three buttons.',
            ],
        }
    return {
        'usb_dump': True,
        'rom_dump': False,
        'bootloader_from': 'usb',
        'summary': [
            'Bootloader, application firmware and identity blocks all come '
            'off over USB.',
            'Unattended - nothing to hold, nothing to press.',
        ],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--json', help='write the detection result here')
    ap.add_argument('--quiet', action='store_true')
    args = ap.parse_args()

    def out(msg=''):
        if not args.quiet:
            say(msg)

    if not os.path.isdir(FW_DIR):
        out('This does not look like a Steam Deck.')
        out(f'Expected to find: {FW_DIR}')
        return 3

    host = host_info()
    usb = probe_usb()
    major = usb.get('major')
    dmi_board = read_first_line('/sys/class/dmi/id/board_name') or ''

    # Hardware ID lives in the device-info partition, which is only reachable
    # once the board is in bootloader mode. Detection deliberately stops short
    # of that, so the board description here is by USB type only and is refined
    # during the dump.
    board, chip_primary, chip_secondary = identify(major, None, dmi_board)

    result = {
        'host': host,
        'usb': usb,
        'major': major,
        'type_name': TYPE_NAMES.get(major),
        'board_guess': board,
        'primary_chip': chip_primary,
        'secondary_chip': chip_secondary,
        'bootloader_source': bootloader_source(major) if major else None,
        'app_build_timestamps': app_build_timestamps(),
        'flash_layout': None,
        'plan': plan_for(major) if major else None,
    }
    if major:
        lay = layout_for(major, usb.get('legacy_pid', False))
        result['flash_layout'] = {
            k: (f'0x{v:08X}' if isinstance(v, int) else v)
            for k, v in lay.items() if k != 'data_flash'}

    dmi = host['dmi']
    osr = host['os_release']
    out()
    out('  Deck')
    out(f'    model          {dmi.get("product_name")} / {dmi.get("board_name")}')
    out(f'    serial         {dmi.get("product_serial") or "(not readable)"}')
    out(f'    SteamOS        {osr.get("NAME")} {osr.get("VERSION_ID")} '
        f'(build {osr.get("BUILD_ID")})')
    out(f'    firmware pkg   {host.get("jupiter_hw_support")}')
    out()

    if not usb['interfaces']:
        out('  Controller')
        out('    No Valve controller interface found on USB.')
        if usb['error']:
            out(f'    ({usb["error"]})')
        out()
        if args.json:
            with open(args.json, 'w') as fh:
                json.dump(result, fh, indent=2, default=str)
        return 2

    out('  Controller')
    out(f'    type           {TYPE_NAMES.get(major, f"unrecognised ({major})")}')
    rel = usb.get('release_number')
    out(f'    USB release    {f"0x{rel:04X}" if rel else "(unreadable)"}')
    out(f'    board          {board}')
    out(f'    primary MCU    {chip_primary}')
    out(f'    secondary MCU  {chip_secondary or "none - single MCU"}')
    if usb.get('legacy_pid'):
        out('    USB IDs        pre-release (8 KB bootloader at 0x2000)')
    if usb.get('in_bootloader'):
        out('    state          currently in bootloader mode')
    ts = result['app_build_timestamps']
    if ts:
        out(f'    app build      primary {ts.get("primary")}'
            + (f', secondary {ts.get("secondary")}' if ts.get('secondary') else ''))
    out()

    if result['plan']:
        out('  What can be captured')
        for line in result['plan']['summary']:
            out(f'    - {line}')
        out()

    if args.json:
        with open(args.json, 'w') as fh:
            json.dump(result, fh, indent=2, default=str)
    return 0


if __name__ == '__main__':
    sys.exit(main())
