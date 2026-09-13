# Derived from raflash by Robin Krens (https://github.com/robinkrens/raflash)
# Copyright (C) Robin Krens - 2024. GNU GPL v2 or later; see packer.py for the
# full notice.
#
# CHANGES FROM UPSTREAM:
#   - Device discovery reports every candidate port instead of silently
#     returning the first VID match, so a failed connect is diagnosable.
#   - Area selection is by address rather than hardcoded to area 0, so data
#     flash can be read as well as code flash.
#   - No erase or write path exists.
#   - Adds usb_device_present() and wait_for_port(). The board holds boot mode
#     for only a few seconds, so the dumper has to be waiting before the board
#     is power-cycled rather than started afterwards.

import os
import struct
import time

import serial
import serial.tools.list_ports

from .packer import (ARE_CMD, REA_CMD, SIG_CMD, ACK_CODE, BOOT_CODE,
                     BOOT_CODE_ALT, GENERIC_CODE, LOW_PULSE, DeviceError,
                     pack_pkt, unpack_pkt)

# The RA boot ROM enumerates as USB CDC under Renesas' own IDs, so it shows up
# as /dev/ttyACM* rather than a raw USB device. No udev rule is needed when
# running as root.
VENDOR_ID = 0x045B
PRODUCT_ID = 0x0261

MAX_TRANSFER_SIZE = 2048 + 6


def find_ra_boot_tty():
    """The tty for an RA boot ROM on the bus right now, or None. Race-tolerant.

    Walks sysfs directly instead of asking pyserial. pyserial's Linux backend
    reads idVendor/idProduct for *every* tty it finds and does int(x, 16) on the
    result with no guard; when a USB device disappears between the readdir and
    the read - which is precisely what SetCBPower 0 does, twelve times a run,
    while this polls at 50 ms - read_line returns None and the whole dumper dies
    with "int() can't convert non-string with explicit base". Enumerating a bus
    that is being power-cycled cannot be allowed to raise.

    Every filesystem access here is inside try/except: anything that vanishes
    mid-walk is simply not the device we are looking for this time round.
    """
    root = '/sys/bus/usb/devices'
    want_v, want_p = '%04x' % VENDOR_ID, '%04x' % PRODUCT_ID
    try:
        names = os.listdir(root)
    except OSError:
        return None
    for name in names:
        dev = os.path.join(root, name)
        try:
            with open(os.path.join(dev, 'idVendor')) as f:
                if f.read().strip().lower() != want_v:
                    continue
            with open(os.path.join(dev, 'idProduct')) as f:
                if f.read().strip().lower() != want_p:
                    continue
            ifaces = sorted(os.listdir(dev))
        except OSError:
            continue
        # Interfaces are children named like "3-3:1.3"; cdc_acm puts the tty
        # under <interface>/tty/ttyACMn. Most interfaces have no tty directory
        # at all - on a Steam Controller only 1 of 5 does - so that listdir
        # must fail per-interface, not abandon the device.
        for iface in ifaces:
            if not iface.startswith(name + ':'):
                continue
            try:
                ttys = sorted(os.listdir(os.path.join(dev, iface, 'tty')))
            except OSError:
                continue
            for tty in ttys:
                node = '/dev/' + tty
                if os.path.exists(node):
                    return node
    return None


def list_candidate_ports():
    """Every serial port on the system, flagged for whether it is an RA boot ROM.

    Diagnostics only - discovery uses find_ra_boot_tty(). Kept tolerant of the
    same enumeration race, so a failure report can never itself crash.
    """
    out = []
    try:
        ports = serial.tools.list_ports.comports()
    except Exception:
        ports = []
    for p in ports:
        try:
            out.append({
                'device': p.device,
                'vid': p.vid,
                'pid': p.pid,
                'description': p.description,
                'is_ra_boot': p.vid == VENDOR_ID and p.pid == PRODUCT_ID,
                'is_vid_match': p.vid == VENDOR_ID,
            })
        except Exception:
            continue
    return out


def usb_device_present():
    """Is 045b:0261 on the USB bus right now, regardless of any tty node?

    Asked separately from list_candidate_ports() because the two can disagree,
    and the difference matters. On a Galileo the boot ROM enumerates and cdc_acm
    binds within ~30 ms, but the board leaves boot mode a few seconds later; a
    port list taken after that says "no device" for a completely different reason
    than a board that never entered boot mode at all. Reporting both as "not in
    boot mode" sent a real operator chasing the wrong problem.

    Reads sysfs rather than shelling out to lsusb, so it needs no extra tooling
    and no root.
    """
    root = '/sys/bus/usb/devices'
    if not os.path.isdir(root):
        return False
    want_v, want_p = '%04x' % VENDOR_ID, '%04x' % PRODUCT_ID
    for name in os.listdir(root):
        try:
            with open(os.path.join(root, name, 'idVendor')) as f:
                if f.read().strip().lower() != want_v:
                    continue
            with open(os.path.join(root, name, 'idProduct')) as f:
                if f.read().strip().lower() == want_p:
                    return True
        except OSError:
            continue
    return False


def wait_for_port(timeout=60.0, poll=0.05, on_tick=None):
    """Block until an RA boot-mode serial port exists, and return its device path.

    The board only stays in boot mode for a few seconds, so this polls fast and
    is meant to be already running before the board is power-cycled. Returns
    None on timeout.
    """
    deadline = time.monotonic() + timeout
    saw_usb = False
    while time.monotonic() < deadline:
        node = find_ra_boot_tty()
        if node:
            return node
        if not saw_usb and usb_device_present():
            # The device is on the bus but has no tty yet; cdc_acm is still
            # binding. Worth recording - it separates "never appeared" from
            # "appeared but we were too slow".
            saw_usb = True
        if on_tick:
            on_tick(saw_usb)
        time.sleep(poll)
    return None


class RABoot:
    def __init__(self, port=None, verbose=True, trace=False, budget=5.0):
        self.verbose = verbose
        self.tracing = trace
        self.log = []
        self.entry = None
        self._t0 = time.monotonic()
        self.dev = None
        self.chip_layout = {}
        self.port = port or self._find_port()
        self._open(self.port)
        if not self._handshake(budget=budget):
            still_there = usb_device_present()
            raise RuntimeError(
                'Device did not answer the boot handshake.\n'
                f'  Port: {self.port}\n'
                f'  Still on the USB bus afterwards: '
                f'{"yes" if still_there else "no - it left mid-handshake"}\n'
                '  Traffic:\n' + self.handshake_report())
        if self.verbose:
            print(f'  handshake acknowledged via {self.entry}')

    def _find_port(self):
        # sysfs first: it is the race-tolerant path, and the only one used
        # while power is being cycled.
        node = find_ra_boot_tty()
        if node:
            return node
        cands = list_candidate_ports()
        exact = [c for c in cands if c['is_ra_boot']]
        if exact:
            return exact[0]['device']
        loose = [c for c in cands if c['is_vid_match']]
        if loose:
            return loose[0]['device']
        # Only USB ports can be the boot ROM; a Deck's ~30 /dev/ttyS* would bury
        # the listing for nothing.
        usb = [c for c in cands if c['vid'] is not None]
        if usb:
            listing = '\n'.join(
                f"    {c['device']}  {c['vid']:04x}:{c['pid']:04x}  {c['description']}"
                for c in usb)
        else:
            listing = '    (no USB serial ports)'
        raise RuntimeError(
            f'No Renesas boot-mode device ({VENDOR_ID:04x}:{PRODUCT_ID:04x}) found.\n'
            f'  USB serial ports present:\n{listing}\n'
            '  The board is not in boot mode. Hold Right Bumper + Right Upper Back\n'
            '  (R4) + Right Quick Access, and power-cycle the controller board with\n'
            '  BatCtrl SetCBPower 0 then 1. See README.md.')

    def _open(self, port):
        try:
            # 9600 is the handshake rate; over USB CDC the value is nominal.
            # The read timeout is deliberately tiny: the board stays in boot
            # mode for roughly 3.3 s, so a 1 s blocking read is a third of the
            # entire budget spent learning nothing.
            self.dev = serial.Serial(port, 9600, timeout=0.05, write_timeout=0.5)
        except Exception as err:
            raise RuntimeError(f'Failed to open {port}: {err}')
        # Some CDC firmware will not transmit until the host raises DTR.
        try:
            self.dev.dtr = True
            self.dev.rts = True
        except Exception:
            pass
        try:
            self.dev.reset_input_buffer()
            self.dev.reset_output_buffer()
        except Exception:
            pass

    def _trace(self, direction, data, note=''):
        self.log.append((time.monotonic() - self._t0, direction, bytes(data), note))
        if self.tracing:
            ts = time.monotonic() - self._t0
            body = bytes(data).hex(' ') if data else '(nothing)'
            print(f'    [{ts:6.3f}] {direction} {body} {note}'.rstrip())

    def _read_for(self, nbytes, budget):
        """Read up to nbytes, giving up after `budget` seconds."""
        deadline = time.monotonic() + budget
        buf = bytearray()
        while len(buf) < nbytes and time.monotonic() < deadline:
            chunk = self.dev.read(nbytes - len(buf))
            if chunk:
                buf += chunk
        return buf

    def _handshake(self, budget=5.0):
        """Complete the communication setting phase.

        Per Renesas R01AN5562 rev 1.70 section 4.6.3, "Settings of the USB
        communication (For GrpA, GrpB, and GrpD)" - and that document lists
        RA4E1 as Product Group-B, so it is the governing spec for this part:

          1. host sends 0x00 until the device answers 0x00 (ACK). The device
             needs three *consecutive* 0x00 to select USB as the communication
             method.
          2. host sends 0x55 (generic code)
          3. device answers 0xC6 (boot code) and enters the command acceptable
             phase.

        Step 1 is not optional over USB. The earlier version here opened with
        the generic code, assuming USB skipped the communication setting phase
        the way auto-baud is skipped - it does not. Worse, section 4.6.3 says
        that before the three bytes land, "if data other than 0x00 is received
        ... the count value will be reset", so leading with 0x55 actively held
        the device in the setting phase instead of advancing it.

        That matters beyond a failed connect: section 4.4 makes the device
        software-reset if MD reads high *during communication mode judgement*.
        Staying stuck in that phase is what leaves us exposed to it.

        Everything is bounded and logged - each real attempt costs an operator
        a power cycle with three buttons held down.
        """
        deadline = time.monotonic() + budget

        # --- step 1: 0x00 until ACK ---
        acked = False
        rounds = 0
        while time.monotonic() < deadline and not acked:
            rounds += 1
            try:
                self.dev.write(bytes([LOW_PULSE]) * 3)
                self.dev.flush()
                self._trace('TX', bytes([LOW_PULSE]) * 3, f'0x00 x3 #{rounds}')
                ret = self._read_for(1, 0.1)
                if ret:
                    self._trace('RX', ret, 'expecting 0x00 ACK')
                if ret and ret[0] == ACK_CODE:
                    acked = True
                    break
            except Exception as err:
                self._trace('ERR', b'', f'0x00: {err}')
            if not usb_device_present():
                self._trace('ERR', b'', 'device left the USB bus during ACK wait')
                return False

        if not acked:
            self._trace('ERR', b'', 'no ACK - never selected USB as the comms method')
            return False

        # --- step 2: generic code -> boot code ---
        try:
            self.dev.write(bytes([GENERIC_CODE]))
            self.dev.flush()
            self._trace('TX', bytes([GENERIC_CODE]), 'generic code')
            ret = self._read_for(1, max(0.5, deadline - time.monotonic()))
            self._trace('RX', ret, 'expecting 0xC6 boot code')
        except Exception as err:
            self._trace('ERR', b'', f'0x55: {err}')
            return False

        if not ret:
            self._trace('ERR', b'', 'ACKed but no boot code')
            return False
        if ret[0] == BOOT_CODE:
            self.entry = 'generic-code (0xC6)'
            return True
        if ret[0] == BOOT_CODE_ALT:
            # Not this family's documented value. Say so rather than quietly
            # treating it as success - 0xC3 is also ERR_FLOW in the error table.
            self.entry = 'generic-code (0xC3 - NOT the documented GrpB value)'
            self._trace('ERR', b'', 'got 0xC3, expected 0xC6 - proceeding, but note it')
            return True
        self._trace('ERR', b'', f'unexpected boot code 0x{ret[0]:02X}')
        return False

    def handshake_report(self):
        """Everything sent and received, for diagnosing a failed connect."""
        if not self.log:
            return '    (no traffic recorded)'
        lines = []
        for ts, direction, data, note in self.log:
            body = data.hex(' ') if data else '(nothing)'
            lines.append(f'    [{ts:6.3f}] {direction} {body} {note}'.rstrip())
        return '\n'.join(lines)

    def send(self, packed):
        self.dev.write(packed)

    def recv(self, exp_len, timeout=None):
        if exp_len > MAX_TRANSFER_SIZE:
            raise ValueError(f'length {exp_len} over max transfer size')
        msg = bytearray()
        deadline = time.time() + (timeout or 10)
        while len(msg) < exp_len:
            buf = self.dev.read(exp_len - len(msg))
            if buf:
                msg += buf
            elif time.time() > deadline:
                break
        return msg

    def recv_packet(self, timeout=None):
        """Read one reply using its own length field.

        Upstream reads a fixed byte count per command, which works only for
        success replies - an error reply is 6 bytes and would stall the read
        until timeout and then be misreported. Taking the length from the
        header handles both, so a locked or refusing device is diagnosed
        immediately instead of looking like a dead link.
        """
        header = self.recv(4, timeout=timeout)
        if len(header) < 4:
            raise RuntimeError(
                f'No reply from device (got {len(header)} of 4 header bytes)')
        _sod, lnh, lnl, _res = struct.unpack('<BBBB', header)
        body = self.recv((lnh << 8 | lnl) - 1 + 2, timeout=timeout)
        return unpack_pkt(bytes(header) + bytes(body))

    # ---- queries -------------------------------------------------------

    def signature(self):
        """Per R01AN5562 rev 1.70 section 6.15.2.2.

        Upstream raflash parses '>IIBBH' - a leading 4-byte SCI clock, then a
        2-byte boot firmware version. That is the older RA layout (R01AN5372).
        The Cortex-M33 families have no SCI field at all, a 3-byte version, and
        16 bytes each of device ID and product name. Parsed the old way, a
        perfectly healthy RA4E1 reports "unknown type 0x4E, boot firmware
        version 75.44" - every field shifted, and nothing obviously wrong
        enough to look like a parse bug.
        """
        self.send(pack_pkt(SIG_CMD, ''))
        msg = self.recv_packet()
        body = bytes(int(x, 16) for x in msg)
        if len(body) < 41:
            raise RuntimeError(f'Signature payload truncated ({len(body)}/41)')
        RMB, = struct.unpack('>I', body[0:4])
        NOA = body[4]
        TYP = body[5]
        BFV = body[6:9]
        DID = body[9:25]
        PTN = body[25:41]
        return {
            'type_raw': TYP,
            'chip': {0x01: 'GrpA/GrpB (RA4M2/M3, RA6M4/M5, RA4E1, RA6E1)',
                     0x02: 'GrpC (RA6T2)',
                     0x05: 'GrpD (RA4E2, RA6E2, RA4T1, RA6T3)'}.get(
                         TYP, f'unknown type 0x{TYP:02X}'),
            'max_baud': RMB,
            'num_areas': NOA,
            'boot_fw_version': f'{BFV[0]}.{BFV[1]}.{BFV[2]}',
            'device_id': DID.hex(),
            'product': PTN.decode('ascii', 'replace').rstrip(),
        }

    def area_info(self, num_areas=4):
        """Per R01AN5562 section 6.16.2.2. The area *number* means nothing.

        Earlier this assumed 0 = code, 1 = data, 2 = config. That is wrong, and
        section 6.16.5's worked example for RA4E1 says so outright:

            0  User area0(S)  KOA 0x00  0x00000000-0x0000FFFF   erase 8 KB
            1  User area0(L)  KOA 0x00  0x00010000-0x000FFFFF   erase 32 KB
            2  Data area      KOA 0x10  0x08000000-0x08001FFF   erase 64 B
            3  Config area    KOA 0x20  0x0100A100-0x0100A2FF

        Code flash is split across *two* areas with different block sizes, data
        flash is area 2, and there is a fourth area we never asked for. What a
        region is comes from KOA, not from its index.
        """
        cfg = {}
        for i in range(num_areas):
            try:
                self.send(pack_pkt(ARE_CMD, [str(i)]))
                msg = self.recv_packet()
                KOA, SAD, EAD, EAU, WAU, RAU, CAU = struct.unpack(
                    '>BIIIIII', bytes(int(x, 16) for x in msg)[:25])
                cfg[i] = {'KOA': KOA, 'kind': self.kind_of_area(KOA),
                          'SAD': SAD, 'EAD': EAD,
                          'erase_unit': EAU, 'write_unit': WAU,
                          'read_unit': RAU, 'crc_unit': CAU,
                          # RAU == 0 means the Read command is not available for
                          # this area (6.16.2.2 note *1) - a different thing
                          # from a locked part, and worth saying so.
                          'readable': RAU != 0}
            except DeviceError as e:
                cfg[i] = {'error': str(e), 'code': f'0x{e.code:02X}'}
            except Exception as e:
                cfg[i] = {'error': str(e)}
        self.chip_layout = cfg
        return cfg

    @staticmethod
    def kind_of_area(koa):
        """KOA high nibble: 0x0N user, 0x1N data, 0x2N config."""
        return {0x0: 'user (code flash)',
                0x1: 'data flash',
                0x2: 'config'}.get(koa >> 4, f'unknown KOA 0x{koa:02X}')

    def regions_by_kind(self):
        """Readable address spans, merging areas that are one region really.

        Code flash arrives as two areas only because the erase block size
        changes partway through; as something to read it is one range. Merging
        is legal because 6.20.3 refuses a read only "if SAD and EAD belong to
        different KOA" - not different area indices - and both halves report
        KOA 0x00.

        Only *adjacent* areas are merged, so a part that splits a kind across
        an address gap still gets one read per span instead of one read over
        the hole, which the device would refuse with a parameter error.
        """
        areas = sorted((a for a in self.chip_layout.values()
                        if 'SAD' in a and a.get('readable', True)),
                       key=lambda a: (a['KOA'], a['SAD']))
        spans = []
        for a in areas:
            if spans and spans[-1][0] == a['KOA'] and spans[-1][3] + 1 == a['SAD']:
                koa, kind, lo, _ = spans[-1]
                spans[-1] = (koa, kind, lo, a['EAD'])
                continue
            spans.append((a['KOA'], a['kind'], a['SAD'], a['EAD']))
        return [(kind, lo, hi) for _, kind, lo, hi in spans]

    def area_for(self, addr):
        for idx, a in self.chip_layout.items():
            if 'SAD' in a and a['SAD'] <= addr <= a['EAD']:
                return idx, a
        return None, None

    # ---- read ----------------------------------------------------------

    def read_range(self, start, end, progress=None):
        """Inclusive end, as the protocol expects. Returns bytes."""
        if end <= start:
            raise ValueError('end must be greater than start')
        sad = ['0x{:02X}'.format(b) for b in struct.pack('>I', start)]
        ead = ['0x{:02X}'.format(b) for b in struct.pack('>I', end)]
        self.send(pack_pkt(REA_CMD, sad + ead))

        total = end - start + 1
        npkt = (total + 1023) // 1024
        out = bytearray()
        for i in range(npkt):
            chunk = self.recv_packet(timeout=15)
            out += bytes(int(x, 16) for x in chunk)
            if progress:
                # Clamp: the device always sends a full 1,024-byte packet, so a
                # region smaller than that would otherwise report 200%.
                progress(min(len(out), total), total)
            if len(out) < total:
                self.send(self._read_continue_pkt())
        return bytes(out[:total])

    @staticmethod
    def _read_continue_pkt():
        """The between-chunks acknowledgement, per R01AN5562 6.20.2.2.

        It is a *data* packet (SOD 0x81) shaped like [status OK], not a one-byte
        ack: LNL 0x0A, RES 0x15, STS 0x00, then ST2 and ADR both 0xFFFFFFFF.
        Section 7.10 confirms it by naming "Data packet [status OK]" as the
        Read command's continuation.

        Upstream sends a single 0x00 payload, giving LNL 0x02. Section 6.20.3
        rejects that - "packet length in the received data packet do not comply
        format with this command" - with 0xC1 ERR_PCKT, which is exactly what a
        real RA4E1 returned after the first 1,024 bytes. The first chunk always
        arrives, so the bug looks like a device refusing partway rather than a
        malformed ack.
        """
        payload = bytes([0x00]) + b'\xFF' * 4 + b'\xFF' * 4   # STS, ST2, ADR
        return pack_pkt(REA_CMD, payload, ack=True)

    def close(self):
        try:
            if self.dev:
                self.dev.close()
        except Exception:
            pass
