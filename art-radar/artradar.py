#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""art-radar — 每日绘画参考 采集 / 策展 / 归档

典型用法：
    python artradar.py init                 # 建目录 + 生成 config.toml
    python artradar.py demo                 # 离线样例，验证界面与归档
    python artradar.py collect              # 抓取 -> 去重 -> 打分 -> 生成待确认清单
    python artradar.py pending --json       # 给 DSH 助手读取的候选清单
    python artradar.py accept 1 3 7 --tag 构图 --note "逆光层次好"
    python artradar.py reject 5 --reason "和库里重复"
    python artradar.py digest --open        # 重新生成并打开 HTML 画廊
    python artradar.py stats
"""
from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import math
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _resource_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", _app_dir()))


ROOT = _app_dir()
RESOURCE_DIR = _resource_dir()
CONFIG_PATH = ROOT / "config.toml"

DEFAULT_CONFIG = r'''# art-radar 配置（改完直接生效，无需重启）
[paths]
root = "library"                 # 图库位置，相对本文件或绝对路径
meta = ".artradar"               # 索引/预览/清单放在图库内的这个隐藏目录

[collect]
per_source = 20                  # 每个来源最多取多少条候选
min_side = 700                   # 短边小于该值的图直接丢弃
thumb_px = 640                   # 预览图最长边
request_interval = 0.8           # 每次下载之间的间隔（秒），别把人家站点打疼
proxy = ""                       # 例如 "http://127.0.0.1:7890"；留空则读 HTTPS_PROXY
timeout = 25

[collect.quality]
enabled = true                   # 自动过滤空白/严重模糊/过度压缩的图

# ---- 来源：按需增删，type 见 README ----
[[sources]]
type = "pixiv_ranking"
enabled = true
name = "pixiv-daily"
mode = "daily"                   # daily / weekly / monthly / rookie / original
pages = 1

[[sources]]
type = "weibo_user"
enabled = false
name = "weibo-某画师"
uid = ""                         # 微博数字 UID
cookie = ""                      # 可选，部分时间线匿名拿不到时填浏览器 Cookie
pages = 1

[[sources]]
type = "rss"
enabled = false
name = "rsshub-pixiv"
url = "https://rsshub.app/pixiv/ranking/day"

[[sources]]
type = "rss"
enabled = false
name = "pinterest-board"
url = "https://www.pinterest.com/<user>/<board>.rss"

[review]
top_n = 15                       # 每天推送多少条
expire_days = 14                 # 一直不确认的候选多久后过期

[filing]
reference_dir = "01_reference"
artists_dir = "02_artists"
favorites_dir = "03_favorites"
tags = ["构图", "光影", "色彩", "人体动态", "服饰", "场景氛围", "材质"]
favorite_artists = []            # 命中这些画师时自动归入 02_artists/<画师>/

[notify]
webhook = ""                     # 可选：企业微信/飞书/Telegram 机器人，留空则只在 DSH 里推送
'''

DEFAULT = tomllib.loads(DEFAULT_CONFIG)


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    print(msg, flush=True)


def merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = v
    return out


def load_cfg(path: Path | None = None) -> dict:
    if path is None:
        path = CONFIG_PATH if CONFIG_PATH.exists() else RESOURCE_DIR / "config.toml"
    if not path.exists():
        raise SystemExit(f"找不到 {path}，先运行: python artradar.py init")
    with open(path, "rb") as f:
        cfg = merge(DEFAULT, tomllib.load(f))
    cfg["_dir"] = path.resolve().parent
    root = Path(os.path.expandvars(str(cfg["paths"]["root"])))
    cfg["_root"] = root if root.is_absolute() else cfg["_dir"] / root
    cfg["_meta"] = cfg["_root"] / str(cfg["paths"].get("meta", ".artradar"))
    return cfg


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def parse_dt(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    s = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d", "%a %b %d %H:%M:%S %z %Y", "%Y年%m月%d日 %H:%M"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
        except ValueError:
            pass
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})", s)
    if m:
        return datetime(*map(int, m.groups()), tzinfo=timezone.utc)
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})", s)
    if m:
        return datetime(*map(int, m.groups()), tzinfo=timezone.utc)
    return None


def http_get(url: str, cfg: dict, *, referer: str | None = None,
             cookie: str | None = None, timeout: int | None = None,
             retries: int = 3) -> bytes:
    """带重试的 GET，支持配置里的代理。返回原始 bytes。"""
    proxy = (cfg["collect"].get("proxy") or os.environ.get("HTTPS_PROXY")
             or os.environ.get("https_proxy") or "")
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
    opener = urllib.request.build_opener(*handlers)
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/json,image/*,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "identity",
    }
    if referer:
        headers["Referer"] = referer
    if cookie:
        headers["Cookie"] = cookie
    last: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            req = urllib.request.Request(url, headers=headers)
            with opener.open(req, timeout=timeout or cfg["collect"]["timeout"]) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt + 1 < retries:
                time.sleep(1.2 * (attempt + 1))
    raise last  # type: ignore[misc]


def _proxy_address(cfg: dict) -> tuple[str, int] | None:
    raw = (cfg["collect"].get("proxy") or os.environ.get("HTTPS_PROXY")
           or os.environ.get("https_proxy") or "")
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urllib.parse.urlparse(raw)
    return parsed.hostname or "127.0.0.1", int(parsed.port or 80)


def proxy_alive(cfg: dict) -> bool:
    addr = _proxy_address(cfg)
    if addr is None:
        return True
    try:
        with socket.create_connection(addr, timeout=2):
            return True
    except OSError:
        return False


def ensure_proxy(cfg: dict) -> None:
    """代理不通时尝试唤起蓝灯并点连接；仍失败就明确报错。"""
    if proxy_alive(cfg):
        return
    script = RESOURCE_DIR / "scripts" / "ensure_lantern.ps1"
    if script.exists():
        flags = 0x08000000 if os.name == "nt" else 0
        try:
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(script)],
                capture_output=True, timeout=90, creationflags=flags)
        except Exception:  # noqa: BLE001
            pass
    if not proxy_alive(cfg):
        raise RuntimeError("蓝灯代理不可用：请打开蓝灯并点一下「连接」")


def strip_html(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text or "")
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def safe_name(text: str, limit: int = 60) -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", text or "")
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:limit] or "untitled"


def item_id(source: str, source_id: str) -> str:
    return hashlib.sha1(f"{source}:{source_id}".encode()).hexdigest()[:8]


# --------------------------------------------------------------------------- #
# 图库 / 数据库
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    artist      TEXT,
    title       TEXT,
    page_url    TEXT,
    image_url   TEXT,
    referer     TEXT,
    width       INTEGER,
    height      INTEGER,
    engagement  INTEGER DEFAULT 0,
    score       REAL DEFAULT 0,
    phash       TEXT,
    thumb       TEXT,
    status      TEXT DEFAULT 'pending',
    tags        TEXT,
    tags_manual TEXT DEFAULT '',
    note        TEXT,
    folder      TEXT,
    filed_path  TEXT,
    published_at TEXT,
    created_at  TEXT,
    decided_at  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_source ON items(source, source_id);
CREATE INDEX IF NOT EXISTS ix_status ON items(status);
"""


def atomic_write(path: Path, text: str) -> None:
    """整份覆写的状态文件先写临时文件再替换，中途崩溃或断电不会留下半截 JSON。"""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def db(cfg: dict) -> sqlite3.Connection:
    path = cfg["_meta"] / "index.sqlite"
    try:
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)
        cols = {r["name"] for r in con.execute("PRAGMA table_info(items)")}
        if "tags_manual" not in cols:
            con.execute("ALTER TABLE items ADD COLUMN tags_manual TEXT DEFAULT ''")
            con.execute("UPDATE items SET tags_manual=COALESCE(tags,'') WHERE status='kept'")
        con.execute(
            "UPDATE items SET tags_manual=COALESCE(tags,'') "
            "WHERE status='kept' AND trim(COALESCE(tags_manual,''))='' "
            "AND trim(COALESCE(tags,''))<>''")
        con.commit()
        return con
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"无法打开索引数据库 {path}: {exc}") from exc


def ensure_layout(cfg: dict) -> None:
    root = cfg["_root"]
    meta = cfg["_meta"]
    for p in (root / "00_inbox", meta / "thumbs", meta / "digests", meta / "sidecars",
              meta / "uploads"):
        p.mkdir(parents=True, exist_ok=True)
    for tag in cfg["filing"]["tags"]:
        (root / cfg["filing"]["reference_dir"] / tag).mkdir(parents=True, exist_ok=True)
    (root / cfg["filing"]["artists_dir"]).mkdir(parents=True, exist_ok=True)
    (root / cfg["filing"]["favorites_dir"]).mkdir(parents=True, exist_ok=True)
    db(cfg).close()


def recent_phashes(con: sqlite3.Connection) -> list[str]:
    rows = con.execute("SELECT phash FROM items WHERE status IN ('pending','kept','library') AND phash IS NOT NULL")
    return [r["phash"] for r in rows]


def hamming(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count()


# --------------------------------------------------------------------------- #
# 图像处理
# --------------------------------------------------------------------------- #
def dhash(img) -> str:
    from PIL import Image
    g = img.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    px = g.tobytes()
    bits = 0
    for y in range(8):
        for x in range(8):
            bits = (bits << 1) | (1 if px[y * 9 + x] > px[y * 9 + x + 1] else 0)
    return f"{bits:016x}"


def quality_issues(img, nbytes: int) -> str:
    """保守的低质量检测：空白、严重模糊、压缩过度、细节极少。

    只拦明显有问题的图，避免误伤速写/线稿/暗调作品。
    """
    try:
        import numpy as np
        w, h = img.size
        g = img.convert("L")
        g.thumbnail((512, 512))
        a = np.asarray(g, dtype=np.float32)
        if float(a.std()) < 6:
            return "近乎空白/纯色"
        lap = np.abs(4 * a[1:-1, 1:-1] - a[:-2, 1:-1] - a[2:, 1:-1]
                     - a[1:-1, :-2] - a[1:-1, 2:])
        blur = float(lap.var())
        edges = float((lap > 18).mean())
        small = img.copy()
        small.thumbnail((256, 256))
        arr = np.asarray(small, dtype=np.int32) >> 3
        codes = (arr[..., 0] * 1024 + arr[..., 1] * 32 + arr[..., 2]).ravel()
        counts = np.bincount(codes, minlength=32768).astype(np.float64)
        p = counts[counts > 0] / counts.sum()
        entropy = float(-(p * np.log2(p)).sum())
        bpp = nbytes / max(1, w * h)
        if entropy < 1.2 and edges < 0.01:
            return "细节极少"
        if blur < 4 and edges < 0.008:
            return "严重模糊"
        if bpp < 0.02 and blur < 12:
            return "压缩过度/模糊"
    except Exception:  # noqa: BLE001
        return ""
    return ""


def make_preview(raw: bytes, cfg: dict, iid: str) -> tuple[str, int, int, str, str]:
    from PIL import Image
    img = Image.open(io.BytesIO(raw))
    img.load()
    w, h = img.size
    img = img.convert("RGB")
    ph = dhash(img)
    issues = quality_issues(img, len(raw))
    img.thumbnail((cfg["collect"]["thumb_px"], cfg["collect"]["thumb_px"]), Image.Resampling.LANCZOS)
    path = cfg["_meta"] / "thumbs" / f"{iid}.jpg"
    img.save(path, "JPEG", quality=86)
    return str(path), w, h, ph, issues


# --------------------------------------------------------------------------- #
# 各来源抓取（统一返回 list[dict]）
# --------------------------------------------------------------------------- #
@contextmanager
def browser_ctx(cfg: dict, headless: bool = True):
    """复用 .artradar/browser 里的登录态；每个采集器独立开关浏览器。"""
    from playwright.sync_api import sync_playwright

    proxy = cfg["collect"].get("proxy") or ""
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(cfg["_meta"] / "browser"),
            channel="chrome",
            headless=headless,
            proxy={"server": proxy} if proxy else None,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1366, "height": 900},
            locale="zh-CN",
            user_agent=UA,
        )
        try:
            yield ctx
        finally:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass


def fetch_pixiv_follow(cfg: dict, src: dict, cancel=None) -> list[dict]:
    out: list[dict] = []
    with browser_ctx(cfg) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.pixiv.net/", wait_until="domcontentloaded", timeout=60000)
        for p in range(1, int(src.get("pages", 1)) + 1):
            if cancel is not None and cancel.is_set():
                break
            data = page.evaluate(
                """async (p) => {
                    const r = await fetch('/ajax/follow_latest/illust?p=' + p + '&mode=all&lang=zh',
                                          {headers: {'Accept': 'application/json'}});
                    const t = await r.text();
                        try { return JSON.parse(t); } catch { return {}; }
                }""", p)
            if data.get("error"):
                raise RuntimeError(f"Pixiv 未登录或接口报错: {data.get('message')}（先运行 login）")
            body = data.get("body") or {}
            thumbs = (body.get("thumbnails") or {}).get("illust") or body.get("illusts") or []
            if isinstance(thumbs, dict):
                thumbs = list(thumbs.values())
            for it in thumbs:
                pid = str(it.get("id") or it.get("illustId") or "")
                if not pid:
                    continue
                if int(it.get("illustType") or 0) != 0:
                    continue  # 跳过漫画/动图
                if int(it.get("xRestrict") or 0) != 0:
                    continue
                img = (it.get("urls") or {}).get("1200x1200") or it.get("url") or ""
                img = re.sub(r"/c/[^/]+/", "/c/1200x1200/", img)
                out.append({
                    "source": "pixiv_follow",
                    "source_id": pid,
                    "artist": it.get("userName") or "",
                    "title": strip_html(it.get("title") or it.get("illustTitle") or ""),
                    "page_url": f"https://www.pixiv.net/artworks/{pid}",
                    "image_url": img,
                    "referer": "https://www.pixiv.net/",
                    "width": int(it.get("width") or 0),
                    "height": int(it.get("height") or 0),
                    "engagement": int(it.get("bookmarkCount") or 0),
                    "tags": ",".join(str(t) for t in (it.get("tags") or [])[:6]),
                    "published_at": iso(parse_dt(it.get("createDate"))),
                })
    return out


def fetch_weibo_follow(cfg: dict, src: dict) -> list[dict]:
    out: list[dict] = []
    with browser_ctx(cfg) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://weibo.com/", wait_until="domcontentloaded", timeout=60000)
        groups = page.evaluate(
            """async () => {
                const r = await fetch('/ajax/feed/allGroups',
                    {credentials: 'include', headers: {'Accept': 'application/json'}});
                const t = await r.text();
                        try { return JSON.parse(t); } catch { return {}; }
            }""")
        list_id = None

        def walk(node) -> None:
            nonlocal list_id
            if isinstance(node, dict):
                if list_id is None and node.get("gid") and node.get("title") in ("全部关注", "关注"):
                    list_id = str(node["gid"])
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(groups)
        if not list_id:
            raise RuntimeError("找不到微博「全部关注」分组，可能未登录（先运行 login）")
        own_uid = None
        for grp in groups.get("groups", []):
            for g in grp.get("group", []):
                if g.get("title") == "全部关注":
                    own_uid = str(g.get("uid") or "")
        art_uids: set[str] = set()
        if own_uid:
            friends: list[dict] = []
            for pg in range(1, 6):
                fd = page.evaluate(
                    """async ([u, pg]) => (await fetch(`/ajax/friendships/friends?page=${pg}&uid=${u}&count=50`,
                        {credentials:'include', headers:{'Accept':'application/json',
                         'X-Requested-With':'XMLHttpRequest', 'Referer':'https://weibo.com/'}})).json()""",
                    [own_uid, pg])
                users = fd.get("users") or []
                if not users:
                    break
                friends.extend(users)
            words = ("画", "绘", "插画", "原画", "概念", "美术", "漫画", "动画",
                     "cos", "摄影", "artist", "illust", "art", "视觉", "设计", "同人")
            skip = ("官方", "专栏", "平台", "Procreate", "摩点", "游戏", "课堂", "教室", "班长")
            for u in friends:
                name = str(u.get("screen_name") or "")
                blob = " ".join(str(u.get(k) or "") for k in ("screen_name", "description", "verified_reason"))
                if any(s in name for s in skip):
                    continue
                if any(w.lower() in blob.lower() for w in words):
                    art_uids.add(str(u.get("id")))

        data = page.evaluate(
            """async (lid) => {
                const r = await fetch(`/ajax/feed/unreadfriendstimeline?list_id=${lid}&refresh=4&since_id=0&count=50`,
                    {credentials: 'include', headers: {'Accept': 'application/json',
                     'X-Requested-With': 'XMLHttpRequest', 'Referer': 'https://weibo.com/'}});
                const t = await r.text();
                        try { return JSON.parse(t); } catch { return {}; }
            }""", list_id)
        statuses = data.get("statuses") or data.get("data") or []
        if not statuses:
            raise RuntimeError("微博关注流为空（接口返回里没有 statuses）")
        for s in statuses:
            if art_uids and str((s.get("user") or {}).get("id")) not in art_uids:
                continue
            if s.get("page_info") or s.get("isAd"):
                continue  # 跳过视频/文章/广告卡片
            pics = s.get("pics") or []
            if not pics:
                infos = s.get("pic_infos") or {}
                for pid in (s.get("pic_ids") or []):
                    info = infos.get(pid) or {}
                    large = info.get("largest") or info.get("large") or {}
                    if large.get("url"):
                        pics.append({"pid": pid, "large": large})
            if not pics:
                continue
            text = strip_html(s.get("text_raw") or s.get("text") or "")
            user = (s.get("user") or {}).get("screen_name") or ""
            mid = str(s.get("id") or s.get("mid") or "")
            mblogid = s.get("mblogid") or mid
            for i, pic in enumerate(pics):
                large = pic.get("large") or {}
                url = large.get("url") or pic.get("url") or ""
                if not url:
                    continue
                out.append({
                    "source": "weibo",
                    "source_id": f"{mid}-{pic.get('pid') or i}",
                    "artist": user,
                    "title": text[:60].replace("\n", " "),
                    "page_url": f"https://weibo.com/{s.get('user', {}).get('id', '')}/{mblogid}",
                    "image_url": url.replace("/mw690/", "/large/").replace("/thumb150/", "/large/"),
                    "referer": "https://weibo.com/",
                    "width": int(large.get("width") or 0),
                    "height": int(large.get("height") or 0),
                    "engagement": int(s.get("attitudes_count") or 0),
                    "tags": "",
                    "published_at": iso(parse_dt(s.get("created_at"))),
                })
    return out


WEIBO_ART_WORDS = ("画", "绘", "插画", "原画", "概念", "美术", "漫画", "动画",
                   "cos", "摄影", "艺术", "设计", "视觉", "素材", "参考",
                   "大触", "速写", "同人", "illust", "artist", "art")
WEIBO_SKIP_WORDS = ("官方", "旗舰", "商城", "专卖", "科技", "手机", "数码",
                    "Procreate", "摩点", "游戏", "招聘", "广告", "电商", "品牌", "福利")
WEIBO_PROMO_WORDS = ("改图服务", "加群", "咨询", "购买", "下单", "优惠", "抽奖",
                     "报名", "课程", "扫码", "私信领取")


def _discover_weibo_art_uids(page, cancel=None, include_all: bool = False) -> list[tuple[str, str]]:
    """从当前账号的关注列表里发现画师/艺术类博主；include_all=True 时返回全部关注。"""
    groups = page.evaluate(
        """async () => {
            const r = await fetch('/ajax/feed/allGroups',
                {credentials: 'include', headers: {'Accept': 'application/json'}});
            const t = await r.text();
            try { return JSON.parse(t); } catch { return {}; }
        }""")
    own_uid = None
    for grp in groups.get("groups", []):
        for g in grp.get("group", []):
            if g.get("title") == "全部关注":
                own_uid = str(g.get("uid") or "")
    if not own_uid:
        return []
    friends: list[dict] = []
    for pg in range(1, 6):
        if cancel is not None and cancel.is_set():
            break
        fd = page.evaluate(
            """async ([u, pg]) => {
                const r = await fetch(`/ajax/friendships/friends?page=${pg}&uid=${u}&count=50`,
                    {credentials: 'include', headers: {'Accept': 'application/json',
                     'X-Requested-With': 'XMLHttpRequest', 'Referer': 'https://weibo.com/'}});
                const t = await r.text();
                try { return JSON.parse(t); } catch { return {}; }
            }""",
            [own_uid, pg])
        users = fd.get("users") or []
        if not users:
            break
        friends.extend(users)
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for u in friends:
        uid = str(u.get("id") or "")
        name = str(u.get("screen_name") or "")
        if not uid or uid in seen:
            continue
        seen.add(uid)
        if include_all:
            out.append((uid, name))
            continue
        blob = " ".join(str(u.get(k) or "") for k in ("screen_name", "description", "verified_reason"))
        if any(s in blob for s in WEIBO_SKIP_WORDS):
            continue
        if any(w.lower() in blob.lower() for w in WEIBO_ART_WORDS):
            out.append((uid, name))
    return out


def fetch_weibo_artists(cfg: dict, src: dict, cancel=None, skip_ids: set[str] | None = None,
                        filter_words: list[str] | None = None) -> list[dict]:
    """按 UID 抓画师/艺术博主动态；自动跳过已入库，必要时向更早的页追溯。"""
    out: list[dict] = []
    per_uid = int(src.get("per_uid", 4))
    per_post = int(src.get("per_post", 0))  # 0 = 一条动态里的图全收
    pages = int(src.get("max_pages", src.get("pages", 1)))
    if skip_ids is None:
        con = db(cfg)
        skip_ids = {r[0] for r in con.execute("SELECT source_id FROM items WHERE source='weibo'")}
        con.close()
    with browser_ctx(cfg) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto("https://weibo.com/", wait_until="domcontentloaded", timeout=60000)
        except Exception:  # noqa: BLE001
            time.sleep(2)
            page.goto("https://weibo.com/", wait_until="domcontentloaded", timeout=60000)
        uids = [str(x) for x in src.get("uids", [])]
        extras: list[tuple[str, str]] = []
        if filter_words:
            try:
                friends = _discover_weibo_art_uids(page, cancel=cancel, include_all=True)
            except Exception as exc:  # noqa: BLE001
                friends = []
                log(f"  读取关注列表失败: {exc}")
            matched = [u for u, n in friends
                       if any(w.lower() in n.lower() for w in filter_words)]
            if matched:
                uids = matched
                per_uid = max(per_uid, 6)
                log("  只抓指定账号: " + "、".join(n for u, n in friends if u in matched))
            else:
                log("  未在关注列表找到匹配账号: " + "、".join(filter_words))
        elif src.get("auto_following"):
            try:
                extras = _discover_weibo_art_uids(page, cancel=cancel)
                if extras:
                    log("  自动纳入艺术类账号: " + "、".join(n for _, n in extras))
                    uids = list(dict.fromkeys(uids + [u for u, _ in extras]))
            except Exception as exc:  # noqa: BLE001
                log(f"  自动发现艺术类账号失败: {exc}")
        for uid in uids:
            if cancel is not None and cancel.is_set():
                break
            added = 0
            posts_taken = 0
            scanned = 0
            new_ids: set[str] = set()
            for pg in range(1, pages + 1):
                if cancel is not None and cancel.is_set():
                    break
                scanned = pg
                try:
                    data = page.evaluate(
                        """async ([u, pg]) => {
                            const r = await fetch(`/ajax/statuses/mymblog?uid=${u}&page=${pg}&feature=0`,
                                {credentials: 'include', headers: {'Accept': 'application/json',
                                 'X-Requested-With': 'XMLHttpRequest', 'Referer': 'https://weibo.com/'}});
                            const t = await r.text();
                            try { return JSON.parse(t); } catch { return {}; }
                        }""", [str(uid), pg])
                except Exception as exc:  # noqa: BLE001
                    log(f"  微博账号 {uid} 第 {pg} 页读取失败: {exc}")
                    time.sleep(0.3)
                    continue
                posts = (data.get("data") or {}).get("list") or data.get("list") or []
                if not posts:
                    break
                for s in posts:
                    if s.get("page_info") or s.get("isAd"):
                        continue
                    pic = s
                    if not (s.get("pic_ids") or s.get("pic_infos")):
                        inner = s.get("retweeted_status") or {}
                        if inner.get("pic_ids") or inner.get("pic_infos"):
                            pic = inner  # 博主转发/搬运：取原帖的图
                    if not (pic.get("pic_ids") or pic.get("pic_infos")):
                        continue
                    text = strip_html(pic.get("text_raw") or pic.get("text") or "")
                    outer_text = strip_html(s.get("text_raw") or s.get("text") or "")
                    if any(w in f"{text} {outer_text}" for w in WEIBO_PROMO_WORDS):
                        continue
                    user = pic.get("user") or {}
                    mid = str(pic.get("id") or pic.get("mid") or "")
                    mblogid = pic.get("mblogid") or mid
                    infos = pic.get("pic_infos") or {}
                    post_added = 0
                    for pid in (pic.get("pic_ids") or []):
                        sid = f"{mid}-{pid}"
                        if sid in skip_ids or sid in new_ids:
                            continue
                        info = infos.get(pid) or {}
                        node = {}
                        for key in ("original", "large", "largest", "bmiddle"):
                            if (info.get(key) or {}).get("url"):
                                node = info[key]
                                break
                        url = node.get("url") or ""
                        if not url:
                            continue
                        if filter_words:
                            account = str((s.get("user") or {}).get("screen_name") or "")
                            blob = f'{account} {user.get("screen_name", "")} {text}'.lower()
                            if not any(w.lower() in blob for w in filter_words):
                                continue
                        out.append({
                            "source": "weibo",
                            "source_id": sid,
                            "artist": user.get("screen_name") or "",
                            "title": text[:60].replace("\n", " "),
                            "page_url": f"https://weibo.com/{user.get('id', '')}/{mblogid}",
                            "image_url": url.replace("/mw690/", "/large/").replace("/thumb150/", "/large/"),
                            "fallback_url": url.replace("/large/", "/mw690/"),
                            "referer": "https://weibo.com/",
                            "width": int(node.get("width") or 0),
                            "height": int(node.get("height") or 0),
                            "engagement": int(pic.get("attitudes_count") or s.get("attitudes_count") or 0),
                            "tags": "",
                            "published_at": iso(parse_dt(pic.get("created_at"))),
                        })
                        new_ids.add(sid)
                        added += 1
                        post_added += 1
                        if per_post > 0 and post_added >= per_post:
                            break
                    if post_added:
                        posts_taken += 1
                    if posts_taken >= per_uid:
                        break
                if posts_taken >= per_uid:
                    break
                time.sleep(0.15)
            log(f"  微博账号 {uid}: 新增 {added} 张 / {posts_taken} 条动态（扫描 {scanned} 页）")
    return out


def login_pinterest(cfg: dict) -> None:
    """单独打开 Pinterest 登录窗口，登录态保存在 .artradar/pinterest-browser。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("需要先安装 playwright") from None
    profile = cfg["_meta"] / "pinterest-browser"
    profile.mkdir(parents=True, exist_ok=True)
    proxy = cfg["collect"].get("proxy") or ""
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(profile), channel="chrome", headless=False,
            proxy={"server": proxy} if proxy else None,
            args=["--disable-blink-features=AutomationControlled"],
            user_agent=UA, locale="zh-CN",
            viewport={"width": 1280, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto("https://www.pinterest.com/login/", wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:  # noqa: BLE001
            log(f"打开 Pinterest 登录页失败: {exc}")
        log("请在弹出的窗口登录 Pinterest；登录成功后窗口会自动关闭（最多等 5 分钟）")
        ok = False
        deadline = time.time() + 300
        while time.time() < deadline:
            time.sleep(2)
            try:
                cookies = {c["name"]: c["value"] for c in ctx.cookies("https://www.pinterest.com")}
                if cookies.get("_auth") == "1":
                    ok = True
                    break
                if page.query_selector('[data-test-id="header-profile"]'):
                    ok = True
                    break
                if page.is_closed():
                    break
            except Exception:  # noqa: BLE001
                break
        if ok:
            log("Pinterest 登录成功，登录态已保存")
        else:
            try:
                names = sorted({c["name"] for c in ctx.cookies("https://www.pinterest.com")})
                log("Pinterest cookie 名称: " + ", ".join(names))
            except Exception:  # noqa: BLE001
                pass
            log("Pinterest 登录超时或未检测到登录态；如果已登录，稍后可直接测试")
        time.sleep(1)
        ctx.close()


def cmd_login(args) -> None:
    cfg = load_cfg()
    ensure_layout(cfg)
    if getattr(args, "pinterest", False):
        login_pinterest(cfg)
        return
    with browser_ctx(cfg, headless=False) as ctx:
        pixiv = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            pixiv.goto("https://www.pixiv.net/", wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:  # noqa: BLE001
            log(f"打开 Pixiv 失败: {exc}")
        weibo = ctx.new_page()
        try:
            weibo.goto("https://weibo.com/login.php", wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:  # noqa: BLE001
            log(f"打开微博失败: {exc}")
        log("请在弹出的 Chrome 里登录 Pixiv（第一个标签）和微博（第二个标签，可直接扫码）。")
        log("两个都登录成功后会自动继续；最多等 15 分钟。")

        deadline = time.time() + 900
        pixiv_ok = weibo_ok = False
        last = 0.0
        while time.time() < deadline:
            if not weibo_ok:
                try:
                    weibo_ok = bool(weibo.evaluate(
                        "() => !!(window.$CONFIG && window.$CONFIG.uid)"))
                except Exception:  # noqa: BLE001
                    pass
            if not pixiv_ok:
                try:
                    r = pixiv.evaluate(
                        "fetch('/ajax/user/extra',{headers:{'Accept':'application/json'}}).then(r=>r.json())")
                    pixiv_ok = bool(r) and not r.get("error") and bool((r.get("body") or {}).get("userId"))
                except Exception:  # noqa: BLE001
                    pass
            if time.time() - last > 15:
                log(f"Pixiv={'已登录' if pixiv_ok else '等待登录'}  微博={'已登录' if weibo_ok else '等待登录'}")
                last = time.time()
            if pixiv_ok and weibo_ok:
                break
            time.sleep(3)

        cookies = ctx.cookies()
        (cfg["_meta"] / "cookies.json").write_text(
            json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"结束：Pixiv={'已登录' if pixiv_ok else '未登录'} 微博={'已登录' if weibo_ok else '未登录'}")
        log(f"Cookie 备份：{cfg['_meta'] / 'cookies.json'}")
        time.sleep(3)


def fetch_pinterest(cfg: dict, src: dict, cancel=None,
                    skip_ids: set[str] | None = None) -> list[dict]:
    """优先用 .artradar/pinterest-browser 里的登录态，没有则匿名搜索。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # noqa: F841
        raise RuntimeError("需要先安装 playwright: pip install playwright") from None

    queries = [q for q in (src.get("queries") or []) if q]
    per_query = int(src.get("per_query", 6))
    limit = int(src.get("limit", 100))
    proxy = cfg["collect"].get("proxy") or ""
    out: list[dict] = []
    if skip_ids is None:
        con = db(cfg)
        skip_ids = {r[0] for r in con.execute("SELECT source_id FROM items WHERE source='pinterest'")}
        con.close()

    with sync_playwright() as p:
        profile_dir = cfg["_meta"] / "pinterest-browser"
        browser = None
        if profile_dir.exists():
            try:
                ctx = p.chromium.launch_persistent_context(
                    str(profile_dir), channel="chrome", headless=True,
                    proxy={"server": proxy} if proxy else None,
                    args=["--disable-blink-features=AutomationControlled"],
                    user_agent=UA, locale="en-US",
                    viewport={"width": 1366, "height": 900})
            except Exception as exc:  # noqa: BLE001
                log(f"  Pinterest 登录态不可用，改用匿名搜索: {exc}")
                browser = p.chromium.launch(
                    channel="chrome", headless=True,
                    proxy={"server": proxy} if proxy else None,
                    args=["--disable-blink-features=AutomationControlled"])
                ctx = browser.new_context(user_agent=UA, locale="en-US",
                                          viewport={"width": 1366, "height": 900})
        else:
            browser = p.chromium.launch(
                channel="chrome", headless=True,
                proxy={"server": proxy} if proxy else None,
                args=["--disable-blink-features=AutomationControlled"])
            ctx = browser.new_context(user_agent=UA, locale="en-US",
                                      viewport={"width": 1366, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        for query in queries:
            if cancel is not None and cancel.is_set():
                break
            if len(out) >= limit:
                break
            # 英文精确词已经有结果时，不再用宽泛中文词兜底，避免混进无关图
            if out and any(ord(ch) > 127 for ch in query):
                continue
            url = "https://www.pinterest.com/search/pins/?q=" + urllib.parse.quote(query)
            try:
                for attempt in range(2):
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=90000)
                        break
                    except Exception:
                        if attempt == 1:
                            raise
                        page.wait_for_timeout(1500)
                page.wait_for_timeout(3500)
                for _ in range(10):
                    page.mouse.wheel(0, 2200)
                    page.wait_for_timeout(1100)
                found = page.eval_on_selector_all(
                    'img[src*="i.pinimg.com"]',
                    """els => els.filter(e => e.naturalWidth >= 150 &&
                         /(236x|474x|736x|originals)/.test(e.src))
                         .map(e => ({src: e.src, srcset: e.srcset || '', alt: e.alt || ''}))""")
            except Exception as exc:  # noqa: BLE001
                log(f"  Pinterest 查询失败 ({query}): {exc}")
                continue

            seen: set[str] = set()
            added = 0
            for item in found:
                url_src = _best_pinimg(item)
                if not url_src:
                    continue
                key = url_src.split("/")[-1]
                if key in seen:
                    continue
                seen.add(key)
                m = re.search(r"/([0-9a-f]{32})\.", url_src)
                sid = m.group(1) if m else hashlib.sha1(url_src.encode()).hexdigest()[:32]
                if sid in skip_ids:
                    continue
                alt = clean_pin_title(item.get("alt") or "")
                out.append({
                    "source": "pinterest",
                    "source_id": sid,
                    "artist": "",
                    "title": alt or f"Pinterest · {query}",
                    "page_url": url,
                    "image_url": url_src,
                    "fallback_url": url_src.replace("/originals/", "/736x/"),
                    "referer": "https://www.pinterest.com/",
                    "width": 0,
                    "height": 0,
                    "engagement": 0,
                    "tags": query,
                    "min_side": int(cfg["collect"].get("pinterest_min_side", 500)),
                    "published_at": None,
                })
                added += 1
                if added >= per_query or len(out) >= limit:
                    break
            if len(out) >= limit:
                break
            log(f"  Pinterest[{query}] 取到 {added} 张")
            page.wait_for_timeout(1500)
        if browser is not None:
            browser.close()
        else:
            ctx.close()
    return out


def _best_pinimg(item: dict) -> str | None:
    """把任意尺寸的 pinimg 地址归一成 originals；60x60 头像是噪声，丢掉。"""
    cands: list[str] = []
    for part in (item.get("srcset") or "").split(","):
        piece = part.strip().split(" ")[0]
        if piece.startswith("http") and "i.pinimg.com" in piece:
            cands.append(piece)
    src = item.get("src") or ""
    if "i.pinimg.com" in src:
        cands.append(src)
    for c in cands:
        if "/60x60/" in c:
            continue
        m = re.match(r"https://i\.pinimg\.com/[^/]+/(.+)$", c)
        if m:
            return "https://i.pinimg.com/originals/" + m.group(1)
    return None


def fetch_pixiv_tag(cfg: dict, src: dict, cancel=None) -> list[dict]:
    """按 tag 搜索；默认用 Pixiv 的 popular_d 热度排序，并向后翻页补量。"""
    out: list[dict] = []
    order = src.get("order", "popular_d")
    per_tag = int(src.get("per_tag", 20))
    pages = max(1, int(src.get("pages", 1)))
    seen: set[str] = set()
    for tag in src.get("tags", []):
        if cancel is not None and cancel.is_set():
            break
        added = 0
        for page in range(1, pages + 1):
            if cancel is not None and cancel.is_set():
                break
            url = ("https://www.pixiv.net/ajax/search/artworks/" + urllib.parse.quote(tag)
                   + f"?word={urllib.parse.quote(tag)}&order={order}&mode=all&p={page}"
                   + "&s_mode=s_tag_full&type=all&lang=zh")
            data = json.loads(http_get(url, cfg, referer="https://www.pixiv.net/"))
            items = ((data.get("body") or {}).get("illustManga") or {}).get("data") or []
            if not items:
                break
            for it in items:
                if int(it.get("xRestrict") or 0) != 0:
                    continue  # 跳过 R-18
                if int(it.get("illustType") or 0) != 0:
                    continue  # 跳过漫画/动图
                pid = str(it.get("id") or "")
                if not pid or pid in seen:
                    continue
                img = re.sub(r"/c/[^/]+/", "/c/1200x1200/", it.get("url") or "")
                out.append({
                    "source": "pixiv_tag",
                    "source_id": pid,
                    "artist": it.get("userName") or "",
                    "title": strip_html(it.get("title") or ""),
                    "page_url": f"https://www.pixiv.net/artworks/{pid}",
                    "image_url": img,
                    "referer": "https://www.pixiv.net/",
                    "width": int(it.get("width") or 0),
                    "height": int(it.get("height") or 0),
                    "engagement": int(it.get("bookmarkCount") or 0),
                    "tags": tag,
                    "published_at": iso(parse_dt(it.get("createDate"))),
                })
                seen.add(pid)
                added += 1
                if added >= per_tag:
                    break
            if added >= per_tag:
                break
    return out


def fetch_pixiv(cfg: dict, src: dict, cancel=None) -> list[dict]:
    out: list[dict] = []
    pages = int(src.get("pages", 1))
    for page in range(1, pages + 1):
        if cancel is not None and cancel.is_set():
            break
        url = (f"https://www.pixiv.net/ranking.php?mode={src.get('mode', 'daily')}"
               f"&content=illust&p={page}&format=json")
        data = json.loads(http_get(url, cfg, referer="https://www.pixiv.net/"))
        for c in data.get("contents", []):
            if str(c.get("illust_type")) != "0":
                continue  # 只要单张插画，跳过漫画/动图
            img = c.get("url") or ""
            if not img:
                continue
            # 榜单给的是缩略图，换成 1200px 预览（仍带 pximg Referer）
            img = re.sub(r"/c/[^/]+/", "/c/1200x1200/", img)
            pid = str(c.get("illust_id"))
            title = strip_html(c.get("title") or "")
            tags = ",".join(str(t) for t in (c.get("tags") or [])[:6])
            out.append({
                "source": "pixiv",
                "source_id": pid,
                "artist": c.get("user_name") or "",
                "title": title,
                "page_url": f"https://www.pixiv.net/artworks/{pid}",
                "image_url": img,
                "referer": "https://www.pixiv.net/",
                "width": int(c.get("width") or 0),
                "height": int(c.get("height") or 0),
                "engagement": int(c.get("view_count") or c.get("rating_count") or 0),
                "tags": tags,
                "published_at": iso(parse_dt(c.get("date"))),
            })
    return out


def fetch_weibo_user(cfg: dict, src: dict) -> list[dict]:
    uid = str(src.get("uid") or "").strip()
    if not uid:
        raise ValueError("weibo_user 需要填 uid")
    out: list[dict] = []
    cookie = src.get("cookie") or None
    for page in range(1, int(src.get("pages", 1)) + 1):
        url = ("https://m.weibo.cn/api/container/getIndex?"
               + urllib.parse.urlencode({"type": "uid", "value": uid,
                                         "containerid": f"107603{uid}", "page": page}))
        raw = http_get(url, cfg, referer=f"https://m.weibo.cn/u/{uid}", cookie=cookie)
        data = json.loads(raw)
        if data.get("ok") != 1:
            raise RuntimeError(f"微博返回异常: {data.get('msg') or data}")
        for card in data.get("data", {}).get("cards", []):
            mb = card.get("mblog")
            if not mb or not mb.get("pics"):
                continue
            text = strip_html(mb.get("text") or "")
            for i, pic in enumerate(mb["pics"]):
                large = (pic.get("large") or {}).get("url") or pic.get("url")
                if not large:
                    continue
                out.append({
                    "source": "weibo",
                    "source_id": f"{mb.get('id')}-{pic.get('pid') or i}",
                    "artist": (mb.get("user") or {}).get("screen_name") or "",
                    "title": text[:60].replace("\n", " "),
                    "page_url": f"https://m.weibo.cn/detail/{mb.get('id')}",
                    "image_url": large,
                    "referer": "https://weibo.com/",
                    "width": 0,
                    "height": 0,
                    "engagement": int(mb.get("attitudes_count") or 0),
                    "tags": "",
                    "published_at": iso(parse_dt(mb.get("created_at"))),
                })
    return out


def _first_img(text: str) -> str | None:
    m = re.search(r'<img[^>]+src=["\']([^"\']+)', text or "", re.I)
    return html.unescape(m.group(1)) if m else None


def fetch_rss(cfg: dict, src: dict) -> list[dict]:
    url = src.get("url")
    if not url:
        raise ValueError("rss 来源需要填 url")
    raw = http_get(url, cfg)
    root = ET.fromstring(raw)

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1].lower()

    entries = [e for e in root.iter() if local(e.tag) in ("item", "entry")]
    out: list[dict] = []
    for e in entries:
        fields: dict[str, str] = {}
        media: str | None = None
        for child in e:
            name = local(child.tag)
            if name in ("content", "encoded") and child.text:
                fields.setdefault("content", child.text)
            elif name in ("description", "summary") and child.text:
                fields.setdefault("description", child.text)
            elif name == "link":
                fields["link"] = (child.text or "").strip() or child.get("href", "")
            elif name == "title" and child.text:
                fields["title"] = child.text
            elif name in ("pubdate", "published", "updated", "date") and child.text:
                fields.setdefault("date", child.text)
            elif name == "content" and child.get("url"):
                media = child.get("url")
            elif name == "thumbnail" and child.get("url"):
                media = media or child.get("url")
            elif name == "enclosure" and child.get("url"):
                media = media or child.get("url")
        link = fields.get("link", "")
        img = media or _first_img(fields.get("content", "")) or _first_img(fields.get("description", ""))
        if not img:
            continue
        out.append({
            "source": "rss",
            "source_id": link or img,
            "artist": src.get("name", "rss"),
            "title": strip_html(fields.get("title", ""))[:80],
            "page_url": link,
            "image_url": img,
            "referer": urllib.parse.urljoin(url, "/"),
            "width": 0,
            "height": 0,
            "engagement": 0,
            "tags": "",
            "published_at": iso(parse_dt(fields.get("date"))),
        })
    return out


FETCHERS = {
    "pixiv_ranking": fetch_pixiv,
    "pixiv_tag": fetch_pixiv_tag,
    "pixiv_follow": fetch_pixiv_follow,
    "weibo_user": fetch_weibo_user,
    "weibo_follow": fetch_weibo_follow,
    "weibo_artists": fetch_weibo_artists,
    "rss": fetch_rss,
    "pinterest_board": fetch_rss,  # Pinterest 官方 RSS，能用时直接走 rss
    "pinterest_search": fetch_pinterest,
}


# --------------------------------------------------------------------------- #
# 打分
# --------------------------------------------------------------------------- #
def score_item(it: dict, max_engagement: int, novelty: int) -> float:
    eng = 0.0
    if max_engagement > 0 and it["engagement"] > 0:
        eng = math.log10(1 + it["engagement"]) / math.log10(1 + max_engagement)
    res = min(1.0, min(it["width"], it["height"]) / 1200)
    dt = parse_dt(it.get("published_at"))
    fresh = 0.5 if dt is None else math.exp(-max(0, (now_utc() - dt).days) / 14)
    nov = min(1.0, novelty / 24)
    return round(0.40 * eng + 0.25 * res + 0.15 * fresh + 0.20 * nov, 4)


# --------------------------------------------------------------------------- #
# 采集主流程
# --------------------------------------------------------------------------- #
def ingest_candidates(cfg: dict, con: sqlite3.Connection, cands: list[dict],
                      label: str = "", cancel=None, stop_after: int | None = None) -> int:
    if not cands:
        if label:
            log(f"[{label}] 没有候选")
        return 0
    max_eng = max((c["engagement"] for c in cands), default=0)
    hashes = recent_phashes(con)
    kept = filtered = 0
    seen = small = dup = failed = 0
    quality_on = bool(cfg["collect"].get("quality", {}).get("enabled", True))
    for it in cands:
        if stop_after and kept >= stop_after:
            break
        if cancel is not None and cancel.is_set():
            log(f"[{label}] 已取消")
            break
        iid = item_id(it["source"], str(it["source_id"]))
        if con.execute("SELECT 1 FROM items WHERE id=?", (iid,)).fetchone():
            seen += 1
            continue
        try:
            raw = http_get(it["image_url"], cfg, referer=it.get("referer"),
                           timeout=15, retries=1)
        except Exception as exc:  # noqa: BLE001
            fallback = it.get("fallback_url")
            if not fallback:
                log(f"  跳过 {iid}: 图片下载失败 {exc}")
                failed += 1
                continue
            try:
                raw = http_get(fallback, cfg, referer=it.get("referer"),
                               timeout=15, retries=1)
                it["image_url"] = fallback
            except Exception:  # noqa: BLE001
                log(f"  跳过 {iid}: 图片下载失败 {exc}")
                failed += 1
                continue
        try:
            thumb, w, h, ph, issues = make_preview(raw, cfg, iid)
        except Exception as exc:  # noqa: BLE001
            log(f"  跳过 {iid}: 图片解析失败 {exc}")
            failed += 1
            continue
        if min(w, h) < int(it.get("min_side") or cfg["collect"]["min_side"]):
            small += 1
            continue
        if max(w, h) > 3.0 * max(1, min(w, h)):
            small += 1
            continue  # 长条图/拼图，不适合做单张参考
        novelty = min((hamming(ph, x) for x in hashes), default=99)
        if novelty <= 6:
            dup += 1
            continue  # 和已有 / 本批重复
        status = "pending"
        note = ""
        if quality_on and issues:
            status = "rejected"
            note = f"自动过滤：{issues}"
        it.update(id=iid, width=w, height=h, phash=ph, thumb=thumb, status=status, note=note,
                  score=score_item({**it, "width": w, "height": h}, max_eng, novelty),
                  created_at=iso(now_utc()))
        con.execute(
            """INSERT INTO items (id,source,source_id,artist,title,page_url,image_url,referer,
               width,height,engagement,score,phash,thumb,status,tags,note,published_at,created_at)
               VALUES (:id,:source,:source_id,:artist,:title,:page_url,:image_url,:referer,
               :width,:height,:engagement,:score,:phash,:thumb,:status,'',:note,:published_at,:created_at)""",
            it)
        con.commit()
        hashes.append(ph)
        if status == "pending":
            kept += 1
        else:
            filtered += 1
        time.sleep(float(cfg["collect"]["request_interval"]))
    if label:
        extra = []
        if seen:
            extra.append(f"已存在 {seen}")
        if filtered:
            extra.append(f"自动过滤 {filtered}")
        if dup:
            extra.append(f"重复 {dup}")
        if small:
            extra.append(f"尺寸不符 {small}")
        if failed:
            extra.append(f"失败 {failed}")
        log(f"[{label}] 新增 {kept} 条候选" + ("，" + "，".join(extra) if extra else ""))
    return kept


def cap_weibo_posts(cands: list[dict], limit: int) -> list[dict]:
    """按微博条数截断，但不把一条多图动态切一半。"""
    out: list[dict] = []
    last = None
    for c in cands:
        if limit and len(out) >= limit and c.get("page_url") != last:
            break
        out.append(c)
        last = c.get("page_url")
    return out


def cmd_collect(args) -> None:
    cfg = load_cfg()
    ensure_proxy(cfg)
    ensure_layout(cfg)
    con = db(cfg)
    cutoff = iso(now_utc() - timedelta(days=int(cfg["review"]["expire_days"])))
    con.execute("UPDATE items SET status='expired', decided_at=? WHERE status='pending' AND filed_path IS NULL AND created_at<?",
                (iso(now_utc()), cutoff))
    con.commit()

    total = 0
    for src in cfg["sources"]:
        if not src.get("enabled", True):
            continue
        name = src.get("name") or src["type"]
        if args.source and args.source not in (name, src["type"]):
            continue
        fetcher = FETCHERS.get(src["type"])
        if not fetcher:
            log(f"[{name}] 未知来源类型 {src['type']}，跳过")
            continue
        try:
            cands = fetcher(cfg, src)
        except Exception as exc:  # noqa: BLE001
            log(f"[{name}] 抓取失败: {exc}")
            continue
        lim = int(src.get("limit", cfg["collect"]["per_source"]))
        if src["type"] == "weibo_artists":
            cands = cap_weibo_posts(cands, lim)
        else:
            cands = cands[:lim]
        total += ingest_candidates(cfg, con, cands, name)

    log(f"本轮共新增 {total} 条，生成待确认清单…")
    path, items = write_digest(cfg, con)
    log(f"画廊: {path}")
    if items:
        preview = "、".join(f'{i["index"]}.{i["artist"] or "未知"}' for i in items[:8])
        log(f"待确认 {len(items)} 条：{preview}")


def detect_mode(text: str) -> str:
    low = (text or "").lower()
    if (text or "").strip().startswith("@"):
        return "weibo"
    if "微博" in text or "weibo" in low:
        return "weibo"
    if "pixiv" in low:
        return "pixiv"
    if "pinterest" in low:
        return "pinterest"
    return "auto"


PIN_ALT_PREFIX = re.compile(
    r"^\s*(this may contain|may contain|其中包括图片|其中可能包括图片|此图片可能包含|この画像に含まれているもの)\s*[:：]?\s*",
    re.I)


def clean_pin_title(title: str) -> str:
    """去掉 Pinterest 自动生成的 alt 前缀（「其中包括图片：」），只剩前缀时返回空。"""
    t = PIN_ALT_PREFIX.sub("", title or "").strip()
    return "" if t.lower() == "pin" else t


def clean_keywords(text: str) -> list[str]:
    t = (text or "").replace("“", "").replace("”", "").replace('"', "").replace("'", "")
    t = t.replace("@", " ")
    t = re.sub(r"(博主|画师|账号|用户|主页)", " ", t)
    s = re.sub(r"(帮我|请|麻烦|找一?组|找|搜索|搜|推送|画师|作品|参考|图片|新图|新作|"
               r"在|再|从|里面|里|的|一组|一些|几张)", " ", t)
    parts = [p.strip() for p in re.split(r"[,，\s]+", s) if p.strip()]
    drop = {"微博", "weibo", "pixiv", "pinterest",
            "再", "在", "帮", "我", "要", "给", "来", "看", "请", "的",
            "相关", "有关", "类似", "左右", "照片", "一张", "图片", "素材"}
    return [p for p in parts if p.lower() not in drop][:4]


PIN_CATEGORY_EN = [
    ("服装", "costume"), ("衣服", "clothing"), ("服饰", "costume"),
    ("穿搭", "outfit"), ("鞋子", "shoes"), ("帽子", "hat"),
    ("发型", "hairstyle"), ("配饰", "accessories"),
    ("服", "costume"), ("装", "outfit"),
]
PIN_QUERY_EN = {
    "侦探": ["detective"],
    "光影": ["cinematic lighting", "lighting"],
    "构图": ["composition"],
    "人体": ["anatomy"],
    "场景": ["environment concept art"],
    "服装": ["costume design"],
    "cosplay": ["cosplay"],
    "赛博朋克": ["cyberpunk"],
    "动态": ["dynamic pose"],
    "速写": ["sketch"],
    "色彩": ["color study"],
    "透视": ["perspective"],
    "建筑": ["architecture"],
    "武器": ["weapon design"],
    "角色": ["character design"],
    "头像": ["portrait"],
    "氛围": ["atmosphere"],
}
PIN_EXACT_QUERY = {
    "侦探服": ["detective costume", "detective outfit",
               "sherlock holmes costume", "detective fashion",
               "detective coat", "vintage trench coat"],
    "侦探": ["detective", "detective outfit", "sherlock holmes"],
    "光影": ["cinematic lighting", "lighting reference"],
    "服装": ["costume design", "fashion reference"],
    "赛博朋克": ["cyberpunk fashion", "cyberpunk reference"],
}


def expand_pinterest_queries(keywords: list[str]) -> list[str]:
    """把中文需求拆成「英文精确短语 + 原词 + 去掉品类后缀」，提高 Pinterest 命中率。"""
    out: list[str] = []
    for kw in keywords:
        variants: list[str] = []
        base, cat_en = kw, ""
        for suffix, en in PIN_CATEGORY_EN:
            if kw.endswith(suffix) and len(kw) > len(suffix) + 1:
                base = kw[: -len(suffix)]
                cat_en = en
                break
        exact = PIN_EXACT_QUERY.get(kw)
        english = exact or []
        if not english:
            english = PIN_QUERY_EN.get(base) or PIN_QUERY_EN.get(kw) or []
        if english:
            for term in english:
                if exact:
                    variants.append(term)
                else:
                    variants.append(f"{term} {cat_en}".strip() if cat_en else term)
        variants.append(kw)
        if base != kw:
            variants.append(base)
        if not english and not re.search(r"[a-zA-Z]", kw):
            variants.append(f"{base} reference")
        for v in variants:
            v = v.strip()
            if v and v not in out:
                out.append(v)
    return out[:8]


def rank_candidates(cands: list[dict]) -> list[dict]:
    """按互动热度、分辨率和来源轮转排序；同一来源内高热/高分辨率优先。"""
    groups: dict[str, list[dict]] = {}
    seen: set[tuple[str, str]] = set()
    for c in cands:
        key = (str(c.get("source") or ""), str(c.get("source_id") or ""))
        if not key[1] or key in seen:
            continue
        seen.add(key)
        groups.setdefault(key[0], []).append(c)
    for group in groups.values():
        group.sort(key=lambda c: (
            int(c.get("engagement") or 0),
            min(int(c.get("width") or 0), int(c.get("height") or 0)),
            int(c.get("width") or 0) * int(c.get("height") or 0),
        ), reverse=True)
    out: list[dict] = []
    idx = 0
    while True:
        any_left = False
        for group in groups.values():
            if idx < len(group):
                out.append(group[idx])
                any_left = True
        if not any_left:
            break
        idx += 1
    return out


def collect_more(cfg: dict, keywords: list[str], text: str = "", cancel=None,
                 platform: str = "all", max_results: int = 10) -> int:
    """「再来一组」：按平台/关键词搜索，多抓候选后按热度/质量补足数量。"""
    ensure_proxy(cfg)
    con = db(cfg)
    cands: list[dict] = []
    max_results = max(1, min(50, int(max_results)))
    fetch_target = min(50, max_results * 4)
    mode = platform if platform in ("weibo", "pixiv", "pinterest") else detect_mode(text)

    def stopped() -> bool:
        return cancel is not None and cancel.is_set()

    if mode == "weibo":
        src = next((s for s in cfg["sources"] if s["type"] == "weibo_artists"), {})
        src = dict(src or {})
        # 每个账号多取几条动态，再按点赞/质量排序；页数也加深，自动往更早翻
        src["per_uid"] = 2
        src["pages"] = max(10, int(src.get("pages", 1)))
        src["max_pages"] = max(10, int(src.get("max_pages", 1)))
        try:
            kws = [k for k in keywords if len(k) >= 2]
            cands += fetch_weibo_artists(cfg, src, cancel=cancel,
                                         filter_words=kws or None)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"微博抓取失败: {exc}") from exc
    elif mode == "pixiv":
        if not keywords:
            try:
                cands += fetch_pixiv_follow(cfg, {"pages": 3}, cancel=cancel)
            except Exception as exc:  # noqa: BLE001
                log(f"[more] Pixiv 关注流失败: {exc}")
        if not stopped():
            tags = keywords or ["構図", "光", "ポーズ", "背景"]
            try:
                cands += fetch_pixiv_tag(cfg, {
                    "tags": tags,
                    "per_tag": max(10, fetch_target // max(1, len(tags))),
                    "pages": 3, "order": "popular_d"}, cancel=cancel)
            except Exception as exc:  # noqa: BLE001
                log(f"[more] Pixiv 标签失败: {exc}")
        if not stopped() and not keywords:
            try:
                cands += fetch_pixiv(cfg, {"mode": "daily", "pages": 3}, cancel=cancel)
            except Exception as exc:  # noqa: BLE001
                log(f"[more] Pixiv 榜单失败: {exc}")
    elif mode == "pinterest":
        try:
            queries = expand_pinterest_queries(keywords) or [
                "cinematic lighting reference", "dynamic pose reference",
                "environment concept art", "composition reference"]
            cands += fetch_pinterest(cfg, {"queries": queries, "per_query": fetch_target,
                                           "limit": fetch_target}, cancel=cancel)
        except Exception as exc:  # noqa: BLE001
            log(f"[more] Pinterest 失败: {exc}")
    elif keywords:
        for kw in keywords[:4]:
            if stopped():
                break
            try:
                cands += fetch_pixiv_tag(cfg, {
                    "tags": [kw], "per_tag": max(12, fetch_target // len(keywords)),
                    "pages": 2, "order": "popular_d"}, cancel=cancel)
            except Exception as exc:  # noqa: BLE001
                log(f"[more] Pixiv 搜索 {kw} 失败: {exc}")
            if stopped():
                break
            try:
                cands += fetch_pinterest(cfg, {
                    "queries": expand_pinterest_queries([kw]),
                    "per_query": min(20, fetch_target),
                    "limit": min(30, fetch_target)}, cancel=cancel)
            except Exception as exc:  # noqa: BLE001
                log(f"[more] Pinterest 搜索 {kw} 失败: {exc}")
    else:
        try:
            cands += fetch_pixiv(cfg, {"mode": "daily", "pages": 3}, cancel=cancel)
        except Exception as exc:  # noqa: BLE001
            log(f"[more] Pixiv 榜单失败: {exc}")
        if not stopped():
            try:
                cands += fetch_pixiv_tag(cfg, {"tags": ["構図", "光", "ポーズ", "背景"],
                                               "per_tag": 12, "pages": 2,
                                               "order": "popular_d"}, cancel=cancel)
            except Exception as exc:  # noqa: BLE001
                log(f"[more] Pixiv 标签失败: {exc}")
        if not stopped():
            try:
                cands += fetch_pinterest(cfg, {
                    "queries": ["cinematic lighting reference", "dynamic pose reference",
                                "environment concept art", "composition reference"],
                    "per_query": 12, "limit": fetch_target}, cancel=cancel)
            except Exception as exc:  # noqa: BLE001
                log(f"[more] Pinterest 失败: {exc}")
    if stopped():
        return 0
    cands = rank_candidates(cands)
    log(f"[more] 候选 {len(cands)} 张，按热度/质量排序后尝试补足 {max_results} 张")
    cands = cands[:fetch_target]
    return ingest_candidates(cfg, con, cands, "more", cancel=cancel,
                             stop_after=max_results)


def cmd_more(args) -> None:
    cfg = load_cfg()
    ensure_layout(cfg)
    text = " ".join(args.keywords)
    added = collect_more(cfg, clean_keywords(text), text)
    log(f"再来一组：新增 {added} 张")


# --------------------------------------------------------------------------- #
# 待确认清单 / 画廊
# --------------------------------------------------------------------------- #
def load_snapshot(cfg: dict) -> list[dict]:
    """上一次生成的待确认清单。编号一旦发出去就固定，避免 reject 后编号漂移。"""
    path = cfg["_meta"] / "pending.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("items", [])
    except (OSError, ValueError):
        return []


def pending_items(cfg: dict, con: sqlite3.Connection, limit: int | None = None,
                  use_snapshot: bool = True) -> list[dict]:
    if limit is None:
        limit = int(cfg["review"]["top_n"])
    snap = load_snapshot(cfg) if use_snapshot else []
    if snap:
        alive = {r["id"] for r in con.execute("SELECT id FROM items WHERE status='pending'")}
        out = [it for it in snap if it["id"] in alive][:limit]
        if out:
            return out
    rows = con.execute(
        "SELECT * FROM items WHERE status='pending' ORDER BY score DESC, created_at DESC LIMIT ?",
        (limit,)).fetchall()
    out = []
    for i, r in enumerate(rows, 1):
        thumb = r["thumb"] or ""
        out.append({
            "index": i,
            "id": r["id"],
            "source": r["source"],
            "artist": r["artist"],
            "title": r["title"],
            "score": round(r["score"], 3),
            "tags": r["tags"] or "",
            "page_url": r["page_url"],
            "thumb": str(Path(thumb).resolve()) if thumb else "",
            "width": r["width"],
            "height": r["height"],
        })
    return out


def write_digest(cfg: dict, con: sqlite3.Connection) -> tuple[Path, list[dict]]:
    items = pending_items(cfg, con, limit=200, use_snapshot=False)
    day = datetime.now().strftime("%Y-%m-%d")
    path = cfg["_meta"] / "digests" / f"{day}.html"
    atomic_write(cfg["_meta"] / "pending.json",
                 json.dumps({"generated_at": iso(now_utc()), "items": items},
                            ensure_ascii=False, indent=2))

    cards = []
    for it in items:
        src = f'{it["source"]} · {it["score"]}'
        cards.append(f'''
      <a class="card" href="{html.escape(it["page_url"] or "#")}" target="_blank">
        <span class="idx">{it["index"]}</span>
        <img loading="lazy" src="../thumbs/{Path(it["thumb"]).name if it["thumb"] else ""}" alt="">
        <div class="meta">
          <div class="artist">{html.escape(it["artist"] or "未知画师")}</div>
          <div class="title">{html.escape(it["title"] or "")}</div>
          <div class="src">{html.escape(src)}</div>
        </div>
      </a>''')
    body = "\n".join(cards) or '<p class="empty">今天没有新候选。检查来源是否可用，或放宽 min_side。</p>'
    doc = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>art-radar · {day}</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 28px 22px 60px; background: #121216; color: #ececf1;
         font: 14px/1.5 "Segoe UI", "Microsoft YaHei", sans-serif; }}
  header {{ display: flex; align-items: baseline; gap: 14px; margin-bottom: 22px; }}
  h1 {{ font-size: 20px; margin: 0; font-weight: 600; letter-spacing: .02em; }}
  .hint {{ color: #8b8b99; font-size: 12px; }}
  .grid {{ display: grid; gap: 16px; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); }}
  .card {{ position: relative; display: block; background: #1b1b22; border-radius: 10px;
          overflow: hidden; text-decoration: none; color: inherit; border: 1px solid #2a2a34;
          transition: transform .15s ease, border-color .15s ease; }}
  .card:hover {{ transform: translateY(-2px); border-color: #6b5cff; }}
  .card img {{ width: 100%; aspect-ratio: 3/4; object-fit: cover; display: block; background: #222; }}
  .idx {{ position: absolute; top: 8px; left: 8px; z-index: 1; min-width: 26px; height: 26px;
         padding: 0 7px; border-radius: 13px; background: #6b5cff; color: #fff; font-weight: 700;
         display: flex; align-items: center; justify-content: center; font-size: 13px; }}
  .meta {{ padding: 9px 11px 11px; }}
  .artist {{ font-weight: 600; font-size: 13px; }}
  .title {{ color: #b9b9c6; font-size: 12px; margin-top: 2px; overflow: hidden;
           text-overflow: ellipsis; white-space: nowrap; }}
  .src {{ color: #6f6f80; font-size: 11px; margin-top: 6px; text-transform: uppercase; letter-spacing: .04em; }}
  .empty {{ color: #8b8b99; }}
</style></head>
<body>
<header><h1>art-radar · {day}</h1>
<span class="hint">共 {len(items)} 条待确认 · 在对话里回复编号即可归档</span></header>
<main class="grid">{body}
</main></body></html>'''
    path.write_text(doc, encoding="utf-8")
    return path, items


def cmd_digest(args) -> None:
    cfg = load_cfg()
    ensure_layout(cfg)
    con = db(cfg)
    path, items = write_digest(cfg, con)
    log(f"{len(items)} 条待确认 -> {path}")
    if args.open and sys.platform == "win32":
        os.startfile(path)  # noqa: S606


def cmd_pending(args) -> None:
    cfg = load_cfg()
    con = db(cfg)
    items = pending_items(cfg, con, args.top)
    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return
    if not items:
        log("没有待确认的候选。")
        return
    for it in items:
        log(f'{it["index"]:>2}. {it["id"]}  {it["score"]:<5}  '
            f'{it["source"]:<6} {it["artist"] or "未知":<14} {it["title"][:30]}')


# --------------------------------------------------------------------------- #
# 归档 / 拒绝
# --------------------------------------------------------------------------- #
def resolve_ref(con: sqlite3.Connection, cfg: dict, ref: str) -> str:
    ref = ref.strip()
    if ref.isdigit() and len(ref) <= 2:
        idx = int(ref)
        for it in load_today(cfg):  # 优先按「今日精选」的编号
            if it.get("index") == idx:
                return it["id"]
        for it in load_snapshot(cfg):
            if it.get("index") == idx:
                return it["id"]
        rows = pending_items(cfg, con, 60)
        for it in rows:
            if it["index"] == idx:
                return it["id"]
        raise SystemExit(f"编号 {ref} 不在当前待确认清单里（清单共 {len(rows)} 条）")
    return ref


def fetch_full_image(row: sqlite3.Row, cfg: dict) -> tuple[bytes, str]:
    """优先拿原图；失败则退回预览图。只对 pixiv 尝试解析原图。"""
    url = row["image_url"] or ""
    ext = Path(urllib.parse.urlparse(url).path).suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        ext = ".jpg"
    if row["source"] == "pixiv" and row["page_url"]:
        try:
            page = http_get(row["page_url"], cfg, referer="https://www.pixiv.net/",
                            timeout=15, retries=1).decode("utf-8", "ignore")
            m = re.search(r'"original":"(https://i\.pximg\.net/[^"]+)"', page)
            if m:
                orig = m.group(1).replace("\\/", "/")
                data = http_get(orig, cfg, referer="https://www.pixiv.net/",
                                timeout=20, retries=1)
                return data, Path(urllib.parse.urlparse(orig).path).suffix or ext
        except Exception:  # noqa: BLE001
            pass
    if url.startswith(("http://", "https://")):
        try:
            return http_get(url, cfg, referer=row["referer"], timeout=20, retries=1), ext
        except Exception:
            alt = url.replace("/large/", "/mw690/")
            if alt != url:
                return http_get(alt, cfg, referer=row["referer"], timeout=20, retries=1), ext
            raise
    p = Path(url)
    if p.exists():
        return p.read_bytes(), p.suffix or ext
    return Path(row["thumb"]).read_bytes(), ".jpg"


def ingest_upload(cfg: dict, con: sqlite3.Connection, raw: bytes, filename: str) -> dict:
    """接收用户发来的图片：保存原图、生成预览、自动建议分类，加入待确认。"""
    from PIL import Image

    if not raw:
        return {"ok": False, "error": "空文件"}
    if len(raw) > 50 * 1024 * 1024:
        return {"ok": False, "error": "图片超过 50MB"}
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
        img = img.convert("RGB")
        w, h = img.size
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"不是有效图片: {exc}"}
    if min(w, h) < 300:
        return {"ok": False, "error": "图片太小（短边至少 300px）"}

    digest = hashlib.sha1(raw).hexdigest()
    iid = item_id("upload", digest)
    if con.execute("SELECT 1 FROM items WHERE id=?", (iid,)).fetchone():
        return {"ok": False, "error": "这张图已经在待确认/图库里了"}

    try:
        thumb, w, h, ph, issues = make_preview(raw, cfg, iid)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"图片处理失败: {exc}"}

    hashes = recent_phashes(con)
    novelty = min((hamming(ph, x) for x in hashes), default=99)
    if novelty <= 6:
        return {"ok": False, "error": "与图库里已有图片重复"}

    suggested = guess_upload_category(img, filename)
    title = Path(filename).stem or "上传图片"
    uploads = cfg["_meta"] / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    suffix = Path(filename).suffix.lower()
    if suffix not in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"):
        suffix = ".jpg"
    local_path = uploads / f"{iid}{suffix}"
    local_path.write_bytes(raw)
    it = {"source": "upload", "source_id": digest, "artist": "", "title": title,
          "page_url": "", "image_url": str(local_path), "referer": "",
          "width": w, "height": h, "engagement": 0, "tags": suggested,
          "published_at": None}
    note = "用户上传" + (f"；{issues}" if issues else "")
    con.execute(
        """INSERT INTO items (id,source,source_id,artist,title,page_url,image_url,referer,
           width,height,engagement,score,phash,thumb,status,tags,note,published_at,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?)""",
        (iid, "upload", digest, "", title, "", str(local_path), "", w, h, 0,
         score_item(it, 0, novelty), ph, thumb, suggested, note, None, iso(now_utc())))
    con.commit()
    return {"ok": True, "id": iid, "suggested": suggested,
            "width": w, "height": h, "note": note}


def cmd_ingest(args) -> None:
    """把手动存进 00_inbox 的图（比如 Pinterest 存的图）纳入待确认清单。"""
    cfg = load_cfg()
    ensure_layout(cfg)
    con = db(cfg)
    inbox = cfg["_root"] / "00_inbox"
    exts = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
    hashes = recent_phashes(con)
    added = 0
    for f in sorted(p for p in inbox.rglob("*") if p.is_file() and p.suffix.lower() in exts):
        sid = str(f.relative_to(cfg["_root"]))
        iid = item_id("manual", sid)
        if con.execute("SELECT 1 FROM items WHERE id=?", (iid,)).fetchone():
            continue
        try:
            thumb, w, h, ph, _issues = make_preview(f.read_bytes(), cfg, iid)
        except Exception as exc:  # noqa: BLE001
            log(f"  跳过 {f.name}: {exc}")
            continue
        if min(w, h) < int(cfg["collect"]["min_side"]):
            continue
        novelty = min((hamming(ph, x) for x in hashes), default=99)
        if novelty <= 6:
            log(f"  跳过 {f.name}: 与库内已有图重复")
            continue
        it = {"source": "manual", "source_id": sid, "artist": "", "title": f.stem,
              "page_url": "", "image_url": str(f), "referer": "",
              "width": w, "height": h, "engagement": 0, "tags": "",
              "published_at": None}
        con.execute(
            """INSERT INTO items (id,source,source_id,artist,title,page_url,image_url,referer,
               width,height,engagement,score,phash,thumb,status,tags,note,published_at,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending','','',?,?)""",
            (iid, "manual", sid, "", f.stem, "", str(f), "", w, h, 0,
             score_item(it, 0, novelty), ph, thumb, None, iso(now_utc())))
        con.commit()
        hashes.append(ph)
        added += 1
    path, items = write_digest(cfg, con)
    log(f"从 00_inbox 收进 {added} 张，当前待确认 {len(items)} 条 -> {path}")


def cmd_scan(args) -> None:
    """把图库里已有的图登记进索引，只用于去重，不会移动或改动它们。"""
    from PIL import Image
    cfg = load_cfg()
    ensure_layout(cfg)
    con = db(cfg)
    exts = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
    added = 0
    for f in cfg["_root"].rglob("*"):
        if not f.is_file() or f.suffix.lower() not in exts:
            continue
        rel = f.relative_to(cfg["_root"])
        if rel.parts[0] in (cfg["paths"].get("meta", ".artradar"), "00_inbox"):
            continue
        sid = str(rel)
        if con.execute("SELECT 1 FROM items WHERE (source='local' AND source_id=?) OR filed_path=?",
                       (sid, str(f))).fetchone():
            continue
        try:
            im = Image.open(f)
            im.load()
            w, h = im.size
            ph = dhash(im)
            iid = item_id("local", sid)
            thumb = cfg["_meta"] / "thumbs" / f"{iid}.jpg"
            preview = im.convert("RGB")
            preview.thumbnail((720, 720), Image.Resampling.LANCZOS)
            preview.save(thumb, "JPEG", quality=85)
        except Exception:  # noqa: BLE001
            continue
        rel = f.relative_to(cfg["_root"])
        parent = rel.parent
        top = rel.parts[0] if rel.parts else ""
        if top == cfg["filing"]["artists_dir"]:
            tag = "画师"
        elif str(parent) == ".":
            tag = "未分类"
        else:
            tag = str(parent).replace("\\", "/")
        con.execute(
            """INSERT INTO items (id,source,source_id,artist,title,page_url,image_url,referer,
               width,height,engagement,score,phash,thumb,status,tags,note,filed_path,folder,
               published_at,created_at,decided_at)
               VALUES (?, 'local', ?, '', ?, '', ?, '', ?, ?, 0, 0, ?, ?, 'kept', ?, '', ?, ?, NULL, ?, ?)""",
            (iid, sid, f.stem, str(f), w, h, ph, str(thumb), tag, str(f), str(f.parent),
             iso(now_utc()), iso(now_utc())))
        added += 1
        if added % 25 == 0:
            con.commit()
            log(f"  已登记 {added} 张…")
    con.commit()
    log(f"图库扫描完成，新登记 {added} 张")


CATEGORY_HINTS = [
    ("光影", ("光", "光影", "light", "lighting", "逆光", "ライティング", "照明", "cinematic")),
    ("动态", ("动态", "pose", "ポーズ", "动作", "姿势", "dynamic", "action", "running", "dance", "战斗", "武术")),
    ("场景", ("场景", "背景", "environment", "background", "風景", "风景", "landscape", "scenery", "room", "interior", "建筑", "城市")),
    ("构图", ("构图", "構図", "composition", "layout")),
    ("人体", ("人体", "anatomy", "解剖", "肌肉", "構造", "body", "figure", "手部", "腿部")),
    ("服装", ("服装", "衣装", "costume", "outfit", "服饰", "穿搭", "fashion", "clothes", "dress")),
    ("cosplay", ("cosplay", "cos", "コスプレ")),
    ("角色参考", ("角色", "同人", "character", "fanart", "fan art", "oc", "设定", "原神", "崩坏", "初音", "明日方舟", "碧蓝")),
]


def _image_category(img) -> str:
    """轻量画面特征分类；识别不了就进收藏，不再默认塞角色参考。"""
    from PIL import Image

    small = img.convert("RGB").resize((48, 48), Image.Resampling.BILINEAR)
    px = list(small.getdata())
    n = max(1, len(px))
    skin = green = blue = 0
    lums: list[float] = []
    for r, g, b in px:
        lums.append((0.299 * r + 0.587 * g + 0.114 * b) / 255.0)
        if r > 95 and g > 40 and b > 20 and max(r, g, b) - min(r, g, b) > 15 \
                and abs(r - g) > 15 and r > g and r > b:
            skin += 1
        if g > r * 1.08 and g > b * 1.08:
            green += 1
        if b > r * 1.08 and b > g * 1.02:
            blue += 1
    w, h = img.size
    aspect = w / max(1, h)
    lum = sum(lums) / n
    contrast = max(lums) - min(lums)
    if (green + blue) / n > 0.55 and skin / n < 0.12:
        return "场景"
    if aspect > 2.0 or aspect < 0.45:
        return "场景"
    if skin / n > 0.24 and 0.6 < aspect < 1.5:
        return "人体"
    if lum < 0.32 and contrast > 0.30:
        return "光影"
    if skin / n > 0.12:
        return "人体"
    if contrast > 0.48:
        return "光影"
    return "收藏"


def guess_category(it: dict, use_image: bool = False) -> str:
    artist = str(it.get("artist") or "").strip()
    if artist:
        return "画师"
    blob = " ".join(str(it.get(k) or "") for k in ("tags", "title")).lower()
    for cat, words in CATEGORY_HINTS:
        if any(w.lower() in blob for w in words):
            return cat
    if use_image:
        path = str(it.get("thumb") or it.get("filed_path") or "")
        if path:
            try:
                from PIL import Image
                with Image.open(path) as img:
                    return _image_category(img)
            except Exception:  # noqa: BLE001
                pass
    return "收藏"


def guess_upload_category(img, filename: str) -> str:
    """文件名优先；没有线索时看画面特征，识别不了进收藏。"""
    from PIL import Image

    name = (filename or "").lower()
    for cat, words in CATEGORY_HINTS:
        if any(w.lower() in name for w in words):
            return cat
    try:
        return _image_category(img)
    except Exception:  # noqa: BLE001
        return "收藏"


def cmd_pick(args) -> None:
    """从待确认里挑一组覆盖各分类的「今日精选」，写入 today.json。"""
    cfg = load_cfg()
    con = db(cfg)
    rows = pending_items(cfg, con, limit=200, use_snapshot=False)
    cats = ["光影", "动态", "场景", "构图", "人体", "服装", "cosplay", "角色参考", "画师", "收藏"]
    picked: list[dict] = []
    used: set[str] = set()

    # 先保证每个来源至少有一条，避免整屏都是 Pinterest 或 Pixiv
    source_order: list[str] = []
    for it in rows:
        if it["source"] not in source_order:
            source_order.append(it["source"])
    for src in source_order[:5]:
        for it in rows:
            if it["source"] == src and it["id"] not in used:
                picked.append(it)
                used.add(it["id"])
                break

    buckets: dict[str, list[dict]] = {c: [] for c in cats}
    for it in rows:
        if it["id"] in used:
            continue
        buckets.setdefault(guess_category(it), []).append(it)
    while len(picked) < args.n:
        added = False
        for cat in cats:
            if buckets.get(cat) and len(picked) < args.n:
                picked.append(buckets[cat].pop(0))
                added = True
        if not added:
            break
    picked = picked[: args.n]
    for i, it in enumerate(picked, 1):
        it["index"] = i
        it["suggested"] = guess_category(it)
    path = cfg["_meta"] / "today.json"
    atomic_write(path, json.dumps({"generated_at": iso(now_utc()), "items": picked},
                                  ensure_ascii=False, indent=2))
    log(f"今日精选 {len(picked)} 条 -> {path}")
    for it in picked:
        log(f'{it["index"]:>2}. {it["id"]}  {it["suggested"]:<4}  '
            f'{it["source"]:<12} {it["artist"] or "未知":<14} {it["title"][:28]}')


def load_today(cfg: dict) -> list[dict]:
    try:
        return json.loads((cfg["_meta"] / "today.json").read_text(encoding="utf-8")).get("items", [])
    except (OSError, ValueError):
        return []


LOW_RES_NOTE = "原图下载失败，存的是预览图"


def file_item(cfg: dict, con: sqlite3.Connection, iid: str, tags: list[str] | None = None,
              note: str = "", artist: bool = False, folder: str | None = None) -> Path | None:
    """把一条候选归档；返回目标文件路径。CLI 和网页 UI 共用。"""
    row = con.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
    if not row:
        return None
    artist_name = (row["artist"] or "").strip()
    folder_artist = cfg["filing"].get("artist_aliases", {}).get(artist_name, artist_name)
    if row["status"] == "kept" and row["filed_path"]:
        if artist and artist_name:
            return move_item(cfg, con, iid,
                             f'{cfg["filing"]["artists_dir"]}/{safe_name(folder_artist, 40)}',
                             keep_tags=True)
        return Path(row["filed_path"])
    explicit = bool(tags)
    # 没指定分类时用人工选的（可多个）；文件只放第一个分类的文件夹，其余分类记在索引和边车里
    cur_tags = list(dict.fromkeys(tags or split_tags(row["tags_manual"])))
    for t in cur_tags:
        check_tag(t)
    if not folder and cur_tags and "/" in cur_tags[0]:
        folder = cur_tags[0]
        cur_tags[0] = cur_tags[0].rsplit("/", 1)[-1]
    is_artist = bool(artist_name) and (artist or artist_name in cfg["filing"].get("favorite_artists", []))
    # 没有人工指定分类时，有画师名就默认进画师文件夹
    if artist_name and not explicit and not cur_tags:
        is_artist = True
    # 有画师信息的图不能留在「角色参考」：直接进画师文件夹并标画师
    if artist_name and not is_artist and "角色参考" in cur_tags:
        is_artist = True
    if is_artist:
        cur_tags = ["画师"]
    if not cur_tags:
        cur_tags = [guess_category(dict(row), use_image=True)]
    if folder:
        dest_dir = cfg["_root"] / folder
    elif is_artist and artist_name:
        dest_dir = cfg["_root"] / cfg["filing"]["artists_dir"] / safe_name(folder_artist, 40)
    elif cur_tags:
        dest_dir = cfg["_root"] / cfg["filing"]["reference_dir"] / safe_name(cur_tags[0], 40)
    else:
        dest_dir = cfg["_root"] / cfg["filing"]["favorites_dir"]
    # 取消收藏过的图：文件还在，不重新下载；没改分类就放回原文件夹
    stashed = Path(row["filed_path"]) if row["filed_path"] else None
    if stashed and not stashed.exists():
        stashed = None
    if stashed and not explicit and not artist and row["folder"]:
        dest_dir = Path(row["folder"])
    dest_dir.mkdir(parents=True, exist_ok=True)

    data = None
    low_res = False
    if stashed:
        ext = stashed.suffix
    else:
        try:
            data, ext = fetch_full_image(row, cfg)
        except Exception as exc:  # noqa: BLE001
            log(f"  {iid} 原图下载失败，使用预览图: {exc}")
            data, ext = Path(row["thumb"]).read_bytes(), ".jpg"
            low_res = True
            note = note or LOW_RES_NOTE

    stem = safe_name(f'{datetime.now():%Y%m%d}_{artist_name or "unknown"}_{row["title"] or ""}_{iid}', 110)
    dest = dest_dir / f"{stem}{ext}"
    n = 2
    while dest.exists():
        dest = dest_dir / f"{stem}_{n}{ext}"
        n += 1
    if stashed:
        side = stashed.with_suffix(".json")
        if side.exists() and _is_our_sidecar(side):
            side.unlink(missing_ok=True)
        shutil.move(str(stashed), str(dest))
    else:
        dest.write_bytes(data)

    sidecar = {
        "id": iid, "source": row["source"], "source_id": row["source_id"],
        "artist": artist_name, "title": row["title"], "page_url": row["page_url"],
        "image_url": row["image_url"], "tags": cur_tags, "note": note,
        "score": row["score"], "width": row["width"], "height": row["height"],
        "published_at": row["published_at"], "collected_at": row["created_at"],
        "filed_at": iso(now_utc()), "file": dest.name, "low_res": low_res,
    }
    atomic_write(dest.with_suffix(".json"), json.dumps(sidecar, ensure_ascii=False, indent=2))
    con.execute("UPDATE items SET status='kept', tags=?, tags_manual=?, note=?, folder=?, filed_path=?, decided_at=? WHERE id=?",
                (",".join(cur_tags), ",".join(cur_tags), note, str(dest_dir), str(dest), iso(now_utc()), iid))
    con.commit()
    if row["source"] == "upload":
        try:
            Path(row["image_url"]).unlink(missing_ok=True)
        except OSError:
            pass
    return dest


def _is_our_sidecar(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return isinstance(data, dict) and "id" in data and "source" in data
    except (OSError, ValueError):
        return False


def split_tags(value: str | None) -> list[str]:
    return [t.strip() for t in (value or "").split(",") if t.strip()]


def check_tag(tag: str) -> None:
    """分类名会变成文件夹路径，也用逗号拼接存储，这里挡掉越出图库和拼接冲突的写法。"""
    if not tag or "," in tag or ":" in tag or tag.startswith(("/", "\\")) \
            or ".." in re.split(r"[\\/]", tag):
        raise ValueError(f"分类名不合法：{tag}")


def _sync_sidecar(path: Path, tags: list[str]) -> None:
    side = path.with_suffix(".json")
    if not side.exists() or not _is_our_sidecar(side):
        return
    data = json.loads(side.read_text(encoding="utf-8"))
    data.update(tags=tags, file=path.name)
    atomic_write(side, json.dumps(data, ensure_ascii=False, indent=2))


def migrate_artist(cfg: dict, con: sqlite3.Connection, name: str) -> int:
    """把这位画师已收藏的图移进画师文件夹，并把分类改成「画师」。"""
    alias = cfg["filing"].get("artist_aliases", {}).get(name, name)
    target = f'{cfg["filing"]["artists_dir"]}/{safe_name(alias, 40)}'
    dest_dir = cfg["_root"] / target
    moved = 0
    rows = con.execute("SELECT id, filed_path FROM items WHERE artist=? AND status='kept' AND filed_path IS NOT NULL",
                       (name,)).fetchall()
    for row in rows:
        path = Path(row["filed_path"])
        if not path.exists():
            continue
        new_path = path
        try:
            if path.parent != dest_dir:
                new_path = move_item(cfg, con, row["id"], target, keep_tags=True)
                moved += 1
            con.execute("UPDATE items SET tags='画师', tags_manual='画师' WHERE id=?",
                        (row["id"],))
            _sync_sidecar(new_path, ["画师"])
        except (OSError, ValueError) as exc:
            log(f"  {row['id']} 移动失败: {exc}")
    con.commit()
    return moved


def retag_kept(cfg: dict, con: sqlite3.Connection, iid: str, tags: list[str]) -> None:
    """改已收藏图的分类。文件已在其中一个分类文件夹（或画师文件夹）里就不动，否则移到第一个分类。"""
    row = con.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
    path = Path(row["filed_path"])
    try:
        rel = path.parent.relative_to(cfg["_root"]).as_posix()
    except ValueError:
        rel = ""
    in_artists = rel.split("/")[0] == cfg["filing"]["artists_dir"]
    if tags and not in_artists and rel not in tags:
        first = tags[0] if "/" in tags[0] else safe_name(tags[0], 40)
        path = move_item(cfg, con, iid, first, keep_tags=True)
    joined = ",".join(tags)
    con.execute("UPDATE items SET tags=?, tags_manual=? WHERE id=?", (joined, joined, iid))
    con.commit()
    _sync_sidecar(path, tags)


def unfile_item(cfg: dict, con: sqlite3.Connection, iid: str, status: str = "pending", note: str = "") -> None:
    """把已归档的图移出分类文件夹，暂存到 .artradar/unfiled，再次收藏时直接放回，不用重新下载。"""
    row = con.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
    if not row or not row["filed_path"]:
        return
    old = Path(row["filed_path"])
    stash_dir = cfg["_meta"] / "unfiled"
    if old.exists() and old.parent != stash_dir:
        stash_dir.mkdir(parents=True, exist_ok=True)
        dest = stash_dir / f"{iid}{old.suffix}"
        side = old.with_suffix(".json")
        if side.exists() and _is_our_sidecar(side):
            side.unlink(missing_ok=True)
        shutil.move(str(old), str(dest))
        con.execute("UPDATE items SET filed_path=? WHERE id=?", (str(dest), iid))
    con.execute("UPDATE items SET status=?, note=?, decided_at=? WHERE id=?",
                (status, note, iso(now_utc()), iid))
    con.commit()


def move_item(cfg: dict, con: sqlite3.Connection, iid: str, rel_folder: str, keep_tags: bool = False) -> Path:
    check_tag(rel_folder)
    row = con.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
    if not row or not row["filed_path"]:
        raise ValueError("这条还没有归档")
    old = Path(row["filed_path"])
    dest_dir = cfg["_root"] / rel_folder
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / old.name
    n = 2
    while dest.exists() and dest != old:
        dest = dest_dir / f"{old.stem}_{n}{old.suffix}"
        n += 1
    if old.exists() and dest != old:
        shutil.move(str(old), str(dest))
        side = old.with_suffix(".json")
        if side.exists() and _is_our_sidecar(side):
            shutil.move(str(side), str(dest.with_suffix(".json")))
    if keep_tags:
        con.execute("UPDATE items SET folder=?, filed_path=? WHERE id=?", (str(dest_dir), str(dest), iid))
    else:
        tag = "画师" if rel_folder.startswith(cfg["filing"]["artists_dir"] + "/") else rel_folder
        con.execute("UPDATE items SET folder=?, filed_path=?, tags=?, tags_manual=? WHERE id=?",
                    (str(dest_dir), str(dest), tag, tag, iid))
    con.commit()
    return dest


def delete_item(cfg: dict, con: sqlite3.Connection, iid: str) -> None:
    row = con.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
    if not row:
        return
    for key in ("filed_path", "thumb"):
        p = row[key]
        if p and Path(p).exists():
            target = Path(p)
            target.unlink(missing_ok=True)
            side = target.with_suffix(".json")
            if side.exists() and _is_our_sidecar(side):
                side.unlink(missing_ok=True)
    con.execute("UPDATE items SET status='deleted', filed_path=NULL, decided_at=? WHERE id=?",
                (iso(now_utc()), iid))
    con.commit()


def cmd_accept(args) -> None:
    cfg = load_cfg()
    con = db(cfg)
    ids = [resolve_ref(con, cfg, x) for x in args.ids]
    tags = list(dict.fromkeys(args.tag or []))
    filed = 0
    for iid in ids:
        try:
            dest = file_item(cfg, con, iid, tags=tags, note=args.note or "",
                             artist=args.artist, folder=args.folder)
        except Exception as exc:  # noqa: BLE001
            log(f"{iid} 归档失败: {exc}")
            continue
        if dest:
            filed += 1
            log(f"已归档 {iid} -> {dest}")
    log(f"完成，归档 {filed} 张。")


def cmd_reject(args) -> None:
    cfg = load_cfg()
    con = db(cfg)
    for ref in args.ids:
        iid = resolve_ref(con, cfg, ref)
        con.execute("UPDATE items SET status='rejected', note=?, decided_at=? WHERE id=?",
                    (args.reason or "", iso(now_utc()), iid))
        log(f"已拒绝 {iid}")
    con.commit()


# --------------------------------------------------------------------------- #
# 统计 / 初始化 / 演示
# --------------------------------------------------------------------------- #
def cmd_stats(args) -> None:
    cfg = load_cfg()
    con = db(cfg)
    rows = con.execute("SELECT status, COUNT(*) n FROM items GROUP BY status").fetchall()
    log("状态统计：")
    for r in rows:
        log(f"  {r['status']:<9} {r['n']}")
    by_src = con.execute("SELECT source, COUNT(*) n FROM items WHERE status='kept' GROUP BY source").fetchall()
    if by_src:
        log("已归档来源：" + "，".join(f"{r['source']} {r['n']}" for r in by_src))
    total = 0
    for r in con.execute("SELECT filed_path FROM items WHERE status='kept' AND filed_path IS NOT NULL"):
        p = Path(r["filed_path"])
        if p.exists():
            total += p.stat().st_size
    log(f"图库占用：{total / 1024 / 1024:.1f} MB")


def cmd_init(args) -> None:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(DEFAULT_CONFIG, encoding="utf-8")
        log(f"已生成 {CONFIG_PATH}")
    cfg = load_cfg()
    ensure_layout(cfg)
    log(f"图库目录：{cfg['_root']}")
    for p in sorted(cfg["_root"].iterdir()):
        log(f"  {p.name}/")
    log("下一步：编辑 config.toml 里的来源和代理，然后 python artradar.py collect")


def cmd_demo(args) -> None:
    """离线生成 8 条假数据，用来验证画廊、编号、归档全链路（不联网）。"""
    from PIL import Image, ImageDraw

    cfg = load_cfg()
    ensure_layout(cfg)
    con = db(cfg)
    demo_dir = cfg["_meta"] / "demo"
    demo_dir.mkdir(parents=True, exist_ok=True)
    artists = ["Aki", "Mori", "Nan", "Sora", "Yuu", "Rin", "Kai", "Hana"]
    titles = ["Dusk Study", "Backlight", "Old Market", "Rainy Roof", "Portrait 04",
              "Glass & Light", "Forest Path", "Silent Room"]
    for i in range(8):
        sid = f"demo-{i}"
        iid = item_id("demo", sid)
        if con.execute("SELECT 1 FROM items WHERE id=?", (iid,)).fetchone():
            continue
        w, h = 900, 1200
        base = Image.new("RGB", (90, 120))
        px = base.load()
        c1 = ((40 + i * 23) % 200 + 30, (90 + i * 17) % 160 + 40, (150 + i * 11) % 120 + 60)
        c2 = (max(10, c1[0] - 60), max(10, c1[1] - 40), max(10, c1[2] - 50))
        for y in range(120):
            for x in range(90):
                t = (x / 89 + y / 119) / 2
                px[x, y] = tuple(int(c1[k] * t + c2[k] * (1 - t)) for k in range(3))
        img = base.resize((w, h), Image.Resampling.BICUBIC)
        d = ImageDraw.Draw(img)
        for k in range(3):
            x0 = 80 + k * 210 + i * 7
            d.ellipse([x0, 260 + k * 90, x0 + 300, 560 + k * 90], outline=(255, 255, 255), width=3)
        d.rectangle([40, 40, w - 40, h - 40], outline=(255, 255, 255), width=2)
        d.text((60, h - 90), f"{artists[i]} / {titles[i]}", fill=(255, 255, 255))
        full = demo_dir / f"{sid}.jpg"
        img.save(full, "JPEG", quality=88)
        thumb = cfg["_meta"] / "thumbs" / f"{iid}.jpg"
        thumb_img = img.copy()
        thumb_img.thumbnail((cfg["collect"]["thumb_px"], cfg["collect"]["thumb_px"]))
        thumb_img.save(thumb, "JPEG", quality=86)
        con.execute(
            """INSERT INTO items (id,source,source_id,artist,title,page_url,image_url,referer,
               width,height,engagement,score,phash,thumb,status,tags,note,published_at,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending','','',?,?)""",
            (iid, "demo", sid, artists[i], titles[i], "https://www.pixiv.net/", str(full),
             "https://www.pixiv.net/", w, h, 4000 - i * 380, round(0.95 - i * 0.055, 3),
             dhash(img), str(thumb), iso(now_utc() - timedelta(days=i)), iso(now_utc())))
        con.commit()
    path, items = write_digest(cfg, con)
    log(f"演示数据已生成，{len(items)} 条待确认 -> {path}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(description="art-radar 绘画参考采集与归档")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="初始化目录与配置").set_defaults(func=cmd_init)

    p = sub.add_parser("login", help="打开浏览器登录 Pixiv / 微博 / Pinterest 并保存登录态")
    p.add_argument("--pinterest", action="store_true", help="只打开 Pinterest 登录窗口")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("collect", help="抓取候选并生成清单")
    p.add_argument("--source", help="只跑某个来源（名字或类型）")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("more", help="再来一组（可带关键词）")
    p.add_argument("keywords", nargs="*", help="搜索关键词；留空则默认模式")
    p.set_defaults(func=cmd_more)

    p = sub.add_parser("demo", help="生成离线演示数据")
    p.set_defaults(func=cmd_demo)

    sub.add_parser("ingest", help="把 00_inbox 里的图纳入待确认").set_defaults(func=cmd_ingest)

    sub.add_parser("scan", help="登记图库已有图片用于去重（不改动文件）").set_defaults(func=cmd_scan)

    p = sub.add_parser("digest", help="重新生成 HTML 画廊")
    p.add_argument("--open", action="store_true", help="生成后用默认浏览器打开")
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser("pending", help="列出待确认候选")
    p.add_argument("--json", action="store_true", help="输出 JSON，供助手解析")
    p.add_argument("--top", type=int, help="只看前 N 条")
    p.set_defaults(func=cmd_pending)

    p = sub.add_parser("pick", help="生成覆盖各分类的今日精选")
    p.add_argument("--n", type=int, default=12)
    p.set_defaults(func=cmd_pick)

    p = sub.add_parser("accept", help="确认归档")
    p.add_argument("ids", nargs="+", help="编号（清单里的 1、2、3）或 8 位 id")
    p.add_argument("--tag", action="append", help="分类标签，可重复，如 --tag 构图 --tag 光影")
    p.add_argument("--note", help="备注")
    p.add_argument("--artist", action="store_true", help="强制归入画师目录")
    p.add_argument("--folder", help="指定相对图库的子目录，优先级最高")
    p.set_defaults(func=cmd_accept)

    p = sub.add_parser("reject", help="拒绝候选")
    p.add_argument("ids", nargs="+")
    p.add_argument("--reason", default="")
    p.set_defaults(func=cmd_reject)

    sub.add_parser("stats", help="统计").set_defaults(func=cmd_stats)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
