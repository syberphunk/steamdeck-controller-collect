# What is in a capture

A capture identifies the machine it came from. That is deliberate — the point
of the archive is to tie a controller's contents to the Deck it was in — but
it means you should know what you are handing over before you hand it over.

## Identifying data

**From the Deck, read from DMI:**

- `product_serial` — the Deck's serial number, the one printed on the back and
  registered to your Steam account
- `board_serial` — the mainboard's serial number
- `product_name` / `board_name` — the model (`Jupiter`, `Galileo`)
- SteamOS version, build ID and kernel version

**From the controller, read from its flash:**

- the controller board's serial number and the unit serial, in the device-info
  partition
- the programming date and hardware ID
- the per-unit calibration blob
- on RA4 boards, the data flash, which holds both serial numbers and the
  programming date in the clear

These appear in the archive filename, in `manifest.json`, in `README.txt`, and
inside the `.bin` images themselves. Removing them from the filename does not
remove them from the images.

## Unidentified contents

The RA4 data flash is 8 KB, divided into 32 records of 256 bytes, each with a
CRC32 in its first four bytes. Fifteen of those records have been identified —
serial numbers, the programming date, calibration values. **Seventeen have
not.** Their purpose is unknown.

No claim is made here about what those seventeen records hold. They are
high-entropy and they are not served over the controller's normal USB
interface, which is a description of the bytes, not evidence of what they are;
that description fits compressed data, a factory-programmed value, or a state
dump equally well. Anyone who tells you they are cryptographic key material is
guessing, and so would we be.

Treat them as unknown, and assume that unknown could matter.

## What that means for sharing

- A capture identifies one specific physical Deck and cannot be anonymised by
  renaming it.
- Publishing one publicly ties your Steam Deck's serial number to whatever
  account or handle you publish it under.
- Sharing privately with someone working on controller firmware is the case
  this tool is built for. Sharing publicly is a decision to make deliberately.
- If you want to share a capture with the unknown records excluded, send the
  `*-app.bin` and `*-bootloader-*.bin` images on their own. Those are the same
  on every unit of a given build and carry nothing specific to yours.

## What the tool does not do

- It does not upload anything. There is no network code in it.
- It does not write to the controller, and it does not modify the Deck.
- It does not send telemetry, and it keeps no copy anywhere but the archive it
  tells you about.

Everything it collected is in the folder and the `.tar.gz` in your home
directory. Deleting those deletes the capture.
