/**
 * 浏览器通道验收包：不是给站点用的规则，是给「渲染通道本身」出的判据。
 *
 * 配套的假站点在 tools/browser-channel-livecheck/serve.py：播放地址只由页面 JS
 * 现挂（先 fetch 一个签名接口再往 video.src 上赋值），所以 HTTP 抓页永远拿不到，
 * 只有真渲染才嗅得出来。这条规则把整条链的结果原样打进应用日志，判据看日志：
 *
 *   LIVECHECK sniff=[…master.m3u8…]      ← 钩子 + 回收
 *   LIVECHECK render=… ua=… elapsed=…    ← HTML 正文 + UA 同源 + 耗时
 *   LIVECHECK cookies=… httponly=1       ← WebView 罐子 → 解析会话（HttpOnly 只有这条路能拿到）
 *
 * 通道不可用（参数关掉、旧宿主、超时熔断）时它如实抛错，日志里就是那句人话原因，
 * 不会假装「未找到播放地址」。
 */
registerRule({
  name: 'browser-livecheck',
  hosts: ['127.0.0.1', 'localhost'],
  stage: 'page',
  priority: 4,
  resolve: function (ctx) {
    var base = ctx.url.slice(0, ctx.url.indexOf('/', ctx.url.indexOf('//') + 2)) + '/'

    // ① 只要地址：走的是 sniff（html:false，宿主不该回传整页正文）
    var sniffed = host.sniff(base, { settle_ms: 1500 })
    host.log('LIVECHECK sniff=' + JSON.stringify(sniffed))

    // ② 要正文：验 render 的返回形状与 UA
    var r = host.render(base, { settle_ms: 1200 })
    host.log('LIVECHECK render title=' + r.title + ' htmlLen=' + (r["html"] || '').length +
             ' ua=' + r.ua + ' elapsedMs=' + r.elapsedMs)
    host.log('LIVECHECK uaMatchesHost=' + (r.ua === host.userAgent(false)))

    // ③ Cookie 双罐同步：hcvisitor 是 HttpOnly，HTTP 侧抓不到，
    //    出现在这里才说明 WebView→会话这条搬运链路真的走通了
    var jar = host.cookies(base)
    host.log('LIVECHECK cookies=' + jar + ' httponly=' +
             (jar.indexOf('hcvisitor=webview-only') >= 0 ? 1 : 0))

    var media = r.media[0] || sniffed[0] || ''
    if (!media) throw new Error('浏览器通道没嗅到播放地址（渲染链路哪一段没通？）')
    return { url: media, title: '浏览器通道验收' }
  },
})
