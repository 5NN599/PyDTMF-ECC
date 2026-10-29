#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PyDTMF-ECC live audio streaming module.

This module provides the streaming packet decoder, microphone receiver, live speaker
transmitter, audio player helper, and rolling waveform/spectrogram buffers used by the GUI.
It follows the packet format and timing presets implemented in ``audio_file_transfer.py``.

The packaged Windows application includes its audio backend. When running from source,
install the dependencies listed in ``requirements-build.txt`` first.
"""

import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import audio_file_transfer as core        # numpy/scipy 가 없으면 여기서 자동 설치됨

import numpy as np
from scipy.io import wavfile
from scipy.signal import butter, sosfilt

# 마이크/스피커 입출력용 선택 라이브러리: 없으면 자동 설치를 시도하고, 실패해도 계속 진행
sd = core.ensure_package("sounddevice", optional=True)
HAVE_SD = sd is not None


# ----------------------------------------------------------------------------
# 1. 스트리밍 디코더
# ----------------------------------------------------------------------------
class StreamDecoder:
    """
    소리 조각을 feed() 로 계속 넣으면 패킷이 완성될 때마다 디코딩한다.

    상태 기계
      search  : 2ms 간격으로 1850Hz 동기 펄스 탐색 (선택된 속도의 분석 창)
      receive : 동기 펄스를 기준으로 선택된 속도 간격에 맞춰 38개 DTMF를 샘플링 -> CRC 검증

    이벤트 큐(self.events)
      ('packet', info)   패킷 하나 처리됨 (info: t, seq, ok, raw)
      ('message', status) 메시지가 완전히 도착함 (컨테이너 길이 기준)
      ('reset', status)   새 메시지가 시작되어 이전(미완성) 메시지를 버림
    """
    # 실제 스피커-공기-마이크 경로는 루프백보다 작고 주파수 응답도 고르지 않다.
    # CRC가 최종 검증을 맡으므로 동기 후보 검출은 너무 엄격하게 막지 않는다.
    MIN_AMP = 0.00035      # 1850Hz 동기 펄스 최소 진폭
    START_MIN_AMP = 0.00025
    START_RATIO = 2.0      # 1000Hz 시작음 / 인접 기준 주파수 에너지 비
    NOISE_REF = 1050.0     # 잡음 기준 주파수
    NOISE_RATIO = 1.6      # 1850Hz 동기음의 잡음 기준 대비 최소 비
    CLEAR_RATIO = 1.25     # 스피커/마이크 주파수 응답 차이를 고려
    SYNC_ROW_RATIO = 4.0  # 음향 경로에서 동기음이 약해지는 경우를 허용

    def __init__(self, fs=core.FS):
        self.fs = fs
        self.tone_n = int(round(fs * core.DEMOD_MS / 1000))                  # 프리셋별 분석 창
        self.period = int(round(fs * (core.TONE_MS + core.GAP_MS) / 1000))    # 프리셋별 톤+공백
        self.pkt_len = self.period * (core.N_SYMBOLS + 1)                     # 1170ms
        self.hop = max(1, int(fs * core.SCAN_HOP_MS / 1000))                  # 2ms
        self.hann = np.hanning(self.tone_n)
        self.hann_sum = float(self.hann.sum())
        self.start_n = int(round(fs * 0.05))                       # 시작음 분석 창 50ms
        self.start_hop = max(1, int(round(fs * 0.01)))             # 10ms 겹침 홉: 프레임 경계에서 시작음을 놓치지 않음
        self.hann_s = np.hanning(self.start_n)
        self.hann_s_sum = float(self.hann_s.sum())

        self.sos = butter(4, [core.BP_LOW, core.BP_HIGH], btype='bandpass', fs=fs, output='sos')
        self.lock = threading.RLock()
        self.events = queue.Queue()
        self.reset_all()

    # --- 상태 초기화 ---
    def reset_all(self):
        with self.lock:
            self.zi = np.zeros((self.sos.shape[0], 2))
            self.buf = np.zeros(0)          # 필터링된 신호 (절대 인덱스 base 부터)
            self.base = 0
            self.total = 0                  # 지금까지 받은 총 샘플 수
            self.scan_abs = 0
            self.state = 'search'
            self.pending = None
            self.verified = False
            # 정상 동기 펄스를 하나 잡은 뒤에는 패킷 주기로 다음 동기를 예측한다.
            # 실제 동기음이 일시적으로 묻혀도 완성된 DTMF 본문을 복구할 수 있다.
            self.predicted_t0 = None
            self.predicted_p0 = None
            self.from_prediction = False
            self.level = 0.0
            self.packets = []
            self.history = []               # 완료된 메시지들
            self.start_scan_abs = 0         # 시작음 검출 진행 위치
            self.start_run = 0
            self.start_gap = 0
            self.start_run_start_abs = None  # first window of the current confirmed start-tone run
            self.last_start_onset_sample = None
            self.last_start_pkt_len = None
            self.start_latched = False
            self.pass_count = 0             # 감지한 전송 시작/반복 라운드 수
            self.auto_clear_count = 0        # 자동 초기화 세대(워터폴도 즉시 비우기 위해 사용)
            self.last_pkt_total = None      # 마지막 처리 패킷을 확인한 시각(입력 샘플 수)
            self.last_packet_end_sample = None  # 마지막 프레임의 실제 패킷 종료 위치
            self.idle_marked = False
            self._new_assembler()

    def set_speed_preset(self, preset):
        """속도 프리셋 변경. 수신 중이면 진행 중 프레임만 폐기하고 계속 듣는다."""
        cfg = core.set_speed_preset(preset)
        with self.lock:
            self.tone_n = int(round(self.fs * core.DEMOD_MS / 1000))
            self.period = int(round(self.fs * (core.TONE_MS + core.GAP_MS) / 1000))
            self.pkt_len = self.period * (core.N_SYMBOLS + 1)
            self.hop = max(1, int(self.fs * core.SCAN_HOP_MS / 1000))
            self.hann = np.hanning(self.tone_n)
            self.hann_sum = float(self.hann.sum())
            # 이전 프리셋으로 잡던 동기/패킷 위치를 새 타이밍에 섞지 않는다.
            self.pending = None
            self.verified = False
            self.state = 'search'
            self.predicted_t0 = self.predicted_p0 = None
            self.from_prediction = False
            self.scan_abs = max(self.base, self.base + len(self.buf) - self.tone_n)
        return cfg

    def _new_assembler(self):
        self.assembler = core.PacketAssembler()
        self.blocks = core.BlockMap()               # 블록 칸 상태 (대기/성공/실패/복원)
        self._announced = False

    def clear_message(self):
        """수신 내용만 지우고(소리 스트림 상태는 유지) 새로 시작"""
        with self.lock:
            self.packets = []
            self._new_assembler()

    # --- 입력 ---
    def feed(self, chunk):
        """모노 float 샘플 조각을 넣는다 (-1.0~1.0)."""
        chunk = np.asarray(chunk, dtype=np.float64).ravel()
        if chunk.size == 0:
            return
        with self.lock:
            self.level = float(np.sqrt(np.mean(chunk ** 2)))
            y, self.zi = sosfilt(self.sos, chunk, zi=self.zi)    # 인과 밴드패스 (상태 유지)
            self.buf = np.concatenate((self.buf, y))
            self.total += chunk.size
            self._process()
            self._check_idle()

    def snapshot(self):
        """GUI 용 현재 상태 복사본"""
        with self.lock:
            return {'status': self.assembler.status(), 'packets': list(self.packets),
                    'level': self.level, 'time': self.total / self.fs, 'state': self.state,
                    'history': list(self.history), 'passes': self.pass_count,
                    'blocks': {'states': self.blocks.states_list(), 'total': self.blocks.total}}

    # --- 내부 처리 ---
    IDLE_S = 3.5           # 이 시간 동안 패킷이 없으면 '전송 끝' -> 못 받은 칸을 실패(빨강)로 표시

    def _process(self):
        self._scan_start()                                   # 긴 시작음 감지 (동기 탐색과 별개로 진행)
        for _ in range(100000):
            if self.state == 'search' and self._try_predicted_packet():
                progressed = True
            else:
                progressed = self._search() if self.state == 'search' else self._receive()
            if not progressed:
                break

    def _try_predicted_packet(self):
        """동기 펄스를 놓친 경우, 직전 패킷 주기의 위치에서 한 번 복호화를 시도한다."""
        if self.predicted_t0 is None or self.predicted_p0 is None:
            return False
        t0 = int(self.predicted_t0)
        end = self.base + len(self.buf)
        needed = t0 + core.N_SYMBOLS * self.period + self.tone_n + int(0.008 * self.fs)
        if end < needed:
            return False
        if t0 < self.base or needed > end:
            # 버퍼가 예측 위치를 이미 지나갔다면 더 이상 쓸 수 없는 예상값이다.
            if t0 < self.base:
                self.predicted_t0 = self.predicted_p0 = None
            return False
        p0 = float(self.predicted_p0)
        # 마지막 패킷 뒤 무음에서 가짜 패킷을 만들지 않도록 첫 3개 DTMF를 확인한다.
        # 동기 펄스만 놓친 실제 패킷이라면 데이터음이 남아 행/열 에너지가 나타난다.
        first_starts = [t0 + self.period, t0 + 2 * self.period, t0 + 3 * self.period]
        first_pw = self._tone_powers(first_starts)
        floor = core.TONE_FLOOR_REL * p0
        audible = 0
        for k in range(3):
            rp = np.sort(first_pw[:4, k])[::-1]
            cp = np.sort(first_pw[4:, k])[::-1]
            if (rp[0] >= floor and cp[0] >= floor
                    and rp[0] > 1.05 * max(rp[1], 1e-30)
                    and cp[0] > 1.05 * max(cp[1], 1e-30)):
                audible += 1
        if audible < 2:
            self.predicted_t0 = self.predicted_p0 = None
            return False
        self.predicted_t0 = self.predicted_p0 = None
        self.pending = (t0, p0)
        self.verified = True
        self.from_prediction = True
        self.state = 'receive'
        predicted_idx = self.blocks._predict(t0 / self.fs)
        meta = self.assembler._fec_meta()
        role, _logical_idx = core.classify_fec_wire_index(predicted_idx, meta)
        if meta is None or role == 'data':
            self.events.put(('sync_prediction', t0 / self.fs))
        return True

    def _trim(self):
        """이미 지난 구간의 버퍼를 버려 메모리를 일정하게 유지"""
        keep_from = min(self.scan_abs - 4 * self.hop, self.start_scan_abs)
        # 다음 동기를 못 잡았을 때 예측 위치에서 패킷을 복호화할 수 있도록
        # 그 위치 전의 오디오를 잠시 보존한다 (최대 약 1.2초, 대략 0.2MB 수준).
        if self.predicted_t0 is not None:
            keep_from = min(keep_from, self.predicted_t0 - 2 * self.hop)
        drop = keep_from - self.base
        if drop > 0:
            drop = min(drop, len(self.buf))
            self.buf = self.buf[drop:]
            self.base += drop

    # ---- 긴 시작음(1000Hz) 감지 ----
    START_CONFIRM_FRAMES = 12       # 10ms 홉 기준 약 120ms 동안 시작음을 확인
    START_REARM_FRAMES = 20         # 10ms 홉 기준 약 200ms 연속 비감지 후 재무장
    START_RUN_GAP_TOLERANCE = 3     # 창 경계/필터 지연에 의한 최대 30ms 미검출 허용
    AUTO_CLEAR_REPEAT_BOUNDARY_S = 0.55  # 0.5s 반복 간격과 0.6s 새 전송 간격의 중간값

    def _scan_start(self):
        """Overlapped 50ms analysis windows, stepped every 10ms, detect and re-arm reliably."""
        N, fs, hop = self.start_n, self.fs, self.start_hop
        self.start_scan_abs = max(self.start_scan_abs, self.base)
        end = self.base + len(self.buf)
        n = (end - N - self.start_scan_abs) // hop + 1
        if n <= 0:
            return
        sa = self.start_scan_abs + hop * np.arange(n)
        P = core.goertzel_power(self.buf, sa - self.base, N,
                                [core.START_FREQ, 941.0, 1100.0], fs, self.hann_s)
        amp = 2.0 * np.sqrt(np.maximum(P[0], 0.0)) / self.hann_s_sum
        ref = np.maximum(np.maximum(P[1], P[2]), 1e-20)
        flag = (amp > self.START_MIN_AMP) & (P[0] > self.START_RATIO * ref)

        for k, is_start_tone in enumerate(flag):
            if bool(is_start_tone):
                # Remember the first frame of this run. The old code reported the 12th
                # confirming frame (~110 ms late), which blurred the 500 ms repeated-pass
                # gap and the 600 ms gap between independent transmissions.
                if self.start_run == 0:
                    self.start_run_start_abs = int(sa[k])
                self.start_run += 1
                self.start_gap = 0
                if self.start_run >= self.START_CONFIRM_FRAMES and not self.start_latched:
                    self.start_latched = True
                    onset_abs = self.start_run_start_abs if self.start_run_start_abs is not None else int(sa[k])
                    self._on_start(float(onset_abs) / fs)
            else:
                self.start_gap += 1
                if self.start_gap >= self.START_RUN_GAP_TOLERANCE and not self.start_latched:
                    self.start_run = 0
                    self.start_run_start_abs = None
                if self.start_gap >= self.START_REARM_FRAMES:
                    # The 300ms post-start silence is long enough to re-arm before the next pass.
                    self.start_run = 0
                    self.start_run_start_abs = None
                    self.start_latched = False
        self.start_scan_abs = int(sa[-1]) + hop

    def _on_start(self, t):
        """Handle a confirmed start tone while preserving repeated passes of the same transfer."""
        had_previous = bool(self.packets or self.assembler.good or self.blocks.total or self.history)
        first_start = self.pass_count == 0
        previous_start_sample = self.last_start_onset_sample
        previous_start_pkt_len = self.last_start_pkt_len
        elapsed_from_packet_end = None
        if self.last_packet_end_sample is not None:
            elapsed_from_packet_end = float(t) - (self.last_packet_end_sample / self.fs)

        # A final packet can be missed by the detector. If the old transfer advertised its
        # packet count, estimate the pass end from the previous start-tone onset + preamble
        # + packet_count * packet_duration. A repeated pass begins ~0.5s later; a separate
        # transmission begins ~0.6s later (trailing + leading silence). This works even when
        # the last one or more data packets were not successfully detected.
        estimated_gap = elapsed_from_packet_end
        expected_packets = self.assembler.expected_packets() if previous_start_sample is not None else None
        if previous_start_sample is not None and expected_packets and previous_start_pkt_len:
            estimated_end_sample = (
                int(previous_start_sample)
                + int(round((core.START_TONE_S + core.START_SILENCE_S) * self.fs))
                + int(expected_packets) * int(previous_start_pkt_len)
            )
            gap_from_expected_end = float(t) - (estimated_end_sample / self.fs)
            if -0.10 <= gap_from_expected_end <= 5.0:
                estimated_gap = gap_from_expected_end

        # The packet-count estimate is preferable for FEC/container transfers. Without a
        # known count (raw data), use the observed last-packet time as a conservative fallback
        # and let a conflicting CRC-valid block trigger a second reset path.
        if expected_packets:
            near_new_send_boundary = (estimated_gap is not None and
                                      estimated_gap >= self.AUTO_CLEAR_REPEAT_BOUNDARY_S)
        else:
            near_new_send_boundary = (estimated_gap is not None and
                                      self.AUTO_CLEAR_REPEAT_BOUNDARY_S <= estimated_gap <= 1.05)
        fresh_transmission = first_start or self.idle_marked or near_new_send_boundary
        self.last_start_onset_sample = int(round(float(t) * self.fs))
        self.last_start_pkt_len = int(self.pkt_len)
        if fresh_transmission:
            self.packets = []
            self.history = []
            self._new_assembler()
            self.predicted_t0 = None
            self.predicted_p0 = None
            self.from_prediction = False
            self.pending = None
            self.verified = False
            self.state = 'search'
            self.last_packet_end_sample = None
            self.pass_count = 0
            if had_previous or first_start:
                self.auto_clear_count += 1
                self.events.put(('auto_clear', {'t': t, 'reason': 'new_transmission'}))

        self.pass_count += 1
        self.blocks.new_pass()
        self.last_pkt_total = self.total
        self.idle_marked = False
        self.events.put(('start', t))

    def _check_idle(self):
        """전송이 끝난 것 같으면(패킷이 한동안 없음) 아직 못 받은 칸을 실패로 확정"""
        if (self.blocks.total and not self.idle_marked and self.last_pkt_total is not None
                and (self.total - self.last_pkt_total) / self.fs > self.IDLE_S):
            self.blocks.mark_unseen_failed()
            self.idle_marked = True
            self.events.put(('idle', None))

    # ---- 동기 펄스 탐색 ----
    def _search(self):
        """동기 펄스를 탐색한다. 짧은 잡음 조각은 버리고 끊긴 검출 구간은 이어 붙인다."""
        fs, N, hop = self.fs, self.tone_n, self.hop
        end = self.base + len(self.buf)
        n_win = (end - N - self.scan_abs) // hop + 1
        if n_win <= 0:
            return False
        sa = self.scan_abs + hop * np.arange(n_win)
        freqs = [core.SYNC_FREQ] + core.ROW_FREQS + [self.NOISE_REF]
        P = core.goertzel_power(self.buf, sa - self.base, N, freqs, fs, self.hann)
        ps, plow, pref = P[0], P[1:5].max(axis=0), P[5]
        amp = 2.0 * np.sqrt(np.maximum(ps, 0.0)) / self.hann_sum
        row_ratio = ps / np.maximum(plow, 1e-20)
        noise_ratio = ps / np.maximum(pref, 1e-20)
        flag = ((amp > self.MIN_AMP) & (ps > self.SYNC_ROW_RATIO * plow)
                & (ps > self.NOISE_RATIO * pref))
        hits = np.flatnonzero(flag)
        if hits.size == 0:
            self.scan_abs = int(sa[-1]) + hop
            self._trim()
            return False

        # Audio callback 청크 경계나 실내 반사 때문에 에너지 조건이 1~2회 스캔에서
        # 끊길 수 있다. 6ms 이내의 구멍은 같은 펄스 구간으로 묶는다.
        groups = np.split(hits, np.flatnonzero(np.diff(hits) > 3) + 1)
        valid_groups = []
        trailing_group = None
        for group in groups:
            # 현재 청크의 끝에서 펄스가 아직 진행 중이면 검출 결정을 미룬다.
            # 단, 청크 안쪽에 이미 완성된 후보가 있다면 그 후보부터 처리한다.
            if int(group[-1]) == n_win - 1:
                trailing_group = group
                continue
            # 진짜 20ms 펄스는 20ms 창을 이동할 때 여러 인접 창에 나타난다.
            # 6ms 미만인 짧은 단발성 검출은 잡음으로 취급한다.
            span = int(group[-1]) - int(group[0])
            if span >= 3:
                valid_groups.append(group)

        if not valid_groups:
            if trailing_group is not None:
                self.scan_abs = int(sa[int(trailing_group[0])])
            else:
                self.scan_abs = int(sa[-1]) + hop
            self._trim()
            return False

        group = valid_groups[0]
        run_starts = sa[group]
        coarse = int(run_starts[int(np.argmax(ps[group]))])
        # 필터/스피커/마이크 지연으로 펄스 꼭대기가 몇 ms 이동할 수 있어,
        # 2ms 주변만이 아니라 검출 구간 전체에 걸쳐 피크 위치를 다시 찾는다.
        left = max(self.base, int(run_starts[0]) - 2 * hop)
        right = min(end - N, int(run_starts[-1]) + 2 * hop)
        fine = np.arange(left, right + 1, 4, dtype=np.int64)
        if fine.size == 0:
            self.scan_abs = int(sa[-1]) + hop
            return False
        pf = core.goertzel_power(self.buf, fine - self.base, N, [core.SYNC_FREQ], fs, self.hann)[0]
        k = int(np.argmax(pf))
        self.pending = (int(fine[k]), float(pf[k]))
        self.verified = False
        self.from_prediction = False
        self.state = 'receive'
        return True

    def _tone_powers(self, starts_abs):
        return core.goertzel_power(self.buf, np.asarray(starts_abs) - self.base, self.tone_n,
                                   core.ROW_FREQS + core.COL_FREQS, self.fs, self.hann)

    def _soft_crc_search(self, pw_one, p0, beam_width=1024):
        """주파수 에너지 상위 후보들을 CRC 제약으로 탐색한다.

        실내 반사/마이크 주파수 응답 때문에 한두 DTMF 글자가 잘못 고르는 경우,
        각 글자의 차선 후보까지 고려하고 CRC가 맞는 전체 패킷만 채택한다.
        """
        # pw_one: (8개 주파수, 38개 심볼)
        eps = 1e-30
        floor = max(core.TONE_FLOOR_REL * p0, eps)
        rpow, cpow = pw_one[:4, :], pw_one[4:, :]
        candidates = []
        for k in range(core.N_SYMBOLS):
            rr = rpow[:, k]
            cc = cpow[:, k]
            rsum = float(np.sum(rr)) + eps
            csum = float(np.sum(cc)) + eps
            rtop = np.argsort(rr)[-3:][::-1]
            ctop = np.argsort(cc)[-3:][::-1]
            rmax, cmax = float(rr[rtop[0]]), float(cc[ctop[0]])
            weak_penalty = min(0.0, float(np.log((rmax + eps) / floor)))                            + min(0.0, float(np.log((cmax + eps) / floor)))
            opts = []
            for r in rtop:
                for c in ctop:
                    h = core.KEY_TO_HEX[core.KEYPAD[int(r)][int(c)]]
                    score = (float(np.log((rr[int(r)] + eps) / rsum))
                             + float(np.log((cc[int(c)] + eps) / csum)) + weak_penalty)
                    opts.append((h, score))
            # 각 위치에서 유망한 조합 6개만 유지 (상위 행/열 조합에 오류가 있어도 대안 제공)
            opts.sort(key=lambda z: z[1], reverse=True)
            candidates.append(opts[:6])

        beam = [(0.0, '')]
        for opts in candidates:
            expanded = [(score + add, text + ch) for score, text in beam for ch, add in opts]
            expanded.sort(key=lambda z: z[0], reverse=True)
            beam = expanded[:beam_width]

        best = None
        for score, packet in beam:
            ok, seq, data = core.parse_packet_string(packet)
            if ok and (best is None or score > best[0]):
                best = (score, packet, seq, data)
        return best

    def _decode_packet_timing_search(self, t0, p0):
        """全局タイミングとシンボル別タイミングを CRC で検証する。"""
        max_offset_ms = max(2, min(8, (core.TONE_MS - core.DEMOD_MS) // 2 + 3))
        offsets_ms = np.arange(-max_offset_ms, max_offset_ms + 1, 1, dtype=float)
        offsets = np.rint(offsets_ms * self.fs / 1000.0).astype(np.int64)
        sym = np.arange(1, core.N_SYMBOLS + 1, dtype=np.int64)
        starts_matrix = t0 + self.period * sym[None, :] + offsets[:, None]
        valid = ((starts_matrix >= self.base).all(axis=1)
                 & ((starts_matrix + self.tone_n) <= self.base + len(self.buf)).all(axis=1))
        offsets_ms = offsets_ms[valid]
        starts_matrix = starts_matrix[valid]
        if len(starts_matrix) == 0:
            return '?' * core.N_SYMBOLS, False, -1, b'', 0.0, False

        starts_rel = (starts_matrix - self.base).reshape(-1)
        pw_all = core.goertzel_power(self.buf, starts_rel, self.tone_n,
                                     core.ROW_FREQS + core.COL_FREQS, self.fs, self.hann)
        pw_all = pw_all.reshape(8, len(offsets_ms), core.N_SYMBOLS).transpose(1, 0, 2)
        eps = 1e-30
        decoded = []
        for oi, powers in enumerate(pw_all):
            rp, cp = powers[:4, :], powers[4:, :]
            r_idx, c_idx = rp.argmax(axis=0), cp.argmax(axis=0)
            r_best = rp[r_idx, np.arange(core.N_SYMBOLS)]
            c_best = cp[c_idx, np.arange(core.N_SYMBOLS)]
            r_sort, c_sort = np.sort(rp, axis=0), np.sort(cp, axis=0)
            score = float(np.sum(np.log((r_sort[-1] + eps) / (r_sort[-2] + eps))
                                 + np.log((c_sort[-1] + eps) / (c_sort[-2] + eps))))
            chars = []
            for k in range(core.N_SYMBOLS):
                if r_best[k] < core.TONE_FLOOR_REL * p0 or c_best[k] < core.TONE_FLOOR_REL * p0:
                    chars.append('?')
                else:
                    chars.append(core.KEY_TO_HEX[core.KEYPAD[int(r_idx[k])][int(c_idx[k])]])
            packet = ''.join(chars)
            ok, seq_no, data = core.parse_packet_string(packet)
            decoded.append((bool(ok), score, packet, seq_no, data, float(offsets_ms[oi]), oi))

        valid_hard = [item for item in decoded if item[0]]
        if valid_hard:
            ok, _score, packet, seq_no, data, offset_ms, _oi = max(valid_hard, key=lambda z: z[1])
            return packet, ok, seq_no, data, offset_ms, False

        # タイミングはマイクや反射でシンボルごとに数msずれることがある。
        # 全38文字に同じ補正値を使う方式で失敗した時だけ、各シンボルの窓を
        # ±8ms走査し、行・列のエネルギーとピークの明瞭さが最大の窓を使う。
        local_ms = np.arange(-max_offset_ms, max_offset_ms + 1, 1, dtype=float)
        local_samples = np.rint(local_ms * self.fs / 1000.0).astype(np.int64)
        expected = t0 + self.period * sym[None, :]
        local_starts = expected + local_samples[:, None]
        if ((local_starts >= self.base).all()
                and ((local_starts + self.tone_n) <= self.base + len(self.buf)).all()):
            lrel = (local_starts - self.base).reshape(-1)
            lpw = core.goertzel_power(self.buf, lrel, self.tone_n,
                                      core.ROW_FREQS + core.COL_FREQS, self.fs, self.hann)
            lpw = lpw.reshape(8, len(local_ms), core.N_SYMBOLS)
            lrp, lcp = lpw[:4], lpw[4:]
            rsort = np.sort(lrp, axis=0)
            csort = np.sort(lcp, axis=0)
            # 強さだけでなく、同じ音群の2位との差も考慮する。
            local_metric = (np.log(rsort[-1] + eps) + np.log(csort[-1] + eps)
                            + 0.5 * np.log((rsort[-1] + eps) / (rsort[-2] + eps))
                            + 0.5 * np.log((csort[-1] + eps) / (csort[-2] + eps)))
            best_local = np.argmax(local_metric, axis=0)
            pw_local = np.stack([lpw[:, best_local[k], k] for k in range(core.N_SYMBOLS)], axis=1)
            chars = []
            for k in range(core.N_SYMBOLS):
                rp, cp = pw_local[:4, k], pw_local[4:, k]
                r, c = int(np.argmax(rp)), int(np.argmax(cp))
                if rp[r] < core.TONE_FLOOR_REL * p0 or cp[c] < core.TONE_FLOOR_REL * p0:
                    chars.append('?')
                else:
                    chars.append(core.KEY_TO_HEX[core.KEYPAD[r][c]])
            packet = ''.join(chars)
            ok, seq_no, data = core.parse_packet_string(packet)
            local_offset_ms = float(np.median(local_ms[best_local]))
            if ok:
                return packet, True, seq_no, data, local_offset_ms, False
        else:
            pw_local = None
            best_local = None

        # Hard 판독 실패 시 상위 전역 타이밍 3개와 심볼별 타이밍 후보 1개에 대해
        # 차선의 행/열까지 CRC 제약 탐색을 한다. 잘못된 심볼은 CRC를 통과할 수 없다.
        candidate_offsets = sorted(decoded, key=lambda z: z[1], reverse=True)[:3]
        soft_best = None
        for _ok, _score, _packet, _seq, _data, offset_ms, oi in candidate_offsets:
            found = self._soft_crc_search(pw_all[oi], p0, beam_width=1024)
            if found is not None and (soft_best is None or found[0] > soft_best[0]):
                soft_best = (found[0], found[1], found[2], found[3], float(offset_ms))
        if pw_local is not None:
            found = self._soft_crc_search(pw_local, p0, beam_width=1024)
            if found is not None and (soft_best is None or found[0] > soft_best[0]):
                soft_best = (found[0], found[1], found[2], found[3],
                             float(np.median(local_ms[best_local])))
        if soft_best is not None:
            _score, packet, seq_no, data, offset_ms = soft_best
            return packet, True, seq_no, data, offset_ms, True

        _ok, _score, packet, seq_no, data, offset_ms, _oi = max(decoded, key=lambda z: z[1])
        return packet, False, seq_no, data, offset_ms, False

    def _receive(self):
        """동기 펄스 이후 38개 DTMF 를 판독한다. 약한 첫 글자 하나로 패킷을 버리지 않는다."""
        t0, p0 = self.pending
        N, period = self.tone_n, self.period
        was_prediction = self.from_prediction
        previous_expected = self.predicted_t0
        previous_expected_p0 = self.predicted_p0
        matched_expected = (previous_expected is not None and
                            abs(int(t0) - int(previous_expected)) <= int(0.020 * self.fs))
        end = self.base + len(self.buf)

        # 첫 두 심볼 중 한 글자만 약해도 전체 패킷을 버리던 기존 검증을 완화한다.
        # 1850Hz는 DTMF 대역 밖에 있으므로, 3글자 중 2글자 이상에서 유효한 DTMF 쌍이
        # 보이면 수신을 계속한다. 약한 첫 글자는 최종 CRC 검증/소프트 판독에 맡긴다.
        if not self.verified:
            if end < t0 + 3 * period + N:
                return False
            pw = self._tone_powers([t0 + period, t0 + 2 * period, t0 + 3 * period])
            floor = core.TONE_FLOOR_REL * p0
            clear_count = 0
            for k in range(3):
                rp = np.sort(pw[:4, k])[::-1]
                cp = np.sort(pw[4:, k])[::-1]
                clear = (rp[0] >= floor and cp[0] >= floor
                         and rp[0] > self.CLEAR_RATIO * max(rp[1], 1e-30)
                         and cp[0] > self.CLEAR_RATIO * max(cp[1], 1e-30))
                clear_count += int(clear)
            if clear_count < 2:
                self.scan_abs = max(self.scan_abs, t0 + N)
                self.state, self.pending = 'search', None
                self._trim()
                return True
            self.verified = True

        # 전체 38개 글자까지 수신하고, 직후의 10ms 공백을 기다리지 않고 판독한다.
        # 最後のシンボルの窓を +8ms まで動かせるよう、数ms余分に待つ。
        if end < t0 + core.N_SYMBOLS * period + N + int(0.008 * self.fs):
            return False
        pkt, ok, seq, data, timing_ms, soft_used = self._decode_packet_timing_search(t0, p0)

        info = {'t': t0 / self.fs, 'seq': seq if ok else -1, 'ok': bool(ok),
                'raw': pkt, 'timing_ms': timing_ms, 'soft': soft_used}
        predicted_idx = self.blocks._predict(info['t'])
        idx = self._add_packet(seq, data) if ok else None
        if ok:
            wire_idx = idx
        elif predicted_idx is not None:
            wire_idx = predicted_idx
        elif self.blocks.anchor is None:
            wire_idx = self.blocks.last_idx + 1
        else:
            wire_idx = 0
        meta = self.assembler._fec_meta()
        role, logical_idx = core.classify_fec_wire_index(wire_idx, meta)
        info['wire_idx'] = wire_idx
        info['fec_role'] = role
        info['logical_idx'] = logical_idx
        info['fec_mode'] = meta['mode'] if meta else 0
        info['fec_internal'] = bool(meta is not None and role != 'data')
        self.packets.append(info)
        # FEC header/parity packets stay internal: don't add them to the visible per-data-block log.
        if not info['fec_internal']:
            self.events.put(('packet', info))
        self.blocks.on_packet(info['t'], ok, idx)
        exp = self.assembler.expected_packets()
        if exp:
            self.blocks.set_total(exp)
        self.last_pkt_total = self.total
        self.last_packet_end_sample = int(t0 + self.pkt_len)
        self.idle_marked = False

        # CRC가 실패했더라도 동기와 첫 3글자가 일관되게 잡힌 프레임이면 다음 주기를
        # 예측한다. 다만 기존 예측 위치와 멀리 떨어진 가짜 동기는 기존 예측을 유지한다.
        if was_prediction or matched_expected or previous_expected is None or ok:
            self.predicted_t0 = int(t0 + self.pkt_len)
            self.predicted_p0 = float(p0)
        else:
            self.predicted_t0 = previous_expected
            self.predicted_p0 = previous_expected_p0
        self.from_prediction = False

        # CRC 실패 후보가 진짜 동기 펄스가 아닐 수도 있다. 패킷 한 길이만큼 건너뛰지
        # 않고 바로 뒤부터 다시 찾아, 그 사이에 있던 정상 동기를 놓치지 않는다.
        if ok:
            self.scan_abs = max(self.base, t0 + self.pkt_len - int(0.025 * self.fs))
        else:
            self.scan_abs = max(self.base, t0 + int(0.030 * self.fs))
        self.state, self.pending = 'search', None
        self._trim()
        return True

    def _add_packet(self, seq, data):
        """CRC 정상 패킷을 조립기에 넣고 절대 인덱스를 반환. 같은 칸에 '다른 내용'이 오면 새 메시지로 간주."""
        asm = self.assembler
        ab = asm.resolve(seq)
        if ab in asm.good and asm.good[ab] != data:
            old_status = asm.status()
            # A start detector can miss a fresh start or its timing can look like a repeat.
            # A conflicting CRC-valid block is definitive evidence of a different message.
            self.packets = []
            self.history = []
            self._new_assembler()
            self.predicted_t0 = None
            self.predicted_p0 = None
            self.from_prediction = False
            self.auto_clear_count += 1
            self.pass_count = 1  # the current start tone belongs to the newly detected transfer
            self.events.put(('auto_clear', {'reason': 'conflicting_valid_block'}))
            self.events.put(('reset', old_status))
            asm = self.assembler
        ab = asm.add(seq, data)
        st = asm.status()
        if st['complete'] and not self._announced:
            self._announced = True
            self.history.append(st)
            self.events.put(('message', st))
        return ab


# ----------------------------------------------------------------------------
# 2. 실시간 수신 세션 (마이크 / 시뮬레이션 -> 디코더 + 링버퍼)
# ----------------------------------------------------------------------------
def list_devices():
    """(입력 장치 목록, 출력 장치 목록) — 각 원소는 (인덱스, 이름). sounddevice 없으면 빈 목록."""
    if not HAVE_SD:
        return [], []
    ins, outs = [], []
    try:
        for i, d in enumerate(sd.query_devices()):
            if d['max_input_channels'] > 0:
                ins.append((i, d['name']))
            if d['max_output_channels'] > 0:
                outs.append((i, d['name']))
    except Exception:
        pass
    return ins, outs


class LiveSession:
    RING_S = 12.0          # 워터폴에 보여 줄 최근 구간 (초)
    CHUNK_S = 0.025        # 짧은 오디오 청크로 실시간 워터폴과 디코더의 입력 지연을 줄임

    def __init__(self, fs=core.FS):
        self.fs = fs
        self.decoder = StreamDecoder(fs)
        self.q = queue.Queue()  # 입력 청크를 조용히 버리지 않도록 무제한 큐 사용
        self.ring = np.zeros(int(self.RING_S * fs))
        self.ring_total = 0
        self.ring_generation = 0
        self._ring_write_pos = 0
        self._ring_lock = threading.Lock()
        self.stream = None
        self.mode = None                    # None / 'mic' / 'sim'
        self._alive = True
        self.error = None
        threading.Thread(target=self._worker, daemon=True).start()

    # --- 입력원 ---
    def start_mic(self, device=None):
        if not HAVE_SD:
            raise RuntimeError("마이크 입력에는 sounddevice 가 필요합니다.\n터미널에서: pip install sounddevice")
        self.stop()
        self.reset()
        self.error = None

        def cb(indata, frames, t, status):
            # 오디오 callback에서는 절대 block하지 않는다. 처리 지연 시 가장 오래된
            # 청크 하나만 버리고 최신 청크를 넣어, 장치 callback이 정지하지 않게 한다.
            if status:
                self.error = RuntimeError(f"오디오 입력 경고: {status}")
            chunk = indata[:, 0].astype(np.float64, copy=True)
            try:
                self.q.put_nowait(chunk)
            except queue.Full:
                try:
                    self.q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self.q.put_nowait(chunk)
                except queue.Full:
                    self.error = RuntimeError("실시간 디코더가 입력을 따라가지 못합니다. 처리 부하를 줄여 주세요.")

        self.stream = sd.InputStream(samplerate=self.fs, channels=1, dtype='float32', device=device,
                                     blocksize=int(self.fs * self.CHUNK_S), callback=cb)
        self.stream.start()
        self.mode = 'mic'

    def stop(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        self.mode = None

    def close(self):
        self.stop()
        self._alive = False

    def reset(self):
        """디코더/링버퍼 완전 초기화"""
        while not self.q.empty():
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        self.decoder.reset_all()
        with self._ring_lock:
            self.ring[:] = 0
            self.ring_total = 0
            self._ring_write_pos = 0
            self.ring_generation += 1

    # --- 내부 작업 스레드 ---
    def _worker(self):
        while self._alive:
            try:
                c = self.q.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                with self._ring_lock:
                    capacity = len(self.ring)
                    k = min(len(c), capacity)
                    data = c[-k:]
                    if k >= capacity:
                        self.ring[:] = data
                        self._ring_write_pos = 0
                    else:
                        pos = self._ring_write_pos
                        first = min(k, capacity - pos)
                        self.ring[pos:pos + first] = data[:first]
                        remaining = k - first
                        if remaining:
                            self.ring[:remaining] = data[first:]
                        self._ring_write_pos = (pos + k) % capacity
                    self.ring_total += len(c)
                clear_generation = self.decoder.auto_clear_count
                self.decoder.feed(c)
                # 시작음이 확정된 바로 그 오디오 청크에서 과거 워터폴 기록을 비운다.
                # GUI 타이머가 늦어도 새 전송 파형을 뒤늦게 지우지 않는다.
                if self.decoder.auto_clear_count != clear_generation:
                    with self._ring_lock:
                        self.ring[:] = 0.0
                        self.ring_generation += 1
            except Exception as e:                   # noqa
                self.error = e

    def clear_ring_history(self):
        """GUI 자동 초기화용: 시간축은 유지하고 기존 워터폴 샘플만 지운다."""
        with self._ring_lock:
            self.ring[:] = 0.0
            self.ring_generation += 1

    def _copy_ring_range_locked(self, start_sample, end_sample):
        """Copy absolute sample range [start, end) in chronological order."""
        end_sample = int(end_sample)
        filled = min(end_sample, len(self.ring))
        oldest = max(0, end_sample - filled)
        actual_start = max(oldest, min(int(start_sample), end_sample))
        count = end_sample - actual_start
        if count <= 0:
            return np.empty(0, dtype=np.float64), actual_start

        if filled < len(self.ring):
            # Before the buffer fills, sample zero is at physical index zero.
            return self.ring[actual_start:end_sample].copy(), actual_start

        # Once full, _ring_write_pos points to the oldest sample.
        offset = (self._ring_write_pos + (actual_start - oldest)) % len(self.ring)
        first = min(count, len(self.ring) - offset)
        if first == count:
            return self.ring[offset:offset + count].copy(), actual_start
        result = np.empty(count, dtype=np.float64)
        result[:first] = self.ring[offset:offset + first]
        result[first:] = self.ring[:count - first]
        return result, actual_start

    def get_ring_delta(self, start_sample=0):
        """Return new samples since an absolute sample index, plus timeline metadata."""
        with self._ring_lock:
            end = int(self.ring_total)
            chunk, actual_start = self._copy_ring_range_locked(start_sample, end)
            return chunk, actual_start, end, int(self.ring_generation)

    def get_ring(self):
        """(recent chronological audio copy, start time in seconds, total time in seconds)"""
        with self._ring_lock:
            end = int(self.ring_total)
            filled = min(end, len(self.ring))
            start = max(0, end - filled)
            sig, start = self._copy_ring_range_locked(start, end)
            return sig, start / self.fs, end / self.fs



# ----------------------------------------------------------------------------
# 3. 실시간 송신 (WAV 저장 없이 바로 재생)
# ----------------------------------------------------------------------------
class Player:
    """WAV 파일 재생 도우미 (sounddevice 가 없을 때의 대체 수단)."""

    def __init__(self):
        self.proc = None

    def play(self, path):
        self.stop()
        try:
            if sys.platform.startswith("win"):
                import winsound
                winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
                return True
            if sys.platform == "darwin":
                self.proc = subprocess.Popen(["afplay", path])
                return True
            for cmd in (["paplay"], ["aplay", "-q"], ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]):
                if shutil.which(cmd[0]):
                    self.proc = subprocess.Popen(cmd + [path])
                    return True
        except Exception:
            pass
        return False

    def stop(self):
        try:
            if sys.platform.startswith("win"):
                import winsound
                winsound.PlaySound(None, winsound.SND_PURGE)
            elif self.proc and self.proc.poll() is None:
                self.proc.terminate()
        except Exception:
            pass
        self.proc = None


class LiveTransmitter:
    """
    패킷을 하나씩 소리로 만들어 만드는 즉시 스피커로 내보낸다 (스트리밍 송신).
    진행 상황은 self.sent / self.total / self.done / self.error 로 읽는다 (GUI 가 주기적으로 확인).

      - sounddevice 있음 : OutputStream.write() 로 패킷 단위 직접 재생 (WAV 파일 불필요)
      - sounddevice 없음 : 임시 WAV 를 만들어 OS 재생기로 재생하고, 진행률은 시간으로 추정
    """

    def __init__(self, fs=core.FS, volume=0.8):
        self.fs = fs
        self.volume = volume
        self.player = Player()
        self._stop = threading.Event()
        self.thread = None
        self.reset_state()

    def reset_state(self):
        self.sent, self.total, self.total_s = 0, 0, 0.0
        self.done, self.error = True, None
        self.t_start = None
        self.mode = None

    @property
    def active(self):
        return self.thread is not None and self.thread.is_alive()

    def stop(self):
        self._stop.set()
        self.player.stop()

    def send(self, payload: bytes, device=None, start_tone=True, repeat=1, only=None, fec_mode=0, fec_percent=0):
        """
        payload : core.prepare_payload() 로 만든 전송 데이터 (컨테이너 포함 가능)
        repeat  : 전체 반복 횟수,  only : 이 패킷 인덱스들만 전송 (누락분 재전송)
        """
        if self.active:
            return
        packets = core.select_packets(core.bytes_to_packet_strings(payload, fec_mode=fec_mode, fec_percent=fec_percent), only)
        repeat = max(1, int(repeat))
        self.sent, self.total = 0, len(packets) * repeat
        self.total_s = core.transmission_seconds(len(packets), repeat, start_tone)
        self.done, self.error = False, None
        self._stop.clear()
        self.t_start = time.time()
        self.thread = threading.Thread(target=self._run, args=(packets, device, start_tone, repeat), daemon=True)
        self.thread.start()

    def _run(self, packets, device, start_tone, repeat):
        fs, vol = self.fs, self.volume
        parts = core.iter_transmission(packets, fs, start_tone, repeat)
        try:
            if HAVE_SD:
                self.mode = 'stream'
                with sd.OutputStream(samplerate=fs, channels=1, dtype='float32', device=device) as st:
                    for i, (kind, audio) in enumerate(parts):
                        if self._stop.is_set():
                            st.abort()
                            break
                        if i == 1:
                            self.t_start = time.time()                       # 앞 무음 이후 = 신호 시작
                        st.write((audio * vol).astype(np.float32))           # 만들자마자 바로 재생
                        if kind == 'packet':
                            self.sent += 1
            else:
                self.mode = 'file'
                chunks, ends, pos = [], [], 0
                for kind, audio in parts:                                    # 임시 WAV 로 만들어 OS 재생기 사용
                    chunks.append(audio)
                    pos += len(audio)
                    if kind == 'packet':
                        ends.append(pos / fs)
                sig = np.concatenate(chunks) * vol
                fd, path = tempfile.mkstemp(suffix=".wav")
                os.close(fd)
                wavfile.write(path, fs, np.clip(sig * 32767, -32768, 32767).astype(np.int16))
                if not self.player.play(path):
                    raise RuntimeError("No supported audio playback device is available. Check your Windows sound settings.")
                play_t0 = time.time()
                self.t_start = play_t0 + core.LEAD_S
                total = len(sig) / fs
                while time.time() - play_t0 < total and not self._stop.is_set():
                    time.sleep(0.05)
                    el = time.time() - play_t0
                    self.sent = sum(1 for e in ends if e <= el)
                if self._stop.is_set():
                    self.player.stop()
                else:
                    self.sent = len(ends)
                try:
                    os.remove(path)
                except OSError:
                    pass
        except Exception as e:                       # noqa
            self.error = e
        finally:
            self.done = True
