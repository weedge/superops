"""把一组 HTML 页面截成图：图文出图（ops-image-post）用。

    $OPS_RENDER <页面目录> <输出目录> [--width 1080] [--height 1440]

页面目录里每个 *.html（按文件名排序）截成输出目录里同名的 .png，尺寸固定（默认 1080×1440，小红书竖版 3:4）。
用本机 Chrome 无界面打开、等字体和图片加载完再截；不带任何登录态（临时浏览器，不碰 browser-profiles）。
页面里引用的图片用相对路径（和 html 放在同一个目录或子目录），不联网。最后一行打印 JSON：{"images": [...], "errors": [...]}。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

READY_JS = """async () => {
  if (document.fonts && document.fonts.ready) { await document.fonts.ready; }
  const imgs = [...document.images];
  await Promise.all(imgs.map(i => i.complete ? null : new Promise(r => { i.onload = i.onerror = r; })));
  return imgs.filter(i => !i.naturalWidth).map(i => i.getAttribute('src'));
}"""


def render(pages: Path, out: Path, width: int = 1080, height: int = 1440) -> dict:
    from playwright.sync_api import sync_playwright

    files = sorted(pages.glob("*.html"))
    if not files:
        return {"images": [], "errors": [f"{pages} 下没有 .html 页面"]}
    out.mkdir(parents=True, exist_ok=True)
    images, errors = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True, args=["--disable-gpu"])
        try:
            ctx = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1, locale="zh-CN",
                                      offline=True)            # 不联网：页面只能用本地素材
            page = ctx.new_page()
            for f in files:
                try:
                    page.goto(f.resolve().as_uri(), wait_until="load", timeout=30000)
                    broken = page.evaluate(READY_JS)
                    if broken:
                        errors.append(f"{f.name}：这些图片没加载出来 {broken}")
                    target = out / f"{f.stem}.png"
                    page.screenshot(path=str(target), clip={"x": 0, "y": 0, "width": width, "height": height})
                    images.append(str(target.resolve()))
                except Exception as e:  # noqa: BLE001  一页坏了不影响别的页
                    errors.append(f"{f.name}：{type(e).__name__}：{str(e).splitlines()[0][:160] if str(e) else ''}")
        finally:
            browser.close()
    return {"images": images, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把 HTML 页面截成固定尺寸的图片（图文出图用）")
    ap.add_argument("pages")
    ap.add_argument("out")
    ap.add_argument("--width", type=int, default=1080)
    ap.add_argument("--height", type=int, default=1440)
    a = ap.parse_args(argv)
    result = render(Path(a.pages).expanduser(), Path(a.out).expanduser(), a.width, a.height)
    for e in result["errors"]:
        print("✗", e, file=sys.stderr)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["images"] and not result["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
