#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PyDTMF-ECC — DTMF Audio File Transfer
==============================================================
PyDTMF-ECC core and live audio modules.
The application uses NumPy, SciPy, Tkinter, and sounddevice for audio input/output.

[📤 송신]       텍스트 또는 파일 -> [📤 송신] 버튼을 누르면 '바로' 송신 (WAV 저장 불필요)
                 긴 시작음 · 반복 전송 · 누락 블록 재전송
[📡 실시간 수신] 마이크로 자동으로 듣고 즉시 디코딩 — 롤링 워터폴, 블록 칸(EasyPal 스타일:
                 회색 대기 / 초록 성공 / 빨강 실패 / 재전송으로 복원), 글자·파일 도착 표시
[📂 파일 디코딩] 저장된 WAV 를 한 번에 디코딩 (블록 칸 포함)

Application: PyDTMF-ECC
"""

import math
import os
import queue
import re
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audio_file_transfer as core        # numpy/scipy 가 없으면 여기서 자동 설치
import audio_file_transfer_live as live   # sounddevice 도 (선택) 자동 설치

import numpy as np
from scipy.io import wavfile
from scipy.signal import spectrogram

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
except ImportError:                       # tkinter 는 pip 로 설치할 수 없어서 안내만 한다
    print("[오류] tkinter 가 설치되어 있지 않습니다. 운영체제에 맞게 설치해 주세요:\n"
          "  - Ubuntu/Debian : sudo apt install python3-tk\n"
          "  - Fedora        : sudo dnf install python3-tkinter\n"
          "  - macOS(brew)   : brew install python-tk\n"
          "  - Windows       : 파이썬 설치 프로그램에서 'tcl/tk and IDLE' 항목을 선택해 다시 설치")
    sys.exit(1)


# ----------------------------------------------------------------------------
# 1. 워터폴(스펙트로그램) 계산·렌더링  (GUI 와 분리된 순수 함수)
# ----------------------------------------------------------------------------
F_MIN, F_MAX = 550.0, 2050.0     # 화면에 보여 줄 주파수 범위 (Hz)
WF_HEIGHT = 320                  # 워터폴 이미지 높이 (px)
DYN_RANGE_DB = 40.0              # 색으로 표현할 다이내믹 레인지 (dB)


UI_KO_EN = {
    "SSTV + DTMF 소리 전송기": "PyDTMF-ECC",
    "오디오 입력을 사용할 수 없습니다. Windows 오디오 장치를 확인하세요.": "Audio input is unavailable. Check your Windows audio device.",
    "  📤 송신  ": "  📤 Send  ",
    "  📡 실시간 수신  ": "  📡 Live Receive  ",
    "  📂 파일 디코딩  ": "  📂 Decode File  ",
    "언어": "Language", "전송 속도": "Transfer speed", "복구 모드": "Recovery mode",
    "송·수신 속도는 양쪽에서 같게 설정": "Use the same speed on sender and receiver",
    "속도는 양쪽 동일 · 복구 모드는 자동 감지": "Use the same speed on both devices · recovery mode is detected automatically",
    "느림 (톤 50 / 공백 20ms)": "Slow (tone 50 / gap 20 ms)",
    "보통 (톤 30 / 공백 10ms)": "Normal (tone 30 / gap 10 ms)",
    "빠름 (톤 20 / 공백 10ms)": "Fast (tone 20 / gap 10 ms)",
    "모드 0 · CRC만": "Mode 0 · CRC only",
    "모드 1 · 패리티 10%": "Mode 1 · 10% parity",
    "모드 2 · 패리티 20%": "Mode 2 · 20% parity",
    "모드 3 · 패리티 30%": "Mode 3 · 30% parity",
    "복구 예상": "Recovery estimate",
    "사용자 지정 · 패리티 0~100%": "Custom · 0–100% parity",
    "워터폴 (시간 →, 주파수 ↑)": "Waterfall (time →, frequency ↑)",
    "패킷 번호": "Packet number", "확대/축소": "Zoom",
    "사용자 지정 패리티 (%)": "Custom parity (%)",
    "(아직 표시할 오디오가 없습니다)": "(No audio to display yet)",
    "블록 (데이터 1칸 = 16바이트 · 복구용 패킷은 내부 처리)": "Data blocks (16 bytes each; recovery packets are hidden)",
    "■ 대기": "■ Waiting", "■ 성공(CRC OK)": "■ Success (CRC OK)",
    "■ 실패": "■ Failed", "▣ 복원됨(FEC/재전송)": "▣ Recovered (FEC/retransmission)",
    "1. 보낼 내용": "1. Content to send", "텍스트 메시지 (받는 쪽에서 글자로 바로 표시)": "Text message (shown as text on the receiving device)",
    "파일 (원래 파일명·크기도 함께 전송)": "File (includes the original filename and size)",
    "찾아보기...": "Browse...", "2. 보내기": "2. Send",
    "📤  송신  (누르면 바로 전송)": "📤 Send now", "■ 중지": "■ Stop",
    "💾 WAV로 저장...": "💾 Save as WAV...", "긴 시작음(1초)": "Long start tone (1 s)",
    "자동 압축": "Auto compress", "   반복": "   Repeats", "회   |   재전송할 블록": "times | blocks to resend",
    "예) 3,7,12-15  (비우면 전체)": "e.g. 3,7,12-15 (blank = all)",
    "🔊 스피커로 송신 (마이크로 수신)": "🔊 Send via speaker (receive via mic)",
    "송신하면 수신 탭으로 이동": "Switch to receive tab when sending", "출력 장치": "Output device",
    "  (sounddevice 없음 → 스피커 송신은 OS 재생기 사용, 마이크 수신은 불가. ": "  (sounddevice missing: OS player for output; mic input unavailable. ",
    "입력": "Input", "마이크": "Microphone", "🎙 수신 시작": "🎙 Start receiving",
    "⏹ 수신 중지": "⏹ Stop receiving", "실행 시 자동 수신": "Auto-listen on launch",
    "SNR": "SNR", "dB": "dB", "입력 레벨": "Input level",
    "실시간 워터폴 (최근 12초)": "Live waterfall (last 12 seconds)",
    "실패한 블록 번호": "Failed block numbers", "복사": "Copy",
    "➜ 송신 탭 '재전송할 블록'에 채우기": "➜ Fill 'blocks to resend' on Send tab",
    "수신 중인 메시지": "Message being received", "아직 수신된 내용이 없습니다.": "No content received yet.",
    "💾 저장...": "💾 Save...", "수신 로그": "Receive log",
    "1. 받은 소리(WAV) 선택": "1. Select received audio (WAV)",
    "밴드패스(600~2200Hz) 적용 후 스펙트럼 보기": "View spectrum after band-pass (600–2200 Hz)",
    "🔍 디코딩 시작": "🔍 Start decoding", "💾 복원한 내용 저장...": "💾 Save recovered content...",
    "패킷 목록 (클릭하면 워터폴 이동)": "Packet list (click to move waterfall)",
    "번호": "Index", "시각(초)": "Time (s)", "CRC": "CRC",
    "복원 내용 미리보기": "Recovered content preview", "💾 저장...": "💾 Save...",
    "원본 데이터 블록 대비 추가 패리티 비율 · 0%는 CRC만 사용": "Parity overhead as a percentage of data blocks. Set to 0% for CRC-only mode.",
    "송신·수신 장치의 속도는 같게 설정 · 복구 모드는 자동 감지": "Use the same speed on both devices · recovery mode is detected automatically",
    "실시간 워터폴 (최근 12초)": "Live waterfall (last 12 seconds)", "데이터 블록": "Data blocks",
    "dB (+50Hz 험, 시작 오프셋)": "dB (+50 Hz hum, start offset)",
    "밴드패스(600~2200Hz) 적용 후 스펙트럼 보기": "Show the spectrum after applying a 600–2200 Hz band-pass filter",
    "패킷 목록 (클릭하면 워터폴 이동)": "Packet list (select a packet to jump to it in the waterfall)",
    "복원 내용 미리보기": "Recovered content preview", "복사": "Copy",
    "사용자 지정 · 패리티 0~100%": "Custom · 0–100% parity",
    "  (sounddevice 없음 → 스피커 송신은 OS 재생기 사용, 마이크 수신은 불가. pip install sounddevice 권장)":
        "  (sounddevice is not installed. Speaker output can use an external player, but microphone input is unavailable. Install it with: pip install sounddevice)",
    "이미 전송 중입니다. 먼저 ■ 중지하거나 끝날 때까지 기다려 주세요.":
        "A transmission is already in progress. Stop it first or wait for it to finish.",
    "이 환경에서는 자동 재생을 쓸 수 없습니다.": "Automatic playback is not available in this environment.",
    "디코딩할 WAV 파일을 선택해 주세요.": "Please select a WAV file to decode.",
    "실패한 블록이 없습니다.": "There are no failed blocks.",
    "보낼 파일을 선택해 주세요.": "Please select a file to send.",
    "보낼 텍스트를 입력해 주세요.": "Please enter a text message.",
    "새 전송 감지 — 이전 결과 자동 초기화": "New transmission detected — previous results cleared",
    "보낼 파일 선택": "Select a file to send", "WAV 저장 위치": "Save WAV file",
    "수신한 내용 저장": "Save received content", "WAV 선택": "Select a WAV file",
    "WAV 오디오": "WAV audio", "모든 파일": "All files", "복원한 내용 저장": "Save recovered content",
    "압축 꺼짐": "Compression disabled", "압축해도 이득 없음 → 그대로 전송": "Compression would not reduce the size; sending unchanged",
    "일부 패킷 문제": "some packets had errors", "raw(헤더 없음)": "raw (no header)", "알 수 없음": "Unknown",
}

UI_EN_KO = {v: k for k, v in UI_KO_EN.items()}
UI_PHRASES = [
    ("새 전송 감지 — 이전 결과 자동 초기화", "New transmission detected — previous results cleared"),
    ("오류가 발생했습니다", "An error occurred"),
    ("송신 중지", "Send stopped"),
    ("마이크로 실시간 수신 중...", "Live microphone reception..."),
    ("실시간 수신 중...", "Live reception..."),
    ("실패한 블록 번호를 복사했습니다", "Copied failed block numbers"),
    ("재전송할 블록을 채웠습니다 — 보낼 내용을 확인하고 📤 송신을 누르세요", "Filled blocks to resend — review content and press Send"),
    ("수신 중지", "Reception stopped"),
    ("모든 블록 성공", "All blocks successful"),
    ("재전송하면 복원됩니다", "resend the missing blocks to complete the transfer"),
    ("복원할 블록이 없습니다.", "No blocks to recover."),
    ("압축 꺼짐", "compression disabled"),
    ("압축해도 이득 없음 → 그대로 전송", "no compression benefit → send unchanged"),
    ("큰 파일이라 압축 결과는 송신할 때 계산됩니다", "Large file: compression will be calculated when transmission starts"),
    ("수신 내용", "received content"),
    ("송신하면 수신 탭으로 이동", "Switch to receive tab when sending"),
    ("종류:", "Kind:"), ("형식:", "Format:"), ("파일명:", "Filename:"),
    ("크기", "Size"), ("바이트", "bytes"), ("블록", "blocks"),
    ("수신 완료", "Reception complete"), ("수신 종료", "Reception ended"),
    ("완료", "complete"),
    ("성공", "success"), ("실패", "failed"), ("복원", "recovered"), ("복구", "recovered"),
    ("누락", "missing"), ("에러", "error"), ("대기", "waiting"), ("총", "total"),
    ("압축", "compressed"), ("예상", "estimated"), ("원본", "original"),
    ("송신 중", "Sending"), ("송신 완료", "Send complete"),
    ("송신 중지", "Sending stopped"), ("준비됨", "Ready"), ("정지됨", "Stopped"),
    ("마이크로 실시간 수신 중...", "Live microphone reception..."),
    ("실시간 수신 중...", "Live reception..."), ("전송 실패", "Transfer failed"),
    ("동기화 펄스를 찾지 못했습니다.", "No synchronization pulse found."),
    ("디코딩 완료", "Decoding complete"), ("디코딩 실패: 패킷 없음", "Decoding failed: no packets"),
    ("듣는 중 (시작음/동기 펄스 대기)", "Listening for the start tone or a sync pulse"),
    ("블록 수신 중", "Receiving blocks"), ("시작음 감지", "Start tone detected"),
    ("아직 수신된 내용이 없습니다.", "No content received yet."),
    ("워터폴 계산 중...", "Calculating waterfall..."),
    ("속도 설정:", "Speed settings:"), ("톤", "tone"), ("공백", "gap"),
    ("파일", "File"), ("텍스트", "Text"), ("압축 없음", "No compression"),
    ("동기화 펄스", "sync pulse"), ("선택됨", "selected"),
    ("저장 완료:", "Saved:"), ("복구 모드", "Recovery mode"), ("FEC 모드", "FEC mode"),
    ("송신·수신 장치 모두 같은 속도를 사용하세요", "use the same speed on both devices"),
    ("준비됨", "Ready"),
    ("대기 중", "Idle"),
    ("수신 중지", "Reception stopped"),
    ("송신 중지", "Transmission stopped"),
    ("수신 시작", "Reception started"),
    ("파일 디코딩", "File decoding"),
    ("송신 탭", "Send tab"),
    ("수신 탭", "Receive tab"),
    ("실패한 블록 번호를 복사했습니다", "Copied the failed block numbers"),
    ("실패한 블록이 없습니다.", "There are no failed blocks."),
    ("보낼 파일을 선택해 주세요.", "Please select a file to send."),
    ("보낼 텍스트를 입력해 주세요.", "Please enter a text message."),
    ("WAV 저장 위치", "Save WAV file"),
    ("수신한 내용 저장", "Save received content"),
    ("전송 실패", "Transmission failed"),
    ("전송 중", "Transmitting"),
    ("수신 중", "Receiving"),
    ("새 메시지 시작", "New message started"),
    ("일부 누락", "some data is missing"),
    ("누락된 데이터 블록 번호", "Missing data block indices"),
    ("텍스트 수신 완료", "Text message received"),
    ("수신 완료", "Reception complete"),
    ("전송 완료", "Transmission complete"),
    ("확인", "Notice"),
    ("안내", "Notice"),
    ("오류", "Error"),
    ("복구 모드", "Recovery mode"),
    ("사용자 지정", "Custom"),
    ("입력 레벨", "Input level"),
    ("사용자 지정 패리티", "Custom parity"),
    ("바이트", "bytes"),
    ("데이터 블록", "data blocks"),
    ("데이터 누락", "missing data"),
    ("남은 약", "about"),
    ("초 예상", "s estimated"),
    ("수신 종료: 실패", "Reception ended: failed"),
]


def localize_ui_text(text, language):
    """Translate UI labels and status phrases without touching the user's message preview."""
    value = str(text)
    if language == 'en':
        if value in UI_KO_EN:
            return UI_KO_EN[value]
        # Translate frequently updated numeric/status strings as complete sentences so
        # English UI text remains natural instead of being assembled word by word.
        m = re.fullmatch(r"진단 입력 ([+-]?[\d.]+) dBFS \| 1000Hz 최대 진폭 ([\d.]+), 최대 비율 ([\d.]+)x \| ([\d.]+)Hz 최대 진폭 ([\d.]+), 행비 ([\d.]+)x / 잡음비 ([\d.]+)x", value)
        if m:
            level, amp1k, ratio1k, sync_hz, amp_sync, row_ratio, noise_ratio = m.groups()
            return (f"Input diagnostics {level} dBFS | 1000 Hz peak amplitude {amp1k}, peak ratio {ratio1k}x | "
                    f"{sync_hz} Hz peak amplitude {amp_sync}, row ratio {row_ratio}x / noise ratio {noise_ratio}x")
        m = re.fullmatch(r"성공 (\d+) · 복원 (\d+) · 실패 (\d+) · 대기 (\d+)  /  총 (\d+)", value)
        if m:
            ok, recovered, failed, waiting, total = m.groups()
            return f"OK {ok} · Recovered {recovered} · Failed {failed} · Waiting {waiting} / Total {total}"
        m = re.fullmatch(r"속도 설정: 톤 (\d+)ms / 공백 (\d+)ms — 송신·수신 장치 모두 같은 속도를 사용하세요", value)
        if m:
            return f"Speed: {m.group(1)} ms tone / {m.group(2)} ms gap — use the same speed on both devices"
        m = re.fullmatch(r"전송 시작 · 총 ([\d.]+)초 예상", value)
        if m:
            return f"Transmission started · estimated duration: {m.group(1)} s"
        m = re.fullmatch(r"송신 중 (\d+)% · 남은 약 ([\d.]+)초", value)
        if m:
            return f"Sending {m.group(1)}% · about {m.group(2)} s remaining"
        m = re.fullmatch(r"저장됨: 블록 (\d+)개, ([\d.]+)초", value)
        if m:
            return f"Saved: {m.group(1)} packets, {m.group(2)} s"
        m = re.fullmatch(r"수신 종료: 실패 (\d+)블록", value)
        if m:
            return f"Reception ended: {m.group(1)} data blocks failed"
        m = re.fullmatch(r"저장 완료: (.+)", value)
        if m:
            return f"Saved to: {m.group(1)}"
        m = re.fullmatch(r"사용자 지정 패리티 (\d+)% 선택됨", value)
        if m:
            return f"Custom parity set to {m.group(1)}%"
        m = re.fullmatch(r"복구 모드 (\d+) 선택됨", value)
        if m:
            return f"Recovery mode {m.group(1)} selected"
        # Full-line patterns are handled before word substitutions so dynamic labels
        # don't end up as a mixture of English and Korean.
        m = re.fullmatch(r"원본 ([\d,]+)B → 전송 ([\d,]+)B \(([^()]+) 압축, (\d+)%\) · 데이터 블록 (\d+)→(\d+) · 예상 ([\d.]+)초 \(압축 안 하면 ([\d.]+)초\)", value)
        if m:
            original, sent, codec, ratio, raw_blocks, packed_blocks, seconds, raw_seconds = m.groups()
            codec_name = {"zlib": "zlib", "LZMA": "LZMA"}.get(codec, codec)
            return (f"Original: {original} bytes → {sent} bytes transmitted "
                    f"({codec_name}-compressed, {ratio}% of original) · "
                    f"Data blocks: {raw_blocks} → {packed_blocks} · "
                    f"Estimated time: {seconds} s (uncompressed: {raw_seconds} s)")
        m = re.fullmatch(r"원본 ([\d,]+)B · (압축 꺼짐|압축해도 이득 없음 → 그대로 전송) · 데이터 블록 (\d+) · 예상 ([\d.]+)초", value)
        if m:
            original, reason, blocks, seconds = m.groups()
            why = "compression disabled" if reason == "압축 꺼짐" else "compression would not reduce the size; sending unchanged"
            return f"Original: {original} bytes · {why} · Data blocks: {blocks} · Estimated time: {seconds} s"
        m = re.fullmatch(r"원본 ([\d,]+)B — 큰 파일이라 압축 결과는 송신할 때 계산됩니다", value)
        if m:
            return f"Original: {m.group(1)} bytes — compression will be calculated when transmission starts."
        m = re.fullmatch(r"데이터 블록 (\d+)/(\d+) · CRC 정상 (\d+) · 오류 (\d+) · 복원 ([\d,]+)바이트(.*)", value)
        if m:
            resolved, total, ok, errors, size, suffix = m.groups()
            return (f"Data blocks: {resolved}/{total} · CRC OK: {ok} · Errors: {errors} · "
                    f"Recovered data: {size} bytes{suffix}")
        m = re.fullmatch(r"감지 (\d+) · CRC 정상 (\d+) · 에러 (\d+) · 누락 (\d+) · 복원 ([\d,]+)바이트", value)
        if m:
            detected, ok, errors, missing, size = m.groups()
            return (f"Detected packets: {detected} · CRC OK: {ok} · Errors: {errors} · "
                    f"Missing: {missing} · Recovered data: {size} bytes")
        # Translate receive/file metadata while preserving filenames verbatim.
        if value.startswith("종류: "):
            parts = value.split("  ·  ")
            translated = []
            for index, part in enumerate(parts):
                if index == 0:
                    kind = part[len("종류: "):]
                    kind_map = {"텍스트": "Text", "파일": "File", "raw(헤더 없음)": "Raw data (no header)"}
                    translated.append("Kind: " + kind_map.get(kind, kind))
                elif part.startswith("파일명: "):
                    translated.append("Filename: " + part[len("파일명: "):])
                elif part.startswith("크기 "):
                    m_size = re.fullmatch(r"크기 ([\d,]+) 바이트(?: \(([^ ]+) 압축 → ([\d,]+)바이트 전송\))?", part)
                    if m_size:
                        size, codec, stored = m_size.groups()
                        segment = f"Size: {size} bytes"
                        if codec:
                            segment += f" ({codec} compressed → {stored} bytes transmitted)"
                        translated.append(segment)
                    else:
                        translated.append(part.replace("크기 ", "Size: ").replace(" 바이트", " bytes"))
                elif re.fullmatch(r"\d+/\d+ 데이터 블록(?:  ✅ 완료)?", part):
                    m_blocks = re.fullmatch(r"(\d+/\d+) 데이터 블록(  ✅ 완료)?", part)
                    translated.append(m_blocks.group(1) + " data blocks" + ("  ✅ Complete" if m_blocks.group(2) else ""))
                else:
                    m_codec = re.fullmatch(r"([^ ]+) 압축 ([\d,]+)→([\d,]+)바이트", part)
                    if m_codec:
                        codec, packed, original = m_codec.groups()
                        translated.append(f"{codec} compression: {packed} → {original} bytes")
                    elif part == "⚠ 일부 누락":
                        translated.append("⚠ Some data is missing")
                    else:
                        m_fec = re.fullmatch(r"Custom FEC \((\d+)%\) · (\d+) data blocks? recovered", part)
                        if m_fec:
                            pct, count = m_fec.groups()
                            unit = "data block" if count == "1" else "data blocks"
                            translated.append(f"Custom FEC ({pct}%) · {count} {unit} recovered")
                        else:
                            translated.append(part)
            return "  ·  ".join(translated)
        m = re.fullmatch(r"형식: (.+?)  ·  (\d+)블록 수신", value)
        if m:
            fmt, count = m.groups()
            fmt = "Unknown" if fmt == "알 수 없음" else fmt
            return f"Format: {fmt} · {count} packets received"
        m = re.fullmatch(r"\[\s*([\d.]+)s\] 데이터 블록 #(\d+) CRC 실패 ✗(?: · 수신열=(.*))?", value)
        if m:
            seconds, data_idx, raw = m.groups()
            suffix = f" · received sequence={raw}" if raw else ""
            return f"[{float(seconds):6.1f}s] Data block #{data_idx} CRC failed ✗{suffix}"
        m = re.fullmatch(r"\[\s*([\d.]+)s\] (?:데이터 블록 #(\d+) 성공|블록 #(\d+) 성공)(.*)", value)
        if m:
            seconds, data_idx, packet_idx, suffix = m.groups()
            label = f"Data block #{data_idx}" if data_idx else f"Block #{packet_idx}"
            suffix = re.sub(r" \(타이밍 보정 ([+-]?[\d.]+) ms\)", r" (timing adjusted by \1 ms)", suffix)
            suffix = suffix.replace(" (소프트 CRC 복원)", " (soft-decision CRC recovery)")
            return f"[{float(seconds):6.1f}s] {label} received successfully{suffix}"
        m = re.fullmatch(r"\[\s*([\d.]+)s\] 블록 #(\d+) 성공(.*)", value)
        if m:
            seconds, packet_idx, suffix = m.groups()
            return f"[{float(seconds):6.1f}s] Packet #{packet_idx} received successfully{suffix}"
        m = re.fullmatch(r"성공 (\d+) · 복원 (\d+) · 실패 (\d+) · 대기 (\d+)  /  총 (\d+)", value)
        if m:
            return value
        for ko, en in sorted(UI_PHRASES, key=lambda x: len(x[0]), reverse=True):
            value = value.replace(ko, en)
        return value
    if value in UI_EN_KO:
        return UI_EN_KO[value]
    m = re.fullmatch(r"Custom parity set to (\d+)%", value)
    if m:
        return f"사용자 지정 패리티 {m.group(1)}% 선택됨"
    m = re.fullmatch(r"Recovery mode (\d+) selected", value)
    if m:
        return f"복구 모드 {m.group(1)} 선택됨"
    # Round-trip the richer dynamic English status lines when the user switches back to Korean.
    m = re.fullmatch(r"Input diagnostics ([+-]?[\d.]+) dBFS \| 1000 Hz peak amplitude ([\d.]+), peak ratio ([\d.]+)x \| ([\d.]+) Hz peak amplitude ([\d.]+), row ratio ([\d.]+)x / noise ratio ([\d.]+)x", value)
    if m:
        level, amp1k, ratio1k, sync_hz, amp_sync, row_ratio, noise_ratio = m.groups()
        return (f"진단 입력 {level} dBFS | 1000Hz 최대 진폭 {amp1k}, 최대 비율 {ratio1k}x | "
                f"{sync_hz}Hz 최대 진폭 {amp_sync}, 행비 {row_ratio}x / 잡음비 {noise_ratio}x")
    m = re.fullmatch(r"Original: ([\d,]+) bytes → ([\d,]+) bytes transmitted \(([^,]+)-compressed, (\d+)% of original\) · Data blocks: (\d+) → (\d+) · Estimated time: ([\d.]+) s \(uncompressed: ([\d.]+) s\)", value)
    if m:
        original, sent, codec, pct, blocks0, blocks1, sec, sec0 = m.groups()
        return f"원본 {original}B → 전송 {sent}B ({codec} 압축, {pct}%) · 데이터 블록 {blocks0}→{blocks1} · 예상 {sec}초 (압축 안 하면 {sec0}초)"
    m = re.fullmatch(r"Original: ([\d,]+) bytes · (compression disabled|compression would not reduce the size; sending unchanged) · Data blocks: (\d+) · Estimated time: ([\d.]+) s", value)
    if m:
        original, reason, blocks, sec = m.groups()
        why = "압축 꺼짐" if reason == "compression disabled" else "압축해도 이득 없음 → 그대로 전송"
        return f"원본 {original}B · {why} · 데이터 블록 {blocks} · 예상 {sec}초"
    m = re.fullmatch(r"Original: ([\d,]+) bytes — compression will be calculated when transmission starts\.", value)
    if m:
        return f"원본 {m.group(1)}B — 큰 파일이라 압축 결과는 송신할 때 계산됩니다"
    m = re.fullmatch(r"Data blocks: (\d+)/(\d+) · CRC OK: (\d+) · Errors: (\d+) · Recovered data: ([\d,]+) bytes(.*)", value)
    if m:
        resolved, total, ok, errors, size, suffix = m.groups()
        suffix = re.sub(r" · Custom FEC \((\d+)%\) · (\d+) data blocks? recovered", r" · 사용자 지정 FEC \1% · 복구 \2 데이터 블록", suffix)
        return f"데이터 블록 {resolved}/{total} · CRC 정상 {ok} · 오류 {errors} · 복원 {size}바이트{suffix}"
    m = re.fullmatch(r"Detected packets: (\d+) · CRC OK: (\d+) · Errors: (\d+) · Missing: (\d+) · Recovered data: ([\d,]+) bytes", value)
    if m:
        detected, ok, errors, missing, size = m.groups()
        return f"감지 {detected} · CRC 정상 {ok} · 에러 {errors} · 누락 {missing} · 복원 {size}바이트"
    if value.startswith("Kind: "):
        parts = value.split("  ·  ")
        translated = []
        for index, part in enumerate(parts):
            if index == 0:
                kind = part[len("Kind: "):]
                kind_map = {"Text": "텍스트", "File": "파일", "Raw data (no header)": "raw(헤더 없음)"}
                translated.append("종류: " + kind_map.get(kind, kind))
            elif part.startswith("Filename: "):
                translated.append("파일명: " + part[len("Filename: "):])
            elif part.startswith("Size: "):
                m_size = re.fullmatch(r"Size: ([\d,]+) bytes(?: \(([^ ]+) compressed → ([\d,]+) bytes transmitted\))?", part)
                if m_size:
                    size, codec, stored = m_size.groups()
                    segment = f"크기 {size} 바이트"
                    if codec:
                        segment += f" ({codec} 압축 → {stored}바이트 전송)"
                    translated.append(segment)
                else:
                    translated.append(part.replace("Size: ", "크기 ").replace(" bytes", " 바이트"))
            elif re.fullmatch(r"\d+/\d+ data blocks(?:  ✅ Complete)?", part):
                m_blocks = re.fullmatch(r"(\d+/\d+) data blocks(  ✅ Complete)?", part)
                translated.append(m_blocks.group(1) + " 데이터 블록" + ("  ✅ 완료" if m_blocks.group(2) else ""))
            else:
                m_codec = re.fullmatch(r"([^ ]+) compression: ([\d,]+) → ([\d,]+) bytes", part)
                if m_codec:
                    codec, packed, original = m_codec.groups()
                    translated.append(f"{codec} 압축 {packed}→{original}바이트")
                elif part == "⚠ Some data is missing":
                    translated.append("⚠ 일부 누락")
                else:
                    m_fec = re.fullmatch(r"Custom FEC \((\d+)%\) · (\d+) data blocks? recovered", part)
                    if m_fec:
                        pct, count = m_fec.groups()
                        translated.append(f"사용자 지정 FEC {pct}% · 복구 {count} 데이터 블록")
                    else:
                        translated.append(part)
        return "  ·  ".join(translated)
    m = re.fullmatch(r"Format: (.+?) · (\d+) packets received", value)
    if m:
        fmt, count = m.groups()
        return f"형식: {'알 수 없음' if fmt == 'Unknown' else fmt}  ·  {count}블록 수신"
    m = re.fullmatch(r"\[\s*([\d.]+)s\] Data block #(\d+) CRC failed ✗(?: · received sequence=(.*))?", value)
    if m:
        seconds, data_idx, raw = m.groups()
        suffix = f" · 수신열={raw}" if raw else ""
        return f"[{float(seconds):6.1f}s] 데이터 블록 #{data_idx} CRC 실패 ✗{suffix}"
    m = re.fullmatch(r"\[\s*([\d.]+)s\] Data block #(\d+) received successfully(.*)", value)
    if m:
        seconds, idx, suffix = m.groups()
        suffix = re.sub(r" \(timing adjusted by ([+-]?[\d.]+) ms\)", r" (타이밍 보정 \1 ms)", suffix)
        suffix = suffix.replace(" (soft-decision CRC recovery)", " (소프트 CRC 복원)")
        return f"[{float(seconds):6.1f}s] 데이터 블록 #{idx} 성공{suffix}"
    m = re.fullmatch(r"Saved to: (.+)", value)
    if m:
        return f"저장 완료: {m.group(1)}"
    m = re.fullmatch(r"OK (\d+) · Recovered (\d+) · Failed (\d+) · Waiting (\d+) / Total (\d+)", value)
    if m:
        ok, recovered, failed, waiting, total = m.groups()
        return f"성공 {ok} · 복원 {recovered} · 실패 {failed} · 대기 {waiting}  /  총 {total}"
    m = re.fullmatch(r"Speed: (\d+) ms tone / (\d+) ms gap — use the same speed on both devices", value)
    if m:
        return f"속도 설정: 톤 {m.group(1)}ms / 공백 {m.group(2)}ms — 송신·수신 장치 모두 같은 속도를 사용하세요"
    m = re.fullmatch(r"Transmission started · estimated duration: ([\d.]+) s", value)
    if m:
        return f"전송 시작 · 총 {m.group(1)}초 예상"
    m = re.fullmatch(r"Sending (\d+)% · about ([\d.]+) s remaining", value)
    if m:
        return f"송신 중 {m.group(1)}% · 남은 약 {m.group(2)}초"
    m = re.fullmatch(r"Reception ended: (\d+) data blocks failed", value)
    if m:
        return f"수신 종료: 실패 {m.group(1)}블록"
    for ko, en in sorted(UI_PHRASES, key=lambda x: len(x[1]), reverse=True):
        value = value.replace(en, ko)
    return value


def load_wav_mono(path):
    fs, raw = wavfile.read(path)
    x = raw.astype(np.float64)
    if x.ndim == 2:
        x = x.mean(axis=1)
    if np.issubdtype(raw.dtype, np.integer):
        x /= np.iinfo(raw.dtype).max + 1.0
    return x, fs


def compute_spectrogram(x, fs, hi_floor=None):
    """
    STFT 스펙트로그램(dB). 긴 오디오는 hop 을 키워 메모리를 아낀다.
    hi_floor: 색 상단 기준의 하한 (실시간 표시에서 무음일 때 잡음이 밝게 보이는 것을 방지)
    """
    duration = len(x) / fs
    hop = 256 if duration <= 30 else (512 if duration <= 120 else 1024)
    nperseg = 1024
    f, t, S = spectrogram(x, fs=fs, window='hann', nperseg=nperseg,
                          noverlap=nperseg - hop, nfft=2048, mode='psd')
    sel = (f >= F_MIN - 60) & (f <= F_MAX + 60)
    db = 10.0 * np.log10(S[sel] + 1e-14)
    dt = float(t[1] - t[0]) if len(t) > 1 else hop / fs
    hi = float(np.percentile(db, 99.5))
    if hi_floor is not None:
        hi = max(hi, hi_floor)
    return {'db': db, 'f': f[sel], 't': t, 'dt': dt, 'duration': duration, 'hi': hi, 't_offset': 0.0}


_HI_FLOOR_CACHE = {}


def live_hi_floor(fs):
    """진폭 0.003(약 -50dBFS) 사인파가 만드는 스펙트로그램 상단값 = 실시간 표시의 최소 기준."""
    if fs not in _HI_FLOOR_CACHE:
        t = np.arange(fs) / fs
        _HI_FLOOR_CACHE[fs] = compute_spectrogram(0.003 * np.sin(2 * np.pi * 1000 * t), fs)['hi']
    return _HI_FLOOR_CACHE[fs]


class LiveWaterfallBuffer:
    """Incremental spectrogram builder for live audio.

    Only the newest audio samples are transformed. A 1024-sample Hann window
    with 256-sample hops gives about 16 ms time resolution at 16 kHz, while
    retaining a rolling history for the UI.
    """
    NPERSEG = 1024
    HOP = 256
    NFFT = 2048

    def __init__(self, fs, window_s=12.0):
        self.fs = int(fs)
        self.window_s = float(window_s)
        self.reset(0)

    def reset(self, start_sample=0):
        self.buffer = np.empty(0, dtype=np.float64)
        self.buffer_start_sample = int(start_sample)
        self.db = None
        self.times = np.empty(0, dtype=np.float64)
        self.f = None

    def append(self, samples, start_sample):
        samples = np.asarray(samples, dtype=np.float64).reshape(-1)
        start_sample = int(start_sample)
        if samples.size == 0:
            return

        expected_start = self.buffer_start_sample + len(self.buffer)
        if start_sample != expected_start:
            # If the UI fell behind the audio ring buffer, restart at the
            # first available sample instead of drawing misaligned columns.
            self.reset(start_sample)
        if self.buffer.size:
            self.buffer = np.concatenate((self.buffer, samples))
        else:
            self.buffer = samples.copy()
            self.buffer_start_sample = start_sample

        if len(self.buffer) < self.NPERSEG:
            return

        f_all, t_local, power = spectrogram(
            self.buffer, fs=self.fs, window='hann', nperseg=self.NPERSEG,
            noverlap=self.NPERSEG - self.HOP, nfft=self.NFFT,
            detrend=False, mode='psd'
        )
        if t_local.size == 0:
            return

        select = (f_all >= F_MIN - 60) & (f_all <= F_MAX + 60)
        f = f_all[select]
        db_new = 10.0 * np.log10(power[select] + 1e-14)
        time_new = self.buffer_start_sample / self.fs + t_local
        if self.db is None:
            self.f = f
            self.db = db_new
            self.times = time_new
        else:
            self.db = np.concatenate((self.db, db_new), axis=1)
            self.times = np.concatenate((self.times, time_new))

        # Consume one hop per computed frame; keep the overlap for the next call.
        consumed = int(t_local.size * self.HOP)
        self.buffer = self.buffer[consumed:].copy()
        self.buffer_start_sample += consumed

        end_time = (start_sample + len(samples)) / self.fs
        cutoff = end_time - self.window_s - 0.25
        keep = self.times >= cutoff
        if not np.all(keep):
            self.times = self.times[keep]
            self.db = self.db[:, keep]

    def make_spec(self, end_sample, hi_floor=None):
        if self.db is None or self.times.size == 0:
            return None
        end_time = max(0.0, int(end_sample) / self.fs)
        window_start = max(0.0, end_time - self.window_s)
        mask = self.times >= window_start
        if not np.any(mask):
            return None
        db = self.db[:, mask]
        times = self.times[mask]
        hi = float(np.percentile(db, 99.5))
        if hi_floor is not None:
            hi = max(hi, float(hi_floor))
        return {
            'db': db,
            'f': self.f,
            't': times - window_start,
            'dt': self.HOP / self.fs,
            'duration': max(1.0 / self.fs, end_time - window_start),
            'hi': hi,
            't_offset': window_start,
        }


def _make_lut():
    """검정 -> 남색 -> 하늘색 -> 노랑 -> 빨강"""
    pos = [0.0, 0.25, 0.5, 0.75, 1.0]
    r, g, b = [0, 10, 0, 255, 255], [0, 10, 200, 240, 50], [0, 110, 255, 0, 0]
    xs = np.linspace(0, 1, 256)
    return np.stack([np.interp(xs, pos, c) for c in (r, g, b)], axis=1).astype(np.uint8)


_LUT = _make_lut()


def render_waterfall_rgb(spec, pps, height=WF_HEIGHT):
    """스펙트로그램 -> (height, width, 3) RGB. 축소 시 시간 방향 최대값 풀링으로 짧은 톤 보존."""
    db, f, t, dt = spec['db'], spec['f'], spec['t'], spec['dt']
    width = max(1, int(spec['duration'] * pps))
    freq_y = F_MAX - (np.arange(height) + 0.5) / height * (F_MAX - F_MIN)
    rows = np.clip(np.searchsorted(f, freq_y), 0, len(f) - 1)
    rows_db = db[rows]
    n_t = rows_db.shape[1]
    edges = np.clip(np.round((np.arange(width) / pps - t[0]) / dt).astype(int), 0, n_t - 1)
    if pps * dt >= 1.0:
        img = rows_db[:, edges]
    else:
        img = np.maximum.reduceat(rows_db, edges, axis=1)
    lo = spec['hi'] - DYN_RANGE_DB
    v = np.clip((img - lo) / DYN_RANGE_DB, 0.0, 1.0)
    return _LUT[(v * 255).astype(np.uint8)]


def rgb_to_ppm(rgb):
    h, w, _ = rgb.shape
    return b'P6 %d %d 255\n' % (w, h) + rgb.tobytes()


# ----------------------------------------------------------------------------
# 2. 워터폴 위젯 (정지 화면 / 재생 커서 / 실시간 롤링)
# ----------------------------------------------------------------------------
class WaterfallView(ttk.Frame):
    TOP, BOT, AXIS_W = 18, 22, 100
    GUIDES = [(697, "697"), (770, "770"), (852, "852"), (941, "941"),
              (1209, "1209"), (1850, "1850 · 동기"), (1336, "1336"), (1477, "1477"), (1633, "1633")]
    MAX_W = 16000

    def __init__(self, master, with_zoom=True, height=WF_HEIGHT):
        super().__init__(master)
        self.h = height
        self.language = 'en'
        self.spec = None
        self.markers = []
        self.pps = 100.0
        self.live = False
        self.cursor_t = None
        self._cursor_id = None
        self._photo = None
        self._width = 1
        self._live_static_ready = False
        self._live_image_id = None
        self._live_canvas_width = 0
        self._live_marker_signature = None
        self._live_marker_items = []
        total_h = self.TOP + self.h + self.BOT

        bar = ttk.Frame(self)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 2))
        self.title_lbl = ttk.Label(bar, text="워터폴 (시간 →, 주파수 ↑)")
        self.title_lbl.pack(side="left")
        if with_zoom:
            ttk.Button(bar, text="＋", width=3, command=lambda: self.zoom(2.0)).pack(side="right")
            self.zoom_lbl = ttk.Label(bar, text="", width=11, anchor="center")
            self.zoom_lbl.pack(side="right")
            ttk.Button(bar, text="－", width=3, command=lambda: self.zoom(0.5)).pack(side="right")
            ttk.Label(bar, text="확대/축소").pack(side="right", padx=(0, 6))
        else:
            self.zoom_lbl = ttk.Label(bar, text="")
            self.zoom_lbl.pack(side="right")

        self.axis = tk.Canvas(self, width=self.AXIS_W, height=total_h, bg="#111111", highlightthickness=0)
        self.axis.grid(row=1, column=0)
        self.canvas = tk.Canvas(self, height=total_h, bg="#000000", highlightthickness=0)
        self.canvas.grid(row=1, column=1, sticky="ew")
        self.hbar = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
        self.hbar.grid(row=2, column=1, sticky="ew")
        self.canvas.configure(xscrollcommand=self.hbar.set)
        self.columnconfigure(1, weight=1)

        self.canvas.bind("<MouseWheel>", lambda e: self.canvas.xview_scroll(-int(e.delta / 120) * 4, "units"))
        self.canvas.bind("<Button-4>", lambda e: self.canvas.xview_scroll(-4, "units"))
        self.canvas.bind("<Button-5>", lambda e: self.canvas.xview_scroll(4, "units"))

        self._draw_axis()
        self._placeholder = self.canvas.create_text(12, total_h // 2, anchor="w", fill="#777777",
                                                    text="(아직 표시할 오디오가 없습니다)")

    # --- 외부 API ---
    def set_language(self, language):
        """Translate text rendered directly on Canvas (not ordinary Tk widgets)."""
        new_language = 'en' if language == 'en' else 'ko'
        if getattr(self, 'language', 'ko') == new_language:
            return
        self.language = new_language
        self._live_static_ready = False
        self._draw_axis()
        if self.spec is None and getattr(self, '_placeholder', None) is not None:
            self.canvas.itemconfig(
                self._placeholder,
                text='(No audio to display yet)' if self.language == 'en'
                     else '(아직 표시할 오디오가 없습니다)')

    def load(self, spec, markers=None):
        """정지 화면으로 표시 (전체 길이, 확대/축소·스크롤 가능)"""
        self.live = False
        self.spec, self.markers = spec, markers or []
        self.pps = float(min(150.0, self.MAX_W / max(spec['duration'], 0.1)))
        self.render()

    def show_live(self, spec, markers, window_s, now_s=None):
        """Update a rolling live waterfall without rebuilding its whole canvas."""
        self.live = True
        self.spec, self.markers = spec, markers or []
        canvas_w = max(self.canvas.winfo_width(), 600) - 4
        canvas_w = max(canvas_w, 1)
        self.pps = canvas_w / max(float(window_s), 0.1)
        rgb = render_waterfall_rgb(spec, self.pps, self.h)
        self._photo = tk.PhotoImage(data=rgb_to_ppm(rgb), format="PPM")
        self._width = canvas_w
        off = float(spec.get('t_offset', 0.0))
        duration = float(spec.get('duration', 0.0))
        image_x = max(0, canvas_w - int(rgb.shape[1]))
        if now_s is None:
            now_s = off + duration

        c = self.canvas
        if (not self._live_static_ready) or self._live_canvas_width != canvas_w:
            c.delete("all")
            self._live_marker_items = []
            self._live_marker_signature = None
            self._live_canvas_width = canvas_w
            self._live_image_id = c.create_image(image_x, self.TOP, anchor="nw", image=self._photo)
            y0 = self.TOP + self.h

            # Static frequency guides. The image itself is replaced in place below.
            for f, _ in self.GUIDES:
                y = self._y(f)
                c.create_line(0, y, canvas_w, y, fill="#444444", dash=(2, 8), tags=("live_grid",))

            # Relative time ruler stays fixed while the newest audio enters at the right.
            sections = 6
            for i in range(sections + 1):
                x = int(round(canvas_w * i / sections))
                c.create_line(x, y0, x, y0 + 5, fill="#aaaaaa", tags=("live_grid",))
                age = float(window_s) * (sections - i) / sections
                if i == sections:
                    label = "Now" if self.language == 'en' else "지금"
                else:
                    label = f"-{age:g}s"
                anchor = "ne" if i == sections else ("nw" if i == 0 else "n")
                c.create_text(x, y0 + 6, anchor=anchor, fill="#aaaaaa", font=("TkDefaultFont", 8),
                              text=label, tags=("live_grid",))
            self._live_static_ready = True
            c.configure(scrollregion=(0, 0, canvas_w, self.TOP + self.h + self.BOT))
        else:
            c.coords(self._live_image_id, image_x, self.TOP)
            c.itemconfigure(self._live_image_id, image=self._photo)

        signature = tuple((round(float(m.get('t', 0.0)), 3), bool(m.get('ok')), str(m.get('label', '')))
                          for m in self.markers)
        if signature != self._live_marker_signature:
            c.delete("live_marker")
            self._live_marker_items = []
            y0 = self.TOP + self.h
            for m in self.markers:
                colour = "#44ff66" if m.get('ok') else "#ff4444"
                line_id = c.create_line(0, self.TOP, 0, y0, fill=colour, tags=("live_marker",))
                label_id = None
                if self.pps >= 12 or not m.get('ok'):
                    label_id = c.create_text(0, 1, anchor="nw", fill=colour, font=("TkDefaultFont", 8),
                                             text=str(m.get('label', '')), tags=("live_marker",))
                self._live_marker_items.append((m, line_id, label_id))
            self._live_marker_signature = signature

        # Move existing marker objects; don't recreate the image, axes, or tick labels each frame.
        y0 = self.TOP + self.h
        for m, line_id, label_id in self._live_marker_items:
            x = int(round(canvas_w - (float(now_s) - float(m.get('t', 0.0))) * self.pps))
            if x < -20 or x > canvas_w + 20:
                x = -100
            c.coords(line_id, x, self.TOP, x, y0)
            if label_id is not None:
                c.coords(label_id, x + 2, 1)
        c.tag_raise("live_grid")
        c.tag_raise("live_marker")
        self.zoom_lbl.config(text="" if self.live else f"{self.pps:.0f} px/s")

    def zoom(self, factor):
        if self.spec is None or self.live:
            return
        hi = max(4.0, min(800.0, self.MAX_W / max(self.spec['duration'], 0.1)))
        self.pps = float(min(max(self.pps * factor, 4.0), hi))
        self.render()

    def scroll_to(self, t_sec):
        if self.spec is None:
            return
        x = (t_sec - self.spec.get('t_offset', 0.0)) * self.pps
        self.canvas.xview_moveto(max(0.0, (x - self.canvas.winfo_width() / 3) / max(self._width, 1)))

    def set_cursor(self, t_sec):
        """재생 위치 커서(흰 세로선). None 이면 제거. 화면 밖이면 자동 스크롤."""
        self.cursor_t = t_sec
        self._draw_cursor(follow=True)

    # --- 내부 ---
    def _y(self, f):
        return self.TOP + (F_MAX - f) / (F_MAX - F_MIN) * self.h

    def _draw_axis(self):
        a = self.axis
        a.delete("all")
        for f, label in self.GUIDES:
            shown = label
            if self.language == 'en':
                shown = shown.replace('동기', 'sync')
            else:
                shown = shown.replace('sync', '동기')
            a.create_text(self.AXIS_W - 4, self._y(f), anchor="e",
                          fill="#ff7777" if f == 1209 else "#bbbbbb",
                          text=shown if f == 1209 else shown + " Hz", font=("TkDefaultFont", 8))
        a.create_text(self.AXIS_W - 4, self.TOP // 2, anchor="e", fill="#888888",
                      text="Packet number" if self.language == 'en' else "패킷 번호",
                      font=("TkDefaultFont", 8))

    def _draw_cursor(self, follow=False):
        c = self.canvas
        if self._cursor_id is not None:
            c.delete(self._cursor_id)
            self._cursor_id = None
        if self.cursor_t is None or self.spec is None:
            return
        x = (self.cursor_t - self.spec.get('t_offset', 0.0)) * self.pps
        if 0 <= x <= self._width:
            self._cursor_id = c.create_line(x, self.TOP, x, self.TOP + self.h, fill="#ffffff", width=2)
            if follow and not self.live:
                l, r = c.xview()
                if not (l * self._width + 20 < x < r * self._width - 20):
                    c.xview_moveto(max(0.0, (x - c.winfo_width() / 3) / self._width))

    def render(self):
        self._live_static_ready = False
        self._live_image_id = None
        self._live_marker_signature = None
        self._live_marker_items = []
        spec = self.spec
        rgb = render_waterfall_rgb(spec, self.pps, self.h)
        self._photo = tk.PhotoImage(data=rgb_to_ppm(rgb), format="PPM")
        self._width = rgb.shape[1]
        off = spec.get('t_offset', 0.0)

        c = self.canvas
        c.delete("all")
        self._cursor_id = None
        c.create_image(0, self.TOP, anchor="nw", image=self._photo)
        c.configure(scrollregion=(0, 0, self._width, self.TOP + self.h + self.BOT))

        for f, _ in self.GUIDES:                                   # DTMF 주파수 가이드선
            y = self._y(f)
            c.create_line(0, y, self._width, y, fill="#444444", dash=(2, 8))

        step = next(s for s in (0.1, 0.2, 0.5, 1, 2, 5, 10, 30, 60, 120, 300) if s * self.pps >= 70)
        y0 = self.TOP + self.h
        k = math.ceil(off / step)
        while k * step <= off + spec['duration']:                  # 시간 눈금 (절대 시각)
            x = (k * step - off) * self.pps
            c.create_line(x, y0, x, y0 + 5, fill="#aaaaaa")
            c.create_text(x + 2, y0 + 6, anchor="nw", fill="#aaaaaa", font=("TkDefaultFont", 8),
                          text=f"{k * step:g}s")
            k += 1

        for m in self.markers:                                     # 패킷(동기 펄스) 마커
            x = (m['t'] - off) * self.pps
            if x < 0 or x > self._width:
                continue
            col = "#44ff66" if m['ok'] else "#ff4444"
            c.create_line(x, self.TOP, x, y0, fill=col)
            if self.pps >= 12 or not m['ok']:
                c.create_text(x + 2, 1, anchor="nw", fill=col, font=("TkDefaultFont", 8), text=m['label'])

        self._draw_cursor()
        self.zoom_lbl.config(text="" if self.live else f"{self.pps:.0f} px/초")


# ----------------------------------------------------------------------------
# 2-1. 블록 칸 (EasyPal 스타일): 패킷 1개 = 칸 1개
# ----------------------------------------------------------------------------
class BlockGrid(ttk.Frame):
    """
    회색=대기 / 초록=CRC 성공 / 빨강=실패(CRC 에러·미수신) / 초록+노란 테두리=실패했다가 재전송으로 복원
    states: core.BlockMap.states_list() 형식 (0 대기, 1 성공, 2 실패, 3 복원)
    """
    COLORS = {0: "#8d949b", 1: "#2ecc71", 2: "#e74c3c", 3: "#2ecc71"}
    BG = "#1b1e22"

    def __init__(self, master, height=110):
        super().__init__(master)
        head = ttk.Frame(self)
        head.pack(fill="x")
        ttk.Label(head, text="블록 (데이터 1칸 = 16바이트 · 복구용 패킷은 내부 처리)").pack(side="left")
        for txt, col in (("■ 대기", "#8d949b"), ("■ 성공(CRC OK)", "#2ecc71"),
                         ("■ 실패", "#e74c3c"), ("▣ 복원됨(FEC/재전송)", "#2ecc71")):
            ttk.Label(head, text=txt, foreground=col).pack(side="left", padx=(10, 0))
        self.count_lbl = ttk.Label(head, text="")
        self.count_lbl.pack(side="right")

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, pady=(2, 0))
        self.canvas = tk.Canvas(body, height=height, bg=self.BG, highlightthickness=0)
        self.vbar = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vbar.pack(side="right", fill="y")
        self.canvas.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(-int(e.delta / 120), "units"))
        self.canvas.bind("<Button-4>", lambda e: self.canvas.yview_scroll(-1, "units"))
        self.canvas.bind("<Button-5>", lambda e: self.canvas.yview_scroll(1, "units"))
        self._geom = None
        self._drawn = []
        self._rects = []

    def clear(self):
        self.update_blocks([])

    def update_blocks(self, states):
        n = len(states)
        w = max(self.canvas.winfo_width(), 300)
        cell = 22 if n <= 300 else (12 if n <= 1500 else 6)
        gap = 3 if cell >= 12 else 1
        cols = max(1, (w - 8) // (cell + gap))
        geom = (n, cell, cols)
        if geom != self._geom:                                   # 칸 수/폭이 바뀌면 다시 그림
            c = self.canvas
            c.delete("all")
            self._rects = []
            for i in range(n):
                x = 4 + (i % cols) * (cell + gap)
                y = 4 + (i // cols) * (cell + gap)
                r = c.create_rectangle(x, y, x + cell, y + cell, fill=self.COLORS[0], outline=self.BG)
                self._rects.append(r)
                if cell >= 20:
                    c.create_text(x + cell / 2, y + cell / 2, text=str(i), fill="#0b0b0b", font=("TkDefaultFont", 7))
            rows = -(-n // cols) if n else 1
            c.configure(scrollregion=(0, 0, w, 8 + rows * (cell + gap)))
            self._drawn = [0] * n
            self._geom = geom
        for i, st in enumerate(states):                          # 바뀐 칸만 색 갱신
            if st != self._drawn[i]:
                if st == 3:
                    self.canvas.itemconfig(self._rects[i], fill=self.COLORS[3], outline="#f1c40f", width=2)
                else:
                    self.canvas.itemconfig(self._rects[i], fill=self.COLORS[st], outline=self.BG, width=1)
                self._drawn[i] = st
        if n:
            cnt = [states.count(k) for k in range(4)]
            self.count_lbl.config(text=f"성공 {cnt[1]} · 복원 {cnt[3]} · 실패 {cnt[2]} · 대기 {cnt[0]}  /  총 {n}")
        else:
            self.count_lbl.config(text="")


# ----------------------------------------------------------------------------
# 3. 공용 도우미
# ----------------------------------------------------------------------------
def fill_preview(widget, data, kind):
    """미리보기 Text 에 내용 표시: 텍스트면 글자 그대로, 아니면 UTF-8 시도 후 16진수 덤프"""
    widget.config(state="normal")
    widget.delete("1.0", "end")
    if data:
        shown = None
        try:
            shown = data.decode("utf-8")
            if kind != core.KIND_TEXT and any(ord(ch) < 9 or (13 < ord(ch) < 32) for ch in shown[:2000]):
                shown = None                     # 제어문자가 섞여 있으면 바이너리로 간주
        except UnicodeDecodeError:
            if kind == core.KIND_TEXT:           # 수신 도중 잘린 글자는 대체문자로 표시
                shown = data.decode("utf-8", errors="replace")
        if shown is not None:
            widget.insert("1.0", shown[:4000])
        else:
            dump = " ".join(f"{b:02X}" for b in data[:192])
            widget.insert("1.0", f"[바이너리 데이터 - 앞 {min(len(data), 192)}바이트(16진수)]\n{dump}")
    widget.config(state="disabled")
    try:
        widget.see("end")
    except Exception:
        pass


def kind_text(kind):
    return core.KIND_LABEL.get(kind, "raw(헤더 없음)")


def data_only_block_states(wire_states, status):
    # Hide FEC metadata/parity packets and show only logical data blocks.
    mode = int(status.get('fec_mode', 0) or 0)
    total = int(status.get('fec_data_blocks', 0) or 0)
    if not mode or total <= 0:
        return list(wire_states)
    group_k = int(status.get('fec_group_k') or core.FEC_GROUP_K)
    parity = int(status.get('fec_parity_count') or core.fec_parity_count(mode))
    recovered = set(status.get('fec_recovered_indices', []))
    out = []
    for logical_idx in range(total):
        group, slot = divmod(logical_idx, group_k)
        wire_idx = core.FEC_HEADER_COPIES + group * (group_k + parity) + slot
        state = wire_states[wire_idx] if wire_idx < len(wire_states) else core.BlockMap.UNKNOWN
        if logical_idx in recovered:
            state = core.BlockMap.RECOVERED
        out.append(state)
    return out


# ----------------------------------------------------------------------------
# 4. 메인 앱
# ----------------------------------------------------------------------------
class App(tk.Tk):
    LIVE_TICK_MS = 250
    LIVE_WATERFALL_TICK_MS = 40

    def __init__(self):
        super().__init__()
        self.title("PyDTMF-ECC")
        # Load the branded icon from the bundled EXE resources or source folder.
        try:
            app_base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
            app_icon = os.path.join(app_base, "pydtmf_ecc.ico")
            if os.path.isfile(app_icon):
                self.iconbitmap(app_icon)
        except Exception:
            pass
        self.geometry("1100x900")
        self.minsize(900, 700)

        self.busy = False
        self.player = live.Player()
        self.tx = live.LiveTransmitter()
        self.session = live.LiveSession()
        self.payload = b""                        # 파일 디코딩 탭 결과
        self.file_msg = None
        self.live_status = None
        self._hi_run = None
        self._live_wf_buffer = LiveWaterfallBuffer(self.session.fs, self.session.RING_S)
        self._wf_cursor_sample = 0
        self._wf_generation = None
        self._wf_auto_clear_count = None
        self._wf_packets = []
        self._job = None                          # 진행 중인 송신 작업 정보
        self._tx_error_shown = False
        self._live_was_running = False
        self._flash_until = 0.0
        self.last_spec = None

        bottom = ttk.Frame(self)
        bottom.pack(side="bottom", fill="x", padx=8, pady=4)
        self.status = tk.StringVar(value="Ready")
        ttk.Label(bottom, textvariable=self.status).pack(side="left")
        self.progress = ttk.Progressbar(bottom, mode="indeterminate", length=140)
        self.progress.pack(side="right")

        # Shared settings: speed must match on sender and receiver; FEC mode is self-described in packets.
        self.lang_var = tk.StringVar(value="English")
        self.speed_key = tk.StringVar(value="normal")
        self.speed_display = tk.StringVar(value="")
        self.fec_mode = tk.IntVar(value=1)
        self.fec_display = tk.StringVar(value="")
        self.fec_percent_var = tk.StringVar(value="25")
        self.topbar = ttk.Frame(self, padding=(8, 5, 8, 2))
        self.topbar.pack(side="top", fill="x")
        self.lang_lbl = ttk.Label(self.topbar, text="언어")
        self.lang_lbl.pack(side="left")
        self.lang_combo = ttk.Combobox(self.topbar, textvariable=self.lang_var,
                                       values=["한국어", "English"], state="readonly", width=9)
        self.lang_combo.pack(side="left", padx=(4, 14))
        self.lang_combo.bind("<<ComboboxSelected>>", self._on_language_change)
        self.speed_lbl = ttk.Label(self.topbar, text="전송 속도")
        self.speed_lbl.pack(side="left")
        self.speed_combo = ttk.Combobox(self.topbar, textvariable=self.speed_display,
                                        state="readonly", width=27)
        self.speed_combo.pack(side="left", padx=(4, 12))
        self.speed_combo.bind("<<ComboboxSelected>>", self._on_speed_change)
        self.fec_lbl = ttk.Label(self.topbar, text="복구 모드")
        self.fec_lbl.pack(side="left")
        self.fec_combo = ttk.Combobox(self.topbar, textvariable=self.fec_display,
                                      state="readonly", width=50)
        self.fec_combo.pack(side="left", padx=4)
        self.fec_combo.bind("<<ComboboxSelected>>", self._on_fec_change)
        self.fec_custom_row = ttk.Frame(self, padding=(8, 0, 8, 2))
        self.fec_custom_row.pack(side="top", fill="x")
        self.fec_percent_lbl = ttk.Label(self.fec_custom_row, text="Custom parity (%)")
        self.fec_percent_lbl.pack(side="left", padx=(0, 2))
        self.fec_percent_spin = ttk.Spinbox(self.fec_custom_row, from_=0, to=100, increment=1,
                                            textvariable=self.fec_percent_var, width=4, wrap=False)
        self.fec_percent_spin.pack(side="left", padx=(0, 8))
        self.fec_percent_spin.bind("<FocusOut>", self._on_fec_percent_change)
        self.fec_percent_spin.bind("<Return>", self._on_fec_percent_change)
        self.fec_percent_var.trace_add("write", self._schedule_estimate)
        self.fec_custom_hint = ttk.Label(self.fec_custom_row,
            text="Parity overhead as a percentage of data blocks. Set to 0% for CRC-only mode.", foreground="#666666")
        self.fec_custom_hint.pack(side="left")
        self.speed_hint = ttk.Label(self.fec_custom_row,
            text="Use the same speed on both devices · recovery mode is detected automatically", foreground="#666666")
        self.speed_hint.pack(side="left", padx=(18, 0))
        self.fec_capacity_row = ttk.Frame(self, padding=(8, 0, 8, 3))
        self.fec_capacity_row.pack(side="top", fill="x")
        self.fec_capacity_var = tk.StringVar(value="Select content to estimate recovery capacity.")
        self.fec_capacity_lbl = ttk.Label(self.fec_capacity_row, textvariable=self.fec_capacity_var,
                                           foreground="#245d8c")
        self.fec_capacity_lbl.pack(side="left", anchor="w")
        self._refresh_setting_choices()

        self.nb = nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=(8, 0))
        self.enc_tab = ttk.Frame(nb, padding=8)
        self.live_tab = ttk.Frame(nb, padding=8)
        self.dec_tab = ttk.Frame(nb, padding=8)
        nb.add(self.enc_tab, text="  📤 송신  ")
        nb.add(self.live_tab, text="  📡 실시간 수신  ")
        nb.add(self.dec_tab, text="  📂 파일 디코딩  ")

        self.in_devs, self.out_devs = live.list_devices()
        self.have_audio = bool(live.HAVE_SD and self.in_devs and self.out_devs)
        self._build_send_tab()
        self._build_live_tab()
        self._build_file_tab()
        self._apply_language()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._tx_tick)
        self.after(self.LIVE_TICK_MS, self._live_tick)
        self.after(self.LIVE_WATERFALL_TICK_MS, self._live_waterfall_tick)
        if self.have_audio and self.auto_listen.get():          # 실시간 수신 자동 시작
            self.after(600, self._auto_start_mic)

    def _walk_widgets(self, root=None):
        root = root or self
        for child in root.winfo_children():
            yield child
            yield from self._walk_widgets(child)

    def _show_dialog(self, method, title, message):
        """Show dialogs in the currently selected UI language."""
        language = 'en' if self.lang_var.get() == 'English' else 'ko'
        if language == 'en':
            titles = {'확인': 'Please check', '안내': 'Notice', '오류': 'Error',
                      '전송 오류': 'Transmission error', '마이크 오류': 'Microphone error'}
            title = titles.get(title, localize_ui_text(title, language))
            message = localize_ui_text(str(message), language)
        fn = getattr(messagebox, method)
        return fn(title, message, parent=self)

    def _speed_label(self, key):
        labels = {
            'slow': ('느림 (톤 50 / 공백 20ms)', 'Slow (tone 50 / gap 20 ms)'),
            'normal': ('보통 (톤 30 / 공백 10ms)', 'Normal (tone 30 / gap 10 ms)'),
            'fast': ('빠름 (톤 20 / 공백 10ms)', 'Fast (tone 20 / gap 10 ms)'),
        }
        return labels[key][1 if self.lang_var.get() == 'English' else 0]

    def _fec_label(self, mode):
        labels = {
            0: ('모드 0 · CRC만', 'Mode 0 · CRC only'),
            1: ('모드 1 · 패리티 10%', 'Mode 1 · 10% parity'),
            2: ('모드 2 · 패리티 20%', 'Mode 2 · 20% parity'),
            3: ('모드 3 · 패리티 30%', 'Mode 3 · 30% parity'),
            4: ('사용자 지정 · 패리티 0~100%', 'Custom · 0–100% parity'),
        }
        return labels[int(mode)][1 if self.lang_var.get() == 'English' else 0]

    def _update_fec_capacity(self, data_blocks=0, approximate=False):
        """Show the total upper-bound of lost data blocks recoverable at the chosen FEC rate."""
        language = 'en' if self.lang_var.get() == 'English' else 'ko'
        mode = int(self.fec_mode.get())
        configured_mode, pct = self._selected_fec_config()
        data_blocks = max(0, int(data_blocks or 0))
        if data_blocks <= 0:
            if language == 'en':
                text = 'Estimated recovery: select or enter content to calculate the maximum recoverable blocks.'
            else:
                text = '복구 예상: 전송할 내용이나 파일을 선택하면 최대 복구 블록 수를 계산합니다.'
        else:
            capacity = core.fec_recovery_capacity(data_blocks, configured_mode, pct)
            max_blocks = int(capacity['max_recoverable_blocks'])
            rate = int(capacity['parity_percent'])
            qualifier = ' (rough estimate)' if approximate else ''
            parity_blocks = int(capacity['parity_blocks'])
            if language == 'en':
                if configured_mode == 0:
                    text = f'Max recovery (ideal): 0/{data_blocks:,} data blocks (CRC only){qualifier}'
                else:
                    text = (f'Max recovery (ideal): up to {max_blocks:,}/{data_blocks:,} data blocks '
                            f'({rate}% parity; {parity_blocks:,} parity blocks; per-group limit){qualifier}')
            else:
                if configured_mode == 0:
                    text = f'최대 복구(이론상): 데이터 {data_blocks:,}개 중 0개(CRC 검사만){qualifier}'
                else:
                    text = (f'최대 복구(이론상): 데이터 {data_blocks:,}개 중 최대 {max_blocks:,}개 '
                            f'({rate}% 패리티 · 복구용 {parity_blocks:,}블록 · 그룹별 한도){qualifier}')
        self.fec_capacity_var.set(text)

    def _selected_fec_config(self):
        """Return the wire mode and custom parity percentage currently selected in the UI."""
        ui_mode = int(self.fec_mode.get())
        if ui_mode != 4:
            return ui_mode, 0
        try:
            pct = max(0, min(100, int(float(self.fec_percent_var.get().strip() or '0'))))
        except (ValueError, tk.TclError):
            pct = 25
        # A custom rate of 0% is exactly the existing CRC-only wire format.
        return (4, pct) if pct > 0 else (0, 0)

    def _refresh_setting_choices(self):
        speed_values = [self._speed_label(k) for k in ('slow', 'normal', 'fast')]
        self.speed_combo.configure(values=speed_values)
        self.speed_display.set(self._speed_label(self.speed_key.get()))
        fec_values = [self._fec_label(k) for k in range(5)]
        self.fec_combo.configure(values=fec_values)
        self.fec_display.set(self._fec_label(self.fec_mode.get()))
        if hasattr(self, 'fec_percent_lbl'):
            language = 'en' if self.lang_var.get() == 'English' else 'ko'
            self.fec_percent_lbl.config(text='Custom parity (%)' if language == 'en' else '사용자 지정 패리티 (%)')
            self.fec_custom_hint.config(text=(
                'Modes 1/2/3 use 10/20/30% parity; Custom allows 0–100%.' if language == 'en'
                else '모드 1/2/3은 패리티 10/20/30% · 사용자 지정은 0~100%'))
            state = 'normal' if int(self.fec_mode.get()) == 4 else 'disabled'
            self.fec_percent_spin.configure(state=state)

    def _on_speed_change(self, _event=None):
        for key in ('slow', 'normal', 'fast'):
            if self.speed_display.get() == self._speed_label(key):
                self.speed_key.set(key)
                break
        cfg = core.set_speed_preset(self.speed_key.get())
        # Reconfigure live decoder timing at the current point; don't force-stop a microphone stream.
        self.session.decoder.set_speed_preset(self.speed_key.get())
        self._schedule_estimate()
        tone, gap = cfg['tone_ms'], cfg['gap_ms']
        self.status.set(f"속도 설정: 톤 {tone}ms / 공백 {gap}ms — 송신·수신 장치 모두 같은 속도를 사용하세요")
        self._apply_language()

    def _on_fec_change(self, _event=None):
        for mode in range(5):
            if self.fec_display.get() == self._fec_label(mode):
                self.fec_mode.set(mode)
                break
        self._refresh_setting_choices()
        self._schedule_estimate()
        mode, pct = self._selected_fec_config()
        if int(self.fec_mode.get()) == 4:
            status_text = f"Custom parity set to {pct}%" if self.lang_var.get() == 'English' else f"사용자 지정 패리티 {pct}% 선택됨"
        elif mode in (1, 2, 3):
            rate = core.fec_percent_for_mode(mode)
            status_text = (f"Recovery mode {mode} selected ({rate}% parity)" if self.lang_var.get() == 'English'
                           else f"복구 모드 {mode} 선택됨 (패리티 {rate}%)")
        else:
            status_text = f"Recovery mode {mode} selected" if self.lang_var.get() == 'English' else f"복구 모드 {mode} 선택됨"
        self.status.set(status_text)
        self._apply_language()

    def _on_fec_percent_change(self, _event=None):
        try:
            pct = int(float(self.fec_percent_var.get().strip() or '0'))
        except (ValueError, tk.TclError):
            pct = 25
        pct = max(0, min(100, pct))
        self.fec_percent_var.set(str(pct))
        self._schedule_estimate()
        if int(self.fec_mode.get()) == 4:
            self.status.set(f"Custom parity set to {pct}%" if self.lang_var.get() == 'English' else f"사용자 지정 패리티 {pct}% 선택됨")

    def _on_language_change(self, _event=None):
        self._refresh_setting_choices()
        self._apply_language()
        self._schedule_estimate()

    def _apply_language(self):
        language = 'en' if self.lang_var.get() == 'English' else 'ko'
        self.title('PyDTMF-ECC' if language == 'en' else 'PyDTMF-ECC · 소리 파일 전송')
        self.lang_lbl.config(text='Language' if language == 'en' else '언어')
        self.speed_lbl.config(text='Transfer speed' if language == 'en' else '전송 속도')
        self.fec_lbl.config(text='Recovery mode' if language == 'en' else '복구 모드')
        self.fec_percent_lbl.config(text='Custom parity (%)' if language == 'en' else '사용자 지정 패리티 (%)')
        self.fec_custom_hint.config(text=('Modes 1/2/3 use 10/20/30% parity; Custom allows 0–100%.' if language == 'en' else '모드 1/2/3은 패리티 10/20/30% · 사용자 지정은 0~100%'))
        if hasattr(self, 'fec_capacity_var'):
            self._update_fec_capacity(getattr(self, '_fec_estimated_data_blocks', 0),
                                      getattr(self, '_fec_estimated_approximate', False))
        self.speed_hint.config(text=('Use the same speed on both devices · recovery mode is detected automatically' if language == 'en'
                                     else '송신·수신 장치의 속도는 같게 설정 · 복구 모드는 자동 감지'))
        # Localize ordinary widget labels and LabelFrame titles. User-entered preview text is untouched.
        for widget in self._walk_widgets():
            if isinstance(widget, WaterfallView):
                widget.set_language(language)
            try:
                txt = widget.cget('text')
                if txt:
                    localized = localize_ui_text(txt, language)
                    if localized != txt:
                        widget.configure(text=localized)
            except Exception:
                pass
        for tab, ko in ((self.enc_tab, '  📤 송신  '), (self.live_tab, '  📡 실시간 수신  '),
                        (self.dec_tab, '  📂 파일 디코딩  ')):
            label = UI_KO_EN[ko] if language == 'en' else ko
            self.nb.tab(tab, text=label)
        # Tk variable-backed status labels are refreshed after each UI tick, so localize them here too.
        for name in ('status', 'size_info', 'tx_info', 'live_state', 'live_info', 'dec_info', 'msg_lbl'):
            var = getattr(self, name, None)
            if var is not None:
                try:
                    old = var.get()
                    new = localize_ui_text(old, language)
                    if old != new:
                        var.set(new)
                except Exception:
                    pass
        if hasattr(self, 'tree'):
            for col, ko, en in (('no', '번호', 'Index'), ('time', '시각(초)', 'Time (s)'), ('state', 'CRC', 'CRC')):
                self.tree.heading(col, text=en if language == 'en' else ko)

    def _on_close(self):
        try:
            self.tx.stop()
            self.session.close()
            self.player.stop()
        finally:
            self.destroy()

    # ---------------- 백그라운드 작업 ----------------
    def run_bg(self, work, done, status="처리 중..."):
        if self.busy:
            return
        self.busy = True
        self.status.set(status)
        self.progress.start(12)
        q = queue.Queue()

        def target():
            try:
                q.put(("ok", work()))
            except Exception as e:           # noqa
                q.put(("err", e))

        threading.Thread(target=target, daemon=True).start()

        def poll():
            try:
                kind, val = q.get_nowait()
            except queue.Empty:
                self.after(80, poll)
                return
            self.progress.stop()
            # ttk 인디eterminate 바는 stop() 후 마지막 초록 블록이 남을 수 있어 값도 초기화한다.
            try:
                self.progress["value"] = 0
            except Exception:
                pass
            self.busy = False
            if kind == "ok":
                done(val)
            else:
                self.status.set("오류가 발생했습니다")
                self._show_dialog("showerror", "오류", str(val))

        self.after(80, poll)

    # =====================================================================
    # 📤 송신 탭
    # =====================================================================
    def _build_send_tab(self):
        t = self.enc_tab
        box = ttk.LabelFrame(t, text="1. 보낼 내용", padding=8)
        box.pack(fill="x")
        self.src_mode = tk.StringVar(value="text")
        ttk.Radiobutton(box, text="텍스트 메시지 (받는 쪽에서 글자로 바로 표시)", variable=self.src_mode,
                        value="text").grid(row=0, column=0, sticky="w")
        self.text_in = tk.Text(box, height=3, wrap="word")
        self.text_in.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(2, 6))
        self.text_in.insert("1.0", "Hello! This is a real-time message sent over audio. 🎵")
        ttk.Radiobutton(box, text="파일 (원래 파일명·크기도 함께 전송)", variable=self.src_mode,
                        value="file").grid(row=2, column=0, sticky="w")
        self.file_var = tk.StringVar()
        ttk.Entry(box, textvariable=self.file_var).grid(row=3, column=0, columnspan=2, sticky="ew")
        ttk.Button(box, text="찾아보기...", command=self._pick_send_file).grid(row=3, column=2, padx=(6, 0))
        box.columnconfigure(0, weight=1)
        box.columnconfigure(1, weight=1)

        act = ttk.LabelFrame(t, text="2. 보내기", padding=8)
        act.pack(fill="x", pady=8)

        row1 = ttk.Frame(act)
        row1.pack(fill="x")
        ttk.Button(row1, text="📤  송신  (누르면 바로 전송)", command=self._send_now).pack(side="left", ipadx=14, ipady=4)
        ttk.Button(row1, text="■ 중지", command=self._send_stop).pack(side="left", padx=8)
        ttk.Button(row1, text="💾 WAV로 저장...", command=self._save_wav).pack(side="left")

        row2 = ttk.Frame(act)
        row2.pack(fill="x", pady=(8, 0))
        self.opt_start = tk.BooleanVar(value=True)
        ttk.Checkbutton(row2, text="긴 시작음(1초)", variable=self.opt_start).pack(side="left")
        self.opt_compress = tk.BooleanVar(value=True)
        ttk.Checkbutton(row2, text="자동 압축", variable=self.opt_compress).pack(side="left", padx=(8, 0))
        ttk.Label(row2, text="   반복").pack(side="left")
        self.opt_repeat = tk.IntVar(value=1)
        ttk.Spinbox(row2, from_=1, to=10, textvariable=self.opt_repeat, width=3).pack(side="left", padx=3)
        ttk.Label(row2, text="회   |   재전송할 블록").pack(side="left")
        self.only_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.only_var, width=22).pack(side="left", padx=4)
        ttk.Label(row2, text="예) 3,7,12-15  (비우면 전체)").pack(side="left")

        row2b = ttk.Frame(act)
        row2b.pack(fill="x", pady=(6, 0))
        self.size_info = tk.StringVar(value="")
        ttk.Label(row2b, textvariable=self.size_info, foreground="#0b5cad").pack(side="left")

        row3 = ttk.Frame(act)
        row3.pack(fill="x", pady=(8, 0))
        # Consumer release uses the physical audio path only; development loopback is hidden.
        self.link_mode = tk.StringVar(value="speaker")
        ttk.Label(row3, text="🔊 스피커로 송신 (마이크로 수신)").pack(side="left")
        self.goto_live = tk.BooleanVar(value=True)
        ttk.Checkbutton(row3, text="송신하면 수신 탭으로 이동", variable=self.goto_live).pack(side="left", padx=12)

        row4 = ttk.Frame(act)
        row4.pack(fill="x", pady=(6, 0))
        ttk.Label(row4, text="출력 장치").pack(side="left")
        self.out_var = tk.StringVar()
        if live.HAVE_SD and self.out_devs:
            names = ["(기본 장치)"] + [f"{i}: {n}" for i, n in self.out_devs]
            self.out_combo = ttk.Combobox(row4, textvariable=self.out_var, values=names, state="readonly", width=40)
            self.out_combo.current(0)
            self.out_combo.pack(side="left", padx=6)
        else:
            ttk.Label(row4, text="  (sounddevice 없음 → 스피커 송신은 OS 재생기 사용, 마이크 수신은 불가. "
                                 "pip install sounddevice 권장)").pack(side="left")

        row5 = ttk.Frame(act)
        row5.pack(fill="x", pady=(8, 0))
        self.tx_bar = ttk.Progressbar(row5, mode="determinate", maximum=100)
        self.tx_bar.pack(side="left", fill="x", expand=True)
        self.tx_info = tk.StringVar(value="대기 중")
        ttk.Label(row5, textvariable=self.tx_info, width=42).pack(side="left", padx=8)

        self.enc_view = WaterfallView(t, height=260)
        self.enc_view.pack(fill="both", expand=True)

        # 내용/옵션이 바뀌면 '압축 후 크기·예상 시간'을 자동으로 다시 계산
        self._est_after = None
        self.text_in.bind("<KeyRelease>", self._schedule_estimate)
        for var in (self.src_mode, self.file_var, self.opt_start, self.opt_compress, self.opt_repeat):
            var.trace_add("write", self._schedule_estimate)
        self._schedule_estimate()

    def _schedule_estimate(self, *_):
        if self._est_after is not None:
            self.after_cancel(self._est_after)
        self._est_after = self.after(350, self._update_estimate)

    def _update_estimate(self):
        """압축 후 크기와 예상 전송 시간을 보여 준다 (큰 파일은 전송할 때 계산)."""
        self._est_after = None
        try:
            if self.src_mode.get() == "file":
                p = self.file_var.get().strip()
                if not p or not os.path.isfile(p):
                    self.size_info.set("")
                    self._fec_estimated_data_blocks = 0
                    self._fec_estimated_approximate = False
                    self._update_fec_capacity(0)
                    return
                if os.path.getsize(p) > 128 * 1024:
                    file_size = os.path.getsize(p)
                    name_bytes = len(os.path.basename(p).encode('utf-8', errors='replace'))
                    rough_payload_bytes = 13 + name_bytes + file_size
                    approx_blocks = core.n_packets_for(rough_payload_bytes)
                    self._fec_estimated_data_blocks = approx_blocks
                    self._fec_estimated_approximate = bool(self.opt_compress.get())
                    self._update_fec_capacity(approx_blocks, approximate=bool(self.opt_compress.get()))
                    self.size_info.set(f"Original {file_size:,} B — compression estimate is calculated when sending") if self.lang_var.get() == 'English' else self.size_info.set(f"원본 {file_size:,}B — 큰 파일의 압축 결과는 송신할 때 계산됩니다")
                    return
            data, kind, name = self._get_send_message()
            payload = core.prepare_payload(data, kind, name, compress=bool(self.opt_compress.get()))
            d = core.describe_payload(payload)
            fec_mode, fec_percent = self._selected_fec_config() if hasattr(self, 'fec_mode') else (0, 0)
            d['packets'] = core.count_wire_packets(payload, fec_mode, fec_percent)
            raw_payload = core.prepare_payload(data, kind, name, compress=False)
            d['packets_plain'] = core.count_wire_packets(raw_payload, fec_mode, fec_percent)
            try:
                rep_n = max(1, min(10, int(self.opt_repeat.get())))
            except (tk.TclError, ValueError):
                rep_n = 1
            st = bool(self.opt_start.get())
            t1 = core.transmission_seconds(d['packets'], rep_n, st)
            t0 = core.transmission_seconds(d['packets_plain'], rep_n, st)
            data_blocks = core.n_packets_for(len(payload))
            plain_data_blocks = core.n_packets_for(len(raw_payload))
            self._fec_estimated_data_blocks = data_blocks
            self._fec_estimated_approximate = False
            self._update_fec_capacity(data_blocks)
            if d['codec'] != core.CODEC_NONE:
                if self.lang_var.get() == 'English':
                    self.size_info.set(
                        f"Original {d['orig']:,} B → payload {d['stored']:,} B ({core.CODEC_LABEL[d['codec']]} compression, "
                        f"{d['stored'] / max(d['orig'], 1) * 100:.0f}%) · Data blocks {plain_data_blocks}→{data_blocks} · "
                        f"Estimated {t1:.0f} s (uncompressed {t0:.0f} s)")
                else:
                    self.size_info.set(
                        f"원본 {d['orig']:,}B → 전송 {d['stored']:,}B ({core.CODEC_LABEL[d['codec']]} 압축, "
                        f"{d['stored'] / max(d['orig'], 1) * 100:.0f}%) · 데이터 블록 {plain_data_blocks}→{data_blocks} · "
                        f"예상 {t1:.0f}초 (압축 안 하면 {t0:.0f}초)")
            else:
                why = ("Compression disabled" if not self.opt_compress.get() else "Compression would not reduce the payload") if self.lang_var.get() == 'English' else ("압축 꺼짐" if not self.opt_compress.get() else "압축해도 이득 없음 → 그대로 전송")
                self.size_info.set((f"Original {d['orig']:,} B · {why} · Data blocks {data_blocks} · Estimated {t1:.0f} s") if self.lang_var.get() == 'English' else f"원본 {d['orig']:,}B · {why} · 데이터 블록 {data_blocks} · 예상 {t1:.0f}초")
        except Exception:
            self.size_info.set("")
            self._fec_estimated_data_blocks = 0
            self._fec_estimated_approximate = False
            self._update_fec_capacity(0)

    def _pick_send_file(self):
        p = filedialog.askopenfilename(title=localize_ui_text("보낼 파일 선택", 'en' if self.lang_var.get() == "English" else 'ko'))
        if p:
            self.file_var.set(p)
            self.src_mode.set("file")

    def _get_send_message(self):
        """-> (data, kind, name)"""
        if self.src_mode.get() == "file":
            p = self.file_var.get().strip()
            if not p or not os.path.isfile(p):
                raise ValueError("보낼 파일을 선택해 주세요.")
            with open(p, "rb") as f:
                return f.read(), core.KIND_FILE, os.path.basename(p)
        text = self.text_in.get("1.0", "end-1c")
        if not text:
            raise ValueError("보낼 텍스트를 입력해 주세요.")
        return text.encode("utf-8"), core.KIND_TEXT, ""

    def _prepare_send(self):
        """보낼 내용 + 옵션을 검증해 작업 dict 로 만든다."""
        data, kind, name = self._get_send_message()
        payload = core.prepare_payload(data, kind, name, compress=bool(self.opt_compress.get()))
        info = core.describe_payload(payload)
        fec_mode, fec_percent = self._selected_fec_config()
        n_msg = core.count_wire_packets(payload, fec_mode, fec_percent)
        info['packets'] = n_msg
        try:
            repeat = max(1, min(10, int(self.opt_repeat.get())))
        except (tk.TclError, ValueError):
            repeat = 1
        only_txt = self.only_var.get().strip()
        only = None
        if only_txt:
            try:
                only = core.parse_index_list(only_txt, limit=n_msg)
            except ValueError as e:
                raise ValueError(f"재전송할 블록 입력이 올바르지 않습니다.\n{e}\n(이 메시지는 총 {n_msg}블록: 0~{n_msg - 1})")
        # A subset resend must not emit a new-start preamble: the receiver would
        # interpret that as a fresh message and clear the blocks already received.
        start_tone = bool(self.opt_start.get()) and not bool(only)
        n_send = len(only) if only else n_msg
        return {'data': data, 'kind': kind, 'name': name, 'payload': payload, 'n_msg': n_msg, 'info': info,
                'start_tone': start_tone, 'repeat': repeat, 'only': only, 'n_send': n_send,
                'fec_mode': fec_mode, 'fec_percent': fec_percent, 'fec_ui_mode': int(self.fec_mode.get()),
                'speed_preset': self.speed_key.get(),
                'total_s': core.transmission_seconds(n_send, repeat, start_tone)}

    def _selected_out_device(self):
        if not (live.HAVE_SD and self.out_devs):
            return None
        s = self.out_var.get()
        return None if (not s or s.startswith("(")) else int(s.split(":")[0])

    def _job_signal(self, job):
        sig, _ = core.build_signal_from_payload(job['payload'], core.FS, start_tone=job['start_tone'],
                                                repeat=job['repeat'], only=job['only'],
                                                fec_mode=job.get('fec_mode', 0), fec_percent=job.get('fec_percent', 0))
        return sig

    def _send_now(self):
        """📤 송신: 누르는 즉시 전송 시작 (WAV 저장 없음)."""
        if self.tx.active:
            self._show_dialog("showinfo", "안내", "이미 전송 중입니다. 먼저 ■ 중지하거나 끝날 때까지 기다려 주세요.")
            return
        try:
            core.set_speed_preset(self.speed_key.get())
            self.session.decoder.set_speed_preset(self.speed_key.get())
            job = self._prepare_send()
        except Exception as e:
            self._show_dialog("showwarning", "확인", str(e))
            return
        job['mode'] = 'speaker'
        if job.get('only'):
            self._log("부분 재전송에서는 기존 수신 블록을 유지하도록 시작음을 생략합니다.", "msg")
        job['t0'] = time.time()
        job['speed'] = 1.0
        self._tx_error_shown = False
        sig_clean = None

        if live.HAVE_SD and self.session.mode is None:      # 송신하면서 바로 마이크로 실시간 수신
            try:
                self.session.start_mic(self._selected_in_device())
            except Exception as e:                          # noqa
                self._log(f"(마이크 자동 시작 실패: {e})", "bad")
        self.tx.send(job['payload'], device=self._selected_out_device(), start_tone=job['start_tone'],
                     repeat=job['repeat'], only=job['only'], fec_mode=job['fec_mode'], fec_percent=job.get('fec_percent', 0))
        self._job = job
        self.tx_info.set(f"전송 시작 · 총 {job['total_s']:.0f}초 예상")
        self.status.set("송신 중...")
        if self.goto_live.get():
            self.nb.select(self.live_tab)

        def work():                                             # 화면용 워터폴은 따로 계산
            sig = sig_clean if sig_clean is not None else self._job_signal(job)
            return compute_spectrogram(sig, core.FS)

        def done(spec):
            self.last_spec = spec
            self.enc_view.load(spec)
            # 미리보기 계산 완료 후 '워터폴 계산 중...' 문구가 영구히 남지 않게 한다.
            if self._job is not None:
                active = self.tx.active
                self.status.set("송신 중..." if active else "송신 완료")
            elif self.session.mode is not None:
                self.status.set("실시간 수신 중...")
            else:
                self.status.set("준비됨")

        self.run_bg(work, done, "워터폴 계산 중...")

    def _send_stop(self):
        self.tx.stop()
        self._job = None
        self.enc_view.set_cursor(None)
        self.tx_info.set("중지됨")
        self.status.set("송신 중지")

    def _save_wav(self):
        try:
            job = self._prepare_send()
        except Exception as e:
            self._show_dialog("showwarning", "확인", str(e))
            return
        path = filedialog.asksaveasfilename(title=localize_ui_text("WAV 저장 위치", 'en' if self.lang_var.get() == "English" else 'ko'), defaultextension=".wav",
                                            initialfile="dtmf_transfer.wav", filetypes=[("WAV 오디오", "*.wav")])
        if not path:
            return

        def work():
            n, dur = core.encode_payload_to_wav(job['payload'], path, start_tone=job['start_tone'],
                                                repeat=job['repeat'], only=job['only'],
                                                fec_mode=job['fec_mode'], fec_percent=job.get('fec_percent', 0))
            x, fs = load_wav_mono(path)
            return n, dur, compute_spectrogram(x, fs)

        def done(res):
            n, dur, spec = res
            self.dec_path.set(path)
            self.tx_info.set(f"저장됨: 블록 {job['n_send']}개, {dur:.1f}초")
            self.status.set(f"저장 완료: {path}")
            self.enc_view.load(spec)

        self.run_bg(work, done, "WAV 생성 중...")

    def _tx_tick(self):
        """송신 진행률 / 워터폴 재생 커서 갱신 (100ms)"""
        job = self._job
        if job:
            speaker = job['mode'] == 'speaker'
            active = self.tx.active
            el = (time.time() - job['t0']) * job['speed']
            total = job['total_s']
            if speaker and self.tx.done and self.tx.error is not None:
                if not self._tx_error_shown:
                    self._tx_error_shown = True
                    self.tx_info.set("전송 실패")
                    self._show_dialog("showerror", "전송 오류", str(self.tx.error))
                self.enc_view.set_cursor(None)
                self._job = None
            elif active and el < total:
                self.tx_bar["value"] = min(100.0, el / total * 100)
                extra = (f" · 블록 {min(self.tx.sent + 1, self.tx.total)}/{self.tx.total}"
                         if speaker and not job.get('fec_mode', 0) else "")
                self.tx_info.set(f"송신 중 {el / total * 100:.0f}%{extra} · 남은 약 {max(0, total - el):.0f}초")
                if self.last_spec is not None:
                    self.enc_view.set_cursor(el)
            else:
                self.tx_bar["value"] = 100
                if job.get('fec_mode', 0):
                    shown_blocks = core.n_packets_for(len(job['payload']))
                    fec_desc = (f"Custom FEC ({job.get('fec_percent', 0)}%)" if job['fec_mode'] == 4
                                else f"FEC mode {job['fec_mode']}")
                    completion = (f"Transmission complete ({shown_blocks} data blocks · {fec_desc} · "
                                  f"{job['repeat']} pass(es))") if self.lang_var.get() == 'English' else (                                  f"송신 완료 (데이터 블록 {shown_blocks}개 · "
                                  f"{'사용자 지정 FEC ' + str(job.get('fec_percent', 0)) + '%' if job['fec_mode'] == 4 else 'FEC 모드 ' + str(job['fec_mode'])} · "
                                  f"{job['repeat']}회)")
                else:
                    completion = (f"Transmission complete ({job['n_send']} packets × {job['repeat']} pass(es))"
                                  if self.lang_var.get() == 'English' else "송신 완료" + f" ({job['n_send']}블록 × {job['repeat']}회)")
                self.tx_info.set(completion)
                self.enc_view.set_cursor(None)
                if not active:
                    self._job = None
                    self.status.set("송신 완료")
        self.after(100, self._tx_tick)

    # =====================================================================
    # 📡 실시간 수신 탭
    # =====================================================================
    def _build_live_tab(self):
        t = self.live_tab
        ctl = ttk.LabelFrame(t, text="입력", padding=6)
        ctl.pack(fill="x")

        row1 = ttk.Frame(ctl)
        row1.pack(fill="x")
        ttk.Label(row1, text="마이크").pack(side="left")
        self.in_var = tk.StringVar()
        if live.HAVE_SD and self.in_devs:
            names = ["(기본 장치)"] + [f"{i}: {n}" for i, n in self.in_devs]
            cb = ttk.Combobox(row1, textvariable=self.in_var, values=names, state="readonly", width=36)
            cb.current(0)
            cb.pack(side="left", padx=4)
        else:
            ttk.Label(row1, text="(오디오 입력을 사용할 수 없습니다. Windows 오디오 장치를 확인하세요.)").pack(side="left", padx=4)
        self.mic_btn = ttk.Button(row1, text="🎙 수신 시작", command=self._toggle_mic)
        self.mic_btn.pack(side="left", padx=8)
        self.auto_listen = tk.BooleanVar(value=self.have_audio)
        ttk.Checkbutton(row1, text="실행 시 자동 수신", variable=self.auto_listen).pack(side="left")
        ttk.Button(row1, text="🗑 수신 내용 지우기", command=self._live_clear).pack(side="left", padx=8)

        meter = ttk.Frame(t)
        meter.pack(fill="x", pady=(6, 2))
        ttk.Label(meter, text="입력 레벨").pack(side="left")
        self.level_bar = ttk.Progressbar(meter, mode="determinate", maximum=100, length=200)
        self.level_bar.pack(side="left", padx=6)
        self.level_lbl = ttk.Label(meter, text="-- dBFS", width=10)
        self.level_lbl.pack(side="left")
        self.live_state = tk.StringVar(value="정지됨")
        ttk.Label(meter, textvariable=self.live_state, font=("TkDefaultFont", 10, "bold")).pack(side="left", padx=12)

        self.live_view = WaterfallView(t, with_zoom=False, height=190)
        self.live_view.title_lbl.config(text="실시간 워터폴 (최근 12초)")
        self.live_view.pack(fill="x")

        self.live_grid = BlockGrid(t, height=96)
        self.live_grid.pack(fill="x", pady=(6, 0))
        miss = ttk.Frame(t)
        miss.pack(fill="x", pady=(2, 0))
        ttk.Label(miss, text="실패한 블록 번호").pack(side="left")
        self.miss_var = tk.StringVar(value="")
        ttk.Entry(miss, textvariable=self.miss_var, state="readonly", width=40).pack(side="left", padx=6)
        ttk.Button(miss, text="복사", width=5, command=self._copy_missing).pack(side="left")
        ttk.Button(miss, text="➜ 송신 탭 '재전송할 블록'에 채우기", command=self._fill_resend).pack(side="left", padx=8)

        lower = ttk.PanedWindow(t, orient="horizontal")
        lower.pack(fill="both", expand=True, pady=(6, 0))
        left = ttk.LabelFrame(lower, text="수신 중인 메시지", padding=6)
        self.live_info = tk.StringVar(value="아직 수신된 내용이 없습니다.")
        ttk.Label(left, textvariable=self.live_info).pack(anchor="w")
        self.live_bar = ttk.Progressbar(left, mode="determinate", maximum=100)
        self.live_bar.pack(fill="x", pady=3)
        preview_frame = ttk.Frame(left)
        preview_frame.pack(fill="both", expand=True)
        self.live_preview = tk.Text(preview_frame, height=4, wrap="word", state="disabled")
        preview_scroll = ttk.Scrollbar(preview_frame, orient="vertical", command=self.live_preview.yview)
        self.live_preview.configure(yscrollcommand=preview_scroll.set)
        self.live_preview.pack(side="left", fill="both", expand=True)
        preview_scroll.pack(side="right", fill="y")
        self._live_preview_signature = None
        self.live_save = ttk.Button(left, text="💾 저장...", command=self._live_save, state="disabled")
        self.live_save.pack(anchor="e", pady=(3, 0))
        lower.add(left, weight=3)

        right = ttk.LabelFrame(lower, text="수신 로그", padding=6)
        self.log = tk.Text(right, height=4, wrap="none", state="disabled", width=36)
        sb = ttk.Scrollbar(right, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.log.tag_configure("ok", foreground="#1a8f3a")
        self.log.tag_configure("bad", foreground="#cc2222")
        self.log.tag_configure("msg", foreground="#0b5cad")
        lower.add(right, weight=2)

    def _log(self, text, tag=None, translate=True):
        if translate:
            text = localize_ui_text(text, 'en' if self.lang_var.get() == 'English' else 'ko')
        self.log.config(state="normal")
        self.log.insert("end", text + "\n", tag or ())
        self.log.see("end")
        self.log.config(state="disabled")

    def _selected_in_device(self):
        s = self.in_var.get()
        return None if (not s or s.startswith("(")) else int(s.split(":")[0])

    def _auto_start_mic(self):
        if self.session.mode is None:
            self._toggle_mic(silent=True)

    def _toggle_mic(self, silent=False):
        if self.session.mode == 'mic':
            self.session.stop()
            self.status.set("수신 중지")
            return
        try:
            self.session.start_mic(self._selected_in_device())
        except Exception as e:
            if not silent:
                self._show_dialog("showerror", "마이크 오류", str(e))
            else:
                self._log(f"(자동 수신 시작 실패: {e})", "bad")
            return
        self.status.set("마이크로 실시간 수신 중...")
        self._log("— 마이크 수신 시작 —", "msg")

    def _clear_live_display(self):
        """수신 탭의 결과 표시만 비운다. 활성 마이크/디코더 스트림은 건드리지 않는다."""
        self.live_view.markers = []
        self._hi_run = None
        self.live_info.set("아직 수신된 내용이 없습니다.")
        self.live_bar["value"] = 0
        self._live_preview_signature = None
        fill_preview(self.live_preview, b"", None)
        self.live_save.config(state="disabled")
        self.live_grid.clear()
        self.miss_var.set("")
        self.live_status = None
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")

    def _live_clear(self):
        self.session.reset()
        self._clear_live_display()

    def _copy_missing(self):
        self.clipboard_clear()
        self.clipboard_append(self.miss_var.get())
        self.status.set("실패한 블록 번호를 복사했습니다")

    def _fill_resend(self):
        if not self.miss_var.get():
            self._show_dialog("showinfo", "안내", "실패한 블록이 없습니다.")
            return
        text = self.miss_var.get()
        st = self.live_status or {}
        mode = int(st.get('fec_mode', 0) or 0)
        if mode:
            # 원래 메시지의 FEC 모드를 선택해야 재전송 패킷의 물리 번호가 일치한다.
            if int(self.fec_mode.get()) != mode:
                self.fec_mode.set(mode)
            if mode == 4:
                self.fec_percent_var.set(str(int(st.get('fec_percent', 25) or 25)))
            self._refresh_setting_choices()
            # 표시 번호는 논리 데이터 블록 번호. 송신기의 선택 목록은 실제 패킷 위치이므로 변환한다.
            try:
                logical = core.parse_index_list(text, limit=int(st.get('fec_data_blocks') or 0))
            except ValueError as e:
                self._show_dialog("showwarning", "확인", str(e))
                return
            k = int(st.get('fec_group_k') or core.FEC_GROUP_K)
            parity = int(st.get('fec_parity_count') or core.fec_parity_count(mode))
            physical = [core.FEC_HEADER_COPIES + (i // k) * (k + parity) + (i % k)
                        for i in logical]
            text = core.format_index_list(physical)
        self.only_var.set(text)
        self.nb.select(self.enc_tab)
        self.status.set("재전송할 블록을 채웠습니다 — 보낼 내용을 확인하고 📤 송신을 누르세요")

    def _live_save(self):
        st = self.live_status
        if not st or not st['data']:
            return
        data = st['data']
        if not st['container']:                                  # raw 데이터는 패딩 제거
            stripped = data.rstrip(b'\x00')
            if stripped.endswith(b'\x80'):
                data = stripped[:-1]
        name = st['name'] or ("message.txt" if st['kind'] == core.KIND_TEXT else "recovered.bin")
        p = filedialog.asksaveasfilename(title=localize_ui_text("수신한 내용 저장", 'en' if self.lang_var.get() == "English" else 'ko'), initialfile=name)
        if p:
            with open(p, "wb") as f:
                f.write(data)
            self.status.set(f"저장 완료: {p}")

    def _live_tick(self):
        """실시간 수신 화면 갱신 (250ms): 이벤트 + 레벨 + 블록 칸 + 메시지 + 롤링 워터폴"""
        try:
            self._live_update()
        except Exception as exc:                                 # keep the UI alive on recoverable refresh errors
            self.status.set(("Receive refresh error: " if self.lang_var.get() == "English" else "수신 화면 갱신 오류: ") + str(exc))
        self.after(self.LIVE_TICK_MS, self._live_tick)

    def _live_waterfall_tick(self):
        """Refresh only the rolling waterfall at 25 FPS, using new audio samples only."""
        try:
            s = self.session
            running = s.mode is not None
            if running or self._live_was_running:
                # Snapshot only the samples added since the previous waterfall frame.
                cursor_before = self._wf_cursor_sample
                chunk, actual_start, end_sample, generation = s.get_ring_delta(cursor_before)
                auto_clear = int(getattr(s.decoder, 'auto_clear_count', 0))
                reset_needed = (
                    self._wf_generation != generation
                    or self._wf_auto_clear_count != auto_clear
                    or end_sample < cursor_before
                )
                if reset_needed:
                    self._live_wf_buffer.reset(actual_start)
                    self._hi_run = None
                    self._wf_generation = generation
                    self._wf_auto_clear_count = auto_clear

                if chunk.size:
                    self._live_wf_buffer.append(chunk, actual_start)
                self._wf_cursor_sample = end_sample

                if chunk.size or reset_needed:
                    spec = self._live_wf_buffer.make_spec(end_sample, hi_floor=live_hi_floor(s.fs))
                    if spec is not None:
                        raw_hi = spec['hi']
                        # Smooth the colour scale so a loud burst doesn't make the rest of the trace flicker.
                        self._hi_run = raw_hi if self._hi_run is None else max(raw_hi, self._hi_run - 0.08)
                        spec['hi'] = self._hi_run
                        total_s = end_sample / s.fs
                        markers = [
                            {'t': p['t'], 'ok': p['ok'],
                             'label': str(p.get('logical_idx')) if p.get('fec_mode') and p.get('logical_idx') is not None
                                      else (str(p['seq']) if p['ok'] else '✗')}
                            for p in self._wf_packets
                            if p.get('t', 0.0) >= spec.get('t_offset', 0.0) - 0.2
                            and not p.get('fec_internal', False)
                        ]
                        self.live_view.show_live(spec, markers, s.RING_S, now_s=total_s)
                self._live_was_running = running
        except Exception as exc:
            self.status.set(("Waterfall refresh error: " if self.lang_var.get() == "English" else "워터폴 갱신 오류: ") + str(exc))
        self.after(self.LIVE_WATERFALL_TICK_MS, self._live_waterfall_tick)

    def _live_update(self):
        s = self.session
        dec = s.decoder
        # 입력 장치/실시간 큐 오류는 워터폴만 움직이는 증상과 구분할 수 있도록 로그에 표시
        err = getattr(s, "error", None)
        if err is not None and str(err) != getattr(self, "_last_live_error", None):
            self._last_live_error = str(err)
            self._log(f"⚠ 실시간 오디오: {err}", "bad")
        # (1) 이벤트 -> 로그
        while True:
            try:
                kind, info = dec.events.get_nowait()
            except queue.Empty:
                break
            if kind == 'auto_clear':
                # Clear stale receive display only for a fresh transfer; keep repeated passes merged.
                self._clear_live_display()
                self.status.set(
                    "New transmission detected — previous receive results cleared"
                    if self.lang_var.get() == 'English' else
                    "새 전송 감지 — 이전 결과 자동 초기화"
                )
            elif kind == 'start':
                self._flash_until = time.time() + 2.5
                self._log(f"📣 시작음 감지 — 전송 시작 ({info:.1f}s)", "msg")
            elif kind == 'sync_prediction':
                self._log(f"[{info:6.1f}s] 동기 펄스 누락 — 이전 패킷 주기로 복구 시도", "msg")
            elif kind == 'packet':
                if info['ok']:
                    adj = float(info.get('timing_ms', 0.0))
                    suffix = f" (타이밍 보정 {adj:+.0f} ms)" if abs(adj) >= 0.5 else ""
                    if info.get('soft'):
                        suffix += " (소프트 CRC 복원)"
                    label = (f"데이터 블록 #{info['logical_idx']}" if info.get('fec_mode') and
                             info.get('logical_idx') is not None else f"블록 #{info['seq']}")
                    self._log(f"[{info['t']:6.1f}s] {label} 성공{suffix}", "ok")
                else:
                    label = (f"데이터 블록 #{info['logical_idx']} CRC 실패" if info.get('fec_mode') and
                             info.get('logical_idx') is not None else "블록 CRC 실패")
                    self._log(f"[{info['t']:6.1f}s] {label} ✗", "bad")
            elif kind == 'message':
                if info['kind'] == core.KIND_TEXT:
                    text_prefix = "✅ Text received: " if self.lang_var.get() == 'English' else "✅ 텍스트 수신 완료: "
                    self._log(f"{text_prefix}{info['data'].decode('utf-8', errors='replace')[:60]}", "msg", translate=False)
                else:
                    self._log(f"✅ {kind_text(info['kind'])} 수신 완료: {info['name'] or '(이름 없음)'} "
                              f"({len(info['data'])}바이트)", "msg")
                self.status.set("수신 완료")
            elif kind == 'reset' and not info['complete']:
                self._log("↺ 새 메시지 시작 (이전 미완성 메시지 폐기)", "bad")
            elif kind == 'idle':
                st_now = dec.snapshot()['blocks']['states']
                n_fail = st_now.count(2)
                if n_fail:
                    self._log(f"⏹ 전송 종료 — 실패 {n_fail}블록 (재전송하면 복원됩니다)", "bad")
                    self.status.set(f"수신 종료: 실패 {n_fail}블록")
                else:
                    self._log("⏹ 전송 종료 — 모든 블록 성공", "ok")

        snap = dec.snapshot()
        self._wf_packets = snap.get('packets', [])
        running = s.mode is not None
        self.mic_btn.config(text="⏹ 수신 중지" if s.mode == 'mic' else "🎙 수신 시작")
        # (2) 레벨 미터 / 상태 문구
        db = 20 * math.log10(max(snap['level'], 1e-6)) if running else -60
        self.level_bar["value"] = max(0.0, min(100.0, (db + 60) / 60 * 100))
        self.level_lbl.config(text=f"{db:5.0f} dBFS" if running else "-- dBFS")
        if not running:
            self.live_state.set("정지됨")
        elif time.time() < self._flash_until and snap['state'] != 'receive':
            self.live_state.set("📣 시작음 감지!")
        elif snap['state'] == 'receive':
            self.live_state.set("📶 블록 수신 중…")
        else:
            self.live_state.set("👂 듣는 중 (시작음/동기 펄스 대기)")

        # (3) 블록 칸 + 실패 목록
        st = snap['status']
        # FEC 헤더/패리티 패킷은 숨기고 데이터 블록만 표시한다.
        states = data_only_block_states(snap['blocks']['states'], st)
        self.live_grid.update_blocks(states)
        self.miss_var.set(core.format_index_list([i for i, v in enumerate(states) if v == core.BlockMap.FAIL]))

        # (4) 메시지 패널
        self.live_status = st if st['received'] else None
        if st['received']:
            exp = st['expected']
            is_fec = bool(st.get('fec_mode', 0))
            if is_fec:
                resolved = int(st.get('fec_resolved_count', 0))
                data_total = int(st.get('fec_data_blocks', 0))
                progress = 100.0 if st['complete'] else (min(100.0, resolved / data_total * 100) if data_total else 0.0)
                block_text = f"{resolved}/{data_total} 데이터 블록"
            else:
                progress = (100.0 if st['complete'] else
                            min(100.0, st['received'] / exp * 100) if exp else 0.0)
                block_text = f"{st['received']}/{exp} 블록" if st['container'] else f"{st['received']}블록 수신"
            if st['container']:
                head = f"종류: {kind_text(st['kind'])}"
                if st['name']:
                    head += f"  ·  파일명: {st['name']}"
                size_txt = f"크기 {st['size']:,} 바이트"
                if st['codec']:
                    size_txt += f" ({core.CODEC_LABEL.get(st['codec'], '?')} 압축 → {st['stored_size']:,}바이트 전송)"
                head += f"  ·  {size_txt}  ·  {block_text}"
            else:
                head = f"형식: {kind_text(None)}  ·  {block_text}"
            self.live_bar["value"] = progress
            if st.get('complete'):
                head += "  ✅ 완료"
            if is_fec:
                preset_rate = core.fec_percent_for_mode(st['fec_mode'])
                if self.lang_var.get() == 'English':
                    fec_desc = (f"Custom FEC ({st.get('fec_percent', 0)}%)" if st['fec_mode'] == 4
                                else f"FEC Mode {st['fec_mode']} ({preset_rate}% parity)" if st['fec_mode'] in (1, 2, 3)
                                else "CRC only")
                else:
                    fec_desc = (f"사용자 지정 FEC {st.get('fec_percent', 0)}%" if st['fec_mode'] == 4
                                else f"FEC 모드 {st['fec_mode']} (패리티 {preset_rate}%)" if st['fec_mode'] in (1, 2, 3)
                                else "CRC만")
                recovered_n = int(st.get('fec_recovered', 0))
                recovered_unit = "data block" if recovered_n == 1 else "data blocks"
                recovered_desc = f"{recovered_n} {recovered_unit} recovered" if self.lang_var.get() == 'English' else f"복구 {recovered_n} 데이터 블록"
                head += f"  ·  {fec_desc} · {recovered_desc}"
            self.live_info.set(head)
            signature = (st['data'], st['kind'])
            if signature != self._live_preview_signature:
                fill_preview(self.live_preview, st['data'], st['kind'])
                self.live_preview.see("end")
                self._live_preview_signature = signature
            self.live_save.config(state="normal" if st['data'] else "disabled")


        self._apply_language()

    # =====================================================================
    # 📂 파일 디코딩 탭
    # =====================================================================
    def _build_file_tab(self):
        t = self.dec_tab
        top = ttk.LabelFrame(t, text="1. 받은 소리(WAV) 선택", padding=8)
        top.pack(fill="x")
        self.dec_path = tk.StringVar()
        ttk.Entry(top, textvariable=self.dec_path).grid(row=0, column=0, sticky="ew")
        ttk.Button(top, text="찾아보기...", command=self._pick_wav).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(top, text="▶", width=3, command=self._play_dec).grid(row=0, column=2, padx=(6, 0))
        ttk.Button(top, text="■", width=3, command=self.player.stop).grid(row=0, column=3, padx=(2, 0))
        top.columnconfigure(0, weight=1)


        act = ttk.Frame(t)
        act.pack(fill="x", pady=6)
        ttk.Button(act, text="🔍 디코딩 시작", command=self._do_decode).pack(side="left")
        self.save_btn = ttk.Button(act, text="💾 복원한 내용 저장...", command=self._save_payload, state="disabled")
        self.save_btn.pack(side="left", padx=6)
        self.dec_info = tk.StringVar(value="")
        ttk.Label(act, textvariable=self.dec_info).pack(side="left", padx=10)

        self.dec_view = WaterfallView(t, height=250)
        self.dec_view.pack(fill="x")
        self.dec_grid = BlockGrid(t, height=90)
        self.dec_grid.pack(fill="x", pady=(6, 0))

        lower = ttk.PanedWindow(t, orient="horizontal")
        lower.pack(fill="both", expand=True, pady=(6, 0))
        left = ttk.LabelFrame(lower, text="패킷 목록 (클릭하면 워터폴 이동)", padding=4)
        self.tree = ttk.Treeview(left, columns=("no", "time", "state"), show="headings", height=5)
        for c, txt, w in (("no", "번호", 60), ("time", "시각(초)", 90), ("state", "CRC", 90)):
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=w, anchor="center")
        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("ok", foreground="#1a8f3a")
        self.tree.tag_configure("bad", foreground="#cc2222")
        self.tree.bind("<<TreeviewSelect>>", self._on_pick_packet)
        lower.add(left, weight=1)

        right = ttk.LabelFrame(lower, text="복원 내용 미리보기", padding=4)
        self.msg_lbl = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.msg_lbl).pack(anchor="w")
        self.preview = tk.Text(right, height=5, wrap="word", state="disabled")
        self.preview.pack(fill="both", expand=True)
        lower.add(right, weight=2)

    def _pick_wav(self):
        p = filedialog.askopenfilename(title=localize_ui_text("WAV 선택", 'en' if self.lang_var.get() == "English" else 'ko'), filetypes=[(localize_ui_text("WAV 오디오", 'en' if self.lang_var.get() == "English" else 'ko'), "*.wav"), (localize_ui_text("모든 파일", 'en' if self.lang_var.get() == "English" else 'ko'), "*.*")])
        if p:
            self.dec_path.set(p)

    def _play_dec(self):
        p = self.dec_path.get().strip()
        if p and os.path.isfile(p) and not self.player.play(p):
            self._show_dialog("showinfo", "안내", "이 환경에서는 자동 재생을 쓸 수 없습니다.")

    def _do_decode(self):
        path = self.dec_path.get().strip()
        if not path or not os.path.isfile(path):
            self._show_dialog("showwarning", "확인", "디코딩할 WAV 파일을 선택해 주세요.")
            return
        def work():
            payload, stats = core.decode_wav_to_bytes(path, verbose=False)
            x, fs = load_wav_mono(path)
            spec = compute_spectrogram(x, fs)
            return payload, stats, spec

        self.run_bg(work, self._decode_done, "디코딩 중...")

    def _decode_done(self, res):
        payload, stats, spec = res
        self.payload = payload
        self.file_msg = stats['message']
        pk = stats['packets']
        self.dec_view.load(spec, [{'t': p['t'], 'ok': p['ok'],
                                   'label': str(p.get('logical_idx')) if p.get('fec_mode') and p.get('logical_idx') is not None
                                            else (str(p['seq']) if p['ok'] else '✗')}
                                  for p in pk if not p.get('fec_internal', False)])
        block_states = data_only_block_states(stats['blocks']['states'], stats)
        self.dec_grid.update_blocks(block_states)

        self.tree.delete(*self.tree.get_children())
        visible_pk = [p for p in pk if not p.get('fec_internal', False)]
        for p in visible_pk:
            packet_label = (p.get('logical_idx') if p.get('fec_mode') and p.get('logical_idx') is not None
                            else (p['seq'] if p['ok'] else "??"))
            self.tree.insert("", "end", values=(packet_label, f"{p['t']:.3f}", "OK" if p['ok'] else "에러"),
                             tags=("ok" if p['ok'] else "bad",))

        if not pk:
            self.dec_info.set("동기화 펄스를 찾지 못했습니다.")
            self.status.set("디코딩 실패: 패킷 없음")
        else:
            miss = len(stats['packets_missing'])
            visible_pk = [p for p in pk if not p.get('fec_internal', False)]
            crc_ok_visible = sum(bool(p['ok']) for p in visible_pk)
            crc_error_visible = len(visible_pk) - crc_ok_visible
            if stats.get('fec_mode', 0):
                total_data = int(stats.get('fec_data_blocks', 0))
                resolved_data = int(stats.get('fec_resolved_count', 0))
                info_text = (f"데이터 블록 {resolved_data}/{total_data} · CRC 정상 {crc_ok_visible} · "
                             f"오류 {crc_error_visible} · 복원 {len(payload)}바이트")
                preset_rate = core.fec_percent_for_mode(stats['fec_mode'])
                if self.lang_var.get() == 'English':
                    fec_desc = (f"Custom FEC ({stats.get('fec_percent', 0)}%)" if stats['fec_mode'] == 4
                                else f"FEC Mode {stats['fec_mode']} ({preset_rate}% parity)" if stats['fec_mode'] in (1, 2, 3)
                                else "CRC only")
                else:
                    fec_desc = (f"사용자 지정 FEC {stats.get('fec_percent', 0)}%" if stats['fec_mode'] == 4
                                else f"FEC 모드 {stats['fec_mode']} (패리티 {preset_rate}%)" if stats['fec_mode'] in (1, 2, 3)
                                else "CRC만")
                recovered_n = int(stats.get('fec_recovered', 0))
                recovered_unit = "data block" if recovered_n == 1 else "data blocks"
                recovered_desc = f"{recovered_n} {recovered_unit} recovered" if self.lang_var.get() == 'English' else f"복구 {recovered_n} 데이터 블록"
                info_text += f" · {fec_desc} · {recovered_desc}"
            else:
                info_text = (f"감지 {len(visible_pk)} · CRC 정상 {crc_ok_visible} · "
                             f"에러 {crc_error_visible} · 누락 {miss} · 복원 {len(payload)}바이트")
            self.dec_info.set(info_text)
            message_stats = stats.get('message') or {}
            has_problem = (crc_error_visible > 0 or miss > 0 or bool(message_stats.get('truncated')))
            self.status.set("디코딩 완료" + (" (일부 패킷 문제)" if has_problem else ""))

        m = self.file_msg
        if m:
            label = f"종류: {kind_text(m['kind'])}" + (f"  ·  파일명: {m['name']}" if m['name'] else "")
            if m.get('codec'):
                label += f"  ·  {core.CODEC_LABEL.get(m['codec'], '?')} 압축 {m['stored_size']:,}→{m['raw_size']:,}바이트"
            if m['truncated']:
                label += "  ⚠ 일부 누락"
            self.msg_lbl.set(label)
        else:
            self.msg_lbl.set("")
        self.save_btn.config(state="normal" if payload else "disabled")
        fill_preview(self.preview, payload, m['kind'] if m else None)

    def _on_pick_packet(self, _event):
        sel = self.tree.selection()
        if sel:
            self.dec_view.scroll_to(float(self.tree.item(sel[0], "values")[1]))

    def _save_payload(self):
        if not self.payload:
            return
        m = self.file_msg or {}
        name = m.get('name') or ("message.txt" if m.get('kind') == core.KIND_TEXT else "recovered.bin")
        p = filedialog.asksaveasfilename(title=localize_ui_text("복원한 내용 저장", 'en' if self.lang_var.get() == "English" else 'ko'), initialfile=name)
        if p:
            with open(p, "wb") as f:
                f.write(self.payload)
            self.status.set(f"저장 완료: {p}")


if __name__ == "__main__":
    App().mainloop()
