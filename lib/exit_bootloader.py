#!/usr/bin/env python3
"""Send a controller stuck in Valve's bootloader mode back to its application.

Reading the flash requires the controller to be in bootloader mode, and it
stays there until something tells it to leave. If a dump is interrupted -
Ctrl-C, a closed terminal, a killed process - the board is left enumerating as
28de:1004 instead of 28de:1205, and SteamOS sees no controller.

That is not damage. The application firmware is untouched; the bootloader is
simply still running. This sends the one command that ends it, the same
command Valve's own `d20bootloader.py reset` sends.

Nothing is erased or written. The device is reached through the same read-only
proxy the dumper uses, which permits `reboot` and refuses everything that could
modify the controller.

Exit status:
    0  the controller is running its application (recovered, or never stuck)
    1  a reboot was sent but the application did not come back
    2  no Valve controller is on the bus at all
    3  this is not a Steam Deck
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import (FW_DIR, PID_APP, PID_APP_LEGACY,            # noqa: E402
                    TYPE_NAMES, VALVE_VID, hr, probe_usb, say, usb_present)
from dump_usb import ReadOnlyDevice, load_module, self_audit    # noqa: E402

APP_WAIT_S = 15


def app_running():
    return usb_present(VALVE_VID, PID_APP) or \
        usb_present(VALVE_VID, PID_APP_LEGACY)


def wait_for_app(timeout=APP_WAIT_S):
    """Wait for the application to enumerate. It is not instant: the MCU
    resets, re-runs its startup, and the host re-enumerates it."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if app_running():
            return True
        time.sleep(0.2)
    return False


def reboot_d21(path):
    """Type 1 - both MCUs are SAMD21 and answer on one d21bootloader16 handle."""
    mod = load_module(path, 'valve_d21_exit')
    dev = ReadOnlyDevice(mod.DogBootloader(reset=False))
    say('  [ok] opened via d21bootloader16.py')
    dev.reboot()
    say('  [->] reboot sent')
    return 1


def reboot_d20(path, major):
    """Types 2 and 3 - d20bootloader.py, one handle per MCU.

    The secondary goes first. Rebooting the primary brings the application
    back, and once it is running the secondary's bootloader interface is no
    longer there to talk to.
    """
    mod = load_module(path, 'valve_d20_exit')
    mcu = getattr(mod, 'DogBootloaderMCU', None)
    if mcu is None:
        raise RuntimeError('this copy of d20bootloader.py has no '
                           'DogBootloaderMCU')

    sent = 0
    if major != 3:
        try:
            sec = ReadOnlyDevice(mod.DogBootloader(mcu=mcu.SECONDARY,
                                                   reset=False))
            say('  [ok] secondary-left opened')
            sec.reboot()
            sent += 1
            say('  [->] reboot sent to secondary-left')
            time.sleep(1)
        except Exception as exc:                                # noqa: BLE001
            # Normal when only the primary was left in bootloader mode.
            say(f'  [--] secondary not in bootloader mode ({type(exc).__name__})')

    label = 'primary-single' if major == 3 else 'primary-right'
    prim = ReadOnlyDevice(mod.DogBootloader(mcu=mcu.PRIMARY, reset=False))
    say(f'  [ok] {label} opened')
    prim.reboot()
    sent += 1
    say(f'  [->] reboot sent to {label}')
    return sent


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()

    if not os.path.isdir(FW_DIR):
        say('This does not look like a Steam Deck.')
        return 3

    # The audit covers every file under lib/, this one included.
    self_audit()

    usb = probe_usb()
    if not usb['interfaces']:
        say('No Valve controller is on the bus, in either mode.')
        say('If the controller is missing entirely, the board may be held in')
        say('Renesas ROM boot mode instead - see exit-boot-mode.sh.')
        return 2

    if not usb['in_bootloader']:
        say('The controller is running its application firmware already.')
        say('Nothing to do.')
        return 0

    major = usb.get('major')
    hr('-')
    say(f'Controller is in bootloader mode ({TYPE_NAMES.get(major, "type ?")})')
    hr('-')

    d20_path = os.path.join(FW_DIR, 'd20bootloader.py')
    d21_path = os.path.join(FW_DIR, 'd21bootloader16.py')

    # Same rule the dumper uses: the major byte of the USB release number
    # chooses the tool, with the other one as a fallback.
    attempts = []
    if major == 1 and os.path.exists(d21_path):
        attempts.append(('d21', d21_path))
    if major in (2, 3) and os.path.exists(d20_path):
        attempts.append(('d20', d20_path))
    if os.path.exists(d21_path) and not any(k == 'd21' for k, _ in attempts):
        attempts.append(('d21', d21_path))
    if os.path.exists(d20_path) and not any(k == 'd20' for k, _ in attempts):
        attempts.append(('d20', d20_path))

    if not attempts:
        say(f'Neither bootloader script is present in {FW_DIR}.')
        return 1

    sent = 0
    for kind, path in attempts:
        try:
            sent = reboot_d21(path) if kind == 'd21' \
                else reboot_d20(path, major or 2)
            break
        except Exception as exc:                                # noqa: BLE001
            say(f'  [--] {os.path.basename(path)} could not do it '
                f'({type(exc).__name__}: {exc})')

    if not sent:
        say('')
        say('Could not reach the bootloader to tell it to leave.')
        return 1

    say('')
    say(f'Waiting up to {APP_WAIT_S}s for the application to enumerate...')
    if wait_for_app():
        say('')
        say('Controller is back on 28de:1205 - SteamOS will pick it up.')
        return 0

    say('')
    say('The application has not come back yet.')
    say('The firmware is intact - nothing here can erase or write to it.')
    say('Try, in order: run this again; then reboot the Deck.')
    return 1


if __name__ == '__main__':
    sys.exit(main())
