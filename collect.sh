#!/bin/bash
# Collect the firmware and bootloader out of a Steam Deck's controller board.
#
# Identifies the controller, says what can be captured from it, asks what to
# do, reads it, and packs everything into one .tar.gz named after the Deck and
# what was found.
#
# Read-only. Nothing is erased or written to the controller. The dumpers have
# no code path that can write, and they verify that about themselves before
# opening the device.
#
# Usage:
#   ./collect.sh                 identify, ask, then collect
#   ./collect.sh --firmware      firmware only, no prompts
#   ./collect.sh --full          firmware and bootloader, no prompts
#   ./collect.sh --detect-only   identify and stop
#   ./collect.sh --help

set -uo pipefail

HERE=$(dirname "$(readlink -f "$0")")
LIB="$HERE/lib"

FWDIR=/usr/share/jupiter_controller_fw_updater
RA_FWDIR="$FWDIR/RA_bootloader_updater"
BATCTRL="$RA_FWDIR/linux_host_tools/BatCtrl"

BOOT_ID=045b:0261       # Renesas RA USB Boot - the MCU's factory boot ROM
BL_ID=28de:1004         # Valve bootloader - where reading flash over USB happens
CTRL_ID=28de:1205       # Valve Steam Controller - normal firmware

MODE=ask                # ask | firmware | full | detect
for arg in "$@"; do
    case "$arg" in
        --firmware)    MODE=firmware ;;
        --full)        MODE=full ;;
        --detect-only) MODE=detect ;;
        -h|--help)     awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next}
                            NR>1 {exit}' "$0"; exit 0 ;;
        *) echo "Unknown option: $arg (try --help)"; exit 2 ;;
    esac
done

die() { echo; echo "ERROR: $*"; echo; exit 1; }

if [ ! -d "$FWDIR" ]; then
    cat <<EOF

This does not look like a Steam Deck.

Expected to find: $FWDIR

Run this on the Steam Deck itself, in Desktop Mode, using Konsole.
EOF
    exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    if ! sudo -n true 2>/dev/null; then
        pwstate=$(passwd -S "$USER" 2>/dev/null | awk '{print $2}')
        if [ "$pwstate" = "NP" ] || [ "$pwstate" = "L" ]; then
            cat <<EOF

This needs administrator access, and the '$USER' account has no password set,
so sudo cannot be used yet.

Set one first:

    passwd

It will ask for a new password twice. Nothing is shown on screen as you type -
that is normal. Then run this script again:

    ./collect.sh

EOF
            exit 1
        fi
        echo
        echo "This needs administrator access to read the controller over USB."
        echo "You will be asked for your Steam Deck password."
        echo "Nothing appears on screen as you type it - that is normal."
        echo
    fi
    exec sudo -- "$0" "$@"
fi

OWNER=${SUDO_USER:-}
if [ -n "$OWNER" ]; then
    HOMEDIR=$(getent passwd "$OWNER" | cut -d: -f6)
else
    HOMEDIR=$HOME
fi
[ -d "$HOMEDIR" ] || HOMEDIR=$HOME

WORKDIR="$HOMEDIR/.controller-collect-$$"
mkdir -p "$WORKDIR" || die "could not create $WORKDIR"

LOGFILE="$HOMEDIR/.controller-collect-$$.log"
exec > >(tee -a "$LOGFILE") 2>&1

PY="python3 -u"

echo "========================================================================"
echo " Steam Deck controller firmware collection"
echo "========================================================================"

$PY "$LIB/detect.py" --json "$WORKDIR/detect.json"
detect_rc=$?

if [ $detect_rc -eq 3 ]; then
    rm -rf "$WORKDIR" "$LOGFILE"
    die "not a Steam Deck"
fi
if [ $detect_rc -eq 2 ]; then
    rm -rf "$WORKDIR" "$LOGFILE"
    cat <<EOF
No controller was found on USB.

Things to check:
  * Close Steam completely, then try again - Steam can hold the controller.
  * This tool needs a working controller; it cannot read a dead one.
  * Reboot the Deck and run it once more.
EOF
    exit 1
fi

BL_SOURCE=$(python3 -c "
import json,sys
d=json.load(open('$WORKDIR/detect.json'))
print(d.get('bootloader_source') or '')
" 2>/dev/null)

if [ "$MODE" = "detect" ]; then
    rm -rf "$WORKDIR" "$LOGFILE"
    exit 0
fi

DO_ROM=0
if [ "$BL_SOURCE" = "rom" ]; then
    case "$MODE" in
        full) DO_ROM=1 ;;
        firmware) DO_ROM=0 ;;
        ask)
            cat <<'EOF'
  Choose what to collect
  ----------------------

    1) Firmware only                                    (about 5 minutes)
       Application firmware, data flash and identity blocks.
       One pass. Nothing to hold. You can leave it running.

    2) Firmware AND bootloader                          (about 10 minutes)
       Everything in option 1, then a second pass to read the bootloader.
       The controller will not hand that region to its normal firmware
       interface - only the chip's built-in boot ROM will, and starting
       that means restarting the controller board with buttons held.

       So for the second pass you must hold three buttons throughout:
           Right Bumper (R1)
           Right Upper Back (R4)
           Right Quick Access (the "..." button)

       The controller stops working during that pass and is handed back
       automatically at the end.

EOF
            read -r -p "  Enter 1 or 2 [1]: " choice
            case "${choice:-1}" in
                2) DO_ROM=1 ;;
                *) DO_ROM=0 ;;
            esac
            echo
            if [ "$DO_ROM" = "1" ]; then
                cat <<'EOF'
  The order this happens in
  -------------------------

    Now      Pass 1, about 5 minutes. Unattended. DO NOT hold anything
             yet - just leave it alone until it finishes.

             During this pass the controller will refuse to hand over the
             bootloader region. That is expected, it is not a fault, and
             it is the whole reason there is a second pass.

    Then     The script stops and waits for you, and only then do you
             hold the three buttons. Nothing happens until you press
             Enter at that prompt.

EOF
            fi
            ;;
    esac
else
    if [ "$MODE" = "ask" ]; then
        echo "  This board hands over its bootloader as well, so one pass"
        echo "  collects everything. Nothing to hold."
        echo
        read -r -p "  Press Enter to begin (Ctrl-C to cancel) " _ || true
        echo
    fi
fi

release_board() {
    "$BATCTRL" SetCBPower 1 >/dev/null 2>&1     # never leave it unpowered
    lsusb -d "$BOOT_ID" >/dev/null 2>&1 || return 0

    echo
    echo "---- Returning the controller to normal --------------------------------"
    cat <<'EOF'
The bootloader pass is finished.

TAKE YOUR HANDS OFF THE CONTROLLER NOW.

Holding any of R1, R4 or the "..." button through the power cycle puts the
board back into boot mode. This waits for you - it will not go on by itself.

EOF
    read -r -t 300 -p "All three released? Press Enter " _ || true
    echo

    for attempt in 1 2 3 4 5; do
        "$BATCTRL" SetCBPower 0 >/dev/null 2>&1
        sleep 1
        "$BATCTRL" SetCBPower 1 >/dev/null 2>&1
        for _ in $(seq 1 100); do               # up to 10 s to re-enumerate
            if lsusb -d "$CTRL_ID" >/dev/null 2>&1; then
                cat <<EOF
Controller is back on the bus ($CTRL_ID), running its own firmware.

If it does not respond once this finishes - no sticks, no buttons, Steam
sees nothing - the controller is fine and Steam has just not re-attached
to it after the board dropped off the bus. Close Steam and reopen it, or
reboot the Deck. A reboot always clears it.
EOF
                return 0
            fi
            sleep 0.1
        done
        if lsusb -d "$BOOT_ID" >/dev/null 2>&1; then
            echo "  attempt $attempt: it came back in boot mode, so a button is"
            echo "               still held. Put the controller down entirely."
        else
            echo "  attempt $attempt: nothing on the bus yet, trying again."
        fi
    done

    cat <<EOF
The controller has not re-enumerated as $CTRL_ID.
Nothing was written to it - these tools cannot erase or write - so the
firmware is intact. Release the buttons and run:

    sudo $HERE/rescue.sh
EOF
}

RESTORED=0
restore_controller() {
    [ "$RESTORED" = "1" ] && return 0    # do not prompt twice on Ctrl-C
    RESTORED=1
    if lsusb -d "$BL_ID" >/dev/null 2>&1; then
        echo
        echo "---- Returning the controller to its firmware --------------------------"
        $PY "$LIB/exit_bootloader.py" || true
    fi
    if lsusb -d "$BOOT_ID" >/dev/null 2>&1; then
        release_board
    fi
}

interrupted() {
    cat <<EOF

------------------------------------------------------------------------
Interrupted. Nothing was written to the controller.

Whatever had been read is kept, unpacked, in:
    $WORKDIR
and the session log so far is:
    $LOGFILE

If the controller has stopped working, run:
    $HERE/rescue.sh
EOF
}

echo "------------------------------------------------------------------------"
echo " Reading the controller over USB"
echo "------------------------------------------------------------------------"
trap restore_controller EXIT
trap 'restore_controller; interrupted; exit 130' INT TERM
$PY "$LIB/dump_usb.py" --outdir "$WORKDIR"
usb_rc=$?
if [ $usb_rc -ne 0 ]; then
    echo
    echo "The USB dump did not complete (exit $usb_rc)."
    echo "Whatever it managed to read is kept and packed below."
    echo "If the controller stops working, run:  ./rescue.sh"
fi

if [ "$DO_ROM" = "1" ]; then
    if [ ! -x "$BATCTRL" ]; then
        echo
        echo "Cannot do the bootloader pass: $BATCTRL is missing."
        echo "The USB dump above is kept."
    else
        cat <<'EOF'

------------------------------------------------------------------------
 Reading the bootloader through the boot ROM
------------------------------------------------------------------------

Press and HOLD all three, and KEEP HOLDING until this pass finishes:

    * Right Bumper  (R1)
    * Right Upper Back  (R4)
    * Right Quick Access  (the "..." button)

Do not let go when the board is found. A board that nothing is talking to
resets itself out of boot mode after about 3 seconds, so hold throughout.

EOF
        read -r -p "Holding all three? Press Enter to begin (Ctrl-C to skip) " _

        echo
        echo "---- Starting the dumper in watch mode ---------------------------------"
        $PY "$LIB/ra4_boot_dump.py" --outdir "$WORKDIR" --wait 120 dump &
        dumper=$!

        sleep 3

        echo
        echo "---- Power-cycling the controller board --------------------------------"
        echo "(cycling until the dumper has it - keep holding)"
        echo

        HELD_SECONDS=6
        cycle=0
        while kill -0 "$dumper" 2>/dev/null; do
            cycle=$((cycle + 1))
            printf 'power cycle %d\n' "$cycle"
            "$BATCTRL" SetCBPower 0 >/dev/null 2>&1
            sleep 1
            "$BATCTRL" SetCBPower 1 >/dev/null 2>&1

            present=0
            for _ in $(seq 1 100); do           # up to 10 s per cycle
                kill -0 "$dumper" 2>/dev/null || break
                if lsusb -d "$BOOT_ID" >/dev/null 2>&1; then
                    present=$((present + 1))
                    if [ "$present" -ge $((HELD_SECONDS * 10)) ]; then
                        echo "board held in boot mode - the dumper has it"
                        break 2
                    fi
                else
                    present=0
                fi
                sleep 0.1
            done
        done

        wait "$dumper"
        rom_rc=$?
        if [ $rom_rc -ne 0 ]; then
            echo
            echo "The bootloader pass did not succeed (exit $rom_rc)."
            echo "If it never saw the board, the button combination did not take."
            echo "The USB dump is still kept and packed below."
        fi

        release_board
    fi
fi

echo
echo "------------------------------------------------------------------------"
echo " Packing"
echo "------------------------------------------------------------------------"

sleep 1
cp "$LOGFILE" "$WORKDIR/collect-log.txt" 2>/dev/null

archive=$(python3 "$LIB/pack.py" --workdir "$WORKDIR" --destdir "$HOMEDIR" \
                  --owner "$OWNER" | tail -1)
pack_rc=$?
rm -f "$LOGFILE"

if [ $pack_rc -ne 0 ] || [ -z "$archive" ]; then
    echo
    echo "Packing failed. The collected files are still here:"
    echo "    $WORKDIR"
    exit 1
fi

cat <<EOF

========================================================================
 Done - your Deck is back to normal
========================================================================

Your capture is this one file:

    $archive

To find it: select the line above, copy it (Ctrl+Shift+C in the terminal),
then open Dolphin, press Ctrl+L, paste and press Enter.

The unpacked folder sits next to it with the same name.

This file contains your Deck's serial number, the controller board's serial
number, and per-unit factory data from the controller. Read PRIVACY.md
before sharing it.

EOF
exit 0
