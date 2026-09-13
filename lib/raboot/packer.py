# Derived from raflash by Robin Krens (https://github.com/robinkrens/raflash)
#
# Copyright (C) Robin Krens - 2024
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.
#
# CHANGES FROM UPSTREAM:
#   The erase (0x12) and write (0x13) command constants are deliberately absent.
#   They are not defined here, so no code in this bundle can name them. This is a
#   read-only tool that runs against working hardware.

import struct

# Commands sent to the boot firmware. Read-only subset.
INQ_CMD = 0x00          # inquiry - is the device listening
REA_CMD = 0x15          # read memory, takes start/end address
IDA_CMD = 0x30          # ID authentication (not implemented upstream either)
SIG_CMD = 0x3A          # signature - device type, boot firmware version
ARE_CMD = 0x3B          # area info - real SAD/EAD/erase unit per area

STATUS_OK = 0x00
STATUS_ERR = 0x80

error_codes = {
    0x0C: 'ERR_UNSU  unsupported command',
    0xC1: 'ERR_PCKT  packet error',
    0xC2: 'ERR_CHKS  checksum error',
    0xC3: 'ERR_FLOW  command not allowed in the current phase',
    0xD0: 'ERR_ADDR  address error - region refused',
    0xD4: 'ERR_BAUD  baud rate error',
    0xDA: 'ERR_PROT  protection error - device is locked',
    0xDB: 'ERR_ID    ID mismatch - authentication required',
    0xDC: 'ERR_SERI  serial programming disabled',
    0xE1: 'ERR_ERA   erase error',
    0xE2: 'ERR_WRI   write error',
    0xE7: 'ERR_SEQ   sequence error',
}

# Communication setting phase, per Renesas R01AN5562 rev 1.70 section 4.6.
# The host sends 0x00 until the device answers ACK, then the generic code, and
# the device answers the boot code and enters the command acceptable phase.
LOW_PULSE = 0x00
ACK_CODE = 0x00
GENERIC_CODE = 0x55
# 0xC6 for the Cortex-M33 families, which is what an RA4E1 is (R01AN5562 lists
# RA4E1 as Product Group-B, and sections 4.6.2/4.6.3/7 all say 0xC6). Upstream
# raflash uses 0xC3, which is not this family's value - and 0xC3 is ERR_FLOW in
# the error table below, so mistaking one for the other reads a refusal as a
# successful handshake.
BOOT_CODE = 0xC6
BOOT_CODE_ALT = 0xC3


def calc_sum(cmd, data):
    data_len = len(data)
    lnh = (data_len + 1 & 0xFF00) >> 8
    lnl = data_len + 1 & 0x00FF
    res = lnh + lnl + cmd
    for i in range(data_len):
        if isinstance(data[i], str):
            res += int(data[i], 16)
        elif isinstance(data[i], int):
            res += data[i]
        else:
            res += ord(data[i])
    res = ~(res - 1) & 0xFF                      # two's complement
    return (lnh, lnl, res)


def pack_pkt(res, data, ack=False):
    """Packet layout: [SOD|LNH|LNL|RES|DAT|SUM|ETX]"""
    SOD = 0x81 if ack else 0x01
    if len(data) > 1024:
        raise Exception(f'Data packet too large, length is {len(data)} (>1024)')
    LNH, LNL, SUM = calc_sum(int(res), data)
    if not isinstance(data, bytes):
        DAT = bytes([int(x, 16) for x in data])
    else:
        DAT = data
    fmt = '<BBBB' + str(len(data)) + 'sBB'
    return struct.pack(fmt, SOD, LNH, LNL, res, DAT, SUM, 0x03)


def unpack_pkt(data):
    """Returns the payload as a list of '0xNN' strings, as upstream does."""
    if len(data) < 6:
        raise Exception(f'Short packet: {len(data)} bytes, expected at least 6')
    SOD, LNH, LNL, RES = struct.unpack('<BBBB', data[0:4])
    if SOD != 0x81:
        raise Exception(f'Wrong start of packet, got 0x{SOD:02X} expected 0x81')
    pkt_len = (LNH << 8 | LNL) - 1
    raw = struct.unpack_from('<' + str(pkt_len) + 's', data, 4)[0]
    message = ['0x{:02X}'.format(b) for b in raw]
    if RES & 0x80:
        code = int(message[0], 16)
        raise DeviceError(code, error_codes.get(code, f'unknown error 0x{code:02X}'))
    SUM, ETX = struct.unpack_from('<BB', data, 4 + pkt_len)
    _, _, local_sum = calc_sum(RES, message)
    if SUM != local_sum:
        raise Exception(f'Checksum mismatch, read {SUM} expected {local_sum}')
    if ETX != 0x03:
        raise Exception('Packet ETX error')
    return message


class DeviceError(Exception):
    """The MCU answered with an error status. code is the raw byte."""

    def __init__(self, code, text):
        super().__init__(f'MCU error 0x{code:02X}: {text}')
        self.code = code
        self.text = text
