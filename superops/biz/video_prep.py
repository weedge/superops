"""视频任务降级到非 Antigravity 时的预处理：每 2 秒抽一帧拼宫格 + whisper 转写（03 §3.1）。"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

WHISPER_MODEL = Path("~/.cache/hyperframes/whisper/models/ggml-small.bin").expanduser()


def prepare(run_dir: Path, max_frames: int = 60) -> list[str]:
    """返回写进提示词的说明行；产物写到 run_dir/context/。"""
    media = run_dir / "media"
    videos = sorted(p for p in media.glob("*") if p.suffix.lower() in (".mp4", ".mov", ".m4v", ".webm")) if media.exists() else []
    notes = []
    ctx = run_dir / "context"
    ctx.mkdir(exist_ok=True)
    ff = shutil.which("ffmpeg")
    if not videos or not ff:
        return ["降级分析：没有找到可预处理的视频或本机没有 ffmpeg，只能基于脚本和文字信息判断，结论里必须注明。"]
    for v in videos[:3]:
        stem = v.stem
        grid = ctx / f"{stem}_grid.jpg"
        subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(v), "-vf",
                        f"fps=1/2,scale=360:-1,tile=6x{max(1, max_frames // 6)}", "-frames:v", "1", str(grid)],
                       timeout=300, check=False)
        wav = ctx / f"{stem}.wav"
        subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(v), "-ar", "16000", "-ac", "1", str(wav)],
                       timeout=300, check=False)
        txt = None
        wb = shutil.which("whisper-cli")
        if wb and WHISPER_MODEL.exists() and wav.exists():
            out = ctx / f"{stem}_transcript"
            subprocess.run([wb, "-m", str(WHISPER_MODEL), "-f", str(wav), "-l", "zh", "-osrt", "-of", str(out)],
                           timeout=900, check=False, capture_output=True)
            if out.with_suffix(".srt").exists():
                txt = out.with_suffix(".srt")
        wav.unlink(missing_ok=True)
        notes.append(f"视频 {v.name}：宫格抽帧 `context/{grid.name}`（每 2 秒一帧，从左到右、从上到下）"
                     + (f"，带时间轴的转写 `context/{txt.name}`" if txt else "，转写失败"))
    notes.append("**这是降级分析**：你看不到原视频，只能看抽帧和转写。summary 里必须注明“降级分析，未直接看视频”，"
                 "口型、卡顿、音画同步等无法判断的项目标为“未检查”。")
    return notes
