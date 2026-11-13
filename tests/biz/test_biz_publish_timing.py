"""自动发布的发布时间（立即 / 定时）、横封面生成、视频转码判断。页面操作本身在真实后台上用试运行验证，这里测判定逻辑。"""
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from superops.biz.executors import publishers as pubs
from superops.biz.timeutil import parse_iso

AT = parse_iso("2026-11-11T17:20:00+08:00")


def plan(platform, scheduled, timing=None):
    when, note = pubs.publish_time({"scheduled_at": scheduled, "timing": timing}, platform, AT)
    return (when.strftime("%m-%d %H:%M") if when else None), note


def test_来得及就定在计划的时间():
    assert plan("douyin", "2026-11-11T20:00:00+08:00") == ("11-11 20:00", "定时发布 11-11 20:00")
    assert plan("xiaohongshu", "2026-11-11T19:00:00+08:00")[0] == "11-11 19:00"          # 小红书只要提前 1 小时
    assert plan("wechat_channels", "2026-11-12T08:00:00+08:00")[0] == "11-12 08:00"


def test_不够平台的提前量_定在允许的最早时间_不会比计划早():
    when, note = plan("douyin", "2026-11-11T19:00:00+08:00")                           # 抖音要提前 2 小时
    assert when == "11-11 19:25" and "至少提前 2 小时" in note and "比计划的 19:00 晚" in note


def test_已过或不足半小时_立即发布():
    assert plan("douyin", "2026-11-11T17:00:00+08:00")[0] is None
    when, note = plan("douyin", "2026-11-11T17:45:00+08:00")
    assert when is None and note.startswith("立即发布（计划 17:45")


def test_发布包要求立即发_或者没写时间_立即发布():
    assert plan("douyin", "2026-11-12T20:00:00+08:00", "immediate") == (None, "立即发布")
    assert plan("douyin", None) == (None, "立即发布")
    assert plan("douyin", "今晚八点")[0] is None                                         # 认不出来的时间不猜


def test_超过十四天报错转人工():
    with pytest.raises(pubs.PublishError):
        plan("douyin", "2026-12-01T20:00:00+08:00")


def test_发布包里的时间和发布方式会带进发布器(tmp_path):
    (tmp_path / "video.mp4").write_bytes(b"x")
    (tmp_path / "cover.jpg").write_bytes(b"x")
    (tmp_path / "wide.jpg").write_bytes(b"x")
    meta = {"title": "t", "media": {"video": "video.mp4", "cover": "cover.jpg", "cover_horizontal": "wide.jpg"},
            "per_platform": {"douyin": {"scheduled_at": "2026-11-12T20:00:00+08:00", "publish_timing": "immediate"}}}
    pl = pubs.payload_for({"id": "pk1", "platform": "douyin", "pack_dir": str(tmp_path), "title": "t", "scheduled_at": None}, meta)
    assert pl["scheduled_at"] == "2026-11-12T20:00:00+08:00" and pl["timing"] == "immediate"
    assert pl["cover_horizontal"] == str(tmp_path / "wide.jpg")
    row_only = pubs.payload_for({"id": "pk1", "platform": "douyin", "pack_dir": str(tmp_path), "title": "t",
                                 "scheduled_at": "2026-11-12T19:00:00+08:00"}, {"media": {"video": "video.mp4"}})
    assert row_only["scheduled_at"] == "2026-11-12T19:00:00+08:00" and row_only["timing"] is None and row_only["cover_horizontal"] is None


def test_视频号默认声明原创_发布包可以关掉(tmp_path):
    row = lambda platform: {"id": "pk1", "platform": platform, "pack_dir": str(tmp_path), "title": "t", "scheduled_at": None}  # noqa: E731
    assert pubs.payload_for(row("wechat_channels"), {})["declare_original"] is True
    assert pubs.payload_for(row("douyin"), {})["declare_original"] is False
    off = {"per_platform": {"wechat_channels": {"declare_original": False}}}
    assert pubs.payload_for(row("wechat_channels"), off)["declare_original"] is False
    assert pubs.payload_for(row("wechat_channels"), {"declare_original": False})["declare_original"] is False


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="本机没有 ffmpeg")
def test_横封面由竖封面裁出四比三_挑内容多的一块(tmp_path):
    cover = tmp_path / "cover.jpg"
    # 上半部纯色、下半部有图案的竖图：应当裁到有图案的那一块
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=white:s=1080x960", "-f", "lavfi", "-i",
                    "testsrc2=s=1080x960", "-filter_complex", "[0][1]vstack", "-frames:v", "1", str(cover)], check=True)
    wide = pubs.horizontal_cover(cover, tmp_path)
    size = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(wide)],
                          capture_output=True, text=True).stdout.strip()
    assert wide.name == "cover-4x3.jpg" and size == "1440,1080"
    top = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(wide), "-vf", "crop=iw:60:0:0,scale=1:1", "-f", "rawvideo",
                          "-pix_fmt", "gray", "-"], capture_output=True).stdout
    assert top and top[0] < 250                                                         # 顶上不是整片留白
    assert pubs.horizontal_cover(cover, tmp_path) == wide and not list(tmp_path.glob(".cover-4x3-*"))   # 有缓存，不留临时文件


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="本机没有 ffmpeg")
def test_平台只收H264时_别的编码先转码(tmp_path):
    h264, other = tmp_path / "a.mp4", tmp_path / "b.mp4"
    base = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=160x284:d=1", "-pix_fmt", "yuv420p"]
    subprocess.run([*base, "-c:v", "libx264", str(h264)], check=True)
    subprocess.run([*base, "-c:v", "mpeg4", str(other)], check=True)
    assert pubs.ensure_h264(h264, tmp_path) == (h264, None)
    out, note = pubs.ensure_h264(other, tmp_path)
    assert out.name == "b-h264.mp4" and pubs.video_codec(out) == "h264" and "已转成 H.264" in note


def test_发布结果记下平台定时发出的时刻():
    res = pubs.PublishResult(ok=True, dry_run=False, publish_at=(AT + timedelta(hours=3)).isoformat(timespec="seconds"))
    assert res.publish_at == "2026-11-11T20:20:00+08:00" and pubs.PublishResult(ok=True, dry_run=True).publish_at is None


def test_读视频编码时去掉csv的结尾逗号(monkeypatch):
    class R:
        returncode, stdout = 0, "h264,\n"
    monkeypatch.setattr(pubs.subprocess, "run", lambda *a, **k: R())
    assert pubs.video_codec(Path("x.mp4")) == "h264"
    assert pubs.ensure_h264(Path("x.mp4"), Path("."))[1] is None        # H.264 不再转码
