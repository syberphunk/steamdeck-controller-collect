#!/bin/bash

set -uo pipefail

FWDIR=/usr/share/jupiter_controller_fw_updater/RA_bootloader_updater
BATCTRL="$FWDIR/linux_host_tools/BatCtrl"
BOOT_ID=${BOOT_ID:-045b:0261}      # Renesas RA USB Boot - the ROM
CTRL_ID=${CTRL_ID:-28de:1205}      # Valve Steam Controller - normal firmware
TRIES=${TRIES:-3}

if [ ! -x "$BATCTRL" ]; then
    echo "ERROR: $BATCTRL not found. This needs SteamOS with jupiter-hw-support."
    exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "Re-running under sudo (BatCtrl needs root)..."
    exec sudo -- "$0" "$@"
fi

on_bus() { lsusb -d "$1" >/dev/null 2>&1; }

echo "========================================================================"
echo " Return the controller board to normal"
echo "========================================================================"
echo
echo "Current state:"
if on_bus "$BOOT_ID"; then
    echo "  $BOOT_ID  RA USB Boot        PRESENT  - held in ROM boot mode"
else
    echo "  $BOOT_ID  RA USB Boot        absent"
fi
if on_bus "$CTRL_ID"; then
    echo "  $CTRL_ID  Steam Controller   PRESENT  - normal firmware running"
else
    echo "  $CTRL_ID  Steam Controller   absent"
fi
echo

if on_bus "$CTRL_ID" && ! on_bus "$BOOT_ID"; then
    echo "Nothing to do - the controller is already running normally."
    exit 0
fi

cat <<'EOF'
RELEASE ALL THREE BUTTONS before continuing.

    * Right Bumper  (R1)
    * Right Upper Back  (R4)
    * Right Quick Access  (the "..." button)

Holding any of them through the power cycle puts the board straight back
into boot mode, which is exactly what this script is trying to undo.

EOF
read -r -t 20 -p "Buttons released? Press Enter (continuing in 20s) " || true
echo

for attempt in $(seq 1 "$TRIES"); do
    echo "---- power cycle $attempt of $TRIES ----"
    "$BATCTRL" SetCBPower 0 >/dev/null 2>&1
    sleep 1
    "$BATCTRL" SetCBPower 1 >/dev/null 2>&1

    for _ in $(seq 1 100); do          # up to 10 s
        if on_bus "$CTRL_ID"; then
            echo
            echo "Controller is back: $CTRL_ID"
            if on_bus "$BOOT_ID"; then
                echo "NOTE: $BOOT_ID is also present - that is the OTHER board,"
                echo "      or a button is still held. Re-run if the pad misbehaves."
            fi
            echo "SteamOS should pick it up within a second or two."
            exit 0
        fi
        sleep 0.1
    done
    echo "  not back yet."
done

cat <<EOF

------------------------------------------------------------------------
The controller has not re-enumerated as $CTRL_ID after $TRIES attempts.

The firmware is still intact - nothing in this toolchain can erase or
write to the MCU. Things to try, in order:

  1. Make sure no buttons are held, and run this script again.
  2. Fully reboot the Deck. The EC keeps the 5V rail state, and a reboot
     re-runs the normal controller bring-up.
  3. Check by hand what is on the bus:
       lsusb | grep -i -e valve -e renesas -e hitachi

If $BOOT_ID is still present, the board is in boot mode and healthy -
it is waiting for a host, not failing.
EOF
exit 1
