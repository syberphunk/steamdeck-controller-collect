#!/usr/bin/env python3
"""Merge the collected results, name them, and pack a single .tar.gz.

Reads whatever of detect.json, usb-dump.json and rom-dump.json exist in the
working directory, writes a combined manifest.json and README.txt, renames the
directory to describe what was captured, and packs it.

The name carries the Deck model, SteamOS version, controller type and hardware
ID, the application and bootloader build stamps, and the Deck and controller
board serial numbers, so that archives from many units sort and compare
without being opened.
"""

import argparse
import datetime
import json
import os
import pwd
import sys
import tarfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import (COLLECTOR_VERSION, TYPE_NAMES, model_tag,  # noqa: E402
                    say, sha256, slug, steamos_tag)


def load(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def archive_name(detect, usb, rom):
    """Build the descriptive archive basename."""
    host = (usb or {}).get('host') or (detect or {}).get('host') or {}
    ident = (usb or {}).get('identity') or {}

    major = ident.get('major') or (detect or {}).get('major')
    hwid = ident.get('hw_id')

    app_build = ident.get('app_build')
    if not app_build:
        ts = (detect or {}).get('app_build_timestamps') or {}
        app_build = ts.get('primary')

    # The bootloader stamp comes from whichever path actually read the
    # bootloader region: USB on SAMD boards, the ROM path on RA4.
    bl_build = (rom or {}).get('bootloader_build') or ident.get('bootloader_build')

    dmi = host.get('dmi') or {}
    deck_serial = dmi.get('product_serial')
    board_serial = ident.get('board_serial')

    parts = ['steamdeck-controller', model_tag(host)]
    parts.append(f'type{major}' if major else 'typeunknown')
    parts.append(f'hwid{hwid}' if hwid is not None else 'hwidunknown')
    parts.append(steamos_tag(host))
    if app_build:
        parts.append(f'fw{slug(app_build, 12)}')
    if bl_build:
        parts.append(f'bl{slug(bl_build, 12)}')
    if deck_serial:
        parts.append(f'deck{slug(deck_serial, 20)}')
    if board_serial:
        parts.append(f'board{slug(board_serial, 20)}')
    parts.append(datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    return '-'.join(p for p in parts if p)


def apply_prefix(workdir, stem):
    """Rename every image to carry the descriptive stem.

    A .bin lifted out of the archive keeps its identity that way: which Deck,
    which SteamOS, which firmware and bootloader build, which serials. Returns
    the old name -> new name mapping so the stage manifests can be corrected.

    Names are capped at 250 bytes, under the 255-byte limit of the filesystems
    SteamOS uses, by trimming the stem rather than the descriptive suffix.
    """
    mapping = {}
    for name in sorted(os.listdir(workdir)):
        if not name.endswith('.bin') or name.startswith(stem):
            continue
        new = f'{stem}-{name}'
        if len(new.encode()) > 250:
            keep = 250 - len(name.encode()) - 1
            new = f'{stem[:keep]}-{name}' if keep > 0 else name
        if new == name or os.path.exists(os.path.join(workdir, new)):
            continue
        os.rename(os.path.join(workdir, name), os.path.join(workdir, new))
        mapping[name] = new
    return mapping


def retarget(obj, mapping):
    """Rewrite file names inside an already-written stage manifest.

    dump_usb.py records its files as a list of {'name': ...}; the ROM dumper
    records them as a dict keyed by name. Both appear verbatim in the combined
    manifest, so both have to follow the rename.
    """
    if isinstance(obj, dict):
        for key, val in list(obj.items()):
            if key == 'name' and isinstance(val, str) and val in mapping:
                obj[key] = mapping[val]
            elif key == 'files' and isinstance(val, dict):
                obj[key] = {mapping.get(k, k): v for k, v in val.items()}
                retarget(obj[key], mapping)
            else:
                retarget(val, mapping)
    elif isinstance(obj, list):
        for item in obj:
            retarget(item, mapping)


def scan_files(workdir, skip=()):
    out = []
    for name in sorted(os.listdir(workdir)):
        path = os.path.join(workdir, name)
        if os.path.isfile(path) and name not in skip:
            out.append({'name': name, 'bytes': os.path.getsize(path),
                        'sha256': sha256(path)})
    return out


def write_readme(path, manifest, stem=''):
    ident = manifest.get('controller') or {}
    host = manifest.get('host') or {}
    dmi = host.get('dmi') or {}
    osr = host.get('os_release') or {}

    with open(path, 'w') as fh:
        w = fh.write
        w('Steam Deck controller firmware capture\n')
        w('======================================\n')
        w(f'Created:    {manifest.get("created")}\n')
        w(f'Collector:  {manifest.get("collector_version")}\n\n')

        w('Deck\n----\n')
        w(f'  model            {dmi.get("product_name")} / {dmi.get("board_name")}\n')
        w(f'  serial           {dmi.get("product_serial")}\n')
        w(f'  SteamOS          {osr.get("NAME")} {osr.get("VERSION_ID")} '
          f'(build {osr.get("BUILD_ID")})\n')
        w(f'  kernel           {host.get("kernel")}\n')
        w(f'  firmware package {host.get("jupiter_hw_support")}\n\n')

        w('Controller\n----------\n')
        w(f'  type             {ident.get("type_name")}\n')
        bad_record = ident.get('device_info_crc_valid') is False
        w(f'  hardware ID      {ident.get("hw_id")}'
          f'{"  (SEE BELOW - not trustworthy)" if bad_record else ""}\n')
        w(f'  board            {ident.get("board")}\n')
        w(f'  primary MCU      {ident.get("primary_chip")}\n')
        w(f'  secondary MCU    {ident.get("secondary_chip") or "none - single MCU"}\n')
        w(f'  board serial     {ident.get("board_serial")}\n')
        w(f'  unit serial      {ident.get("unit_serial")}\n')
        w(f'  app build        {ident.get("app_build")}\n')
        w(f'  bootloader build {ident.get("bootloader_build")}\n\n')

        if bad_record:
            w('  ! The device-info record failed its own CRC check.\n'
              '!\n'
              '!   The hardware ID and both serial numbers above were read\n'
              '!   straight out of that record, so they are what is stored,\n'
              '!   not what the controller accepts. The running firmware\n'
              '!   rejects this record and substitutes a built-in hardware\n'
              '!   ID of its own, which looks like an ordinary ID and gives\n'
              '!   no sign that anything is wrong.\n'
              '!\n'
              '!   Nothing here caused it - this capture is read-only and\n'
              '!   the record was already in this state. But do not treat\n'
              '!   this capture as evidence of what hardware ID a board of\n'
              '!   this type carries.\n\n'.replace('\n!', '\n  !'))

        w('What was captured\n-----------------\n')
        for line in manifest.get('captured') or ['(nothing)']:
            w(f'  {line}\n')
        w('\n')

        files = manifest.get('files') or []
        if files:
            w('Files\n-----\n')
            if stem:
                w(f'  Image names are listed without their common prefix,\n')
                w(f'  "{stem}-".\n\n')
            for f in files:
                name = f['name']
                if stem and name.startswith(stem + '-'):
                    name = name[len(stem) + 1:]
                w(f'  {name:<44} {f["bytes"]:>10,} bytes\n')
            w('\n')

        notes = manifest.get('notes') or []
        if notes:
            w('Notes\n-----\n')
            for n in notes:
                w(f'  {n}\n')
            w('\n')

        w('Reading this archive\n--------------------\n')
        w('  Every image is named after the Deck and controller it came from, so\n')
        w('  the files stay identifiable once they are out of this folder. The\n')
        w('  part after that common prefix says what the file is:\n\n')
        w('  *-app.bin               application firmware\n')
        w('  *-bootloader-*.bin      bootloader, read in the normal pass\n')
        w('  *-rom-bootloader-*.bin  bootloader, read through the chip boot ROM\n')
        w('  *-dataflash.bin         per-unit factory data\n')
        w('  *-devinfo.bin           device-info partition: hardware ID, serials\n')
        w('  *-unit-blob.bin         per-unit calibration blob\n')
        w('  *-full-flash.bin        the whole flash as read, before carving\n\n')
        w('  primary-right / secondary-left name the two boards on models that\n')
        w('  have two; primary-single names the one board on models that do not.\n\n')
        w('  manifest.json           all of this in full, with SHA-256 hashes\n')
        w('  README.txt              this file\n')
        w('  *-log.txt               console output of each stage, as it happened\n\n')

        w('This capture is read-only. Nothing was erased or written to the\n')
        w('controller, and the Deck was not modified.\n\n')
        w('It contains this Deck\'s serial number and the controller board\'s\n')
        w('serial number, and the data-flash images hold per-unit factory\n')
        w('data. See PRIVACY.md in the repository before sharing it.\n')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--workdir', required=True,
                    help='directory holding the collected files')
    ap.add_argument('--destdir', required=True,
                    help='where to place the renamed directory and archive')
    ap.add_argument('--owner', default=os.environ.get('SUDO_USER') or '',
                    help='user to hand the results back to')
    args = ap.parse_args()

    workdir = os.path.abspath(args.workdir)
    detect = load(os.path.join(workdir, 'detect.json'))
    usb = load(os.path.join(workdir, 'usb-dump.json'))
    rom = load(os.path.join(workdir, 'rom-dump.json'))

    host = (usb or {}).get('host') or (detect or {}).get('host') or {}
    ident = dict((usb or {}).get('identity') or {})

    # The bootloader build is whatever the successful path found.
    if (rom or {}).get('bootloader_build'):
        ident['bootloader_build'] = rom['bootloader_build']
    if not ident.get('app_build'):
        ts = (detect or {}).get('app_build_timestamps') or {}
        ident['app_build'] = ts.get('primary')
    if not ident.get('type_name'):
        ident['type_name'] = TYPE_NAMES.get((detect or {}).get('major'))

    captured = []
    notes = list((usb or {}).get('notes') or [])
    if usb:
        captured.append(f'USB dump: {usb.get("assessment")}')
        for r in usb.get('mcus') or []:
            captured.append(f'  {r.get("label")} ({r.get("chip")}): '
                            f'{r.get("verdict")}')
    else:
        captured.append('USB dump: not run')

    if rom:
        regions = rom.get('regions') or {}
        got = [n for n, r in regions.items() if r.get('status') == 'read']
        refused = [n for n, r in regions.items() if r.get('status') != 'read']
        captured.append('ROM boot-mode dump: '
                        + (', '.join(got) if got else 'nothing read'))
        if refused:
            captured.append('  refused or failed: ' + ', '.join(refused))
        if rom.get('bootloader_build'):
            captured.append(f'  bootloader build {rom["bootloader_build"]}')
    elif (detect or {}).get('bootloader_source') == 'rom':
        captured.append('ROM boot-mode dump: not run - the bootloader region '
                        'of this board is not in this archive')

    name = archive_name(detect, usb, rom)

    # Name the images before anything is hashed or listed, so the manifest
    # describes the files as they will actually be found in the archive.
    mapping = apply_prefix(workdir, name)
    for stage in (detect, usb, rom):
        if stage:
            retarget(stage, mapping)

    manifest = {
        'collector_version': COLLECTOR_VERSION,
        'created': datetime.datetime.now().isoformat(timespec='seconds'),
        'host': host,
        'controller': ident,
        'captured': captured,
        'notes': notes,
        'files': scan_files(workdir),
        'detect': detect,
        'usb_dump': usb,
        'rom_dump': rom,
    }

    # README first, then the listing again so README.txt is in it, then the
    # manifest. manifest.json cannot list its own hash, and is the one file
    # missing from the listing.
    write_readme(os.path.join(workdir, 'README.txt'), manifest, name)
    manifest['files'] = scan_files(workdir, skip=('manifest.json',))
    with open(os.path.join(workdir, 'manifest.json'), 'w') as fh:
        json.dump(manifest, fh, indent=2, default=str)

    destdir = os.path.abspath(args.destdir)
    os.makedirs(destdir, exist_ok=True)
    final = os.path.join(destdir, name)
    if os.path.exists(final):
        final = f'{final}-{os.getpid()}'
    os.rename(workdir, final)

    archive = final + '.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        tar.add(final, arcname=os.path.basename(final))

    # Hand ownership back so the results are usable without sudo.
    if args.owner:
        try:
            pw = pwd.getpwnam(args.owner)
            for root, dirs, fnames in os.walk(final):
                for p in dirs + fnames:
                    os.chown(os.path.join(root, p), pw.pw_uid, pw.pw_gid)
            os.chown(final, pw.pw_uid, pw.pw_gid)
            os.chown(archive, pw.pw_uid, pw.pw_gid)
        except (KeyError, OSError):
            pass

    say(archive)
    return 0


if __name__ == '__main__':
    sys.exit(main())
