#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 kazu0617
# SPDX-License-Identifier: 0BSD
"""Resonite のダウンロードデータ (Records + Assets) からインベントリのフォルダ構造を再現する。

使い方:
    python3 rebuild_photos.py SRC_DIR OUT_DIR [--folders Photos_VR Photos-Root] [--dry-run]

    --folders: Inventory 直下の対象フォルダ名 (アカウントごとに名前が違うので必要に応じて変更)
    再実行時は同サイズの既存ファイルをスキップする

    SRC_DIR: ResoniteDownload ディレクトリ (U-xxx/Records と Assets を含む)
    OUT_DIR: 出力先 (例: /mnt/data/ResonitePhotos)

画像実体の解決順:
    1. tags の texture_asset:resdb:///<hash>
    2. assetManifest のうちオブジェクト本体・サムネイル以外で最大の画像
    3. サムネイル (ファイル名末尾に _thumb を付ける)
"""
import argparse
import csv
import json
import os
import re
import shutil
import sys
from datetime import datetime

NTFS_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize(name, fallback="_"):
    name = NTFS_BAD.sub("_", name or "").strip().rstrip(". ")
    return name or fallback


def longpath(path):
    """Windows の 260 文字制限を回避する。"""
    if os.name == "nt":
        path = os.path.abspath(path)
        if not path.startswith("\\\\?\\"):
            return "\\\\?\\" + path
    return path


def image_ext(path):
    with open(path, "rb") as f:
        b = f.read(16)
    if b[:3] == b"\xff\xd8\xff":
        return "jpg"
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "webp"
    return None


def resdb_hash(uri):
    if not uri or not uri.startswith("resdb:///"):
        return None, None
    h, _, ext = uri[len("resdb:///"):].partition(".")
    return h, ext.lower() or None


def resolve_image(rec, assets):
    """(asset_path, ext, source) を返す。見つからなければ None。"""
    for tag in rec.get("tags") or []:
        if tag.startswith("texture_asset:"):
            h, ext = resdb_hash(tag[len("texture_asset:"):])
            if h and os.path.exists(os.path.join(assets, h)):
                p = os.path.join(assets, h)
                return p, image_ext(p) or ext or "bin", "texture"
            break
    obj, _ = resdb_hash(rec.get("assetUri"))
    thumb, _ = resdb_hash(rec.get("thumbnailUri"))
    cands = sorted((m for m in rec.get("assetManifest") or [] if m["hash"] not in (obj, thumb)),
                   key=lambda m: -m["bytes"])
    for m in cands:
        p = os.path.join(assets, m["hash"])
        if os.path.exists(p) and image_ext(p):
            return p, image_ext(p), "manifest"
    if thumb and os.path.exists(os.path.join(assets, thumb)):
        p = os.path.join(assets, thumb)
        return p, image_ext(p) or "webp", "thumbnail"
    return None


def photo_time(rec):
    for tag in rec.get("tags") or []:
        if tag.startswith("timestamp:"):
            try:
                return datetime.fromisoformat(tag[len("timestamp:"):][:26].rstrip("Z") + "+00:00").astimezone()
            except ValueError:
                pass
    return datetime.fromisoformat(rec["creationTime"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--folders", nargs="+", default=["Photos_VR", "Photos-Root"])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    a.out = os.path.abspath(a.out)

    owners = [d for d in os.listdir(a.src) if re.match(r"^[UG]-", d) and os.path.isdir(os.path.join(a.src, d))]
    assets = os.path.join(a.src, "Assets")
    prefixes = tuple("Inventory\\" + f for f in a.folders)

    def in_scope(path):
        return any(path == p or path.startswith(p + "\\") for p in prefixes)

    def local_dir(path):
        parts = path.split("\\")[1:]  # "Inventory" を除く
        return os.path.join(a.out, *(sanitize(p) for p in parts))

    dirs, photos = set(), []
    for u in owners:
        rdir = os.path.join(a.src, u, "Records")
        if not os.path.isdir(rdir):
            continue
        for fn in os.listdir(rdir):
            with open(os.path.join(rdir, fn), encoding="utf-8") as f:
                rec = json.load(f)
            if rec.get("isDeleted"):
                continue
            path = rec.get("path") or ""
            if rec["recordType"] == "directory":
                full = path + "\\" + rec["name"]
                if in_scope(full):
                    dirs.add(full)
            elif rec["recordType"] == "object" and in_scope(path):
                photos.append(rec)

    photos.sort(key=lambda r: (r["path"], photo_time(r), r["id"]))
    used, rows, total = set(), [], 0
    stats = {"texture": 0, "manifest": 0, "thumbnail": 0, "missing": 0, "skipped": 0}

    for d in sorted(dirs):
        if not a.dry_run:
            os.makedirs(longpath(local_dir(d)), exist_ok=True)

    for rec in photos:
        res = resolve_image(rec, assets)
        if not res:
            stats["missing"] += 1
            rows.append([rec["id"], rec["path"], rec["name"], "", "missing", ""])
            continue
        src, ext, source = res
        t = photo_time(rec)
        base = f"{t:%Y-%m-%d_%H%M%S}_{sanitize(rec['name'], 'Photo')}"[:100]
        if source == "thumbnail":
            base += "_thumb"
        dst = os.path.join(local_dir(rec["path"]), f"{base}.{ext}")
        if dst.lower() in used:
            dst = os.path.join(local_dir(rec["path"]), f"{base}_{rec['id'][2:10]}.{ext}")
        used.add(dst.lower())
        stats[source] += 1
        size = os.path.getsize(src)
        rows.append([rec["id"], rec["path"], rec["name"], os.path.relpath(dst, a.out), source, os.path.basename(src)])
        if a.dry_run:
            total += size
            continue
        if os.path.exists(longpath(dst)) and os.path.getsize(longpath(dst)) == size:
            stats["skipped"] += 1
            continue
        total += size
        os.makedirs(longpath(os.path.dirname(dst)), exist_ok=True)
        shutil.copyfile(src, longpath(dst))
        ts = t.timestamp()
        os.utime(longpath(dst), (ts, ts))

    if not a.dry_run:
        with open(os.path.join(a.out, "_index.csv"), "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["record_id", "inventory_path", "name", "file", "source", "asset_hash"])
            w.writerows(rows)

    print(f"dirs={len(dirs)} photos={len(photos)} {stats} bytes={total / 1e9:.2f}GB", file=sys.stderr)


if __name__ == "__main__":
    main()
