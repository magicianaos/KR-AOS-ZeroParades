#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ZERO PARADES 한글패치 - Steam Deck / SteamOS(리눅스) 설치 도구

Windows 한글패치 런처(ZeroParades_KR_Patch_v1.0.exe)의 설치 엔진(zp_patch · zp_build · bundlelib ·
ufs · loctable · crcforge)을 파이썬 표준 라이브러리와 시스템 liblz4 로 옮긴 것입니다.
런처와 같은 순서 · 같은 방법으로 내 PC 의 Steam 정품 원본에서 한글 파일을 만들고, 결과의 SHA-256 이
manifest.json 의 patched_sha256 과 같을 때만 바꿉니다(= Windows 런처 결과와 바이트 단위로 같음).

- 패치 자료(manifest · 한국어 표 · 글꼴)는 공식 릴리스 exe 안에서 꺼내 씁니다.
  exe 는 실행하지 않습니다. SHA-256 을 확인한 뒤 PyInstaller 묶음을 읽기만 합니다.
- 백업 위치(게임 폴더/KR-AOS_한글패치_백업/ZeroParades/)와 receipt.json 형식은 Windows 런처와 같습니다.

사용법:
  python3 zp_kr_linux.py status     현재 상태 보기
  python3 zp_kr_linux.py check      게임 파일은 그대로 두고 만들기 · 결과 해시만 미리 확인
  python3 zp_kr_linux.py install    한글패치 설치
  python3 zp_kr_linux.py restore    원래대로 되돌리기
옵션:
  --game <폴더>        게임 폴더 직접 지정 (기본: Steam 라이브러리에서 자동으로 찾음)
  --steam-root <폴더>  Steam 폴더 직접 지정 (예: Flatpak Steam)
  --exe <파일>         미리 받아 둔 공식 런처 exe (기본: GitHub 릴리스에서 받아 ~/.cache 에 보관)
"""
import argparse
import ctypes
import gzip
import hashlib
import json
import lzma
import os
import re
import shutil
import struct
import sys
import tempfile
import urllib.request
import uuid
import zlib
from datetime import datetime
from pathlib import Path

PATCH_VERSION = "v1.0"
RELEASE_EXE = {
    "url": "https://github.com/magicianaos/KR-AOS-ZeroParades/releases/download/v1.0/ZeroParades_KR_Patch_v1.0.exe",
    "name": "ZeroParades_KR_Patch_v1.0.exe",
    "size": 18566610,
    "sha256": "d5a823ba8cd2c67d0c1964f21dfc5490b832a4a4f7392690ada6d2182a27b28c",
}
PAYLOAD_NAMES = ("manifest.json", "ko_values.json.gz", "NotoSansKR-sub.otf", "LICENSE-NotoSansCJK-OFL.txt")


class PatchError(Exception):
    pass


class BuildError(Exception):
    pass


def log(msg):
    print(msg, flush=True)


def file_sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(4194304), b""):
            h.update(block)
    return h.hexdigest()


# ============================================================================ 공식 런처 exe 에서 패치 자료 꺼내기
PYI_COOKIE = b"MEI\x0c\x0b\x0a\x0b\x0e"


def obtain_exe(exe_arg):
    if exe_arg:
        path = Path(exe_arg).expanduser()
    else:
        cache = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "kr-aos-zeroparades"
        path = cache / RELEASE_EXE["name"]
        if not (path.is_file() and file_sha256(path) == RELEASE_EXE["sha256"]):
            cache.mkdir(parents=True, exist_ok=True)
            log("공식 런처 받는 중: " + RELEASE_EXE["url"])
            part = path.with_name(path.name + ".part")
            with urllib.request.urlopen(RELEASE_EXE["url"], timeout=60) as response, part.open("wb") as out:
                shutil.copyfileobj(response, out)
            os.replace(part, path)
    if not path.is_file():
        raise PatchError("런처 exe 를 찾지 못했습니다: %s" % path)
    data = path.read_bytes()
    if len(data) != RELEASE_EXE["size"] or hashlib.sha256(data).hexdigest() != RELEASE_EXE["sha256"]:
        raise PatchError("런처 exe 가 공식 %s 릴리스와 다릅니다(SHA-256 불일치): %s" % (PATCH_VERSION, path))
    return data


def read_payload(exe):
    """PyInstaller CArchive 목차를 읽어 payload/* 를 꺼낸다. 실행하지 않고 바이트만 읽는다."""
    cpos = exe.rfind(PYI_COOKIE)
    if cpos < 0:
        raise PatchError("런처 exe 형식이 예상과 다릅니다.")
    _, pkg_len, toc_off, toc_len, _, _ = struct.unpack_from("!8sIIii64s", exe, cpos)
    overlay = cpos + 88 - pkg_len
    p = overlay + toc_off
    end = p + toc_len
    entries = {}
    while p < end:
        esize, epos, csize, usize, cflag, _ = struct.unpack_from("!IIIIBc", exe, p)
        name = exe[p + 18:p + esize].split(b"\x00", 1)[0].decode("utf-8").replace("\\", "/")
        entries[name] = (overlay + epos, csize, usize, cflag)
        p += esize
    out = {}
    for name in PAYLOAD_NAMES:
        if "payload/" + name not in entries:
            raise PatchError("런처 exe 안에 설치 자료가 없습니다: " + name)
        pos, csize, usize, cflag = entries["payload/" + name]
        raw = exe[pos:pos + csize]
        if cflag == 1:
            raw = zlib.decompress(raw)
        if len(raw) != usize:
            raise PatchError("런처 exe 안의 설치 자료가 손상되었습니다: " + name)
        out[name] = raw
    return out


# ============================================================================ LZ4 (시스템 liblz4)
_LZ4 = None


def _lz4():
    global _LZ4
    if _LZ4 is None:
        lib = None
        for name in ("liblz4.so.1", "liblz4.so"):
            try:
                lib = ctypes.CDLL(name)
                break
            except OSError:
                continue
        if lib is None:
            raise PatchError("liblz4 를 찾지 못했습니다. lz4 라이브러리를 설치해 주세요.")
        lib.LZ4_compressBound.argtypes = [ctypes.c_int]
        lib.LZ4_compressBound.restype = ctypes.c_int
        lib.LZ4_createStream.restype = ctypes.c_void_p
        lib.LZ4_freeStream.argtypes = [ctypes.c_void_p]
        lib.LZ4_freeStream.restype = ctypes.c_int
        lib.LZ4_compress_fast_continue.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p,
                                                   ctypes.c_int, ctypes.c_int, ctypes.c_int]
        lib.LZ4_compress_fast_continue.restype = ctypes.c_int
        lib.LZ4_decompress_safe.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
        lib.LZ4_decompress_safe.restype = ctypes.c_int
        lib.LZ4_versionString.restype = ctypes.c_char_p
        _LZ4 = lib
    return _LZ4


def lz4c(b):
    """런처의 lz4.block.compress(b, store_size=False) 와 같은 출력.
    python-lz4 4.4.5(lz4/block/_block.c)는 새 스트림에 LZ4_compress_fast_continue(가속 1)를 쓴다.
    (LZ4_compress_default 와는 64KB 미만 입력에서 결과가 다르다.)"""
    lib = _lz4()
    b = bytes(b)
    cap = lib.LZ4_compressBound(len(b))
    dst = ctypes.create_string_buffer(cap)
    stream = lib.LZ4_createStream()
    if not stream:
        raise MemoryError("LZ4_createStream")
    try:
        n = lib.LZ4_compress_fast_continue(stream, b, dst, len(b), cap, 1)
    finally:
        lib.LZ4_freeStream(stream)
    if n <= 0:
        raise BuildError("LZ4 압축에 실패했습니다.")
    return dst.raw[:n]


def lz4d(b, usize):
    lib = _lz4()
    b = bytes(b)
    dst = ctypes.create_string_buffer(usize)
    n = lib.LZ4_decompress_safe(b, dst, len(b), usize)
    if n != usize:
        raise BuildError("LZ4 풀기에 실패했습니다.")
    return dst.raw[:n]


# ============================================================================ ufs (UnityFS 읽기)
def cstr(d, p):
    e = d.index(b"\x00", p)
    return d[p:e].decode(), e + 1


def _dec(b, ct, u):
    if ct == 0:
        return bytes(b)
    if ct == 1:
        return lzma.decompress(b[:5] + struct.pack("<Q", u) + b[5:], format=lzma.FORMAT_ALONE)
    return lz4d(b, u)


def ufs_load(path):
    d = Path(path).read_bytes()
    sig, p = cstr(d, 0)
    if sig != "UnityFS":
        return None
    ver = struct.unpack(">I", d[p:p + 4])[0]
    p += 4
    _, p = cstr(d, p)
    _, p = cstr(d, p)
    size, csz, usz, flags = struct.unpack(">qIII", d[p:p + 20])
    p += 20
    if ver >= 7:
        p = (p + 15) & -16
    if flags & 128:
        bi = d[-csz:]
    else:
        bi = d[p:p + csz]
        p += csz
    if flags & 512:
        p = (p + 15) & -16
    ct = flags & 63
    bi = _dec(bi, ct, usz)
    q = 16
    nb = struct.unpack(">I", bi[q:q + 4])[0]
    q += 4
    blocks = []
    for _ in range(nb):
        u, c, f = struct.unpack(">IIH", bi[q:q + 10])
        q += 10
        blocks.append((u, c, f))
    nn = struct.unpack(">I", bi[q:q + 4])[0]
    q += 4
    nodes = []
    for _ in range(nn):
        o, s, f = struct.unpack(">qqI", bi[q:q + 20])
        q += 20
        nm, q = cstr(bi, q)
        nodes.append((o, s, f, nm))
    data = bytearray()
    for u, c, f in blocks:
        data += _dec(d[p:p + c], f & 63, u)
        p += c
    return nodes, bytes(data), ver, ct


# ============================================================================ bundlelib (UnityFS 쓰기 · SerializedFile)
def read_header(path):
    with open(path, "rb") as fh:
        d = fh.read(512)
    sig, p = cstr(d, 0)
    ver = struct.unpack(">I", d[p:p + 4])[0]
    p += 4
    v1, p = cstr(d, p)
    v2, p = cstr(d, p)
    return ver, v1, v2


def pack_bundle(ver, v1, v2, nodes, data, flags=579, blocksize=131072, compress=True):
    blocks = []
    payload = bytearray()
    for i in range(0, len(data), blocksize):
        chunk = data[i:i + blocksize]
        c = lz4c(chunk) if compress else None
        if c is not None and len(c) < len(chunk):
            blocks.append((len(chunk), len(c), 3))
            payload += c
        else:
            blocks.append((len(chunk), len(chunk), 0))
            payload += chunk
    bi = bytearray(b"\x00" * 16)
    bi += struct.pack(">I", len(blocks))
    for u, c, f in blocks:
        bi += struct.pack(">IIH", u, c, f)
    bi += struct.pack(">I", len(nodes))
    for o, s, f, nm in nodes:
        bi += struct.pack(">qqI", o, s, f) + nm.encode() + b"\x00"
    bic = lz4c(bytes(bi))
    hdr = bytearray(b"UnityFS\x00" + struct.pack(">I", ver) + v1.encode() + b"\x00" + v2.encode() + b"\x00")
    pos = len(hdr) + 20
    pad1 = (-pos) % 16 if ver >= 7 else 0
    pos += pad1 + len(bic)
    pad2 = (-pos) % 16 if flags & 512 else 0
    total = pos + pad2 + len(payload)
    out = (bytearray(hdr) + struct.pack(">qIII", total, len(bic), len(bi), flags)
           + b"\x00" * pad1 + bic + b"\x00" * pad2 + payload)
    if len(out) != total:
        raise BuildError("번들 크기 계산이 맞지 않습니다.")
    return bytes(out)


class Reader:
    def __init__(self, d, p=0, le=True):
        self.d = d
        self.p = p
        self.e = "<" if le else ">"

    def u(self, f, n):
        v = struct.unpack_from(self.e + f, self.d, self.p)
        self.p += n
        return v[0]

    def i32(self):
        return self.u("i", 4)

    def u32(self):
        return self.u("I", 4)

    def i64(self):
        return self.u("q", 8)

    def u16(self):
        return self.u("H", 2)

    def u8(self):
        return self.u("B", 1)

    def cstr(self):
        e = self.d.index(b"\x00", self.p)
        v = self.d[self.p:e].decode("utf8", "replace")
        self.p = e + 1
        return v

    def align(self, n=4):
        self.p = (self.p + n - 1) // n * n


def sf_table(data):
    h = Reader(data, 0, False)
    h.u32(); h.u32(); h.u32(); h.u32()
    h.u32(); h.u32()
    fsize_pos = h.p
    fsize = h.i64()
    doff = h.i64()
    h.i64()
    r = Reader(data, h.p, True)
    r.cstr(); r.i32()
    tt = r.u8()
    nt = r.i32()
    for _ in range(nt):
        cid = r.i32(); r.u8(); r.u16()
        if cid == 114:
            r.p += 16
        r.p += 16
        if tt:
            nn = r.i32()
            sb = r.i32()
            r.p += 32 * nn + sb
            ndeps = r.i32()
            r.p += 4 * ndeps
    no = r.i32()
    objs = []
    for _ in range(no):
        r.align(4)
        tp = r.p
        pid = r.i64(); bs = r.i64(); sz = r.u32(); ti = r.i32()
        objs.append([pid, bs, sz, ti, tp])
    return dict(fsize_pos=fsize_pos, fsize=fsize, doff=doff, objs=objs)


def sf_replace(data, repl, align=8):
    t = sf_table(data)
    doff = t["doff"]
    objs = t["objs"]
    order = sorted(range(len(objs)), key=lambda i: objs[i][1])
    body = bytearray()
    newpos = {}
    for i in order:
        pid, bs, sz, ti, tp = objs[i]
        b = repl.get(pid)
        if b is None:
            b = data[doff + bs:doff + bs + sz]
        while len(body) % align:
            body.append(0)
        newpos[i] = (len(body), len(b))
        body += b
    out = bytearray(data[:doff])
    for i, (o, s) in newpos.items():
        struct.pack_into("<qI", out, objs[i][4] + 8, o, s)
    out += body
    struct.pack_into(">q", out, t["fsize_pos"], len(out))
    return bytes(out)


# ============================================================================ loctable (언어 표)
def rd_str(b, p):
    n = struct.unpack_from("<i", b, p)[0]
    s = b[p + 4:p + 4 + n].decode("utf8")
    p += 4 + n
    p = (p + 3) & -4
    return s, p


def wr_str(s):
    e = s.encode("utf8")
    out = struct.pack("<i", len(e)) + e
    while len(out) % 4:
        out += b"\x00"
    return out


def parse_table(b):
    p = 12
    p += 1
    p = (p + 3) & -4
    p += 12
    name, p = rd_str(b, p)
    lang = struct.unpack_from("<i", b, p)[0]
    p += 4
    nk = struct.unpack_from("<i", b, p)[0]
    p += 4
    kpos = p
    keys = []
    for _ in range(nk):
        s, p = rd_str(b, p)
        keys.append(s)
    kend = p
    nv = struct.unpack_from("<i", b, p)[0]
    p += 4
    vpos = p
    vals = []
    for _ in range(nv):
        s, p = rd_str(b, p)
        vals.append(s)
    return dict(name=name, lang=lang, keys=keys, vals=vals, kpos=kpos, kend=kend, vpos=vpos,
                vend=p, tail=b[p:], head=b[:vpos - 4])


def build_table(b, newvals, extra_keys=()):
    t = parse_table(b)
    if len(newvals) != len(t["keys"]) + len(extra_keys):
        raise BuildError("대사 표 값 개수가 맞지 않습니다.")
    if extra_keys:
        if set(extra_keys) & set(t["keys"]) or len(set(extra_keys)) != len(extra_keys):
            raise BuildError("추가할 대사 키가 올바르지 않습니다.")
        out = bytearray(b[:t["kpos"] - 4])
        out += struct.pack("<i", len(t["keys"]) + len(extra_keys))
        out += b[t["kpos"]:t["kend"]]
        for s in extra_keys:
            out += wr_str(s)
    else:
        out = bytearray(b[:t["vpos"] - 4])
    out += struct.pack("<i", len(newvals))
    for s in newvals:
        out += wr_str(s)
    out += t["tail"]
    return bytes(out)


# ============================================================================ crcforge (끝 4바이트로 CRC32 맞추기)
_CRC_T = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ 0xEDB88320 if _c & 1 else _c >> 1
    _CRC_T.append(_c)
_CRC_R = {t >> 24: i for i, t in enumerate(_CRC_T)}


def forge4(data, target):
    s0 = ~zlib.crc32(data) & 0xFFFFFFFF
    reg = ~target & 0xFFFFFFFF
    idxs = []
    for _ in range(4):
        i = _CRC_R[reg >> 24]
        idxs.append(i)
        reg = ((reg ^ _CRC_T[i]) << 8) & 0xFFFFFFFF
    reg = s0
    out = bytearray()
    for j in range(4):
        i = idxs[3 - j]
        out.append((reg ^ i) & 0xFF)
        reg = (reg >> 8) ^ _CRC_T[i]
    if zlib.crc32(data + bytes(out)) != target:
        raise BuildError("CRC 맞추기 계산이 맞지 않습니다.")
    return bytes(out)


# ============================================================================ zp_build (번들 4개 만들기)
LOC_PATTERN = re.compile(r"^g5ibkj7vdwf2g67g_assets_all_[0-9a-f]{32}\.bundle$")
FONT = "997bd53a567ef5d457721758bc6bcd16.bundle"
LOGO = "b4eaac691daffe7383c27cded60a02bc.bundle"
LOGO_SIMPLE = "ff109a53fc7ba40b5238fc7c63d0c4b7.bundle"
ZH_PID = 3966629885548636553
FONT_PID = 7514427770069646306
LOGO_PID = 6565727547191193006
LOGO_SIMPLE_PID = 1387651915370697747
COPYRIGHT_PID = -7474377447927364237
COPYRIGHT_OLD = ("© 2025–2026 Zaum Studio Limited. All rights reserved. \n"
                 "ZERO PARADES™ is a trademark of Zaum Studio Limited. \n"
                 "ZA/UM™ is a trademark of Zaum Studio OÜ. \n"
                 "All other trademarks are the property of their respective owners.\r\n").encode()
COPYRIGHT_NEW = ("© 2025–2026 Zaum Studio Limited. 모든 권리 보유. \n"
                 "ZERO PARADES™는 Zaum Studio Limited의 상표입니다. \n"
                 "ZA/UM™은 Zaum Studio OÜ의 상표입니다. \n"
                 "그 밖의 모든 상표는 각 소유자의 자산입니다.\r\n").encode()


def load_ko_payload(raw):
    data = json.loads(gzip.decompress(raw).decode("utf-8"))
    if not isinstance(data.get("values"), dict) or not isinstance(data.get("extra"), list):
        raise BuildError("한국어 표 자료 형식이 다릅니다.")
    return data


def _nodes_after(sf2_len, nodes):
    nn = [(0, sf2_len, nodes[0][2], nodes[0][3])]
    off = sf2_len
    for k in range(1, len(nodes)):
        nn.append((off, nodes[k][1], nodes[k][2], nodes[k][3]))
        off += nodes[k][1]
    return nn


def _object(sfd, t, pid_want):
    pid, bs, sz, ti, tp = [o for o in t["objs"] if o[0] == pid_want][0]
    o = t["doff"] + bs
    return pid, sfd[o:o + sz]


def localization_tables(data):
    t = sf_table(data)
    out = {}
    for pid, bs, sz, ti, tp in t["objs"]:
        ob = data[t["doff"] + bs:t["doff"] + bs + sz]
        try:
            name, _ = rd_str(ob, 28)
        except (UnicodeDecodeError, ValueError, struct.error):
            continue
        if name.endswith("LocalizationTable"):
            out[name] = (pid, ob)
    return out


def _replace_native_name(data):
    """'简体中文'(12B) → '한국어'(9B + 0 패딩 3B), 길이 필드 12 → 9"""
    old = "简体中文".encode()
    new = "한국어".encode()
    data = bytearray(data)
    for m in list(re.finditer(re.escape(old), bytes(data))):
        p = m.start()
        if struct.unpack_from("<i", data, p - 4)[0] != 12:
            raise BuildError("언어 이름 문자열 구조가 예상과 다릅니다.")
        struct.pack_into("<i", data, p - 4, 9)
        data[p:p + 12] = new + b"\x00\x00\x00"
    return bytes(data)


def fallback_values(data):
    """한국어 값이 없는 키는 원본 독일어 표 값(없으면 원래 값)으로 채운다."""
    tabs = localization_tables(data)
    if "deLocalizationTable" not in tabs:
        raise BuildError("독일어 표를 찾지 못했습니다.")
    de_tab = parse_table(tabs["deLocalizationTable"][1])
    de = dict(zip(de_tab["keys"], de_tab["vals"]))
    _, ob = _object(data, sf_table(data), ZH_PID)
    pt = parse_table(ob)
    return pt, ob, [de.get(k, v) for k, v in zip(pt["keys"], pt["vals"])]


def build_localization(src, out, ko, target_crc):
    nodes, data, _, _ = ufs_load(src)
    hv, v1, v2 = read_header(src)
    if zlib.crc32(data) != target_crc:
        raise BuildError("대사 원본 CRC 가 카탈로그와 다릅니다.")
    data = _replace_native_name(data)
    pt, ob, fb = fallback_values(data)
    values = ko["values"]
    vals = [values[k] if k in values else f for k, f in zip(pt["keys"], fb)]
    extra = [k for k, _ in ko["extra"]]
    if set(extra) & set(pt["keys"]):
        raise BuildError("추가할 새 대사 키가 이미 원본 표에 있습니다(게임 버전이 다른가요?).")
    vals += [v for _, v in ko["extra"]]
    newobj = build_table(ob, vals, extra_keys=extra)
    chk = parse_table(newobj)
    if chk["keys"] != pt["keys"] + extra or chk["vals"] != vals or chk["tail"] != pt["tail"] or len(newobj) % 4:
        raise BuildError("새 대사 표 직렬화 확인에 실패했습니다.")
    d2 = bytearray(sf_replace(data, {ZH_PID: newobj}, align=16) + b"\x00\x00\x00\x00")
    struct.pack_into(">q", d2, sf_table(bytes(d2))["fsize_pos"], len(d2))
    d2 = bytes(d2)
    d2 = d2[:-4] + forge4(d2[:-4], target_crc)
    nn = [(0, len(d2), nodes[0][2], nodes[0][3])]
    Path(out).write_bytes(pack_bundle(hv, v1, v2, nn, d2))
    r = ufs_load(out)
    if r[1] != d2 or zlib.crc32(r[1]) != target_crc:
        raise BuildError("대사 번들 다시 읽기 확인에 실패했습니다.")
    return {"keys": len(chk["keys"]), "ko": sum(1 for k in pt["keys"] if k in values) + len(extra),
            "added": len(extra)}


def _rebuild(src, out, pid_want, edit, what):
    nodes, data, _, _ = ufs_load(src)
    hv, v1, v2 = read_header(src)
    sfd = data[:nodes[0][1]]
    rest = data[nodes[0][1]:]
    pid, obj = _object(sfd, sf_table(sfd), pid_want)
    sf2 = sf_replace(sfd, {pid: edit(obj)}, align=16)
    nn = _nodes_after(len(sf2), nodes)
    Path(out).write_bytes(pack_bundle(hv, v1, v2, nn, sf2 + rest))
    r = ufs_load(out)
    if r[0] != nn or r[1] != sf2 + rest:
        raise BuildError(what + " 번들 다시 읽기 확인에 실패했습니다.")


def build_font(src, out, font):
    def edit(obj):
        i = obj.find(b"OTTO")
        if i < 4:
            raise BuildError("원본 글꼴 자리를 찾지 못했습니다.")
        length = struct.unpack_from("<I", obj, i - 4)[0]
        tail = obj[i + length:].replace(b"IBM Plex Sans SC", b"Noto Sans CJK KR")
        return obj[:i - 4] + struct.pack("<I", len(font)) + font + b"\x00" * ((-len(font)) % 4) + tail
    _rebuild(src, out, FONT_PID, edit, "글꼴")


def _logo(src, out, pid_want, oldg, newg, old, new):
    def edit(obj):
        if obj.count(oldg) != 1 or obj.count(old) != 1:
            raise BuildError("로고 원본 구조가 예상과 다릅니다.")
        return obj.replace(oldg, newg).replace(old, new)
    _rebuild(src, out, pid_want, edit, "로고")


def build_logo(src, out):
    _logo(src, out, LOGO_PID, b"64504324d0f0e7f489d6a1743d4b2dc2", b"4138faea5d4c8b74ca53a5c07fcbe8d1",
          struct.pack("<i", 18) + b"ZP Logo@2x_chinese\x00\x00",
          struct.pack("<i", 10) + b"ZP Logo@2x\x00\x00")


def build_logo_simple(src, out):
    _logo(src, out, LOGO_SIMPLE_PID, b"9962da01a1f7c2a40ba88bf4ef6fa37e", b"64f2d05b99b88fb428f97568a5795ace",
          struct.pack("<i", 25) + b"ZP Logo Simple@2x_chinese" + b"\x00\x00\x00",
          struct.pack("<i", 17) + b"ZP Logo Simple@2x" + b"\x00\x00\x00")


def build_copyright(path):
    """로고 번들(build_logo 를 거친 것)의 첫 화면 저작권 문구를 한국어로. 제자리 덮어쓰기."""
    def full(s):
        return struct.pack("<I", len(s)) + s + b"\x00" * ((-len(s)) % 4)

    def edit(obj):
        if len(COPYRIGHT_OLD) != 226 or obj.count(full(COPYRIGHT_OLD)) != 1:
            raise BuildError("첫 화면 저작권 문구가 예상과 다릅니다.")
        return obj.replace(full(COPYRIGHT_OLD), full(COPYRIGHT_NEW))
    _rebuild(path, path, COPYRIGHT_PID, edit, "저작권 문구")


def force_crc(path, target_crc):
    nodes, payload, _, _ = ufs_load(path)
    if zlib.crc32(payload) != target_crc:
        payload += forge4(payload, target_crc)
        Path(path).write_bytes(pack_bundle(*read_header(path), nodes, payload))
    if zlib.crc32(ufs_load(path)[1]) != target_crc:
        raise BuildError("카탈로그 CRC 맞추기에 실패했습니다: " + Path(path).name)


def build_all(src_dir, loc_name, out_dir, ko, font, crcs):
    src_dir = Path(src_dir)
    out_dir = Path(out_dir)
    if not LOC_PATTERN.match(loc_name):
        raise BuildError("대사 번들 이름이 예상과 다릅니다.")
    log("[진행 30%] 글꼴 번들 만드는 중")
    build_font(src_dir / FONT, out_dir / FONT, font)
    log("[진행 38%] 로고 번들 만드는 중")
    build_logo(src_dir / LOGO, out_dir / LOGO)
    build_logo_simple(src_dir / LOGO_SIMPLE, out_dir / LOGO_SIMPLE)
    build_copyright(out_dir / LOGO)
    log("[진행 48%] 대사 번들 만드는 중")
    stats = build_localization(src_dir / loc_name, out_dir / loc_name, ko, crcs[loc_name])
    log("[진행 70%] 카탈로그 CRC 맞추는 중")
    for name in (FONT, LOGO, LOGO_SIMPLE):
        force_crc(out_dir / name, crcs[name])
    return stats


# ============================================================================ Steam 확인
APP_ID = "2863680"
EXE_NAME = "ZeroParades.exe"
DATA_DIR_NAME = "ZeroParades_Data"
BACKUP_DIR_NAME = "KR-AOS_한글패치_백업"
BACKUP_SUB = "ZeroParades"
STEAM_BUSY_FLAGS = 1026
STAGE_PREFIX = ".aos-zp-"
NEED_FREE_BYTES = 400000000
PINNED_MODULES = [
    ("ZeroParades_Data/Plugins/x86_64/steam_api64.dll",
     "670d654aa3255c5061cf0236a1cdbb2f5076cbd5ae44f613b6f4921558e83e2d"),
    ("ZeroParades.exe", "6c12efda4a8a5c517e95e2e945b177d9c6b5308ba6cc76cf1f4d84e45ac93416"),
    ("GameAssembly.dll", "9c30d3e94f581311e3efd39120aef5a549b3c8156f469a508d085bde194edffe"),
]
MODULE_DIRS = [".", "ZeroParades_Data/Plugins/x86_64"]
CRACK_MARKS = {
    "3dmgame.dll", "3dmgame.ini", "account_name.txt", "ali213.ini", "codex.ini", "coldclientloader.ini",
    "cpy.ini", "cream_api.ini", "creamapi.ini", "dinput8.dll", "emu.ini", "force_account_name.txt",
    "force_steamid.txt", "gbe_fork", "goldberg", "goldberg_steamemu", "local_save.txt", "lumaemu.ini",
    "onlinefix.ini", "onlinefix.url", "onlinefix64.dll", "rld.ini", "rune.ini", "skidrow.ini",
    "smartsteamemu", "smartsteamemu.ini", "smartsteamloader.exe", "steam_api.cdx", "steam_api64.cdx",
    "steam_api64_o.dll", "steam_api_o.dll", "steam_appid.txt", "steam_emu", "steam_emu.ini",
    "steam_interfaces.txt", "steam_settings", "steamclient_loader.exe", "steamclient_loader_x32.exe",
    "steamclient_loader_x64.exe", "steamemu.ini", "steamless", "tenoke.ini", "unsteam.ini", "unsteam64.dll",
    "user_steam_id.txt", "version.dll", "winmm.dll",
}


def steam_roots(arg):
    if arg:
        return [Path(arg).expanduser().resolve()]
    home = Path.home()
    out = []
    for cand in (home / ".local/share/Steam", home / ".steam/steam", home / ".steam/root",
                 home / ".var/app/com.valvesoftware.Steam/.local/share/Steam"):
        try:
            root = cand.resolve()
        except OSError:
            continue
        if (root / "steamapps").is_dir() and root not in out:
            out.append(root)
    return out


def _vdf_field(text, name):
    m = re.search('"' + re.escape(name) + r'"\s*"([^"]*)"', text)
    return m.group(1) if m else ""


def steam_manifests(roots):
    seen = set()
    found = []
    for root in roots:
        libraries = [root]
        try:
            text = (root / "steamapps/libraryfolders.vdf").read_text(encoding="utf-8", errors="replace")
            libraries += [Path(p.replace("\\\\", "\\")) for p in re.findall(r'"path"\s*"([^"]+)"', text)]
        except OSError:
            pass
        for library in libraries:
            key = str(library.resolve())
            if key in seen:
                continue
            seen.add(key)
            try:
                text = (library / ("steamapps/appmanifest_%s.acf" % APP_ID)).read_text(encoding="utf-8",
                                                                                     errors="replace")
            except OSError:
                continue
            folder = _vdf_field(text, "installdir")
            if not folder or Path(folder).name != folder or folder in (".", ".."):
                continue
            game = library / "steamapps/common" / folder
            found.append({"app_id": _vdf_field(text, "appid"), "build_id": _vdf_field(text, "buildid"),
                          "owner": _vdf_field(text, "LastOwner"), "flags": _vdf_field(text, "StateFlags"),
                          "path": game, "exists": game.is_dir(), "steam_root": root})
    return found


def steam_check(game, records):
    game = Path(game).resolve()
    record = next((m for m in records if m["app_id"] == APP_ID and Path(m["path"]).resolve() == game
                   and m["exists"]), None)
    if record is None:
        raise PatchError("Zero Parades의 Steam 설치 기록을 찾지 못했습니다.")
    try:
        flags = int(record["flags"] or 0)
    except ValueError:
        flags = 0
    if flags & STEAM_BUSY_FLAGS:
        raise PatchError("Steam에서 Zero Parades 업데이트가 진행 중이거나 대기 중입니다.")
    owner = record["owner"]
    if not (flags & 4) or not owner.isdigit() or int(owner) <= 76561197960265728:
        raise PatchError("Steam 설치 완료·소유 계정 정보를 확인할 수 없습니다.")
    try:
        users = (Path(record["steam_root"]) / "config/loginusers.vdf").read_text(encoding="utf-8", errors="replace")
    except OSError:
        users = ""
    if not re.search('"' + re.escape(owner) + r'"\s*\{', users):
        raise PatchError("설치 소유 계정의 Steam 로그인 기록을 확인할 수 없습니다.")
    return {"build_id": record["build_id"]}


def game_running():
    """Proton 으로 실행 중인 ZeroParades.exe 프로세스가 있는지 /proc 에서 찾는다."""
    for d in Path("/proc").iterdir():
        if not d.name.isdigit() or d.name == str(os.getpid()):
            continue
        try:
            args = (d / "cmdline").read_bytes().split(b"\x00")
        except OSError:
            continue
        for a in args:
            if a.replace(b"\\", b"/").rsplit(b"/", 1)[-1].lower() == EXE_NAME.lower().encode():
                return True
    return False


def genuine(game, records):
    """Windows 런처 steam_verify 중 리눅스에서 그대로 할 수 있는 확인.
    Windows 전용인 Valve 디지털 서명 확인과 steam_api64.dll 소유 확인은 하지 않는다."""
    log("정품 확인 1/3: 게임 안 Steam 모듈 파일 확인")
    for rel, sha in PINNED_MODULES:
        p = Path(game) / rel
        if not p.is_file() or file_sha256(p) != sha:
            raise PatchError("게임의 Steam 모듈이 정품과 다릅니다 (%s).\n"
                             "Steam에서 게임 파일 무결성 검사를 한 뒤 다시 시도해 주세요." % Path(rel).name)
    log("정품 확인 2/3: 크랙·에뮬레이터 흔적 확인")
    game = Path(game)
    hits = []
    for directory, folders, files in os.walk(game, followlinks=False):
        rel = Path(directory).relative_to(game)
        folders[:] = [d for d in folders if not d.startswith("KR-AOS_") and not d.startswith(".aos-")
                      and d != BACKUP_DIR_NAME]
        hits += [str(rel / n) for n in tuple(folders + files) if n.casefold() in CRACK_MARKS]
        if len(rel.parts) >= 2:
            folders[:] = []
    for module_dir in MODULE_DIRS:
        path = game / module_dir
        if path.is_dir():
            hits += [module_dir + "/" + item.name for item in path.iterdir() if item.name.casefold() in CRACK_MARKS]
    if hits:
        raise PatchError("게임 폴더에 크랙·Steam 에뮬레이터 흔적이 있습니다 (%s).\n"
                         "Steam 정품 설치에서만 설치할 수 있습니다." % hits[0])
    log("정품 확인 3/3: Steam 설치 기록·소유 계정 로그인 기록 확인")
    return steam_check(game, records)


# ============================================================================ 설치 엔진 (zp_patch)
class Backend:
    def __init__(self, payload):
        self.raw = payload
        m = json.loads(payload["manifest.json"].decode("utf-8-sig"))
        if (m.get("game"), m.get("steam_appid"), m.get("exe")) != ("Zero Parades", int(APP_ID), EXE_NAME):
            raise PatchError("설치 자료의 게임 정보가 다릅니다.")
        if m.get("patch_version") != PATCH_VERSION:
            raise PatchError("설치 자료와 도구의 패치 버전이 다릅니다.")
        files = m.get("files") or []
        if sorted(f.get("kind") for f in files) != ["bundle", "bundle", "bundle", "localization", "resources"]:
            raise PatchError("설치 대상 목록이 올바르지 않습니다.")
        for f in files:
            for key in ("original_sha256", "patched_sha256"):
                if not re.fullmatch("[0-9a-f]{64}", f.get(key, "")):
                    raise PatchError("설치 대상 확인값이 올바르지 않습니다.")
            target = Path(f["target"])
            if target.is_absolute() or ".." in target.parts or not target.parts:
                raise PatchError("설치 자료의 경로가 허용 범위를 벗어났습니다.")
        self.manifest = m
        self.files = files
        self.loc = next(f for f in files if f["kind"] == "localization")
        if not LOC_PATTERN.match(Path(self.loc["target"]).name):
            raise PatchError("대사 번들 이름이 올바르지 않습니다.")
        self.guards = m.get("guards") or []
        if len(self.guards) != 2:
            raise PatchError("카탈로그 확인값이 올바르지 않습니다.")
        self.res = m["resources"]
        self.payload = {p["path"]: p for p in m.get("payload") or []}
        if set(self.payload) != set(PAYLOAD_NAMES[1:]):
            raise PatchError("설치 자료 목록이 올바르지 않습니다.")
        self.manifest_hash = hashlib.sha256(payload["manifest.json"]).hexdigest()

    def payload_bytes(self, name):
        item = self.payload[name]
        raw = self.raw[name]
        if len(raw) != item["size"] or hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise PatchError("설치 자료가 손상되었습니다: " + name)
        return raw

    def backup_root(self, game):
        return Path(game) / BACKUP_DIR_NAME / BACKUP_SUB

    def check_backup(self, game):
        root = self.backup_root(game)
        for f in self.files:
            path = root / f["target"]
            if not path.is_file() or path.stat().st_size != f["original_size"] or \
                    file_sha256(path) != f["original_sha256"]:
                return False
        return True

    def file_states(self, game):
        out = []
        for f in self.files:
            path = Path(game) / f["target"]
            if not path.is_file():
                state, digest = "missing", ""
            else:
                digest = file_sha256(path)
                size = path.stat().st_size
                if (digest, size) == (f["original_sha256"], f["original_size"]):
                    state = "original"
                elif (digest, size) == (f["patched_sha256"], f["patched_size"]):
                    state = "patched"
                else:
                    state = "unknown"
            out.append({"file": f, "path": path, "state": state, "sha256": digest})
        return out

    def guards_bad(self, game):
        return [g["target"] for g in self.guards
                if not (Path(game) / g["target"]).is_file() or file_sha256(Path(game) / g["target"]) != g["sha256"]]

    def other_loc(self, game):
        folder = Path(game) / Path(self.loc["target"]).parent
        try:
            return [p.name for p in folder.iterdir() if LOC_PATTERN.match(p.name)]
        except OSError:
            return []

    def inspect(self, game):
        states = self.file_states(game)
        kinds = {s["state"] for s in states}
        if self.guards_bad(game) or self.other_loc(game) != [Path(self.loc["target"]).name]:
            state = "missing" if "missing" in kinds else "unknown"
        elif "missing" in kinds:
            state = "missing"
        elif kinds == {"original"}:
            state = "original"
        elif kinds == {"patched"}:
            state = "patched"
        elif kinds <= {"original", "patched"}:
            state = "mixed"
        else:
            state = "unknown"
        return state, states

    def unsupported(self, check):
        expected = str(self.manifest.get("steam_build", ""))
        build = str((check or {}).get("build_id", ""))
        head = "게임 업데이트로 지원하지 않는 버전입니다. 이 패치는 Zero Parades %s(Steam 빌드 %s) 전용입니다." % (
            self.manifest["game_version"], expected)
        if build and build != expected:
            head += " (지금 Steam 빌드 %s)" % build
        return PatchError(head + "\n아무 파일도 바꾸지 않았습니다. 패치 업데이트를 기다리거나, 게임을 고친 적이 있다면 "
                                 "Steam에서 게임 파일 무결성 검사를 한 뒤 다시 시도해 주세요.")

    def preflight(self, game, records):
        if game_running():
            raise PatchError("Zero Parades가 실행 중입니다. 게임을 종료해 주세요.")
        return steam_check(game, records)

    def _replace_from(self, source, target, expected):
        fd, temp = tempfile.mkstemp(prefix=STAGE_PREFIX, dir=Path(target).parent)
        os.close(fd)
        try:
            shutil.copyfile(source, temp)
            if file_sha256(temp) != expected:
                raise PatchError("복사 확인에 실패했습니다: " + Path(target).name)
            os.replace(temp, target)
        finally:
            Path(temp).unlink(missing_ok=True)
        if file_sha256(target) != expected:
            raise PatchError("교체 확인에 실패했습니다: " + Path(target).name)

    def _build_into(self, src_root, stage):
        ko = load_ko_payload(self.payload_bytes("ko_values.json.gz"))
        font = self.payload_bytes("NotoSansKR-sub.otf")
        crcs = {f["payload"]: f["catalog_crc"] for f in self.files if "payload" in f}
        stats = build_all(Path(src_root) / Path(self.loc["target"]).parent, Path(self.loc["target"]).name,
                          stage, ko, font, crcs)
        log("대사 표 %s키 (한국어 %s줄 반영, 새 줄 %d개 추가)" % (format(stats["keys"], ","),
                                                         format(stats["ko"], ","), stats["added"]))
        log("[진행 75%] 줄바꿈 설정 바이트 켜는 중")
        res = next(f for f in self.files if f["kind"] == "resources")
        data = bytearray((Path(src_root) / res["target"]).read_bytes())
        off = int(self.res["offset"])
        if len(data) != int(self.res["size"]) or data[off] != 0:
            raise PatchError("resources.assets 구조가 예상과 다릅니다.")
        data[off] = 1
        (Path(stage) / "resources.assets").write_bytes(bytes(data))
        staged = {}
        for f in self.files:
            p = Path(stage) / f.get("payload", "resources.assets")
            if not p.is_file() or p.stat().st_size != f["patched_size"] or file_sha256(p) != f["patched_sha256"]:
                raise PatchError("만든 파일의 결과 해시가 manifest와 다릅니다: %s\n"
                                 "(liblz4 %s) 아무 파일도 바꾸지 않았습니다."
                                 % (p.name, _lz4().LZ4_versionString().decode()))
            staged[f["target"]] = p
        log("[진행 85%] 만든 파일 5개의 결과 해시 확인 완료")
        return staged

    def status(self, game):
        state, states = self.inspect(game)
        names = {"original": "원본", "patched": "한글패치 적용됨", "unknown": "지원하지 않는 버전",
                 "missing": "파일 없음"}
        for s in states:
            log("  %-16s %s" % (names[s["state"]], Path(s["file"]["target"]).name))
        bad = self.guards_bad(game)
        log("  카탈로그: " + ("정상" if not bad else "지원 버전과 다름 " + ", ".join(bad)))
        log("  원본 백업: " + ("있음" if self.check_backup(game) else "없음"))
        text = {"original": "원본 (설치 가능)", "patched": "한글패치 적용됨", "mixed": "일부만 한글패치(되돌리기 필요)",
                "unknown": "지원하지 않는 버전(게임 업데이트?)", "missing": "파일 없음"}[state]
        log("상태: " + text)
        return state

    def check(self, game, records):
        self.preflight(game, records)
        state, _ = self.inspect(game)
        if state != "original":
            raise PatchError("미리 확인은 원본 상태에서만 할 수 있습니다(지금: %s)." % state)
        genuine(game, records)
        with tempfile.TemporaryDirectory(prefix="kr-aos-zp-check-") as stage:
            self._build_into(game, stage)
        log("미리 확인 완료: 게임 파일은 바꾸지 않았습니다.")

    def install(self, game, records):
        game = Path(game)
        check = self.preflight(game, records)
        log("[진행 5%] 게임 파일 확인 중")
        state, states = self.inspect(game)
        if state == "patched":
            log("이미 설치되어 있습니다.")
            return
        if state == "mixed":
            raise PatchError("일부 파일만 한글패치된 상태입니다. restore 를 먼저 해 주세요.")
        if state != "original":
            raise self.unsupported(check)
        genuine(game, records)
        self.payload_bytes("ko_values.json.gz")
        self.payload_bytes("NotoSansKR-sub.otf")
        if shutil.disk_usage(game).free < NEED_FREE_BYTES:
            raise PatchError("게임이 있는 드라이브의 빈 공간이 부족합니다(400MB 이상 필요).")
        root = self.backup_root(game)
        log("[진행 12%] 원본 백업 중")
        for f in self.files:
            src = game / f["target"]
            dst = root / f["target"]
            if dst.is_file() and file_sha256(dst) == f["original_sha256"]:
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            if file_sha256(dst) != f["original_sha256"]:
                raise PatchError("원본 백업 확인에 실패했습니다: " + Path(f["target"]).name)
        if not self.check_backup(game):
            raise PatchError("원본 백업 확인에 실패했습니다.")
        stage = root / (".staging-" + uuid.uuid4().hex[:8])
        stage.mkdir()
        touched = []
        try:
            try:
                staged = self._build_into(root, stage)
                self.preflight(game, records)
                for s in self.file_states(game):
                    if s["state"] != "original":
                        raise PatchError("적용 직전 원본 파일이 바뀌었습니다: " + Path(s["file"]["target"]).name)
                if self.guards_bad(game):
                    raise PatchError("적용 직전 카탈로그가 바뀌었습니다.")
                for f in self.files:
                    target = game / f["target"]
                    touched.append(f)
                    os.replace(staged[f["target"]], target)
                    if file_sha256(target) != f["patched_sha256"]:
                        raise PatchError("교체한 파일 확인에 실패했습니다: " + target.name)
                _atomic_json(root / "receipt.json", {
                    "phase": "installed",
                    "patch_version": self.manifest["patch_version"],
                    "game_version": self.manifest["game_version"],
                    "manifest_sha256": self.manifest_hash,
                    "patched_sha256": {Path(f["target"]).name: f["patched_sha256"] for f in self.files},
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                })
                log("[진행 100%] 한글패치를 설치했습니다.")
            except Exception as e:
                failed = []
                for f in reversed(touched):
                    try:
                        self._replace_from(root / f["target"], game / f["target"], f["original_sha256"])
                    except Exception:
                        failed.append(Path(f["target"]).name)
                if failed:
                    raise PatchError("설치 실패 후 원상 복구도 일부 실패했습니다(%s). 백업은 %s 에 남아 있습니다. "
                                     "Steam 무결성 검사를 해 주세요." % (", ".join(failed), root)) from e
                raise PatchError("설치 실패, 원본으로 되돌렸습니다: %s" % e) from e
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def restore(self, game, records):
        game = Path(game)
        self.preflight(game, records)
        if not self.check_backup(game):
            raise PatchError("검증된 원본 백업이 없습니다. Steam에서 게임 파일 무결성 검사를 이용해 주세요.")
        root = self.backup_root(game)
        states = self.file_states(game)
        for s in states:
            if s["state"] == "unknown":
                raise PatchError("설치 뒤 바뀐 파일은 덮어쓰지 않습니다(게임 업데이트?): %s\n"
                                 "Steam에서 게임 파일 무결성 검사를 이용해 주세요." % Path(s["file"]["target"]).name)
        log("[진행 20%] 원본으로 되돌리는 중")
        for s in states:
            if s["state"] != "original":
                self._replace_from(root / s["file"]["target"], s["path"], s["file"]["original_sha256"])
        if any(s["state"] != "original" for s in self.file_states(game)):
            raise PatchError("되돌린 결과 확인에 실패했습니다.")
        _atomic_json(root / "receipt.json", {"phase": "restored",
                                             "updated_at": datetime.now().isoformat(timespec="seconds")})
        log("[진행 100%] 원본으로 되돌렸습니다. 백업은 그대로 둡니다.")


def _atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def find_game(arg, records):
    if arg:
        game = Path(arg).expanduser().resolve()
    else:
        found = [m for m in records if m["app_id"] == APP_ID and m["exists"]]
        if not found:
            raise PatchError("Zero Parades의 Steam 설치 기록을 찾지 못했습니다. --game 으로 게임 폴더를 지정해 주세요.")
        if len(found) > 1:
            raise PatchError("Zero Parades 설치가 여러 곳에 있습니다. --game 으로 지정해 주세요:\n  "
                             + "\n  ".join(str(m["path"]) for m in found))
        game = Path(found[0]["path"]).resolve()
    if not (game / DATA_DIR_NAME).is_dir() or not (game / EXE_NAME).is_file():
        raise PatchError("Zero Parades 게임 폴더가 아닙니다: %s" % game)
    return game


def main(argv=None):
    parser = argparse.ArgumentParser(description="ZERO PARADES 한글패치 %s - SteamOS/리눅스 설치 도구" % PATCH_VERSION)
    parser.add_argument("command", choices=["status", "check", "install", "restore"])
    parser.add_argument("--game", help="게임 폴더 (기본: Steam 라이브러리에서 자동으로 찾음)")
    parser.add_argument("--steam-root", help="Steam 폴더 (기본: ~/.local/share/Steam 등에서 자동으로 찾음)")
    parser.add_argument("--exe", help="미리 받아 둔 공식 런처 exe (%s)" % RELEASE_EXE["name"])
    args = parser.parse_args(argv)
    try:
        records = steam_manifests(steam_roots(args.steam_root))
        game = find_game(args.game, records)
        log("게임 폴더: %s" % game)
        backend = Backend(read_payload(obtain_exe(args.exe)))
        if args.command == "status":
            backend.status(game)
        elif args.command == "check":
            backend.check(game, records)
        elif args.command == "install":
            backend.install(game, records)
            backend.status(game)
            log("게임 설정 → 게임플레이 → 텍스트 언어에서 '한국어'를 고르세요.")
        elif args.command == "restore":
            backend.restore(game, records)
            backend.status(game)
    except (PatchError, BuildError) as e:
        log("[중단] %s" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
