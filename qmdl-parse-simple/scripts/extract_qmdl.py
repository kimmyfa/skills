#!/usr/bin/env python3
"""Extract audio streams and modem log text from a Qualcomm SPF .qmdl dump.

Standalone: only depends on numpy (stdlib otherwise). No scat/QXDM needed.

Format model (validated against reference QCAT-style extractions):
  - A .qmdl file is a byte stream of DIAG frames delimited by 0x7E, HDLC
    escaped (0x7D 5E -> 0x7E, 0x7D 5D -> 0x7D), each frame ends with a CRC16.
  - DIAG_LOG_F (cmd 0x10) packets carry the audio/spf streams.
    Packet header (16 B): cmd 0x10, reserved, len1, len2, lid, qxdm_ts.
    Inside the payload the first 88 bytes are an SPF header:
        +0  u32  header length (0x58 = 88)
        +4  u32  module id            (file token, e.g. 0x100732C2)
        +8  u32  sequence counter
        +12 u16  payload length (bytes after the 88-byte header)
        +44 u32  PORT id              -> file token 0x410/0x411 ...
        +48 u16  sample rate
        +52 u16  channel count
        +54 u16  bits per sample (16)
    The audio payload (88 B .. end) is int16 LE, laid out as per-packet
    CHANNEL BLOCKS: channel c occupies payload[c*per : (c+1)*per] where
    per = samples//channels. Concatenate the same channel across packets.
    Each per-channel block is one 10 ms frame (sr/100 samples). Do NOT
    "deinterleave" channel-major: that aliases the signal and raises pitch
    (sounds speeded-up / transposed).
  - DIAG_EXT_MSG_F (cmd 0x79) packets carry text modem logs; decoded with a
    printf-style format and written to {qmdl}.messages.txt.

Only 16 kHz streams are ever exported (48k and other rates are skipped).

Modes:
  -1 (default) : merged 8-channel 16 kHz wav only -> {qmdl stem}_8ch.wav.
                 Channel layout:
                   ch1 = 0x1532 0x410 0x1   ch5 = 0x1532 0x411 0x3
                   ch2 = 0x1532 0x410 0x2   ch6 = 0x1532 0x411 0x4
                   ch3 = 0x1532 0x411 0x1   ch7 = 0x1533 0x410 0x1
                   ch4 = 0x1532 0x411 0x2   ch8 = 0x1533 0x410 0x2
                 Channels are aligned to the longest channel: any channel
                 shorter than that is zero-padded at the end, channels missing
                 from the file are zero-filled (with a warning).
  -2           : per-channel 16k mono wavs only for the configured streams:
                   0x1532 port 0x410  (all channels)
                   0x1531 port 0x411  (all channels)
                   0x1533 port 0x410  (all channels)
  -3           : {qmdl}.messages.txt only (decoded 0x79 EXT_MSG text logs).
"""
import sys, struct, collections, os, wave, re, datetime

import numpy as np

WANT = {0x1531, 0x1532, 0x1533}

# Mode 2: only these (lid, port) streams are exported, at 16 kHz only.
MODE2_STREAMS = {
    (0x1532, 0x410),
    (0x1531, 0x411),
    (0x1533, 0x410),
}

# Merged 8-channel layout: output channel n <- (lid, port, ch) of the stream.
MERGE_CHANNELS = (
    (0x1532, 0x410, 1),
    (0x1532, 0x410, 2),
    (0x1532, 0x411, 1),
    (0x1532, 0x411, 2),
    (0x1532, 0x411, 3),
    (0x1532, 0x411, 4),
    (0x1533, 0x410, 1),
    (0x1533, 0x410, 2),
)
MERGE_SR = 16000

# Preferred module id per (lid, port); if it isn't found in the file we fall
# back to any 16k stream with the same (lid, port) and enough channels.
PREF_MERGE_MOD = {
    (0x1532, 0x410): 0x10073282,
    (0x1532, 0x411): 0x100732C2,
    (0x1533, 0x410): 0x10073081,
}

KNOWN_SR = {8000, 11025, 16000, 22050, 24000, 32000, 44100, 48000, 88200, 96000}

CFMT = re.compile(r'(%(?:(?:[-+0 #]{0,5})(?:\d+|\*)?(?:\.(?:\d+|\*))?(?:h|l|ll|w|I|I32|I64)?[duxX])|%%)')
CFMT_NUMS = re.compile(r'%((?:[-+0 #]{0,5})(?:\d+|\*)?(?:\.(?:\d+|\*))?)(?:h|l|ll|w|I|I32|I64)?[duxX]')

QXDM_EPOCH = datetime.datetime(1980, 1, 6, 0, 0, 0, tzinfo=datetime.timezone.utc)


def unwrap(arr):
    t = arr.replace(b'\x7d\x5e', b'\x7e')
    t = t.replace(b'\x7d\x5d', b'\x7d')
    return t


def parse_qxdm_ts(ts):
    ts_upper = ts >> 16
    ts_lower = ts & 0xffff
    try:
        delta = datetime.timedelta(seconds=ts_upper * 1.25 + ts_lower * (1 / 40960))
        return QXDM_EPOCH + delta
    except OverflowError:
        return datetime.datetime.now(tz=datetime.timezone.utc)


def format_ext_msg(pkt_body):
    if len(pkt_body) < 20:
        return None
    _cmd, _ts_type, num_args, _drop = struct.unpack('<BBBB', pkt_body[0:4])
    ts = struct.unpack('<Q', pkt_body[4:12])[0]
    line_no = struct.unpack('<H', pkt_body[12:14])[0]
    subsys = struct.unpack('<H', pkt_body[14:16])[0]
    n_args = max(0, min(num_args, 16))
    pkt_args = list(struct.unpack('<{}L'.format(n_args), pkt_body[20:20 + 4 * n_args]))
    rest = pkt_body[20 + 4 * n_args:]
    rest = rest.rstrip(b'\0').rsplit(b'\0', maxsplit=1)
    if len(rest) == 2:
        src_fname, log_content = rest[1], rest[0]
    else:
        src_fname, log_content = b'', rest[0]
    log_content = log_content.decode(errors='backslashreplace')

    fmt_strs = CFMT.findall(log_content)
    pyfmt = CFMT.sub('{}', log_content)
    vals = []
    i = 0
    if len(pkt_args) == len(fmt_strs):
        for fs in fmt_strs:
            m = CFMT_NUMS.match(fs)
            fmt_num = m.group(1) if m else ''
            if fs == '%%':
                vals.append('%')
            else:
                v = pkt_args[i]
                if fs[-1] in 'xX':
                    vals.append(('{:' + fmt_num + fs[-1] + '}').format(v))
                elif fs[-1] == 'd':
                    if v > 2147483648:
                        v = -(4294967296 - v)
                    vals.append(('{:' + fmt_num + '}').format(v))
                else:
                    vals.append(('{:' + fmt_num + '}').format(v))
            i += 1
        try:
            log_content = pyfmt.format(*vals)
        except Exception:
            pass
    return '{} [subsys={} line={} {}] {}'.format(
        parse_qxdm_ts(ts).strftime('%Y-%m-%d %H:%M:%S.%f'), subsys, line_no,
        src_fname.decode(errors='replace'), log_content)


def channel_block(pkts, ch):
    """Build the channel-block matrix (npkts*per x ch) for a stream group."""
    per = pkts[0].size // ch
    full = np.zeros((len(pkts) * per, ch), dtype=np.int64)
    for i, p in enumerate(pkts):
        full[i * per:(i + 1) * per] = p[:per * ch].reshape(ch, per).T
    return full


def read_frames(qmdl):
    """Yield unwrapped frame bytes (CRC stripped) from the qmdl file."""
    with open(qmdl, 'rb') as f:
        old = b''
        while True:
            buf = f.read(0x100000)
            if not buf:
                break
            buf = old + buf
            parts = buf.split(b'\x7e')
            if buf[-1] != 0x7e:
                old = parts.pop()
            else:
                old = b''
            for pk in parts:
                if len(pk) < 30:
                    continue
                try:
                    u = unwrap(pk)
                except Exception:
                    continue
                if len(u) < 32:
                    continue
                yield u[:-2]  # strip CRC16


def write_messages(base, outdir, msgs):
    if msgs:
        msg_path = os.path.join(outdir, base + '.messages.txt')
        with open(msg_path, 'w') as f:
            f.write('\n'.join(msgs) + '\n')
        print('wrote %d log lines -> %s' % (len(msgs), os.path.basename(msg_path)))
    else:
        print('no 0x79 messages found')


def write_merged_8ch(base, outdir, groups):
    out_chs = []
    for out_ch, (mlid, mport, mch) in enumerate(MERGE_CHANNELS, start=1):
        cands = [g for g in groups
                 if g[0] == mlid and g[3] == mport and g[4] == MERGE_SR and g[5] >= mch]
        if not cands:
            out_chs.append(None)
            print('[warn] merged ch%d (lid=%#x port=%#x ch=0x%x): no matching stream -> zero-filled'
                  % (out_ch, mlid, mport, mch))
            continue
        pref = PREF_MERGE_MOD.get((mlid, mport))
        if pref is not None:
            p = [g for g in cands if g[2] == pref]
            if p:
                cands = p
        g = max(cands, key=lambda g: len(groups[g]))
        _lid, _plen, _mod, _port, _sr, ch = g
        out_chs.append(channel_block(groups[g], ch)[:, mch - 1])

    valid = [c for c in out_chs if c is not None]
    if not valid:
        print('no streams available for merged 8ch wav')
        return
    maxlen = max(c.size for c in valid)
    merged = np.zeros((maxlen, len(MERGE_CHANNELS)), dtype=np.int16)
    for i, c in enumerate(out_chs):
        if c is not None:
            n = min(c.size, maxlen)
            merged[:n, i] = np.clip(c[:n], -32768, 32767).astype(np.int16)
    stem = base[:-5] if base.lower().endswith('.qmdl') else base
    mpath = os.path.join(outdir, stem + '_8ch.wav')
    with wave.open(mpath, 'wb') as w:
        w.setnchannels(len(MERGE_CHANNELS))
        w.setsampwidth(2)
        w.setframerate(MERGE_SR)
        w.writeframes(merged.tobytes())
    missing = sum(1 for c in out_chs if c is None)
    padded = sum(1 for c in out_chs if c is not None and c.size < maxlen)
    print('wrote merged 8ch wav -> %s (%d frames, %.1fs, %d channel(s) missing, %d channel(s) tail-zero-padded)'
          % (os.path.basename(mpath), maxlen, maxlen / MERGE_SR, missing, padded))


def write_stream_wavs(base, outdir, groups, want_pairs):
    written = 0
    for key, pkts in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if len(pkts) < 2:
            continue
        lid, plen, mod, port, sr, ch = key
        if sr != MERGE_SR or (lid, port) not in want_pairs:
            continue
        mods = '0x%08X' % mod
        ports = '0x%x' % port
        srk = '16k'
        full = channel_block(pkts, ch)
        for c in range(ch):
            seg = full[:, c]
            path = os.path.join(outdir, '%s.%04x.pcm.%s.%s.0x%x.tx.%s.wav' % (
                base, lid, mods, ports, c + 1, srk))
            with wave.open(path, 'wb') as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(sr)
                w.writeframes(seg.astype('<i2').tobytes())
            written += 1
        per = pkts[0].size // ch
        print('lid=0x%04x mod=0x%08x port=0x%04x plen=0x%04x sr=%d ch=%d npkts=%d dur=%.1fs' % (
            lid, mod, port, plen, sr, ch, len(pkts), len(pkts) * per / sr))
    print('wrote %d wav files -> %s (mode: 2)' % (written, outdir))


def extract(qmdl, outdir, mode=1):
    base = os.path.basename(qmdl)
    groups = collections.defaultdict(list)
    msgs = []

    for b in read_frames(qmdl):
        cmd = b[0]
        if cmd == 0x79:
            if mode == 3:
                line = format_ext_msg(b)
                if line:
                    msgs.append(line)
            continue
        if mode == 3 or cmd != 0x10:
            continue
        lid = struct.unpack('<H', b[6:8])[0]
        if lid not in WANT:
            continue
        body = b[16:]
        plen = struct.unpack('<H', body[12:14])[0]
        if plen < 32 or plen % 2 or len(body) < 88 + plen:
            continue
        mod = struct.unpack('<I', body[4:8])[0]
        port = struct.unpack('<I', body[44:48])[0]
        sr = struct.unpack('<H', body[48:50])[0]
        ch = struct.unpack('<H', body[52:54])[0]
        if sr not in KNOWN_SR:
            sr = 16000 if plen < 4000 else 48000
        if ch < 1 or ch > 32:
            ch = 1
        s16 = np.frombuffer(body[88:88 + plen], dtype='<i2').astype(np.int64)
        per = s16.size // ch
        if per < 1:
            continue
        groups[(lid, plen, mod, port, sr, ch)].append(s16)

    os.makedirs(outdir, exist_ok=True)

    if mode == 3:
        write_messages(base, outdir, msgs)
    elif mode == 2:
        write_stream_wavs(base, outdir, groups, MODE2_STREAMS)
    else:
        write_merged_8ch(base, outdir, groups)


def parse_args():
    argv = sys.argv[1:]
    outdir = os.getcwd()
    mode = 1
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == '--out':
            if i + 1 < len(argv):
                outdir = argv[i + 1]
                i += 2
            else:
                i += 1
        elif a in ('-1', '--mode1'):
            mode = 1
            i += 1
        elif a in ('-2', '--mode2'):
            mode = 2
            i += 1
        elif a in ('-3', '--mode3'):
            mode = 3
            i += 1
        elif a in ('--all', '-a'):
            i += 1  # ignored: 48k/all-stream export was removed
        else:
            rest.append(a)
            i += 1
    return rest, mode, os.path.abspath(outdir)


if __name__ == '__main__':
    qmdl, mode, outdir = parse_args()
    if not qmdl:
        print(__doc__)
        print('usage: extract_qmdl.py [-1|-2|-3] [--out DIR] <file.qmdl>')
        print('  -1 (default) : merged 8ch 16k wav only')
        print('  -2           : 16k mono wavs only for 0x1532@0x410, 0x1531@0x411, 0x1533@0x410')
        print('  -3           : {qmdl}.messages.txt only')
        sys.exit(1)
    extract(qmdl[0], outdir, mode=mode)