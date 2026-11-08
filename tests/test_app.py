import json
from types import SimpleNamespace

import openai
import pytest

import app as app_module


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, 'CACHE_FILE', str(tmp_path / 'articles.json'))
    monkeypatch.setattr(app_module, 'OWNER_PASSWORD', '')
    app_module.app.config['TESTING'] = True
    app_module._ip_hits.clear()
    app_module._daily.update(date='', count=0)
    app_module._login_hits.clear()
    app_module._login_fails.clear()
    app_module._fulltext_cache.clear()
    return app_module.app.test_client()


ARTICLE = {
    'id': 'abc123', 'title': 'テスト記事', 'url': 'https://www.asahi.com/articles/X.html',
    'time': '10月19日 19:00', 'summary': '東京都で訓練が行われた',
}


def test_visitors_page_removed(client):
    assert client.get('/visitors').status_code == 404


def _today():
    return app_module.datetime.now(app_module.JST).strftime('%Y-%m-%d')


def test_reader_shows_summary_and_source_link(client):
    app_module._save_cache({'version': app_module.CACHE_VERSION, 'date': _today(),
                            'articles': [ARTICLE]})
    page = client.get('/reader/abc123').get_data(as_text=True)
    assert ARTICLE['title'] in page
    assert ARTICLE['url'] in page
    assert json.dumps(ARTICLE['summary']) in page  # tojson 会把日文转成 \uXXXX


def test_old_cache_with_full_text_is_ignored(client):
    app_module._save_cache({'articles': [dict(ARTICLE, text='全文' * 500)]})
    assert app_module._load_cache() == {}


def test_analyze_rejects_too_long_text(client):
    res = client.post('/analyze', json={'text': 'あ' * (app_module.MAX_CHARS + 1)})
    assert res.status_code == 400


def test_annotated_html_escapes_html():
    html = app_module._build_annotated_html(
        '<script>x</script>重要な会議',
        [{'id': 'v1', 'word': '会議', 'meaning': "it's <b>"}],
        [],
    )
    assert '<script>' not in html
    assert '<b>' not in html
    assert "data-tip='it&#x27;s &lt;b&gt;'" in html
    assert "<mark class='vocab' data-id='v1'" in html


def test_analyze_with_mocked_deepseek(client, monkeypatch):
    fake = {
        'vocabulary': [{'id': 'v1', 'word': '訓練', 'meaning': '训练'}],
        'grammar': [],
        'translation': [{'jp': '東京都で訓練が行われた', 'zh': '东京都进行了训练'}],
    }

    class FakeOpenAI:
        def __init__(self, **kwargs):
            create = lambda **kw: SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(fake, ensure_ascii=False)))])
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))

    monkeypatch.setattr(app_module, 'OpenAI', FakeOpenAI)
    monkeypatch.setattr(app_module, 'DEEPSEEK_API_KEY', 'test-key')

    res = client.post('/analyze', json={'text': '東京都で訓練が行われた'})
    data = res.get_json()
    assert res.status_code == 200
    assert "<mark class='vocab' data-id='v1' data-tip='训练'>訓練</mark>" in data['annotated_html']
    assert data['translation'][0]['zh'] == '东京都进行了训练'


def test_reader_scrapes_when_cache_is_empty(client, monkeypatch):
    # Render 重启后缓存为空，直接打开文章链接也要能显示
    monkeypatch.setattr(app_module, '_scrape_asahi', lambda: [ARTICLE])
    page = client.get('/reader/abc123').get_data(as_text=True)
    assert ARTICLE['url'] in page


def test_force_refresh_has_cooldown(client, monkeypatch):
    calls = []
    monkeypatch.setattr(app_module, '_scrape_asahi', lambda: calls.append(1) or [ARTICLE])
    client.get('/api/articles')
    client.get('/api/articles?force=1')  # 刚抓过，不应再抓
    assert len(calls) == 1


def test_analyze_rate_limit(client, monkeypatch):
    monkeypatch.setattr(app_module, 'DEEPSEEK_API_KEY', 'test-key')
    monkeypatch.setattr(app_module, 'RATE_PER_IP', 2)
    app_module._ip_hits.clear()

    class BrokenOpenAI:  # 不真正调用 API
        def __init__(self, **kwargs):
            raise RuntimeError('no network in tests')

    monkeypatch.setattr(app_module, 'OpenAI', BrokenOpenAI)
    codes = [client.post('/analyze', json={'text': 'テスト'}).status_code for _ in range(3)]
    assert codes == [500, 500, 429]


# ─── DeepSeek：key、模型名、错误提示 ─────────────────────────────────────

def _fake_openai(*replies, calls=None):
    """按顺序返回 replies：字符串 = 模型输出，异常 = 抛出。"""
    replies = list(replies)

    class FakeOpenAI:
        def __init__(self, **kwargs):
            def create(**kw):
                if calls is not None:
                    calls.append(kw)
                reply = replies.pop(0)
                if isinstance(reply, Exception):
                    raise reply
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply))])
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
    return FakeOpenAI


def _status_error(cls, code, msg):
    return cls(msg, response=SimpleNamespace(request=None, status_code=code, headers={}), body=None)


GOOD_REPLY = json.dumps({'vocabulary': [], 'grammar': [], 'translation': []})


def test_api_key_is_cleaned():
    assert app_module._clean_key('  "sk-abc123"\n') == 'sk-abc123'
    assert app_module._clean_key("'sk-abc123'") == 'sk-abc123'
    assert app_module._clean_key(None) == ''


def test_default_model_is_current_deepseek_name():
    # deepseek-chat 在 2026-07-24 停用了
    assert app_module.DEEPSEEK_MODEL == 'deepseek-flash'


def test_analyze_uses_configured_model(client, monkeypatch):
    calls = []
    monkeypatch.setattr(app_module, 'OpenAI', _fake_openai(GOOD_REPLY, calls=calls))
    monkeypatch.setattr(app_module, 'DEEPSEEK_API_KEY', 'sk-test')
    monkeypatch.setattr(app_module, 'DEEPSEEK_MODEL', 'deepseek-something')
    assert client.post('/analyze', json={'text': 'テスト'}).status_code == 200
    assert calls[0]['model'] == 'deepseek-something'


def test_analyze_401_says_key_rejected(client, monkeypatch):
    err = _status_error(openai.AuthenticationError, 401, 'Authentication Fails')
    monkeypatch.setattr(app_module, 'OpenAI', _fake_openai(err))
    monkeypatch.setattr(app_module, 'DEEPSEEK_API_KEY', 'sk-test')
    res = client.post('/analyze', json={'text': 'テスト'})
    assert res.status_code == 503
    assert '401' in res.get_json()['error'] and 'DEEPSEEK_API_KEY' in res.get_json()['error']


def test_analyze_402_says_no_balance(client, monkeypatch):
    err = _status_error(openai.APIStatusError, 402, 'Insufficient Balance')
    monkeypatch.setattr(app_module, 'OpenAI', _fake_openai(err))
    monkeypatch.setattr(app_module, 'DEEPSEEK_API_KEY', 'sk-test')
    res = client.post('/analyze', json={'text': 'テスト'})
    assert res.status_code == 503 and '额度' in res.get_json()['error']


def test_analyze_400_mentions_model_name(client, monkeypatch):
    err = _status_error(openai.BadRequestError, 400, 'Model Not Exist')
    monkeypatch.setattr(app_module, 'OpenAI', _fake_openai(err))
    monkeypatch.setattr(app_module, 'DEEPSEEK_API_KEY', 'sk-test')
    res = client.post('/analyze', json={'text': 'テスト'})
    assert res.status_code == 502 and app_module.DEEPSEEK_MODEL in res.get_json()['error']


def test_analyze_retries_empty_json_once(client, monkeypatch):
    calls = []
    monkeypatch.setattr(app_module, 'OpenAI', _fake_openai('   ', GOOD_REPLY, calls=calls))
    monkeypatch.setattr(app_module, 'DEEPSEEK_API_KEY', 'sk-test')
    res = client.post('/analyze', json={'text': 'テスト'})
    assert res.status_code == 200 and len(calls) == 2


def test_analyze_ignores_malformed_lists(client, monkeypatch):
    reply = json.dumps({'vocabulary': {'oops': 1}, 'grammar': ['x', {'id': 'g1', 'pattern': 'について'}]})
    monkeypatch.setattr(app_module, 'OpenAI', _fake_openai(reply))
    monkeypatch.setattr(app_module, 'DEEPSEEK_API_KEY', 'sk-test')
    data = client.post('/analyze', json={'text': '日本について'}).get_json()
    assert data['vocabulary'] == [] and len(data['grammar']) == 1 and data['translation'] == []


# ─── 站长全文模式 ────────────────────────────────────────────────────────

FULL_TEXT = '第一段の本文です。テスト用に作った文章で、二十五文字より長くしてあります。\n\n第二段の本文です。こちらもテスト用の文章で、ある程度の長さがあります。'


def _login(client, monkeypatch, password='secret-pw'):
    monkeypatch.setattr(app_module, 'OWNER_PASSWORD', password)
    return client.post('/login', data={'password': password, 'next': '/reader/abc123'})


def _cache_article():
    app_module._save_cache({'version': app_module.CACHE_VERSION, 'date': _today(),
                            'articles': [ARTICLE]})


def test_visitor_gets_full_text(client, monkeypatch):
    monkeypatch.setattr(app_module, 'OWNER_PASSWORD', 'secret-pw')
    monkeypatch.setattr(app_module, '_get_full_text', lambda url: {'text': FULL_TEXT, 'paywalled': False})
    _cache_article()
    page = client.get('/reader/abc123').get_data(as_text=True)  # 没有登录
    assert json.dumps(FULL_TEXT) in page
    assert '下方是全文' in page


def test_visitor_sees_summary_when_full_text_fails(client, monkeypatch):
    monkeypatch.setattr(app_module, '_get_full_text', lambda url: {'text': '', 'paywalled': False})
    _cache_article()
    page = client.get('/reader/abc123').get_data(as_text=True)
    assert json.dumps(ARTICLE['summary']) in page
    assert '全文没抓到' in page


def test_owner_gets_full_text(client, monkeypatch):
    monkeypatch.setattr(app_module, '_get_full_text', lambda url: {'text': FULL_TEXT, 'paywalled': False})
    _cache_article()
    res = _login(client, monkeypatch)
    assert res.status_code == 302 and res.headers['Location'].endswith('/reader/abc123')
    page = client.get('/reader/abc123').get_data(as_text=True)
    assert json.dumps(FULL_TEXT) in page
    assert '全文模式' in page
    # 全文不写进 articles.json
    with open(app_module.CACHE_FILE, encoding='utf-8') as f:
        assert '第一段' not in f.read()


def test_wrong_password_and_login_limit(client, monkeypatch):
    monkeypatch.setattr(app_module, 'OWNER_PASSWORD', 'secret-pw')
    pages = [client.post('/login', data={'password': 'nope'}).get_data(as_text=True)
             for _ in range(app_module.LOGIN_PER_IP + 1)]
    assert '密码不对' in pages[0]
    assert '试太多次' in pages[-1]
    # 被限制后正确密码也不行
    page = client.post('/login', data={'password': 'secret-pw'}).get_data(as_text=True)
    assert '试太多次' in page


def test_login_disabled_without_password(client):
    page = client.post('/login', data={'password': ''}).get_data(as_text=True)
    assert '还没开启' in page


def test_login_redirect_stays_on_site():
    assert app_module._safe_next('/reader/x') == '/reader/x'
    for bad in ('//evil.example', 'https://evil.example', '/\\evil.example', ''):
        assert app_module._safe_next(bad) == '/'


def test_extract_asahi_body_keeps_paragraphs_and_drops_noise():
    html = """
    <html><body><article>
      <h1>見出し</h1>
      <p>第一段の本文です。テスト用に作った文章で、二十五文字より長くしてあります。</p>
      <p>短い</p>
      <p>有料会員になると会員限定の有料記事もお読みいただけます。長めの案内文です。</p>
      <p>第二段の本文です。こちらもテスト用の文章で、ある程度の長さがあります。</p>
      <p>第二段の本文です。こちらもテスト用の文章で、ある程度の長さがあります。</p>
      <p>【今ならキャンペーン価格】有料記事が2カ月間 月額100円で読み放題！詳しくはこちら</p>
      <div>この記事は有料記事です。残り1234文字</div>
    </article></body></html>
    """
    result = app_module._extract_asahi_body(html)
    assert result['text'] == FULL_TEXT
    assert result['paywalled'] is True


def test_get_full_text_caches_in_memory(client, monkeypatch):
    calls = []

    class Resp:
        status_code = 200
        text = '<article><p>' + 'テスト用の本文です。' * 5 + '</p></article>'
        encoding = None

        def raise_for_status(self):
            pass

    monkeypatch.setattr(app_module.http, 'get', lambda url, **kw: calls.append(url) or Resp())
    first = app_module._get_full_text('https://www.asahi.com/articles/X.html')
    second = app_module._get_full_text('https://www.asahi.com/articles/X.html')
    assert first['text'] and first == second and len(calls) == 1


# ─── 页面 ────────────────────────────────────────────────────────────────

def test_index_has_bookmarklet_and_login_link(client):
    page = client.get('/').get_data(as_text=True)
    assert 'id="bmLink"' in page and '/static/jnr.js' in page
    assert 'href="/login"' in page


def test_static_js_is_served(client):
    res = client.get('/static/jnr.js')
    assert res.status_code == 200 and b'jnrBookmarklet' in res.data


def test_reader_knows_max_chars_and_has_no_maxlength(client):
    page = client.get('/reader').get_data(as_text=True)
    assert f'const MAX_CHARS = {app_module.MAX_CHARS};' in page
    assert 'maxlength' not in page
