#!/usr/bin/env python3
"""
logrotate.py — trims the cryptobot .log/.err files so they can't grow unbounded.

Audit finding (low): 16 launchd jobs write to ~/cryptobot/*.log/*.err with no rotation;
the freqtrade bots alone add ~280 KB/day each (~100 MB/yr), which makes the logs slow
to grep exactly when you need them for debugging.

Uses COPYTRUNCATE semantics (keep the tail in place, same inode) — NOT move-and-replace —
because launchd holds these files open in append mode; replacing the inode would leave the
daemon writing to a now-unlinked file. Reading the tail then truncating keeps the live fd
valid. A negligible line or two can race during the truncate; fine for logs.
"""
import glob
import os

BASE = os.path.dirname(os.path.abspath(__file__))
MAX_BYTES = 2 * 1024 * 1024     # rotate anything over 2 MB
KEEP_BYTES = 512 * 1024         # keep the most recent ~512 KB


def rotate(path):
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size <= MAX_BYTES:
        return None
    try:
        with open(path, "r+b") as f:
            f.seek(-KEEP_BYTES, os.SEEK_END)
            tail = f.read()
            # start the kept portion at a clean line boundary
            nl = tail.find(b"\n")
            if 0 <= nl < len(tail) - 1:
                tail = tail[nl + 1:]
            header = (f"--- log rotated: trimmed {size//1024} KB → "
                      f"{len(tail)//1024} KB tail kept ---\n").encode()
            f.seek(0)
            f.write(header + tail)
            f.truncate()
        return (size, len(tail) + len(header))
    except OSError as e:
        print(f"  {os.path.basename(path)}: rotate failed ({e})", flush=True)
        return None


def main():
    files = glob.glob(os.path.join(BASE, "*.log")) + glob.glob(os.path.join(BASE, "*.err"))
    rotated = 0
    for path in sorted(files):
        r = rotate(path)
        if r:
            rotated += 1
            print(f"  {os.path.basename(path)}: {r[0]//1024} KB → {r[1]//1024} KB", flush=True)
    print(f"logrotate: checked {len(files)} files, rotated {rotated}", flush=True)


if __name__ == "__main__":
    main()
