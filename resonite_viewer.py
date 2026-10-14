#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 kazu0617
# SPDX-License-Identifier: 0BSD
"""Resonite アカウントエクスポートのローカルビューア。

Python 3.8+ の標準ライブラリのみで動作 (Linux / Windows / macOS)。

使い方:
    python3 resonite_viewer.py [EXPORT_DIR] [--port 8765] [--no-browser]

    EXPORT_DIR: U-xxx/ と Assets/ を含むディレクトリ (例: ResoniteDownload)
                省略時はカレントディレクトリかスクリプトと同じ場所の ResoniteDownload を探す

ブラウザで http://127.0.0.1:8765/ が開く。データはローカルからのみ参照され、外部には送信しない。
"""
import argparse
import gzip
import json
import os
import re
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

HASH_RE = re.compile(r"^[0-9a-f]{64}$")
URI_RE = re.compile(r"^(?:resdb|neosdb):///([0-9a-f]{64})")
OWNER_RE = re.compile(r"^[UG]-[^/\\]+$")
MAIN_ASSET_TAGS = ("texture_asset:", "clip_asset:", "raw_file_asset:")
PHOTO_FLAGS = ("360_photo", "stereo_photo")
META_KEYS = ("location_name", "location_host", "location_accesslevel", "timestamp", "world_url", "clip_length")


def read_json(path):
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def uri_hash(uri):
    m = URI_RE.match(uri or "")
    return m.group(1) if m else None


def sniff(path):
    """(mime, ext) を先頭バイトから判定する。"""
    try:
        with open(path, "rb") as f:
            b = f.read(16)
    except OSError:
        return None, None
    if b[:3] == b"\xff\xd8\xff":
        return "image/jpeg", "jpg"
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", "png"
    if b[:4] == b"GIF8":
        return "image/gif", "gif"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp", "webp"
    if b[:4] == b"RIFF" and b[8:12] == b"WAVE":
        return "audio/wav", "wav"
    if b[:4] == b"OggS":
        return "audio/ogg", "ogg"
    if b[:4] == b"fLaC":
        return "audio/flac", "flac"
    if b[:3] == b"ID3" or b[:2] in (b"\xff\xfb", b"\xff\xf3"):
        return "audio/mpeg", "mp3"
    if b[4:8] == b"ftyp":
        return "video/mp4", "mp4"
    if b[:4] == b"\x1aE\xdf\xa3":
        return "video/webm", "webm"
    if b[:4] == b"%PDF":
        return "application/pdf", "pdf"
    if b[:4] == b"FrDT":
        return "application/octet-stream", "brson"
    if b[:3] == b"\x5d\x00\x00":
        return "application/octet-stream", "7zbson"
    return "application/octet-stream", "bin"


class Store:
    def __init__(self, root):
        self.root = root
        self.assets_dir = os.path.join(root, "Assets")
        self.assets = set(os.listdir(self.assets_dir)) if os.path.isdir(self.assets_dir) else set()
        self.owners = sorted(d for d in os.listdir(root)
                             if OWNER_RE.match(d) and os.path.isdir(os.path.join(root, d)))
        self._sniff = {}
        self._cache = {}
        self._lock = threading.Lock()

    def asset_path(self, h):
        return os.path.join(self.assets_dir, h) if h in self.assets else None

    def mime(self, h):
        if h not in self._sniff:
            self._sniff[h] = sniff(os.path.join(self.assets_dir, h))
        return self._sniff[h]

    def owner_list(self):
        out = []
        for o in self.owners:
            name = o
            p = os.path.join(self.root, o, "UserData", "User.json")
            if os.path.exists(p):
                try:
                    name = read_json(p).get("username") or o
                except (OSError, ValueError):
                    pass
            out.append({"id": o, "name": name})
        return out

    def owner(self, oid):
        with self._lock:
            if oid not in self._cache:
                self._cache[oid] = self._load(oid)
            return self._cache[oid]

    def _dir(self, oid, *parts):
        return os.path.join(self.root, oid, *parts)

    def _json_files(self, d):
        if not os.path.isdir(d):
            return
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".json"):
                try:
                    yield fn, read_json(os.path.join(d, fn))
                except (OSError, ValueError):
                    print(f"skip (unreadable): {os.path.join(d, fn)}", file=sys.stderr)

    def _main_asset(self, rec, obj, thumb):
        for tag in rec.get("tags") or []:
            if tag.startswith(MAIN_ASSET_TAGS):
                h = uri_hash(tag.split(":", 1)[1])
                if h and h in self.assets:
                    return h
        cands = sorted((m for m in rec.get("assetManifest") or rec.get("neosDBmanifest") or []
                        if m.get("hash") not in (obj, thumb) and m.get("hash") in self.assets),
                       key=lambda m: -m.get("bytes", 0))
        for m in cands:
            mime, _ = self.mime(m["hash"])
            if mime and mime.split("/")[0] in ("image", "audio", "video"):
                return m["hash"]
        return None

    def _compact(self, rec):
        tags = rec.get("tags") or []
        meta, users, plain = {}, [], []
        for t in tags:
            k, sep, v = t.partition(":")
            if sep and k == "user":
                users.append(v)
            elif sep and k in META_KEYS:
                meta[k] = v
            elif not sep:
                plain.append(t)
        photo = "camera_photo" in plain
        obj = uri_hash(rec.get("assetUri"))
        thumb = uri_hash(rec.get("thumbnailUri"))
        main = self._main_asset(rec, obj, thumb) if rec.get("recordType") != "directory" else None
        mime, ext = self.mime(main) if main else (None, None)
        path = (rec.get("path") or "").replace("\\", "/")
        out = {
            "id": rec.get("id"), "n": rec.get("name") or "", "t": rec.get("recordType"), "p": path,
            "ct": rec.get("creationTime"), "mt": rec.get("lastModificationTime"),
        }
        if thumb and thumb in self.assets:
            out["th"] = thumb
        if main:
            out["a"], out["ak"], out["ax"] = main, mime.split("/")[0], ext
        if photo:
            out["ph"] = 1
            out["f"] = [t for t in plain if t in PHOTO_FLAGS]
        else:
            out["tg"] = plain
        if meta:
            out["m"] = meta
        if users:
            out["u"] = users
        if rec.get("description"):
            out["d"] = rec["description"]
        if rec.get("isPublic"):
            out["pub"] = 1
        if rec.get("isDeleted"):
            out["del"] = 1
        if rec.get("recordType") in ("link", "world") or (rec.get("assetUri") or "").startswith("resrec"):
            out["au"] = rec.get("assetUri")
        return out

    def _load(self, oid):
        print(f"loading {oid} ...", file=sys.stderr)
        d = {"owner": oid, "user": None, "patreon": [], "records": [], "contacts": [], "convs": [],
             "variables": [], "audit": [], "assetCount": len(self.assets)}
        files = {}
        for fn, rec in self._json_files(self._dir(oid, "Records")):
            if rec.get("id"):
                files[rec["id"]] = fn
                d["records"].append(self._compact(rec))
        up = self._dir(oid, "UserData", "User.json")
        if os.path.exists(up):
            d["user"] = read_json(up)
        d["patreon"] = [p for _, p in self._json_files(self._dir(oid, "UserData", "Patreon"))]
        for _, c in self._json_files(self._dir(oid, "Contacts")):
            prof = c.get("profile") or {}
            d["contacts"].append({"id": c.get("id"), "n": c.get("contactUsername"), "s": c.get("contactStatus"),
                                  "acc": c.get("isAccepted"), "last": c.get("latestMessageTime"),
                                  "icon": uri_hash(prof.get("iconUrl"))})
        mdir = self._dir(oid, "Messages")
        convs = set()
        if os.path.isdir(mdir):
            for cid in sorted(os.listdir(mdir)):
                if not os.path.isdir(os.path.join(mdir, cid)):
                    continue
                convs.add(cid)
                n, last, snip = 0, "", ""
                for _, m in self._json_files(os.path.join(mdir, cid)):
                    n += 1
                    st = m.get("sendTime") or ""
                    if st >= last:
                        last = st
                        snip = (m.get("content") or "")[:80] if m.get("messageType") == "Text" else f"[{m.get('messageType')}]"
                d["convs"].append({"id": cid, "c": n, "last": last, "snip": snip})
        for _, v in self._json_files(self._dir(oid, "Variables")):
            d["variables"].append({"p": v.get("path"), "v": v.get("value"), "ts": v.get("timestamp")})
        for _, a in self._json_files(self._dir(oid, "RecordAudit")):
            d["audit"].append({"ts": a.get("timestamp"), "op": a.get("operation"), "rid": a.get("recordId"),
                               "n": a.get("originalName"), "p": (a.get("originalPath") or "").replace("\\", "/"),
                               "src": a.get("modifyingSource"), "u": a.get("modifyingUserId"),
                               "au": a.get("originalAssetUrl")})
        body = json.dumps(d, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        print(f"loaded {oid}: {len(d['records'])} records, {len(d['contacts'])} contacts", file=sys.stderr)
        return {"files": files, "convs": convs, "json": body, "gz": gzip.compress(body, 6)}

    def record(self, oid, rid):
        o = self.owner(oid)
        fn = o["files"].get(rid)
        if not fn:
            return None
        rec = read_json(self._dir(oid, "Records", fn))
        manifest = []
        for m in rec.get("assetManifest") or rec.get("neosDBmanifest") or []:
            h = m.get("hash")
            ok = h in self.assets
            mime, ext = self.mime(h) if ok else (None, None)
            manifest.append({"hash": h, "bytes": m.get("bytes"), "exists": ok, "mime": mime, "ext": ext})
        return {"rec": rec, "manifest": manifest}

    def messages(self, oid, cid):
        if cid not in self.owner(oid)["convs"]:
            return None
        out = []
        for _, m in self._json_files(self._dir(oid, "Messages", cid)):
            c = m.get("content")
            if m.get("messageType") != "Text" and isinstance(c, str):
                try:
                    c = json.loads(c)
                except ValueError:
                    pass
            out.append({"id": m.get("id"), "s": m.get("senderId"), "r": m.get("recipientId"),
                        "t": m.get("messageType"), "c": c, "st": m.get("sendTime"), "rt": m.get("readTime")})
        out.sort(key=lambda x: x["st"] or "")
        return out


class Handler(BaseHTTPRequestHandler):
    store = None

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, body, ctype, gz=None):
        use_gz = gz is not None and "gzip" in (self.headers.get("Accept-Encoding") or "")
        data = gz if use_gz else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if use_gz:
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", gzip.compress(body, 6))

    def _owner(self, q):
        oid = (q.get("owner") or [""])[0]
        return oid if oid in self.store.owners else None

    def do_GET(self):
        try:
            self._route()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _route(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/":
            body = PAGE.encode("utf-8")
            return self._send(200, body, "text/html; charset=utf-8", gzip.compress(body))
        if u.path == "/api/owners":
            return self._json(self.store.owner_list())
        if u.path == "/api/data":
            oid = self._owner(q)
            if not oid:
                return self._json({"error": "unknown owner"}, 404)
            o = self.store.owner(oid)
            return self._send(200, o["json"], "application/json; charset=utf-8", o["gz"])
        if u.path == "/api/record":
            oid = self._owner(q)
            r = self.store.record(oid, (q.get("id") or [""])[0]) if oid else None
            return self._json(r) if r else self._json({"error": "not found"}, 404)
        if u.path == "/api/messages":
            oid = self._owner(q)
            r = self.store.messages(oid, (q.get("cid") or [""])[0]) if oid else None
            return self._json(r) if r is not None else self._json({"error": "not found"}, 404)
        if u.path.startswith("/asset/"):
            h = u.path[len("/asset/"):]
            p = self.store.asset_path(h) if HASH_RE.match(h) else None
            if not p:
                return self._send(404, b"not found", "text/plain")
            return self._file(p, self.store.mime(h), (q.get("dl") or [None])[0])
        self._send(404, b"not found", "text/plain")

    def _file(self, path, mime_ext, dl):
        mime, ext = mime_ext
        size = os.path.getsize(path)
        start, end, status = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range") or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", mime or "application/octet-stream")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "max-age=604800, immutable")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if dl:
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", dl).strip() or "asset"
            if ext and not name.lower().endswith("." + ext):
                name += "." + ext
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(name)}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            remain = end - start + 1
            while remain > 0:
                chunk = f.read(min(1 << 16, remain))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remain -= len(chunk)


def app_dir():
    """スクリプト、または exe としてビルドした場合は exe のあるディレクトリ。"""
    return os.path.dirname(os.path.abspath(sys.argv[0]))


def find_export_dir(arg):
    cands = [arg] if arg else [os.getcwd(), os.path.join(os.getcwd(), "ResoniteDownload"),
                               app_dir(), os.path.join(app_dir(), "ResoniteDownload")]
    for c in cands:
        if c and os.path.isdir(c) and any(OWNER_RE.match(d) for d in os.listdir(c)):
            return os.path.abspath(c)
    return None


def fail(msg):
    print(msg, file=sys.stderr)
    if "__compiled__" in globals():  # exe をダブルクリックした場合にウィンドウがすぐ閉じないようにする
        try:
            input("Enter キーで終了します / Press Enter to exit")
        except EOFError:
            pass
    sys.exit(1)


def main():
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(description="Resonite export viewer")
    ap.add_argument("export_dir", nargs="?")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()

    root = find_export_dir(a.export_dir)
    if not root:
        fail("ダウンロードデータのフォルダが見つかりません。\n"
             "U-xxx/ と Assets/ を含むフォルダ (例: ResoniteDownload) を指定してください。\n"
             "Windows では、そのフォルダを ResoniteViewer.exe にドラッグ&ドロップするか、exe と同じ場所に置いてください。\n"
             "Export directory not found (expects a folder containing U-xxx/ and Assets/).")
    Handler.store = Store(root)
    print(f"export: {root}  owners: {', '.join(Handler.store.owners)}  assets: {len(Handler.store.assets)}")

    httpd = None
    for port in range(a.port, a.port + 20):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if not httpd:
        fail("No free port found.")
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"open {url}  (Ctrl+C or close this window to stop)")
    threading.Thread(target=Handler.store.owner, args=(Handler.store.owners[0],), daemon=True).start()
    if not a.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


PAGE = r"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Resonite Export Viewer</title>
<style>
:root{
  --bg:#1c1c1c;--bg2:#262626;--bg3:#333333;--fg:#e8e8e8;--fg2:#999999;--bd:#444444;
  --red:#d94452;--yellow:#e6a23c;--green:#4caf50;--blue:#5c9fd4;
}
*{box-sizing:border-box}
html,body{margin:0;height:100%;}
body{display:flex;flex-direction:column;background:var(--bg);color:var(--fg);
  font:14px/1.5 system-ui,-apple-system,"Segoe UI","Hiragino Sans","Noto Sans JP",Meiryo,sans-serif}
a{color:var(--blue);text-decoration:none}a:hover{text-decoration:underline}
button,select,input{font:inherit;color:var(--fg);background:var(--bg2);border:1px solid var(--bd);border-radius:4px;padding:5px 9px}
button{cursor:pointer}button:hover{background:var(--bg3)}
input::placeholder{color:var(--fg2)}
header{display:flex;align-items:center;gap:16px;padding:8px 16px;border-bottom:1px solid var(--bd);background:var(--bg2);flex-wrap:wrap}
header h1{font-size:15px;margin:0;font-weight:600}
nav{display:flex;gap:2px;flex-wrap:wrap}
nav button{border:none;background:none;color:var(--fg2);padding:6px 12px;border-radius:4px}
nav button.on{color:var(--fg);background:var(--bg3)}
#status{color:var(--fg2);margin-left:auto;font-size:12px}
main{flex:1;min-height:0}
section{display:none;height:100%;overflow:auto}
section.on{display:block}
.pad{padding:16px}
.muted{color:var(--fg2)}.small{font-size:12px}
.split{display:flex;height:100%}
.side{width:300px;min-width:220px;border-right:1px solid var(--bd);overflow:auto;background:var(--bg)}
.body{flex:1;overflow:auto;min-width:0}
.bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:10px 16px;border-bottom:1px solid var(--bd);position:sticky;top:0;background:var(--bg);z-index:2}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin:12px 0 24px}
.tile{background:var(--bg2);border:1px solid var(--bd);border-radius:6px;padding:10px 12px}
.tile b{display:block;font-size:22px;font-weight:600}
.tile span{color:var(--fg2);font-size:12px}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--bd);vertical-align:top;word-break:break-word}
th{color:var(--fg2);font-weight:normal;position:sticky;top:0;background:var(--bg)}
.kv th{position:static;width:160px}
h2{font-size:16px;margin:22px 0 8px;font-weight:600}
h2:first-child{margin-top:0}
pre{background:var(--bg2);border:1px solid var(--bd);border-radius:4px;padding:10px;overflow:auto;white-space:pre-wrap;word-break:break-all;font:12px/1.45 ui-monospace,Consolas,Menlo,monospace;max-height:480px}
.badge{display:inline-block;font-size:11px;padding:0 6px;border-radius:3px;border:1px solid var(--bd);color:var(--fg2);white-space:nowrap}
.b-green{border-color:var(--green);color:var(--green)}.b-red{border-color:var(--red);color:var(--red)}
.b-yellow{border-color:var(--yellow);color:var(--yellow)}.b-blue{border-color:var(--blue);color:var(--blue)}
.chips{display:flex;flex-wrap:wrap;gap:4px}
.chip{background:var(--bg3);border-radius:3px;padding:1px 7px;font-size:12px;cursor:pointer}
.chip:hover{outline:1px solid var(--bd)}
/* tree */
.tree{list-style:none;margin:0;padding:6px 0;font-size:13px}
.tree ul{list-style:none;margin:0;padding-left:14px}
.node{display:flex;align-items:center;gap:2px;padding:2px 8px;cursor:pointer;white-space:nowrap}
.node:hover{background:var(--bg2)}
.node.sel{background:var(--bg3)}
.caret{width:14px;color:var(--fg2);text-align:center;flex:none;font-size:10px}
.node .nm{overflow:hidden;text-overflow:ellipsis}
.node .ct{color:var(--fg2);font-size:11px;margin-left:6px}
/* grid */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px;padding:12px 16px}
.card{background:var(--bg2);border:1px solid var(--bd);border-radius:6px;overflow:hidden;cursor:pointer;display:flex;flex-direction:column}
.card:hover{border-color:var(--fg2)}
.thumb{aspect-ratio:16/10;background:var(--bg3);display:flex;align-items:center;justify-content:center;color:var(--fg2);font-size:12px;overflow:hidden}
.thumb img{width:100%;height:100%;object-fit:cover;display:block}
.card .cap{padding:5px 8px;font-size:12px;line-height:1.35}
.card .cap .t{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.folder .thumb{aspect-ratio:auto;height:44px;justify-content:flex-start;padding:0 10px;font-size:13px;color:var(--fg)}
.ghead{grid-column:1/-1;color:var(--fg2);font-size:13px;margin-top:8px;border-bottom:1px solid var(--bd);padding-bottom:3px}
.more{grid-column:1/-1;height:1px}
.crumb a{cursor:pointer}
/* drawer & lightbox */
#drawer{position:fixed;top:0;right:0;width:min(520px,100%);height:100%;background:var(--bg2);border-left:1px solid var(--bd);z-index:20;overflow:auto;display:none}
#drawer.on{display:block}
#drawer .hd{display:flex;justify-content:space-between;align-items:center;padding:10px 14px;border-bottom:1px solid var(--bd);position:sticky;top:0;background:var(--bg2)}
#drawer .ct{padding:14px}
.preview img,.preview video{max-width:100%;max-height:360px;display:block;margin:0 auto 10px;border-radius:4px}
.preview audio{width:100%;margin-bottom:10px}
#lb{position:fixed;inset:0;background:rgba(0,0,0,.92);z-index:30;display:none}
#lb.on{display:flex}
#lb .img{flex:1;display:flex;align-items:center;justify-content:center;min-width:0;position:relative}
#lb img{max-width:100%;max-height:100vh;object-fit:contain}
#lb .info{width:320px;background:var(--bg2);border-left:1px solid var(--bd);overflow:auto;padding:14px}
#lb .nav{position:absolute;top:50%;transform:translateY(-50%);font-size:20px;padding:10px 14px;background:rgba(38,38,38,.8)}
#lb .prev{left:10px}#lb .next{right:10px}
/* messages */
.conv{padding:8px 12px;border-bottom:1px solid var(--bd);cursor:pointer}
.conv:hover{background:var(--bg2)}.conv.sel{background:var(--bg3)}
.conv .r1{display:flex;justify-content:space-between;gap:8px}
.conv .snip{color:var(--fg2);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.msgs{padding:14px 18px;display:flex;flex-direction:column;gap:8px}
.msg{max-width:70%;align-self:flex-start;background:var(--bg2);border:1px solid var(--bd);border-radius:8px;padding:6px 10px;white-space:pre-wrap;word-break:break-word}
.msg.me{align-self:flex-end;background:var(--bg3)}
.msg .tm{color:var(--fg2);font-size:11px;margin-top:2px}
.msg img{max-width:240px;border-radius:4px;display:block;cursor:pointer}
.msg audio{width:260px}
.loading{padding:40px;text-align:center;color:var(--fg2)}
@media (max-width:760px){
  .split{flex-direction:column}.side{width:100%;height:40%;border-right:none;border-bottom:1px solid var(--bd)}
  #lb{flex-direction:column}#lb .info{width:100%;height:40%}
  .msg{max-width:90%}
}
</style>
</head>
<body>
<header>
  <h1>Resonite Export Viewer</h1>
  <select id="owner"></select>
  <nav id="tabs">
    <button data-t="overview">概要</button>
    <button data-t="inventory">インベントリ</button>
    <button data-t="photos">写真</button>
    <button data-t="messages">コンタクト / メッセージ</button>
    <button data-t="variables">変数</button>
    <button data-t="audit">操作履歴</button>
  </nav>
  <span id="status"></span>
</header>
<main>
  <section id="t-overview"><div class="pad" id="ov"></div></section>
  <section id="t-inventory"><div class="split">
    <div class="side"><ul class="tree" id="tree"></ul></div>
    <div class="body">
      <div class="bar">
        <span class="crumb" id="crumb"></span>
        <span style="flex:1"></span>
        <input id="invq" placeholder="インベントリ全体を検索" size="24">
        <select id="invsort"><option value="name">名前順</option><option value="new">新しい順</option><option value="old">古い順</option></select>
      </div>
      <div id="invgrid"></div>
    </div>
  </div></section>
  <section id="t-photos">
    <div class="bar">
      <input id="pq" placeholder="ワールド名 / パスで絞り込み" size="22">
      <input id="pu" placeholder="写っている人 (名前 or ID)" size="20" list="pul">
      <datalist id="pul"></datalist>
      <select id="py"></select>
      <select id="pk"><option value="">すべての種類</option><option value="normal">通常</option><option value="360_photo">360</option><option value="stereo_photo">ステレオ</option></select>
      <select id="ps"><option value="new">新しい順</option><option value="old">古い順</option></select>
      <span class="muted small" id="pcount"></span>
    </div>
    <div id="pgrid"></div>
  </section>
  <section id="t-messages"><div class="split">
    <div class="side">
      <div class="bar" style="padding:8px">
        <input id="cq" placeholder="名前 / ID" style="flex:1;min-width:0">
        <select id="cs"><option value="msg">会話あり</option><option value="">すべて</option><option value="Accepted">フレンド</option><option value="Ignored">Ignored</option></select>
      </div>
      <div id="convs"></div>
    </div>
    <div class="body" id="chat"><div class="loading">左から会話を選択</div></div>
  </div></section>
  <section id="t-variables">
    <div class="bar"><input id="vq" placeholder="パス / 値で絞り込み" size="30"><span class="muted small" id="vcount"></span></div>
    <div class="pad" style="padding-top:0"><table id="vt"></table></div>
  </section>
  <section id="t-audit">
    <div class="bar">
      <input id="aq" placeholder="名前 / パス / レコードID" size="30">
      <select id="aop"><option value="">すべての操作</option></select>
      <span class="muted small" id="acount"></span>
    </div>
    <div class="pad" style="padding-top:0"><table id="at"></table><div id="amore"></div></div>
  </section>
</main>
<div id="drawer"><div class="hd"><b id="dtitle"></b><button id="dclose">閉じる</button></div><div class="ct" id="dbody"></div></div>
<div id="lb">
  <div class="img"><img id="lbimg" alt=""><button class="nav prev" id="lbprev">&lt;</button><button class="nav next" id="lbnext">&gt;</button></div>
  <div class="info" id="lbinfo"></div>
</div>
<script>
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=t=>{if(!t)return'';const d=new Date(t);return isNaN(d)?esc(t):d.toLocaleString('ja-JP',{hour12:false})};
const A=(h,dl)=>'/asset/'+h+(dl?'?dl='+encodeURIComponent(dl):'');
const uh=u=>{const m=/^(?:resdb|neosdb):\/\/\/([0-9a-f]{64})/.exec(u||'');return m?m[1]:null};
const bytes=n=>n==null?'':n<1024?n+' B':n<1048576?(n/1024).toFixed(1)+' KB':n<1073741824?(n/1048576).toFixed(1)+' MB':(n/1073741824).toFixed(2)+' GB';
const store={get(k){try{return localStorage.getItem(k)}catch(e){return null}},set(k,v){try{localStorage.setItem(k,v)}catch(e){}}};
const TYPE_JA={object:'オブジェクト',directory:'フォルダ',link:'リンク',audio:'音声',world:'ワールド',texture:'テクスチャ'};
let S={};

function scroller(el){for(let p=el.parentElement;p;p=p.parentElement){const o=getComputedStyle(p).overflowY;if(o==='auto'||o==='scroll')return p}return null}
/* 一覧を少しずつ描画する (スクロールで追加) */
function chunked(el,list,render,size=150){
  el.innerHTML='';let i=0;
  const sentinel=document.createElement('div');sentinel.className='more';
  const io=new IntersectionObserver(es=>{if(es.some(e=>e.isIntersecting))step()},{root:scroller(el),rootMargin:'600px'});
  function step(){
    const frag=document.createDocumentFragment();
    const end=Math.min(i+size,list.length);
    for(;i<end;i++){const n=render(list[i],i);if(n)frag.appendChild(n)}
    sentinel.remove();el.appendChild(frag);
    if(i<list.length){el.appendChild(sentinel)}else io.disconnect();
  }
  io.observe(sentinel);step();
  el._io&&el._io.disconnect();el._io=io;
}
const h=html=>{const t=document.createElement('template');t.innerHTML=html.trim();return t.content.firstChild};
const uname=id=>S.names[id]||id;
function userChips(ids){return '<div class="chips">'+ids.map(u=>`<span class="chip" data-user="${esc(u)}" title="${esc(u)}">${esc(uname(u))}</span>`).join('')+'</div>'}
document.addEventListener('click',e=>{
  const c=e.target.closest('[data-user]');if(c){closeAll();tab('photos');$('#pu').value=c.dataset.user;renderPhotos();return}
  const f=e.target.closest('[data-folder]');if(f){closeAll();tab('inventory');openFolder(f.dataset.folder);return}
});

/* ---------- 起動 ---------- */
async function init(){
  const owners=await (await fetch('/api/owners')).json();
  $('#owner').innerHTML=owners.map(o=>`<option value="${esc(o.id)}">${esc(o.name)} (${esc(o.id)})</option>`).join('');
  $('#owner').style.display=owners.length>1?'':'none';
  $('#owner').onchange=()=>load($('#owner').value);
  document.querySelectorAll('#tabs button').forEach(b=>b.onclick=()=>tab(b.dataset.t));
  if(owners.length)load(owners[0].id);else $('#status').textContent='データが見つかりません';
}
async function load(owner){
  $('#status').textContent='読み込み中...';
  document.querySelectorAll('section .pad,#invgrid,#pgrid,#convs').forEach(e=>{});
  const d=await (await fetch('/api/data?owner='+encodeURIComponent(owner))).json();
  S={owner,d,names:{},rendered:{}};
  for(const c of d.contacts)if(c.n)S.names[c.id]=c.n;
  if(d.user)S.names[d.user.id]=d.user.username;
  S.byId=new Map(d.records.map(r=>[r.id,r]));
  $('#status').textContent=`レコード ${d.records.length.toLocaleString()} / アセット ${d.assetCount.toLocaleString()}`;
  tab(location.hash.slice(1)||store.get('tab')||'overview');
}
function tab(t){
  document.querySelectorAll('#tabs button').forEach(b=>b.classList.toggle('on',b.dataset.t===t));
  document.querySelectorAll('main section').forEach(s=>s.classList.toggle('on',s.id==='t-'+t));
  store.set('tab',t);history.replaceState(null,'','#'+t);
  if(!S.d||S.rendered[t])return;S.rendered[t]=1;
  ({overview:renderOverview,inventory:initInventory,photos:initPhotos,messages:initMessages,variables:initVariables,audit:initAudit})[t]();
}

/* ---------- 概要 ---------- */
function renderOverview(){
  const d=S.d,u=d.user||{};
  const recs=d.records,photos=recs.filter(r=>r.ph).length;
  const types={};for(const r of recs)types[r.t]=(types[r.t]||0)+1;
  const msgs=d.convs.reduce((a,c)=>a+c.c,0);
  const icon=uh(u.profile&&u.profile.iconUrl);
  let html=`<div style="display:flex;gap:14px;align-items:center">
    ${icon?`<img src="${A(icon)}" style="width:64px;height:64px;border-radius:6px;object-fit:cover" alt="">`:''}
    <div><div style="font-size:20px;font-weight:600">${esc(u.username||S.owner)}</div>
    <div class="muted small">${esc(S.owner)}${u.registrationDate?' / 登録 '+fmt(u.registrationDate):''}</div></div></div>
  <div class="tiles">
    ${[['レコード',recs.length],['フォルダ',types.directory||0],['写真',photos],['コンタクト',d.contacts.length],['会話',d.convs.length],['メッセージ',msgs],['変数',d.variables.length],['操作履歴',d.audit.length],['アセットファイル',d.assetCount]]
      .map(([k,v])=>`<div class="tile"><b>${v.toLocaleString()}</b><span>${k}</span></div>`).join('')}
  </div>
  <h2>レコード種別</h2><table style="max-width:420px">${Object.entries(types).sort((a,b)=>b[1]-a[1]).map(([k,v])=>`<tr><td>${esc(TYPE_JA[k]||k)}</td><td class="muted">${esc(k)}</td><td style="text-align:right">${v.toLocaleString()}</td></tr>`).join('')}</table>`;
  if(u.tags&&u.tags.length)html+=`<h2>ユーザータグ</h2><div class="chips">${u.tags.map(t=>`<span class="chip">${esc(t)}</span>`).join('')}</div>`;
  if(d.patreon.length){
    const ps=[...d.patreon].sort((a,b)=>(a.paymentTime||'').localeCompare(b.paymentTime||''));
    html+=`<h2>Patreon 支払い履歴</h2><table><tr><th>支払日</th><th>金額</th><th>有効期間</th><th>追加ストレージ</th><th></th></tr>${ps.map(p=>{
      const st=(p.entitlements||[]).filter(e=>e.$type==='storageSpace').reduce((a,e)=>a+(e.bytes||0),0);
      return `<tr><td>${fmt(p.paymentTime)}</td><td>${esc(p.amount)} ${esc(p.currency)}</td><td>${fmt(p.validityStart)} - ${fmt(p.validityEnd)}</td><td>${bytes(st)}</td><td>${p.isInvalidated?'<span class="badge b-red">無効</span>':''}</td></tr>`}).join('')}</table>`;
  }
  if(d.user)html+=`<h2>ユーザー情報 (User.json)</h2><table class="kv">${Object.entries(d.user).map(([k,v])=>`<tr><th>${esc(k)}</th><td>${v!==null&&typeof v==='object'?`<pre>${esc(JSON.stringify(v,null,2))}</pre>`:esc(v)}</td></tr>`).join('')}</table>`;
  $('#ov').innerHTML=html;
}

/* ---------- インベントリ ---------- */
function initInventory(){
  const kids=new Map([['',new Set()]]),items=new Map(),cnt=new Map();
  const add=p=>{if(kids.has(p))return;kids.set(p,new Set());const i=p.lastIndexOf('/');const par=i<0?'':p.slice(0,i);add(par);kids.get(par).add(p)};
  for(const r of S.d.records){
    if(r.t==='directory'){add(r.p?r.p+'/'+r.n:r.n);continue}
    const p=r.p||'(パスなし)';add(p);
    if(!items.has(p))items.set(p,[]);items.get(p).push(r);
    let q=p;while(true){cnt.set(q,(cnt.get(q)||0)+1);const i=q.lastIndexOf('/');if(i<0)break;q=q.slice(0,i)}
  }
  S.inv={kids,items,cnt,open:new Set(['Inventory']),cur:null};
  $('#invq').oninput=debounce(()=>showFolder(),250);
  $('#invsort').onchange=()=>showFolder();
  const first=kids.has('Inventory')?'Inventory':[...kids.get('')][0];
  openFolder(first||'');
}
function openFolder(p){
  if(!S.inv){S.rendered.inventory=1;initInventory()}
  let q=p;while(q){S.inv.open.add(q);const i=q.lastIndexOf('/');if(i<0)break;q=q.slice(0,i)}
  S.inv.cur=p;$('#invq').value='';renderTree();showFolder();
  const sel=document.querySelector('#tree .node.sel');sel&&sel.scrollIntoView({block:'nearest'});
}
function renderTree(){
  const {kids,cnt,open,cur}=S.inv;
  const sorted=s=>[...s].sort((a,b)=>a.localeCompare(b,'ja'));
  const build=p=>{
    const name=p.slice(p.lastIndexOf('/')+1),ch=kids.get(p)||new Set(),isOpen=open.has(p);
    let s=`<li><div class="node${p===cur?' sel':''}" data-p="${esc(p)}"><span class="caret">${ch.size?(isOpen?'v':'>'):''}</span><span class="nm">${esc(name)}</span><span class="ct">${(cnt.get(p)||0).toLocaleString()}</span></div>`;
    if(ch.size&&isOpen)s+='<ul>'+sorted(ch).map(build).join('')+'</ul>';
    return s+'</li>';
  };
  $('#tree').innerHTML=sorted(kids.get('')).map(build).join('');
  $('#tree').onclick=e=>{
    const n=e.target.closest('.node');if(!n)return;const p=n.dataset.p;
    if(e.target.classList.contains('caret')){open.has(p)?open.delete(p):open.add(p);renderTree();return}
    S.inv.cur=p;open.add(p);$('#invq').value='';renderTree();showFolder();
  };
}
function sortRecs(list){
  const m=$('#invsort').value;
  if(m==='name')return list.sort((a,b)=>a.n.localeCompare(b.n,'ja'));
  const k=r=>(r.m&&r.m.timestamp)||r.ct||'';
  return list.sort((a,b)=>m==='new'?k(b).localeCompare(k(a)):k(a).localeCompare(k(b)));
}
function showFolder(){
  const {kids,items,cnt,cur}=S.inv,q=$('#invq').value.trim().toLowerCase();
  let list;
  if(q){
    $('#crumb').innerHTML=`<span class="muted">検索: </span>${esc(q)}`;
    list=S.d.records.filter(r=>r.t!=='directory'&&(r.n.toLowerCase().includes(q)||r.id.toLowerCase()===q||(r.tg||[]).some(t=>t.toLowerCase().includes(q))||(r.m&&(r.m.location_name||'').toLowerCase().includes(q))));
    list=sortRecs(list);
  }else{
    const parts=cur.split('/');
    $('#crumb').innerHTML=parts.map((s,i)=>`<a data-folder="${esc(parts.slice(0,i+1).join('/'))}">${esc(s)}</a>`).join(' <span class="muted">/</span> ');
    const subs=[...(kids.get(cur)||[])].sort((a,b)=>a.localeCompare(b,'ja')).map(p=>({folder:p}));
    list=[...subs,...sortRecs([...(items.get(cur)||[])])];
  }
  const g=$('#invgrid');g.className='grid';
  chunked(g,list,x=>{
    if(x.folder){const n=x.folder.slice(x.folder.lastIndexOf('/')+1);
      return h(`<div class="card folder" data-folder="${esc(x.folder)}"><div class="thumb">${esc(n)}</div><div class="cap muted">${(cnt.get(x.folder)||0).toLocaleString()} 件</div></div>`)}
    const el=card(x,q?x.p:null);el.onclick=()=>openDetail(x);return el;
  });
  if(!list.length)g.innerHTML='<div class="loading">なし</div>';
}
function card(r,sub){
  const img=r.th||(r.ak==='image'?r.a:null);
  const label=r.ak&&r.ak!=='image'?r.ak:(TYPE_JA[r.t]||r.t);
  return h(`<div class="card"><div class="thumb">${img?`<img loading="lazy" src="${A(img)}" alt="">`:esc(label)}</div>
    <div class="cap"><div class="t" title="${esc(r.n)}">${esc(r.n)||'<span class=muted>(無題)</span>'}</div>
    <div class="muted small t">${sub?esc(sub):esc(TYPE_JA[r.t]||r.t)}${r.pub?' / 公開':''}</div></div></div>`);
}
function debounce(f,ms){let t;return(...a)=>{clearTimeout(t);t=setTimeout(()=>f(...a),ms)}}

/* ---------- 詳細パネル ---------- */
async function openDetail(r){
  $('#dtitle').textContent=r.n||'(無題)';$('#drawer').classList.add('on');
  $('#dbody').innerHTML='<div class="loading">読み込み中...</div>';
  const res=await fetch(`/api/record?owner=${encodeURIComponent(S.owner)}&id=${encodeURIComponent(r.id)}`);
  const x=res.ok?await res.json():{rec:null,manifest:[]};
  let pv='';
  if(r.a&&r.ak==='image')pv=`<a href="${A(r.a)}" target="_blank"><img src="${A(r.a)}" alt=""></a>`;
  else if(r.a&&r.ak==='audio')pv=`<audio controls preload="none" src="${A(r.a)}"></audio>`;
  else if(r.a&&r.ak==='video')pv=`<video controls preload="metadata" src="${A(r.a)}"></video>`;
  else if(r.th)pv=`<img src="${A(r.th)}" alt="">`;
  const m=r.m||{},rows=[['種類',`${esc(TYPE_JA[r.t]||r.t)} <span class="muted">${esc(r.t)}</span>`],['パス',r.p?`<a data-folder="${esc(r.p)}">${esc(r.p)}</a>`:'<span class="muted">なし</span>'],
    ['作成',fmt(r.ct)],['更新',fmt(r.mt)]];
  if(m.timestamp)rows.push(['撮影',fmt(m.timestamp)]);
  if(m.location_name!=null)rows.push(['ワールド',esc(m.location_name)]);
  if(m.location_host)rows.push(['ホスト',esc(uname(m.location_host))]);
  if(m.location_accesslevel)rows.push(['アクセス',esc(m.location_accesslevel)]);
  if(m.world_url)rows.push(['ワールドURL',esc(m.world_url)]);
  if(r.au)rows.push(['参照先',esc(r.au)]);
  if(r.d)rows.push(['説明',esc(r.d)]);
  if(r.f&&r.f.length)rows.push(['写真種別',r.f.map(esc).join(', ')]);
  rows.push(['公開',r.pub?'公開':'非公開'],['ID',`<span class="small">${esc(r.id)}</span>`]);
  if(r.u&&r.u.length)rows.push([`写っている人 (${r.u.length})`,userChips(r.u)]);
  if(r.tg&&r.tg.length)rows.push(['タグ',`<div class="chips">${r.tg.map(t=>`<span class="chip">${esc(t)}</span>`).join('')}</div>`]);
  const dl=r.a?`<p><a href="${A(r.a)}" target="_blank">本体を開く</a> / <a href="${A(r.a,r.n)}">ダウンロード (.${esc(r.ax)})</a></p>`:'';
  const man=x.manifest.length?`<h2>アセット (${x.manifest.length})</h2><table>${x.manifest.map(a=>`<tr><td class="small">${esc(a.hash.slice(0,16))}...</td><td>${esc(a.ext||'')}</td><td style="text-align:right">${bytes(a.bytes)}</td><td>${a.exists?`<a href="${A(a.hash,r.n)}">DL</a>`:'<span class="badge b-red">欠損</span>'}</td></tr>`).join('')}</table>`:'';
  $('#dbody').innerHTML=`<div class="preview">${pv}</div>${dl}<table class="kv">${rows.map(([k,v])=>`<tr><th>${k}</th><td>${v}</td></tr>`).join('')}</table>${man}
    ${x.rec?`<details><summary class="muted" style="cursor:pointer;margin-top:14px">生データ (JSON)</summary><pre>${esc(JSON.stringify(x.rec,null,2))}</pre></details>`:''}`;
}
function closeAll(){$('#drawer').classList.remove('on');$('#lb').classList.remove('on')}
$('#dclose').onclick=closeAll;

/* ---------- 写真 ---------- */
function initPhotos(){
  const ps=S.d.records.filter(r=>r.ph);
  for(const r of ps)r._t=(r.m&&r.m.timestamp)||r.ct||'';
  S.photos=ps;
  const years=[...new Set(ps.map(r=>r._t.slice(0,4)))].sort().reverse();
  $('#py').innerHTML='<option value="">すべての年</option>'+years.map(y=>`<option>${esc(y)}</option>`).join('');
  const us=new Map();for(const r of ps)for(const u of r.u||[])us.set(u,(us.get(u)||0)+1);
  $('#pul').innerHTML=[...us].sort((a,b)=>b[1]-a[1]).map(([u,n])=>`<option value="${esc(u)}">${esc(uname(u))} (${n})</option>`).join('');
  ['#pq','#pu'].forEach(s=>$(s).oninput=debounce(renderPhotos,250));
  ['#py','#pk','#ps'].forEach(s=>$(s).onchange=renderPhotos);
  renderPhotos();
}
function renderPhotos(){
  if(!S.photos){S.rendered.photos=1;initPhotos();return}
  const q=$('#pq').value.trim().toLowerCase(),uq=$('#pu').value.trim().toLowerCase(),y=$('#py').value,k=$('#pk').value;
  let list=S.photos.filter(r=>{
    if(y&&!r._t.startsWith(y))return false;
    if(k==='normal'?(r.f&&r.f.length):k&&!(r.f||[]).includes(k))return false;
    if(q&&!((r.m&&r.m.location_name||'').toLowerCase().includes(q)||r.p.toLowerCase().includes(q)||r.n.toLowerCase().includes(q)))return false;
    if(uq&&!(r.u||[]).some(u=>u.toLowerCase()===uq||uname(u).toLowerCase().includes(uq)))return false;
    return true;
  });
  list.sort((a,b)=>$('#ps').value==='new'?b._t.localeCompare(a._t):a._t.localeCompare(b._t));
  S.plist=list;$('#pcount').textContent=list.length.toLocaleString()+' 枚';
  const g=$('#pgrid');g.className='grid';let last=null;
  chunked(g,list,(r,i)=>{
    const frag=document.createDocumentFragment(),mo=r._t.slice(0,7);
    if(mo!==last){last=mo;frag.appendChild(h(`<div class="ghead">${esc(mo.replace('-','年'))}月</div>`))}
    const img=r.th||r.a;
    const el=h(`<div class="card"><div class="thumb">${img?`<img loading="lazy" src="${A(img)}" alt="">`:'画像なし'}</div>
      <div class="cap"><div class="t">${esc((r.m&&r.m.location_name)||r.n)}</div><div class="muted small">${fmt(r._t)}${(r.f||[]).map(f=>' <span class="badge">'+esc(f.replace('_photo',''))+'</span>').join('')}</div></div></div>`);
    el.onclick=()=>openLb(i);frag.appendChild(el);return frag;
  },120);
  if(!list.length)g.innerHTML='<div class="loading">該当なし</div>';
}
function openLb(i){
  S.lbi=i;const r=S.plist[i];if(!r)return;
  const full=r.a&&r.ak==='image'?r.a:r.th;
  $('#lbimg').src=full?A(full):'';$('#lb').classList.add('on');
  const m=r.m||{};
  $('#lbinfo').innerHTML=`<div style="display:flex;justify-content:space-between"><b>${esc(m.location_name||r.n)}</b><button onclick="closeAll()">閉じる</button></div>
    <p class="muted small">${i+1} / ${S.plist.length}</p>
    <table class="kv"><tr><th>撮影</th><td>${fmt(r._t)}</td></tr>
    ${m.location_host?`<tr><th>ホスト</th><td>${esc(uname(m.location_host))}</td></tr>`:''}
    ${m.location_accesslevel?`<tr><th>アクセス</th><td>${esc(m.location_accesslevel)}</td></tr>`:''}
    <tr><th>フォルダ</th><td><a data-folder="${esc(r.p)}">${esc(r.p)}</a></td></tr>
    ${r.a&&r.ak==='image'?'':'<tr><th>注意</th><td><span class="badge b-yellow">本体なし (サムネイル表示)</span></td></tr>'}</table>
    ${r.u&&r.u.length?`<h2>写っている人 (${r.u.length})</h2>${userChips(r.u)}`:''}
    <p>${full?`<a href="${A(full)}" target="_blank">原寸で開く</a> / <a href="${A(full,(m.location_name||r.n)+'_'+r._t.slice(0,19).replace(/[:T]/g,'-'))}">ダウンロード</a>`:''}</p>
    <p><a href="#" id="lbdetail">詳細情報</a></p>`;
  $('#lbdetail').onclick=e=>{e.preventDefault();openDetail(r)};
}
$('#lbprev').onclick=()=>openLb(S.lbi-1);$('#lbnext').onclick=()=>openLb(S.lbi+1);
$('#lb .img').onclick=e=>{if(e.target===e.currentTarget)closeAll()};
document.addEventListener('keydown',e=>{
  if(e.key==='Escape')closeAll();
  if($('#lb').classList.contains('on')){if(e.key==='ArrowLeft')openLb(S.lbi-1);if(e.key==='ArrowRight')openLb(S.lbi+1)}
});

/* ---------- コンタクト / メッセージ ---------- */
function initMessages(){
  const m=new Map();
  for(const c of S.d.contacts)m.set(c.id,{id:c.id,n:c.n,s:c.s,icon:c.icon,last:c.last||'',c:0});
  for(const v of S.d.convs){const x=m.get(v.id)||{id:v.id,n:null,s:null,last:''};x.c=v.c;x.snip=v.snip;if(v.last>x.last)x.last=v.last;m.set(v.id,x)}
  S.conv=[...m.values()].sort((a,b)=>(b.last||'').localeCompare(a.last||''));
  $('#cq').oninput=debounce(renderConvs,200);$('#cs').onchange=renderConvs;renderConvs();
}
function renderConvs(){
  const q=$('#cq').value.trim().toLowerCase(),st=$('#cs').value;
  const list=S.conv.filter(c=>(st==='msg'?c.c>0:!st||c.s===st)&&(!q||c.id.toLowerCase().includes(q)||(c.n||'').toLowerCase().includes(q)));
  chunked($('#convs'),list,c=>{
    const el=h(`<div class="conv" data-c="${esc(c.id)}"><div class="r1"><span>${esc(c.n||c.id)}</span><span class="muted small">${c.c?c.c+'件':''}</span></div>
      <div class="snip">${c.s?`<span class="badge ${c.s==='Accepted'?'b-green':'b-red'}">${esc(c.s)}</span> `:''}${esc(c.snip||'')}</div>
      <div class="muted small">${fmt(c.last)}</div></div>`);
    el.onclick=()=>openConv(c,el);return el;
  },200);
}
async function openConv(c,el){
  document.querySelectorAll('.conv.sel').forEach(e=>e.classList.remove('sel'));el&&el.classList.add('sel');
  const chat=$('#chat');
  const head=`<div class="bar"><b>${esc(c.n||c.id)}</b><span class="muted small">${esc(c.id)}</span>${c.s?`<span class="badge">${esc(c.s)}</span>`:''}
    <a class="small" data-user="${esc(c.id)}">一緒に写っている写真</a></div>`;
  if(!c.c){chat.innerHTML=head+'<div class="loading">メッセージなし</div>';return}
  chat.innerHTML=head+'<div class="loading">読み込み中...</div>';
  const ms=await (await fetch(`/api/messages?owner=${encodeURIComponent(S.owner)}&cid=${encodeURIComponent(c.id)}`)).json();
  chat.innerHTML=head+'<div class="msgs">'+ms.map(m=>`<div class="msg${m.s===S.owner?' me':''}">${msgBody(m)}<div class="tm">${m.s!==S.owner?esc(uname(m.s))+' / ':''}${fmt(m.st)}</div></div>`).join('')+'</div>';
  chat.scrollTop=chat.scrollHeight;
}
function msgBody(m){
  const c=m.c;
  if(m.t==='Text')return esc(c);
  if(c&&typeof c==='object'){
    if(m.t==='Sound'){const a=uh(c.assetUri);return a?`<audio controls preload="none" src="${A(a)}"></audio>`:'[音声: アセットなし]'}
    if(m.t==='Object'){const th=uh(c.thumbnailUri);const tex=(c.tags||[]).map(t=>t.startsWith('texture_asset:')?uh(t.slice(14)):null).find(Boolean);
      const big=tex||th;return `${big?`<a href="${A(big)}" target="_blank"><img loading="lazy" src="${A(th||big)}" alt=""></a>`:''}<span class="small">${esc(c.name||'オブジェクト')}</span>`}
    if(m.t==='SessionInvite')return `<span class="badge b-blue">セッション招待</span> ${esc(c.name)}<div class="small muted">host: ${esc(c.hostUsername||c.hostUserId||'')}</div>`;
    if(m.t==='InviteRequest')return `<span class="badge b-yellow">招待リクエスト</span> ${esc(c.requestingFromUsername||c.requestingFromUserId||'')}${c.forSessionName?' → '+esc(c.forSessionName):''}`;
  }
  return `<span class="badge">${esc(m.t)}</span> <span class="small">${esc(typeof c==='string'?c:JSON.stringify(c)).slice(0,300)}</span>`;
}

/* ---------- 変数 ---------- */
function initVariables(){$('#vq').oninput=debounce(renderVariables,200);renderVariables()}
function renderVariables(){
  const q=$('#vq').value.trim().toLowerCase();
  const list=S.d.variables.filter(v=>!q||(v.p||'').toLowerCase().includes(q)||String(v.v).toLowerCase().includes(q)).sort((a,b)=>(a.p||'').localeCompare(b.p||''));
  $('#vcount').textContent=list.length+' 件';
  $('#vt').innerHTML='<tr><th>パス</th><th>値</th><th>更新</th></tr>'+list.map(v=>`<tr><td>${esc(v.p)}</td><td>${esc(v.v)}</td><td class="muted small" style="white-space:nowrap">${fmt(v.ts)}</td></tr>`).join('');
}

/* ---------- 操作履歴 ---------- */
function initAudit(){
  const ops=[...new Set(S.d.audit.map(a=>a.op))].sort();
  $('#aop').innerHTML='<option value="">すべての操作</option>'+ops.map(o=>`<option>${esc(o)}</option>`).join('');
  $('#aq').oninput=debounce(renderAudit,200);$('#aop').onchange=renderAudit;renderAudit();
}
function renderAudit(){
  const q=$('#aq').value.trim().toLowerCase(),op=$('#aop').value;
  const list=S.d.audit.filter(a=>(!op||a.op===op)&&(!q||[a.n,a.p,a.rid].some(x=>(x||'').toLowerCase().includes(q)))).sort((a,b)=>(b.ts||'').localeCompare(a.ts||''));
  $('#acount').textContent=list.length.toLocaleString()+' 件';
  let n=0;const t=$('#at');
  const more=()=>{
    const part=list.slice(n,n+300);n+=part.length;
    t.insertAdjacentHTML('beforeend',part.map(a=>{const r=S.byId.get(a.rid);
      return `<tr><td class="small" style="white-space:nowrap">${fmt(a.ts)}</td><td><span class="badge ${a.op==='Delete'?'b-red':''}">${esc(a.op)}</span></td>
      <td>${r?`<a href="#" data-rid="${esc(a.rid)}">${esc(a.n)}</a>`:esc(a.n)}</td><td class="small">${esc(a.p)}</td><td class="small muted">${esc(a.src||'')}</td></tr>`}).join(''));
    $('#amore').innerHTML=n<list.length?`<p><button id="amorebtn">さらに表示 (残り ${(list.length-n).toLocaleString()})</button></p>`:'';
    if(n<list.length)$('#amorebtn').onclick=more;
  };
  t.innerHTML='<tr><th>日時</th><th>操作</th><th>名前</th><th>元のパス</th><th>送信元</th></tr>';more();
  t.onclick=e=>{const a=e.target.closest('[data-rid]');if(a){e.preventDefault();openDetail(S.byId.get(a.dataset.rid))}};
}
init();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
