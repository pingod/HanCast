/*
 * 采集站通用规则包 —— 官方包，兜底作用域
 *
 * 「采集站」是同一个 CMS 程序换皮开一百个域名（绿色影视、66dpw 这类播放页），
 * 所以这里不写域名清单，`hosts: ["*"]`：站点专属规则全都拿不下时，注册表
 * （media/rules/base.py 的 matching）才把这类规则排到最后试。
 * 命中了就直接给出地址；没命中照样抛异常，让路给后面的通用正文提取，
 * 抛异常在管线里等于「这条不适用」，不是「这个地址坏了」。
 *
 * 两条规则覆盖两种真实在跑的播放器写法（都是从解析失败台账里的地址取证的）：
 *
 *   ① 苹果CMS / maccms —— 正文里 `player_aaaa = {...}`，播放地址在 `url` 字段，
 *      按 `encrypt` 编码：1 = URL 转义，2 = base64(URI),0/缺省 = 原文。
 *   ② myui 模板 + 外挂 share 播放器 —— 正文里 `var now=base64decode("…")`
 *      给出分享播放器页，该页再用 `var main = "/20240930/…/index.m3u8?sign=…"`
 *      把相对路径拼在自己的 origin 上（正则抓绝对地址永远抓不到这种相对写法）。
 */

var STREAM_EXT = /\.(m3u8|mp4|mkv|flv|mov|webm|ts|mp3|m4a)(\?|#|$)/i;

/* 从 from/encrypt 这类 JSON 字段里取值：先按整对象 parse，取不到再退回逐字段 */
function fieldOf(obj, key) {
  if (obj && typeof obj === 'object' && obj[key] !== undefined) return String(obj[key]);
  return '';
}

/* 正文里第一段 player_aaaa={…} 的对象文本（花括号配对，跳过字符串与转义） */
function jsonObjectAt(text, openIdx) {
  var depth = 0, inStr = false, esc = false;
  for (var i = openIdx; i < text.length && i - openIdx < 60000; i++) {
    var c = text.charAt(i);
    if (inStr) {
      if (esc) { esc = false; }
      else if (c === '\\') { esc = true; }
      else if (c === '"') { inStr = false; }
      continue;
    }
    if (c === '"') { inStr = true; }
    else if (c === '{') { depth++; }
    else if (c === '}') {
      depth--;
      if (depth === 0) return text.slice(openIdx, i + 1);
    }
  }
  return '';
}

registerRule({
  id: 'cms-player-aaaa',
  name: '苹果CMS 播放页（player_aaaa）',
  hosts: ['*'],
  stage: 'page',
  priority: 8,

  resolve: function (ctx) {
    var body = ctx.body || '';
    var at = body.indexOf('player_aaaa');
    if (at < 0) throw new Error('正文里没有 player_aaaa');
    var open = body.indexOf('{', at);
    if (open < 0) throw new Error('player_aaaa 后面不是对象');
    var text = jsonObjectAt(body, open);
    if (!text) throw new Error('player_aaaa 的对象文本取不全');

    // parse 失败（模板把 JS 对象而不是 JSON 写进正文）时退回逐字段取值
    var obj = null;
    try { obj = JSON.parse(text); } catch (e) { }
    function pick(key) {
      if (obj) {
        var v = obj[key];
        return v === undefined || v === null ? '' : String(v);
      }
      var m = new RegExp('["\']' + key + '["\']\\s*:\\s*["\']([^"\']*)["\']').exec(text);
      return m ? m[1] : '';
    }

    var raw = pick('url');
    if (!raw) throw new Error('player_aaaa 没有 url 字段');
    var enc = pick('encrypt') || '0';
    var url = raw;
    if (enc === '1') url = unescape(raw);                  // URI 转义
    else if (enc === '2') url = unescape(host.base64Decode(raw));
    if (!/^https?:\/\//i.test(url)) url = absUrl(ctx.base, url);
    if (!url) throw new Error('player_aaaa 的 url 解出来是空');

    var title = pick('title') || pick('name') || '';
    if (!title) {
      var nodes = host.select(body, 'title', ctx.base);
      title = nodes.length ? nodes[0].text : '';
    }
    // 不是媒体地址就是「播放器页」，抓开它再找一次（不少站把 m3u8 放在那一页里）
    if (!STREAM_EXT.test(url)) {
      var inner = host.getHtml(url, { referer: ctx.url });
      var mm = /(https?:\/\/[^\s'"<>]+?\.(?:m3u8|mp4|flv|mkv|ts)(?:\?[^\s'"<>]*)?)/i.exec(inner);
      if (!mm) throw new Error('player_aaaa 指向的页面里没有流地址');
      return { url: mm[1], title: title, base: url };
    }
    return { url: url, title: title, base: ctx.base };
  }
});

registerRule({
  id: 'cms-share-player',
  name: 'myui 模板的 share 播放器页',
  hosts: ['*'],
  stage: 'page',
  priority: 8,

  resolve: function (ctx) {
    var body = ctx.body || '';
    var m = /\bnow\s*=\s*base64decode\(\s*["']([^"']+)["']\s*\)/.exec(body) ||
            /\bnow\s*=\s*["'](https?:\/\/[^"']+)/.exec(body);
    if (!m) throw new Error('正文里没有 var now 播放器地址');
    var page = /^https?:/i.test(m[1]) ? m[1] : host.base64Decode(m[1]);
    if (!/^https?:\/\//i.test(page)) throw new Error('var now 解出来不是地址');

    var html = host.getHtml(page, { referer: ctx.url });
    // 播放器页给的是相对路径（\/20240930\/…\/index.m3u8?sign=…），必须拼在它自己的 origin 上
    var mm = /\bmain\s*=\s*["']([^"']+)["']/.exec(html);
    if (!mm) throw new Error('播放器页里没有 var main 地址');
    var rel = '';
    try { rel = JSON.parse('"' + mm[1] + '"'); } catch (e) { rel = mm[1]; }
    if (!/\.(m3u8|mp4|flv|mkv|ts)(\?|#|$)/i.test(rel)) {
      throw new Error('var main 不是媒体地址: ' + rel.slice(0, 60));
    }
    var nodes = host.select(body, 'title', ctx.base);
    return { url: absUrl(page, rel), title: nodes.length ? nodes[0].text : '', base: page };
  }
});
