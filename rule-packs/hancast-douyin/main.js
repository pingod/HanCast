/*
 * 抖音规则包 —— 官方包，分享页内嵌 JSON 提取
 *
 * 抖音分享链接（v.douyin.com/xxx 跳转后）的播放地址藏在
 * www.iesdouyin.com/share/video/<aweme_id>/ 的分享页内嵌 JSON 里，
 * 通用 HTML 提取拿不到，所以直接按 JSON 字面量取 play_addr。
 */

var SHARE_ROOT = 'https://www.iesdouyin.com';
var PLAY_ADDR = /"play_addr"\s*:\s*\{[^{}]*?"url_list"\s*:\s*\[\s*"([^"]+)"/;

registerRule({
  id: 'douyin-share',
  name: '抖音分享页',
  hosts: ['douyin.com', 'iesdouyin.com'],
  stage: 'page',

  resolve: function (ctx) {
    var m = /\/(?:video|note)\/(\d+)/.exec(ctx.url) ||
            /[?&](?:vid|aweme_id)=(\d+)/.exec(ctx.url);
    if (!m) throw new Error('未能提取抖音视频 ID');

    // 分享页只对「带 ttwid 的请求」回吐内嵌 JSON：没有这张券时返回的是
    // 「验证码中间页」（argus captcha SDK），再怎么解析都找不到 play_addr。
    // 券由 iesdouyin 根路径下发，host.getString 共用解析会话，
    // 所以先空手领一次，后面的分享页请求自动带上。
    try { host.getString(SHARE_ROOT + '/', { mobile: true }); } catch (e) { }

    // 抖音是按请求抽风的：同一地址连着重发就能撞到有正文的那一份
    var share = '', mp = null, blocked = false;
    for (var i = 0; i < 3 && !mp; i++) {
      if (i) host.sleep(400);
      share = host.getString(SHARE_ROOT + '/share/video/' + m[1] + '/',
                             { mobile: true, referer: ctx.url });
      mp = PLAY_ADDR.exec(share);
      if (!mp && /验证码中间页|captcha|verifyCenter/i.test(share)) blocked = true;
    }
    if (!mp) {
      throw new Error(blocked
        ? '抖音要求人机验证（同 IP 短时间解析越多越容易触发），稍后再试'
        : '抖音分享页未找到播放地址');
    }

    // 分享页 JSON 里的斜杠是 \u002F 形式，必须按 JSON 字面量解码，
    // 否则交给电视的是一个带 \u002F 的废地址
    var videoUrl = JSON.parse('"' + mp[1] + '"');
    // 去水印变体（playwm → play），两者内容一致、play 更通用
    videoUrl = videoUrl.replace('playwm', 'play');

    var title = '';
    var mt = /"desc"\s*:\s*"((?:[^"\\]|\\.)*)"/.exec(share);
    if (mt) {
      try { title = JSON.parse('"' + mt[1] + '"'); } catch (e) { title = mt[1]; }
    }
    if (!title) {
      var nodes = host.select(ctx.body || '', 'title', ctx.base);
      title = (nodes.length ? nodes[0].text : '') || '抖音视频';
    }

    return { url: videoUrl, title: title, base: ctx.url };
  }
});
