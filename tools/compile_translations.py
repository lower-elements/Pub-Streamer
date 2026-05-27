"""Pure-Python .po → .mo compiler.

Generates binary GNU MO files without requiring msgfmt.
Run from the project root: python tools/compile_translations.py
"""

import pathlib
import struct
import sys


def parse_po(path: pathlib.Path) -> list[tuple[str, str]]:
    """Parse a .po file and return a list of (msgid, msgstr) pairs.

    Skips entries where msgstr is empty (untranslated) and the header entry
    (msgid == "").
    """
    pairs: list[tuple[str, str]] = []
    msgid = msgstr = None
    in_msgid = in_msgstr = False
    header_parts: list[str] = []

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()

        if line.startswith("#"):
            continue

        if line.startswith('msgid "'):
            if msgid is not None and msgstr is not None:
                _flush(pairs, msgid, msgstr)
            msgid = _unescape(line[7:-1])
            msgstr = None
            in_msgid = True
            in_msgstr = False

        elif line.startswith('msgstr "'):
            msgstr = _unescape(line[8:-1])
            in_msgid = False
            in_msgstr = True

        elif line.startswith('"') and line.endswith('"'):
            inner = _unescape(line[1:-1])
            if in_msgid:
                msgid = (msgid or "") + inner
            elif in_msgstr:
                msgstr = (msgstr or "") + inner

        elif line == "":
            if msgid is not None and msgstr is not None:
                _flush(pairs, msgid, msgstr)
            msgid = msgstr = None
            in_msgid = in_msgstr = False

    if msgid is not None and msgstr is not None:
        _flush(pairs, msgid, msgstr)

    return pairs


def _flush(pairs, msgid, msgstr):
    # Include the header entry (msgid=="") so the .mo file carries the charset declaration.
    if msgid is not None and msgstr:
        pairs.append((msgid, msgstr))


def _unescape(s: str) -> str:
    return (s.replace("\\n", "\n")
             .replace("\\t", "\t")
             .replace("\\r", "\r")
             .replace('\\"', '"')
             .replace("\\\\", "\\"))


def compile_mo(pairs: list[tuple[str, str]], out: pathlib.Path) -> None:
    """Write binary MO file from (msgid, msgstr) pairs.

    MO format (little-endian):
      magic (4) | revision (4) | N (4) | orig_offset (4) | trans_offset (4)
      | hash_size (4=0) | hash_offset (4=0)
      then N × (len, offset) for originals
      then N × (len, offset) for translations
      then the string data
    """
    pairs.sort(key=lambda p: p[0])   # must be sorted for binary search

    orig_enc  = [o.encode("utf-8") for o, _ in pairs]
    trans_enc = [t.encode("utf-8") for _, t in pairs]

    n = len(pairs)
    # MO layout: header(28) | orig_table(n×8) | trans_table(n×8) | strings
    orig_offset   = 28          # orig descriptor table starts right after 7-field header
    trans_offset  = 28 + n * 8  # trans descriptor table follows orig table
    strings_start = 28 + n * 16 # string data follows both tables

    # Build string data and record (length, file-offset) descriptors.
    orig_descriptors:  list[tuple[int, int]] = []
    trans_descriptors: list[tuple[int, int]] = []
    string_data = bytearray()

    pos = strings_start
    for enc in orig_enc:
        orig_descriptors.append((len(enc), pos))
        string_data += enc + b"\x00"
        pos += len(enc) + 1

    for enc in trans_enc:
        trans_descriptors.append((len(enc), pos))
        string_data += enc + b"\x00"
        pos += len(enc) + 1

    buf = bytearray()
    # Header
    buf += struct.pack("<IIIIIII",
                       0x950412DE,  # magic
                       0,           # revision
                       n,
                       orig_offset,
                       trans_offset,
                       0,           # hash table size (0 = unused)
                       0)           # hash table offset (0 = unused)

    # Original string descriptors
    for length, offset in orig_descriptors:
        buf += struct.pack("<II", length, offset)

    # Translation string descriptors
    for length, offset in trans_descriptors:
        buf += struct.pack("<II", length, offset)

    buf += string_data

    out.write_bytes(bytes(buf))
    print(f"  wrote {out}  ({n} entries, {len(buf)} bytes)")


def main():
    root = pathlib.Path(__file__).parent.parent
    locale_dir = root / "locale"

    compiled = 0
    for po_path in sorted(locale_dir.glob("*/LC_MESSAGES/*.po")):
        mo_path = po_path.with_suffix(".mo")
        print(f"Compiling {po_path.relative_to(root)} …")
        pairs = parse_po(po_path)
        compile_mo(pairs, mo_path)
        compiled += 1

    if compiled == 0:
        print("No .po files found under locale/.")
        sys.exit(1)
    print(f"Done - compiled {compiled} file(s).")


if __name__ == "__main__":
    main()
