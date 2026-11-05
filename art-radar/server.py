#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""art-radar 本地管理界面：点击收藏 / 分类 / 删除 / 移动。

    python server.py [--port 8765] [--open]

默认监听 0.0.0.0，手机才能在局域网连上；本机（Host 为 localhost/127.0.0.1）免令牌，
其它来源必须带配对令牌，图片也一样。
"""
from __future__ import annotations

import argparse
import html
import json
import mimetypes
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import artradar as core  # noqa: E402

CFG = core.load_cfg()
CFG["_meta"].mkdir(parents=True, exist_ok=True)
TOKEN_FILE = CFG["_meta"] / "mobile_token.txt"


def _load_token() -> str:
    try:
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    if not token:
        token = secrets.token_urlsafe(18)
        TOKEN_FILE.write_text(token, encoding="utf-8")
    return token


TOKEN = _load_token()


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:  # noqa: BLE001
        return "127.0.0.1"
    finally:
        s.close()


PORT = 8765
LOCK = threading.Lock()
MORE_LOCK = threading.Lock()
MORE_CANCEL = threading.Event()
MORE_JOB = {"state": "idle", "added": 0, "filtered": 0, "keywords": [], "error": "", "mode": "auto",
            "limit": 10, "platform": "all"}
ACCEPT_LOCK = threading.Lock()
ACCEPT_JOB = {"state": "idle", "total": 0, "done": 0, "added": 0, "low_res": 0, "error": ""}


def connect() -> sqlite3.Connection:
    return core.db(CFG)


FOLLOW_FILE = CFG["_meta"] / "following.json"
FOLLOW_LOCK = threading.Lock()


def load_following() -> dict:
    try:
        data = json.loads(FOLLOW_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("artists"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"artists": {}}


def save_following(data: dict) -> None:
    core.atomic_write(FOLLOW_FILE, json.dumps(data, ensure_ascii=False, indent=2))


def following_stats(con: sqlite3.Connection) -> list[dict]:
    data = load_following()
    out = []
    for name, meta in data.get("artists", {}).items():
        row = con.execute(
            "SELECT COUNT(*) n FROM items WHERE artist=? AND status='pending'", (name,)).fetchone()
        pending = int(row["n"] if row else 0)
        row = con.execute(
            "SELECT COUNT(*) n FROM items WHERE artist=? AND status='kept'", (name,)).fetchone()
        kept = int(row["n"] if row else 0)
        latest = con.execute(
            "SELECT id, thumb FROM items WHERE artist=? AND status IN ('pending','kept') "
            "ORDER BY created_at DESC LIMIT 1", (name,)).fetchone()
        out.append({
            "artist": name,
            "pending": pending,
            "kept": kept,
            "has_new": pending > 0,
            "avatar": f'/img/thumb/{latest["id"]}' if latest else "",
            "followed_at": (meta or {}).get("followed_at", ""),
        })
    return out


def categories() -> list[str]:
    root = CFG["_root"]
    out = list(CFG["filing"]["tags"])
    out.append(CFG["filing"]["favorites_dir"])
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        if d.name in ("00_inbox", CFG["filing"]["artists_dir"], CFG["filing"]["favorites_dir"]):
            continue
        if d.name not in out:
            out.append(d.name)
    for top in sorted(root.iterdir()):
        if not top.is_dir() or top.name in (".artradar", "00_inbox"):
            continue
        for d in sorted(top.iterdir()):
            if d.is_dir():
                name = f"{top.name}/{d.name}"
                if name not in out:
                    out.append(name)
    artists_root = root / CFG["filing"]["artists_dir"]
    if artists_root.exists():
        for d in sorted(artists_root.iterdir()):
            if d.is_dir():
                name = f'{CFG["filing"]["artists_dir"]}/{d.name}'
                if name not in out:
                    out.append(name)
    return out


_TOTAL_CACHE = {"at": 0.0, "n": 0}


def library_total() -> int:
    # 只有旧页面还用这个数字。全盘遍历很慢，放后台算、缓存一分钟，不能拖慢 /api/state（启动器探活只等 1.5 秒）
    def count():
        exts = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
        _TOTAL_CACHE["n"] = sum(1 for p in CFG["_root"].rglob("*")
                                if p.suffix.lower() in exts and p.is_file()
                                and ".artradar" not in p.parts)

    if time.time() - _TOTAL_CACHE["at"] > 60:
        _TOTAL_CACHE["at"] = time.time()
        threading.Thread(target=count, daemon=True).start()
    return _TOTAL_CACHE["n"]


def reveal(target: Path) -> None:
    if sys.platform != "win32":
        raise RuntimeError("只支持在 Windows 上打开文件夹")
    arg = f"/select,{target}" if target.is_file() else str(target)
    subprocess.Popen(["explorer", arg])  # noqa: S603,S607


def migrate_followed() -> None:
    # 补上更新前就关注的画师，以及 CLI、定时任务归档的图
    con = connect()
    try:
        for name in load_following().get("artists", {}):
            moved = core.migrate_artist(CFG, con, name)
            if moved:
                print(f"已把 {name} 的 {moved} 张图整理到画师文件夹", flush=True)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    finally:
        con.close()


def is_low_res(con: sqlite3.Connection, iid) -> bool:
    row = con.execute("SELECT note FROM items WHERE id=?", (str(iid),)).fetchone()
    return bool(row and row["note"] == core.LOW_RES_NOTE)


def use_artist_folder(row, data: dict) -> bool:
    name = (row["artist"] or "").strip() if row else ""
    if not name:
        return False
    return bool(data.get("artist") or data.get("smart")
                or (data.get("by_follow") and name in load_following().get("artists", {})))


def item_json(row: sqlite3.Row) -> dict:
    d = dict(row)
    if d.get("source") == "pinterest":
        d["title"] = core.clean_pin_title(d.get("title") or "")
    d["suggested"] = core.guess_category(d, use_image=(d.get("status") == "pending"))
    d["manual_tags"] = [t for t in (d.get("tags_manual") or "").split(",") if t.strip()]
    if not d["manual_tags"] and d.get("status") == "kept":
        d["manual_tags"] = [t for t in (d.get("tags") or "").split(",") if t.strip()]
    d["thumb_url"] = f'/img/thumb/{d["id"]}'
    d["full_url"] = f'/img/full/{d["id"]}'
    if d.get("filed_path"):
        try:
            d["folder_rel"] = str(Path(d["filed_path"]).parent.relative_to(CFG["_root"])).replace("\\", "/")
        except ValueError:
            d["folder_rel"] = str(Path(d["filed_path"]).parent).replace("\\", "/")
    if not (d.get("artist") or "").strip() and d.get("folder_rel"):
        parts = d["folder_rel"].split("/")
        if (len(parts) == 2 and parts[0] == CFG["filing"]["artists_dir"]
                and parts[1] not in {"其他", "未分类", "未知", "未知名", "unknown", "Unknown"}):
            d["artist"] = parts[1]
    return d


def list_items(status: str, q: str = "") -> list[dict]:
    con = connect()
    order = "decided_at DESC" if status != "pending" else "score DESC, created_at DESC"
    rows = con.execute(f"SELECT * FROM items WHERE status=? ORDER BY {order}",
                       (status,)).fetchall()
    con.close()
    items = [item_json(r) for r in rows]
    if q:
        needle = q.lower()
        items = [x for x in items if needle in json.dumps(x, ensure_ascii=False).lower()]
    return items


class Handler(BaseHTTPRequestHandler):
    server_version = "art-radar/1.0"

    def log_message(self, fmt, *args):  # noqa: ANN001
        return

    # ---------- helpers ----------
    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control",
                         "no-store" if (ctype.startswith("application/json")
                                        or ctype.startswith("text/html")) else "max-age=60")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _host_name(self) -> str:
        host = self.headers.get("Host") or ""
        return urllib.parse.urlsplit("//" + host).hostname or ""

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-Art-Token") or ""
        if not supplied:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            supplied = (qs.get("token") or [""])[0]
        if secrets.compare_digest(supplied.encode("utf-8"), TOKEN.encode("utf-8")):
            return True
        # 本机免令牌，但要看 Host：挡住 DNS 重绑定把恶意网站指到 127.0.0.1
        return self._is_local()

    def _is_local(self) -> bool:
        return (self.client_address[0] in ("127.0.0.1", "::1")
                and self._host_name() in ("127.0.0.1", "localhost", "::1"))

    def _same_origin(self) -> bool:
        # 挡住别的网页往本地服务发 POST（比如偷偷调 /api/delete）
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return origin != "null" and urllib.parse.urlsplit(origin).hostname == self._host_name()

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _handle_upload(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                self._json({"ok": False, "error": "没有收到文件"}, 400)
                return
            if length > 50 * 1024 * 1024:
                self._json({"ok": False, "error": "图片超过 50MB"}, 413)
                return
            raw = self.rfile.read(length)
            name = urllib.parse.unquote(self.headers.get("X-Filename") or "upload.jpg")
            con = connect()
            result = core.ingest_upload(CFG, con, raw, name)
            if result.get("ok"):
                row = con.execute("SELECT * FROM items WHERE id=?", (result["id"],)).fetchone()
                result["item"] = item_json(row)
            con.close()
            self._json(result, 200 if result.get("ok") else 400)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._json({"ok": False, "error": str(exc)}, 500)

    def _image(self, path: Path):
        if not path.exists():
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self._send(200, path.read_bytes(), ctype)

    # ---------- routes ----------
    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if not self._authorized():
            self._send(401, b"ArtRadar: unauthorized (need token)", "text/plain; charset=utf-8")
            return
        try:
            if path == "/":
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
                return
            if path == "/api/pair":
                self._json({"url": f"http://{lan_ip()}:{PORT}/?token={TOKEN}",
                            "token": TOKEN, "ip": lan_ip(), "port": PORT})
                return
            if path == "/api/state":
                con = connect()
                counts = {r["status"]: r["n"] for r in
                          con.execute("SELECT status, COUNT(*) n FROM items GROUP BY status")}
                following = following_stats(con)
                con.close()
                self._json({"counts": counts, "categories": categories(),
                            "library_total": library_total(), "following": following,
                            "artists_dir_name": CFG["filing"]["artists_dir"],
                            "local": self._is_local(),
                            "root": str(CFG["_root"]),
                            "artists_dir": str(CFG["_root"] / CFG["filing"]["artists_dir"])})
                return
            if path == "/api/items":
                qs = urllib.parse.parse_qs(parsed.query)
                status = (qs.get("status") or ["pending"])[0]
                if status not in ("pending", "kept", "rejected", "deleted"):
                    status = "pending"
                self._json({"items": list_items(status, (qs.get("q") or [""])[0])})
                return
            if path == "/api/more/status":
                self._json(MORE_JOB)
                return
            if path == "/api/accept/status":
                self._json(ACCEPT_JOB)
                return
            if path.startswith("/img/thumb/"):
                iid = path.rsplit("/", 1)[-1].split(".")[0]
                thumb = CFG["_meta"] / "thumbs" / f"{iid}.jpg"
                if not thumb.exists():
                    con = connect()
                    row = con.execute("SELECT thumb, filed_path FROM items WHERE id=?", (iid,)).fetchone()
                    con.close()
                    if row and (row["thumb"] or row["filed_path"]):
                        thumb = Path(row["thumb"] or row["filed_path"])
                self._image(thumb)
                return
            if path.startswith("/img/full/"):
                iid = path.rsplit("/", 1)[-1].split(".")[0]
                con = connect()
                row = con.execute("SELECT filed_path, thumb FROM items WHERE id=?", (iid,)).fetchone()
                con.close()
                target = Path(row["filed_path"]) if row and row["filed_path"] else (
                    Path(row["thumb"]) if row and row["thumb"] else CFG["_meta"] / "thumbs" / f"{iid}.jpg")
                self._image(target)
                return
            self._send(404, b"not found", "text/plain; charset=utf-8")
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self._json({"ok": False, "error": "server error"}, 500)

    def do_POST(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if not self._authorized() or not self._same_origin():
            self._json({"ok": False, "error": "unauthorized"}, 401)
            return
        if path == "/api/upload":
            self._handle_upload()
            return
        try:
            data = self._body()
        except Exception:  # noqa: BLE001
            self._json({"ok": False, "error": "bad json"}, 400)
            return
        try:
            con = connect()
            if path == "/api/more":
                raw = str(data.get("keywords") or "").strip()
                keywords = core.clean_keywords(raw)
                try:
                    limit = max(1, min(50, int(data.get("limit") or 10)))
                except (TypeError, ValueError):
                    limit = 10
                platform = str(data.get("platform") or "all")
                if platform not in ("all", "weibo", "pixiv", "pinterest"):
                    platform = "all"
                mode = platform if platform != "all" else core.detect_mode(raw)
                with MORE_LOCK:
                    if MORE_JOB["state"] in ("running", "cancelling"):
                        con.close()
                        self._json({"ok": False, "error": "正在抓取，稍等一下"}, 409)
                        return
                    MORE_CANCEL.clear()
                    MORE_JOB.update(state="running", added=0, filtered=0, keywords=keywords,
                                    error="", mode=mode, limit=limit, platform=platform)

                def run_more():
                    try:
                        c0 = connect()
                        before = c0.execute(
                            "SELECT COUNT(*) FROM items WHERE status='rejected'").fetchone()[0]
                        c0.close()
                        added = core.collect_more(CFG, keywords, raw, cancel=MORE_CANCEL,
                                                  platform=platform, max_results=limit)
                        c1 = connect()
                        after = c1.execute(
                            "SELECT COUNT(*) FROM items WHERE status='rejected'").fetchone()[0]
                        c1.close()
                        MORE_JOB.update(filtered=max(0, after - before))
                        if MORE_CANCEL.is_set():
                            MORE_JOB.update(state="cancelled", added=added)
                        else:
                            MORE_JOB.update(state="done", added=added)
                    except Exception as exc:  # noqa: BLE001
                        traceback.print_exc()
                        MORE_JOB.update(state="error", error=str(exc))

                threading.Thread(target=run_more, daemon=True).start()
                con.close()
                self._json({"ok": True, "state": "running", "keywords": keywords,
                            "mode": mode, "limit": limit, "platform": platform})
                return
            if path == "/api/more/cancel":
                with MORE_LOCK:
                    if MORE_JOB["state"] == "running":
                        MORE_CANCEL.set()
                        MORE_JOB.update(state="cancelling")
                con.close()
                self._json({"ok": True, "state": MORE_JOB["state"]})
                return
            if path == "/api/accept":
                ids = data.get("ids") or []
                category = (data.get("category") or "").strip() or None
                note = data.get("note") or ""
                if data.get("async"):
                    with ACCEPT_LOCK:
                        if ACCEPT_JOB["state"] == "running":
                            con.close()
                            self._json({"ok": False, "error": "上一批还在归档，稍等一下"}, 409)
                            return
                        ACCEPT_JOB.update(state="running", total=len(ids), done=0,
                                          added=0, low_res=0, error="")

                    def run_accept():
                        job_con = connect()
                        added = 0
                        try:
                            for iid in ids:
                                try:
                                    row = job_con.execute(
                                        "SELECT artist FROM items WHERE id=?", (str(iid),)).fetchone()
                                    use_artist = use_artist_folder(row, data)
                                    dest = core.file_item(
                                        CFG, job_con, str(iid),
                                        tags=[category] if category else None,
                                        note=note, artist=use_artist)
                                    if dest:
                                        added += 1
                                        if is_low_res(job_con, iid):
                                            ACCEPT_JOB["low_res"] += 1
                                except Exception:  # noqa: BLE001
                                    traceback.print_exc()
                                with ACCEPT_LOCK:
                                    ACCEPT_JOB["done"] += 1
                                    ACCEPT_JOB["added"] = added
                            with ACCEPT_LOCK:
                                ACCEPT_JOB.update(state="done", added=added)
                        except Exception as exc:  # noqa: BLE001
                            traceback.print_exc()
                            with ACCEPT_LOCK:
                                ACCEPT_JOB.update(state="error", error=str(exc))
                        finally:
                            job_con.close()

                    threading.Thread(target=run_accept, daemon=True).start()
                    con.close()
                    self._json({"ok": True, "state": "running", "total": len(ids)})
                    return
                results = []
                for iid in ids:
                    try:
                        row = con.execute("SELECT artist FROM items WHERE id=?", (str(iid),)).fetchone()
                        use_artist = use_artist_folder(row, data)
                        dest = core.file_item(CFG, con, str(iid),
                                              tags=[category] if category else None,
                                              note=note, artist=use_artist)
                        results.append({"id": iid, "ok": bool(dest), "path": str(dest) if dest else "",
                                        "low_res": is_low_res(con, iid)})
                    except Exception as exc:  # noqa: BLE001
                        results.append({"id": iid, "ok": False, "error": str(exc)})
                con.close()
                self._json({"ok": True, "results": results})
                return
            if path == "/api/reject":
                ids = data.get("ids") or []
                reason = data.get("reason") or ""
                for iid in ids:
                    row = con.execute("SELECT status FROM items WHERE id=?", (str(iid),)).fetchone()
                    if row and row["status"] == "kept":
                        core.unfile_item(CFG, con, str(iid), "rejected", reason)
                    else:
                        con.execute("UPDATE items SET status='rejected', note=?, decided_at=? WHERE id=?",
                                    (reason, core.iso(core.now_utc()), str(iid)))
                con.commit()
                con.close()
                self._json({"ok": True})
                return
            if path == "/api/follow":
                artist = str(data.get("artist") or "").strip()
                if not artist:
                    con.close()
                    self._json({"ok": False, "error": "缺少画师名"}, 400)
                    return
                with FOLLOW_LOCK:
                    fd = load_following()
                    artists = fd.setdefault("artists", {})
                    if data.get("follow"):
                        artists.setdefault(artist, {"followed_at": core.iso(core.now_utc())})
                    else:
                        artists.pop(artist, None)
                    save_following(fd)
                # 关注时把已收藏的图归到画师文件夹；取关不搬回去，App 里照样能看到
                moved = core.migrate_artist(CFG, con, artist) if data.get("follow") else 0
                stats = following_stats(con)
                con.close()
                self._json({"ok": True, "following": stats, "moved": moved})
                return
            if path == "/api/reveal":
                # 在电脑上弹资源管理器，只给本机页面用；手机连上来时没有意义
                if not self._is_local():
                    con.close()
                    self._json({"ok": False, "error": "只能在电脑上打开文件夹"}, 403)
                    return
                target = CFG["_root"]
                if data.get("id"):
                    row = con.execute("SELECT filed_path FROM items WHERE id=?", (str(data["id"]),)).fetchone()
                    target = Path(row["filed_path"]) if row and row["filed_path"] else None
                con.close()
                if not target or not target.exists():
                    self._json({"ok": False, "error": "文件不在了"}, 404)
                    return
                reveal(target)
                self._json({"ok": True})
                return
            if path == "/api/tag":
                ids = data.get("ids") or []
                tags = list(dict.fromkeys(str(t).strip() for t in (data.get("tags") or []) if str(t).strip()))
                for t in tags:
                    core.check_tag(t)
                add = bool(data.get("add", True))
                mode = str(data.get("mode") or ("add" if add else "remove"))
                out_items = []
                for iid in ids:
                    row = con.execute("SELECT * FROM items WHERE id=?", (str(iid),)).fetchone()
                    if not row:
                        continue
                    manual = core.split_tags(row["tags_manual"])
                    if mode == "replace":
                        manual = tags
                    elif mode == "remove":
                        manual = [t for t in manual if t not in tags]
                    else:
                        manual = list(dict.fromkeys(manual + tags))
                    if row["status"] == "kept" and row["filed_path"]:
                        core.retag_kept(CFG, con, str(iid), manual)
                    else:
                        # tags 列留给 AI 猜分类用；改过分类就不再放回取消收藏前的文件夹
                        con.execute("UPDATE items SET tags_manual=?, folder=NULL WHERE id=?",
                                    (",".join(manual), str(iid)))
                    out_items.append(item_json(con.execute(
                        "SELECT * FROM items WHERE id=?", (str(iid),)).fetchone()))
                con.commit()
                con.close()
                self._json({"ok": True, "items": out_items})
                return
            if path == "/api/restore":
                for iid in data.get("ids") or []:
                    row = con.execute("SELECT status FROM items WHERE id=?", (str(iid),)).fetchone()
                    if row and row["status"] == "kept":
                        core.unfile_item(CFG, con, str(iid))
                    else:
                        con.execute("UPDATE items SET status='pending', note='' WHERE id=?", (str(iid),))
                con.commit()
                con.close()
                self._json({"ok": True})
                return
            if path == "/api/move":
                folder = (data.get("folder") or "").strip()
                if not folder or ".." in Path(folder).parts:
                    self._json({"ok": False, "error": "bad folder"}, 400)
                    return
                ids = data.get("ids") or ([data.get("id")] if data.get("id") else [])
                paths = []
                for iid in ids:
                    paths.append(str(core.move_item(CFG, con, str(iid), folder)))
                con.close()
                self._json({"ok": True, "paths": paths})
                return
            if path == "/api/delete":
                ids = data.get("ids") or ([data.get("id")] if data.get("id") else [])
                for iid in ids:
                    core.delete_item(CFG, con, str(iid))
                con.close()
                self._json({"ok": True, "deleted": len(ids)})
                return
            con.close()
            self._json({"ok": False, "error": "unknown endpoint"}, 404)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._json({"ok": False, "error": str(exc)}, 500)


PAGE = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>art-radar 素材库</title>
<style>
:root{color-scheme:dark;--bg:#101014;--panel:#191920;--line:#2a2a35;--text:#ececf2;--dim:#9a9aab;
      --accent:#6b5cff;--ok:#39c07a;--danger:#e05a5a}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 "Segoe UI","Microsoft YaHei",sans-serif}
header{position:sticky;top:0;z-index:20;background:rgba(16,16,20,.92);backdrop-filter:blur(8px);
       border-bottom:1px solid var(--line);padding:14px 20px;display:flex;flex-wrap:wrap;gap:12px;align-items:center}
h1{font-size:17px;margin:0;font-weight:600}
.counts{color:var(--dim);font-size:12px}
.tabs{margin-left:auto;display:flex;gap:6px}
.tabs button{background:transparent;border:1px solid var(--line);color:var(--dim);padding:7px 14px;
             border-radius:8px;cursor:pointer}
.tabs button.on{background:var(--accent);border-color:var(--accent);color:#fff}
main{padding:18px 20px 80px}
.toolbar{display:flex;gap:10px;align-items:center;margin-bottom:16px}
.checkall{display:flex;gap:6px;align-items:center;color:var(--dim);font-size:12px;white-space:nowrap}
.selbox{position:absolute;top:8px;right:8px;z-index:3;width:18px;height:18px;accent-color:var(--accent);
        cursor:pointer;border-radius:4px;opacity:.92}
.batchbar{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);display:none;gap:8px;align-items:center;
          background:#1d1d27;border:1px solid var(--line);padding:10px 12px;border-radius:12px;z-index:30;
          box-shadow:0 10px 30px rgba(0,0,0,.45);flex-wrap:wrap;max-width:94vw}
.batchbar.show{display:flex}
.batchbar .n{font-weight:600;margin-right:4px}
.batchbar select{max-width:180px}
.morebar{display:flex;gap:10px;align-items:center;margin-bottom:14px;flex-wrap:wrap}
.morebar input{flex:1;min-width:240px;background:var(--panel);border:1px solid var(--line);
      color:var(--text);border-radius:8px;padding:9px 12px;outline:none}
.morebar input#limit{flex:none;width:64px;min-width:0;padding:7px 8px}
.morebar select{background:var(--panel);border:1px solid var(--line);color:var(--text);
      border-radius:8px;padding:8px 10px;outline:none}
.morebar .opt{display:flex;gap:6px;align-items:center;color:var(--dim);font-size:12px;white-space:nowrap}
.morebar .hint{color:var(--dim);font-size:12px}
input[type=search]{flex:1;max-width:340px;background:var(--panel);border:1px solid var(--line);
      color:var(--text);border-radius:8px;padding:9px 12px;outline:none}
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fill,minmax(230px,1fr))}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden;display:flex;
      flex-direction:column;min-width:0}
.card.selected{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent) inset}
.thumb{position:relative;background:#0b0b0e;cursor:pointer}
.thumb img{width:100%;aspect-ratio:4/5;object-fit:cover;display:block}
.zoom{position:absolute;right:8px;bottom:8px;z-index:3;width:28px;height:28px;padding:0;border-radius:8px;
      background:rgba(16,16,20,.72);border:1px solid rgba(255,255,255,.25);color:#fff;font-size:15px;
      line-height:1;cursor:zoom-in}
.badge{position:absolute;top:8px;left:8px;background:rgba(107,92,255,.92);color:#fff;font-size:11px;
       padding:3px 8px;border-radius:20px}
.body{padding:10px 11px 12px;display:flex;flex-direction:column;gap:4px;flex:1;min-width:0}
.artist{font-weight:600;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.title{color:var(--dim);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.src{color:#6f6f82;font-size:11px;text-transform:uppercase;letter-spacing:.04em}
.controls{display:flex;flex-wrap:wrap;gap:6px;margin-top:auto;padding-top:8px;min-width:0}
.controls .cat{flex:1 1 100%;width:100%;min-width:0;max-width:100%;box-sizing:border-box;text-overflow:ellipsis}
button,.select{background:#23232d;border:1px solid var(--line);color:var(--text);border-radius:7px;
      padding:6px 9px;font-size:12px;cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
button.ok{background:var(--ok);border-color:var(--ok);color:#082014}
button.ghost{background:transparent}
button.danger{color:#ff9c9c;border-color:#4a2b2b}
button:disabled{opacity:.5;cursor:default}
select{background:#23232d;color:var(--text);border:1px solid var(--line);border-radius:7px;padding:6px}
.path{color:#8d8d9f;font-size:11px;word-break:break-all}
.toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:#2b2b38;color:#fff;
       padding:10px 18px;border-radius:10px;opacity:0;transition:.2s;pointer-events:none;z-index:50}
.toast.show{opacity:1}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.86);display:none;align-items:center;justify-content:center;
       z-index:40;padding:20px}
.modal.show{display:flex}
.confirm-box{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:20px;
             max-width:440px;width:90%;box-shadow:0 20px 60px rgba(0,0,0,.5)}
.confirm-box p{margin:0 0 18px;line-height:1.6}
.confirm-actions{display:flex;justify-content:flex-end;gap:10px}
.modal img{max-width:92vw;max-height:88vh;object-fit:contain;border-radius:8px}
.modal .close{position:absolute;top:18px;right:24px;font-size:28px;background:none;border:0;color:#fff}
.empty{color:var(--dim);padding:40px 0;text-align:center;grid-column:1/-1}

/* ---- warm editorial redesign ---- */
:root{color-scheme:dark;--bg:#171311;--panel:#211b18;--panel2:#281f1b;--line:#3d2f28;
  --text:#f3ece3;--dim:#a99a8d;--accent:#e07a5f;--accent2:#f0a58d;--ok:#9bbf8f;--danger:#e39a8e}
*{box-sizing:border-box}
body{background:radial-gradient(900px 500px at 18% -8%,#33231d 0%,transparent 60%),var(--bg);
  color:var(--text);font-family:"Avenir Next","Segoe UI","Noto Sans SC","Microsoft YaHei",sans-serif}
header{background:linear-gradient(180deg,rgba(23,19,17,.98),rgba(23,19,17,.9));border-bottom:1px solid var(--line);
  padding:24px 30px 0;display:flex;flex-wrap:wrap;align-items:flex-end;gap:12px}
.brand{flex:1;min-width:230px}
h1{font-family:Georgia,"Times New Roman","Noto Serif SC",serif;font-size:38px;font-weight:400;
  letter-spacing:-.025em;line-height:1;margin:0;color:var(--text)}
h1 span{color:var(--accent)}
.counts{display:block;margin-top:10px;color:var(--dim);font-size:13px;letter-spacing:.01em}
.header-actions{display:flex;gap:8px;align-items:center}
.iconbtn{background:transparent;border:1px solid var(--line);color:var(--dim);border-radius:12px;
  padding:9px 13px;font-size:13px;cursor:pointer}
.iconbtn:hover{color:var(--text);border-color:var(--accent)}
.android-only{display:none}
body.android .android-only{display:inline-flex}
.tabs{width:100%;display:flex;gap:28px;margin-top:18px;border-bottom:1px solid var(--line)}
.tabs button{background:none;border:0;color:var(--dim);padding:10px 0 13px;font-size:14px;
  position:relative;border-radius:0;cursor:pointer}
.tabs button.on{background:transparent;border-color:transparent;color:var(--text);font-weight:600}
.tabs button.on::after{content:"";position:absolute;left:0;right:0;bottom:-1px;height:2px;background:var(--accent)}
main{max-width:1480px;margin:0 auto;padding:20px 30px 120px}
.searchrow{display:flex;gap:10px;align-items:center;margin:0 0 12px}
.searchbox{flex:1;display:flex;align-items:center;gap:10px;min-width:0;
  background:var(--panel);border:1px solid var(--line);border-radius:17px;padding:6px 7px 6px 16px}
.searchbox:focus-within{border-color:var(--accent)}
.searchicon{font-size:22px;color:var(--accent);line-height:1;flex:none}
#kw{flex:1;min-width:80px;background:transparent;border:0;outline:none;color:var(--text);
  font-size:15px;padding:10px 2px}
#kw::placeholder{color:#7f7167}
.numbox{display:flex;align-items:center;gap:2px;padding:0 10px;border-left:1px solid var(--line);flex:none}
#limit{width:40px;background:transparent;border:0;outline:none;color:var(--text);font-size:15px;text-align:right}
.numbox span{color:var(--dim);font-size:12px}
.send{width:48px;height:48px;border-radius:13px;background:var(--accent);border:1px solid var(--accent);
  color:#24130e;font-size:22px;font-weight:700;cursor:pointer;flex:none;padding:0}
.send:hover{background:var(--accent2)}
.filterrow{display:flex;align-items:center;gap:16px;flex-wrap:wrap;min-height:32px;margin:4px 0 14px}
.platforms{display:flex;gap:20px;overflow-x:auto;scrollbar-width:none;flex:1;min-width:0}
.platforms::-webkit-scrollbar{display:none}
.platforms button{display:flex;align-items:center;gap:7px;background:none;border:0;color:var(--dim);
  padding:6px 0;font-size:14px;white-space:nowrap;cursor:pointer}
.platforms button.on{color:var(--text)}
.platforms button i{width:6px;height:6px;border-radius:50%;background:transparent;flex:none}
.platforms button.on i{background:var(--accent)}
.toolbar{display:flex;align-items:center;gap:16px;margin:2px 0 18px;padding-bottom:12px;
  border-bottom:1px solid var(--line)}
.checkall{display:flex;align-items:center;gap:7px;color:var(--dim);font-size:13px;cursor:pointer;white-space:nowrap}
.checkall input{accent-color:var(--accent)}
#q{flex:1;max-width:340px;background:transparent;border:0;border-bottom:1px solid var(--line);
  color:var(--text);padding:9px 2px;outline:none;font-size:13px}
#q:focus{border-color:var(--accent)}
.grid{display:grid;gap:18px;grid-template-columns:repeat(auto-fill,minmax(260px,1fr))}
.card{background:linear-gradient(160deg,var(--panel2),var(--panel));border:1px solid var(--line);
  border-radius:16px;overflow:hidden;display:flex;flex-direction:column;min-width:0;position:relative;
  user-select:none;transition:border-color .18s ease,box-shadow .18s ease,transform .18s ease}
.card.selected{border-color:var(--accent);box-shadow:0 0 0 2px var(--accent),0 16px 36px rgba(224,122,95,.16);
  transform:translateY(-2px)}
.card.selected .thumb::after{content:"✓";position:absolute;top:10px;right:10px;z-index:9;pointer-events:none;
  width:28px;height:28px;display:flex;align-items:center;justify-content:center;border-radius:50%;
  background:var(--accent);color:#24130e;font-weight:800;font-size:16px;
  box-shadow:0 4px 12px rgba(0,0,0,.35);animation:selPop .22s cubic-bezier(.2,1.5,.5,1)}
@keyframes selPop{0%{transform:scale(.4);opacity:0}100%{transform:scale(1);opacity:1}}
.thumb{position:relative;background:#0f0c0b;cursor:zoom-in}
.thumb img{width:100%;aspect-ratio:4/5;object-fit:cover;display:block;transition:filter .2s}
.thumb:hover img{filter:brightness(1.05)}
.body{padding:12px 13px 14px;display:flex;flex-direction:column;gap:5px;flex:1;min-width:0}
.artist{font-weight:600;font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text)}
.title{color:var(--dim);font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.src{color:#7e6d61;font-size:10.5px;text-transform:uppercase;letter-spacing:.07em}
.controls{display:flex;flex-wrap:wrap;gap:7px;margin-top:auto;padding-top:10px;min-width:0}
.controls .cat{flex:1 1 100%;width:100%;min-width:0;max-width:100%;box-sizing:border-box;
  background:#2b211d;border:1px solid var(--line);color:var(--text);border-radius:9px;padding:8px 10px;
  font-size:12.5px;text-overflow:ellipsis}
button{background:#2b211d;border:1px solid var(--line);color:var(--text);border-radius:9px;
  padding:8px 11px;font-size:12.5px;cursor:pointer;transition:background .15s,border-color .15s}
button:hover{border-color:#6a4d40}
button.primary{background:var(--accent);border-color:var(--accent);color:#24130e;font-weight:600}
button.ghost{background:transparent}
button.danger{color:#e8a195;border-color:#5a332c}
button:disabled{opacity:.4;cursor:default}
select{background:#2b211d;color:var(--text);border:1px solid var(--line);border-radius:9px;padding:7px}
.batchbar{background:#241c18;border:1px solid #4a342b;box-shadow:0 18px 50px rgba(0,0,0,.55)}
.batchbar .n{color:var(--accent);font-weight:600}
.confirm-box{background:var(--panel2);border-color:var(--line)}
.modal{border-radius:12px}
.toast{background:#2c211d;border:1px solid #4a342b;color:var(--text)}
.morebar .hint,.filterrow .hint{color:var(--dim);font-size:12px}
@media(max-width:700px){
  header{padding:16px 16px 0}
  h1{font-size:30px}
  .header-actions{gap:6px}
  .iconbtn{padding:8px 10px;font-size:12px}
  .tabs{gap:22px;margin-top:12px}
  main{padding:14px 14px 130px}
  .searchrow{gap:8px}
  .searchbox{padding-left:12px}
  #kw{font-size:14px}
  .numbox{padding:0 7px}
  .send{width:44px;height:44px}
  .filterrow{gap:10px}
  .platforms{gap:16px}
  .grid{grid-template-columns:1fr;gap:14px}
  .controls button,.controls select{flex:1 1 100%;width:100%;min-width:0;max-width:100%;box-sizing:border-box}
  .controls .cat{font-size:13px;padding:9px 10px}
  .batchbar{bottom:10px;max-width:96vw;padding:9px;gap:6px}
}
</style></head>
<body>
<header>
  <div class="brand">
    <h1>Art<span>Radar</span></h1>
    <span class="counts" id="counts"></span>
  </div>
  <div class="header-actions">
    <button id="mobilePair" class="iconbtn">手机端</button>
    <button id="refreshBtn" class="iconbtn">刷新</button>
    <button id="settingsBtn" class="iconbtn android-only">设置</button>
  </div>
  <div class="tabs">
    <button data-tab="pending" class="on">待确认</button>
    <button data-tab="kept">已收藏</button>
    <button data-tab="rejected">已拒绝</button>
  </div>
</header>
<main>
  <div class="searchrow">
    <div class="searchbox">
      <span class="searchicon">⌕</span>
      <input id="kw" placeholder="关键词，或：在微博帮我找一组画师稿">
      <span class="numbox"><input id="limit" type="number" min="1" max="50" value="10"><span>张</span></span>
      <button id="more" class="send" title="再来一组">→</button>
    </div>
    <input id="fileInput" type="file" accept="image/*" multiple hidden>
    <button id="uploadBtn" class="iconbtn">上传</button>
  </div>
  <div class="filterrow">
    <div class="platforms" id="platformPills">
      <button data-platform="all" class="on"><i></i>全部平台</button>
      <button data-platform="weibo"><i></i>微博</button>
      <button data-platform="pixiv"><i></i>Pixiv</button>
      <button data-platform="pinterest"><i></i>Pinterest</button>
    </div>
    <select id="platform" hidden>
      <option value="all">全部</option>
      <option value="pixiv">Pixiv</option>
      <option value="weibo">微博</option>
      <option value="pinterest">Pinterest</option>
    </select>
    <button id="moreCancel" class="ghost danger" style="display:none">取消</button>
    <span class="hint" id="moreState"></span>
  </div>
  <div class="toolbar">
    <label class="checkall"><input type="checkbox" id="selAll"> 全选</label>
    <input id="q" type="search" placeholder="搜索画师 / 标题 / 分类">
  </div>
  <div class="grid" id="grid"></div>
</main>
<div class="batchbar" id="batchbar">
  <span class="n" id="batchCount">已选 0</span>
  <button data-batch="artist" class="ghost">存画师</button>
  <select id="batchCat"></select>
  <button data-batch="save-cat">存到分类</button>
  <button data-batch="reject" class="ghost danger">不要</button>
  <button data-batch="move">移动</button>
  <button data-batch="restore">放回待确认</button>
  <button data-batch="delete" class="ghost danger">删除</button>
  <button data-batch="clear" class="ghost">取消</button>
</div>
<div class="modal confirm" id="confirm">
  <div class="confirm-box">
    <p id="confirmText"></p>
    <div class="confirm-actions">
      <button id="confirmCancel" class="ghost">取消</button>
      <button id="confirmOk" class="primary">确认</button>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>
<div class="modal" id="modal"><button class="close" id="close">×</button><img id="big" alt=""></div>
<script>
const state={tab:'pending',cats:[],items:[]};
const sel=new Set();
let allIds=[];
let selAnchor=null;
const coarse=matchMedia('(pointer:coarse)').matches;
let batchBusy=false;
const $=s=>document.querySelector(s);
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function toast(t){const el=$('#toast');el.textContent=t;el.classList.add('show');setTimeout(()=>el.classList.remove('show'),1800)}
const TOKEN=new URLSearchParams(location.search).get('token')||'';
async function api(path,body){const r=await fetch(path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json','X-Art-Token':TOKEN},
  body:body?JSON.stringify(body):undefined});const j=await r.json();if(!j.ok&&j.error)throw new Error(j.error);return j}
async function refresh(){const s=await api('/api/state');state.cats=s.categories;
  $('#counts').textContent=`待确认 ${s.counts.pending||0} · 已收藏 ${s.counts.kept||0} · 图库 ${s.library_total} 张`;
  await load()}
async function load(){const q=$('#q').value.trim();const j=await api('/api/items?status='+state.tab+(q?'&q='+encodeURIComponent(q):''));
  state.items=j.items;sel.clear();selAnchor=null;render()}
function options(sel){return state.cats.map(c=>`<option ${c===sel?'selected':''}>${esc(c)}</option>`).join('')}
function render(){const g=$('#grid');
  allIds=state.items.map(x=>x.id);
  if(!state.items.length){g.innerHTML='<div class="empty">这里还没有内容。</div>';syncSel();return}
  if(state.tab==='pending'){g.innerHTML=state.items.map(it=>`
    <article class="card" data-id="${it.id}">
      <div class="thumb" data-view="${it.id}" title="查看大图"><img loading="lazy" src="${it.thumb_url}" alt=""></div>
      <div class="body">
        <div class="artist">${esc(it.artist||'未知画师')}</div>
        <div class="title">${esc(it.title||'')}</div>
        <div class="src">${esc(it.source)} · ${it.width}×${it.height}</div>
        <div class="controls">
          <select class="cat" data-id="${it.id}">${options(it.suggested)}</select>
          <button data-act="save-cat" data-id="${it.id}">存到分类</button>
          ${it.artist?`<button class="ghost" data-act="artist" data-id="${it.id}">存画师</button>`:''}
          <button class="ghost danger" data-act="reject" data-id="${it.id}">不要</button>
        </div>
      </div>
    </article>`).join('')}
  else {g.innerHTML=state.items.map(it=>`
    <article class="card" data-id="${it.id}">
      <div class="thumb" data-view="${it.id}" title="查看大图"><img loading="lazy" src="${it.thumb_url}" alt=""></div>
      <div class="body">
        <div class="artist">${esc(it.artist||'未知名')}</div>
        <div class="title">${esc(it.title||'')}</div>
        <div class="path">${esc(it.folder_rel||it.folder||'')}</div>
        ${it.note?`<div class="title">备注：${esc(it.note)}</div>`:''}
        <div class="controls">
          ${state.tab==='kept'?`<select class="cat" data-id="${it.id}">${options(it.folder_rel)}</select>
            <button data-act="move" data-id="${it.id}">移动</button>`:
            `<button data-act="restore" data-id="${it.id}">放回待确认</button>`}
          <button class="ghost danger" data-act="delete" data-id="${it.id}">删除</button>
        </div>
      </div>
    </article>`).join('')}
  allIds=state.items.map(x=>x.id);
  syncSel();
}
function syncSel(){
  const n=sel.size;
  $('#batchCount').textContent='已选 '+n;
  $('#batchbar').classList.toggle('show',n>0);
  document.querySelectorAll('.card').forEach(c=>c.classList.toggle('selected',sel.has(c.dataset.id)));
  if(selAnchor&&!allIds.includes(selAnchor))selAnchor=null;
  const total=allIds.length;
  $('#selAll').checked=total>0&&n===total;
  $('#selAll').indeterminate=n>0&&n<total;
  const show=(act,on)=>document.querySelectorAll(`[data-batch="${act}"]`).forEach(b=>b.style.display=on?'':'none');
  const p=state.tab==='pending', k=state.tab==='kept', r=state.tab==='rejected';
  show('artist',p);show('save-cat',p);show('reject',p);
  show('move',k);show('restore',r);show('delete',true);
  const bc=$('#batchCat');
  bc.style.display=(p||k)?'':'none';
  if(bc.options.length!==state.cats.length){bc.innerHTML=state.cats.map(c=>`<option>${esc(c)}</option>`).join('')}
  if(!n){delete bc.dataset.user}
  else if(bc.dataset.user!=='1'){
    const first=state.items.find(x=>sel.has(x.id));
    if(first)bc.value=k?(first.folder_rel||state.cats[0]):(first.suggested||state.cats[0]);
    if(!bc.value)bc.value=state.cats[0];
  }
}
let confirmResolve=null;
function askConfirm(text){
  return new Promise(resolve=>{
    $('#confirmText').textContent=text;
    $('#confirm').classList.add('show');
    confirmResolve=resolve;
  });
}
function closeConfirm(val){
  const r=confirmResolve;confirmResolve=null;
  $('#confirm').classList.remove('show');
  if(r)r(val);
}
$('#confirmOk').onclick=()=>closeConfirm(true);
$('#confirmCancel').onclick=()=>closeConfirm(false);
async function pollAccept(){
  try{
    const s=await api('/api/accept/status');
    if(s.state==='running'){
      $('#batchCount').textContent=`归档中 ${s.done}/${s.total}`;
      setTimeout(pollAccept,1000);return;
    }
    batchBusy=false;$('#batchbar').style.pointerEvents='';
    if(s.state==='error')toast('归档失败：'+s.error);
    else toast('已归档 '+s.added+' 张');
    sel.clear();await refresh();
  }catch(e){batchBusy=false;$('#batchbar').style.pointerEvents='';toast(e.message)}
}
async function batch(act){
  const ids=[...sel];if(!ids.length||batchBusy)return;
  if(act==='clear'){sel.clear();render();return}
  const cat=$('#batchCat').value;
  const n=ids.length;
  const texts={
    artist:`确定把选中的 ${n} 张存到各自画师文件夹？`,
    'save-cat':`确定把选中的 ${n} 张存到「${cat}」？`,
    reject:`确定把选中的 ${n} 张移出待确认（拒绝）？`,
    move:`确定把选中的 ${n} 张移动到「${cat}」？`,
    restore:`确定把选中的 ${n} 张放回待确认？`,
    delete:`确定从磁盘删除选中的 ${n} 张？图片和边车 JSON 都会删除，不能撤销。`
  };
  if(!await askConfirm(texts[act]||`确定对选中的 ${n} 张执行此操作？`))return;
  batchBusy=true;$('#batchbar').style.pointerEvents='none';
  if(act==='artist'||act==='save-cat'){
    try{
      const payload={ids,async:true};
      if(act==='artist')payload.artist=true;
      if(act==='save-cat')payload.category=cat;
      await api('/api/accept',payload);
      $('#batchCount').textContent=`归档中 0/${n}`;
      toast(`开始归档 ${n} 张…`);
      pollAccept();
    }catch(e){toast(e.message);batchBusy=false;$('#batchbar').style.pointerEvents=''}
    return;
  }
  try{
    if(act==='reject')await api('/api/reject',{ids,reason:'网页上批量不要'});
    else if(act==='move')await api('/api/move',{ids,folder:cat});
    else if(act==='restore')await api('/api/restore',{ids});
    else if(act==='delete')await api('/api/delete',{ids});
    toast(`已处理 ${n} 张`);
    sel.clear();await refresh();
  }catch(e){toast(e.message)}
  finally{batchBusy=false;$('#batchbar').style.pointerEvents=''}
}
async function act(btn){const id=btn.dataset.id, a=btn.dataset.act;
  if(a==='artist'){btn.disabled=true;await api('/api/accept',{ids:[id],artist:true});toast('已存到画师文件夹')}
  else if(a==='save-cat'){const sel=document.querySelector(`select.cat[data-id="${id}"]`);btn.disabled=true;
    await api('/api/accept',{ids:[id],category:sel.value});toast('已存到 '+sel.value)}
  else if(a==='reject'){await api('/api/reject',{ids:[id],reason:'网页上手动画不要'});toast('已拒绝')}
  else if(a==='move'){const sel=document.querySelector(`select.cat[data-id="${id}"]`);
    await api('/api/move',{id,folder:sel.value});toast('已移动到 '+sel.value)}
  else if(a==='delete'){if(!await askConfirm('确定从磁盘删除这张图？图片和边车 JSON 都会删除。')){return}
    await api('/api/delete',{id});toast('已删除')}
  else if(a==='restore'){await api('/api/restore',{ids:[id]});toast('已放回待确认')}
  await refresh()}
function selectCard(id,shift,ctrl){
  const idx=allIds.indexOf(id);
  if(idx<0)return;
  if(shift&&selAnchor&&allIds.includes(selAnchor)){
    const a=allIds.indexOf(selAnchor),b=idx,lo=Math.min(a,b),hi=Math.max(a,b);
    if(!ctrl)sel.clear();
    for(let i=lo;i<=hi;i++)sel.add(allIds[i]);
  }else if(ctrl){
    if(sel.has(id))sel.delete(id);else sel.add(id);
    selAnchor=id;
  }else if(coarse){
    if(sel.has(id))sel.delete(id);else sel.add(id);
    selAnchor=id;
  }else{
    if(sel.size===1&&sel.has(id)){sel.clear()}else{sel.clear();sel.add(id)}
    selAnchor=id;
  }
  syncSel();
}
document.addEventListener('click',async e=>{
  const bb=e.target.closest('[data-batch]');if(bb){e.preventDefault();batch(bb.dataset.batch);return}
  const t=e.target.closest('[data-act]');if(t){e.preventDefault();try{await act(t)}catch(err){toast(err.message)}return}
  if(e.target.closest('button,select,input,a'))return;
  const v=e.target.closest('[data-view]');
  if(v){e.preventDefault();$('#big').src='/img/full/'+v.dataset.view;$('#modal').classList.add('show');return}
  const card=e.target.closest('.card');
  if(card){e.preventDefault();selectCard(card.dataset.id,e.shiftKey,e.ctrlKey||e.metaKey)}
});
$('#selAll').onchange=()=>{if($('#selAll').checked)allIds.forEach(id=>sel.add(id));else sel.clear();selAnchor=allIds[0]||null;render()};
$('#batchCat').onchange=()=>{$('#batchCat').dataset.user='1'};
$('#close').onclick=()=>$('#modal').classList.remove('show');
$('#modal').onclick=e=>{if(e.target.id==='modal')$('#modal').classList.remove('show')};
document.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('.tabs button').forEach(x=>x.classList.remove('on'));b.classList.add('on');
  state.tab=b.dataset.tab;load()});
let timer;$('#q').oninput=()=>{clearTimeout(timer);timer=setTimeout(load,250)};
let moreTimer=null;
async function pollMore(){
  try{
    const s=await api('/api/more/status');
    if(s.state==='running'||s.state==='cancelling'){
      $('#moreCancel').style.display='';
      $('#moreState').textContent=s.state==='cancelling'?'正在取消…':'正在抓取，通常 30-90 秒…';
      moreTimer=setTimeout(pollMore,2000);return
    }
    $('#more').disabled=false;$('#more').textContent='→';$('#moreCancel').style.display='none';
    if(s.state==='done'){const label={weibo:'微博',pixiv:'Pixiv',pinterest:'Pinterest',auto:'关键词'}[s.mode]||'';
      const extra=s.filtered?('，自动过滤 '+s.filtered+' 张'):'';
      const short=(s.limit&&s.added<s.limit)?('（请求 '+s.limit+'，实际可用 '+s.added+'）'):'';
      const msg=(!s.added&&!s.filtered)?('没有新作品（可能都已入库）'+short):(label+'：新增 '+s.added+' 张'+extra+short);
      $('#moreState').textContent=msg;toast(msg);
      state.tab='pending';document.querySelectorAll('.tabs button').forEach(x=>x.classList.toggle('on',x.dataset.tab==='pending'));
      await refresh()}
    else if(s.state==='cancelled'){$('#moreState').textContent='已取消，保留已抓到的 '+s.added+' 张';toast('已取消，保留已抓到的 '+s.added+' 张');await refresh()}
    else if(s.state==='error'){$('#moreState').textContent='抓取失败';toast(s.error||'抓取失败')}
    else {$('#moreState').textContent='任务已结束';}
  }catch(e){$('#more').disabled=false;$('#more').textContent='→';$('#moreCancel').style.display='none';
    $('#moreState').textContent='状态检查失败，请刷新';toast(e.message)}
}
$('#moreCancel').onclick=async()=>{
  $('#moreCancel').disabled=true;$('#moreState').textContent='正在取消…';
  try{await api('/api/more/cancel',{})}catch(e){toast(e.message)}
};
const moreSaved=JSON.parse(localStorage.getItem('artradar.more')||'{}');
if(moreSaved.limit)$('#limit').value=moreSaved.limit;
function setPlatform(p){
  if(!['all','weibo','pixiv','pinterest'].includes(p))p='all';
  $('#platform').value=p;
  document.querySelectorAll('#platformPills button').forEach(b=>b.classList.toggle('on',b.dataset.platform===p));
}
setPlatform(moreSaved.platform||'all');
document.querySelectorAll('#platformPills button').forEach(b=>b.onclick=()=>{
  setPlatform(b.dataset.platform);
  const cur=JSON.parse(localStorage.getItem('artradar.more')||'{}');
  localStorage.setItem('artradar.more',JSON.stringify({...cur,platform:b.dataset.platform}));
});
$('#refreshBtn').onclick=()=>refresh();
if(window.AndroidBridge){
  document.body.classList.add('android');
  $('#settingsBtn').onclick=()=>{try{window.AndroidBridge.openSettings()}catch(e){}};
}
$('#more').onclick=async()=>{
  if($('#more').disabled)return;
  const limit=Math.max(1,Math.min(50,parseInt($('#limit').value)||10));
  const platform=$('#platform').value;
  localStorage.setItem('artradar.more',JSON.stringify({limit,platform}));
  $('#more').disabled=true;$('#more').textContent='…';$('#moreState').textContent='';
  $('#moreCancel').style.display='';$('#moreCancel').disabled=false;
  try{await api('/api/more',{keywords:$('#kw').value.trim(),limit,platform});pollMore()}
  catch(e){$('#more').disabled=false;$('#more').textContent='→';$('#moreCancel').style.display='none';toast(e.message)}
};
$('#kw').addEventListener('keydown',e=>{if(e.key==='Enter')$('#more').click()});

async function uploadFiles(files){
  files=[...files].filter(f=>f.type.startsWith('image/'));
  if(!files.length)return;
  const btn=$('#uploadBtn');btn.disabled=true;btn.textContent='上传中…';
  let ok=0;
  for(const f of files){
    try{
      const r=await fetch('/api/upload',{method:'POST',
        headers:{'Content-Type':f.type||'application/octet-stream',
                 'X-Art-Token':TOKEN,
                 'X-Filename':encodeURIComponent(f.name)},body:f});
      const j=await r.json();
      if(j.ok){ok++}else{toast(f.name+'：'+(j.error||'上传失败'))}
    }catch(e){toast(f.name+'：'+e.message)}
  }
  btn.disabled=false;btn.textContent='上传图片';
  if(ok){toast('已上传 '+ok+' 张，已加入待确认并自动建议分类');await refresh()}
}
$('#uploadBtn').onclick=()=>$('#fileInput').click();
$('#fileInput').onchange=e=>{uploadFiles(e.target.files);e.target.value=''};
document.addEventListener('dragover',e=>{if([...e.dataTransfer.types].includes('Files'))e.preventDefault()});
document.addEventListener('drop',e=>{if(e.dataTransfer.files.length){e.preventDefault();uploadFiles(e.dataTransfer.files)}});
$('#mobilePair').onclick=async()=>{
  try{const j=await api('/api/pair');
    window.prompt('手机端配对地址（复制到 ArtRadar App 的设置里）',j.url)
  }catch(e){toast(e.message)}
};
refresh();
</script></body></html>"""


try:
    PAGE = (core.RESOURCE_DIR / "ui.html").read_text(encoding="utf-8")
except OSError:
    pass


def main() -> None:
    global PORT
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    PORT = args.port
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    pair = f"http://{lan_ip()}:{args.port}/?token={TOKEN}"
    print(f"art-radar UI: {url}", flush=True)
    print(f"手机端配对: {pair}", flush=True)
    threading.Thread(target=migrate_followed, daemon=True).start()
    if args.open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
