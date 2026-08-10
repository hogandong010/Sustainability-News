# -*- coding: utf-8 -*-
"""ESG / 可持续发展 每日资讯速览生成器（方案C · 生产可用版）
依赖：仅 Python 标准库（urllib + xml.etree + html.parser），无需 pip 安装。

特性：
  - 双模式信源：RSS（国际组织）+ 网页栏目解析（国内政府/媒体，均无标准 RSS）
  - 关键词命中即筛，并自动给出"业务落点"（对应手册角度库）
  - 一键推送到企业微信机器人（push_to_wecom）

用法：
  1) 把企业微信群机器人 Webhook 填到下方 WEBHOOK
  2) python3 esg_news_digest.py
  3) 用 crontab / 云函数 设置每日 09:00 自动运行（见文件底部部署说明）
"""
import json
import os
import re
import ssl
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from html.parser import HTMLParser

# ===== 企业微信推送配置 =====
# 优先读取环境变量 WEBHOOK（GitHub Actions / 云函数部署时用 Secret 注入，密钥不写进代码）
# 本地手动测试也可临时设置：export WEBHOOK="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx"
WEBHOOK = os.environ.get("WEBHOOK", "")

# ===== 信源（已联网核实）=====
# kind: "rss"  = 标准 RSS/Atom 订阅源（国际组织）
#       "html" = 栏目列表页（国内政府/媒体多数无 RSS，解析 <a> 标题）
SOURCES = [
    # —— 国际（RSS，已实测可用）——
    {"name": "IPCC", "url": "https://www.ipcc.ch/feed/", "kind": "rss"},

    # —— 国内政府/媒体（网页栏目，已核实为文章列表页）——
    {"name": "中国政府网·要闻", "url": "https://www.gov.cn/yaowen", "kind": "html"},
    {"name": "生态环境部·环境要闻", "url": "https://www.mee.gov.cn/xxgk/hjyw/", "kind": "html"},
    {"name": "财新网", "url": "https://www.caixin.com/", "kind": "html"},
    {"name": "21世纪经济报道", "url": "https://www.21jingji.com/", "kind": "html"},

    # —— 备选（如本地网络可访问，取消注释启用）——
    # {"name": "UNFCCC 新闻", "url": "https://unfccc.int/news", "kind": "html"},
    # {"name": "上交所·新闻", "url": "https://www.sse.com.cn/aboutus/mediacenter/", "kind": "html"},
]

# ===== 选题关键词（对应你的三块业务）=====
KEYWORDS = ["双碳", "碳达峰", "碳中和", "碳市场", "碳排放", "碳核算", "碳关税", "CBAM",
            "ESG", "可持续发展", "披露", "绿色", "减排", "碳汇", "生物多样性",
            "生态保护", "湿地", "红树林", "绿色金融", "绿电", "应对气候变化"]

# 关键词 -> 业务落点（自动建议，对应手册"角度库"）
ANGLE_MAP = {
    "碳核算": "对你企业的影响：现在就要建碳数据台账",
    "碳排放": "对你企业的影响：摸清范围 1/2/3 排放",
    "碳市场": "对你企业的影响：供应链碳成本正在传导",
    "CBAM": "对你企业的影响：出口企业须提供嵌入碳排放数据",
    "ESG": "同行怎么做：强制披露下领先企业的作业",
    "披露": "时间线梳理：合规 deadline 临近",
    "碳汇": "看得见的变化：生态价值变现的新玩法",
    "湿地": "看得见的变化：生态修复与碳汇金融",
    "生物多样性": "生态故事：企业可参与的保护行动",
}

WINDOW_DAYS = 3  # 仅 RSS 源按近 N 天过滤；网页源默认取最新列表


# ---------- 网络获取（容忍证书问题）----------
def fetch(url, timeout=12):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        raw = r.read()
    return raw.decode("utf-8", errors="ignore")


# ---------- RSS / Atom 解析 ----------
def parse_rss(xml_text):
    items = []
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return items
    for it in root.iter():
        tag = it.tag.lower()
        if tag.endswith("item") or tag.endswith("entry"):
            title = summary = link = pub = ""
            for c in it:
                ct = c.tag.lower()
                if ct.endswith("title"):
                    title = (c.text or "").strip()
                elif ct.endswith("summary") or ct.endswith("description") or ct.endswith("content"):
                    summary = (c.text or "").strip()
                elif ct.endswith("link"):
                    link = (c.get("href") or (c.text or "")).strip()
                elif ct.endswith("pubdate") or ct.endswith("published") or ct.endswith("updated"):
                    pub = (c.text or "").strip()
            items.append({"title": title, "summary": summary, "link": link, "pub": pub})
    return items

def parse_date(s):
    if not s:
        return None
    s = s.strip()
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z",
               "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            cand = s[:len(fmt) + 4] if "%z" in fmt else s
            return datetime.strptime(cand, fmt)
        except Exception:
            continue
    return None


# ---------- 网页栏目解析 ----------
class AnchorParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.anchors = []
        self._cap = False
        self._data = ""
        self._href = ""
    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href", "")
            self._data = ""
            self._cap = True
    def handle_endtag(self, tag):
        if tag == "a" and self._cap:
            text = self._data.strip()
            if text and len(text) >= 4:
                self.anchors.append((text, self._href))
            self._cap = False
    def handle_data(self, data):
        if self._cap:
            self._data += data

def parse_html_anchors(html_text):
    p = AnchorParser()
    try:
        p.feed(html_text)
    except Exception:
        pass
    return p.anchors


# ---------- 主流程 ----------
def collect():
    now = datetime.now()
    cutoff = now - timedelta(days=WINDOW_DAYS)
    hits = []
    for s in SOURCES:
        name, url, kind = s["name"], s["url"], s["kind"]
        try:
            raw = fetch(url)
        except Exception as e:
            print(f"[跳过] {name}: {e}")
            continue
        if kind == "rss":
            rows = parse_rss(raw)
            for it in rows:
                text = it["title"] + " " + it["summary"]
                matched = [k for k in KEYWORDS if k in text]
                if not matched:
                    continue
                dt = parse_date(it["pub"])
                if dt and dt < cutoff:
                    continue
                link = it["link"]
                if link and link.startswith("/"):
                    link = url.rstrip("/") + link
                hits.append(_make(name, it["title"], link, matched))
        else:  # html 栏目页
            for title, href in parse_html_anchors(raw):
                matched = [k for k in KEYWORDS if k in title]
                if not matched:
                    continue
                link = href
                if link and link.startswith("/"):
                    link = url.rstrip("/") + link
                hits.append(_make(name, title, link, matched))
    # 去重
    seen, uniq = set(), []
    for h in hits:
        if h["title"] in seen:
            continue
        seen.add(h["title"]); uniq.append(h)
    return uniq

def _make(src, title, link, matched):
    angle = "; ".join(ANGLE_MAP.get(k, "") for k in matched if k in ANGLE_MAP)
    return {"src": src, "title": title, "link": link, "kw": matched, "angle": angle}


# ---------- 企业微信推送 ----------
def push_to_wecom(webhook, markdown):
    if not webhook:
        print("[未推送] 未配置 WEBHOOK，仅本地输出")
        return None
    # 企业微信 markdown 限制 4096 字节，超出截断
    payload = {"msgtype": "markdown", "markdown": {"content": markdown[:4000]}}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(webhook, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"[推送失败] {e}")
        return None


def main():
    uniq = collect()
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"# 可持续发展资讯速览（{today}）", f"近 {WINDOW_DAYS} 天命中 {len(uniq)} 条\n"]
    md_parts = [f"**📌 可持续发展资讯速览（{today}）**", f"> 命中 {len(uniq)} 条\n"]
    for h in uniq:
        line = f"- **[{h['src']}]** {h['title']}"
        lines.append(line)
        md_parts.append(f"> **[{h['src']}]** {h['title']}")
        if h["angle"]:
            lines.append(f"  业务落点：{h['angle']}")
            md_parts.append(f"> 落点：{h['angle']}")
        if h["link"]:
            lines.append(f"  链接：{h['link']}")
            md_parts.append(f"> {h['link']}")
        lines.append(""); md_parts.append("")
    out = "\n".join(lines)
    print(out)
    md = "\n".join(md_parts)
    push_to_wecom(WEBHOOK, md)


if __name__ == "__main__":
    main()

