# -*- coding: utf-8 -*-
"""公众号正文抓取（线二知识库）：mp.weixin.qq.com 文章 → 清洗 markdown 入库
抓取策略：
  1. 直连 mp.weixin.qq.com（requests + 完整浏览器头）
  2. 被微信验证码墙拦截（wappoc_appmsgcaptcha）→ 搜狗微信搜索标题
     → 中转页提取 src=11 签名链接（客户端内链接，免验证码）→ 抓正文
用法:
    python fetch_article.py <url> <标题> [公众号名]   # 有标题走旁路更稳
    python fetch_article.py <url>                    # 只直连
入库目录：config.json 的 vault目录\\公众号；名单外/线一公众号一律拦截进 待入库\\ 留痕
"""
import datetime
import html
import json
import os
import re
import sys

import requests

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_JSON = os.path.join(BASE, "config.json")
PENDING = os.path.join(BASE, "待入库")
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def _cfg():
    try:
        with open(CONFIG_JSON, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def vault_mp_dir():
    """公众号入库目录：config.json vault目录\公众号；未配置则落到技能目录内"""
    v = (_cfg().get("vault目录") or "").strip()
    if v and os.path.isdir(v):
        return os.path.join(v, "公众号")
    return os.path.join(BASE, "公众号入库")


VAULT = vault_mp_dir()


def _session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_via_sogou(title):
    """验证码旁路：搜狗搜标题 → 中转页 → 拼 src=11 签名链接 → 正文页"""
    s = _session()
    s.headers["Referer"] = "https://weixin.sogou.com/"
    r = s.get("https://weixin.sogou.com/weixin",
              params={"type": 2, "query": title}, timeout=30)
    r.raise_for_status()
    links = re.findall(r'href="(/link\?url=[^"]+)"', r.text)
    if not links:
        raise RuntimeError("搜狗无搜索结果")
    r2 = s.get("https://weixin.sogou.com" + links[0], timeout=30)
    parts = re.findall(r"url \+= '([^']*)'", r2.text)
    if not parts:
        raise RuntimeError("中转页无签名链接")
    target = "".join(parts)
    return s.get(target, timeout=30, allow_redirects=True)


def fetch(url, title_hint=None):
    """返回 (页面文本)。直连被验证码拦则走搜狗旁路"""
    r = _session().get(url, timeout=30, allow_redirects=True)
    if "wappoc_appmsgcaptcha" in r.url or "js_content" not in r.text:
        if not title_hint:
            raise RuntimeError("被微信验证码拦截，且无标题可走搜狗旁路")
        r = fetch_via_sogou(title_hint)
        if "js_content" not in r.text:
            raise RuntimeError("搜狗旁路也失败")
    return r.text


def parse(page):
    """页面文本 → (标题, 清洗后 markdown 正文)"""
    m = re.search(r'<meta property="og:title" content="([^"]*)"', page)
    if not m:
        m = re.search(r'<h1[^>]*class="rich_media_title[^"]*"[^>]*>'
                      r'([\s\S]*?)</h1>', page)
    title = html.unescape(m.group(1)).strip() if m else "未命名文章"
    title = re.sub(r"<[^>]+>", "", title)

    m = re.search(r'<div[^>]*id="js_content"[^>]*>(.*?)</div>\s*(?:<script|$)',
                  page, re.S)
    if not m:
        m = re.search(r'<div[^>]*class="rich_media_content[^"]*"[^>]*>'
                      r'(.*?)</div>', page, re.S)
    body = m.group(1) if m else ""

    body = re.sub(r"<script[\s\S]*?</script>", "", body)
    body = re.sub(r"<style[\s\S]*?</style>", "", body)
    body = re.sub(r"<br\s*/?>", "\n", body)
    body = re.sub(r"</p>", "\n\n", body)
    body = re.sub(r'<img[^>]*data-src="([^"]+)"[^>]*/?>',
                  r'\n![图](\1)\n', body)
    body = re.sub(r'<img[^>]*src="([^"]+)"[^>]*/?>',
                  r'\n![图](\1)\n', body)
    body = re.sub(r"<strong>|</strong>", "**", body)
    body = re.sub(r"<[^>]+>", "", body)
    body = html.unescape(body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return title, body


def to_markdown(title, body, source, url):
    date = datetime.date.today().isoformat()
    return (f"---\nsource: {source}\ndate: {date}\nurl: {url}\n"
            f"tags: [公众号]\n---\n\n# {title}\n\n{body}\n")


def sanitize(s):
    for ch in '<>:"/\\|?*':
        s = s.replace(ch, "_")
    return s.strip()


def norm_name(s):
    """归一化匹配：只留汉字/字母/数字（与导出脚本同规则）"""
    return "".join(ch for ch in s
                   if ch.isalnum() or "一" <= ch <= "鿿")


def classify(source):
    """公众号名 → 类别（知识价值类/信息活动类/None=名单外）
    硬规则：只有「知识价值类」允许入 vault；线一（信息活动类）与名单外一律拦截"""
    cfg = _cfg()
    for cat, names in (cfg.get("公众号") or {}).items():
        for n in names:
            if norm_name(n) == norm_name(source):
                return cat
    return None


def main():
    if len(sys.argv) < 2:
        print("用法: python fetch_article.py <url> [标题] [公众号名]", flush=True)
        sys.exit(1)
    url = sys.argv[1]
    title_hint = sys.argv[2] if len(sys.argv) > 2 else None
    source = sys.argv[3] if len(sys.argv) > 3 else None

    page = fetch(url, title_hint=title_hint)
    title, body = parse(page)
    md = to_markdown(title, body, source or "未分类", url)

    cat = classify(source) if source else None
    if cat != "知识价值类":
        # 线一公众号/名单外：只提醒不入库，进待入库目录留痕，不进 Obsidian vault
        os.makedirs(PENDING, exist_ok=True)
        out = os.path.join(PENDING, f"{sanitize(source) or '未分类'}_{sanitize(title)}.md")
        with open(out, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"[拦截] 不入库（{cat or '名单外'}）: {out}", flush=True)
        sys.exit(0)

    out_dir = os.path.join(VAULT, sanitize(source))
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"{sanitize(title)}.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"[入库] {out}（正文 {len(body)} 字）", flush=True)


if __name__ == "__main__":
    main()
