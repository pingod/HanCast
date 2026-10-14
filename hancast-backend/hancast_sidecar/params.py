"""后台可调参数登记表

改动点:
- 把散落在各模块的硬编码常量集中成一张表：改参数不用重新编译
- 表本身即 UI 契约：设置页按 specs() 渲染，加参数只改这一处
- 只存「与默认值不同」的覆盖项，默认值升级后老配置不会把新版本钉死

存储位置：hancast_setting.json 的 `advanced` 字典（扁平的点号 key）。
读取时机：调用点每次读（不是 import 时读），所以改完对下一次会话即生效。
"""

import logging
from typing import Dict, List

logger = logging.getLogger("hancast.params")

ADVANCED_KEY = "advanced"

# 官方规则包索引的唯一一份地址：jsplugins 经 params.get("rules.index_url") 读它，
# 两处各写一遍早晚会对不上。
DEFAULT_RULES_INDEX = ("https://raw.githubusercontent.com/pingod/HanCast"
                       "/main/rule-packs/index.json")

# 对外身份的默认值就登记在这里，`utils/identity.py` 反过来读它——
# 两边各写一份正是以前「规则层 Chrome/124、代理层 Chrome/120」的由来。
DEFAULT_UA_DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
DEFAULT_UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                     "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                     "Mobile/15E148 Safari/604.1")

# type: number（配 min/max/unit）| bool | enum（配 options）| text（配 placeholder）
# text 强制 http(s) 前缀，只给地址类参数用；自由文本（UA 这种）走 string
# section 仅用于设置页分组
PARAM_SPECS: List[Dict] = [
    # ── 屏幕投屏 / HLS ──
    {"key": "screencast.browser_segment_seconds", "section": "screencast",
     "type": "number", "default": 1, "min": 1, "max": 10, "unit": "s",
     "label_zh": "浏览器通道分片时长", "label_en": "Browser HLS segment length",
     "help_zh": "越短延迟越低，但分片太碎会增加播放列表请求与解码压力；改这一步"
               "必须同步改关键帧间隔（本项自动跟随）",
     "help_en": "Shorter = lower latency, but more playlist requests and keyframe pressure"},
    {"key": "screencast.dlna_segment_seconds", "section": "screencast",
     "type": "number", "default": 2, "min": 1, "max": 10, "unit": "s",
     "label_zh": "电视通道分片时长", "label_en": "DLNA HLS segment length",
     "help_zh": "电视解码器普遍按 2 秒以上的整分片起解，调到 1 秒部分型号会黑屏",
     "help_en": "TV decoders usually need >=2s whole segments"},
    {"key": "screencast.playlist_size", "section": "screencast",
     "type": "number", "default": 6, "min": 3, "max": 30, "unit": "",
     "label_zh": "播放列表保留分片数", "label_en": "Playlist segments kept",
     "help_zh": "决定慢观众能掉队多久（片数 × 分片时长）",
     "help_en": "How far a slow viewer can fall behind (segments x length)"},
    {"key": "screencast.muxing_queue_size", "section": "screencast",
     "type": "number", "default": 1024, "min": 128, "max": 8192, "unit": "",
     "label_zh": "复用队列上限", "label_en": "Muxing queue size",
     "help_zh": "屏幕帧到达是突发的，队列溢出 FFmpeg 会直接退出（表现为投屏中途断流）",
     "help_en": "Frame bursts overflow this and FFmpeg exits (cast drops mid-way)"},
    {"key": "screencast.x264_preset", "section": "screencast",
     "type": "enum", "default": "veryfast",
     "options": ["ultrafast", "superfast", "veryfast", "faster", "fast"],
     "label_zh": "编码速度档", "label_en": "x264 preset",
     "help_zh": "越快越省 CPU 但同码率画质更差；弱机可降档",
     "help_en": "Faster saves CPU but costs quality at the same bitrate"},
    {"key": "screencast.max_width", "section": "screencast",
     "type": "number", "default": 1920, "min": 320, "max": 3840, "unit": "px",
     "label_zh": "采集宽度兜底上限", "label_en": "Capture width fallback cap",
     "help_zh": "仅在本次投屏没有指定尺寸时生效。高 DPI 屏幕原生 4K 采集既超电视能力又会被码率压成马赛克",
     "help_en": "Applies only when the cast requests no size; 4K HiDPI capture breaks TVs and looks awful at 2M"},
    {"key": "screencast.max_height", "section": "screencast",
     "type": "number", "default": 1080, "min": 240, "max": 2160, "unit": "px",
     "label_zh": "采集高度兜底上限", "label_en": "Capture height fallback cap",
     "help_zh": "仅在本次投屏没有指定尺寸时生效；必须是偶数，x264 拒绝奇数高度",
     "help_en": "Applies only when no size is requested; must stay even, x264 rejects odd heights"},
    {"key": "screencast.viewer_window_seconds", "section": "screencast",
     "type": "number", "default": 15, "min": 5, "max": 120, "unit": "s",
     "label_zh": "观众计数窗口", "label_en": "Viewer counting window",
     "help_zh": "这段时间内拉过播放列表的独立 IP 算一个观众",
     "help_en": "Distinct IPs that pulled the playlist within this window count as viewers"},
    {"key": "screencast.dlna_verify_seconds", "section": "screencast",
     "type": "number", "default": 12, "min": 3, "max": 60, "unit": "s",
     "label_zh": "电视拉流确认窗口", "label_en": "TV pull verify window",
     "help_zh": "超时未拉到播放列表就提示网络/地址问题；老电视加载慢可调大",
     "help_en": "No playlist pull within this window => network/address warning"},

    # ── 浏览器播放器（hls.js）──
    {"key": "player.live_sync_duration_count", "section": "player",
     "type": "number", "default": 1, "min": 1, "max": 10, "unit": "",
     "label_zh": "播放器追直播的分片数", "label_en": "hls.js live sync duration count",
     "help_zh": "缓冲里保持几个分片去追直播点：1 最低延迟，卡顿就加到 2-3",
     "help_en": "Segments behind the live point: 1 = lowest latency, raise to 2-3 if it stutters"},
    {"key": "player.live_max_latency_duration_count", "section": "player",
     "type": "number", "default": 4, "min": 2, "max": 20, "unit": "",
     "label_zh": "允许的最大滞后期数", "label_en": "hls.js max latency",
     "help_zh": "落后超过这个分片数就跳到直播点",
     "help_en": "Jump to the live edge when further behind than this"},
    {"key": "player.low_latency_mode", "section": "player",
     "type": "bool", "default": True,
     "label_zh": "hls.js 低延迟模式", "label_en": "hls.js low latency mode",
     "help_zh": "关闭后播放器按普通 HLS 保守缓冲，老浏览器更稳",
     "help_en": "Off = conservative buffering, more stable on old browsers"},

    # ── 设备发现 ──
    {"key": "discovery.ssdp_mx", "section": "discovery",
     "type": "number", "default": 3, "min": 1, "max": 8, "unit": "s",
     "label_zh": "组播搜索等待上限（MX）", "label_en": "SSDP MX",
     "help_zh": "M-SEARCH 后监听回包的时间；网络慢的设备靠调大它被发现",
     "help_en": "How long to listen for M-SEARCH replies; raise for slow devices"},
    {"key": "discovery.multicast_ttl", "section": "discovery",
     "type": "number", "default": 2, "min": 1, "max": 8, "unit": "跳",
     "label_zh": "组播跳数上限（TTL）", "label_en": "Multicast TTL",
     "help_zh": "DLNA 惯例是 2（只在本地网段）。跨 VLAN/多层路由的设备靠调大它收到通告",
     "help_en": "DLNA convention is 2 (local segment only); raise for routed/multi-VLAN gear"},
    {"key": "discovery.guard_mx", "section": "discovery",
     "type": "number", "default": 1, "min": 1, "max": 8, "unit": "s",
     "label_zh": "单播确认超时", "label_en": "Unicast probe timeout",
     "help_zh": "设备确认（Device Guard）单播探测某台设备的应答等待时间",
     "help_en": "Unicast probe reply timeout used by Device Guard"},
    {"key": "discovery.device_stale_s", "section": "discovery",
     "type": "number", "default": 90, "min": 30, "max": 600, "unit": "s",
     "label_zh": "设备下线判定的静默时长", "label_en": "Device silence threshold",
     "help_zh": "设备这么久没再通告就主动探一次它的描述地址；连探两次都不应答就按离线从列表移除。"
                "调大 = 更不易误删偶尔不心跳的电视，调小 = 关掉的设备消失得更快",
     "help_en": "Silent devices are re-probed after this; two failed probes drop them. "
                "Raise for TVs that skip heartbeats, lower to evict powered-off gear faster"},

    # ── 媒体服务 ──
    {"key": "media.http_timeout_seconds", "section": "media",
     "type": "number", "default": 30, "min": 5, "max": 120, "unit": "s",
     "label_zh": "媒体代理请求超时", "label_en": "Media proxy timeout",
     "help_zh": "拉取远端媒体/分片单次请求的超时；跨境源站靠调大它兜住",
     "help_en": "Per-request timeout when fetching remote media; raise for distant origins"},
    {"key": "media.max_soap_body_kb", "section": "media",
     "type": "number", "default": 1024, "min": 64, "max": 8192, "unit": "KB",
     "label_zh": "SOAP 请求体上限", "label_en": "Max SOAP body size",
     "help_zh": "部分控制器会塞进很长的 DIDL 元数据，超限直接拒收",
     "help_en": "Some controllers send very long DIDL metadata; oversized bodies are refused"},

    # ── 对外请求身份 ──
    # 站点风控看的是「一整套说法是否自洽」，不是单个字段。这两项把解析、代理转发、
    # 交付给电视的请求头收到同一个开关下：改了立刻全链路一致，不必重新编译。
    {"key": "http.ua_desktop", "section": "http", "type": "string",
     "default": DEFAULT_UA_DESKTOP,
     "label_zh": "桌面 User-Agent", "label_en": "Desktop User-Agent",
     "help_zh": "解析分享链接和本机代理拉流时对外报的浏览器身份，改完下一次解析即生效。"
               "只认 UA 不构成「像人」：TLS 指纹仍是 Python 的，把 UA 换成最新版 Chrome "
               "反而更容易被交叉比对识破，要换就整条链一起换",
     "help_en": "Identity reported by parsing and the local media proxy. Takes effect on "
                "the next resolve. UA alone does not make traffic look human - TLS/JA3 is "
                "still Python's, so a very fresh Chrome UA can be more suspicious"},
    {"key": "http.ua_mobile", "section": "http", "type": "string",
     "default": DEFAULT_UA_MOBILE,
     "label_zh": "手机 User-Agent", "label_en": "Mobile User-Agent",
     "help_zh": "规则要求移动站点（分享页、小程序页）时用的身份",
     "help_en": "Identity used when a rule asks for the mobile page"},
    {"key": "http.tls_impersonate", "section": "http", "type": "bool", "default": True,
     "label_zh": "伪造 TLS 指纹（与 UA 对应）", "label_en": "Spoof TLS fingerprint",
     "help_zh": "解析链接和规则包发请求时，按上面的 User-Agent 用对应浏览器的 TLS/JA3 "
               "握手，避免「UA 说是 Chrome、握手暴露成 Python」这种一眼假的组合。"
               "认不出版本的 UA 不会伪装；关掉后回到 Python 原生握手，无需重新编译",
     "help_en": "Handshake with the TLS/JA3 fingerprint of the browser named by the "
                "User-Agent above, so UA and TLS agree. Unrecognised UAs stay unspoofed; "
                "turning this off falls back to Python's native handshake"},

    # ── 浏览器通道 ──
    # 站点的播放地址越来越多由播放器现调签名接口下发，纯 HTTP 抓页永远拿不到。
    # 这三项管的是「借宿主 WebView 渲染一次」的开销与上限，不是功能开关之外的策略。
    {"key": "browser.enabled", "section": "browser", "type": "bool", "default": True,
     "label_zh": "浏览器通道（借本机 WebView 渲染嗅探）",
     "label_en": "Browser channel (render via local WebView)",
     "help_zh": "规则包可以让程序用本机浏览器内核渲染页面，取渲染后的正文、播放器现取的"
               "流地址和 Cookie。渲染比 HTTP 抓页贵得多，也只有真浏览器能过某些站的校验",
     "help_en": "Lets rule packs render a page in the app's own WebView to get post-JS "
                "HTML, player-fetched stream URLs and cookies. Far costlier than an HTTP "
                "fetch, and the only way past some checks"},
    {"key": "browser.timeout_s", "section": "browser",
     "type": "number", "default": 12, "min": 3, "max": 25, "unit": "s",
     "label_zh": "单次渲染上限", "label_en": "Per-render timeout",
     "help_zh": "一次渲染最多等多久（同时受整条解析的 25 秒总预算约束）。"
               "超时后通道冷却 90 秒，不再让每条规则都重等一遍",
     "help_en": "Max wait for one render (also capped by the 25s resolve budget). "
                "A timeout cools the channel down for 90s instead of re-waiting per rule"},
    {"key": "browser.settle_ms", "section": "browser",
     "type": "number", "default": 1500, "min": 0, "max": 8000, "unit": "ms",
     "label_zh": "页面加载后再等待", "label_en": "Settle time after load",
     "help_zh": "加载完成后额外等这么久再收地址——不少站点的播放器是加载后再调接口拿流，"
               "不等就只嗅到页面骨架",
     "help_en": "Extra wait after load; many players fetch their stream only after the "
                "page settled, so a shorter value sniffs just the skeleton"},

    # ── 解析失败台账 ──
    {"key": "failures.enabled", "section": "failures", "type": "bool", "default": True,
     "label_zh": "记录解析失败的地址", "label_en": "Log failed links",
     "help_zh": "按域名记下来解析不出来的链接和失败原因，方便以后照着它给站写规则包",
     "help_en": "Remember unresolvable links grouped by host with the reason"},
    {"key": "failures.max_per_host", "section": "failures",
     "type": "number", "default": 50, "min": 5, "max": 500, "unit": "",
     "label_zh": "每个域名保留条数", "label_en": "Entries kept per host",
     "help_zh": "同一个站超出上限就淘汰最旧的，防止刷粘贴把台账撑爆",
     "help_en": "Oldest entries of a host are dropped past this cap"},
    {"key": "failures.retention_days", "section": "failures",
     "type": "number", "default": 30, "min": 1, "max": 365, "unit": "天",
     "label_zh": "台账保留天数", "label_en": "Failure ledger retention",
     "help_zh": "超过这么久没再出现的地址在下次启动时清掉",
     "help_en": "Hosts unseen for this long are pruned at next start"},

    # ── 日志 ──
    {"key": "logs.tail_mb", "section": "logs",
     "type": "number", "default": 2, "min": 1, "max": 16, "unit": "MB",
     "label_zh": "日志读取尾部大小", "label_en": "Log tail size",
     "help_zh": "应用内「查看详细日志」一次最多读取的文件尾部字节数",
     "help_en": "How much of the log file the in-app viewer reads per request"},

    # ── 解析规则 ──
    # 规则包索引是「下载下来就要执行里面 JS」的入口，地址必须能在设置里换：
    # 官方地址在墙内常拉不动，不给出镜像位就等于没有更新功能。
    {"key": "rules.index_url", "section": "rules",
     "type": "text", "default": DEFAULT_RULES_INDEX,
     "label_zh": "规则包索引地址", "label_en": "Rule pack index URL",
     # 索引本身允许 http（内网镜像是正当用法）；被强制 https 的是清单里每个包的下载
     # 地址——那才是要执行 JS 的入口。help 笼统写「只认 https」会把用户吓走。
     "help_zh": "「检查规则包更新」读取的清单；清单里每个包的下载地址只认 https。"
               "网络不通时填自己的镜像，镜像必须照抄官方条目里的 sha256，"
               "否则装包会因摘要不符被拦下",
     "help_en": "Index read by 'check for pack updates'; every pack URL it lists must "
                "be https. Point it at a mirror if raw.githubusercontent.com is "
                "unreachable; keep the sha256s"},
    # 实测（2026-10-14，本机走 9565 代理）：同一条索引冷启动首次 15s 直接 ReadTimeout，
    # 隧道热起来才 5.7–11.2s。默认卡在 15s 等于「检查规则包更新」在墙内点了必失败，
    # 而这个失败看起来像「没有更新」，用户不会怀疑是超时。
    {"key": "rules.index_timeout_s", "section": "rules",
     "type": "number", "default": 40, "min": 5, "max": 120, "unit": "s",
     "label_zh": "规则包索引超时", "label_en": "Rule pack index timeout",
     "help_zh": "拉索引和查更新时单次网络请求的等待上限",
     "help_en": "Per-request wait limit for fetching the index and update checks"},
]

_SPECS_BY_KEY = {s["key"]: s for s in PARAM_SPECS}
_overrides: Dict = {}


def specs() -> List[Dict]:
    """登记表 + 当前值，供设置页渲染"""
    out = []
    for spec in PARAM_SPECS:
        item = dict(spec)
        item["value"] = get(spec["key"])
        item["overridden"] = spec["key"] in _overrides
        out.append(item)
    return out


def specs_by_key(key: str) -> Dict:
    """单项的「登记表 + 当前值」，设置页与测试按 key 取用"""
    for item in specs():
        if item["key"] == key:
            return item
    raise KeyError(f"Unknown parameter: {key}")


def get(key: str):
    """取参数当前值（覆盖项优先，否则默认值）"""
    if key in _overrides:
        return _overrides[key]
    spec = _SPECS_BY_KEY.get(key)
    if spec is None:
        raise KeyError(f"Unknown parameter: {key}")
    return spec["default"]


def load_from(settings: Dict):
    """从 Config.settings 载入覆盖项（启动/保存后调用）"""
    global _overrides
    raw = (settings or {}).get(ADVANCED_KEY) or {}
    if not isinstance(raw, dict):
        logger.warning("config advanced is not an object, ignored")
        _overrides = {}
        return
    clean = {}
    for key, value in raw.items():
        spec = _SPECS_BY_KEY.get(key)
        if spec is None:
            logger.debug(f"Ignoring unknown advanced param override: {key}")
            continue
        try:
            normalized = _coerce(spec, value)
        except ValueError as e:
            logger.warning(f"Bad saved value for {key}: {e}")
            continue
        if normalized == spec["default"]:
            continue  # 等于默认值就不占覆盖位，新版本改默认时能跟着走
        clean[key] = normalized
    _overrides = clean


def apply(values: Dict) -> List[str]:
    """写入设置页提交的参数，返回错误信息列表（成功项照常落库）

    只改与默认值不同的项；改回默认值即删除覆盖项。
    调用方负责把 settings[ADVANCED_KEY] 落盘（用 snapshot()）。
    """
    errors: List[str] = []
    for key, value in (values or {}).items():
        spec = _SPECS_BY_KEY.get(key)
        if spec is None:
            errors.append(f"未知参数: {key}")
            continue
        try:
            normalized = _coerce(spec, value)
        except ValueError as e:
            errors.append(f"{spec.get('label_zh', key)}: {e}")
            continue
        if normalized == spec["default"]:
            _overrides.pop(key, None)
        else:
            _overrides[key] = normalized
    return errors


def snapshot() -> Dict:
    """当前覆盖项（落盘用）"""
    return dict(_overrides)


def _coerce(spec: Dict, value):
    kind = spec["type"]
    if kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            token = value.strip().lower()
            if token in ("true", "1", "yes", "on"):
                return True
            if token in ("false", "0", "no", "off"):
                return False
        raise ValueError(f"需要布尔值，收到 {value!r}")
    if kind == "enum":
        text = str(value)
        if text not in spec["options"]:
            raise ValueError(f"只能取 {'/'.join(spec['options'])}")
        return text
    if kind == "string":
        text = str(value).strip()
        if not text:
            raise ValueError("不能为空，要改回默认请点「恢复默认」")
        if len(text) > 512:
            raise ValueError("内容过长（上限 512 字符）")
        return text
    if kind == "text":
        text = str(value).strip()
        if not text:
            raise ValueError("不能为空，要改回默认请点「恢复默认」")
        if len(text) > 512:
            raise ValueError("地址过长（上限 512 字符）")
        if not text.lower().startswith(("http://", "https://")):
            raise ValueError("需要 http(s) 地址")
        return text
    # number
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"需要数字，收到 {value!r}")
    low = spec.get("min")
    high = spec.get("max")
    if low is not None and number < low:
        raise ValueError(f"不能小于 {low}")
    if high is not None and number > high:
        raise ValueError(f"不能大于 {high}")
    # 整型参数（分片数、像素、KB…）保持 int，避免下游拼出 "1024.0"
    default = spec["default"]
    if isinstance(default, int) and float(number).is_integer():
        return int(number)
    return number
