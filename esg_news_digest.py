# -*- coding: utf-8 -*-
"""ESG / 可持续发展 每日资讯速览生成器（方案C · 生产可用版）
依赖：仅 Python 标准库（urllib + xml.etree + html.parser），无需 pip 安装。

特性：
  - 双模式信源：RSS（国际组织）+ 网页栏目解析（国内政府/媒体，均无标准 RSS）
  - 关键词命中即筛，并自动给出"业务落点"（对应手册角度库）
  - 一键推送到企业微信机器人（push_to_wecom）

用法：
  1) 把企业微信群机器人 Webhook 填到下方 WEBHOOK（或用 GitHub Secrets 注入）
  2) python3 esg_news_digest.py
  3) 用 GitHub Actions / 云函数 设置每日 09:00 自动运行（见文件底部部署说明）
"""
import json
import os
import re
import ssl
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import urljoin

# ===== 企业微信推送配置 =====
# 优先读取环境变量 WEBHOOK（GitHub Actions / 云函数部署时用 Secret 注入，密钥不写进代码）
# 本地手动测试也可临时设置：export WEBHOOK="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx"
WEBHOOK = os.environ.get("WEBHOOK", "")

# ===== 信源（已联网核实）=====
# kind: "rss"  = 标准 RSS/Atom 订阅源（国际组织）
#       "html" = 栏目列表页（国内政府/媒体多数无 RSS，解析 <a> 标题）
#       "cninfo" = 巨潮网官方披露接口（上市公司 ESG 公告，覆盖上交所/深交所/北交所）
SOURCES = [
    # —— 国际（RSS，已实测可用）——
    {"name": "IPCC", "url": "https://www.ipcc.ch/feed/", "kind": "rss"},

    # —— 国内政府/媒体（网页栏目，已核实为文章列表页）——
    {"name": "中国政府网·要闻", "url": "https://www.gov.cn/yaowen", "kind": "html"},
    {"name": "生态环境部·环境要闻", "url": "https://www.mee.gov.cn/xxgk/hjyw/", "kind": "html"},
    {"name": "财新网", "url": "https://www.caixin.com/", "kind": "html"},
    {"name": "21世纪经济报道", "url": "https://www.21jingji.com/", "kind": "html"},

    # —— 上市公司 ESG 公告：巨潮网（证监会官方统一披露平台，一家覆盖上交所/深交所/北交所）——
    {"name": "巨潮网·上市公司ESG公告", "url": "http://www.cninfo.com.cn/new/hisAnnouncement/query", "kind": "cninfo"},
    # {"name": "UNFCCC 新闻", "url": "https://unfccc.int/news", "kind": "html"},
]

# ===== 选题关键词（对应你的三块业务）=====
KEYWORDS = ["双碳", "碳达峰", "碳中和", "碳市场", "碳排放", "碳核算", "碳关税", "CBAM",
            "ESG", "可持续发展", "披露", "绿色", "减排", "碳汇", "生物多样性",
            "生态保护", "湿地", "红树林", "绿色金融", "绿电", "应对气候变化",
            "可持续发展报告", "ESG报告", "社会责任报告", "绿色债券", "环境信息"]

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
    "可持续发展报告": "同行怎么做：领先企业 ESG 披露范本",
    "ESG报告": "同行怎么做：强制披露下领先企业的作业",
    "社会责任报告": "同行怎么做：ESG 披露范式参考",
    "绿色债券": "看得见的变化：绿色金融新工具",
}

WINDOW_DAYS = 3  # 仅 RSS 源按近 N 天过滤；网页源默认取最新列表

# 巨潮网（cninfo）抓取配置：上市公司 ESG 披露较稀疏，回看 7 天保证覆盖
# 注意：cninfo 的 keyword 参数是「全文检索」（匹配 PDF 正文，几乎每条都命中），
# 无法按标题筛选，故本脚本改为「拉取最新公告 + 按标题短语命中」的方式，干净可靠。
CNINFO_TITLE_PHRASES = ["ESG", "可持续发展报告", "社会责任报告", "绿色债券",
                        "环境、社会与公司治理", "环境、社会及治理", "ESG报告"]
CNINFO_WINDOW_DAYS = 7      # 回看天数
CNINFO_PAGE_SIZE = 30       # 每页条数
CNINFO_MAX_PAGES = 15       # 最多翻页数（上限，避免请求过多）
CNINFO_TARGET = 8           # 收集到这么多条命中即提前停止
CNINFO_API = "http://www.cninfo.com.cn/new/hisAnnouncement/query"


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


# ---------- 巨潮网（cninfo）上市公司 ESG 公告接口 ----------
def _cninfo_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

def fetch_cninfo():
    """巨潮网（cninfo）官方披露接口：一家覆盖上交所/深交所/北交所的 ESG 相关公告。

    策略：cninfo 的 keyword 参数是全文检索（几乎每条公告正文都含 ESG，无法按标题筛选），
    因此这里改为「拉取最近 N 天最新公告，按标题短语命中」——只保留标题里真正出现
    ESG / 可持续发展报告 / 社会责任报告 等字样的公告，干净无噪声。"""
    end = datetime.now()
    start = end - timedelta(days=CNINFO_WINDOW_DAYS)
    se_date = f"{start.strftime('%Y-%m-%d')}~{end.strftime('%Y-%m-%d')}"
    hits, seen_ids = [], set()
    page = 0
    while page < CNINFO_MAX_PAGES and len(hits) < CNINFO_TARGET:
        page += 1
        body = urllib.parse.urlencode({
            "pageNum": str(page), "pageSize": str(CNINFO_PAGE_SIZE),
            "tabName": "全部", "seDate": se_date, "isHL": "false",
        }).encode("utf-8")
        req = urllib.request.Request(CNINFO_API, data=body,
                                     headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        try:
            with urllib.request.urlopen(req, timeout=20, context=_cninfo_ctx()) as r:
                j = json.loads(r.read().decode("utf-8", errors="ignore"))
        except Exception as e:
            print(f"[跳过] 巨潮网(第{page}页): {e}")
            break
        anns = j.get("announcements") or []
        if not anns:
            break
        for a in anns:
            aid = str(a.get("announcementId", ""))
            if aid in seen_ids:
                continue
            title = (a.get("announcementTitle") or "").strip()
            matched = [k for k in CNINFO_TITLE_PHRASES if k in title]
            if not matched:
                continue
            seen_ids.add(aid)
            sec = (a.get("secName") or "").strip()
            link = f"http://www.cninfo.com.cn/new/disclosure/detail?announcementId={aid}"
            disp = f"{sec}：{title}" if sec else title
            # 用与 KEYWORDS 重合的短语去匹配业务落点
            angle_kw = [k for k in matched if k in KEYWORDS]
            hits.append(_make("巨潮网·上市公司ESG公告", disp, link, angle_kw))
    return hits


# ---------- 主流程 ----------
def collect():
    now = datetime.now()
    cutoff = now - timedelta(days=WINDOW_DAYS)
    hits = []
    for s in SOURCES:
        name, url, kind = s["name"], s["url"], s["kind"]
        if kind == "cninfo":
            try:
                hits.extend(fetch_cninfo())
            except Exception as e:
                print(f"[跳过] {name}: {e}")
            continue
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
                if link:
                    link = urljoin(url, link)
                hits.append(_make(name, it["title"], link, matched))
        else:  # html 栏目页
            for title, href in parse_html_anchors(raw):
                matched = [k for k in KEYWORDS if k in title]
                if not matched:
                    continue
                link = urljoin(url, href) if href else ""
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


# ---------- AI 视频钩子（可选，设了 LLM_API_KEY 才生效）----------
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL") or "https://api.openai.com/v1"
LLM_MODEL = os.environ.get("LLM_MODEL") or "gpt-4o-mini"

def llm_complete(prompt):
    url = LLM_BASE_URL.rstrip("/") + "/chat/completions"
    data = {"model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.8}
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LLM_API_KEY}",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read().decode("utf-8"))
        return resp["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[LLM失败] {e}")
        return ""

# 钩子选题打分：优先挑"对客户最有价值、最适合拍视频"的选题，而不是列表里的前 3 条。
# 对齐《运营手册》1.2 热点分级：命中核心业务词、能落到客户、来自可拍信源（同行动作/行业动态）的排前面。
HOOK_PRIORITY_KEYWORDS = ["CBAM", "碳关税", "碳核算", "碳市场", "碳排放", "碳足迹", "范围三",
                          "双碳", "碳达峰", "碳中和", "ESG", "可持续发展报告",
                          "社会责任报告", "绿色债券"]
HOOK_PRIORITY_SOURCES = ["巨潮网·上市公司ESG公告", "财新网", "21世纪经济报道"]

def score_hit(h):
    s = 0
    for k in HOOK_PRIORITY_KEYWORDS:
        if k in h["title"]:
            s += 3
    if h["angle"]:          # 能落到"对你的企业意味着什么"——最值钱
        s += 2
    if h["src"] in HOOK_PRIORITY_SOURCES:
        s += 2
    return s

def generate_hooks(hits, top_n=3):
    out = []
    for h in sorted(hits, key=score_hit, reverse=True)[:top_n]:
        prompt = (
            "你是短视频内容专家，帮一个做企业碳核算/ESG/生态保护的视频号写脚本。\n"
            f"热点：{h['title']}\n业务落点：{h.get('angle','')}\n\n"
            "请只输出三项，简洁口语化：\n"
            "1) 视频开头3秒口播文案（抓人、像真人说话）\n"
            "2) 视频标题（带1个emoji，吸引点击）\n"
            "3) 封面文案（一句话）"
        )
        hook = llm_complete(prompt)
        if hook:
            out.append((h, hook))
    return out


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
    if LLM_API_KEY:
        for h, hook in generate_hooks(uniq):
            md_parts.append(f"> **🎬 视频钩子｜{h['title']}**")
            md_parts.append(hook)
            md_parts.append("")
    md = "\n".join(md_parts)
    push_to_wecom(WEBHOOK, md)


if __name__ == "__main__":
    main()

# ===================== 部署说明 =====================
# 1) 企业微信建群 → 右上角「...」→ 添加群机器人 → 复制 Webhook 填到 WEBHOOK（或用 Secrets 注入）
# 2) 本机定时（mac/Linux）：
#      crontab -e
#      0 9 * * * /usr/bin/python3 /root/esg_news_digest.py >> /root/digest.log 2>&1
#    Windows：任务计划程序，每日 09:00 触发 python3 esg_news_digest.py
# 3) 云上（不掉线，推荐）：GitHub Actions（schedule: '0 1 * * *' = 北京时间 09:00）
#    把本文件部署上去，并在仓库 Secrets 填 WEBHOOK，触发后自动抓取并推送企业微信
# 4) 注意：企业微信消息可在个人微信里接收（微信→我→设置→通用→辅助功能→微信接收企业微信消息）
# 5) 扩展开关：UNFCCC 等备选源在 SOURCES 里以注释形式保留；上市公司 ESG 公告已由
#    巨潮网（cninfo）官方接口统一抓取（覆盖上交所/深交所/北交所）；命中条目也可再喂给大模型写视频钩子

