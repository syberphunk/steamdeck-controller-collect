# steamdeck-controller-collect

Reads the firmware and bootloader out of a Steam Deck's built-in controller
board and packs them into a single, descriptively named `.tar.gz`.

It is read-only. Nothing is erased, written or reflashed. The controller and
the Deck are left exactly as they were found.

## Running it

### First: set a password for the `deck` user

SteamOS ships with **no password** on the `deck` account. Reading the
controller needs administrator access, and `sudo` cannot work until a password
exists, so do this before anything else.

Switch to Desktop Mode (**Steam button → Power → Switch to Desktop**), open
**Konsole** from the taskbar or the application menu, and run:

```
passwd
```

Three things to know about it:

- **It does not show anything as you type.** No dots, no asterisks, no moving
  cursor. That is normal and it is not frozen — type the password and press
  Enter.
- **It asks twice**, to catch typos. Type the same thing both times.
- **Remember it.** You will need it every time you use `sudo`, and after
  updates or a reinstall you may have to set it again. There is no "forgot
  password" on a Deck; recovering from a lost one means a reimage. Write it
  down somewhere you trust.

If you have set one before and cannot remember it, run `passwd` again — as the
`deck` user you can change your own password without knowing the old one.

### Then: clone and run

Still in Konsole:

```
git clone https://github.com/syberphunk/steamdeck-controller-collect.git
cd steamdeck-controller-collect
./collect.sh
```

`git` is already on SteamOS; nothing needs installing. If `git clone` fails
because you are offline, you can instead download the ZIP from the GitHub page,
extract it in Dolphin, then `cd` into the extracted folder and run
`chmod +x collect.sh rescue.sh` before `./collect.sh`.

`collect.sh` asks for the password you just set (again showing nothing as you
type), identifies the controller, tells you what can be captured from it, asks
what to collect, does it, and prints the path of the finished archive in your
home folder.

Leave the Deck plugged in and do not let it sleep while it runs.

### Options

| | |
|---|---|
| `./collect.sh` | identify, ask what to collect, collect it |
| `./collect.sh --firmware` | firmware only, no prompts |
| `./collect.sh --full` | firmware and bootloader, no prompts |
| `./collect.sh --detect-only` | identify the controller and stop |

## What gets captured

The controller is wired into the Deck as an internal USB device, so all of
this happens over USB — including the boot-ROM pass below. What differs
between boards is **how many passes it takes, and whether you have to hold
buttons.** The script works that out before asking anything.

**SAMD boards** (Steam Deck LCD, and OLED units built on the older controller)
hand over their whole flash when asked. One unattended pass gets the
bootloader, the application firmware, the device-info partition and the
per-unit blob, from both the right-hand and left-hand boards.

**RA4 boards** (Renesas RA4E1, a single board rather than two) will not hand
over `0x0`–`0x8000` — the bootloader — to the interface their firmware
provides. That region is only served by the separate boot ROM built into the
chip, and starting it means restarting the controller board while three
buttons are held: Right Bumper, Right Upper Back and Right Quick Access. That
is a second pass, and the script offers it as a choice because it needs you
there holding them. Declining still captures the application firmware and data
flash.

The controller stops responding during the boot-ROM pass and is handed back
automatically at the end.

## If the controller stops working

Run:

```
./rescue.sh
```

**The controller is not bricked.** Reading its flash means putting it into a
mode where it is not running its normal firmware, and it stays in that mode
until something tells it to leave — an interrupted run never gets to tell it.
Nothing has been erased or written: the dumpers have no code path that can,
and they check that about themselves before opening the device.

`collect.sh` already does this for you on the way out, including on Ctrl-C.
`rescue.sh` is for when that could not run — a closed terminal, a killed
process, a crash — and for anyone who would rather just run something. It
handles both stuck modes:

| | |
|---|---|
| `28de:1004` Valve bootloader | left by an interrupted first pass; ends with one command |
| `045b:0261` RA USB Boot | left by an interrupted boot-ROM pass; ends with a restart of the controller board |

It is safe to run at any time. If nothing is wrong it says so and stops.
If it cannot fix things, reboot the Deck — that re-runs the normal controller
bring-up from scratch and clears nearly everything software cannot.

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
| `*-bootloader-*.bin` | bootloader, read in the normal pass |
| `*-rom-bootloader-*.bin` | bootloader, read through the chip's boot ROM |
| `*-dataflash.bin` | per-unit factory data |
| `*-devinfo.bin` | device-info partition: hardware ID, serials, build stamp |
| `*-unit-blob.bin` | per-unit calibration blob |
| `*-full-flash.bin` | the whole flash as read, before being carved up |
| `manifest.json` | everything above in full, with SHA-256 hashes |
| `README.txt` | a plain-text summary of this capture |
| `collect-log.txt` | everything the script printed, and what you answered |
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

## License

MIT — see [LICENSE](LICENSE). `lib/vendor/serial/` is pySerial 3.5 under
BSD-3-Clause; see [lib/vendor/README.md](lib/vendor/README.md).
