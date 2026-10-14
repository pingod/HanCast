/*
 * B 站规则包 —— 官方包，覆盖内置 Python 实现退役后的 B 站解析
 *
 * 线路优先级 iOS APP → TV APP → Web HTML5，任一线路拿到 durl 即交付。
 * APP 签名是公开资料里的 appkey/appsec 方案：参数排序拼 query，再 md5(query + appsec)。
 * 反爬 Cookie（buvid3/buvid4）落盘缓存 1 小时，跨次解析复用，省掉每次一趟 finger/spi。
 * 画质：默认投最高档（qn=80），并按接口回报的 accept_quality 告诉客户端还能切哪些档。
 */

var REFERER = 'https://www.bilibili.com/';
var COOKIE_TTL_MS = 3600 * 1000;
var FALLBACK_BUVID3 =
  'FE6D3664-927F-F75B-B7D4-733E5D4B263F69428infoc';

// 档位 id 直接用 B 站自己的 qn：切换画质时客户端原样回传，不用再做一次映射
var QN_LABELS = {
  '16': '360P 流畅',
  '32': '480P 清晰',
  '64': '720P 超清',
  '80': '1080P 高清',
  '116': '1080P 高帧率',
  '127': '4K 超清'
};
var DEFAULT_QN = '80';

function wantedQn(ctx) {
  var q = String((ctx && ctx.quality) || '').trim();
  // 只认表里的 qn，用户传什么都不会污染签名 query
  return QN_LABELS[q] ? q : DEFAULT_QN;
}

function qnLabel(q) {
  var s = String(q);
  return QN_LABELS[s] || (s + 'P');
}

/** 本次能给的档位表（从高到低）+ 实际交付的档位 */
function qualityInfo(data, deliveredQn) {
  var acc = items(data, 'data.accept_quality');
  var list = [];
  for (var i = 0; i < acc.length; i++) {
    var q = String(acc[i]);
    if (QN_LABELS[q]) list.push({ id: q, label: QN_LABELS[q] });
  }
  list.sort(function (a, b) { return Number(b.id) - Number(a.id); });
  var got = String(int(data, 'data.quality', 0) || deliveredQn || '');
  if (!got) return { qualities: list };
  // 接口没给 accept_quality 时，至少告诉客户端这次实际是什么档
  if (!list.length) return { qualities: [], quality: got };
  return { qualities: list, quality: got };
}

// appkey/appsec 为公开资料（bilibili-API-collect）
var APP_LINES = [
  { key: 'ios', appkey: 'YvirImLGlLANCLvM',
    appsec: 'JNlZNgfNGKZEpaDTkCdPQVXntXhuiJEM',
    ua: 'Bilibili/8.0.0 (bbcallen@gmail.com)', platform: 'ios' },
  { key: 'tv', appkey: '4409e2ce8ffd12b8',
    appsec: '59b43e04ad6965f34319062b478f83dd',
    ua: 'Bilibili Freedoooooom/MOD', platform: 'android' }
];

function appSign(params, appkey, appsec) {
  var keys = Object.keys(params);
  keys.push('appkey');
  keys.sort();
  var parts = [];
  for (var i = 0; i < keys.length; i++) {
    var k = keys[i];
    parts.push(k + '=' + encodeURIComponent(k === 'appkey' ? appkey : params[k]));
  }
  var query = parts.join('&');
  return query + '&sign=' + host.md5(query + appsec);
}

function antiCrawlCookie() {
  var cached = host.readCache('cookie');
  if (cached && host.cacheAge('cookie') >= 0 &&
      host.cacheAge('cookie') < COOKIE_TTL_MS) {
    return cached;
  }
  var buvid3 = FALLBACK_BUVID3, buvid4 = '';
  try {
    var spi = host.getJson('https://api.bilibili.com/x/frontend/finger/spi');
    if (str(spi, 'data.b_3')) buvid3 = str(spi, 'data.b_3');
    if (str(spi, 'data.b_4')) buvid4 = str(spi, 'data.b_4');
  } catch (e) {
    host.log('finger/spi 失败, 用 fallback buvid3: ' + e);
  }
  var cookie = 'buvid3=' + buvid3 + (buvid4 ? '; buvid4=' + buvid4 : '');
  host.writeCache('cookie', cookie);
  return cookie;
}

function extractBvid(url) {
  var m = /(BV[a-zA-Z0-9]{10})/.exec(url);
  if (m) return m[1];
  var short = /b23\.tv\/(\w+)/.exec(url);
  if (short) {
    try {
      // 短链跟到底再取 BV，跳转目标本身就是播放页
      var finalUrl = host.finalUrl('https://b23.tv/' + short[1]);
      var m2 = /(BV[a-zA-Z0-9]{10})/.exec(finalUrl);
      if (m2) return m2[1];
    } catch (e) {
      host.log('b23.tv 短链解析失败: ' + e);
    }
  }
  return '';
}

function pickDurl(data) {
  var durl = items(data, 'data.durl');
  return durl.length ? str(durl[0], 'url') : '';
}

registerRule({
  id: 'bilibili-playurl',
  name: 'B 站取流（APP 签名 → HTML5）',
  hosts: ['bilibili.com', 'bilibili.tv', 'b23.tv'],
  stage: 'url',

  resolve: function (ctx) {
    var bvid = extractBvid(ctx.url);
    if (!bvid) throw new Error('无法从链接提取 BV 号');
    var cookie = antiCrawlCookie();

    var info = host.getJson(
      'https://api.bilibili.com/x/web-interface/view?bvid=' + bvid,
      { headers: { Cookie: cookie, Referer: REFERER } });
    if (int(info, 'code') !== 0) {
      throw new Error('B站API错误: ' + (str(info, 'message') || '未知'));
    }
    var cid = str(info, 'data.cid');
    var title = str(info, 'data.title') || bvid;
    var cover = str(info, 'data.pic');

    var ts = String(Math.floor(Date.now() / 1000));
    var qn = wantedQn(ctx);
    for (var i = 0; i < APP_LINES.length; i++) {
      var line = APP_LINES[i];
      try {
        var signed = appSign({
          bvid: bvid, cid: cid, qn: qn, fnval: '1', fnver: '0',
          fourk: '1', platform: line.platform, ts: ts
        }, line.appkey, line.appsec);
        var data = host.getJson(
          'https://api.bilibili.com/x/player/playurl?' + signed,
          { headers: { 'User-Agent': line.ua, Cookie: cookie, Referer: REFERER } });
        var url = pickDurl(data);
        if (int(data, 'code') === 0 && url) {
          host.log('取流成功 (线路: ' + line.key + ', 档位: ' + qn + ')');
          var qi = qualityInfo(data, qn);
          return { url: url, title: title, thumbnail: cover,
                   qualities: qi.qualities, quality: qi.quality,
                   headers: { Referer: REFERER, Cookie: cookie } };
        }
      } catch (e) {
        host.log('线路 ' + line.key + ' 失败: ' + e);
      }
    }

    // 兜底：Web HTML5 直链（无需签名）
    var web = host.getJson(
      'https://api.bilibili.com/x/player/playurl?bvid=' + bvid +
      '&cid=' + encodeURIComponent(cid) + '&qn=' + qn + '&fnval=0&platform=html5',
      { headers: { Cookie: cookie, Referer: REFERER } });
    var webUrl = pickDurl(web);
    if (!webUrl) throw new Error('所有取流线路均失败，请稍后重试');
    host.log('取流成功 (线路: web html5, 档位: ' + qn + ')');
    var webQi = qualityInfo(web, qn);
    return { url: webUrl, title: title, thumbnail: cover,
             qualities: webQi.qualities, quality: webQi.quality,
             headers: { Referer: REFERER, Cookie: cookie } };
  }
});
