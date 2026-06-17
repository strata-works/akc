#!/usr/bin/env python3
"""Standalone extractor for Encarta .EIT files (ITOLITLS / Microsoft Reader LIT containers).
Parsing logic adapted from calibre's LIT reader (GPLv3, Kovid Goyal / Marshall T. Vandegrift).
Requires the compiled lzx module (calibre's lzx C extension) on sys.path.
"""
import struct
import sys

import os

def _lzx():
    if os.environ.get('STRATA_USE_PY_LZX') != '1':
        try:
            from strata_akc_dump import mspack_lzx
            return mspack_lzx
        except Exception as exc:
            if os.environ.get('STRATA_REQUIRE_MSPACK_LZX') == '1':
                raise
            print(f"[lit] libmspack LZX unavailable, falling back to Python decoder: {exc}", file=sys.stderr)
    sys.path.insert(0, os.environ.get('LZX_PATH', os.path.join(os.path.dirname(__file__), '..', 'lzxbuild')))
    import lzx as _mod
    return _mod

DESENCRYPT_GUID = '{67F6E4A2-60BF-11D3-8540-00C04F58C3CF}'
LZXCOMPRESS_GUID = '{0A9007C6-4076-11D3-8789-0000F8105754}'

CONTROL_TAG = 4
CONTROL_WINDOW_SIZE = 12
RESET_HDRLEN = 12
RESET_UCLENGTH = 16
RESET_INTERVAL = 32


def u32(b): return struct.unpack('<L', b[:4])[0]
def u16(b): return struct.unpack('<H', b[:2])[0]
def int32(b): return struct.unpack('<l', b[:4])[0]


def encint(byts, remaining):
    pos, val = 0, 0
    ba = bytearray(byts)
    while remaining > 0:
        b = ba[pos]
        pos += 1
        remaining -= 1
        val <<= 7
        val |= (b & 0x7f)
        if b & 0x80 == 0:
            break
    return val, byts[pos:], remaining


def msguid(b):
    v = struct.unpack('<LHHBBBBBBBB', b[:16])
    return '{{{:08X}-{:04X}-{:04X}-{:02X}{:02X}-{:02X}{:02X}{:02X}{:02X}{:02X}{:02X}}}'.format(*v)


class EitFile:
    PIECE_SIZE = 16

    def __init__(self, path):
        self.stream = open(path, 'rb')
        hdr = self.read_raw(0, 24)
        if hdr[:8] != b'ITOLITLS':
            raise ValueError('Not an ITOLITLS container')
        self.version = u32(hdr[8:])
        self.hdr_len = u32(hdr[12:])
        self.num_pieces = u32(hdr[16:])
        self.sec_hdr_len = u32(hdr[20:])
        self.header = self.read_raw(0, self.hdr_len + self.num_pieces * self.PIECE_SIZE)
        self.read_secondary_header()
        self.read_header_pieces()
        self.read_section_names()

    def read_raw(self, offset, size):
        self.stream.seek(offset)
        return self.stream.read(size)

    def read_content(self, offset, size):
        return self.read_raw(self.content_offset + offset, size)

    def read_secondary_header(self):
        offset = self.hdr_len + (self.num_pieces * self.PIECE_SIZE)
        byts = self.read_raw(offset, self.sec_hdr_len)
        offset = int32(byts[4:])
        while offset < len(byts):
            blocktype = byts[offset:offset + 4]
            blockver = u32(byts[offset + 4:])
            if blocktype == b'CAOL':
                assert blockver == 2, f'CAOL v{blockver}'
                self.entry_chunklen = u32(byts[offset + 20:])
                self.count_chunklen = u32(byts[offset + 24:])
                self.entry_unknown = u32(byts[offset + 28:])
                self.count_unknown = u32(byts[offset + 32:])
                offset += 48
            elif blocktype == b'ITSF':
                assert blockver == 4, f'ITSF v{blockver}'
                self.content_offset = u32(byts[offset + 16:])
                offset += 48
            else:
                offset += 1  # scan forward defensively

    def read_header_pieces(self):
        src = self.header[self.hdr_len:]
        for i in range(self.num_pieces):
            piece = src[i * self.PIECE_SIZE:(i + 1) * self.PIECE_SIZE]
            offset, size = u32(piece), int32(piece[8:])
            piece = self.read_raw(offset, size)
            if i == 1:
                self.read_directory(piece)

    def read_directory(self, piece):
        assert piece.startswith(b'IFCM'), 'piece 1 is not main directory'
        chunk_size, num_chunks = int32(piece[8:12]), int32(piece[24:28])
        self.entries = {}
        for i in range(num_chunks):
            offset = 32 + (i * chunk_size)
            chunk = piece[offset:offset + chunk_size]
            tag, chunk = chunk[:4], chunk[4:]
            if tag != b'AOLL':
                continue
            remaining, chunk = int32(chunk[:4]), chunk[4:]
            remaining = chunk_size - (remaining + 48)
            entries = u16(chunk[-2:])
            if entries == 0:
                entries = (2 ** 16) - 1
            chunk = chunk[40:]
            for _ in range(entries):
                if remaining <= 0:
                    break
                namelen, chunk, remaining = encint(chunk, remaining)
                if namelen > remaining - 3:
                    break
                try:
                    name = chunk[:namelen].decode('utf-8')
                except UnicodeDecodeError:
                    break
                chunk = chunk[namelen:]
                remaining -= namelen
                section, chunk, remaining = encint(chunk, remaining)
                offset, chunk, remaining = encint(chunk, remaining)
                size, chunk, remaining = encint(chunk, remaining)
                self.entries[name] = (section, offset, size)

    def read_section_names(self):
        raw = self.get_file('::DataSpace/NameList')
        pos = 4
        num_sections = u16(raw[2:pos])
        self.section_names = [''] * num_sections
        self.section_data = [None] * num_sections
        for s in range(num_sections):
            size = u16(raw[pos:pos + 2])
            pos += 2
            size = size * 2 + 2
            self.section_names[s] = raw[pos:pos + size].decode('utf-16-le').rstrip('\0')
            pos += size

    def get_file(self, name):
        section, offset, size = self.entries[name]
        if section == 0:
            return self.read_content(offset, size)
        data = self.get_section(section)
        return data[offset:offset + size]

    def get_section(self, section):
        if self.section_data[section] is None:
            self.section_data[section] = self.get_section_uncached(section)
        return self.section_data[section]

    def get_section_uncached(self, section):
        name = self.section_names[section]
        path = '::DataSpace/Storage/' + name
        transform = self.get_file(path + '/Transform/List')
        content = self.get_file(path + '/Content')
        control = self.get_file(path + '/ControlData')
        while len(transform) >= 16:
            csize = (int32(control) + 1) * 4
            guid = msguid(transform)
            if guid == LZXCOMPRESS_GUID:
                reset_table = self.get_file('/'.join((
                    '::DataSpace/Storage', name, 'Transform',
                    LZXCOMPRESS_GUID, 'InstanceData/ResetTable')))
                content = self.decompress(content, control, reset_table)
                control = control[csize:]
            elif guid == DESENCRYPT_GUID:
                raise ValueError('DRM-encrypted section: ' + name)
            else:
                raise ValueError('Unknown transform: ' + guid)
            transform = transform[16:]
        return content

    def decompress(self, content, control, reset_table):
        assert control[CONTROL_TAG:CONTROL_TAG + 4] == b'LZXC', 'bad ControlData'
        result = []
        # Encarta's LZXC v3 control data stores a compact window value.
        # raw=4 decodes with libmspack as a 128 KiB (2^17) window.
        window_size = u32(control[CONTROL_WINDOW_SIZE:]) + 13
        lzx = _lzx()
        reset_interval = int32(reset_table[RESET_INTERVAL:])
        if hasattr(lzx, 'init'):
            lzx.init(window_size)
        ofs_entry = int32(reset_table[RESET_HDRLEN:]) + 8
        uclength = int32(reset_table[RESET_UCLENGTH:])
        accum = reset_interval
        bytes_remaining = uclength
        window_bytes = (1 << window_size)
        base = 0
        while ofs_entry < len(reset_table):
            if accum >= window_bytes:
                accum = 0
                size = int32(reset_table[ofs_entry:])
                if bytes_remaining >= window_bytes:
                    if hasattr(lzx, 'reset'):
                        lzx.reset()
                    result.append(lzx.decompress(content[base:size], window_bytes))
                    bytes_remaining -= window_bytes
                    base = size
            accum += int32(reset_table[RESET_INTERVAL:])
            ofs_entry += 8
        if 0 < bytes_remaining < window_bytes:
            if hasattr(lzx, 'reset'):
                lzx.reset()
            result.append(lzx.decompress(content[base:], bytes_remaining))
        return b''.join(result)


def main(src, outdir):
    f = EitFile(src)
    print(f'version={f.version} pieces={f.num_pieces} entries={len(f.entries)}')
    print('sections:', f.section_names)
    skipped, written = 0, 0
    for name, (section, offset, size) in sorted(f.entries.items()):
        if name.startswith('::'):
            skipped += 1
            continue
        try:
            data = f.get_file(name)
        except Exception as e:
            print(f'FAIL {name}: {e}')
            continue
        rel = name.lstrip('/')
        if not rel or name.endswith('/'):
            skipped += 1
            continue
        dest = os.path.join(outdir, rel)
        os.makedirs(os.path.dirname(dest) or outdir, exist_ok=True)
        with open(dest, 'wb') as out:
            out.write(data)
        written += 1
    print(f'written={written} internal-skipped={skipped}')


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])


def extract_lit(path, out_dir):
    """Extract a LIT/EIT/STE container to out_dir. Returns count of files written.
    PORTABLE entry point used by the CLI. Wraps the module's main(src, outdir)."""
    import os as _os
    out_dir = str(out_dir)
    _os.makedirs(out_dir, exist_ok=True)
    main(path, out_dir)
    return sum(len(files) for _, _, files in _os.walk(out_dir))
