/*
 * Japanese News Reader — 书签工具 + 阅读页共用的小函数
 *
 * jnrBookmarklet(origin) 会被转成书签（javascript: 链接），在新闻网站上运行，
 * 所以它必须自给自足：不能调用本文件里的其他函数；里面只能用块注释、不能用双斜线注释，
 * 每句都写分号（转成书签时换行会被去掉）。
 */
(function (root) {
  'use strict';

  /* ── 书签：在新闻页面上运行，把正文送到阅读页 ─────────────────────── */
  function jnrBookmarklet(origin) {
    var LIMIT = 10000; /* 最多带多少字，URL 太长有的浏览器打不开 */
    var NOISE = /有料会員になると|会員限定の有料記事|お気に入りのニュース|Googleで優先的|無断で転載|著作権は|権利を保有|あわせて読みたい|関連記事|シェアする|今すぐ「朝日新聞」|The Asahi Shimbun/;
    var SKIP = 'nav,aside,footer,header,figure,figcaption,form,button,script,style,noscript,[hidden],[aria-hidden="true"]';
    var lines = function (s) {
      return String(s || '').split(/\n+/).map(function (l) {
        return l.replace(/^[\s　]+|[\s　]+$/g, '');
      }).filter(Boolean);
    };

    /* 1) 先选中了文字：只送选中的部分 */
    var sel = window.getSelection ? String(window.getSelection()) : '';
    var paras = lines(sel);
    var fromSelection = paras.join('').length >= 10;

    /* 2) 没选中：自动找正文 */
    if (!fromSelection) {
      /* 正文段落：不在导航/图片说明/页脚里，至少 15 字（去掉「広告」、署名），也不是一串链接 */
      var good = function (p) {
        if (p.closest(SKIP)) { return false; }
        var t = (p.innerText || '').trim();
        if (t.length < 15) { return false; }
        var linked = Array.prototype.reduce.call(p.querySelectorAll('a'), function (n, a) {
          return n + (a.innerText || '').length;
        }, 0);
        return !p.closest('a') && linked < t.length / 2;
      };
      var goodIn = function (el) { return Array.prototype.filter.call(el.querySelectorAll('p'), good); };
      var size = function (el) {
        return goodIn(el).reduce(function (n, p) { return n + p.innerText.trim().length; }, 0);
      };

      /* 大多数新闻网站把正文放在 <article> 里；有好几个就选正文最多的 */
      var root = null, rootSize = 0;
      Array.prototype.forEach.call(document.querySelectorAll('article, [role="article"]'), function (a) {
        var n = size(a);
        if (n > rootSize) { root = a; rootSize = n; }
      });

      /* 没有 <article>：从直接装着最多段落的元素出发，往上找，直到再往上（看 3 层）也多不出正文 */
      if (!root || rootSize < 100) {
        var score = new Map(), top = 0;
        root = null;
        Array.prototype.filter.call(document.querySelectorAll('p'), good).forEach(function (p) {
          var e = p.parentElement;
          var v = (score.get(e) || 0) + p.innerText.trim().length;
          score.set(e, v);
          if (v > top) { top = v; root = e; }
        });
        if (root) {
          rootSize = size(root);
          var moved = true;
          while (moved) {
            moved = false;
            var up = root.parentElement;
            for (var k = 0; k < 3 && up && up !== document.body && up !== document.documentElement; k++) {
              var n = size(up);
              if (n >= rootSize * 1.2) { root = up; rootSize = n; moved = true; break; }
              up = up.parentElement;
            }
          }
        }
      }

      paras = [];
      if (root) {
        var seen = {};
        goodIn(root).forEach(function (p) {
          lines(p.innerText).forEach(function (l) {
            if (!NOISE.test(l) && !seen[l]) { seen[l] = 1; paras.push(l); }
          });
        });
      }
    }

    var text = '', truncated = false;
    for (var i = 0; i < paras.length; i++) {
      var next = text ? text + '\n\n' + paras[i] : paras[i];
      if (next.length > LIMIT) { truncated = true; break; }
      text = next;
    }
    if (text.length < 10) {
      alert('没找到正文。可以先选中想分析的段落，再点一次书签。');
      return;
    }

    var h1 = document.querySelector('h1');
    var og = document.querySelector('meta[property="og:title"]');
    var title = (h1 && h1.innerText.trim()) || (og && og.content) || document.title || '';

    /* UTF-8 → base64url，比 encodeURIComponent 短很多 */
    var json = JSON.stringify({ v: 1, title: title.slice(0, 300), url: location.href, text: text, truncated: truncated });
    var bytes = new TextEncoder().encode(json), bin = '';
    for (var j = 0; j < bytes.length; j += 0x8000) {
      bin += String.fromCharCode.apply(null, bytes.subarray(j, j + 0x8000));
    }
    var b64 = btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    var target = origin + '/reader#import=' + b64;
    var w = window.open(target, '_blank');
    if (!w) { location.href = target; }
  }

  /* 把上面的函数做成 javascript: 链接（拖到书签栏或复制代码） */
  function bookmarkletHref(origin) {
    var code = '(' + jnrBookmarklet.toString() + ')(' + JSON.stringify(origin) + ')';
    code = code.replace(/\s*\n\s*/g, ' ');
    return 'javascript:' + encodeURIComponent(code);
  }

  /* ── 阅读页：读出书签带来的内容（URL 的 # 后面，不会发到服务器） ───── */
  function decodeImport(hash) {
    var m = /^#import=([A-Za-z0-9_-]+)$/.exec(hash || '');
    if (!m) { return null; }
    try {
      var b64 = m[1].replace(/-/g, '+').replace(/_/g, '/');
      while (b64.length % 4) { b64 += '='; }
      var bin = atob(b64);
      var bytes = new Uint8Array(bin.length);
      for (var i = 0; i < bin.length; i++) { bytes[i] = bin.charCodeAt(i); }
      var data = JSON.parse(new TextDecoder().decode(bytes));
      if (!data || typeof data.text !== 'string' || !data.text.trim()) { return null; }
      var url = typeof data.url === 'string' ? data.url : '';
      if (!/^https?:\/\//i.test(url) || url.length > 2000) { url = ''; }
      return {
        title: typeof data.title === 'string' ? data.title.slice(0, 300) : '',
        url: url,
        text: data.text.slice(0, 20000),
        truncated: data.truncated === true,
      };
    } catch (e) {
      return null;
    }
  }

  /* ── 长文章分段：按段落装箱，每段不超过 max 字 ─────────────────────── */
  function splitParts(text, max) {
    text = String(text || '').trim();
    if (text.length <= max) { return text ? [text] : []; }
    var sep = /\n[\s　]*\n/.test(text) ? '\n\n' : '\n';
    var paras = text.split(/\n+/).map(function (s) { return s.trim(); }).filter(Boolean);
    var parts = [], cur = '';
    var flush = function () { if (cur) { parts.push(cur); cur = ''; } };
    paras.forEach(function (p) {
      while (p.length > max) {
        /* 一个段落本身就太长：在后半部分最后一个「。」处切开 */
        var cut = p.lastIndexOf('。', max - 1);
        cut = cut >= max / 2 ? cut + 1 : max;
        flush();
        parts.push(p.slice(0, cut));
        p = p.slice(cut).trim();
      }
      if (!p) { return; }
      if (cur && cur.length + sep.length + p.length > max) { flush(); }
      cur = cur ? cur + sep + p : p;
    });
    flush();
    return parts;
  }

  var api = {
    jnrBookmarklet: jnrBookmarklet,
    bookmarkletHref: bookmarkletHref,
    decodeImport: decodeImport,
    splitParts: splitParts,
  };
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  } else {
    root.JNR = api;
  }
})(this);
