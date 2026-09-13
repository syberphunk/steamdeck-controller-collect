# steamdeck-controller-collect

Reads the firmware and bootloader out of a Steam Deck's built-in controller
board and packs them into a single, descriptively named `.tar.gz`.

It is read-only. Nothing is erased, written or reflashed. The controller and
the Deck are left exactly as they were found.

## Running it

On the Steam Deck, in Desktop Mode, open Konsole and run:

```
git clone https://github.com/<owner>/steamdeck-controller-collect.git
cd steamdeck-controller-collect
./collect.sh
```

It asks for your password, identifies the controller, tells you what can be
captured from it, asks what to collect, does it, and prints the path of the
finished archive in your home folder.

### If it says the account has no password

SteamOS ships with no password on the `deck` account, so `sudo` cannot be used
until one is set. Run `passwd`, enter a new password twice (nothing appears on
screen as you type), then run `./collect.sh` again.

### Options

| | |
|---|---|
| `./collect.sh` | identify, ask what to collect, collect it |
| `./collect.sh --firmware` | firmware only, no prompts |
| `./collect.sh --full` | firmware and bootloader, no prompts |
| `./collect.sh --detect-only` | identify the controller and stop |

## What gets captured

Which regions are reachable depends on the controller board, and the script
works this out before asking anything.

**SAMD boards** (Steam Deck LCD, and OLED units built on the older controller)
serve their whole flash over USB. One unattended pass gets the bootloader,
the application firmware, the device-info partition and the per-unit blob,
from both the right-hand and left-hand boards.

**RA4 boards** (Renesas RA4E1, a single board rather than two) refuse to serve
`0x0`–`0x8000` over USB, so the bootloader cannot be read that way. Reaching
it means a second pass through the MCU's own boot ROM, entered by holding
three buttons — Right Bumper, Right Upper Back and Right Quick Access — while
the board's power is cycled. The script offers this as a choice, because it
needs you to hold the buttons for the duration. Declining still captures the
application firmware and data flash.

The controller stops responding during the boot-ROM pass and is handed back
automatically at the end. If something interrupts the script, run
`sudo lib/exit-boot-mode.sh` to return it.

## What you get

One `.tar.gz` in your home folder, alongside an unpacked copy of the same
thing. The name describes the capture:

```
steamdeck-controller-galileo-type3-hwid46-steamos3.7.13-fw<APP>-bl<BOOT>-deck<SERIAL>-board<SERIAL>-20260913-203000.tar.gz
```

— Deck model, controller type and hardware ID, SteamOS version, application
and bootloader build stamps, Deck serial, controller board serial, timestamp.
Every image inside carries the same prefix, so a `.bin` stays identifiable
after it has been copied somewhere else.

Inside:

| | |
|---|---|
| `*-app.bin` | application firmware |
| `*-bootloader-*.bin` | bootloader, read over USB |
| `*-rom-bootloader-*.bin` | bootloader, read through the boot ROM |
| `*-dataflash.bin` | per-unit factory data |
| `*-devinfo.bin` | device-info partition: hardware ID, serials, build stamp |
| `*-unit-blob.bin` | per-unit calibration blob |
| `*-full-flash.bin` | the whole flash as read, before being carved up |
| `manifest.json` | everything above in full, with SHA-256 hashes |
| `README.txt` | a plain-text summary of this capture |
| `*-log.txt` | console output of each stage, as it happened |

Regions the controller refuses are recorded as refusals in `manifest.json`
rather than padded out, so a short read is never mistaken for erased flash.

## Before you share an archive

It contains your Deck's serial number, the controller board's serial number
and per-unit factory data. Read [PRIVACY.md](PRIVACY.md).

## Requirements

SteamOS, with `/usr/share/jupiter_controller_fw_updater` present — the script
drives Valve's own bootloader helpers from that directory rather than
reimplementing the protocols. Python 3 and `python-hid`, both already on
SteamOS. No packages are installed and the read-only root is not touched.

## Safety

The dumpers refuse to start if their own source contains a call to any
erase or write routine, and the boot-ROM dumper additionally refuses any
command outside inquiry, read, signature and area queries. This is checked at
runtime, on every run, against the files on disk.
