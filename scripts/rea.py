#!/usr/bin/env python3
"""rea-video 拉片工具：把一条视频拆成 切点 / 联系表 / 台词 / 响度 / 拉片表骨架。

用法：
  python rea.py all   VIDEO --out WORKDIR [--lang zh] [--model small] [--threshold 0.3]
  python rea.py probe VIDEO --out WORKDIR
  python rea.py shots VIDEO --out WORKDIR [--threshold 0.3] [--min-shot 0.35]
  python rea.py sheet VIDEO --out WORKDIR [--cols 4 --rows 3]
  python rea.py asr   VIDEO --out WORKDIR [--lang zh] [--model small]
  python rea.py audio VIDEO --out WORKDIR
  python rea.py timeline    --out WORKDIR

依赖：ffmpeg / ffprobe 在 PATH 里；Python 3.9+；Pillow、numpy；
台词转写需要 faster-whisper（pip install faster-whisper），没有就跳过这一步。
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover
    sys.exit("需要 Pillow：pip install pillow")

CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


# ---------- 小工具 ----------

def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def need(tool: str) -> str:
    exe = shutil.which(tool)
    if not exe:
        sys.exit(f"找不到 {tool}：请先安装 ffmpeg 并加入 PATH")
    return exe


def tc(t: float) -> str:
    """秒 → 00:12.3 / 1:02:03.4"""
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:04.1f}" if h else f"{int(m):02d}:{s:04.1f}"


def srt_tc(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def font(size: int):
    candidates = [
        "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf",
        "/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/STHeiti Medium.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    ]
    for c in candidates:
        if Path(c).exists():
            try:
                return ImageFont.truetype(c, size)
            except OSError:
                pass
    return ImageFont.load_default()


def load(out: Path, name: str):
    p = out / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def save(out: Path, name: str, data) -> Path:
    p = out / name
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


# ---------- 1. 体检 ----------

def probe(video: Path, out: Path) -> dict:
    r = run([need("ffprobe"), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(video)])
    d = json.loads(r.stdout or "{}")
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), None)
    w, h = int(v.get("width", 0)), int(v.get("height", 0))
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(sd["rotation"])
    if abs(rot) in (90, 270):
        w, h = h, w
    num, _, den = (v.get("r_frame_rate") or "0/1").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 0.0
    dur = float(d.get("format", {}).get("duration") or v.get("duration") or 0)
    ratio = w / h if h else 0
    orient = "竖屏" if ratio < 0.9 else ("横屏" if ratio > 1.1 else "方形")
    info = {
        "file": str(video), "duration": round(dur, 3), "duration_tc": tc(dur),
        "width": w, "height": h, "fps": round(fps, 3), "orientation": orient,
        "aspect": f"{w}:{h}", "has_audio": a is not None,
        "audio": {"codec": a.get("codec_name"), "channels": a.get("channels"),
                  "sample_rate": a.get("sample_rate")} if a else None,
    }
    save(out, "info.json", info)
    print(f"[probe] {info['duration_tc']}  {w}x{h}  {fps:.2f}fps  {orient}  音轨:{'有' if a else '无'}")
    return info


# ---------- 2. 切点 ----------

def shots(video: Path, out: Path, threshold: float = 0.3, min_shot: float = 0.35) -> dict:
    info = load(out, "info.json") or probe(video, out)
    dur = info["duration"]
    vf = f"scale=320:-2,select='gt(scene,{threshold})',showinfo"
    r = run([need("ffmpeg"), "-hide_banner", "-nostats", "-i", str(video), "-vf", vf, "-an", "-f", "null", "-"])
    raw = [float(m) for m in re.findall(r"pts_time:([0-9.]+)", r.stderr)]
    cuts: list[float] = []
    for t in sorted(raw):
        if t < min_shot or dur - t < min_shot:
            continue
        if cuts and t - cuts[-1] < min_shot:
            continue
        cuts.append(round(t, 3))
    edges = [0.0] + cuts + [dur]
    items = [{"i": i + 1, "start": round(a, 3), "end": round(b, 3), "dur": round(b - a, 3)}
             for i, (a, b) in enumerate(zip(edges[:-1], edges[1:]))]
    lens = sorted(s["dur"] for s in items)
    n = len(items)
    stats = {
        "threshold": threshold, "shots": n, "cuts": len(cuts),
        "asl": round(dur / n, 2) if n else 0,
        "median": round(lens[n // 2], 2) if n else 0,
        "min": round(lens[0], 2) if n else 0, "max": round(lens[-1], 2) if n else 0,
        "cuts_per_min": round(len(cuts) / dur * 60, 1) if dur else 0,
    }
    # 每 10 秒一个窗口的切镜数，用来看节奏曲线
    win = []
    for k in range(int(math.ceil(dur / 10))):
        a, b = k * 10, min(dur, k * 10 + 10)
        win.append({"from": a, "to": round(b, 1), "cuts": sum(1 for c in cuts if a <= c < b)})
    stats["per_10s"] = win
    data = {"stats": stats, "shots": items}
    save(out, "shots.json", data)
    print(f"[shots] {n} 个镜头，平均镜长 {stats['asl']}s，中位 {stats['median']}s，每分钟 {stats['cuts_per_min']} 刀"
          f"（阈值 {threshold}；溶镜、闪白、长镜头内运动可能漏检/误检，要对照联系表核）")
    return data


# ---------- 3. 联系表 ----------

def grab(video: Path, t: float, dst: Path, width: int = 480) -> bool:
    r = run([need("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.3f}", "-i", str(video),
             "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "3", str(dst)])
    return dst.exists() and r.returncode == 0


def sheet(video: Path, out: Path, cols: int = 4, rows: int = 3) -> list[str]:
    data = load(out, "shots.json") or shots(video, out)
    info = load(out, "info.json") or probe(video, out)
    portrait = info["orientation"] == "竖屏"
    fdir = out / "frames"
    fdir.mkdir(exist_ok=True)
    thumbs = []
    for s in data["shots"]:
        t = s["start"] + s["dur"] * 0.5
        p = fdir / f"shot_{s['i']:03d}.jpg"
        if p.exists() or grab(video, t, p, 360 if portrait else 480):
            thumbs.append((s, p))
    if not thumbs:
        print("[sheet] 没有截到帧")
        return []
    w0, h0 = Image.open(thumbs[0][1]).size
    pad, lab = 10, 34
    f = font(20)
    per = cols * rows
    paths = []
    for k in range(0, len(thumbs), per):
        chunk = thumbs[k:k + per]
        W = cols * (w0 + pad) + pad
        H = rows * (h0 + lab + pad) + pad
        canvas = Image.new("RGB", (W, H), (24, 24, 23))
        d = ImageDraw.Draw(canvas)
        for j, (s, p) in enumerate(chunk):
            x = pad + (j % cols) * (w0 + pad)
            y = pad + (j // cols) * (h0 + lab + pad)
            im = Image.open(p).convert("RGB").resize((w0, h0))
            canvas.paste(im, (x, y + lab))
            d.text((x + 2, y + 5), f"#{s['i']:02d}  {tc(s['start'])}-{tc(s['end'])}  {s['dur']:.1f}s",
                   font=f, fill=(240, 239, 236))
        sp = out / f"sheet_{k // per + 1:02d}.jpg"
        canvas.save(sp, quality=88)
        paths.append(str(sp))
    print(f"[sheet] 每镜取中间一帧，拼成 {len(paths)} 张联系表：{', '.join(Path(p).name for p in paths)}")
    return paths


# ---------- 4. 台词 ----------

def extract_wav(video: Path, out: Path) -> Path | None:
    wav = out / "audio_16k.wav"
    if not wav.exists():
        r = run([need("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
                 "-vn", "-ac", "1", "-ar", "16000", str(wav)])
        if r.returncode != 0:
            print("[asr] 抽音轨失败：", r.stderr[-300:])
            return None
    return wav


def asr(video: Path, out: Path, lang: str = "zh", model: str = "small") -> dict | None:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("[asr] 没装 faster-whisper，跳过台词转写（pip install faster-whisper）")
        return None
    wav = extract_wav(video, out)
    if not wav:
        return None
    m = WhisperModel(model, device="auto", compute_type="int8")
    # vad_filter + 不依赖上文：防止静音段和音乐段里“编”出台词
    segs, meta = m.transcribe(str(wav), language=lang or None, beam_size=5, vad_filter=True,
                              condition_on_previous_text=False, word_timestamps=True)
    items = []
    for s in segs:
        text = s.text.strip()
        if not text:
            continue
        d = max(0.01, s.end - s.start)
        n_cjk = len(CJK.findall(text))
        items.append({"start": round(s.start, 2), "end": round(s.end, 2), "text": text,
                      "chars": n_cjk, "cps": round(n_cjk / d, 1) if n_cjk else None,
                      "avg_logprob": round(s.avg_logprob, 2)})
    speech = sum(i["end"] - i["start"] for i in items)
    total_chars = sum(i["chars"] for i in items)
    res = {"model": model, "language": meta.language, "segments": items,
           "speech_seconds": round(speech, 1), "chars": total_chars,
           "avg_cps": round(total_chars / speech, 1) if speech else None,
           "note": "自动转写，同音字、人名、说话人都要对照画面和字幕人工校对；avg_logprob 低于 -1 的句子优先复听"}
    save(out, "transcript.json", res)
    with open(out / "transcript.srt", "w", encoding="utf-8") as fh:
        for k, i in enumerate(items, 1):
            fh.write(f"{k}\n{srt_tc(i['start'])} --> {srt_tc(i['end'])}\n{i['text']}\n\n")
    print(f"[asr] {len(items)} 句，说话 {res['speech_seconds']}s，平均每秒 {res['avg_cps']} 字（模型 {model}）")
    return res


# ---------- 5. 声音 ----------

def audio(video: Path, out: Path) -> dict | None:
    info = load(out, "info.json") or probe(video, out)
    if not info["has_audio"]:
        print("[audio] 没有音轨")
        return None
    ff = need("ffmpeg")
    r = run([ff, "-hide_banner", "-nostats", "-i", str(video), "-vn", "-af", "ebur128=peak=true", "-f", "null", "-"])
    pts = [(float(t), float(mv), float(sv)) for t, mv, sv in
           re.findall(r"t:\s*([0-9.]+)\s+TARGET:[^M]*M:\s*(-?[0-9.]+)\s+S:\s*(-?[0-9.]+)", r.stderr)]
    integ = re.findall(r"I:\s+(-?[0-9.]+) LUFS", r.stderr)
    lra = re.findall(r"LRA:\s+([0-9.]+) LU", r.stderr)
    peak = re.findall(r"Peak:\s+(-?[0-9.]+) dBFS", r.stderr)
    # 每秒一个点（取该秒内瞬时响度的平均能量）
    per_sec: dict[int, list[float]] = {}
    for t, mv, _ in pts:
        per_sec.setdefault(int(t), []).append(mv)
    curve = []
    for s in sorted(per_sec):
        vals = [v for v in per_sec[s] if v > -70]
        lufs = 10 * math.log10(sum(10 ** (v / 10) for v in vals) / len(vals)) if vals else -70.0
        curve.append({"t": s, "lufs": round(lufs, 1)})
    r2 = run([ff, "-hide_banner", "-nostats", "-i", str(video), "-vn", "-af", "silencedetect=noise=-38dB:d=0.4",
              "-f", "null", "-"])
    starts = [float(x) for x in re.findall(r"silence_start:\s*(-?[0-9.]+)", r2.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*([0-9.]+)", r2.stderr)]
    silences = [{"start": round(a, 2), "end": round(b, 2), "dur": round(b - a, 2)} for a, b in zip(starts, ends)]

    # 结合台词：说话的秒 vs 不说话的秒，估算对白下的音乐有没有压低
    tr = load(out, "transcript.json")
    speech_secs = set()
    if tr:
        for sgm in tr["segments"]:
            speech_secs.update(range(int(sgm["start"]), int(math.ceil(sgm["end"]))))
    sp = [c["lufs"] for c in curve if c["t"] in speech_secs and c["lufs"] > -60]
    ns = [c["lufs"] for c in curve if c["t"] not in speech_secs and c["lufs"] > -60]

    def med(a):
        a = sorted(a)
        return a[len(a) // 2] if a else None

    # 掐断/静音候选：瞬时响度（0.4 秒窗）在半秒内从正常音量掉到很低，并且低位至少维持 0.3 秒
    drops = []
    ms = [mv for _, mv, _ in pts]
    for k in range(10, len(pts) - 3):
        before = max(ms[k - 10:k])          # 前 1 秒里的最高点
        after = sum(ms[k:k + 3]) / 3        # 之后 0.3 秒的平均
        if before > -28 and after < -32 and before - after >= 12:
            t = pts[k][0]
            if not drops or t - drops[-1]["t"] > 2:
                drops.append({"t": round(t, 1), "drop_db": round(before - after, 1),
                              "in_speech": int(t) in speech_secs})
    res = {
        "integrated_lufs": float(integ[-1]) if integ else None,
        "lra": float(lra[-1]) if lra else None,
        "true_peak_dbfs": float(peak[-1]) if peak else None,
        "speech_median_lufs": med(sp), "non_speech_median_lufs": med(ns),
        "silences": silences, "sudden_drops": drops, "curve": curve,
        "note": "曲线是整轨（人声+音乐+音效）响度。不说话时的响度≈配乐/环境音的音量；"
                "sudden_drops 是声音突然掉下去的点（配乐掐断、全场静音、台词前的留白），要人耳确认。",
    }
    save(out, "audio.json", res)
    print(f"[audio] 整片 {res['integrated_lufs']} LUFS，LRA {res['lra']}，峰值 {res['true_peak_dbfs']} dBFS；"
          f"静音 {len(silences)} 段，骤降候选 {len(drops)} 处")
    return res


# ---------- 6. 图表 ----------

def charts(out: Path) -> list[str]:
    info, sh, au, tr = (load(out, n) for n in ("info.json", "shots.json", "audio.json", "transcript.json"))
    if not (info and sh):
        return []
    dur = info["duration"]
    W, H, L, R, T, B = 1600, 520, 90, 40, 70, 70
    pw, ph = W - L - R, H - T - B
    f, fs = font(26), font(18)
    paths = []

    def x_of(t):
        return L + pw * t / dur

    # 剪辑节奏：每个镜头一根柱，柱高=镜头时长
    im = Image.new("RGB", (W, H), (26, 26, 25))
    d = ImageDraw.Draw(im)
    mx = max(s["dur"] for s in sh["shots"]) or 1
    top = max(4.0, math.ceil(mx))
    for k in range(0, int(top) + 1, max(1, int(top) // 4)):
        y = T + ph - ph * k / top
        d.line([(L, y), (W - R, y)], fill=(58, 58, 55), width=1)
        d.text((L - 12, y), f"{k}s", font=fs, fill=(141, 140, 134), anchor="rm")
    for s in sh["shots"]:
        x0, x1 = x_of(s["start"]) + 1, x_of(s["end"]) - 1
        y = T + ph - ph * min(s["dur"], top) / top
        d.rectangle([x0, y, max(x0 + 1, x1), T + ph], fill=(57, 135, 229))
    for k in range(0, int(dur) + 1, 10 if dur <= 180 else 30):
        d.text((x_of(k), T + ph + 12), tc(k), font=fs, fill=(141, 140, 134), anchor="mt")
    st = sh["stats"]
    d.text((L, 22), f"剪辑节奏：{st['shots']} 个镜头 · 平均镜长 {st['asl']}s · 中位 {st['median']}s · 每分钟 {st['cuts_per_min']} 刀",
           font=f, fill=(240, 239, 236))
    p = out / "chart_rhythm.png"
    im.save(p)
    paths.append(str(p))

    # 响度曲线 + 说话区间 + 切点
    if au and au.get("curve"):
        im = Image.new("RGB", (W, H), (26, 26, 25))
        d = ImageDraw.Draw(im)
        lo, hi = -50.0, -5.0

        def y_of(v):
            return T + ph - ph * (min(hi, max(lo, v)) - lo) / (hi - lo)

        if tr:
            for sgm in tr["segments"]:
                d.rectangle([x_of(sgm["start"]), T, x_of(sgm["end"]), T + ph], fill=(44, 52, 44))
        for v in range(int(lo), int(hi) + 1, 10):
            d.line([(L, y_of(v)), (W - R, y_of(v))], fill=(58, 58, 55))
            d.text((L - 12, y_of(v)), f"{v}", font=fs, fill=(141, 140, 134), anchor="rm")
        for s in sh["shots"][1:]:
            d.line([(x_of(s["start"]), T + ph - 14), (x_of(s["start"]), T + ph)], fill=(141, 140, 134))
        pts = [(x_of(c["t"] + 0.5), y_of(c["lufs"])) for c in au["curve"]]
        if len(pts) > 1:
            d.line(pts, fill=(240, 179, 58), width=3)
        for dr in au.get("sudden_drops", []):
            d.ellipse([x_of(dr["t"]) - 6, T + 4, x_of(dr["t"]) + 6, T + 16], fill=(232, 86, 74))
        for k in range(0, int(dur) + 1, 10 if dur <= 180 else 30):
            d.text((x_of(k), T + ph + 12), tc(k), font=fs, fill=(141, 140, 134), anchor="mt")
        d.text((L, 22), f"响度（LUFS/秒）· 整片 {au['integrated_lufs']} LUFS · 绿底=有人说话 · 红点=骤降候选 · 底部刻度=切点",
               font=f, fill=(240, 239, 236))
        p = out / "chart_loudness.png"
        im.save(p)
        paths.append(str(p))
    print(f"[charts] {', '.join(Path(x).name for x in paths)}")
    return paths


# ---------- 7. 拉片表骨架 ----------

def timeline(out: Path) -> Path | None:
    info, sh, tr, au = (load(out, n) for n in ("info.json", "shots.json", "transcript.json", "audio.json"))
    if not (info and sh):
        print("[timeline] 先跑 probe 和 shots")
        return None
    segs = tr["segments"] if tr else []
    loud = {c["t"]: c["lufs"] for c in (au or {}).get("curve", [])}
    st = sh["stats"]
    lines = [
        f"# 拉片表：{Path(info['file']).name}", "",
        f"- 时长 {info['duration_tc']}｜{info['width']}×{info['height']}｜{info['fps']}fps｜{info['orientation']}",
        f"- 镜头 {st['shots']} 个｜平均镜长 {st['asl']}s｜中位 {st['median']}s｜最短 {st['min']}s｜最长 {st['max']}s｜每分钟 {st['cuts_per_min']} 刀",
    ]
    if tr:
        lines.append(f"- 台词 {len(segs)} 句｜说话 {tr['speech_seconds']}s｜平均每秒 {tr['avg_cps']} 字（自动转写，待校对）")
    if au:
        lines.append(f"- 整片 {au['integrated_lufs']} LUFS｜对白处中位 {au['speech_median_lufs']}｜非对白处中位 {au['non_speech_median_lufs']}"
                     f"｜骤降候选 {', '.join(tc(x['t']) for x in au['sudden_drops']) or '无'}")
    lines += ["", "> 带【】的列由人看联系表/原片后填写；自动切点会漏检溶镜和长镜头内的运动，要对照联系表合并或拆分。", "",
              "| # | 时间 | 时长 | 台词（自动转写） | 响度 | 【景别/机位/运镜】 | 【画面与表演】 | 【光影/色彩】 | 【声音/配乐】 | 【这一刀的作用】 |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for s in sh["shots"]:
        said = [g["text"] for g in segs if s["start"] <= (g["start"] + g["end"]) / 2 < s["end"]]
        ls = [loud[t] for t in range(int(s["start"]), int(math.ceil(s["end"]))) if t in loud]
        lv = f"{sum(ls) / len(ls):.0f}" if ls else ""
        text = " / ".join(said).replace("|", "｜")
        lines.append(f"| {s['i']} | {tc(s['start'])}–{tc(s['end'])} | {s['dur']:.1f}s | {text} | {lv} |  |  |  |  |  |")
    p = out / "timeline.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[timeline] {p.name}：{st['shots']} 行")
    return p


# ---------- 入口 ----------

def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="rea-video 拉片工具")
    ap.add_argument("cmd", choices=["all", "probe", "shots", "sheet", "asr", "audio", "charts", "timeline"])
    ap.add_argument("video", nargs="?", type=Path)
    ap.add_argument("--out", type=Path, required=True, help="工作目录，所有结果写在这里")
    ap.add_argument("--threshold", type=float, default=0.3, help="切点灵敏度，越小切点越多（0.2~0.4）")
    ap.add_argument("--min-shot", type=float, default=0.35, help="短于这个秒数的镜头并入前一镜")
    ap.add_argument("--lang", default="zh")
    ap.add_argument("--model", default="small", help="faster-whisper 模型：tiny/base/small/medium/large-v3")
    ap.add_argument("--cols", type=int, default=4)
    ap.add_argument("--rows", type=int, default=3)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    if a.cmd not in ("timeline", "charts") and (not a.video or not a.video.exists()):
        sys.exit("请给出存在的视频文件路径")
    v = a.video.resolve() if a.video else None
    if a.cmd in ("all", "probe"):
        probe(v, a.out)
    if a.cmd in ("all", "shots"):
        shots(v, a.out, a.threshold, a.min_shot)
    if a.cmd in ("all", "sheet"):
        sheet(v, a.out, a.cols, a.rows)
    if a.cmd in ("all", "asr"):
        asr(v, a.out, a.lang, a.model)
    if a.cmd in ("all", "audio"):
        audio(v, a.out)
    if a.cmd in ("all", "charts"):
        charts(a.out)
    if a.cmd in ("all", "timeline"):
        timeline(a.out)
    if a.cmd == "all":
        print(f"\n完成。下一步：打开 {a.out}/sheet_*.jpg 看画面，校对 transcript.srt，填 timeline.md 里带【】的列。")


if __name__ == "__main__":
    main()
