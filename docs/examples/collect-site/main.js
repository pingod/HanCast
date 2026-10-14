/**
 * 示例插件：采集/影视站的三段式解析。
 *
 * 页面形态（采集站模板的典型布局）：
 *   1. 播放页正文里有一段 `var player_aaaa = {"url":"\u002Fapi\u002Fm3u8?..."}` 的配置；
 *   2. 配置里的接口地址是站内相对路径，返回一个 m3u8 播放列表；
 *   3. 列表要求 Referer 是站内页面，否则 403。
 *
 * 本例演示：ctx.body 正则提取 → absUrl 补全 → host.getJson 二次请求 →
 * 返回值里带 headers 让交付链路自动挂代理。打成 zip（manifest.json + main.js）
 * 从设置页导入即可。
 */
registerRule({
  name: 'collect',
  hosts: ['v.example.com', 'www.example.com'],
  stage: 'page',
  priority: 5,
  resolve: function (ctx) {
    // ① 配置段：body 已归一化 \/ 与 &amp;，一条正则同时吃 JS 转义和 HTML 实体两种写法
    var m = /player_aaaa\s*=\s*(\{.*?\})\s*;?\s*</.exec(ctx.body || '')
    if (!m) throw new Error('页面里没有 player_aaaa 配置段（模板改版或不是播放页）')
    var cfg = parseJson(m[1])

    // ② 接口地址：相对路径按页面地址补全；支持集数参数
    var api = absUrl(ctx.url, str(cfg, 'url'))
    if (!api) throw new Error('配置段里没有 url 字段')

    // ③ 接口通常直接回 m3u8 文本；若是再包一层 JSON（{code:1, data:{url:…}}）就拆一层
    var text = host.getString(api, { referer: ctx.url })
    var play = api
    if (text.charAt(0) !== '#') {
      // 不是 m3u8 文本 → 当 JSON 试一次，取不到就原样交回去让管线自己判
      try {
        play = absUrl(api, str(parseJson(text), 'data.url'))
      } catch (e) { /* 保留 api 原样 */ }
    }

    host.log('collect: ' + ctx.url + ' -> ' + play)

    // ④ 防盗链： Referer 交给交付层；验证不过会自动改走本机代理
    return {
      url: play,
      title: str(cfg, 'title') || '',
      headers: { Referer: ctx.url },
    }
  },
})

/**
 * 第二条规则演示另一种姿势：列表页进来时先抓「当前播放」的直链，
 * 不经过接口。同一插件里多条规则按 priority 依次尝试。
 */
registerRule({
  name: 'direct',
  hosts: ['www.example.com'],
  stage: 'page',
  priority: 6,
  resolve: function (ctx) {
    var nodes = host.select(ctx.body, 'video source', ctx.url)
    for (var i = 0; i < nodes.length; i++) {
      var src = nodes[i].abs && nodes[i].abs.src
      if (src) return { url: src, headers: { Referer: ctx.url } }
    }
    throw new Error('页面里没有 <video><source>')
  },
})
