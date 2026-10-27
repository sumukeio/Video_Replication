"""
抽帧台 (extract_shots.py)
把一条视频号视频还原成「镜头清单」：顺序、时间点、停留时长、原分辨率关键帧。

复刻别人的视频号时用它替代「一张张截图」：
抽出来的不是素材，是素材清单 —— 告诉你原片用了哪些画面、每张停多久、按什么顺序。

核心假设：轮播类视频的每个镜头时长基本相等（原片就是按音频时长均分的），
所以「先猜镜头数，再等分取帧」比逐帧找硬切点更准、更稳。

用法：
  python extract_shots.py 视频.mp4                # 自动猜镜头数
  python extract_shots.py 视频.mp4 --even 18      # 已知 18 张，强制等分
  python extract_shots.py ./视频目录 -o ./out     # 批量

输出：
  <out>/shots/01_000.0s.jpg ...   按时间顺序的关键帧（原分辨率）
  <out>/shots.csv                 序号 / 起止 / 时长 / 文件名
  <out>/contact.jpg               联系表，一眼看完全片镜头
  <out>/manifest.json             机器可读清单
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

try:
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("缺少依赖，请先安装：pip install pillow numpy", file=sys.stderr)
    sys.exit(1)


VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".flv", ".ts", ".webm"}

PROBE_FPS = 5          # 探测采样帧率
PROBE_W = 64           # 探测用缩略图宽
MIN_GAP = 0.8          # 两个切点之间的最小间隔（秒）
DIFF_FLOOR = 2.0       # 帧差绝对下限，低于此值一律不算切点
MAX_SHOTS = 300       # 超过这个数基本是误检


def find_tool(name: str, override: Optional[str] = None) -> str:
    if override:
        return override
    here = Path(__file__).resolve().parent
    for cand in (here / "bin" / f"{name}.exe",
                 Path(r"E:/AIProject/erchuang/bin") / f"{name}.exe"):
        if cand.is_file():
            return str(cand)
    found = shutil.which(name)
    if found:
        return found
    raise RuntimeError(f"找不到 {name}，请装进 PATH 或用 --ffmpeg 指定")


def run(cmd: List[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def probe(ffprobe: str, video: Path) -> dict:
    r = run([ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate",
             "-show_entries", "format=duration", "-of", "json", str(video)])
    data = json.loads(r.stdout or "{}")
    st = (data.get("streams") or [{}])[0]
    dur = float((data.get("format") or {}).get("duration") or 0.0)
    if dur <= 0:
        raise RuntimeError("读不到视频时长，文件可能损坏")
    return {"width": int(st.get("width") or 0), "height": int(st.get("height") or 0),
            "duration": dur, "fps": st.get("r_frame_rate") or ""}


def detect_count(ffmpeg: str, video: Path, duration: float) -> Tuple[int, List[float], bool]:
    """猜镜头数：按固定帧率抽缩略图，算相邻帧差，数峰值。
    同时判断是不是「单图型」——整片就一张画面，只换字幕。"""
    # 长视频降采样率，避免几千张临时帧
    fps = PROBE_FPS if duration <= 180 else (3 if duration <= 420 else 2)
    with tempfile.TemporaryDirectory(prefix="shots_probe_") as tmp:
        pattern = str(Path(tmp) / "%04d.png")
        run([ffmpeg, "-hide_banner", "-loglevel", "quiet", "-y", "-i", str(video),
             "-vf", f"fps={fps},scale={PROBE_W}:-2", pattern], timeout=600)
        files = sorted(Path(tmp).glob("*.png"))
        if len(files) < 6:
            return 0, [], False

        arr = np.stack([np.asarray(Image.open(f).convert("RGB"), dtype=np.float32)
                        for f in files])
        # 切点检测只看画面中段 20%~72%，避开顶部标题条和底部字幕——
        # 这两块文字变化频繁，会把「换字幕」误判成「换镜头」
        h = arr.shape[1]
        mid = arr[:, int(h * 0.20):int(h * 0.72), :]
        diff = np.abs(np.diff(mid, axis=0)).reshape(len(mid) - 1, -1).mean(axis=1)

        # 单图判定：只看画面上部 70%（避开字幕区），以「中位帧」为基准比较
        # 用中位帧而非首帧，是因为很多片子开头有标题淡入淡出，会把首帧带偏
        top = arr[:, : int(h * 0.7), :]
        base = np.median(top, axis=0)
        spread = float(np.median(np.abs(top - base).reshape(len(top), -1).mean(axis=1)))
        single = spread < 3.0

    med = float(np.median(diff))
    th = max(med * 4.0, DIFF_FLOOR)
    win = max(1, int(MIN_GAP * fps / 2))

    peaks: List[float] = []
    for i in range(len(diff)):
        if diff[i] < th:
            continue
        lo, hi = max(0, i - win), min(len(diff), i + win + 1)
        if diff[i] < diff[lo:hi].max():
            continue
        t = (i + 1) / fps
        if t > duration - 0.4:
            continue
        if peaks and t - peaks[-1] < MIN_GAP:
            continue
        peaks.append(round(t, 2))

    return len(peaks) + 1, peaks, single


def even_segments(duration: float, n: int) -> List[Tuple[float, float]]:
    step = duration / n
    return [(round(i * step, 3), round(min((i + 1) * step, duration), 3)) for i in range(n)]


def grab_frame(ffmpeg: str, video: Path, at: float, dst: Path) -> bool:
    """抽单帧。源文件轻微损坏时 ffmpeg 可能返回非 0，但图片照样写出，所以只看文件"""
    try:
        run([ffmpeg, "-hide_banner", "-loglevel", "quiet", "-y",
             "-ss", f"{at:.3f}", "-i", str(video),
             "-frames:v", "1", "-q:v", "2", str(dst)], timeout=90)
    except Exception:
        return False
    return dst.is_file() and dst.stat().st_size > 1024


def load_font(size: int):
    for p in (r"C:/Windows/Fonts/msyhbd.ttc", r"C:/Windows/Fonts/msyh.ttc",
              r"C:/Windows/Fonts/arialbd.ttf"):
        if Path(p).is_file():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def make_contact(frames: List[dict], dst: Path, cols: int = 6, thumb_w: int = 190) -> None:
    if not frames:
        return
    with Image.open(frames[0]["path"]) as im:
        ratio = im.height / im.width
    thumb_h = max(120, int(thumb_w * ratio))
    label_h, pad = 48, 12
    rows = (len(frames) + cols - 1) // cols
    W = cols * thumb_w + (cols + 1) * pad
    H = rows * (thumb_h + label_h) + (rows + 1) * pad

    sheet = Image.new("RGB", (W, H), (247, 246, 243))
    draw = ImageDraw.Draw(sheet)
    f_idx, f_meta = load_font(24), load_font(18)

    for i, f in enumerate(frames):
        r, c = divmod(i, cols)
        x = pad + c * (thumb_w + pad)
        y = pad + r * (thumb_h + label_h + pad)
        try:
            with Image.open(f["path"]) as im:
                sheet.paste(im.convert("RGB").resize((thumb_w, thumb_h),
                                                     Image.Resampling.LANCZOS), (x, y))
        except Exception:
            draw.rectangle([x, y, x + thumb_w, y + thumb_h], fill=(220, 218, 212))
        draw.rectangle([x, y + thumb_h, x + thumb_w, y + thumb_h + label_h], fill=(255, 255, 255))
        draw.text((x + 8, y + thumb_h + 3), f"{f['index']:02d}", font=f_idx, fill=(156, 107, 63))
        draw.text((x + 8, y + thumb_h + 25),
                  f"{f['start']:.1f}s  +{f['duration']:.1f}s", font=f_meta, fill=(110, 108, 102))

    sheet.save(dst, quality=88)


def fmt_ts(sec: float) -> str:
    m, s = divmod(sec, 60)
    return f"{int(m):02d}:{s:05.2f}"


def process(video: Path, outdir: Path, ffmpeg: str, ffprobe: str,
            even: Optional[int]) -> dict:
    meta = probe(ffprobe, video)
    dur = meta["duration"]

    peaks: List[float] = []
    single = False
    if even:
        n = even
    else:
        n, peaks, single = detect_count(ffmpeg, video, dur)
        if single:
            n = 1
            peaks = []
        elif n < 2 or n > MAX_SHOTS:
            n = max(2, round(dur / 3.5))
            peaks = []

    segs = even_segments(dur, n)
    shots_dir = outdir / "shots"
    if shots_dir.exists():
        for p in shots_dir.glob("*.jpg"):
            p.unlink()
    shots_dir.mkdir(parents=True, exist_ok=True)

    frames = []
    for a, b in segs:
        mid = a + (b - a) * 0.5
        dst = shots_dir / f"{len(frames) + 1:02d}_{a:06.1f}s.jpg"
        if not grab_frame(ffmpeg, video, mid, dst):
            continue
        frames.append({"index": len(frames) + 1, "start": round(a, 2), "end": round(b, 2),
                       "duration": round(b - a, 2), "path": dst})

    if not frames:
        raise RuntimeError("一帧都没抽出来，视频可能无法解码")

    with (outdir / "shots.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["序号", "起始(秒)", "结束(秒)", "时长(秒)", "起始(时码)", "关键帧"])
        for f in frames:
            w.writerow([f["index"], f["start"], f["end"], f["duration"],
                        fmt_ts(f["start"]), f["path"].name])

    make_contact(frames, outdir / "contact.jpg")

    manifest = {
        "source": str(video), "width": meta["width"], "height": meta["height"],
        "duration": round(dur, 2), "fps": meta["fps"],
        "mode": "even" if even else "auto",
        "detected_peaks": peaks,
        "single_image": single,
        "shot_count": len(frames),
        "shot_seconds": round(dur / max(1, len(frames)), 2),
        "shots": [{"index": f["index"], "start": f["start"], "end": f["end"],
                   "duration": f["duration"], "frame": f["path"].name} for f in frames],
    }
    (outdir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def collect_videos(target: Path) -> List[Path]:
    if target.is_file():
        return [target]
    return sorted(p for p in target.rglob("*")
                  if p.suffix.lower() in VIDEO_EXT and p.is_file())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="视频号镜头还原：抽帧 + 时长 + 联系表")
    ap.add_argument("target", help="视频文件或包含视频的目录")
    ap.add_argument("-o", "--out", default=None, help="输出目录（默认 ./shots_out/<视频名>）")
    ap.add_argument("--even", type=int, default=None,
                    help="强制等分成 N 段（已知原片张数时最准，推荐）")
    ap.add_argument("--ffmpeg", default=None, help="自定义 ffmpeg 路径")
    args = ap.parse_args(argv)

    target = Path(args.target).resolve()
    if not target.exists():
        print(f"路径不存在: {target}", file=sys.stderr)
        return 1

    try:
        ffmpeg = find_tool("ffmpeg", args.ffmpeg)
        ffprobe = find_tool("ffprobe")
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1

    videos = collect_videos(target)
    if not videos:
        print(f"没有找到视频文件: {target}", file=sys.stderr)
        return 1

    base_out = Path(args.out).resolve() if args.out else Path.cwd() / "shots_out"

    for v in videos:
        outdir = base_out / v.stem
        outdir.mkdir(parents=True, exist_ok=True)
        print(f"[处理] {v.name}")
        try:
            m = process(v, outdir, ffmpeg, ffprobe, args.even)
        except Exception as e:
            print(f"  [失败] {e}", file=sys.stderr)
            continue

        if args.even:
            note = "强制等分"
        elif m["single_image"]:
            note = "单图型 —— 整片只有一张画面，只有字幕在变"
        else:
            note = (f"自动识别 {m['shot_count']} 个镜头，切点 "
                    + (", ".join(f"{p}s" for p in m["detected_peaks"]) or "无")
                    + " —— 请对照联系表核对")
        print(f"  {m['width']}x{m['height']}  {m['duration']}s  "
              f"每镜 {m['shot_seconds']}s  ({note})")
        print(f"  关键帧  {outdir / 'shots'}")
        print(f"  联系表  {outdir / 'contact.jpg'}")
        print(f"  清单    {outdir / 'shots.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
