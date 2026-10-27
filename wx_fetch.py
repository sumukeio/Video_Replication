"""
视频号对标抓取 (wx_fetch.py)
支持两种形态：普通视频（mediaType=4）和图集/图片轮播（mediaType=2）。

图集是 wx-downloader 没实现的部分 —— 它遇到图集会直接报「暂不支持图集或直播」，
但元宝解析接口其实返回了完整的图片直链，本脚本把它接出来。

依赖 erchuang 项目的虚拟环境（复用其元宝登录凭据与 httpx）：

  E:/AIProject/erchuang/.venv/Scripts/python.exe wx_fetch.py <链接...> [-o 输出根目录]

输出：
  <root>/<作者>_<标题前20字>/
      meta.json          作者、标题、mediaType、图片数
      images/01.jpg ...  图集原图（原分辨率）
      video.mp4          普通视频原片
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from wx_channels_cli.auth import load_auth
from wx_channels_cli.inputs import normalize_url

ORIGIN = "https://yuanbao.tencent.com"
PARSE_URL = ORIGIN + "/api/weixin/get_parse_result"
PREVIEW = "https://channels.weixin.qq.com/finder-preview"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 MicroMessenger/8.0.50")


def safe(text: str, limit: int = 24) -> str:
    t = re.sub(r'[\\/:*?"<>|\r\n\t#]', "", text or "").strip()
    t = re.sub(r"\s+", " ", t)
    return (t[:limit].strip(" ._") or "未命名")


def auth_headers(auth: dict) -> dict:
    h = {
        "User-Agent": auth.get("user_agent") or UA,
        "Origin": ORIGIN,
        "Referer": ORIGIN + "/",
        "Accept": "application/json",
        "x-source": "web",
        "Cookie": auth["cookie"],
    }
    for k, v in auth.get("headers", {}).items():
        if k in ("authorization", "t-userid", "x-id", "x-device-id", "x-hy92", "x-hy93"):
            h[k] = v
    return h


def resolve(http: httpx.Client, auth: dict, source: str) -> dict:
    headers = auth_headers(auth)
    r = http.post(PARSE_URL, headers=headers,
                  json={"type": "video_channel_url", "url": source, "scene": 1})
    res = r.json()
    if res.get("code") != 0:
        raise RuntimeError(f"元宝解析失败 code={res.get('code')}")

    parsed = res.get("data") or {}
    purl = parsed.get("playable_url")
    if not purl:
        raise RuntimeError("元宝未返回预览地址")

    q = parse_qs(urlsplit(purl).query)
    if "token" not in q or "eid" not in q:
        raise RuntimeError("预览地址缺少 token/eid")

    params = {"_rid": f"{int(time.time()):x}-{secrets.token_hex(4)}",
              "_pageUrl": PREVIEW + "/pages/feed"}
    r2 = http.post(PREVIEW + "/api/feed/get_feed_info?" + urlencode(params),
                   headers={"User-Agent": headers["User-Agent"],
                            "Origin": "https://channels.weixin.qq.com",
                            "Referer": purl},
                   json={"baseReq": {"generalToken": q["token"][0]},
                         "exportId": q["eid"][0]})
    prof = r2.json()
    data = prof.get("data") or {}
    feed = data.get("feedInfo") or {}
    if prof.get("errCode", 0) != 0 or not feed:
        raise RuntimeError("视频号详情获取失败，内容可能已删除或不可见")

    return {
        "author": (data.get("authorInfo") or {}).get("nickname") or parsed.get("author") or "",
        "desc": feed.get("description") or parsed.get("desc") or "",
        "mediaType": feed.get("mediaType"),
        "pics": [p.get("url") for p in (feed.get("picInfo") or []) if p.get("url")],
        "video": (feed.get("h264VideoInfo") or {}).get("videoUrl")
                 or feed.get("videoUrl")
                 or (feed.get("h265VideoInfo") or {}).get("videoUrl"),
        "raw": feed,
    }


def stream_to(http: httpx.Client, url: str, dst: Path, referer: str) -> int:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    with http.stream("GET", url,
                     headers={"User-Agent": UA, "Referer": referer, "Accept": "*/*"}) as r:
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        with tmp.open("wb") as fh:
            for chunk in r.iter_bytes(256 * 1024):
                fh.write(chunk)
    tmp.replace(dst)
    return dst.stat().st_size


def fetch_one(http: httpx.Client, auth: dict, link: str, root: Path) -> dict:
    info = resolve(http, auth, link)
    folder = root / f"{safe(info['author'], 16)}_{safe(info['desc'], 22)}"
    folder.mkdir(parents=True, exist_ok=True)

    mt = info["mediaType"]
    result = {"link": link, "author": info["author"], "desc": info["desc"],
              "mediaType": mt, "folder": str(folder), "count": 0, "bytes": 0}

    if mt == 2 and info["pics"]:
        result["kind"] = "图集"
        for i, u in enumerate(info["pics"], 1):
            dst = folder / "images" / f"{i:02d}.jpg"
            try:
                result["bytes"] += stream_to(http, u, dst, "https://channels.weixin.qq.com/")
                result["count"] += 1
            except Exception as e:
                print(f"    第 {i} 张失败: {e}")
    elif info["video"]:
        result["kind"] = "视频"
        dst = folder / "video.mp4"
        result["bytes"] = stream_to(http, info["video"], dst, "https://channels.weixin.qq.com/")
        result["count"] = 1
    else:
        raise RuntimeError(f"未识别的类型 mediaType={mt}")

    (folder / "meta.json").write_text(json.dumps({
        "link": link, "author": info["author"], "desc": info["desc"],
        "mediaType": mt, "kind": result["kind"],
        "pic_count": len(info["pics"]),
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="抓取视频号视频或图集")
    ap.add_argument("links", nargs="+", help="分享链接")
    ap.add_argument("-o", "--out", default="./对标", help="输出根目录")
    args = ap.parse_args(argv)

    root = Path(args.out).resolve()
    root.mkdir(parents=True, exist_ok=True)

    try:
        auth = load_auth()
    except Exception as e:
        print(f"读取登录凭据失败：{e}\n请先运行 wx-downloader login", file=sys.stderr)
        return 1

    ok = 0
    with httpx.Client(timeout=90, follow_redirects=True) as http:
        for link in args.links:
            print(f"[抓取] {link}")
            try:
                r = fetch_one(http, auth, link, root)
            except Exception as e:
                print(f"  [失败] {e}", file=sys.stderr)
                continue
            ok += 1
            print(f"  {r['kind']}  {r['count']} 个  {r['bytes'] / 1048576:.1f} MB  →  {r['folder']}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
