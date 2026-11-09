#!/usr/bin/env python3
"""Download one member from a remote ZIP without storing the full archive."""

from __future__ import annotations

import argparse
import binascii
import struct
import urllib.request
import zlib
from pathlib import Path


EOCD_SIGNATURE = b"PK\x05\x06"
CENTRAL_SIGNATURE = b"PK\x01\x02"
LOCAL_SIGNATURE = b"PK\x03\x04"
MAX_EOCD_SIZE = 65_557


def fetch_range(url: str, start: int, end: int) -> tuple[bytes, int]:
    request = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})
    with urllib.request.urlopen(request, timeout=120) as response:
        if response.status != 206:
            raise RuntimeError(f"Server ignored byte range {start}-{end}: HTTP {response.status}")
        content_range = response.headers.get("Content-Range", "")
        try:
            total_size = int(content_range.rsplit("/", 1)[1])
        except (IndexError, ValueError) as exc:
            raise RuntimeError(f"Invalid Content-Range header: {content_range!r}") from exc
        return response.read(), total_size


def remote_size(url: str) -> int:
    _, total_size = fetch_range(url, 0, 0)
    return total_size


def find_member(url: str, member_name: str) -> tuple[int, int, int, int, int]:
    total_size = remote_size(url)
    tail_start = max(0, total_size - MAX_EOCD_SIZE)
    tail, _ = fetch_range(url, tail_start, total_size - 1)
    eocd_offset = tail.rfind(EOCD_SIGNATURE)
    if eocd_offset < 0:
        raise RuntimeError("ZIP end-of-central-directory record was not found")

    _, disk_number, central_disk, disk_entries, total_entries, central_size, central_offset, _ = (
        struct.unpack_from("<4s4H2LH", tail, eocd_offset)
    )
    if disk_number != 0 or central_disk != 0 or disk_entries != total_entries:
        raise RuntimeError("Multi-disk ZIP archives are not supported")
    if central_offset == 0xFFFFFFFF or central_size == 0xFFFFFFFF:
        raise RuntimeError("ZIP64 archives are not supported")

    central, _ = fetch_range(url, central_offset, central_offset + central_size - 1)
    offset = 0
    while offset < len(central):
        values = struct.unpack_from("<4s6H3I5H2I", central, offset)
        if values[0] != CENTRAL_SIGNATURE:
            raise RuntimeError(f"Invalid central-directory entry at offset {offset}")
        flags, method = values[3], values[4]
        crc32, compressed_size, uncompressed_size = values[7:10]
        name_length, extra_length, comment_length = values[10:13]
        local_offset = values[16]
        name_start = offset + 46
        name = central[name_start:name_start + name_length].decode("utf-8")
        if name == member_name:
            if flags & 0x1:
                raise RuntimeError("Encrypted ZIP members are not supported")
            return local_offset, method, compressed_size, uncompressed_size, crc32
        offset = name_start + name_length + extra_length + comment_length

    raise RuntimeError(f"ZIP member was not found: {member_name}")


def download_member(url: str, member_name: str, output: Path) -> None:
    local_offset, method, compressed_size, uncompressed_size, expected_crc = find_member(url, member_name)
    header, _ = fetch_range(url, local_offset, local_offset + 29)
    values = struct.unpack("<4s5H3I2H", header)
    if values[0] != LOCAL_SIGNATURE:
        raise RuntimeError("Invalid local ZIP header")
    name_length, extra_length = values[-2:]
    data_start = local_offset + 30 + name_length + extra_length
    compressed, _ = fetch_range(url, data_start, data_start + compressed_size - 1)

    if method == 0:
        content = compressed
    elif method == 8:
        content = zlib.decompress(compressed, -zlib.MAX_WBITS)
    else:
        raise RuntimeError(f"Unsupported ZIP compression method: {method}")

    if len(content) != uncompressed_size:
        raise RuntimeError(f"Size mismatch: expected {uncompressed_size}, received {len(content)}")
    actual_crc = binascii.crc32(content) & 0xFFFFFFFF
    if actual_crc != expected_crc:
        raise RuntimeError(f"CRC mismatch: expected {expected_crc:08x}, received {actual_crc:08x}")

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(content)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("member")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    download_member(args.url, args.member, args.output)


if __name__ == "__main__":
    main()
