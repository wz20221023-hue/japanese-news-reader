import json
from types import SimpleNamespace

import pytest

import app as app_module


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, 'CACHE_FILE', str(tmp_path / 'articles.json'))
    app_module.app.config['TESTING'] = True
    app_module._ip_hits.clear()
    app_module._daily.update(date='', count=0)
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
