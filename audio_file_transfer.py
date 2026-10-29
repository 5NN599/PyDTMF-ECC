#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PyDTMF-ECC protocol and audio-processing core.

This module implements:
- DTMF packet encoding and decoding, with a dedicated 1850 Hz sync pulse.
- 16-byte data blocks protected by CRC-16/CCITT-FALSE.
- Optional systematic Reed–Solomon-style FEC (preset modes and custom parity %).
- Payload framing, optional compression, packet assembly, and WAV encode/decode helpers.

The desktop application's entry point is ``pydtmf_ecc_entry.py``. This module is an
internal core library and is not intended to be launched directly as the GUI.

Transfer timing presets:
- Slow: 50 ms tone + 20 ms gap
- Normal: 30 ms tone + 10 ms gap
- Fast: 20 ms tone + 10 ms gap
"""

import importlib
import importlib.util
import math
import lzma
import os
import subprocess
import sys
import zlib


# ----------------------------------------------------------------------------
# 0. 필요한 라이브러리 자동 설치
# ----------------------------------------------------------------------------
def ensure_package(import_name: str, pip_name: str = None, optional: bool = False):
    """
    패키지를 불러오고, 없으면 pip 로 자동 설치한 뒤 다시 불러온다.
      - 일반 환경 -> 가상환경 -> --user -> --break-system-packages 순서로 시도
      - optional=True 면 설치에 실패해도 None 을 돌려주고 계속 진행 (예: sounddevice)
      - 환경변수 DTMF_NO_AUTO_INSTALL=1 이면 자동 설치를 하지 않음
    """
    pip_name = pip_name or import_name
    try:
        return importlib.import_module(import_name)
    except Exception as err:
        installed = (not isinstance(err, ImportError)) or importlib.util.find_spec(import_name) is not None
        if installed:
            # 설치는 돼 있는데 불러오기에 실패 (예: sounddevice 는 있는데 PortAudio 가 없음)
            print(f"[경고] '{import_name}' 을(를) 불러오지 못했습니다: {err}")
            if import_name == "sounddevice" and sys.platform.startswith("linux"):
                print("       Linux 에서는 'sudo apt install libportaudio2' 가 필요할 수 있습니다.")
            if optional:
                return None
            raise

    if os.environ.get("DTMF_NO_AUTO_INSTALL"):
        if optional:
            return None
        raise ImportError(f"'{import_name}' 이(가) 필요합니다: pip install {pip_name}")

    # A frozen PyInstaller app must never invoke its own EXE as `-m pip`.
    # All mandatory packages are expected to be bundled at build time.
    if getattr(sys, "frozen", False):
        msg = (f"Packaged dependency '{import_name}' could not be loaded: {err}. "
               "Please rebuild or reinstall the application package.")
        if optional:
            print("[warning] " + msg, flush=True)
            return None
        raise ImportError(msg) from err

    print(f"[자동 설치] '{pip_name}' 이(가) 없어서 설치합니다. 잠시만 기다려 주세요...", flush=True)
    if importlib.util.find_spec("pip") is None:                       # pip 자체가 없으면 먼저 준비
        subprocess.run([sys.executable, "-m", "ensurepip", "--upgrade"], capture_output=True)
    base = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q"]
    last = None
    for extra in ([], ["--user"], ["--break-system-packages"], ["--user", "--break-system-packages"]):
        last = subprocess.run(base + extra + [pip_name], capture_output=True, text=True)
        if last.returncode == 0:
            break

    importlib.invalidate_caches()
    try:                                                              # --user 설치 경로를 현재 실행에 반영
        import site
        user_site = site.getusersitepackages()
        if os.path.isdir(user_site) and user_site not in sys.path:
            sys.path.append(user_site)
    except Exception:
        pass
    try:
        mod = importlib.import_module(import_name)
        print(f"[자동 설치] '{pip_name}' 설치 완료", flush=True)
        return mod
    except Exception as err2:
        if last is not None and last.returncode == 0:
            # pip 설치는 성공했는데 불러오기에 실패 (예: sounddevice 인데 시스템에 PortAudio 가 없음)
            msg = f"'{pip_name}' 은(는) 설치됐지만 불러오지 못했습니다: {err2}"
            if import_name == "sounddevice" and sys.platform.startswith("linux"):
                msg += "\n       Linux 에서는 'sudo apt install libportaudio2' 후 다시 실행해 주세요."
        else:
            tail = (last.stderr or "").strip().splitlines()[-3:] if last is not None else []
            msg = (f"'{pip_name}' 자동 설치에 실패했습니다. 직접 설치해 주세요:  pip install {pip_name}\n"
                   + "\n".join(tail))
        if optional:
            print("[경고] " + msg)
            return None
        raise SystemExit(msg)


ensure_package("numpy")
ensure_package("scipy")
import numpy as np
from scipy.io import wavfile
from scipy.signal import butter, sosfiltfilt

# ----------------------------------------------------------------------------
# 1. 상수 정의
# ----------------------------------------------------------------------------
FS = 44100                    # 샘플링 레이트 (Hz)
# 프리셋 이름은 언어 UI와 무관하게 slow/normal/fast 로 유지한다.
SPEED_PRESETS = {
    'slow':   {'tone_ms': 50, 'gap_ms': 20, 'demod_ms': 20},
    'normal': {'tone_ms': 30, 'gap_ms': 10, 'demod_ms': 20},
    'fast':   {'tone_ms': 20, 'gap_ms': 10, 'demod_ms': 16},
}
SPEED_PRESET = 'normal'
TONE_MS = SPEED_PRESETS[SPEED_PRESET]['tone_ms']
DEMOD_MS = SPEED_PRESETS[SPEED_PRESET]['demod_ms']
GAP_MS = SPEED_PRESETS[SPEED_PRESET]['gap_ms']
SYNC_FREQ = 1850.0            # 동기화 펄스 주파수 (Hz), DTMF 1209~1633Hz 대역과 분리

PAYLOAD_BYTES = 16            # 패킷당 데이터 바이트 수
N_SEQ_SYMBOLS = 2             # 패킷번호 DTMF 글자 수
N_DATA_SYMBOLS = PAYLOAD_BYTES * 2   # 데이터 DTMF 글자 수 (32)
N_CRC_SYMBOLS = 4             # CRC-16 DTMF 글자 수
N_SYMBOLS = N_SEQ_SYMBOLS + N_DATA_SYMBOLS + N_CRC_SYMBOLS   # 38

ROW_FREQS = [697.0, 770.0, 852.0, 941.0]        # DTMF 저음군
COL_FREQS = [1209.0, 1336.0, 1477.0, 1633.0]    # DTMF 고음군

# 표준 DTMF 키패드 (행=저음군, 열=고음군)
KEYPAD = [
    ['1', '2', '3', 'A'],
    ['4', '5', '6', 'B'],
    ['7', '8', '9', 'C'],
    ['*', '0', '#', 'D'],
]

# 16진수 문자 <-> 키패드 키 매핑 ( '*' = E, '#' = F )
HEX_TO_KEY = {h: h for h in "0123456789ABCD"}
HEX_TO_KEY.update({'E': '*', 'F': '#'})
KEY_TO_HEX = {k: h for h, k in HEX_TO_KEY.items()}

# 키 -> (저음 주파수, 고음 주파수)
KEY_TO_FREQS = {
    KEYPAD[r][c]: (ROW_FREQS[r], COL_FREQS[c])
    for r in range(4) for c in range(4)
}

# 수신부 튜닝 파라미터
BP_LOW, BP_HIGH = 600.0, 2200.0   # DTMF + 1850Hz 동기 펄스를 모두 통과
SYNC_LOW_RATIO = 5.0             # 동기 판정: P(1850) > 이 값 × max(P(저음군))
SYNC_FLOOR_REL = 0.05             # 동기 판정: 최소 에너지 (기준 대비 비율)
TONE_FLOOR_REL = 0.005             # DTMF 판정: 최소 에너지 (동기 펄스 에너지 대비 비율)
SCAN_HOP_MS = 2                   # 동기 펄스 탐색 간격 (ms)

# 전송 시작음(프리앰블) / 전송 구성
START_FREQ = 1000.0               # 긴 시작음 주파수 (DTMF·동기 주파수와 겹치지 않는 값)
START_TONE_S = 1.0                # 시작음 길이 (초)
START_SILENCE_S = 0.3             # 시작음 뒤 무음 (초)
PASS_GAP_S = 0.5                  # 반복 전송 사이 무음 (초)
LEAD_S, TRAIL_S = 0.3, 0.3        # 신호 앞/뒤 무음 (초)
PKT_SECONDS = (N_SYMBOLS + 1) * (TONE_MS + GAP_MS) / 1000.0


def set_speed_preset(preset: str):
    """속도 프리셋을 설정한다. 'slow'/'normal'/'fast' 또는 한국어 이름을 받는다."""
    global SPEED_PRESET, TONE_MS, GAP_MS, DEMOD_MS, PKT_SECONDS
    aliases = {'느림': 'slow', '보통': 'normal', '빠름': 'fast',
               'slow': 'slow', 'normal': 'normal', 'fast': 'fast'}
    key = aliases.get(str(preset).strip().lower(), 'normal')
    cfg = SPEED_PRESETS[key]
    SPEED_PRESET = key
    TONE_MS, GAP_MS, DEMOD_MS = cfg['tone_ms'], cfg['gap_ms'], cfg['demod_ms']
    PKT_SECONDS = (N_SYMBOLS + 1) * (TONE_MS + GAP_MS) / 1000.0
    return dict(preset=key, tone_ms=TONE_MS, gap_ms=GAP_MS, demod_ms=DEMOD_MS,
                packet_seconds=PKT_SECONDS)


# ----------------------------------------------------------------------------
# 2. CRC-16/CCITT-FALSE
# ----------------------------------------------------------------------------
def _make_crc_table():
    table = []
    for i in range(256):
        crc = i << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if (crc & 0x8000) else (crc << 1)
            crc &= 0xFFFF
        table.append(crc)
    return table


_CRC_TABLE = _make_crc_table()


def crc16_ccitt(data: bytes, init: int = 0xFFFF) -> int:
    """CRC-16/CCITT-FALSE 계산 (poly=0x1021, init=0xFFFF)."""
    crc = init
    for b in data:
        crc = ((crc << 8) & 0xFFFF) ^ _CRC_TABLE[((crc >> 8) ^ b) & 0xFF]
    return crc


# ----------------------------------------------------------------------------
# 2-1. 메시지 컨테이너 (텍스트/파일 구분 + 파일명 + 길이)
# ----------------------------------------------------------------------------
# 헤더 배치(v3):  'DT'(2) 버전(1) 종류|압축방식<<4 (1) 저장길이(4) 원본길이(4) 이름길이(1) 이름(n) 저장된 데이터
#   → 숫자 필드가 모두 앞쪽 13바이트에 있어서, '첫 패킷 하나'만 받아도
#     전체 전송 크기와 전체 패킷 수(=블록 수), 압축 여부, 원본 크기를 알 수 있다.
#   저장길이 = 실제로 소리로 보내는 바이트 수(압축했다면 압축 후 크기)
CONTAINER_MAGIC = b'DT'
CONTAINER_VER = 3
KIND_FILE, KIND_TEXT = 0, 1
KIND_LABEL = {KIND_FILE: '파일', KIND_TEXT: '텍스트'}

# 압축 방식 (종류 바이트의 상위 4비트)
CODEC_NONE, CODEC_ZLIB, CODEC_LZMA = 0, 1, 2
CODEC_LABEL = {CODEC_NONE: '압축 없음', CODEC_ZLIB: 'zlib', CODEC_LZMA: 'LZMA'}
_LZMA_FILTERS = [{"id": lzma.FILTER_LZMA2, "preset": 9, "dict_size": 1 << 20}]   # 송·수신 공통 설정
LZMA_MAX_BYTES = 8 * 1024 * 1024        # 이보다 큰 데이터는 LZMA 시도를 건너뜀 (너무 느림)
MIN_COMPRESS_BYTES = 24                 # 이보다 작으면 압축 시도 안 함


def _compress(codec: int, data: bytes) -> bytes:
    if codec == CODEC_ZLIB:                       # raw deflate: zlib 헤더(2B)·체크섬(4B) 없이 -> 오버헤드 0
        c = zlib.compressobj(9, zlib.DEFLATED, -15)
        return c.compress(data) + c.flush()
    if codec == CODEC_LZMA:                       # raw LZMA2: xz 헤더 없이
        return lzma.compress(data, format=lzma.FORMAT_RAW, filters=_LZMA_FILTERS)
    return data


def choose_codec(data: bytes, allow: bool = True):
    """
    압축 방식 자동 선택: 원본 / zlib / LZMA 중 '가장 작은 것'을 고른다.
    이미 압축된 파일(jpg, zip, mp3...)이나 아주 짧은 데이터는 이득이 없으면 그대로 보낸다.
    반환: (codec, 저장할 바이트열)
    """
    best = (CODEC_NONE, data)
    if allow and len(data) >= MIN_COMPRESS_BYTES:
        for codec in (CODEC_ZLIB, CODEC_LZMA):
            if codec == CODEC_LZMA and len(data) > LZMA_MAX_BYTES:
                continue
            packed = _compress(codec, data)
            if len(packed) < len(best[1]):
                best = (codec, packed)
    return best


def decompress_stream(codec: int, stored: bytes):
    """
    압축을 푼다. 데이터가 '일부만' 도착했어도 풀 수 있는 만큼 풀어서 돌려준다
    (실시간 수신 중 글자가 도착하는 대로 보여 주기 위함).
    반환: (풀린 바이트열, 끝까지 정상적으로 풀렸는지)
    """
    if codec == CODEC_NONE:
        return stored, True
    out = []
    try:
        if codec == CODEC_ZLIB:
            d = zlib.decompressobj(-15)
        elif codec == CODEC_LZMA:
            d = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=_LZMA_FILTERS)
        else:
            return b'', False
        for i in range(0, len(stored), 512):          # 조금씩 넣어 가며 풀어서, 중간에 깨져도 앞부분은 건짐
            out.append(d.decompress(stored[i:i + 512]))
            if d.eof:
                break
        return b''.join(out), bool(d.eof)
    except Exception:
        return b''.join(out), False


def pack_container(data: bytes, kind: int = KIND_FILE, name: str = "", compress: bool = True) -> bytes:
    """데이터를 (필요하면 압축한 뒤) [종류·압축방식·길이·파일명] 헤더와 함께 포장한다."""
    codec, stored = choose_codec(data, compress)
    name = name or ""
    nb = name.encode('utf-8')
    while len(nb) > 255:                     # 파일명은 최대 255바이트
        name = name[:-1]
        nb = name.encode('utf-8')
    return (CONTAINER_MAGIC + bytes([CONTAINER_VER, (codec << 4) | (kind & 0x0F)])
            + len(stored).to_bytes(4, 'big') + len(data).to_bytes(4, 'big')
            + bytes([len(nb)]) + nb + stored)


def parse_container_header(buf: bytes):
    """
    헤더 해석. 반환: ('ok', (kind, name, stored_len, header_len, name_complete, codec, raw_len))
                    / ('short', None) / ('none', None)
      ok    = 숫자 필드(앞 13바이트)가 도착함. 이름이 아직 다 안 왔으면 name_complete=False
      short = 아직 13바이트가 안 모임,  none = 컨테이너가 아님(raw 데이터)
    """
    m = CONTAINER_MAGIC
    head = buf[:len(m)]
    if head != m[:len(head)]:
        return 'none', None
    if len(buf) >= 3 and buf[2] != CONTAINER_VER:
        return 'none', None
    if len(buf) < 13:
        return 'short', None
    kind, codec = buf[3] & 0x0F, buf[3] >> 4
    slen = int.from_bytes(buf[4:8], 'big')
    rlen = int.from_bytes(buf[8:12], 'big')
    name_len = buf[12]
    hlen = 13 + name_len
    name = buf[13:hlen].decode('utf-8', errors='replace')
    return 'ok', (kind, name, slen, hlen, len(buf) >= hlen, codec, rlen)


def n_packets_for(total_bytes: int) -> int:
    """패딩(0x80 포함)까지 고려한 전체 패킷 수"""
    return -(-(total_bytes + 1) // PAYLOAD_BYTES)


def parse_packet_string(pkt: str):
    """38글자 패킷 문자열 -> (CRC정상여부, 패킷번호, 데이터 16바이트)"""
    if len(pkt) != N_SYMBOLS or '?' in pkt:
        return False, -1, b''
    seq = int(pkt[0:2], 16)
    data = bytes.fromhex(pkt[2:34])
    return int(pkt[34:38], 16) == crc16_ccitt(bytes([seq]) + data), seq, data


def format_index_list(indices) -> str:
    """[3,7,12,13,14,15] -> '3,7,12-15'"""
    idx = sorted(set(int(i) for i in indices))
    out, i = [], 0
    while i < len(idx):
        j = i
        while j + 1 < len(idx) and idx[j + 1] == idx[j] + 1:
            j += 1
        out.append(str(idx[i]) if i == j else f"{idx[i]}-{idx[j]}")
        i = j + 1
    return ",".join(out)


def parse_index_list(text: str, limit=None):
    """'3,7,12-15' -> [3,7,12,13,14,15]. 형식 오류/범위 초과는 ValueError."""
    res = set()
    for part in text.replace(' ', '').split(','):
        if not part:
            continue
        if '-' in part:
            a, b = part.split('-', 1)
            a, b = int(a), int(b)
            if a > b:
                raise ValueError(f"범위가 거꾸로입니다: {part}")
            res.update(range(a, b + 1))
        else:
            res.add(int(part))
    if limit is not None and any(i < 0 or i >= limit for i in res):
        raise ValueError(f"블록 번호는 0~{limit - 1} 사이여야 합니다.")
    return sorted(res)


# ----------------------------------------------------------------------------
# 2-2. Selectable FEC (systematic Reed-Solomon block error correction)
# ----------------------------------------------------------------------------
# Mode 0 keeps the original CRC-only wire format.
# Current modes 1/2/3 use 10/20/30% parity overhead, respectively.
# Custom mode (4) accepts 0–100% parity overhead. FEC groups contain up to 100 data blocks.
# Protocol v3 stores the selected rate in the final header byte. Decoders remain compatible
# with legacy v1 (8 data + mode-count parity) and v2 (12 data + 3/5/10 parity) headers.
FEC_MAGIC = b'FEC1'
FEC_VERSION = 3
FEC_GROUP_K = 100
FEC_CUSTOM_GROUP_K = 100
FEC_HEADER_COPIES = 2
FEC_MODE_PERCENT = {1: 10, 2: 20, 3: 30}
FEC_LEGACY_V2_PARITY = {1: 3, 2: 5, 3: 10}


def fec_group_size(mode: int) -> int:
    return FEC_CUSTOM_GROUP_K if int(mode) in (1, 2, 3, 4) else FEC_GROUP_K


def fec_percent_for_mode(mode: int, custom_percent: int = 0) -> int:
    """Return the configured parity overhead percentage for a preset/custom mode."""
    mode = int(mode)
    if mode in FEC_MODE_PERCENT:
        return FEC_MODE_PERCENT[mode]
    if mode == 4:
        return max(0, min(100, int(custom_percent)))
    return 0


def fec_parity_for_group(mode: int, group_data_count: int, custom_percent: int = 0) -> int:
    """Return parity packets for one group, rounded up so the selected percentage is met."""
    k = max(0, int(group_data_count))
    pct = fec_percent_for_mode(mode, custom_percent)
    return int(math.ceil(k * pct / 100.0)) if k and pct else 0


def fec_parity_count(mode: int, custom_percent: int = 0) -> int:
    """Parity packet count for a full group (used by UI and mapping helpers)."""
    return fec_parity_for_group(mode, fec_group_size(mode), custom_percent)


def _fec_total_parity(data_count: int, mode: int, custom_percent: int = 0) -> int:
    group_k = fec_group_size(mode)
    full_groups, remainder = divmod(max(0, int(data_count)), group_k)
    total = full_groups * fec_parity_for_group(mode, group_k, custom_percent)
    if remainder:
        total += fec_parity_for_group(mode, remainder, custom_percent)
    return total


def fec_recovery_capacity(data_blocks: int, mode: int, custom_percent: int = 0) -> dict:
    """Estimate maximum erasures recoverable if every parity packet arrives intact.

    Capacity is per FEC group; summing parity counts yields an upper bound across all groups.
    """
    data_blocks = max(0, int(data_blocks))
    mode = int(mode)
    pct = fec_percent_for_mode(mode, custom_percent)
    parity_blocks = _fec_total_parity(data_blocks, mode, custom_percent) if mode in (1, 2, 3, 4) else 0
    return {
        'data_blocks': data_blocks,
        'parity_percent': pct,
        'parity_blocks': parity_blocks,
        'max_recoverable_blocks': min(data_blocks, parity_blocks),
        'group_size': fec_group_size(mode),
    }


def _legacy_v2_parity_total(data_count: int, mode: int, custom_percent: int = 0) -> int:
    """Parity packet count as emitted by protocol v2."""
    data_count = max(0, int(data_count))
    if mode in FEC_LEGACY_V2_PARITY:
        groups = (data_count + 12 - 1) // 12
        return groups * FEC_LEGACY_V2_PARITY[mode]
    if mode == 4:
        group_k = FEC_CUSTOM_GROUP_K
        full_groups, remainder = divmod(data_count, group_k)
        total = full_groups * int(math.ceil(group_k * custom_percent / 100.0))
        if remainder:
            total += int(math.ceil(remainder * custom_percent / 100.0))
        return total
    return 0


def fec_parity_for_meta(meta: dict, group_data_count: int) -> int:
    """Return the parity count for a group according to the received protocol version."""
    k = max(0, int(group_data_count))
    if not k:
        return 0
    version = int(meta.get('version', FEC_VERSION))
    mode = int(meta['mode'])
    if version == 1:
        return mode
    if version == 2 and mode in FEC_LEGACY_V2_PARITY:
        return FEC_LEGACY_V2_PARITY[mode]
    return fec_parity_for_group(mode, k, int(meta.get('custom_percent', 0)))


def classify_fec_wire_index(wire_index, meta):
    """Classify a physical packet as logical data, an internal header, or parity."""
    if wire_index is None:
        return 'unknown', None
    try:
        wire_index = int(wire_index)
    except (TypeError, ValueError):
        return 'unknown', None
    if wire_index < 0:
        return 'unknown', None
    if meta is None:
        return 'data', wire_index
    if wire_index < FEC_HEADER_COPIES:
        return 'header', None
    group_k = int(meta['group_k'])
    parity = int(meta['parity'])
    stride = group_k + parity
    offset = wire_index - FEC_HEADER_COPIES
    group = offset // stride
    local = offset % stride
    if group < 0 or group >= int(meta['groups']):
        return 'unknown', None
    data_count = int(meta['data_blocks'])
    k = min(group_k, data_count - group * group_k)
    group_parity = fec_parity_for_group(meta['mode'], k, meta.get('custom_percent', 0))
    if local < k:
        return 'data', group * group_k + local
    if local < k + group_parity:
        return 'parity', None
    return 'unknown', None

def _gf_tables():
    exp = [0] * 512
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


_GF_EXP, _GF_LOG = _gf_tables()


def _gf_mul(a, b):
    if not a or not b:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _gf_inv(a):
    if not a:
        raise ZeroDivisionError('GF(256) 0 has no inverse')
    return _GF_EXP[255 - _GF_LOG[a]]


def _gf_matrix_inverse(matrix):
    n = len(matrix)
    aug = [list(row) + [1 if i == j else 0 for j in range(n)] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = next((r for r in range(col, n) if aug[r][col]), None)
        if pivot is None:
            raise ValueError('singular GF(256) matrix')
        aug[col], aug[pivot] = aug[pivot], aug[col]
        inv = _gf_inv(aug[col][col])
        aug[col] = [_gf_mul(v, inv) for v in aug[col]]
        for r in range(n):
            if r == col or not aug[r][col]:
                continue
            factor = aug[r][col]
            aug[r] = [a ^ _gf_mul(factor, b) for a, b in zip(aug[r], aug[col])]
    return [row[n:] for row in aug]


def _gf_matrix_mul(a, b):
    return [[_gf_xor_sum(_gf_mul(a[i][k], b[k][j]) for k in range(len(b)))
             for j in range(len(b[0]))] for i in range(len(a))]


def _gf_xor_sum(values):
    out = 0
    for value in values:
        out ^= value
    return out


def _rs_generator(k, parity_count):
    """Return a systematic (k+p, k) MDS generator matrix over GF(256)."""
    if k <= 0:
        return []
    n = k + parity_count
    vandermonde = [[_gf_exp_power(i + 1, j) for j in range(k)] for i in range(n)]
    top_inverse = _gf_matrix_inverse(vandermonde[:k])
    return _gf_matrix_mul(vandermonde, top_inverse)


def _gf_exp_power(base, power):
    out = 1
    for _ in range(power):
        out = _gf_mul(out, base)
    return out


def _fec_header(mode, stream_len, data_blocks, custom_percent=0):
    """Build the protocol-v3 FEC header (16 bytes)."""
    mode = int(mode)
    group_k = fec_group_size(mode)
    if mode in FEC_MODE_PERCENT:
        rate = FEC_MODE_PERCENT[mode]
    elif mode == 4:
        rate = max(1, min(100, int(custom_percent)))
    else:
        raise ValueError('FEC header mode must be 1, 2, 3, or 4')
    return (FEC_MAGIC + bytes([FEC_VERSION, mode]) + int(stream_len).to_bytes(4, 'big')
            + int(data_blocks).to_bytes(4, 'big') + bytes([group_k, rate]))


def parse_fec_header(block):
    if not isinstance(block, (bytes, bytearray)) or len(block) < PAYLOAD_BYTES:
        return None
    b = bytes(block[:PAYLOAD_BYTES])
    version, mode = b[4], b[5]
    if b[:4] != FEC_MAGIC or version not in (1, 2, FEC_VERSION):
        return None
    if version == 1:
        if mode not in (1, 2, 3):
            return None
    elif mode not in (1, 2, 3, 4):
        return None

    stream_len = int.from_bytes(b[6:10], 'big')
    data_count = int.from_bytes(b[10:14], 'big')
    group_k = b[14]
    reserved = b[15]
    custom_percent = 0

    if version == 1:
        # Legacy v1: groups of 8 data blocks, with 1/2/3 parity packets.
        if group_k != 8:
            return None
        parity = mode
    elif version == 2 and mode in FEC_LEGACY_V2_PARITY:
        # Legacy v2 presets: groups of 12 data blocks and fixed 3/5/10 parity packets.
        if group_k != 12 or reserved != 0:
            return None
        parity = FEC_LEGACY_V2_PARITY[mode]
    elif version == 2 and mode == 4:
        # Legacy v2 custom mode: groups of 100 data blocks and a percentage in reserved.
        if group_k != FEC_CUSTOM_GROUP_K or not 1 <= reserved <= 100:
            return None
        custom_percent = reserved
        parity = int(math.ceil(group_k * custom_percent / 100.0))
    else:
        # Current v3: all FEC modes use up to 100 data blocks per group. The rate byte
        # is fixed at 10/20/30 for modes 1/2/3 and user-selected for Custom mode.
        if group_k != FEC_CUSTOM_GROUP_K or not 1 <= reserved <= 100:
            return None
        if mode in FEC_MODE_PERCENT and reserved != FEC_MODE_PERCENT[mode]:
            return None
        custom_percent = reserved
        parity = int(math.ceil(group_k * custom_percent / 100.0))

    if stream_len % PAYLOAD_BYTES or data_count != stream_len // PAYLOAD_BYTES or data_count <= 0:
        return None
    groups = (data_count + group_k - 1) // group_k
    if version == 1:
        parity_total = parity * groups
    elif version == 2:
        parity_total = _legacy_v2_parity_total(data_count, mode, custom_percent)
    else:
        parity_total = _fec_total_parity(data_count, mode, custom_percent)
    wire_count = FEC_HEADER_COPIES + data_count + parity_total
    if wire_count > 0x7FFFFFFF:
        return None
    return {'version': version, 'mode': mode, 'custom_percent': custom_percent,
            'stream_len': stream_len, 'data_blocks': data_count, 'group_k': group_k,
            'parity': parity, 'groups': groups, 'wire_blocks': wire_count}


def fec_wire_packet_count(payload_len: int, fec_mode: int, custom_percent: int = 0) -> int:
    """Number of physical packets generated by this FEC configuration."""
    mode = int(fec_mode)
    base_blocks = n_packets_for(payload_len)
    if mode not in (0, 1, 2, 3, 4):
        raise ValueError('Recovery mode must be 0–3 or 4 (Custom).')
    if mode == 4:
        pct = int(custom_percent)
        if pct == 0:
            return base_blocks
        if not 1 <= pct <= 100:
            raise ValueError('Custom parity must be between 0 and 100 percent.')
    parity_total = _fec_total_parity(base_blocks, mode, custom_percent)
    if not parity_total:
        return base_blocks
    return FEC_HEADER_COPIES + base_blocks + parity_total


def _fec_encode_stream(payload: bytes, mode: int, custom_percent=0):
    """Return physical payload blocks: two internal headers, then data and parity groups."""
    mode = int(mode)
    group_k = fec_group_size(mode)
    if mode not in (1, 2, 3, 4):
        raise ValueError('fec_mode must be 1, 2, 3, or 4 (custom)')
    if mode == 4 and not (1 <= int(custom_percent) <= 100):
        raise ValueError('custom_percent must be between 1 and 100 for custom FEC')
    custom_percent = int(custom_percent) if mode == 4 else 0
    padded = payload + b'\x80'
    padded += b'\x00' * ((-len(padded)) % PAYLOAD_BYTES)
    data_blocks = [padded[i:i + PAYLOAD_BYTES] for i in range(0, len(padded), PAYLOAD_BYTES)]
    physical = [_fec_header(mode, len(padded), len(data_blocks), custom_percent)] * FEC_HEADER_COPIES
    for offset in range(0, len(data_blocks), group_k):
        group = data_blocks[offset:offset + group_k]
        k = len(group)
        parity_count = fec_parity_for_group(mode, k, custom_percent)
        generator = _rs_generator(k, parity_count)
        physical.extend(group)
        for row in generator[k:k + parity_count]:
            par = bytearray(PAYLOAD_BYTES)
            for j, block in enumerate(group):
                coef = row[j]
                if coef:
                    for b in range(PAYLOAD_BYTES):
                        par[b] ^= _gf_mul(coef, block[b])
            physical.append(bytes(par))
    return physical

def _fec_recover_group(data_group, parity_group, mode):
    """Recover absent 16-byte data blocks when enough valid parity blocks remain."""
    k = len(data_group)
    missing = [i for i, block in enumerate(data_group) if block is None]
    if not missing:
        return data_group, []
    available = [(i, b) for i, b in enumerate(parity_group[:mode]) if b is not None]
    if len(available) < len(missing):
        return data_group, missing
    generator = _rs_generator(k, mode)
    selected = available[:len(missing)]
    coeff = [[generator[k + parity_i][data_i] for data_i in missing] for parity_i, _ in selected]
    try:
        inv = _gf_matrix_inverse(coeff)
    except ValueError:
        return data_group, missing
    recovered = [bytearray(PAYLOAD_BYTES) for _ in missing]
    rhs = [[0] * PAYLOAD_BYTES for _ in selected]
    for r, (parity_i, parity_block) in enumerate(selected):
        rhs[r] = list(parity_block)
        row = generator[k + parity_i]
        for data_i, data_block in enumerate(data_group):
            if data_block is None:
                continue
            c = row[data_i]
            if c:
                for b in range(PAYLOAD_BYTES):
                    rhs[r][b] ^= _gf_mul(c, data_block[b])
    for unknown_i in range(len(missing)):
        for r in range(len(selected)):
            c = inv[unknown_i][r]
            if c:
                for b in range(PAYLOAD_BYTES):
                    recovered[unknown_i][b] ^= _gf_mul(c, rhs[r][b])
    out = list(data_group)
    for i, block in zip(missing, recovered):
        out[i] = bytes(block)
    return out, []


class PacketAssembler:
    """CRC 정상 음향 블록을 모으고, 선택된 FEC가 있으면 손실 데이터 블록을 복구한다."""

    def __init__(self):
        self.good = {}
        self._prev = None
        self._expected = None
        self._dcache = None
        self._fec_cache = None

    @property
    def count(self):
        return len(self.good)

    def _fec_meta(self):
        # 첫 두 패킷은 같은 FEC 메타데이터다. 첫 패킷 하나가 사라져도 모드를 알 수 있다.
        for i in (0, 1):
            meta = parse_fec_header(self.good.get(i, b''))
            if meta is not None:
                return meta
        return None

    def expected_packets(self):
        if self._expected is None:
            meta = self._fec_meta()
            if meta is not None:
                self._expected = meta['wire_blocks']
            elif 0 in self.good:
                st, info = parse_container_header(self.good[0])
                if st == 'ok':
                    self._expected = n_packets_for(info[3] + info[2])
        return self._expected

    def resolve(self, seq: int) -> int:
        """패킷번호(0~255) -> 절대 물리 블록 인덱스 (저장은 하지 않음)."""
        exp = self.expected_packets()
        ref = (self._prev + 1) if self._prev is not None else 0
        if exp:
            cands = list(range(seq, exp, 256))
            if cands:
                pool = [c for c in cands if c not in self.good] or cands
                return min(pool, key=lambda c: abs(c - ref))
        if self._prev is None:
            return seq
        base = self._prev - (self._prev % 256)
        ab = base + seq
        if ab < self._prev - 128:
            ab += 256
        elif ab > self._prev + 128 and ab - 256 >= 0:
            ab -= 256
        return ab

    def add(self, seq: int, data: bytes) -> int:
        ab = self.resolve(seq)
        self._prev = ab
        if self.good.get(ab) != data:
            self.good.setdefault(ab, data)
            self._fec_cache = None
            self._dcache = None
        # Header may arrive after a later packet established an unknown total.
        self._expected = None
        return ab

    def _decode_fec(self):
        meta = self._fec_meta()
        if meta is None:
            return None
        cache_key = (len(self.good), hash(tuple(sorted(self.good.items()))))
        if self._fec_cache is not None and self._fec_cache[0] == cache_key:
            return self._fec_cache[1]
        mode, data_count = meta['mode'], meta['data_blocks']
        custom_percent = int(meta.get('custom_percent', 0))
        group_k, parity_count = meta['group_k'], meta['parity']
        output_blocks = []
        missing_data = []
        recovered_indices = []
        full_prefix_blocks = []
        prefix_open = True
        groups = meta['groups']
        for g in range(groups):
            source_start = g * group_k
            k = min(group_k, data_count - source_start)
            wire_start = FEC_HEADER_COPIES + g * (group_k + parity_count)
            group_parity = fec_parity_for_meta(meta, k)
            data_group = [self.good.get(wire_start + j) for j in range(k)]
            parity_group = [self.good.get(wire_start + k + j) for j in range(group_parity)]
            before_missing = [j for j, block in enumerate(data_group) if block is None]
            fixed_group, unresolved = _fec_recover_group(data_group, parity_group, group_parity)
            recovered_indices.extend(source_start + j for j in before_missing if j not in unresolved)
            for j, block in enumerate(fixed_group):
                abs_data_idx = source_start + j
                if block is None:
                    missing_data.append(abs_data_idx)
                    output_blocks.append(b'\x00' * PAYLOAD_BYTES)
                    prefix_open = False
                else:
                    output_blocks.append(block)
                if prefix_open and block is not None:
                    full_prefix_blocks.append(block)
            # unresolved items are precisely the remaining missing slots; update missing list if
            # not already covered and never let a later group falsely extend the contiguous prefix.
            for j in unresolved:
                abs_idx = source_start + j
                if abs_idx not in missing_data:
                    missing_data.append(abs_idx)
            if unresolved:
                prefix_open = False
        joined = b''.join(output_blocks)[:meta['stream_len']]
        prefix = b''.join(full_prefix_blocks)
        # Prefix is cut at first missing logical data block; trim only after preserving its boundary.
        if len(prefix) > meta['stream_len']:
            prefix = prefix[:meta['stream_len']]
        result = {'meta': meta, 'joined': joined, 'prefix': prefix,
                  'missing': sorted(set(missing_data)), 'recovered': sorted(set(recovered_indices)),
                  'complete': not missing_data}
        self._fec_cache = (cache_key, result)
        return result

    def prefix(self) -> bytes:
        """논리 데이터 기준으로 첫 미복구 블록 전까지의 연속 프리픽스."""
        fec = self._decode_fec()
        if fec is not None:
            return fec['prefix']
        out, i = [], 0
        while i in self.good:
            out.append(self.good[i])
            i += 1
        return b''.join(out)

    def assemble(self):
        """(재조립 바이트열, 누락 논리 블록 번호 리스트, 예상 전송 패킷 수)."""
        if not self.good:
            return b'', [], None
        fec = self._decode_fec()
        if fec is not None:
            return fec['joined'], list(fec['missing']), fec['meta']['wire_blocks']
        expected = self.expected_packets()
        last = max(self.good)
        if expected:
            last = max(last, expected - 1)
        missing = [i for i in range(last + 1) if i not in self.good]
        joined = b''.join(self.good.get(i, b'\x00' * PAYLOAD_BYTES) for i in range(last + 1))
        return joined, missing, expected

    def status(self):
        """GUI 실시간 상태. FEC 모드에서는 물리 블록 수와 복구된 데이터 블록을 별도 표시."""
        fec = self._decode_fec()
        prefix = fec['prefix'] if fec is not None else self.prefix()
        meta = fec['meta'] if fec is not None else None
        st, info = parse_container_header(prefix if prefix else (self.good.get(0, b'') if meta is None else b''))
        fec_data_received = 0
        if meta is not None:
            for logical_idx in range(meta['data_blocks']):
                group, slot = divmod(logical_idx, meta['group_k'])
                wire_idx = FEC_HEADER_COPIES + group * (meta['group_k'] + meta['parity']) + slot
                fec_data_received += int(wire_idx in self.good)
        out = {'container': False, 'kind': None, 'name': None, 'size': None, 'data': prefix,
               'expected': self.expected_packets(), 'received': len(self.good), 'complete': False,
               'codec': None, 'stored_size': None, 'fec_mode': meta['mode'] if meta else 0,
               'fec_percent': meta.get('custom_percent', 0) if meta else 0,
               'fec_version': meta['version'] if meta else 0,
               'fec_group_k': meta['group_k'] if meta else FEC_GROUP_K,
               'fec_parity_count': meta['parity'] if meta else 0,
               'fec_data_blocks': meta['data_blocks'] if meta else 0,
               'fec_data_received': fec_data_received,
               'fec_resolved_count': (meta['data_blocks'] - len(fec['missing'])) if fec and meta else 0,
               'fec_recovered': len(fec['recovered']) if fec else 0,
               'fec_recovered_indices': list(fec['recovered']) if fec else [],
               'fec_header_recovered': bool(meta is not None and 0 not in self.good and 1 in self.good),
               'fec_missing': list(fec['missing']) if fec else []}
        if st == 'ok':
            kind, name, slen, hlen, name_done, codec, rlen = info
            stored = prefix[hlen:hlen + slen] if name_done else b''
            key = (codec, len(stored), hash(stored))
            if name_done:
                if self._dcache is not None and self._dcache[0] == key:
                    data, finished = self._dcache[1]
                else:
                    data, finished = decompress_stream(codec, stored)
                    self._dcache = (key, (data, finished))
            else:
                data, finished = b'', False
            if meta is not None:
                full_stream = fec['joined']
                inner_complete = (len(full_stream) >= hlen + slen and name_done and
                                  len(data) == rlen and (codec == CODEC_NONE or finished))
                complete = bool(fec['complete'] and inner_complete)
                exp = meta['wire_blocks']
            else:
                exp = n_packets_for(hlen + slen)
                complete = all(i in self.good for i in range(exp))
            out.update(container=True, kind=kind, name=name, size=rlen, stored_size=slen, codec=codec,
                       data=data, expected=exp, complete=complete)
        elif st == 'short':
            out['data'] = b''
        elif meta is not None:
            out['complete'] = bool(fec['complete'])
            out['data'] = prefix
        return out


class BlockMap:
    """
    EasyPal 스타일 '블록 칸' 상태 관리.
      UNKNOWN(대기) / OK(CRC 성공) / FAIL(CRC 실패·미수신) / RECOVERED(실패했다가 재전송으로 CRC 통과)
    - CRC 에러 패킷은 번호를 알 수 없으므로, 패킷이 1.56초 간격으로 오는 것을 이용해
      '시간 위치'로 어느 칸인지 추정한다. (동기 펄스를 놓친 칸도 시간 간격으로 알 수 있음)
    - 상태는 UNKNOWN→OK/FAIL, FAIL→RECOVERED 로만 바뀐다 (이미 성공한 칸은 절대 내려가지 않음).
    """
    UNKNOWN, OK, FAIL, RECOVERED = 0, 1, 2, 3
    PHASE_TOL = 0.12

    def __init__(self):
        self.state = {}
        self.total = None
        self.anchor = None          # (칸 번호, 시각) — 시간 위치 추정 기준
        self.last_idx = -1

    def set_total(self, n):
        if n:
            self.total = int(n)

    def new_pass(self):
        """새 전송 라운드 시작 (시작음 감지 등)"""
        self.anchor = None
        self.last_idx = -1

    def _mark_fail(self, i):
        if i >= 0 and self.state.get(i, 0) == self.UNKNOWN and (self.total is None or i < self.total):
            self.state[i] = self.FAIL

    def _predict(self, t):
        if self.anchor is None:
            return None
        r = (t - self.anchor[1]) / PKT_SECONDS
        if abs(r - round(r)) > self.PHASE_TOL:
            return None             # 시간 위상이 안 맞음 = 다른 전송 라운드
        return self.anchor[0] + int(round(r))

    def on_packet(self, t, ok, idx=None):
        """t: 동기 펄스 시각(초), ok: CRC 결과, idx: CRC 성공 시 패킷 인덱스"""
        pred = self._predict(t)
        if pred is None and self.anchor is not None:
            self.last_idx = -1      # 위상 불일치 -> 새 라운드
        if ok and idx is not None:
            for i in range(self.last_idx + 1, idx):          # 건너뛴 칸은 실패 처리
                self._mark_fail(i)
            prev = self.state.get(idx, self.UNKNOWN)
            self.state[idx] = self.RECOVERED if prev in (self.FAIL, self.RECOVERED) else self.OK
            self.anchor, self.last_idx = (idx, t), idx
        else:
            if pred is not None:
                guess = pred
            elif self.anchor is not None:
                guess = 0           # 새 라운드의 첫 패킷은 0번으로 가정
            else:
                guess = self.last_idx + 1
            for i in range(self.last_idx + 1, guess):
                self._mark_fail(i)
            self._mark_fail(guess)
            self.anchor, self.last_idx = (guess, t), guess

    def mark_unseen_failed(self):
        """전송이 끝났는데(타임아웃) 아직 대기 상태인 칸은 미수신 = 실패"""
        if self.total:
            for i in range(self.total):
                self._mark_fail(i)

    def states_list(self):
        n = self.total if self.total else (max(self.state) + 1 if self.state else 0)
        return [self.state.get(i, self.UNKNOWN) for i in range(n)]


def extract_message(joined: bytes, missing=()):
    """합친 바이트열에서 실제 메시지를 꺼낸다 (컨테이너 해석 + 압축 해제, 또는 raw 패딩 제거)."""
    st, info = parse_container_header(joined)
    if st == 'ok':
        kind, name, slen, hlen, _done, codec, rlen = info
        stored = joined[hlen:hlen + slen]
        data, finished = decompress_stream(codec, stored)
        need = -(-(hlen + slen) // PAYLOAD_BYTES)
        truncated = (len(stored) < slen or any(i < need for i in missing)
                     or not finished or len(data) != rlen)
        return dict(kind=kind, name=name, data=data, container=True, truncated=truncated,
                    codec=codec, raw_size=rlen, stored_size=slen)
    stripped = joined.rstrip(b'\x00')
    if stripped.endswith(b'\x80'):
        return dict(kind=None, name=None, data=stripped[:-1], container=False, truncated=bool(missing),
                    codec=None, raw_size=None, stored_size=None)
    return dict(kind=None, name=None, data=joined, container=False, truncated=True,
                codec=None, raw_size=None, stored_size=None)


# ----------------------------------------------------------------------------
# 3. 송신부 (Encoder)
# ----------------------------------------------------------------------------
def _make_tone(freqs, n_samples, fs, amplitude=0.8, ramp_ms=1.0):
    """
    여러 사인파를 합성해 하나의 톤을 만든다.
    - 전체 최대 진폭이 amplitude 가 되도록 성분별로 amplitude/len(freqs) 씩 배분
    - 양 끝 1ms 를 raised-cosine 으로 서서히 키우고 줄여서 '딱' 소리(스펙트럼 번짐)를 방지
    """
    t = np.arange(n_samples) / fs
    sig = np.zeros(n_samples)
    for f in freqs:
        sig += (amplitude / len(freqs)) * np.sin(2 * np.pi * f * t)

    r = int(fs * ramp_ms / 1000)
    if r > 0:
        ramp = 0.5 * (1 - np.cos(np.pi * np.arange(r) / r))
        env = np.ones(n_samples)
        env[:r] = ramp
        env[-r:] = ramp[::-1]
        sig *= env
    return sig


def bytes_to_packet_strings(data: bytes, fec_mode: int = 0, fec_percent: int = 0):
    """Encode payload bytes as CRC packets, optionally adding Reed–Solomon parity."""
    fec_mode = int(fec_mode)
    if fec_mode == 4:
        fec_percent = int(fec_percent)
        if fec_percent == 0:
            fec_mode = 0  # Custom 0% is intentionally identical to CRC-only mode.
        elif not 1 <= fec_percent <= 100:
            raise ValueError('Custom parity must be between 0 and 100 percent.')
    if fec_mode not in (0, 1, 2, 3, 4):
        raise ValueError('Recovery mode must be 0–3 or 4 (Custom).')
    if fec_mode:
        blocks = _fec_encode_stream(data, fec_mode, fec_percent)
        packets = []
        for i, block in enumerate(blocks):
            seq = i % 256
            crc = crc16_ccitt(bytes([seq]) + block)
            packets.append(f'{seq:02X}{block.hex().upper()}{crc:04X}')
        return packets
    # CRC-only 모드는 기존 와이어 형식을 그대로 유지한다.
    padded = data + b'\x80'
    padded += b'\x00' * ((-len(padded)) % PAYLOAD_BYTES)
    hex_str = padded.hex().upper()
    step = PAYLOAD_BYTES * 2
    packets = []
    for i in range(len(hex_str) // step):
        seq = i % 256
        chunk_hex = hex_str[i * step:(i + 1) * step]
        crc = crc16_ccitt(bytes([seq]) + bytes.fromhex(chunk_hex))
        packets.append(f'{seq:02X}{chunk_hex}{crc:04X}')
    return packets


def count_wire_packets(payload: bytes, fec_mode: int = 0, fec_percent: int = 0) -> int:
    """Return the physical packet count without computing the parity payloads."""
    return fec_wire_packet_count(len(payload), fec_mode, fec_percent)


def make_packet_synth(fs: int = FS):
    """패킷 문자열(38글자) -> 오디오 배열 함수를 반환 (현재 프리셋 톤 16종을 미리 생성)."""
    tone_n = int(round(fs * TONE_MS / 1000))
    gap = np.zeros(int(round(fs * GAP_MS / 1000)))
    tone_cache = {key: _make_tone(freqs, tone_n, fs) for key, freqs in KEY_TO_FREQS.items()}
    sync_tone = _make_tone([SYNC_FREQ], tone_n, fs)

    def synth(pkt: str):
        parts = [sync_tone, gap]                       # 동기화 펄스 30ms + 공백 10ms
        for hex_ch in pkt:                             # DTMF 38글자
            parts.append(tone_cache[HEX_TO_KEY[hex_ch]])
            parts.append(gap)
        return np.concatenate(parts)

    return synth


def make_start_tone(fs: int = FS, seconds: float = START_TONE_S):
    """전송 맨 앞에 붙는 긴 시작음 (1000Hz 단일 톤, 양 끝 20ms 페이드)"""
    return _make_tone([START_FREQ], int(fs * seconds), fs, ramp_ms=20.0)


def iter_transmission(packets, fs: int = FS, start_tone: bool = True, repeat: int = 1,
                      lead_silence_s: float = LEAD_S, trail_silence_s: float = TRAIL_S):
    """
    전송 구성을 순서대로 내보내는 생성기: (종류, 오디오 배열) 
      종류: 'silence' / 'start' / 'packet'
    한 라운드 = [시작음 1초][무음 0.3초][패킷 × N],  라운드 사이에 무음 0.5초
    (실시간 송신은 이 생성기를 그대로 스피커에 쓰고, WAV 저장은 이어 붙인다)
    """
    synth = make_packet_synth(fs)
    start = make_start_tone(fs) if start_tone else None
    yield 'silence', np.zeros(int(fs * lead_silence_s))
    for r in range(max(1, int(repeat))):
        if r > 0:
            yield 'silence', np.zeros(int(fs * PASS_GAP_S))
        if start is not None:
            yield 'start', start
            yield 'silence', np.zeros(int(fs * START_SILENCE_S))
        for pkt in packets:
            yield 'packet', synth(pkt)
    yield 'silence', np.zeros(int(fs * trail_silence_s))


def transmission_seconds(n_packets: int, repeat: int = 1, start_tone: bool = True):
    """전송 전체 길이(초): 앞뒤 무음 포함"""
    one = n_packets * PKT_SECONDS + ((START_TONE_S + START_SILENCE_S) if start_tone else 0.0)
    return LEAD_S + TRAIL_S + repeat * one + (repeat - 1) * PASS_GAP_S


def prepare_payload(data: bytes, kind=None, name: str = "", compress: bool = True) -> bytes:
    """
    전송할 바이트열을 만든다.
      kind 지정   : 메시지 컨테이너로 포장 (+ compress=True 면 이득이 있을 때 자동 압축)
      kind=None   : raw 그대로 (컨테이너가 없어 압축 불가)
    """
    return pack_container(data, kind, name, compress) if kind is not None else data


def describe_payload(payload: bytes) -> dict:
    """전송 데이터 요약: 원본/전송 크기, 압축 방식, 패킷 수 (압축 안 했을 때와 비교용)"""
    st, info = parse_container_header(payload)
    n = n_packets_for(len(payload))
    if st != 'ok':
        return dict(container=False, orig=len(payload), stored=len(payload), codec=CODEC_NONE,
                    packets=n, packets_plain=n)
    _kind, _name, slen, hlen, _d, codec, rlen = info
    return dict(container=True, orig=rlen, stored=slen, codec=codec,
                packets=n, packets_plain=n_packets_for(hlen + rlen))


def select_packets(packets, only=None):
    """only(패킷 인덱스 목록)가 있으면 그 패킷만 골라 반환 (누락분만 재전송)"""
    if not only:
        return packets
    return [packets[i] for i in sorted(set(only)) if 0 <= i < len(packets)]


def build_signal_from_payload(payload: bytes, fs: int = FS, lead_silence_s: float = LEAD_S,
                              trail_silence_s: float = TRAIL_S, start_tone: bool = True,
                              repeat: int = 1, only=None, fec_mode: int = 0, fec_percent: int = 0):
    """
    전송 데이터(payload) -> (오디오 float 배열, 메시지의 전체 패킷 수).
    WAV 저장·실시간 송신·루프백 테스트가 공통 사용.
      start_tone : 맨 앞 긴 시작음 포함 여부
      repeat     : 전체를 몇 번 반복 전송할지 (앞 라운드에서 실패한 칸이 뒤 라운드에서 복원됨)
      only       : 지정한 패킷 인덱스만 전송 (예: [3, 7, 12, 13])
    """
    packets = bytes_to_packet_strings(payload, fec_mode=fec_mode, fec_percent=fec_percent)
    chosen = select_packets(packets, only)
    chunks = [a for _k, a in iter_transmission(chosen, fs, start_tone, repeat, lead_silence_s, trail_silence_s)]
    return np.concatenate(chunks), len(packets)


def build_signal(data: bytes, fs: int = FS, kind=None, name: str = "",
                 lead_silence_s: float = LEAD_S, trail_silence_s: float = TRAIL_S,
                 start_tone: bool = True, repeat: int = 1, only=None, compress: bool = True,
                 fec_mode: int = 0, fec_percent: int = 0):
    """Encode data to audio; pass kind to include the transfer container."""
    return build_signal_from_payload(prepare_payload(data, kind, name, compress), fs,
                                     lead_silence_s, trail_silence_s, start_tone, repeat, only, fec_mode, fec_percent)


def encode_payload_to_wav(payload: bytes, wav_path: str, fs: int = FS,
                          lead_silence_s: float = LEAD_S, trail_silence_s: float = TRAIL_S,
                          start_tone: bool = True, repeat: int = 1, only=None, fec_mode: int = 0, fec_percent: int = 0):
    """Write a payload as a WAV transmission."""
    signal, n_pkt = build_signal_from_payload(payload, fs, lead_silence_s, trail_silence_s,
                                              start_tone, repeat, only, fec_mode, fec_percent)
    pcm = np.clip(signal * 32767, -32768, 32767).astype(np.int16)   # 16비트 PCM
    wavfile.write(wav_path, fs, pcm)
    return n_pkt, len(signal) / fs


def encode_bytes_to_wav(data: bytes, wav_path: str, fs: int = FS,
                        lead_silence_s: float = LEAD_S, trail_silence_s: float = TRAIL_S,
                        kind=None, name: str = "", start_tone: bool = True, repeat: int = 1,
                        only=None, compress: bool = True, fec_mode: int = 0, fec_percent: int = 0):
    """Encode bytes to WAV, optionally wrapping them in a text/file container."""
    return encode_payload_to_wav(prepare_payload(data, kind, name, compress), wav_path, fs,
                                 lead_silence_s, trail_silence_s, start_tone, repeat, only, fec_mode, fec_percent)

def encode_file_to_wav(in_path: str, wav_path: str, fs: int = FS, **kw):
    """임의의 파일(바이너리/텍스트)을 읽어 .wav 로 인코딩한다. (원래 파일명 포함, 자동 압축)"""
    with open(in_path, 'rb') as f:
        data = f.read()
    return encode_bytes_to_wav(data, wav_path, fs, kind=KIND_FILE, name=os.path.basename(in_path), **kw)


# ----------------------------------------------------------------------------
# 4. 수신부 (Decoder)
# ----------------------------------------------------------------------------
def bandpass_filter(x, fs, low=BP_LOW, high=BP_HIGH, order=4):
    """
    Butterworth 밴드패스 (600~2200Hz; 1850Hz 동기음 포함).
    sosfiltfilt 는 앞뒤로 두 번 필터링해 '위상 지연이 0'이므로
    필터 때문에 동기 타이밍이 밀리지 않는다.
    """
    sos = butter(order, [low, high], btype='bandpass', fs=fs, output='sos')
    return sosfiltfilt(sos, x)


def goertzel_power(x, starts, frame_len, freqs, fs, window=None, chunk=1024):
    """
    Goertzel 알고리즘으로 여러 구간 × 여러 주파수의 에너지를 한꺼번에 계산한다.

    - x         : 신호 (1차원)
    - starts    : 각 분석 구간의 시작 샘플 인덱스 배열
    - frame_len : 구간 길이 (20ms = 882 샘플)
    - freqs     : 검사할 주파수 목록 (FFT 와 달리 필요한 주파수만 계산)
    - window    : 창 함수 (Hann 등, 스펙트럼 누설 감소)

    Goertzel 점화식 (주파수마다):
        s[n] = x[n] + coeff * s[n-1] - s[n-2],   coeff = 2*cos(2*pi*f/fs)
        power = s[N-1]^2 + s[N-2]^2 - coeff * s[N-1] * s[N-2]
    파이썬 루프는 '샘플 수(882)' 만큼만 돌고, 구간 × 주파수 축은 numpy 로 벡터화한다.

    반환: shape (len(freqs), len(starts)) 의 에너지 배열
    """
    starts = np.asarray(starts, dtype=np.int64)
    freqs = np.asarray(freqs, dtype=float)
    n_f = len(freqs)
    coeff = (2.0 * np.cos(2.0 * np.pi * freqs / fs))[:, None]   # (F, 1)
    idx = np.arange(frame_len)
    out = np.empty((n_f, len(starts)))

    for c0 in range(0, len(starts), chunk):
        s = starts[c0:c0 + chunk]
        m = len(s)
        frames = x[s[:, None] + idx[None, :]]           # (M, N)
        if window is not None:
            frames = frames * window
        frames = np.ascontiguousarray(frames.T)         # (N, M) - 행 접근을 빠르게

        s1 = np.zeros((n_f, m))
        s2 = np.zeros((n_f, m))
        for n in range(frame_len):                      # Goertzel 점화식
            s0 = frames[n] + coeff * s1 - s2
            s2, s1 = s1, s0
        out[:, c0:c0 + m] = s1 * s1 + s2 * s2 - coeff * s1 * s2

    return np.maximum(out, 0.0)


def find_sync_pulses(x, fs, tone_n, hann):
    """
    신호 전체를 훑으면서 1850Hz 동기화 펄스 내부의 분석 창 위치를 찾는다.
    창은 30ms 펄스 안에 들어가도록 20ms로 설정한다.

    판정 조건 (DTMF 데이터와 구분하기 위해 '저음군 부재'를 함께 확인):
        P(1850) > floor   그리고   P(1850) > SYNC_LOW_RATIO * max(P(697,770,852,941))
    연속으로 판정된 구간(run)마다 에너지가 최대인 위치를 찾고, 샘플 단위로 미세 보정한다.

    반환: [(동기 펄스 안의 분석 창 시작 샘플, 그 위치에서의 1850Hz 에너지), ...]
    """
    period = int(round(fs * (TONE_MS + GAP_MS) / 1000))
    packet_len = period * (N_SYMBOLS + 1)
    hop = max(1, int(fs * SCAN_HOP_MS / 1000))

    if len(x) < tone_n + hop:
        return []

    # --- (1) 거친 탐색: 2ms 간격으로 1850Hz 와 저음군 4개 주파수 에너지 측정 ---
    starts = np.arange(0, len(x) - tone_n, hop)
    P = goertzel_power(x, starts, tone_n, [SYNC_FREQ] + ROW_FREQS, fs, hann)
    p_sync = P[0]
    p_low = P[1:].max(axis=0)

    floor = SYNC_FLOOR_REL * np.percentile(p_sync, 99)
    flag = (p_sync > floor) & (p_sync > SYNC_LOW_RATIO * p_low)
    hits = np.flatnonzero(flag)
    if len(hits) == 0:
        return []

    # --- (2) 연속 구간(run)으로 묶기 ---
    runs = np.split(hits, np.flatnonzero(np.diff(hits) > 1) + 1)

    # --- (3) run 마다 최대 에너지 위치 -> 샘플 단위 미세 보정 ---
    found = []
    last_t0 = None
    for run in runs:
        best = run[np.argmax(p_sync[run])]
        coarse = int(starts[best])
        fine_starts = np.arange(max(0, coarse - hop), min(len(x) - tone_n, coarse + hop) + 1, 2)
        pf = goertzel_power(x, fine_starts, tone_n, [SYNC_FREQ], fs, hann)[0]
        k = int(np.argmax(pf))
        t0, p0 = int(fine_starts[k]), float(pf[k])

        # 이전 패킷 구간 안쪽의 오탐/중복은 무시하되, 스피커·필터에 따른
        # 동기 창 위치 변동이 5ms를 넘을 수 있으므로 약 25ms 정도의 여유를 둔다.
        if last_t0 is not None and t0 < last_t0 + packet_len - int(0.040 * fs):
            continue
        found.append((t0, p0))
        last_t0 = t0
    return found


def decode_wav_to_bytes(wav_path: str, verbose: bool = True):
    """
    .wav 파일을 디코딩해 (복원된 바이트열, 통계 dict) 를 반환한다.
    각 패킷의 CRC-16 검증 결과를 출력한다.
    """
    # --- (0) 파일 읽기 -> float, 모노 변환 ---
    fs, raw = wavfile.read(wav_path)
    x = raw.astype(np.float64)
    if x.ndim == 2:
        x = x.mean(axis=1)
    if np.issubdtype(raw.dtype, np.integer):
        x /= np.iinfo(raw.dtype).max + 1.0

    # --- (1) 밴드패스 필터 (600~2200Hz) ---
    x = bandpass_filter(x, fs)

    tone_n = int(round(fs * DEMOD_MS / 1000))
    period = int(round(fs * (TONE_MS + GAP_MS) / 1000))
    hann = np.hanning(tone_n)

    # --- (2) 1850Hz 동기화 펄스 탐색 ---
    syncs = find_sync_pulses(x, fs, tone_n, hann)

    # 신호 끝에서 잘린 패킷은 제외
    syncs = [(t0, p) for (t0, p) in syncs if t0 + N_SYMBOLS * period + tone_n <= len(x)]

    # --- (3) 각 동기 펄스 기준 정확히 40ms 간격으로 DTMF 38글자 샘플링 ---
    # t0는 30ms 동기음 안의 20ms 분석 창 위치이므로 k번째 데이터 창은 t0 + k*40ms
    if syncs:
        starts = np.concatenate([
            t0 + period * np.arange(1, N_SYMBOLS + 1) for (t0, _) in syncs
        ])
        PW = goertzel_power(x, starts, tone_n, ROW_FREQS + COL_FREQS, fs, hann)
        row_p = PW[:4].reshape(4, len(syncs), N_SYMBOLS)
        col_p = PW[4:].reshape(4, len(syncs), N_SYMBOLS)

    packets = []     # (seq, data_bytes, crc_ok)
    pkt_info = []    # GUI/워터폴 표시용: 패킷별 시각·번호·CRC 결과
    n_ok = 0
    for i, (t0, p_sync) in enumerate(syncs):
        tone_floor = TONE_FLOOR_REL * p_sync      # 동기 펄스 세기를 기준으로 한 '톤 존재' 최소 에너지
        chars = []
        for k in range(N_SYMBOLS):
            rp, cp = row_p[:, i, k], col_p[:, i, k]
            r, c = int(np.argmax(rp)), int(np.argmax(cp))
            if rp[r] < tone_floor or cp[c] < tone_floor:
                chars.append('?')                  # 톤이 없거나 너무 약함 (소실)
            else:
                chars.append(KEY_TO_HEX[KEYPAD[r][c]])
        pkt = ''.join(chars)

        # --- (4) CRC-16 검증 ---
        crc_ok, seq, data = False, -1, b''
        if '?' not in pkt:
            seq = int(pkt[0:2], 16)
            data = bytes.fromhex(pkt[2:34])
            rx_crc = int(pkt[34:38], 16)
            calc_crc = crc16_ccitt(bytes([seq]) + data)
            crc_ok = (rx_crc == calc_crc)
        n_ok += crc_ok
        packets.append((seq, data, crc_ok))
        pkt_info.append({'t': t0 / fs, 'seq': seq if crc_ok else -1,
                         'ok': bool(crc_ok), 'raw': pkt})

    # --- (5) 패킷 재조립 (패킷번호 unwrap 포함) ---
    asm = PacketAssembler()
    bm = BlockMap()
    for (seq, data, ok), info in zip(packets, pkt_info):     # 도착 순서대로 (조립 + 블록 칸 상태)
        predicted_idx = bm._predict(info['t'])
        idx = asm.add(seq, data) if ok else None
        if ok:
            wire_idx = idx
        elif predicted_idx is not None:
            wire_idx = predicted_idx
        elif bm.anchor is None:
            wire_idx = bm.last_idx + 1
        else:
            wire_idx = 0
        info['wire_idx'] = wire_idx
        bm.on_packet(info['t'], ok, idx)
    total_blocks = asm.expected_packets() or ((max(asm.good) + 1) if asm.good else None)
    bm.set_total(total_blocks)
    bm.mark_unseen_failed()                                  # 끝까지 못 받은 칸 = 실패(빨강)

    fec_status = asm.status()
    fec_meta = asm._fec_meta()
    for info in pkt_info:
        role, logical_idx = classify_fec_wire_index(info.get('wire_idx'), fec_meta)
        info['fec_role'] = role
        info['logical_idx'] = logical_idx
        info['fec_mode'] = fec_meta['mode'] if fec_meta else 0
        info['fec_internal'] = bool(fec_meta is not None and role != 'data')
    stats = {
        'packets_detected': len(syncs),
        'packets_crc_ok': n_ok,
        'packets_crc_error': len(syncs) - n_ok,
        'packets_missing': [],
        'packets': pkt_info,          # [{'t': 초, 'seq': 번호, 'ok': CRC결과, 'raw': 수신문자열}, ...]
        'message': None,              # {'kind','name','container','truncated'}
        'blocks': {'states': bm.states_list(), 'total': bm.total},   # 블록 칸 상태 (0 대기/1 OK/2 실패/3 복원)
        'fec_mode': fec_status.get('fec_mode', 0),
        'fec_percent': fec_status.get('fec_percent', 0),
        'fec_recovered': fec_status.get('fec_recovered', 0),
        'fec_recovered_indices': fec_status.get('fec_recovered_indices', []),
        'fec_header_recovered': fec_status.get('fec_header_recovered', False),
        'fec_data_blocks': fec_status.get('fec_data_blocks', 0),
        'fec_group_k': fec_status.get('fec_group_k', FEC_GROUP_K),
        'fec_parity_count': fec_status.get('fec_parity_count', 0),
        'fec_resolved_count': fec_status.get('fec_resolved_count', 0),
    }

    if asm.count == 0:
        if verbose:
            print("[수신] 복원 가능한 패킷이 없습니다.")
        return b'', stats

    joined, missing, _expected = asm.assemble()
    msg = extract_message(joined, missing)
    stats['packets_missing'] = missing
    stats['message'] = {k: msg.get(k) for k in ('kind', 'name', 'container', 'truncated',
                                               'codec', 'raw_size', 'stored_size')}

    if verbose:
        visible_pk = [p for p in pkt_info if not p.get('fec_internal', False)]
        visible_ok = sum(bool(p['ok']) for p in visible_pk)
        visible_errors = len(visible_pk) - visible_ok
        print(f"[수신] 데이터 블록 감지 {len(visible_pk)}개 / CRC 정상 {visible_ok}개 / "
              f"CRC 오류 {visible_errors}개 / 데이터 누락 {len(missing)}개")
        for packet_info in visible_pk:
            t_sec = packet_info['t']
            index_label = (packet_info.get('logical_idx') if packet_info.get('fec_mode')
                           else packet_info.get('seq', -1))
            if packet_info['ok']:
                print(f"  데이터 블록 #{index_label} @ {t_sec:7.3f}s : CRC OK")
            else:
                print(f"  데이터 블록 #{index_label} @ {t_sec:7.3f}s : CRC 오류")
        if msg['container']:
            print(f"       메시지 종류: {KIND_LABEL.get(msg['kind'], '?')}"
                  f"{' / 파일명: ' + msg['name'] if msg['name'] else ''}")
            if msg['codec']:
                print(f"       압축: {CODEC_LABEL.get(msg['codec'], '?')}  "
                      f"{msg['stored_size']}바이트 -> 원본 {msg['raw_size']}바이트")
        if missing:
            shown = missing[:20]
            print(f"       누락된 데이터 블록 번호: {shown}{' ...' if len(missing) > 20 else ''}")
        if msg['truncated'] and not missing:
            print("[경고] 패딩 표식(0x80)을 찾지 못했습니다. 마지막 패킷이 손실됐을 수 있습니다.")
    return msg['data'], stats


def decode_wav_to_file(wav_path: str, out_path: str, verbose: bool = True):
    """.wav 를 디코딩해 파일로 저장한다."""
    data, stats = decode_wav_to_bytes(wav_path, verbose)
    with open(out_path, 'wb') as f:
        f.write(data)
    return stats


