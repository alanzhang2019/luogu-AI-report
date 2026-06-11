#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载 39 所强基 985 高校校徽 PNG（从 Wikipedia Commons 抓 64px 缩略图）。

三级回退策略（谁先命中就用谁）：
  1) zh.wikipedia.org 页面 images API 找 SVG（logo/emblem/seal 关键词）
  2) en.wikipedia.org 页面 images API 找 SVG（兜底中文 wiki 缺图的情况）
  3) commons.wikimedia.org list=search（补前两级都拿不到的少量特殊校）

支持 CLI：
  python tools/fetch_school_logos.py                 # 跑全 39
  python tools/fetch_school_logos.py neu 东北大学     # 单独补一所
  python tools/fetch_school_logos.py neu 东北大学 --force  # 强制重下
  python tools/fetch_school_logos.py --width 96      # 自定义缩略图尺寸

输出：<project_root>/static/schools/{slug}.png
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

UA = "LuoguAIReport/3.8 (https://oi.aijiangti.cn) python-urllib"
OUT_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "static", "schools"))
THUMB_WIDTH = 64
SLEEP_SEC = 1.0
EXCLUDE_KW = (
    "commons-logo", "commons_icon", "wikisource", "wikibooks", "wikipedia-logo",
    "w3c", "gnu", "creative", "edit-clear", "ambox", "shock", "symbol",
    "wikinews", "wikiquote", "wikiversity", "wikivoyage", "wiktionary",
    "arrow", "question book", "progressive",
)
KEEP_KW = ("logo", "emblem", "seal", "shield", "crest", "crown", "校徽", "校名")

# (slug, zh_title, en_title, cn_name, fallback_emoji, province)
SCHOOLS = [
    ("pku",   "北京大学",          "Peking University",                          "北京大学",         "🔵", "北京"),
    ("thu",   "清华大学",          "Tsinghua University",                        "清华大学",         "🟣", "北京"),
    ("ruc",   "中国人民大学",      "Renmin University of China",                 "中国人民大学",     "🔴", "北京"),
    ("buaa",  "北京航空航天大学",  "Beihang University",                         "北京航空航天大学", "🔵", "北京"),
    ("bit",   "北京理工大学",      "Beijing Institute of Technology",            "北京理工大学",     "🟢", "北京"),
    ("cau",   "中国农业大学",      "China Agricultural University",              "中国农业大学",     "🟡", "北京"),
    ("bnu",   "北京师范大学",      "Beijing Normal University",                   "北京师范大学",     "🔵", "北京"),
    ("muc",   "中央民族大学",      "Minzu University of China",                  "中央民族大学",     "⚪", "北京"),
    ("nankai","南开大学",          "Nankai University",                          "南开大学",         "🟣", "天津"),
    ("tju",   "天津大学",          "Tianjin University",                         "天津大学",         "🔵", "天津"),
    ("dlut",  "大连理工大学",      "Dalian University of Technology",            "大连理工大学",     "🟢", "辽宁"),
    ("neu",   "东北大学",          "Northeastern University (China)",            "东北大学",         "🟡", "辽宁"),
    ("jlu",   "吉林大学",          "Jilin University",                           "吉林大学",         "🔴", "吉林"),
    ("hit",   "哈尔滨工业大学",    "Harbin Institute of Technology",             "哈尔滨工业大学",   "🔵", "黑龙江"),
    ("fdu",   "复旦大学",          "Fudan University",                           "复旦大学",         "🔴", "上海"),
    ("tongji","同济大学",          "Tongji University",                          "同济大学",         "🔵", "上海"),
    ("sjtu",  "上海交通大学",      "Shanghai Jiao Tong University",              "上海交通大学",     "🔵", "上海"),
    ("ecnu",  "华东师范大学",      "East China Normal University",               "华东师范大学",     "🟢", "上海"),
    ("nju",   "南京大学",          "Nanjing University",                         "南京大学",         "🟣", "江苏"),
    ("seu",   "东南大学",          "Southeast University",                       "东南大学",         "🟡", "江苏"),
    ("zju",   "浙江大学",          "Zhejiang University",                        "浙江大学",         "🔴", "浙江"),
    ("ustc",  "中国科学技术大学",  "University of Science and Technology of China","中国科学技术大学","🔴", "安徽"),
    ("xmu",   "厦门大学",          "Xiamen University",                          "厦门大学",         "🟡", "福建"),
    ("sdu",   "山东大学",          "Shandong University",                        "山东大学",         "🔵", "山东"),
    ("ouc",   "中国海洋大学",      "Ocean University of China",                  "中国海洋大学",     "🔵", "山东"),
    ("whu",   "武汉大学",          "Wuhan University",                           "武汉大学",         "🟣", "湖北"),
    ("hust",  "华中科技大学",      "Huazhong University of Science and Technology","华中科技大学",   "🟢", "湖北"),
    ("csu",   "中南大学",          "Central South University",                   "中南大学",         "🟡", "湖南"),
    ("hnu",   "湖南大学",          "Hunan University",                           "湖南大学",         "🔴", "湖南"),
    ("nudt",  "国防科技大学",      "National University of Defense Technology",  "国防科技大学",     "🟢", "湖南"),
    ("sysu",  "中山大学",          "Sun Yat-sen University",                     "中山大学",         "🔵", "广东"),
    ("scut",  "华南理工大学",      "South China University of Technology",       "华南理工大学",     "🔴", "广东"),
    ("scu",   "四川大学",          "Sichuan University",                         "四川大学",         "🟡", "四川"),
    ("cqu",   "重庆大学",          "Chongqing University",                       "重庆大学",         "🔵", "重庆"),
    ("uestc", "电子科技大学",      "University of Electronic Science and Technology of China","电子科技大学","🔵", "四川"),
    ("xjtu",  "西安交通大学",      "Xi'an Jiaotong University",                  "西安交通大学",     "🔴", "陕西"),
    ("nwpu",  "西北工业大学",      "Northwestern Polytechnical University",      "西北工业大学",     "🔵", "陕西"),
    ("nwafu", "西北农林科技大学",  "Northwest A&F University",                   "西北农林科技大学", "🟢", "陕西"),
    ("lzu",   "兰州大学",          "Lanzhou University",                         "兰州大学",         "🔵", "甘肃"),
]


# ---------------- HTTP ----------------
def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "image/png,image/svg+xml,text/html,*/*"})
    return urllib.request.urlopen(req, timeout=timeout).read()


# ---------------- API helpers ----------------
def api_search_svg_on_page(page_title, lang="zh"):
    """在指定 wiki 页面找所有 SVG 图片，过滤关键词后返回最佳候选 file_title"""
    api = f"https://{lang}.wikipedia.org/w/api.php?" + urllib.parse.urlencode({
        "action": "query", "format": "json",
        "titles": page_title, "prop": "images", "imlimit": "80",
    })
    try:
        data = json.loads(fetch(api).decode("utf-8"))
    except Exception as e:
        print(f"    [warn] {lang} page fetch fail: {e}", flush=True)
        return None
    cands = []
    for p in data.get("query", {}).get("pages", {}).values():
        for im in p.get("images", []):
            t = im.get("title", "")
            tl = t.lower()
            if not tl.endswith(".svg"):
                continue
            if any(x in tl for x in EXCLUDE_KW):
                continue
            if any(k in tl for k in KEEP_KW):
                cands.append(t)
    if not cands:
        return None
    # 优先级：logo+university > emblem > seal > 其他 logo
    for c in cands:
        cl = c.lower()
        if "university" in cl and "logo" in cl:
            return c
    for c in cands:
        cl = c.lower()
        if "emblem" in cl:
            return c
    for c in cands:
        cl = c.lower()
        if "seal" in cl:
            return c
    return cands[0]


def api_commons_search(query, limit=20):
    """commons 搜文件"""
    api = "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode({
        "action": "query", "format": "json",
        "list": "search", "srsearch": query,
        "srnamespace": "6", "srlimit": str(limit),
    })
    try:
        data = json.loads(fetch(api).decode("utf-8"))
        return [r["title"] for r in data.get("query", {}).get("search", [])]
    except Exception as e:
        print(f"    [warn] commons search fail: {e}", flush=True)
        return []


def get_thumb_url(file_title, width, lang="zh"):
    """imageinfo + iiurlwidth 拿缩略图 URL（lang 决定 API host）"""
    api = f"https://{lang}.wikipedia.org/w/api.php?" + urllib.parse.urlencode({
        "action": "query", "format": "json",
        "titles": file_title, "prop": "imageinfo",
        "iiprop": "url", "iiurlwidth": str(width),
    })
    try:
        data = json.loads(fetch(api).decode("utf-8"))
        pages = data.get("query", {}).get("pages", {})
        for p in pages.values():
            for info in p.get("imageinfo", []):
                return info.get("thumburl") or info.get("url")
    except Exception as e:
        print(f"    [warn] imageinfo fail: {e}", flush=True)
    return None


# ---------------- 主逻辑 ----------------
def download_one(slug, zh_title, en_title, cn_name, force=False):
    """对单校执行三级回退，下载到 OUT_DIR/{slug}.png"""
    out_png = os.path.join(OUT_DIR, f"{slug}.png")
    if not force and os.path.exists(out_png) and os.path.getsize(out_png) > 1500:
        print(f"  [skip] {slug} ({cn_name}) · 已有 {os.path.getsize(out_png)//1024}KB", flush=True)
        return True

    # 1) zh wiki 页面 images
    file_title = api_search_svg_on_page(zh_title, "zh")
    src_lang = "zh"
    if file_title:
        print(f"  [hit-zh] {slug} <- {file_title.split(':')[-1]}", flush=True)
    # 2) en wiki 页面 images
    if not file_title:
        file_title = api_search_svg_on_page(en_title, "en")
        src_lang = "en"
        if file_title:
            print(f"  [hit-en] {slug} <- {file_title.split(':')[-1]}", flush=True)
    # 3) commons search
    if not file_title:
        for q in (cn_name, f"{cn_name} logo", f"{cn_name} 校徽", en_title, f"{en_title} logo"):
            results = api_commons_search(q, 20)
            for r in results:
                rl = r.lower()
                if any(x in rl for x in EXCLUDE_KW):
                    continue
                if not rl.endswith(".svg"):
                    continue
                if not any(k in rl for k in KEEP_KW + ("university", "institute", "polytechnic", "academy", "chinese", "jiao", "tsinghua", "fudan", "nankai", "tongji", "wuhan", "huazhong", "zhongnan", "hunan", "nudt", "sun yat", "sichuan", "chongqing", "uestc", "electronic", "northwest", "lanzhou", "beihang", "agricultural", "normal", "minzu", "lzu")):
                    continue
                file_title = r
                src_lang = "commons"
                print(f"  [hit-cs] {slug} <- {r.split(':')[-1]}", flush=True)
                break
            if file_title:
                break

    if not file_title:
        print(f"  [miss] {slug} ({cn_name}) · 三级回退全失败，将回退 emoji", flush=True)
        return False

    # commons 搜出来的 file_title 没有指定 lang，但 imageinfo 用哪个 wiki host 都能解析
    thumb = get_thumb_url(file_title, THUMB_WIDTH, src_lang)
    if not thumb:
        print(f"  [miss] {slug} · 拿不到 thumburl", flush=True)
        return False
    try:
        data = fetch(thumb)
        if len(data) < 500:
            print(f"  [miss] {slug} · 缩略图太小 ({len(data)}B)", flush=True)
            return False
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(out_png, "wb") as f:
            f.write(data)
        print(f"  [ok]   {slug} ({cn_name}) · {len(data)//1024}KB", flush=True)
        return True
    except Exception as e:
        print(f"  [fail] {slug} · {e}", flush=True)
        return False


def main():
    global THUMB_WIDTH, SLEEP_SEC
    force = False
    targets = list(SCHOOLS)
    # 解析 CLI
    i = 1
    while i < len(sys.argv):
        a = sys.argv[i]
        if a == "--force":
            force = True
        elif a == "--width" and i + 1 < len(sys.argv):
            THUMB_WIDTH = int(sys.argv[i + 1]); i += 1
        elif a == "--sleep" and i + 1 < len(sys.argv):
            SLEEP_SEC = float(sys.argv[i + 1]); i += 1
        elif a == "--out" and i + 1 < len(sys.argv):
            global OUT_DIR
            OUT_DIR = sys.argv[i + 1]; i += 1
        else:
            # 自定义单校：python tools/fetch_school_logos.py <slug> <zh_title>
            if i + 1 < len(sys.argv):
                slug = a
                zh = sys.argv[i + 1]
                targets = [(slug, zh, zh, zh, "🔵", "")]
                i += 1
            else:
                print(f"未知参数: {a}", file=sys.stderr)
        i += 1

    print(f"OUT_DIR   = {OUT_DIR}")
    print(f"WIDTH     = {THUMB_WIDTH}px")
    print(f"SLEEP     = {SLEEP_SEC}s")
    print(f"FORCE     = {force}")
    print(f"TARGETS   = {len(targets)} 校\n")

    ok = skip = miss = 0
    for entry in targets:
        slug, zh, en, cn, emoji, prov = entry
        print(f"-> {slug} {cn}")
        if download_one(slug, zh, en, cn, force=force):
            ok += 1
        else:
            miss += 1
        time.sleep(SLEEP_SEC)

    n_png = len([f for f in os.listdir(OUT_DIR) if f.endswith(".png")]) if os.path.isdir(OUT_DIR) else 0
    print(f"\n=== 完成 ===")
    print(f"  成功: {ok}  缺失: {miss}  共下载: {n_png} 张")


if __name__ == "__main__":
    main()
