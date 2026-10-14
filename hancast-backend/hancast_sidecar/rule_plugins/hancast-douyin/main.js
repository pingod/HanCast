/*
 * 抖音规则包 —— 与内置 Python 规则 media/rules/douyin.py 同逻辑的脚本版
 *
 * 抖音分享链接（v.douyin.com/xxx 跳转后）的播放地址藏在
 * www.iesdouyin.com/share/video/<aweme_id>/ 的分享页内嵌 JSON 里，
 * 通用 HTML 提取拿不到，所以直接按 JSON 字面量取 play_addr。
 */

registerRule({
  id: 'douyin-share',
  name: '抖音分享页',
  hosts: ['douyin.com', 'iesdouyin.com'],
  stage: 'page',

  resolve: function (ctx) {
    var m = /\/(?:video|note)\/(\d+)/.exec(ctx.url) ||
            /[?&](?:vid|aweme_id)=(\d+)/.exec(ctx.url);
    if (!m) throw new Error('未能提取抖音视频 ID');

    // 分享页对手机 UA 才给完整内嵌 JSON
    var share = host.getString(
      'https://www.iesdouyin.com/share/video/' + m[1] + '/',
      { mobile: true, referer: ctx.url });

    var mp = /"play_addr"\s*:\s*\{[^{}]*?"url_list"\s*:\s*\[\s*"([^"]+)"/.exec(share);
    if (!mp) throw new Error('抖音分享页未找到播放地址');

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
