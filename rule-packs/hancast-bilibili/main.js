/*
 * B 站规则包 —— 官方包，覆盖内置 Python 实现退役后的 B 站解析
 *
 * 线路优先级 iOS APP → TV APP → Web HTML5，任一线路拿到 durl 即交付。
 * APP 签名是公开资料里的 appkey/appsec 方案：参数排序拼 query，再 md5(query + appsec)。
 * 反爬 Cookie（buvid3/buvid4）落盘缓存 1 小时，跨次解析复用，省掉每次一趟 finger/spi。
 */

var REFERER = 'https://www.bilibili.com/';
var COOKIE_TTL_MS = 3600 * 1000;
var FALLBACK_BUVID3 =
  'FE6D3664-927F-F75B-B7D4-733E5D4B263F69428infoc';

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
    for (var i = 0; i < APP_LINES.length; i++) {
      var line = APP_LINES[i];
      try {
        var signed = appSign({
          bvid: bvid, cid: cid, qn: '80', fnval: '1', fnver: '0',
          fourk: '1', platform: line.platform, ts: ts
        }, line.appkey, line.appsec);
        var data = host.getJson(
          'https://api.bilibili.com/x/player/playurl?' + signed,
          { headers: { 'User-Agent': line.ua, Cookie: cookie, Referer: REFERER } });
        var url = pickDurl(data);
        if (int(data, 'code') === 0 && url) {
          host.log('取流成功 (线路: ' + line.key + ')');
          return { url: url, title: title, thumbnail: cover,
                   headers: { Referer: REFERER, Cookie: cookie } };
        }
      } catch (e) {
        host.log('线路 ' + line.key + ' 失败: ' + e);
      }
    }

    // 兜底：Web HTML5 直链（无需签名）
    var web = host.getJson(
      'https://api.bilibili.com/x/player/playurl?bvid=' + bvid +
      '&cid=' + encodeURIComponent(cid) + '&qn=80&fnval=0&platform=html5',
      { headers: { Cookie: cookie, Referer: REFERER } });
    var webUrl = pickDurl(web);
    if (!webUrl) throw new Error('所有取流线路均失败，请稍后重试');
    host.log('取流成功 (线路: web html5)');
    return { url: webUrl, title: title, thumbnail: cover,
             headers: { Referer: REFERER, Cookie: cookie } };
  }
});
