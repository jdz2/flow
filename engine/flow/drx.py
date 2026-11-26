"""Read and write DaVinci Resolve grade files (.drx) so Flow can put real power windows on nodes.

A .drx is XML; the grade itself is the hex <Body> of <pClipFullVer>: one flag byte (0x81) followed by a
zstd frame holding a protobuf message (no public schema). Resolve's API can apply a .drx
(Graph.ApplyGradeFromDRX) but cannot add nodes or windows, so Flow edits the protobuf directly:

    grade (field 1)
      node (field 7, repeated)      1 id · 2 index · 4/5 position in the node editor · 6 label
        correction (field 9)
          tool (field 1)            1 type (4 = window) · 6 › 2 › params (field 3, repeated)
            param                   1 id · 2 value (1 float | 2 int | 10 matrix of 9 floats)
      link (field 8, repeated)      1 from · 3 to (serial chain)

Field meanings were worked out by applying edited copies to a test clip and measuring the result
(tests/drx_lab.py). Only the parts Flow edits are interpreted; everything else round-trips untouched.
"""
from __future__ import annotations

import binascii
import re
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Any, List, Optional, Tuple, Union

Field = List[Any]          # [field_no, wire_type, value]
Msg = List[Field]

# ---------------------------------------------------------------- protobuf wire format


def _varint(buf: bytes, i: int) -> Tuple[int, int]:
    shift = val = 0
    while True:
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, i
        shift += 7


def _enc_varint(v: int) -> bytes:
    out = bytearray()
    v &= (1 << 64) - 1
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def decode(buf: bytes) -> Msg:
    """Flat decode: length-delimited values stay bytes (decode them again to go deeper)."""
    out: Msg = []
    i = 0
    while i < len(buf):
        key, i = _varint(buf, i)
        fn, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(buf, i)
        elif wt == 1:
            v, i = buf[i:i + 8], i + 8
        elif wt == 2:
            n, i = _varint(buf, i)
            v, i = buf[i:i + n], i + n
        elif wt == 5:
            v, i = buf[i:i + 4], i + 4
        else:
            raise ValueError(f"unsupported wire type {wt} at {i}")
        if i > len(buf) or fn == 0:
            raise ValueError("truncated")
        out.append([fn, wt, v])
    return out


def encode(msg: Msg) -> bytes:
    out = bytearray()
    for fn, wt, v in msg:
        out += _enc_varint((fn << 3) | wt)
        if wt == 0:
            out += _enc_varint(int(v))
        elif wt == 2:
            b = encode(v) if isinstance(v, list) else bytes(v)
            out += _enc_varint(len(b)) + b
        else:
            out += bytes(v)
    return bytes(out)


def is_message(b: bytes) -> bool:
    """Best guess whether a length-delimited value is itself a message."""
    if not b:
        return False
    try:
        return encode(decode(b)) == b
    except Exception:
        return False


def get(msg: Msg, fn: int) -> List[Field]:
    return [f for f in msg if f[0] == fn]


def first(msg: Msg, fn: int) -> Optional[Field]:
    for f in msg:
        if f[0] == fn:
            return f
    return None


def f32(b: bytes) -> float:
    return struct.unpack("<f", b)[0]


def p32(v: float) -> bytes:
    return struct.pack("<f", float(v))


def dump(buf: Union[bytes, Msg], depth: int = 0, max_depth: int = 8) -> str:
    """Readable tree for reverse engineering."""
    msg = decode(buf) if isinstance(buf, (bytes, bytearray)) else buf
    lines = []
    pad = "  " * depth
    for fn, wt, v in msg:
        if wt == 0:
            lines.append(f"{pad}{fn}: {v}")
        elif wt == 5:
            lines.append(f"{pad}{fn}: f32 {f32(v):g}")
        elif wt == 1:
            lines.append(f"{pad}{fn}: f64 {struct.unpack('<d', v)[0]:g}")
        elif depth < max_depth and is_message(v):
            lines.append(f"{pad}{fn} {{")
            lines.append(dump(v, depth + 1, max_depth))
            lines.append(f"{pad}}}")
        else:
            try:
                s = v.decode("utf-8")
                ok = s.isprintable()
            except Exception:
                ok = False
            lines.append(f"{pad}{fn}: " + (repr(s) if ok else f"bytes {v.hex()}"))
    return "\n".join(lines)


# ---------------------------------------------------------------- .drx container

_BODY = re.compile(r"(<pClipFullVer>.*?<Body>)([0-9a-fA-F]+)(</Body>)", re.S)


def _zstd(data: bytes, decompress: bool) -> bytes:
    """Resolve only accepts single-segment frames that carry the content size and no checksum, so
    compress from a file (stdin would leave the size out) with --no-check."""
    import tempfile
    exe = shutil.which("zstd") or "/opt/homebrew/bin/zstd"
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in"
        src.write_bytes(data)
        args = [exe, "-q", "-c"] + (["-d"] if decompress else ["-19", "--no-check"]) + [str(src)]
        p = subprocess.run(args, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"zstd failed: {p.stderr.decode(errors='replace')[:200]}")
    if not decompress and p.stdout[4:5] and not p.stdout[4] & 0x20:
        raise RuntimeError("zstd frame is not single-segment; Resolve would reject it")
    return p.stdout


def zstd_available() -> bool:
    return bool(shutil.which("zstd") or Path("/opt/homebrew/bin/zstd").exists())


def read_grade(path: Union[str, Path]) -> Tuple[str, bytes]:
    """-> (xml text, decompressed grade protobuf)."""
    xml = Path(path).read_text(encoding="utf-8")
    m = _BODY.search(xml)
    if not m:
        raise ValueError(f"no clip grade body in {path}")
    raw = binascii.unhexlify(m.group(2))
    if raw[:1] != b"\x81":
        raise ValueError(f"unknown grade body flag {raw[:1].hex()}")
    return xml, _zstd(raw[1:], True)


def write_grade(xml: str, grade: bytes, out: Union[str, Path]) -> Path:
    body = (b"\x81" + _zstd(grade, False)).hex()
    new = _BODY.sub(lambda m: m.group(1) + body + m.group(3), xml, count=1)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(new, encoding="utf-8")
    return out


# ---------------------------------------------------------------- nodes and windows

def grade_root(grade: bytes) -> Msg:
    return decode(first(decode(grade), 1)[2])


def nodes(root: Msg) -> List[Msg]:
    return [decode(f[2]) for f in get(root, 7)]


def node_label(node: Msg) -> str:
    f = first(node, 6)
    return f[2].decode("utf-8", "replace") if f else ""


def window_params(node: Msg) -> Optional[List[Msg]]:
    """The param list of the first window on a node (None if it has no window)."""
    corr = first(node, 9)
    if not corr:
        return None
    tool = first(decode(corr[2]), 1)
    if not tool:
        return None
    t = decode(tool[2])
    six = first(t, 6)
    if not six:
        return None
    two = first(decode(six[2]), 2)
    if not two:
        return None
    return [decode(p[2]) for p in get(decode(two[2]), 3)]


def param_value(p: Msg) -> Any:
    pid = first(p, 1)[2]
    val = decode(first(p, 2)[2])
    f = val[0]
    if f[0] == 1 and f[1] == 5:
        return pid, f32(f[2])
    if f[0] == 2 and f[1] == 0:
        return pid, f[2]
    if f[0] == 10:
        m = decode(f[2])
        return pid, [f32(x[2]) for x in m]
    return pid, val


def edit_window(root: Msg, node_label_or_index: Union[str, int], fn) -> Msg:
    """Rewrite the window params of one node: fn(params: list of (pid, value)) -> new list.
    Values keep their kind (float / int / matrix list)."""
    out: Msg = []
    idx = 0
    for f in root:
        if f[0] != 7:
            out.append(f)
            continue
        idx += 1
        node = decode(f[2])
        hit = node_label(node) == node_label_or_index if isinstance(node_label_or_index, str) \
            else idx == node_label_or_index
        if hit:
            node = _rewrite_node_window(node, fn)
        out.append([7, 2, encode(node)])
    return out


def _encode_value(old: Msg, value: Any) -> Msg:
    f = old[0]
    if f[0] == 1 and f[1] == 5:
        return [[1, 5, p32(value)]]
    if f[0] == 2 and f[1] == 0:
        return [[2, 0, int(value)]]
    if f[0] == 10:
        m = decode(f[2])
        return [[10, 2, encode([[x[0], 5, p32(v)] for x, v in zip(m, value)])]]
    return old


def _rewrite_node_window(node: Msg, fn) -> Msg:
    corr_f = first(node, 9)
    corr = decode(corr_f[2])
    tool_f = first(corr, 1)
    tool = decode(tool_f[2])
    six_f = first(tool, 6)
    six = decode(six_f[2])
    two_f = first(six, 2)
    two = decode(two_f[2])
    params = [decode(p[2]) for p in get(two, 3)]
    cur = [param_value(p) for p in params]
    new = dict(fn(list(cur)))
    rebuilt: Msg = []
    for f in two:
        if f[0] != 3:
            rebuilt.append(f)
            continue
        p = decode(f[2])
        pid = first(p, 1)[2]
        if pid in new:
            val_old = decode(first(p, 2)[2])
            p = [[1, 0, pid], [2, 2, encode(_encode_value(val_old, new[pid]))]] + \
                [x for x in p if x[0] not in (1, 2)]
        rebuilt.append([3, 2, encode(p)])
    two_f[2] = encode(rebuilt)
    six_f[2] = encode(six)
    tool_f[2] = encode(tool)
    corr_f[2] = encode(corr)
    return node


def rebuild_grade(grade: bytes, root: Msg) -> bytes:
    top = decode(grade)
    for f in top:
        if f[0] == 1:
            f[2] = encode(root)
            break
    return encode(top)
