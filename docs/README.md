# HanCast 文档索引

> 最后校对：2026-10-15，对应版本 v2.3.13（`package.json:4`）

这里是**唯一入口**：想知道什么，按下面的表走。每份文档头部都有一行「最后校对」，
写的是它对齐过的版本；引用形式统一为「源码路径 + 冒号 + 行号」，如 `commands.py:110-197`，
指向仓库内的真实行号，可用 `tools/audit-doc-refs.py` 机械校验。

## 1. 按问题找文档

| 你想弄明白的 | 看这份 | 行数 |
| --- | --- | --- |
| 程序整体怎么分层、几个进程、端口、线程、落盘状态 | [架构总览.md](架构总览.md) | 316 |
| 我要开始改代码：环境、跑法、测试、调试、提交规矩 | [开发指南.md](开发指南.md) | 330 |
| 前端能调哪些命令、收到哪些事件、参数与返回长什么样 | [HanCast-Frontend-API.md](HanCast-Frontend-API.md) | 893 |
| 前端组件/store/视图的结构、样式与 i18n 约定 | [HanCast-Frontend-Spec.md](HanCast-Frontend-Spec.md) | 492 |
| 投屏发现不了 / 推上去没画面 / 停不下来 / 声音不对 | [故障排查.md](故障排查.md) | 339 |
| 粘贴链接怎么变成可播地址（规则管线） | [分享解析规则.md](分享解析规则.md) | 203 |
| 怎么写一个 JS 规则包（沙箱 API、签名原语、Cookie） | [解析插件开发指南.md](解析插件开发指南.md) | 432 |
| 官方规则包怎么打包、发布、被用户拉到 | [../rule-packs/README.md](../rule-packs/README.md) | 415 |
| 屏幕投屏（HLS 双供、采集、环回、观看页、FFmpeg 依赖检测与按需下载） | [屏幕投屏.md](屏幕投屏.md) | 867 |
| 投屏确认（Device Guard 决策链） | [投屏确认.md](投屏确认.md) | 640 |
| 版本号从哪来、三平台打包、产物验收、发布顺序 | [构建与发布.md](构建与发布.md) | 655 |
| `tools/` 里那堆脚本各验什么 | [../tools/README.md](../tools/README.md) | 221 |
| 反爬为什么这么判、浏览器通道怎么取证 | [反爬实现取证与浏览器通道.md](反爬实现取证与浏览器通道.md) | 396 |
| 每个版本改了什么 | [../CHANGELOG.md](../CHANGELOG.md) | 249 |

## 2. 推荐阅读路径

**新人第一次接触这个仓库**：
[架构总览](架构总览.md) §1-§4（三进程与契约）→ [开发指南](开发指南.md) §1-§5（装依赖、跑起来、测试口径）→
挑一个子系统读专文（[屏幕投屏](屏幕投屏.md) 或 [投屏确认](投屏确认.md)）→ 出问题时用 [故障排查](故障排查.md)。

**只加一个站点解析能力**（不改主程序）：
[分享解析规则](分享解析规则.md) §1-§3 → [解析插件开发指南](解析插件开发指南.md) →
[rule-packs/README.md](../rule-packs/README.md) §4（索引与产物）。

**发版**：
[构建与发布](构建与发布.md) §2（版本 SSOT）→ §4-§6（本地与 CI 打包、产物验收）→ §7（发布顺序）。

**前端/壳层联调新命令**：
[HanCast-Frontend-API.md](HanCast-Frontend-API.md) 找同组命令的写法 → [架构总览](架构总览.md) §5 的数据流 →
[开发指南](开发指南.md) §8 的「新能力同步清单」（七步，含文档与 i18n）。

## 3. 历史/已取代的文档

保留是为了考古，**不要按它们改代码**：

| 文件 | 状态 | 取代者 |
| --- | --- | --- |
| [HanCast-Backend-Spec.md](HanCast-Backend-Spec.md) | 2.0 期设计草案（2026-07-05），模块名、命令表、端口假设均已被实现推翻 | [架构总览.md](架构总览.md) + [HanCast-Frontend-API.md](HanCast-Frontend-API.md) |
| [Macast-Frontend-Spec.md](Macast-Frontend-Spec.md) | Macast 时期的前端草案 | [HanCast-Frontend-Spec.md](HanCast-Frontend-Spec.md) |
| [Macast-Frontend-API.md](Macast-Frontend-API.md) | Macast 时期的命令表，缺屏幕投屏/解析/规则包/参数/日志等整批命令，且列了无发送方的 `cast_error` | [HanCast-Frontend-API.md](HanCast-Frontend-API.md) |
| 顶层 `AGENTS.md`（旧名 `CLAUDE.md`，现仅存指针） | 仍有效，但只做速览与安全/约定条目，深度内容以本索引为准 | 本索引各专文 |

## 4. 文档维护约定

这是**硬要求**，不是建议（同 [开发指南](开发指南.md) §11）：

1. **改了什么，同一轮就把文档改成什么**。命令、事件、后台参数、端口、落盘文件、平台限制——这些都是契约，文档滞后等于契约过期。
2. **每条关键论断挂 `文件:行号`**。行号会漂，所以每次大改后跑一次体检：

   ```bash
   python3 tools/audit-doc-refs.py            # 全仓文档
   python3 tools/audit-doc-refs.py docs/故障排查.md --show   # 单份，并打印被引原文
   ```

   脚本会自己分辨四类**刻意**的引用，它们在结尾的「放行」计数里，不报缺陷：

   | 放行类别 | 判据 | 例子 |
   | --- | --- | --- |
   | URL 片段 / 占位名 | 行内 http(s) 或 www 段先被挖掉；`[xX]+`、`\d…`、`seg…` 组成的示例名 | `xxxx.png`、`seg%03d.ts` |
   | 运行时与依赖包内路径 | 名字在 `RUNTIME_NAMES` 里、basename 命中 HLS 切片模式，或被 `.gitignore` 忽略（`git check-ignore`） | `window_config.json`、`certifi/__init__.py`、`.understand-anything/knowledge-graph.json` |
   | 历史/否定/上游语句 | 该行（或同段上一行）含「曾、删、已移除、上游、没有、不存在、取代…」等标记词 | 「`bili.py` 一起删掉了」 |
   | 已取代的整份草案 | 文档前 15 行出现「已被/取代/历史/草案/superseded」 | `HanCast-Backend-Spec.md` 里的 2.0 期模块名 |

   **行号越界不在放行范围内**——它永远算缺陷。所以引用历史文件名没问题，引用历史**行号**不行。
   往脚本里加放行规则要谨慎：四类都只影响「文件找不到」，不放宽行号校验。
3. **头部一行「最后校对」**：`> 最后校对：YYYY-MM-DD，对应版本 vX.Y.Z（...）`。改内容就改这行，别让它说谎。
4. **数字要写口径**。命令数、参数数、测试数这类计数必须写清「谁数的、从哪来」（例：`#[tauri::command]` 66（`lib.rs:1373-1440`）/ Sidecar 注册 59（`commands.py:110-197`）/ `commands.ts` 导出 65 —— v2.3.11 加 4 条 FFmpeg 依赖命令前是 62 / 55 / 61），
   否则下一次改动看起来就像文档坏了。
5. **发布轮补 [CHANGELOG.md](../CHANGELOG.md)**：一个版本一节，写用户可感知的变化 + 发布提交号。
6. **别删证据**。已被推翻的结论用「历史一句」保留（为什么曾经这么做、哪一版改的），不要静默抹掉。

新增文档时：在本索引 §1 表里登记一行，并在 [架构总览](架构总览.md) §9 的专文索引里加链接。
