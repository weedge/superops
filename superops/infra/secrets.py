"""密钥只放 macOS 钥匙串：服务名 <instance.keychain_service>/<密钥名>，账号名就是 instance.keychain_service。"""
from __future__ import annotations

import os
import subprocess

from .instance import current


def service_for(name: str) -> str:
    """钥匙串里的服务名，如 popto-ops/tikhub。"""
    return f"{current().keychain_service}/{name}"


def add_command(name: str) -> str:
    """把这个密钥放进钥匙串的命令（最后接 -w <值>）。"""
    return f"security add-generic-password -s {service_for(name)} -a {current().keychain_service} -w"


def keychain(name: str) -> str | None:
    env = os.environ.get(f"OPS_SECRET_{name.upper().replace('-', '_')}")  # 仅测试用
    if env:
        return env
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", service_for(name), "-w"],
                           capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
