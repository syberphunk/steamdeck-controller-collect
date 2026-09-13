# Vendored dependencies

## serial/ — pySerial 3.5

Unmodified copy of pySerial 3.5, from <https://github.com/pyserial/pyserial>,
(C) 2001-2020 Chris Liechti, BSD-3-Clause (see the SPDX headers on each file).

It is vendored because the Renesas boot ROM speaks USB CDC, and SteamOS does
not ship pySerial. The root filesystem is read-only and installing packages on
a Steam Deck is disruptive, so the library travels with the tool instead.

`lib/ra4_boot_dump.py` puts this directory on `sys.path` ahead of any
system-installed copy.
