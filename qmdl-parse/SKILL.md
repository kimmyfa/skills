---
name: qmdl-parse
description: Parse Qualcomm SPF / QXDM .qmdl diagnostic dumps and extract per-channel audio WAV files plus readable modem log text. Use this skill whenever the user mentions .qmdl files, Qualcomm/QCAT/QXDM/SPF modem or ADSP dump files, on-device diagnostic logs, extracting audio/PCM/mic streams from modem logs, or asks "解析qmdl"/"解析诊断日志"/"提取音频" regardless of phrasing. This covers both single files and scan-style directories containing .qmdl files. It produces the same output convention as QCAT reference extractions (per-channel {qmdl}.{lid}.pcm.{module}.{port}.0x{c}.tx.{srk}.wav plus {qmdl}.messages.txt). Even if the user only says they have a "diag dump" or wants to "decode the logs", check for .qmdl files first.
---

# QMDL Parse — Qualcomm SPF dump → per-channel audio + modem logs

## When to use

- User has `.qmdl` files (Qualcomm QXDM / SPF diagnostic dumps, often in folders named `diag_logfile_*.qmdl` or `diag_dir_*`)
- User wants the **audio streams** inside a modem log (mic/ref/etc), or the **camera/system text logs**
- User asks to parse / decode / 解析 / 提取 audio from a Qualcomm dump

Do NOT use this for: reading other file formats (wav/pcm conversion of already-decoded audio), cellular RRC/NAS signaling analysis (SCAT/QCSuper is the tool for that), or Qt/other .qmdl meanings.

## Core workflow

### 1. Locate the .qmdl file(s)

A `.qmdl` may be a raw file or a **directory** named `diag_logfile_....qmdl` containing the raw file + already-extracted outputs. If the user gives a directory, find the actual raw `*.qmdl` file inside it (may be a few hundred MB). If given a scan/collection directory, either process the newest one or ask which one.

### 2. Run the extractor

```bash
python3 <skill>/scripts/extract_qmdl.py [--all] <file.qmdl>
```

- **Default (no flag):** only exports the three reference audio streams:
  - `0x1532` mod `0x100732C2` port `0x411` → ch `0x1..0x4`
  - `0x1532` mod `0x10073282` port `0x410` → ch `0x1..0x2`
  - `0x1533` mod `0x10073081` port `0x410` → ch `0x1..0x2`
- **`--all`:** exports every audio stream in the file (useful for exploration / new device logs)

Both modes always write `{qmdl}.messages.txt` (decoded 0x79 EXT_MSG text logs).
Output goes into the same directory as the qmdl file.

Only dependency: `python3` with `numpy`. The script is standalone (no QXDM/scat required).

### 3. Report results

Summarize what was produced:
- list of per-channel wav files (`{qmdl}.{lid:04x}.pcm.{module:08X}.{port:x}.0x{c}.tx.{16k|48k}.wav`)
- number of log lines in `{qmdl}.messages.txt`
- per-stream summary line (lid/module/port/channels/sample rate/packets/duration)

## Understanding the format (why the script does what it does)

Knowing a little about the format helps you troubleshoot and answer "is this right?" questions:

- **Framing:** `.qmdl` = DIAG frames delimited by `0x7E`, HDLC-escaped (`0x7D 5E`→`0x7E`, `0x7D 5D`→`0x7D`), CRC16 trailing.
- **Audio streams (cmd `0x10`, DIAG_LOG_F):** 16-byte packet header, then an 88-byte SPF header:
  - `+0` header length (0x58=88), `+4` module id, `+12` payload length, `+44` PORT id, `+48` sample rate (u16), `+52` channel count (u16)
  - Audio = int16 LE payload after byte 88, laid out as **per-packet channel blocks**: channel `c` occupies `payload[c*per:(c+1)*per]`, `per = samples//channels`. Each block = one 10 ms frame. **Concatenate the same channel across packets** (do NOT interleave channel-major across the whole stream — that subsamples the signal and raises the pitch, i.e. "变调/变快").
- **Log text (cmd `0x79`, DIAG_EXT_MSG_F):** printf-style text; the script formats the args and timestamps (QXDM epoch 1980-01-06).
- **Channel count is real:** splitting by the header `ch` field is correct; the earlier "mono" interpretation was wrong.

## Troubleshooting

- **Pitch-shifted / speeded-up audio** → wrong channel layout was used. Must be channel-block concat, not global interleave, not mono-of-everything.
- **Silent channels (rms≈0)** → normal; reference streams often have quiet spares (e.g. ch2 of the 2ch streams).
- **Huge durations (hundreds of seconds)** → the whole payload incl. padding was concatenated. Use per-10 ms blocks only.
- **No .wav produced but messages.txt exists** → the file may have no audio streams (some dumps are logs-only); report that plainly.
- **New device/module IDs not in SUBSET** → use `--all` to enumerate available streams first.