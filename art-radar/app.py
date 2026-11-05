#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ArtRadar 单文件启动器：默认打开素材库 UI，也可以直接跑 CLI 子命令。"""
from __future__ import annotations

import os
import socket
import sys
import traceback
import urllib.request
import webbrowser

CLI_COMMANDS = {
    "init", "login", "collect", "more", "demo", "ingest", "scan",
    "digest", "pending", "pick", "accept", "reject", "stats",
}


def _port_open(port: int) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/api/state", timeout=1.5) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


def _port_listening(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.8)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def _run_ui(args: list[str]) -> None:
    port = 8765
    if "--port" in args:
        i = args.index("--port")
        if i + 1 < len(args):
            port = int(args[i + 1])
    url = f"http://127.0.0.1:{port}/"
    if _port_open(port):
        print(f"ArtRadar 已经在运行：{url}")
        webbrowser.open(url)
        return

    if _port_listening(port):
        print("=" * 56)
        print(f"  端口 {port} 被占用，但 ArtRadar 没有响应。")
        print("  请先在任务管理器结束所有 ArtRadar.exe，再重新启动。")
        print("=" * 56)
        try:
            input("按 Enter 退出…")
        except Exception:  # noqa: BLE001
            pass
        return

    import server
    print("=" * 56)
    print("  ArtRadar 素材库")
    print(f"  界面：{url}")
    print("  关闭这个窗口即可停止服务")
    print("=" * 56)
    sys.argv = ["ArtRadar", "--port", str(port), "--open"]
    server.main()


def main() -> None:
    if os.name == "nt":
        try:
            os.system("title ArtRadar 素材库")
        except Exception:  # noqa: BLE001
            pass
    args = sys.argv[1:]
    # 无参数、ui/serve、或直接 --port：启动界面
    if not args or args[0] in ("ui", "serve") or args[0].startswith("--port"):
        _run_ui(args[1:] if args and args[0] in ("ui", "serve") else args)
        return
    if args[0] in ("-h", "--help", "help"):
        print("用法：")
        print("  ArtRadar.exe                 打开素材库界面")
        print("  ArtRadar.exe ui --port 8765  指定端口")
        print("  ArtRadar.exe stats           查看统计")
        print("  ArtRadar.exe paths           查看实际配置路径")
        print("  ArtRadar.exe collect         采集")
        print("  ArtRadar.exe more 光影       再来一组")
        print("  ArtRadar.exe login           登录 Pixiv / 微博")
        print("  ArtRadar.exe login --pinterest  登录 Pinterest")
        return
    if args[0] == "paths":
        import artradar
        cfg = artradar.load_cfg()
        print("exe dir  :", artradar.ROOT)
        print("resource :", artradar.RESOURCE_DIR)
        print("config   :", artradar.CONFIG_PATH if artradar.CONFIG_PATH.exists()
              else artradar.RESOURCE_DIR / "config.toml")
        print("library  :", cfg["_root"])
        print("meta     :", cfg["_meta"])
        return
    import artradar
    artradar.main()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        if getattr(sys, "frozen", False):
            try:
                input("\n出错了，按 Enter 退出…")
            except Exception:  # noqa: BLE001
                pass
        raise
