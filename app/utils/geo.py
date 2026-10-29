# -*- coding: utf-8 -*-
"""本机定位（`T82-④`）：**只在你显式要求时**探测一次，不做后台轮询。

三条纪律，都是从真实反馈里来的：

1. **探测必须显式触发**（`/city detect`）。定位结果会写进世界书并每轮注入【天气】，
   猜错就是持续性假信息；而"她随时知道你在哪"与项目"感知层只给事实、不猜"的取向冲突。
2. **Windows 定位优先，公网 IP 兜底**，且两条路的结果**各自标注来源**。
   实测（2026-09-28 这台机器）：系统定位给成都（精度 212 米），公网 IP 反查给台中
   ——IP 走的是宽带出口，换城市也会骗人。
3. **只发打码后的坐标给第三方做逆地理**：0.1 度 ≈ 10km，判城市足够，
   但不足以定位到住址。精确坐标只在本机内存里过一遍，不落盘、不上传。

实现上不用 WinRT 直连：`Geolocator` 在 Windows PowerShell 的 WinRT 投影里
**没有 `RequestAccessAsync`**（实测报 `does not contain a method named
'RequestAccessAsync'`）。改走 .NET 的 `System.Device.Location.GeoCoordinateWatcher`
——它后面就是同一套 Windows 定位平台，而这台机器上实测能拿到坐标。

那个 PowerShell 脚本**只能写 ASCII**：Windows PowerShell 5.1 按 GBK 读 .ps1，
脚本里出现中文会解析错位直接语法错误（第一次探针就是这么废的，已加测试钉住）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from app.log import get

log = get("geo")

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "geo_probe.ps1")


def _script_path() -> str:
    """探测脚本的绝对路径（测试会断言它存在且是纯 ASCII）。"""
    return _SCRIPT


def _find_powershell() -> str | None:
    """找 Windows PowerShell（`powershell.exe`，不是 pwsh）。

    **不能用 PowerShell 7**：`System.Device.Location` 是 .NET Framework 才有的
    WPF/WinForms 组件，pwsh（.NET 8）里 `Add-Type -AssemblyName System.Device` 会失败。
    """
    if os.name != "nt":
        return None
    for cand in ("powershell.exe",
                 os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                              "System32", "WindowsPowerShell", "v1.0", "powershell.exe")):
        try:
            subprocess.run([cand, "-NoProfile", "-Command", "exit 0"],
                           capture_output=True, timeout=15)
            return cand
        except Exception:
            continue
    return None


def detect_windows(timeout: int = 40) -> dict:
    """Windows 定位平台取一次坐标。返回 `{ok, lat, lon, acc, source, error}`。

    失败一律以 `ok=False` + `error` 返回（调用方据此回退到 IP），**不抛异常**：
    定位失败不该让一条命令崩掉。
    """
    bad = {"ok": False, "lat": None, "lon": None, "acc": None, "source": "", "error": ""}
    if os.name != "nt":
        return {**bad, "error": "非 Windows 平台"}
    exe = _find_powershell()
    if not exe:
        return {**bad, "error": "找不到 powershell.exe"}
    if not os.path.exists(_SCRIPT):
        return {**bad, "error": f"探测脚本不存在：{_SCRIPT}"}
    try:
        proc = subprocess.run(
            [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", _SCRIPT],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout)
    except subprocess.TimeoutExpired:
        return {**bad, "error": f"定位超时（{timeout}s）"}
    except Exception as exc:
        return {**bad, "error": f"{type(exc).__name__}: {exc}"}

    lines = [l for l in (proc.stdout or "").splitlines() if l.strip().startswith("{")]
    if not lines:
        err = ((proc.stderr or "").strip().splitlines() or ["（无输出）"])[-1]
        return {**bad, "error": f"探测脚本没有返回 JSON：{err[:120]}"}
    try:
        data = json.loads(lines[-1])
    except Exception as exc:
        return {**bad, "error": f"解析探测结果失败：{type(exc).__name__}"}
    if not data.get("ok"):
        return {**bad, "error": str(data.get("error") or "定位失败")[:120]}
    return {"ok": True, "lat": data.get("lat"), "lon": data.get("lon"),
            "acc": data.get("acc"), "source": str(data.get("source") or "Windows 定位"),
            "error": ""}
