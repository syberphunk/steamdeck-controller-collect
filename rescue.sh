#!/bin/bash
# Put the controller back to normal after an interrupted collection.
#
# Run this if the controller has stopped working - Steam sees no controller,
# the sticks and buttons do nothing - after collect.sh was cancelled, closed
# or killed part-way through.
#
# THE CONTROLLER IS NOT BRICKED. Reading its flash means putting it into a
# mode where it is not running its normal firmware, and it stays in that mode
# until something tells it to leave. An interrupted run never gets to tell it.
# The firmware itself is untouched: nothing in this toolchain can erase or
# write to the controller, and both dumpers verify that about themselves
# before they open the device.
#
# There are two such modes, and this handles either:
#
#   28de:1004  Valve bootloader  - left behind by an interrupted USB dump.
#                                  Ends with one command over USB.
#   045b:0261  Renesas RA USB Boot - left behind by an interrupted boot-ROM
#                                  dump on an RA4 board. Ends with a power
#                                  cycle of the controller board.
#
# Safe to run at any time. If nothing is wrong it says so and stops.
#
# Usage:
#   ./rescue.sh

set -uo pipefail

HERE=$(dirname "$(readlink -f "$0")")
LIB="$HERE/lib"

FWDIR=/usr/share/jupiter_controller_fw_updater
BATCTRL="$FWDIR/RA_bootloader_updater/linux_host_tools/BatCtrl"

APP_ID=28de:1205        # Valve Steam Controller - normal firmware
APP_LEGACY=28de:1204    # pre-release units
BOOT_ID=28de:1004       # Valve bootloader
BOOT_LEGACY=28de:1003
ROM_ID=045b:0261        # Renesas RA USB Boot - the MCU's factory boot ROM

case "${1:-}" in
    -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next}
                    NR>1 {exit}' "$0"; exit 0 ;;
    '') ;;
    *) echo "Unknown option: $1 (try --help)"; exit 2 ;;
esac

if [ ! -d "$FWDIR" ]; then
    echo "This does not look like a Steam Deck. Expected: $FWDIR"
    exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "Re-running under sudo (talking to the controller needs root)..."
    exec sudo -- "$0" "$@"
fi

on_bus() { lsusb -d "$1" >/dev/null 2>&1; }

app_present() { on_bus "$APP_ID" || on_bus "$APP_LEGACY"; }
boot_present() { on_bus "$BOOT_ID" || on_bus "$BOOT_LEGACY"; }

report_state() {
    echo "On the USB bus now:"
    app_present  && echo "  $APP_ID  Steam Controller     normal firmware running"
    boot_present && echo "  $BOOT_ID  Valve bootloader     stuck in bootloader mode"
    on_bus "$ROM_ID" && echo "  $ROM_ID  RA USB Boot          held in ROM boot mode"
    if ! app_present && ! boot_present && ! on_bus "$ROM_ID"; then
        echo "  (nothing - no controller in any mode)"
    fi
    echo
}

echo "========================================================================"
echo " Return the controller to normal"
echo "========================================================================"
echo
report_state

did_something=0

# ---------------------------------------------- Valve bootloader mode (USB)
# Handled first: it is the common case, it needs no buttons and no power
# cycle, and on a two-board model it can be true of one MCU while the other
# is fine.
if boot_present; then
    echo "---- Leaving Valve bootloader mode -------------------------------------"
    python3 -u "$LIB/exit_bootloader.py"
    echo
    did_something=1
fi

# ------------------------------------------------ Renesas ROM boot mode
# Only reachable by cutting the board's power, so it is a separate script and
# it asks the operator to let go of the buttons first.
if on_bus "$ROM_ID"; then
    echo "---- Leaving Renesas ROM boot mode -------------------------------------"
    if [ -x "$LIB/exit-boot-mode.sh" ]; then
        "$LIB/exit-boot-mode.sh"
    else
        echo "ERROR: $LIB/exit-boot-mode.sh is missing."
    fi
    echo
    did_something=1
fi

# ----------------------------------------------------- nothing on the bus
# No controller in any mode. A power cycle is the only thing left to try from
# here, and it is the same call the ROM recovery makes.
if [ "$did_something" = "0" ] && ! app_present; then
    echo "---- No controller in any mode -----------------------------------------"
    echo
    echo "Nothing is enumerating - not the application, not either bootloader."
    echo
    if [ -x "$BATCTRL" ]; then
        echo "Power-cycling the controller board, which is all that can be done"
        echo "from software."
        echo
        "$BATCTRL" SetCBPower 0 >/dev/null 2>&1
        sleep 1
        "$BATCTRL" SetCBPower 1 >/dev/null 2>&1
        for _ in $(seq 1 100); do
            app_present && break
            sleep 0.1
        done
        did_something=1
        echo
    else
        echo "BatCtrl is not available, so even that cannot be tried here."
        echo
    fi
fi

# ---------------------------------------------------------------- outcome
echo "------------------------------------------------------------------------"
report_state

if app_present && ! boot_present && ! on_bus "$ROM_ID"; then
    # Everything this script can see is a USB state, and every USB state here
    # is correct. That is NOT the same as the controller working: after a
    # boot-mode pass the board drops off the bus and comes back, and Steam does
    # not always re-attach to it. The controller is then present, running its
    # own firmware, and dead to the user. Saying "nothing was wrong" at someone
    # holding a dead controller is worse than saying nothing, so say which
    # layer is confirmed good and where to look next.
    if [ "$did_something" = "1" ]; then
        echo "Recovered - the controller is running its own firmware again."
    else
        echo "There was nothing for this script to fix: the controller is on the"
        echo "bus and running its own firmware, not stuck in any dump mode."
    fi
    cat <<'EOF'

If it still does not respond - Steam sees no controller, the sticks and
buttons do nothing - then the controller is fine and Steam has simply not
re-attached to it. That is a Steam-side problem, and this script cannot
reach it. In order:

  1. Close Steam completely and reopen it.
  2. Reboot the Deck. This is known to clear it.

Nothing has been erased or written either way.
EOF
    exit 0
fi

cat <<EOF
The controller is still not running its application firmware.

Nothing has been erased or written to it, so the firmware is intact. Try, in
order:

  1. Make sure no buttons are being held, and run this script again.
  2. Reboot the Deck. That re-runs the normal controller bring-up from
     scratch, which clears most states this cannot.
  3. Check by hand what is on the bus:
       lsusb | grep -i -e valve -e renesas -e hitachi

If $ROM_ID is still listed, the board is in boot mode and healthy - it is
waiting for a host to talk to it, not failing.
EOF
exit 1
