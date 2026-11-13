"""python -m superops.biz.executors.publishers check —— 检查已启用自动发布的平台登录态（16:30，17:00 发布卡片前）。"""
from __future__ import annotations

import sys

from ...config import load_agents_config
from ...scriptkit import emit_result, hub_client
from . import PLATFORMS, account_label, check_login, default_account


def main(argv: list[str]) -> int:
    if argv[:1] != ["check"]:
        print(__doc__)
        return 2
    cfg = load_agents_config()
    enabled = [pf for pf, c in (cfg.get("publishers") or {}).items()
               if pf in PLATFORMS and (c or {}).get("mode") in ("live", "dry_run")]
    bad, ok = [], []
    for pf in enabled:
        key = default_account(pf)          # 同一平台多个号：查默认账号（发布包不指定账号就发到它）
        try:
            good, _ = check_login(key)
        except Exception as e:  # noqa: BLE001  网络问题不当成登录失效
            ok.append(f"{account_label(key)}（检查失败：{str(e)[:60]}）")
            continue
        (ok if good else bad).append(account_label(key) if good else key)
    if bad:
        lines = [f"- {account_label(key)}：在面板“内容 → 账号”扫码，或在电脑上运行 `superops publish qrlogin {key}`" for key in bad]
        hub_client().notify("alert", "自动发布账号登录失效，请在 17:00 前扫码",
                            "以下平台的创作者后台登录已失效，不处理的话今晚这些平台会转人工发布：\n\n" + "\n".join(lines),
                            priority=1, ref="publish-login-check", dedup=False)
    emit_result(f"自动发布登录检查：正常 {len(ok)} 个" + (f"，失效 {len(bad)} 个（{'、'.join(bad)}），已提醒" if bad else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
