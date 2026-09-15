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

Output convention (same as the reference Dataprepro folders):
  {qmdl}.{lid:04x}.pcm.{module:08X}.{port:x}.0x{c}.tx.{16k|48k}.wav   (per channel)

Modes:
  default : only export the three reference audio streams:
              lid 0x1532 mod 0x100732C2 port 0x411  -> ch 0x1..0x4  (2C2)
              lid 0x1532 mod 0x10073282 port 0x410  -> ch 0x1..0x2  (282)
              lid 0x1533 mod 0x10073081 port 0x410  -> ch 0x1..0x2  (081)
  --all   : export every audio stream found.
Both modes always write {qmdl}.messages.txt.
"""
import sys, struct, collections, os, wave, re, datetime

import numpy as np

WANT = {0x1531, 0x1532, 0x1533}

# Default subset: (lid, module) -> set of port ids to export
SUBSET_PORTS = {
    (0x1532, 0x100732C2): {0x411},
    (0x1532, 0x10073282): {0x410},
    (0x1533, 0x10073081): {0x410},
    (0x1532, 0x100732c2): {0x411},
    (0x1532, 0x10073282): {0x410},
    (0x1533, 0x10073081): {0x410},
}

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


def extract(qmdl, outdir, all_streams=False):
    base = os.path.basename(qmdl)
    groups = collections.defaultdict(list)
    msgs = []

    for b in read_frames(qmdl):
        cmd = b[0]
        if cmd == 0x79:
            line = format_ext_msg(b)
            if line:
                msgs.append(line)
            continue
        if cmd != 0x10:
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
        if sr not in (8000, 11025, 16000, 22050, 24000, 32000, 44100, 48000, 88200, 96000):
            sr = 16000 if plen < 4000 else 48000
        if ch < 1 or ch > 32:
            ch = 1
        s16 = np.frombuffer(body[88:88 + plen], dtype='<i2').astype(np.int64)
        per = s16.size // ch
        if per < 1:
            continue
        groups[(lid, plen, mod, port, sr, ch)].append(s16)

    os.makedirs(outdir, exist_ok=True)

    # --- modem log text (always) ---
    if msgs:
        msg_path = os.path.join(outdir, base + '.messages.txt')
        with open(msg_path, 'w') as f:
            f.write('\n'.join(msgs) + '\n')
        print('wrote %d log lines -> %s' % (len(msgs), os.path.basename(msg_path)))
    else:
        print('no 0x79 messages found')

    # --- audio streams ---
    written = 0
    for key, pkts in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if len(pkts) < 2:
            continue
        lid, plen, mod, port, sr, ch = key
        if not all_streams:
            if (lid, mod) not in SUBSET_PORTS or port not in SUBSET_PORTS[(lid, mod)]:
                continue
        srk = '16k' if sr == 16000 else '%dk' % (sr // 1000)
        mods = '0x%08X' % mod
        ports = '0x%x' % port
        per = pkts[0].size // ch
        full = np.zeros((len(pkts) * per, ch), dtype=np.int64)
        for i, p in enumerate(pkts):
            full[i * per:(i + 1) * per] = p[:per * ch].reshape(ch, per).T
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
        print('lid=0x%04x mod=0x%08x port=0x%04x plen=0x%04x sr=%d ch=%d npkts=%d dur=%.1fs' % (
            lid, mod, port, plen, sr, ch, len(pkts), len(pkts) * per / sr))
    print('\nwrote %d wav files -> %s (mode: %s)' % (
        written, outdir, 'ALL' if all_streams else 'SUBSET(2C2/282/081)'))


if __name__ == '__main__':
    argv = [a for a in sys.argv[1:] if not a.startswith('-')]
    all_streams = any(a in ('--all', '-a') for a in sys.argv[1:])
    if not argv:
        print(__doc__)
        print('usage: extract_qmdl.py [--all] <file.qmdl>')
        sys.exit(1)
    qmdl = argv[0]
    outdir = os.path.dirname(os.path.abspath(qmdl)) or '.'
    extract(qmdl, outdir, all_streams=all_streams)