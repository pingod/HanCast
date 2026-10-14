/*
 * HanCast 解析规则包 —— 模板 / 用法样例
 *
 * 一个包 = manifest.json + 若干 .js；脚本里每条 registerRule({...}) 就是一条规则。
 * 打包：把这两三个文件压成 zip（manifest 与 js 同级，或整体放进一层目录），
 * 在「设置 → 解析插件」里选 ZIP 文件或填 URL 导入，装好即用，改完热生效。
 *
 * resolve(ctx) 的入参（都是字符串）：
 *   ctx.url    用户粘贴后提取出的候选地址（本条规则命中的那一条）
 *   ctx.base   同 ctx.url，作相对地址基准
 *   ctx.host   小写主机名，如 'weibo.com'
 *   ctx.scheme 'http' | 'https'
 *   ctx.path   路径，如 '/status/5099...'
 *   ctx.body   页面正文（仅 stage:'page' 且管线已抓到正文时非空；已归一化 \/ 与 \u002F）
 *   ctx.quality 用户点名的画质档位 id（首次解析是空串）——见下面 qualities 契约
 *
 * resolve 的返回：
 *   'https://...'                                  最简：直接给播放地址
 *   { url, headers?, title?, base?, thumbnail?,
 *     qualities?, quality? }                       完整：headers 是取流必需的头
 *                                                    （Referer / Cookie 等），会在
 *                                                    改走本机代理时带上；base 用作 Referer
 *   null / undefined                               「这条不适用」——管线继续往下让路
 * 抛异常也算让路（不会把站点判死），异常文本进日志。
 *
 * 画质档位（可选）：qualities: [{id, label}] 给界面当选项，quality 填本次**实际交付**的档。
 * id 是「重解析的钥匙」不是地址——切换画质时程序拿同一条来源地址 + 这个 id 再跑一次
 * resolve，所以 id 要能重新换算成站点的清晰度参数；直链带时效签名，存下来必过期。
 * ctx.quality 是用户可控字符串，先白名单校验再用，脏值别拼进签名 query。
 *
 * stage：'url' = 拿到链接就能解析；'page' = 必须先有正文。
 * priority：4~8，数字小先试；管线里只有规则包规则与声明式 JSON 规则两种来源，
 *           包规则天然排在声明式之前。
 */

registerRule({
  id: 'template-direct-url',
  name: '示例：正文里找直链',
  hosts: ['hancast-template.invalid'],   // 保留 TLD，真实世界解析不到，不会误伤
  stage: 'page',
  priority: 8,

  resolve: function (ctx) {
    // ── 1. 从正文提取播放地址 ────────────────────────────────────────────
    // host.getHtml/select 是 lxml 视图；正则则最省事，正文里 \u002F 已归一化。
    var m = /"(?:playUrl|video_url|videoUrl)"\s*:\s*"([^"]+)"/.exec(ctx.body || '');
    if (!m) throw new Error('正文里没有 playUrl');
    var playUrl = absUrl(ctx.base, JSON.parse('"' + m[1] + '"'));  // 解 \u002F 与 \/

    // ── 2. 标题：正文有就用，没有就退回站点名 ────────────────────────────
    var titleNodes = host.select(ctx.body || '', 'title', ctx.base);
    var title = titleNodes.length ? titleNodes[0].text : '';

    // ── 3. 返回 ─────────────────────────────────────────────────────────
    return {
      url: playUrl,
      title: title,
      base: ctx.url,                       // 作为 Referer 交付
      headers: { 'Referer': ctx.url }      // 电视裸拉不通时，本机代理会带上这组头
    };
  }
});

registerRule({
  id: 'template-api-lookup',
  name: '示例：先调接口再取流',
  hosts: ['hancast-template.invalid'],
  stage: 'url',
  priority: 8,

  resolve: function (ctx) {
    // 从链接里拿对象 ID
    var id = /\/([A-Za-z0-9]{6,20})(?:[/?#]|$)/.exec(ctx.path);
    if (!id) return null;                  // 不适用，交给下一条规则

    // 接口要 Cookie 才能出流？host 的网络函数共用同一次解析的会话，
    // Cookie 在这些调用之间自动延续，不用自己拼 Cookie 头。
    var meta = host.getJson('https://api.' + ctx.host + '/video?id=' + id, {
      referer: ctx.url,
      headers: { 'Accept': 'application/json' }
    });

    // str/at/items 是宿主预置的取值助手，字段缺了给空而不是抛
    var play = str(meta, 'data.url');
    if (!play) throw new Error('接口没给播放地址: ' + str(meta, 'message'));

    // 计算类接口常用签名：host.md5 / sha256 / base64Encode 都是同步的；
    // HMAC / AES / RSA 公钥、随机量与本机恒定设备号（host.deviceId）见开发指南 §3.5/§3.6
    var ts = Math.floor(Date.now() / 1000);
    var sign = host.md5('id=' + id + '&ts=' + ts + '&secret');

    // 多档位：ctx.quality 是用户点名的 id（可能乱填），先过白名单，缺省投最高档
    var TIERS = { 'full': 1080, 'hd': 720, 'sd': 480 };
    var want = String(ctx.quality || '').trim();
    var qn = TIERS[want] ? want : 'full';

    // 想少打一次网络，可以把接口结果缓存一会儿（落在插件目录 _cache/ 下）
    var cacheKey = 'meta-' + id + '.json';
    if (host.cacheAge(cacheKey) < 0 || host.cacheAge(cacheKey) > 10 * 60 * 1000) {
      host.writeCache(cacheKey, JSON.stringify(meta));
    }
    host.log('template 取流 ' + id + ' sign=' + sign + ' qn=' + qn);   // 进 sidecar 日志，便于排错

    return {
      url: absUrl(ctx.base, play) + '?br=' + TIERS[qn],
      title: str(meta, 'data.title'),
      thumbnail: str(meta, 'data.cover'),
      base: ctx.url,
      headers: { Referer: ctx.url, 'X-Sign': sign, 'X-Timestamp': String(ts) },
      // 界面按这两张表画画质选项；quality 填本次「实际交付」的那一档
      qualities: [{ id: 'full', label: '1080P' },
                  { id: 'hd', label: '720P' },
                  { id: 'sd', label: '480P' }],
      quality: qn
    };
  }
});
