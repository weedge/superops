"""平台代码与中文名：库里一律存代码（douyin 等），对人展示用中文名。Agent 输出常写中文名，入库时统一归一。"""
from __future__ import annotations

LABELS = {"douyin": "抖音", "wechat_channels": "视频号", "xiaohongshu": "小红书", "kuaishou": "快手",
          "bilibili": "B站", "zhihu": "知乎", "wechat_mp": "公众号"}
_ALIASES = {
    "抖音": "douyin", "dy": "douyin", "tiktok_cn": "douyin",
    "视频号": "wechat_channels", "微信视频号": "wechat_channels", "channels": "wechat_channels", "shipinhao": "wechat_channels",
    "小红书": "xiaohongshu", "红书": "xiaohongshu", "xhs": "xiaohongshu", "rednote": "xiaohongshu",
    "快手": "kuaishou", "ks": "kuaishou",
    "b站": "bilibili", "哔哩哔哩": "bilibili", "bili": "bilibili",
    "知乎": "zhihu",
    "公众号": "wechat_mp", "微信公众号": "wechat_mp", "gongzhonghao": "wechat_mp",
}


def normalize(name: str | None) -> str | None:
    if not name:
        return name
    s = str(name).strip()
    if s in LABELS:
        return s
    return _ALIASES.get(s) or _ALIASES.get(s.lower()) or s


def label(code: str | None) -> str:
    return LABELS.get(normalize(code) or "", code or "")
