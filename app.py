"""
Japanese News Reader — app.py
朝日新闻 文章列表 + DeepSeek 语法分析
"""

from flask import Flask, request, jsonify, render_template
from openai import OpenAI
from bs4 import BeautifulSoup
import requests as http
from dotenv import load_dotenv
import html as htmllib
import json
import re
import os
import hashlib
import time
from collections import defaultdict, deque
from datetime import datetime
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─── 配置 ────────────────────────────────────────────────────────────────
load_dotenv()  # 本地开发时从 .env 读取；Render 上直接用环境变量
DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY', '')

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(BASE_DIR, 'articles.json')
JST        = ZoneInfo('Asia/Tokyo')  # Render 服务器是 UTC，日期统一按日本时间算
MAX_CHARS  = 2000  # /analyze 单次最多分析的字数（控制 API 费用）
SUMMARY_CHARS = 120  # 摘要最多显示的字数

# 防止滥用：网站是公开的，任何人都能调用 /analyze（花的是我的 API 额度）
RATE_WINDOW   = 600   # 秒
RATE_PER_IP   = 10    # 每个 IP 在 RATE_WINDOW 内最多分析几次
DAILY_LIMIT   = 300   # 每个 gunicorn 进程每天最多分析几次（费用上限）
REFRESH_COOLDOWN = 600  # 手动刷新新闻列表的最短间隔（秒），避免频繁访问朝日

# 朝日新闻 RSS（RDF 格式）。只取标题、链接和官方摘要，不抓取正文
ASAHI_RSS = 'https://www.asahi.com/rss/asahi/newsheadlines.rdf'

HTTP_HEADS = {
    'User-Agent':      'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) '
                       'Chrome/124.0.0.0 Safari/537.36',
    'Accept-Language': 'ja,zh-CN;q=0.8,en;q=0.5',
    'Accept':          'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Encoding': 'gzip, deflate, br',
}

app = Flask(__name__)



# ─── 缓存 ────────────────────────────────────────────────────────────────

CACHE_VERSION = 2  # v1 缓存里存有文章全文，升级后直接作废


def _load_cache():
    try:
        with open(CACHE_FILE, encoding='utf-8') as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if data.get('version') == CACHE_VERSION:
        return data
    return {}


def _save_cache(data):
    # 先写临时文件再替换：线上有 2 个 gunicorn 进程，避免另一个进程读到写了一半的文件
    tmp = f'{CACHE_FILE}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CACHE_FILE)


# ─── 朝日新闻 抓取 ──────────────────────────────────────────────────────

def get_articles(force=False):
    today = datetime.now(JST).strftime('%Y-%m-%d')
    cache = _load_cache()
    fresh = cache.get('date') == today and cache.get('articles')
    recently_updated = time.time() - cache.get('updated_ts', 0) < REFRESH_COOLDOWN
    if fresh and (not force or recently_updated):
        return cache['articles'], None

    try:
        articles = _scrape_asahi()
        _save_cache({
            'version':    CACHE_VERSION,
            'date':       today,
            'updated_at': datetime.now(JST).strftime('%H:%M'),
            'updated_ts': time.time(),
            'articles':   articles,
        })
        return articles, None
    except Exception as exc:
        return cache.get('articles', []), str(exc)


def _scrape_asahi():
    """
    朝日新闻 RSS (RDF) → 标题 / 链接 / 时间 + 官方摘要。
    """
    resp = http.get(ASAHI_RSS, headers=HTTP_HEADS, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f"朝日新闻 RSS 不可用 (HTTP {resp.status_code})")

    soup = BeautifulSoup(resp.content, 'lxml-xml')
    items = soup.find_all('item')
    if not items:
        raise RuntimeError("朝日新闻 RSS 中未找到文章")

    seen_ids = set()
    stubs = []

    for item in items:
        if len(stubs) >= 15:
            break

        title_el = item.find('title')
        link_el  = item.find('link')
        desc_el  = item.find('description')
        date_el  = (item.find('dc:date')
                    or item.find('pubDate')
                    or item.find('date'))

        if not (title_el and link_el):
            continue

        title = (title_el.text or '').strip()
        link  = (link_el.text or '').strip()
        desc  = (desc_el.text or '').strip() if desc_el else ''
        date_raw = (date_el.text or '').strip() if date_el else ''

        # 用 URL 路径的 hash 作为 article ID
        article_id = hashlib.md5(link.encode()).hexdigest()[:12]

        if article_id in seen_ids:
            continue
        seen_ids.add(article_id)

        # 格式化发布时间
        time_str = ''
        try:
            # RDF 格式: 2026-05-27T19:45:00+09:00
            dt_str = re.sub(r'[-:]', '', date_raw[:19])
            if len(dt_str) >= 14:
                dt = datetime.strptime(dt_str[:14], '%Y%m%dT%H%M%S')
                time_str = f"{dt.month}月{dt.day}日 {dt.hour:02d}:{dt.minute:02d}"
        except Exception:
            time_str = date_raw[:16] if date_raw else ''

        stubs.append({
            'id':       article_id,
            'title':    title,
            'url':      link,
            'time':     time_str,
            'rss_desc': desc,
        })

    if not stubs:
        raise RuntimeError("朝日新闻 RSS 无法解析任何文章")

    # 并发抓取每篇文章页里官方提供的摘要（最多 10 篇）。不抓取、不保存正文
    top10     = stubs[:10]
    summaries = [''] * len(top10)

    with ThreadPoolExecutor(max_workers=5) as pool:
        fmap = {
            pool.submit(_fetch_asahi_summary, s['url']): i
            for i, s in enumerate(top10)
        }
        for fut in as_completed(fmap):
            idx = fmap[fut]
            try:
                summaries[idx] = fut.result(timeout=15)
            except Exception:
                summaries[idx] = ''

    articles = []
    for i, stub in enumerate(top10):
        summary = summaries[i] or stub['rss_desc']
        if len(summary) > SUMMARY_CHARS:
            summary = summary[:SUMMARY_CHARS] + '…'

        articles.append({
            'id':      stub['id'],
            'title':   stub['title'],
            'url':     stub['url'],
            'time':    stub['time'],
            'summary': summary,
        })

    return articles


def _fetch_asahi_summary(url: str) -> str:
    """读取文章页 <meta> 里朝日新闻自己公开的摘要（og:description / description）。"""
    try:
        resp = http.get(url, headers=HTTP_HEADS, timeout=15)
        resp.encoding = 'utf-8'
        soup = BeautifulSoup(resp.text, 'html.parser')
    except Exception:
        return ''

    meta = (soup.find('meta', attrs={'property': 'og:description'})
            or soup.find('meta', attrs={'name': 'description'}))
    if not meta:
        return ''
    summary = (meta.get('content') or '').strip().rstrip('…')
    # 官方摘要常在句子中间截断，只保留到最后一个「。」，方便翻译
    if '。' in summary:
        summary = summary[:summary.rindex('。') + 1]
    return summary


# ─── 路由 ────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/reader')
def reader_blank():
    return render_template('reader.html', article=None)


@app.route('/reader/<article_id>')
def reader(article_id):
    # Render 免费版重启后磁盘会清空，所以缓存里没有时先抓取一次
    articles, _ = get_articles()
    article = next((a for a in articles if a['id'] == article_id), None)
    return render_template('reader.html', article=article)


# ─── 简单限流（每个进程各自计数，够用即可） ─────────────────────────────

_ip_hits = defaultdict(deque)
_daily   = {'date': '', 'count': 0}


def _client_ip():
    # Render 在代理后面，真实 IP 在 X-Forwarded-For 的第一个
    fwd = request.headers.get('X-Forwarded-For', '')
    return fwd.split(',')[0].strip() or request.remote_addr or ''


def _rate_limited():
    """超过限制时返回错误提示，否则记一次并返回 None。"""
    now   = time.time()
    today = datetime.now(JST).strftime('%Y-%m-%d')
    if _daily['date'] != today:
        _daily.update(date=today, count=0)
        _ip_hits.clear()
    if _daily['count'] >= DAILY_LIMIT:
        return '今天的分析次数已用完，请明天再试'

    hits = _ip_hits[_client_ip()]
    while hits and now - hits[0] > RATE_WINDOW:
        hits.popleft()
    if len(hits) >= RATE_PER_IP:
        return f'请求太频繁，请 {RATE_WINDOW // 60} 分钟后再试'

    hits.append(now)
    _daily['count'] += 1
    return None


@app.route('/api/articles')
def api_articles():
    force    = request.args.get('force') == '1'
    articles, err = get_articles(force=force)
    cache    = _load_cache()
    resp = {
        'articles':   articles,
        'date':       cache.get('date', ''),
        'updated_at': cache.get('updated_at', ''),
    }
    if err:
        resp['warning'] = f'抓取失败，已使用缓存数据：{err}'
    return jsonify(resp)


@app.route('/analyze', methods=['POST'])
def analyze():
    data = request.get_json(force=True, silent=True)
    if data is None:
        return jsonify({'error': '请求格式错误'}), 400

    text = data.get('text', '').strip()
    if not text:
        return jsonify({'error': '请输入日语文章'}), 400
    if len(text) > MAX_CHARS:
        return jsonify({'error': f'文章太长（{len(text)} 字），请控制在 {MAX_CHARS} 字以内'}), 400
    if not DEEPSEEK_API_KEY:
        return jsonify({'error': '服务器未设置 DEEPSEEK_API_KEY 环境变量'}), 500
    limit_msg = _rate_limited()
    if limit_msg:
        return jsonify({'error': limit_msg}), 429

    orig_len = len(text)
    print(f"[analyze] 原文长度: {orig_len} 字符", flush=True)

    try:
        client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url='https://api.deepseek.com')

        prompt = f"""你是日语教学专家。请完成三项任务。

【原文】
{text}

══════════════════════════════════════
任务 1：词汇（vocabulary）
══════════════════════════════════════
筛选 8-15 个值得学习的实词（名词、动词、形容词、副词、复合词）。
⚠️ 排除：人名、地名、机构名、职位头衔、数字、日期。

══════════════════════════════════════
任务 2：语法（grammar）
══════════════════════════════════════
筛选 5-10 个 N1/N2 级别的日语句型/语法点。

✅ 标注对象（N2 级别示例）：
  〜わけにはいかない / 〜に違いない / 〜に決まっている / 〜どころか
  〜ざるを得ない / 〜おそれがある / 〜に過ぎない / 〜上で
  〜にわたって / 〜に応じて / 〜に基づいて / 〜をめぐって
  〜を通じて / 〜次第だ / 〜に伴って / 〜どころではない

✅ 标注对象（N1 级别示例）：
  〜までもない / 〜には及ばない / 〜に越したことはない
  〜を皮切りに / 〜まみれ / 〜たりとも / 〜が早いか
  〜と思いきや / 〜べからず / 〜まじき / 〜かたがた

✅ 也可以是文章里出现的常见复合句型：
  〜ことになる / 〜ものだ / 〜わけだ / 〜べきだ
  〜なければならない / 〜てもいい / 〜てはいけない
  〜に対して / 〜にとって / 〜として / 〜について
  〜によって / 〜において / 〜にわたって

⚠️ 禁止：
  - 标注基础助词（は/が/を/に/で/へ/も/から/まで/より）
  - 标注活用形（〜た/〜て/〜ない/〜ます/〜ば/〜たら）
  - 标注语态（被动〜れる、使役〜させる、使役被动〜させられる）
  - 标注单个助动词（〜たい/〜そうだ/〜ようだ）
  - 标注整词（如"開かれた""述べた""含む"）
  - 标注人地名

pattern 字段写句型中出现在原文里的部分（4-12 字），如 "わけにはいかない" "に基づいて" "をめぐって"。
不要带 〜 前缀。

══════════════════════════════════════
任务 3：逐段翻译（translation）
══════════════════════════════════════
按原文段落逐段翻译成自然流畅的中文，意译为主。

══════════════════════════════════════
仅输出 JSON，无说明文字：
══════════════════════════════════════
{{
  "vocabulary": [
    {{"id":"v1","word":"公表","reading":"こうひょう","pos":"名詞・サ変","meaning":"公布","example":"結果を公表した。"}}
  ],
  "grammar": [
    {{"id":"g1","pattern":"に基づいて","connection":"名詞＋に基づいて","explanation":"以……为根据","example":"調査結果に基づいて判断する。"}}
  ],
  "translation": [
    {{"jp":"段落原文","zh":"中文翻译"}}
  ]
}}

⚠️ 关键约束：
1. 不要标注人名、地名、机构名、职位名作为词汇
2. 语法只标 N1/N2 句型或常用复合句型（4-12字），禁止标助词/活用/语态
3. 词汇 8-15 个，语法 5-10 个"""

        print(f"[analyze] prompt 长度: {len(prompt)} 字符", flush=True)

        response = client.chat.completions.create(
            model='deepseek-chat',
            messages=[
                {
                    'role': 'system',
                    'content': '你是专业日语教学助手，只输出纯 JSON，禁止输出代码块标记（```）或说明文字。'
                },
                {'role': 'user', 'content': prompt}
            ],
            temperature=0.1,
            max_tokens=8000,
            response_format={'type': 'json_object'},
        )

        raw = response.choices[0].message.content.strip()
        raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
        raw = re.sub(r'```\s*$', '', raw, flags=re.MULTILINE)
        raw = raw.strip()

        result = json.loads(raw)
        result.setdefault('vocabulary', [])
        result.setdefault('grammar', [])
        result.setdefault('translation', [])

        # ── 在 Python 端构建 annotated_html ──
        result['annotated_html'] = _build_annotated_html(
            text, result['vocabulary'], result['grammar']
        )

        print(f"[analyze] vocab={len(result['vocabulary'])}, "
              f"grammar={len(result['grammar'])}, "
              f"translation={len(result['translation'])}",
              flush=True)

        return jsonify(result)

    except json.JSONDecodeError as e:
        return jsonify({'error': f'AI 返回格式解析失败，请重试（{str(e)}）'}), 500
    except Exception as e:
        msg = str(e)
        if any(k in msg for k in ('auth', 'Auth', '401', 'invalid_api_key', 'API key')):
            return jsonify({'error': 'API Key 无效，请检查环境变量 DEEPSEEK_API_KEY'}), 401
        if '402' in msg or 'Insufficient Balance' in msg:
            return jsonify({'error': 'AI 服务额度暂时用完了，请稍后再试'}), 503
        print(f"[analyze] error: {msg}", flush=True)  # 详细错误只写日志，不返回给用户
        return jsonify({'error': '分析失败，请稍后重试'}), 500


def _build_annotated_html(text, vocab_list, grammar_list):
    """在 Python 端根据 vocab/grammar 列表给原文加 <mark> 标注。

    策略：
    1. 先标 grammar（短功能语素），再标 vocab（长实词）
    2. 同一次匹配避免标到已有 <mark> 内部
    3. 每种模式至多标前 3 次出现
    4. 原文和 AI 返回的内容都先做 HTML 转义，防止注入（XSS）
    """
    # 原文转义后，换行 → <br>
    html = htmllib.escape(text, quote=False).replace('\n', '<br>')

    # 收集所有标注：(pattern, class, data-id, data-tip, priority)
    # priority 越小越先标：grammar=0, vocab=1
    annotations = []
    for v in vocab_list:
        word = v.get('word', '').strip()
        if word:
            annotations.append((word, 'vocab', v.get('id', ''), v.get('meaning', ''), 1))
    for g in grammar_list:
        pat = g.get('pattern', '').strip().lstrip('〜')
        if pat:
            annotations.append((pat, 'grammar', g.get('id', ''), g.get('explanation', ''), 0))

    # 按优先级排序（grammar 先），同优先级按长度降序（避免短串先匹配破坏长串）
    annotations.sort(key=lambda x: (x[4], -len(x[0])))

    for pattern, cls, aid, tip, _pri in annotations:
        pattern = htmllib.escape(pattern, quote=False)
        aid, tip = htmllib.escape(aid), htmllib.escape(tip)
        # 短模式限制出现次数，避免匹配到词内部
        max_occur = 1 if len(pattern) <= 1 else (2 if len(pattern) <= 2 else 3)
        count = 0
        idx = 0
        while count < max_occur:
            m = re.search(re.escape(pattern), html[idx:])
            if not m:
                break
            pos = idx + m.start()

            # 检查是否在已有 <mark> 内部
            before = html[:pos]
            if before.count('<mark ') > before.count('</mark>'):
                idx = pos + 1  # 在 mark 内，跳过此位置
                continue

            mark_tag = f"<mark class='{cls}' data-id='{aid}' data-tip='{tip}'>{pattern}</mark>"
            html = html[:pos] + mark_tag + html[pos + len(pattern):]
            idx = pos + len(mark_tag)  # 跳过刚插入的标签
            count += 1

    return html


if __name__ == '__main__':
    port = int(os.environ.get('FLASK_PORT', 5000))
    app.run(debug=True, use_reloader=False, port=port)
