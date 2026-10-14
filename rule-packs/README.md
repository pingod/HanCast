# HanCast — 规则包目录（`rule-packs/`）

> 这个目录回答一个问题：**官方解析规则包的「源 / 产物 / 索引」三件套是怎么组织的、怎么打包发布、用户机器上怎么装到**。写规则包的 API 细节不在这里。
>
> 最后校对：2026-10-15，对应版本 v2.3.6（`package.json:4`）

---

## 术语

| 词 | 含义 |
| --- | --- |
| 规则包 | 一个 zip：`manifest.json` + 若干 `.js`，在 sidecar 的 QuickJS 沙箱里注册站点解析规则 |
| Sidecar | Python 后台进程（`hancast-backend/`），由壳层用 stdin/stdout JSON 驱动 |
| 壳层 | Rust/Tauri 那一层（`src-tauri/`），**不参与下载与校验**，只转发命令 |
| 后台参数 | `hancast-backend/hancast_sidecar/params.py` 的 `PARAM_SPECS` 登记表暴露的可调项（当前 26 条，`params.py:35-201`） |
| 声明式规则 | `share_rules/*.json` 那种不写代码的站点规则，与规则包同一条管线（见 `docs/分享解析规则.md`） |

**先记住这条产品契约**：程序里**没有任何编译进去的站点规则**，全新安装就是不解析任何站点，解析能力只能来自用户装的规则包（`hancast-backend/hancast_sidecar/media/rules/jsplugins.py:16-19`、`hancast-backend/scripts/build_sidecar.py:248-249`、`src/locales/zh-CN.json:81`）。所以这个目录不是「附属资源」，而是官方解析能力的**唯一出货口**。

---

## 1. 目录结构

```
rule-packs/
├── hancast-bilibili/          ← 源：一个包一个目录
│   ├── manifest.json
│   └── main.js
├── hancast-douyin/
│   ├── manifest.json
│   └── main.js
├── hancast-template/
│   ├── manifest.json
│   └── main.js
├── dist/                      ← 产物：tools/build-rule-packs.py 生成，入库
│   ├── hancast-bilibili-1.1.0.zip
│   ├── hancast-douyin-1.1.0.zip
│   └── hancast-template-1.1.0.zip
└── index.json                 ← 索引：设置页「检查规则包更新」读的就是它
```

### 1.1 三者是一条单向流水线

```
rule-packs/<name>/{manifest.json,*.js}      源（人写的）
        │  python3 tools/build-rule-packs.py
        ▼
rule-packs/dist/<name>-<version>.zip        产物（脚本生成，确定性字节）
        │  同一个脚本一次跑完
        ▼
rule-packs/index.json                       索引（name/version/url/sha256/description）
        │  用户点「检查规则包更新」
        ▼
<SETTING_DIR>/share_rules/plugins/<name>/   用户机器上的安装目录
```

- 哪些目录算「包」由脚本扫出来，不是硬编码清单：`rule-packs/` 下**任何含 `manifest.json` 的子目录**都算，`dist` 自身跳过（`tools/build-rule-packs.py:48-59`）。
- 路径常量在 `tools/build-rule-packs.py:31-34`：`ROOT` / `PACKS_DIR` / `DIST_DIR` / `INDEX_PATH`。
- 索引条目里的 `url` 与 `sha256` 都是从 zip 现算的，**不是人手填的**（`tools/build-rule-packs.py:139-151`）。所以「改了源忘了重跑脚本」会被 `--check` 当场抓出来（见 §4.2）。

### 1.2 zip 内部布局：一层同名目录

产物不是把文件平铺在 zip 根，而是整体套一层与包名同名的目录（`tools/build-rule-packs.py:101`，`ZipInfo(f"{name}/{rel}")`）：

```
hancast-bilibili-1.1.0.zip
├── hancast-bilibili/manifest.json      272 B
└── hancast-bilibili/main.js           6414 B
```

导入端两种布局都收（`jsplugins.py:50` 的条目正则允许可选的一层目录），落盘时拍平：`rel = info.filename.rsplit("/", 1)[-1]`（`jsplugins.py:526`）。**产物统一用带目录那种**，是为了让 zip 解开就是一个能直接看的包目录。

### 1.3 `dist/` 是入库的，不是构建垃圾

`.gitignore:49` 全局忽略 `dist/`，紧接着 `:52-53` 又把它捞回来：

```gitignore
dist/
# 例外：rule-packs/dist 是官方规则包索引的下载地址（raw.githubusercontent 必须取到），
# 不是构建垃圾；这里一忽略，设置页的「检查规则包更新」就永远 404
!rule-packs/dist/
!rule-packs/dist/*.zip
```

（`.gitignore:49-53`，注释是仓库原文。）换句话说：**zip 不提交上去，用户点「安装」就是 404**。提交时源目录、`dist/*.zip`、`index.json` 必须一起进（`docs/解析插件开发指南.md:328-329`）。

### 1.4 安装后的落点

用户机器上不是 `rule-packs/`，而是配置目录下的 `share_rules/plugins/`：

| 平台 | `<SETTING_DIR>` | 证据 |
| --- | --- | --- |
| Windows | `%APPDATA%\HanCast` | `hancast-backend/hancast_sidecar/utils/config.py:23` |
| macOS | `~/Library/Application Support/HanCast` | `config.py:25` |
| Linux | `~/.config/hancast` | `config.py:27` |

```
<SETTING_DIR>/share_rules/
├── *.json                       ← 声明式规则（另一条来源）
└── plugins/                     ← 规则包根
    ├── state.json               ← 停用清单 + 各包 prefs（重装不丢）
    ├── _cache/<plugin>/…        ← host.readCache/writeCache 落点
    └── <name>/
        ├── manifest.json
        └── *.js
```

- `share_rules` 根可被设置项覆盖（`config.py:117-122`，默认 `os.path.join(SETTING_DIR, "share_rules")`）；解析器取它再拼 `plugins`（`hancast-backend/hancast_sidecar/media/share_resolver.py:341-344`）。
- 目录布局与 `state.json` / `_cache/` 的说明是 `jsplugins.py:7-14` 的模块 docstring 原文。
- 安装目录名 = `manifest.name`，不是 zip 文件名（`jsplugins.py:520`，`target = os.path.join(self.root, name)`）。

---

## 2. `manifest.json` 字段表与 `index.json` schema

### 2.1 `manifest.json`

三个官方包都是同一个 5 键形状（`name` / `version` / `author` / `description` / `scripts`）（`rule-packs/hancast-bilibili/manifest.json:1-7`、`hancast-douyin/manifest.json:1-7`、`hancast-template/manifest.json:1-7`）。约束分两处：**打包期**由 `tools/build-rule-packs.py` 挡，**导入期**由 `jsplugins.py` 挡，两边口径一致。

| 字段 | 类型 | 必填 | 含义 | 约束（含证据） |
| --- | --- | --- | --- | --- |
| `name` | string | ✓ | 包标识，同时是**用户机器上的安装目录名** | 必须等于源目录名，否则打包报错（`build-rule-packs.py:65-68`，理由：落盘用 `manifest.name`，不一致时索引里的包名和用户目录名对不上）；导入期还要求匹配 `^[A-Za-z0-9_-]{1,64}$`（`jsplugins.py:48`、`:506-509`） |
| `version` | string | ✓ | 包版本，决定 zip 文件名与「是否提示升级」 | 不能为空（`build-rule-packs.py:69-71`）；进 zip 文件名（`:109-110`、`:127`）；更新比对按 `_version_key` 拆数字段比较，非数字段按 0、缺段不报错（`jsplugins.py:73-77`、`:383`） |
| `author` | string | | 作者名，设置页展示 | 无校验，缺失按空串（`jsplugins.py:173`、`:256`）；三个官方包都填 `"HanCast"` |
| `description` | string | | 一句话说明，设置页展示，**并被抄进 `index.json`** | 无校验（`jsplugins.py:174`；`build-rule-packs.py:126`、`:151`） |
| `scripts` | string[] | ✓\* | 要加载的 `.js` 文件名列表 | 不能为空（`build-rule-packs.py:72-75`）；每个文件必须真存在（`:76-78`）；条目必须是 `.js` 结尾的**纯文件名**，带路径或以 `.` 开头都拒（`jsplugins.py:512-514`、`:209-211`）；zip 内顺序固定为 manifest 在前、脚本按名排序（`build-rule-packs.py:82-87`） |
| `main` | string | | `scripts` 的旧写法，二者取一 | 两处回退逻辑一致：`scripts` 缺了就取 `[manifest["main"]]`（`build-rule-packs.py:72-73`；`jsplugins.py:202-203`、`:510-511`）；两者都没有时，导入端按文件名排序加载包内全部 `.js`（`jsplugins.py:204-206`），而打包端直接报错（`build-rule-packs.py:74-75`）——**官方包必须显式声明** |
| `update_url` | string | | 包自己的 zip 下载地址 | 官方索引里查不到这个包时，`pull` 退回用它（`jsplugins.py:176`、`:400`、`:401-402`）；三个官方包都不写，因为它们就在索引里 |

\* `scripts` 与 `main` 至少要有一个；官方包一律写 `scripts`。

**还有一条不在字段表里的硬约束**：包目录里出现**未被 `scripts` 声明的 `.js`**，打包当场失败（`build-rule-packs.py:88-91`）。理由是导入端的条目白名单收得进这个文件、执行清单里却没有它，「静默丢包不如报错」。

### 2.2 `index.json`

schema 就是一个 `packs` 数组，5 个键（`rule-packs/index.json:1-25`，渲染代码 `build-rule-packs.py:149-153`，`ensure_ascii=False, indent=2` + 末尾换行）：

```json
{
  "packs": [
    {
      "name": "hancast-bilibili",
      "version": "1.1.0",
      "url": "https://raw.githubusercontent.com/pingod/HanCast/main/rule-packs/dist/hancast-bilibili-1.1.0.zip",
      "sha256": "70e223a6ef256f13092065b4ca846e0ddc147bc6c004fab372f7cfdaca08c9d1",
      "description": "B 站解析：APP 签名多线路取流 iOS → TV → Web HTML5，反爬 Cookie 落盘复用；默认投最高画质，投屏中可切换档位。"
    }
  ]
}
```

| 键 | 类型 | 含义 | 消费方怎么用 |
| --- | --- | --- | --- |
| `name` | string | 包名 | 不匹配 `^[A-Za-z0-9_-]{1,64}$` 的条目**直接丢掉**，不拖垮整页检查更新（`jsplugins.py:358-364`） |
| `version` | string | 索引里的版本 | 与本机已装版本比大小，高了才进「可更新」堆（`jsplugins.py:383-384`） |
| `url` | string | zip 下载地址 | **必须 https**，否则条目被丢掉（`jsplugins.py:363-364`）。理由：这是「下载来就要执行里面 JS」的入口，不给明文地址留门（`:333-336` docstring） |
| `sha256` | string | zip 的 SHA-256 | 硬约束：下载完先比摘要，不符就报「包校验失败（sha256 不匹配，可能被劫持或索引过期）」且**不落盘**（`jsplugins.py:548-553`） |
| `description` | string | 展示文案 | 进设置页的两堆列表（`jsplugins.py:379-380`；`src/components/PluginSettings.vue:402`、`:417`） |

**索引里没有的东西，别指望它出现**：

| 参数 | 在不在 `index.json` | 真源 |
| --- | --- | --- |
| 索引地址本身 | ✗ | 后台参数 `rules.index_url`，默认值 `params.DEFAULT_RULES_INDEX`（`params.py:19-22`、`:182-192`）；`jsplugins.index_url()` 只读它，别处不再抄一遍（`jsplugins.py:55-61`） |
| 拉索引的超时 | ✗ | 后台参数 `rules.index_timeout_s`，默认 40s、min 5、max 120（`params.py:196-200`）；`jsplugins.index_timeout()` 读它，读不到才回退 40.0（`jsplugins.py:64-70`） |
| 包体积上限等 | ✗ | 写死在导入端：zip 2MB / 文件数 128 / 解压后 8MB / 单文件 2MB（`jsplugins.py:44-47`） |

> `rules.index_timeout_s` 为什么默认 40 而不是 15：`params.py:193-195` 记了 2026-10-14 的实测——同一条索引冷启动首次 15s 直接 ReadTimeout，隧道热起来才 5.7–11.2s。卡在 15s 等于墙内点了必失败，而这个失败看起来像「没有更新」，用户不会怀疑是超时。

**索引地址允许 http，包地址不允许**：`params.py:185-186` 的注释写明——索引本身允许 http（内网镜像是正当用法），被强制 https 的是清单里每个包的下载地址。`fetch_index` 也照这个口径：索引地址只要求 `http(s)` 前缀（`jsplugins.py:343-344`），条目 `url` 必须 https（`:363-364`）。用自己的镜像时必须照抄官方条目的 sha256，否则装包被摘要拦下（`params.py:187-189`）。

---

## 3. 三个官方包现状

| 包 | 版本 | 一句话能力 | `hosts` / `stage` | 证据 |
| --- | --- | --- | --- | --- |
| `hancast-bilibili` | 1.1.0 | B 站取流：iOS APP → TV APP → Web HTML5 三条线路依次试，appkey/appsec 做 md5 签名，反爬 Cookie（buvid3/buvid4）落盘缓存 1 小时复用；默认投 1080P（`qn=80`）并按接口回报的 `accept_quality` 给出可切档位 | `['bilibili.com','bilibili.tv','b23.tv']` / `'url'` | `index.json:3-9`；`rule-packs/hancast-bilibili/main.js:1-8`（docstring）、`:10-13`（REFERER / TTL）、`:16-24`（`QN_LABELS` 六档 + `DEFAULT_QN='80'`）、`:26-30`（`wantedQn` 白名单，脏值污染不了签名 query）、`:117-121`（`registerRule`） |
| `hancast-douyin` | 1.1.0 | 抖音分享页解析：先空手领一次 `ttwid` 券（没这张券拿到的是验证码中间页），再从 `www.iesdouyin.com/share/video/<id>/` 的内嵌 JSON 里抠 `play_addr`，并对抖音按请求抽风做重试 | `['douyin.com','iesdouyin.com']` / `'page'` | `index.json:10-16`；`rule-packs/hancast-douyin/main.js:1-7`（docstring）、`:9-10`（`SHARE_ROOT` / `PLAY_ADDR` 正则）、`:12-16`（`registerRule`）、`:23-27`（领券，`host.getString` 共用解析会话）、`:29-30`（重试） |
| `hancast-template` | 1.1.0 | **教学样例，不解析任何真实站点**：两条规则演示 `stage:'page'` 的正文正则取流与 `stage:'url'` 的「先调接口再取流」，含签名原语、缓存、多画质档位契约 | `['hancast-template.invalid']` ×2 / `'page'`、`'url'` | `index.json:17-23`（描述明写「只匹配保留域名 .invalid，不参与真实解析」）；`rule-packs/hancast-template/main.js:1-34`（一整段契约 docstring）、`:36-62`（第一条）、`:64-117`（第二条）、`:39`/`:67`（`.invalid` 保留 TLD） |

几点值得单独说：

- **模板包留在官方索引里是刻意的**：它是用户点「检查规则包更新」时能一键装到手的活文档，比 `docs/` 里的代码片段更不容易过时。它的 `hosts` 是保留 TLD `.invalid`，真实世界解析不到，不会误伤任何站点（`main.js:39`）。
- **优先级只有两档**：包规则的 `priority` 缺省 5，并被钳在 **4–8**（`jsplugins.py:96`，`_clamp` 在 `:154`）；声明式规则固定 5（`media/rules/base.py:121` 的注释「包规则统一落在 4-8」，注册表按 `(priority, name)` 排序在 `:143-145`）。笔误或坏心的规则既插不了队也垫不到底。
- **B 站包是多画质契约的现成参考**：档位 id 直接用 B 站自己的 `qn`，切档时客户端原样回传、包内再过一次白名单（`main.js:15`、`:26-30`），并如实回报实际交付的那一档。契约本身见 `docs/分享解析规则.md:172-178`。
- 三个包都是 `author: "HanCast"`，`scripts: ["main.js"]`，单脚本。

---

## 4. 打包与发布流程

### 4.1 打包：`tools/build-rule-packs.py`

```bash
python3 tools/build-rule-packs.py            # 重建 dist/*.zip + index.json
python3 tools/build-rule-packs.py --check    # 只校验产物/索引与源是否一致，不写盘（CI 与验收用）
python3 tools/build-rule-packs.py --clean    # 删掉 dist 里源目录已不存在的孤儿 zip
```

（用法原文见 `build-rule-packs.py:8-11`，argparse 在 `:197-210`。）

**一次 build 干的事**（`build-rule-packs.py:133-182`）：

1. `scan()` 扫源目录，逐包读 manifest 并做四项校验（`:118-130`，校验体在 `:62-79`）；
2. `build_zip()` 在内存里造 zip（`:95-106`）；
3. 算 sha256，与 `dist/` 里现有文件逐字节比，不同才写盘（`:139-148`）；
4. 用扫出来的条目渲染整份 `index.json`（`:149-153`、`:172-173`）；
5. 列出 `dist/` 里源目录已不存在的孤儿 zip，提示 `--clean`（`:156-157`、`:180-181`）。

**为什么产物是确定性的**（`build-rule-packs.py:13-14` docstring + 三处实现）：

| 手段 | 位置 |
| --- | --- |
| 固定时间戳 `1980-01-01 00:00:00` | `:40`（`_ZIP_DATE`）、`:101` |
| 固定条目顺序：`manifest.json` 在前，脚本按名排序 | `:82-87` |
| 固定外部属性：`create_system = 3`（unix）、`external_attr = (S_IFREG \| 0o644) << 16` | `:103-104`，注释说明「导入端拒收软链接；这里固定成普通文件位，产物就不可能带出软链接」 |

结论是：**内容没改 ⇒ 字节没改 ⇒ sha256 没改 ⇒ `index.json` 不跟着无意义地跳**，`--check` 才有意义（`:13-14`）。这条确定性也是 CI 敢直接从 tag 工作区上传 zip 的前提（§4.3）。

### 4.2 `--check` 的判定

只比对不写盘，退出码 1 时逐行列出问题（`build-rule-packs.py:159-170`）：

| 输出 | 触发条件 |
| --- | --- |
| `需要重建: dist/<file>（源已变，摘要与产物不符）` | 现算字节 ≠ 盘上字节（`:163-164`） |
| `需要重建: index.json（与源不一致）` | 渲染结果 ≠ 盘上内容（`:165-166`） |
| `孤儿产物: dist/… → --clean` | `dist/` 里有源目录已不存在的 zip（`:167-168`） |
| `规则包产物与索引均已同步（N 个包）` | 三者都干净，退出码 0（`:160-162`） |

**摘要不符 = 用户装不上包**：`pull` 下载完先比索引里的 sha256，不符直接拒装（`jsplugins.py:548-553`）。所以改了包里任何字节，都必须抬 `manifest.version` 并重跑打包脚本，把新 zip 与新 `index.json` 一起提交。

### 4.3 zip 怎么随 draft Release 发布

CI 的 `release` job（`.github/workflows/build.yml:553-609`）：

| 步骤 | 行号 | 做什么 |
| --- | --- | --- |
| 触发条件 | `:556-559` | `build` 成功 **且** `github.ref_type == 'tag'` **且**（是 push 事件 **或** `workflow_dispatch` 勾了 `make_release`） |
| `Checkout this tag` | `:566-567` | 检出 tag 工作区 |
| `Check rule-pack assets match the index` | `:569-571` | `python3 tools/build-rule-packs.py --check`，产物或索引与源不一致就非 0 退出，Release 不发 |
| `Stage rule-pack zips for the release` | `:573-578` | `mkdir -p artifacts/rule-packs && cp rule-packs/dist/*.zip artifacts/rule-packs/` |
| `Download all build artifacts` | `:580-588` | `pattern: bundle-*` + `merge-multiple: true`，只取每平台「全格式合一」的那份，避免同一安装包被上传两次 |
| `Publish draft release` | `:595-609` | `softprops/action-gh-release@v2`，`draft: true`，`files:` 九个 glob，最后一个是 **`artifacts/rule-packs/*.zip`**（`:609`） |

**为什么值得为 zip 单独加这两步**（`build.yml:562-565` 注释原文大意 + 提交 `356f6eb`「ci(release): 官方规则包 zip 随 draft Release 发布」）：规则包 zip 是被「设置页 → 检查规则包更新 → 安装」下载的，索引里的 sha256 必须和实际下载到的字节逐位一致；而 zip 是固定时间戳/固定顺序生成的，所以从 tag 工作区直接上传的字节和重跑 `build-rule-packs.py` 的字节相同——**不需要另建一条产线**。

### 4.4 索引 URL 为什么指向 `main` 分支的 raw 路径

```python
# 产物发布在仓库 main 分支上，raw 地址即官方索引；分支不是 main 时产物装不上，
# 所以这里宁可直接写死，让「换了分支忘了改」当场炸出来而不是静默产出坏索引。
RAW_BASE = "https://raw.githubusercontent.com/pingod/HanCast/main/rule-packs/dist/"
```

（`tools/build-rule-packs.py:36-38`；`zip_url()` 在 `:113-115`，对文件名过一道 `quote`。）

三点推论，都是硬要求：

1. **产物必须落到 `main`**。用户机器上真正下载的是 `raw.githubusercontent.com/.../main/rule-packs/dist/<file>`——只在 tag 或 Release 上有 zip 而 `main` 上没有，点「安装」就是 404。Release 里那份是同字节的副本，随安装包一起归档。
2. **`RAW_BASE` 故意写死不跟随当前分支**：换到别的分支时产物装不上，宁可当场炸出来，也不静默产出一份坏索引（`:36-37` 注释原文）。
3. **索引地址可换、包地址的域名不可换**。默认索引 `params.py:21-22` 也指向 `main/rule-packs/index.json`；用户可以把 `rules.index_url` 改成自己的镜像（`params.py:182-192`），但镜像必须同时镜像 zip 并把 `url` 改成自己的 https 地址、sha256 照抄官方（`params.py:187-189`）。

### 4.5 用户侧「检查规则包更新」的完整链路

**整条链只有 sidecar 在下载和校验**：壳层（Rust）只转发命令，前端只渲染结果。摘要校验在后端完成这一点，前端注释写得很明白（`src/api/commands.ts:892-893`「摘要校验在后端完成：包按索引条目下载，sha256 对不上就不落盘」）。

| # | 环节 | 位置 | 说明 |
| --- | --- | --- | --- |
| 1 | 用户点「检查规则包更新」 | `src/components/PluginSettings.vue:388-390` | 设置 → 解析插件 → 规则包更新段（`:385-386`） |
| 2 | `onCheckUpdates()` | `PluginSettings.vue:132-150` | 置 `checking`，成功后把结果拆进 `packUpdates` / `packAvailable` / `indexUrl` |
| 3 | `pluginCheckUpdates(indexUrl = '')` | `src/api/commands.ts:877-890` | `invoke('plugin_check_updates', { indexUrl: indexUrl \|\| null })`；留空即走后台参数 |
| 4 | Rust command `plugin_check_updates` | `src-tauri/src/lib.rs:324-335` | 纯转发：`sidecar.send_command("plugin_check_updates", {"index_url": …})` |
| 5 | sidecar 命令表 | `hancast-backend/hancast_sidecar/commands.py:144` | 注册到 `CommandHandler` |
| 6 | `_plugin_check_updates()` | `commands.py:676-680` | 先 `mgr.refresh()`（指纹变了就热重载），再 `mgr.check_updates(index_url)` |
| 7 | `PluginManager.check_updates()` | `jsplugins.py:371-386` | 拉索引 → 对上本地 `self._plugins` → 分两堆：本机没装的进 `available`（`:381-382`），索引版本更高的进 `updates`（带 `current`，`:383-384`）；返回 `{index_url, updates, available, installed}` |
| 8 | `fetch_index()` | `jsplugins.py:331-369` | `requests` GET 索引，超时取 `rules.index_timeout_s`，UA 用 `_IMPORT_UA`（`:52`）；HTTP ≥400 / 非法 JSON / 网络异常都翻成 `PluginError`（`:349-355`）；坏条目静默丢掉（`:358-364`） |
| 9 | 前端渲染两堆 | `PluginSettings.vue:396-424` | 「可更新」卡片 `:396-409`，「官方源里有、本机没装」卡片 `:411-424`；两堆都空时显示 `rulesNoUpdates`（`:426-429`） |
| 10 | 用户点「更新」/「安装」→ `onPull(entry)` | `PluginSettings.vue:152-166` | 成功后用返回的全量清单替换 `script_packs`，并把该项从两堆里过滤掉（`:156-158`） |
| 11 | `pluginPull(name, indexUrl = '')` | `src/api/commands.ts:892-901` | `invoke('plugin_pull', { name, indexUrl: indexUrl \|\| null })`，返回 `resp.plugins ?? []` |
| 12 | Rust command `plugin_pull` | `src-tauri/src/lib.rs:337-350` | 纯转发。**注意参数改名**：壳层收 `index_url`，往 sidecar 发的是 `{"name": …, "url": …}`（`:347`） |
| 13 | `_plugin_pull()` | `commands.py:682-692` | `mgr.pull(name, params["url"])`，然后回 **`{"plugins": mgr.list_info()}`**（全量清单，不是单包 info） |
| 14 | `PluginManager.pull()` | `jsplugins.py:388-403` | 先查索引拿 `url` + `sha256`；索引不可用时退回包自己声明的 `update_url`（`:400`）；两者都没有才报「既不在官方索引里，也没声明 update_url」（`:397-402`） |
| 15 | `import_url()` | `jsplugins.py:534-554` | 流式下载、64KB 分块、超 2MB 立刻断（`:543-547`），**然后比 sha256**（`:548-553`） |
| 16 | `import_zip_bytes()` | `jsplugins.py:475-532` | 四道体积上限（`:476-487`）→ 路径合法性（`:488-491`）→ 条目白名单（`:492-493`）→ 单文件上限（`:494-495`）→ 软链接拒收（`:496-497`）→ 恰好一个 manifest（`:498-501`）→ name 正则（`:506-509`）→ scripts 必须 `.js` 且必须存在（`:510-518`）→ `rmtree` 同名目录后落盘（`:520-528`）→ `self._fingerprint = None; self.refresh()`（`:530-531`） |

第 13 步的响应形状是踩过坑的：命令层必须回 `{"plugins": 全量清单}`，回单个包 info 会让前端整块刷新时把已装的包全弄丢，而 dev mock 恰好掩盖了这条（提交 `9227adf` 说明原文；回归测试在 `hancast-backend/tests/test_jsplugins.py`）。

**同名导入 = 覆盖更新**：`jsplugins.py:522-523` 先 `shutil.rmtree` 再写，`prefs` 与停用清单存在 `plugins/state.json` 里，不随覆盖丢失（`:287-299`、`:300-306`）。

**不点 GUI 验这条链**：

```bash
# 打包版 sidecar 走一遍 plugin_check_updates → plugin_pull（会先干净退出 GUI 以免两个 sidecar 抢 1900）
python3 tools/pull-rule-packs-installed.py                 # 升全部有更新的包
python3 tools/pull-rule-packs-installed.py --only hancast-douyin
python3 tools/pull-rule-packs-installed.py --no-relaunch   # 只拉不重启 GUI
```

（`tools/pull-rule-packs-installed.py:13-20`。`:2-7` 写了它存在的理由：截图上点一次「检查更新」只能证明那一次，而这条链路每次发新包都要重验；**手改 `share_rules/plugins/` 里的文件是拿补丁冒充验收，产物里根本没跑过 `plugin_pull`**。需要能访问 `raw.githubusercontent.com`，走系统代理时记得带 `HTTPS_PROXY`。）

```bash
# 源码 sidecar 跑真实解析管线，看每条链接由哪条规则命中（判据看输出的 via）
hancast-backend/.venv/bin/python tools/verify-rule-packs-live.py "<链接或口令>"
```

（`tools/verify-rule-packs-live.py:2-17`。`via` 是 `<包名>/<规则 id>` 才算走了 JS 规则包；脚本默认把 repo 根 `rule-packs/dist/` 的官方包当**用户导入的 zip** 装进临时目录，装之前先比 `index.json` 里的 sha256，产物过期当场失败。）

---

## 5. 与既有文档的分工

**本 README 只管这个目录本身**：源/产物/索引的关系、字段与 schema 的打包期约束、发布链路、历史沿革。写规则包的人要看的都在下面这几份里，别在这里找。

| 你要做什么 | 去看 | 具体章节 |
| --- | --- | --- |
| 写一个规则包：包结构、`resolve(ctx)` 入参与返回值、`host.*` API、沙箱边界 | [`docs/解析插件开发指南.md`](../docs/解析插件开发指南.md) | §1 插件包长什么样（含面向作者的 manifest 字段表，`:14-62`）、§2 `resolve(ctx)`（`:64-96`）、§3 宿主 API（`:98-192`，其中 §3.5 签名原语、§3.6 身份与 Cookie）、§4 运行边界（`:194-202`）、§5 安装与管理（用户视角，`:204-225`） |
| 官方包与索引的作者视角说明、发布新包到官方索引的清单 | 同上 | §6 官方规则包与那份索引（`:227-263`）、§7 规则总览：两种来源一处管理（`:265-288`）、§8 打包约束（`:290-297`）、§9 排错（`:299-316`）、**§10 把一个新规则包发布到官方索引的清单（`:318-338`，八步，改包前照它走一遍）** |
| 哪些站点实测取不到流 | 同上 | §11 附录（`:340-363`） |
| 写**声明式 JSON 站点规则**（不写代码那种） | [`docs/分享解析规则.md`](../docs/分享解析规则.md) | §2 规则只有两种来源（`:25-41`）、§3 外置声明式规则（`:43-122`，含 §3.2 字段表与 §3.3 排错）、§4 采集站的三类坑（`:124-157`） |
| 声明式规则与规则包怎么在同一条管线里协作、多画质契约 | 同上 | §1 解析管线（`:9-23`）、§5 JS 插件规则包（`:159-178`） |
| 一个完整的声明式规则样例工程 | [`docs/examples/collect-site/`](../docs/examples/collect-site/) | — |
| `tools/` 下的验收与取证脚本怎么选、怎么跑 | [`tools/README.md`](../tools/README.md) | §1.5 解析与规则包 |
| 版本号、构建、CI、发布 runbook、产物验收 | [`docs/构建与发布.md`](../docs/构建与发布.md) | §4.5 Release 与规则包 zip、§5 发布流程、§6 产物验收判据 |

**不重复的原则**：上面这些章节里已经写清的内容，本 README 只给结论 + `路径:行号`，需要细节就跳过去。反过来，`docs/解析插件开发指南.md` §6 与本 README §4 会有意重叠一小段（索引与摘要约束）——那份是**作者视角的操作口径**，这份是**目录视角的机制口径**，两处都必须与源码一致；改了任一侧的实现，两份都要改。

---

## 6. 「种子包随包播种」机制：已于 v2.3.3 退役

> ⚠️ **本节写的是「不存在的东西」，因为很多旧文档、旧提交说明和旧印象里它还在。** 当前代码里没有任何播种逻辑，装完首启**不会**往用户目录播任何包。

### 6.1 它曾经是什么

v2.3.2（提交 `60f8432`「feat(plugins): 内置规则包随包播种 + 规则总览统一管理」）引入过一整套机制，提交说明原文：

- `hancast-backend/hancast_sidecar/rule_plugins/` 随包三个规则包（`hancast-template` / `hancast-bilibili` / `hancast-douyin`），启动时按版本播种进用户插件目录：**缺失即装、随包版本更高才升级、用户自己导入的同名包永不覆盖**；
- `.hancast-builtin` 标记由播种独占（zip 导入器的条目白名单造不出它），`prefs` 与停用清单在覆盖升级中不丢；
- Nuitka 补 `--include-data-dir=rule_plugins`，`tools/accept-macos-artifact.sh` 加产物级播种契约；
- 设置页「规则总览」里内置包带徽章、无删除按钮，另有「内置 Python 兜底」一档，停用状态存 `state.json` 的 `disabled_builtin`。

### 6.2 v2.3.3 整体删除

提交 `9227adf`「feat(rules): 解析规则彻底包化，程序不再携带任何内置规则 + 官方规则包索引与一键更新」把它连同编译进程序的站点规则一起删了。提交说明原文第一条：

> 删除编译进程序的站点规则（bili.py / douyin.py / bili_resolver.py）与随包播种的「内置规则包」概念：站点解析能力只能来自用户安装的规则包，**全新安装就是不解析**

被删掉的东西（`git show --stat 9227adf`）：

| 路径 | 结局 |
| --- | --- |
| `hancast-backend/hancast_sidecar/media/bili_resolver.py` | 删（262 行） |
| `hancast-backend/hancast_sidecar/media/rules/bili.py` | 删（19 行） |
| `hancast-backend/hancast_sidecar/media/rules/douyin.py` | 删（63 行） |
| `hancast-backend/hancast_sidecar/rule_plugins/` | 整目录删；`hancast-{bilibili,douyin}/manifest.json` 各删 7 行，`main.js` 以 rename 方式搬到仓库根 `rule-packs/`（各改 1 行），manifest 在新位置重建 |
| `rule_set_builtin_enabled` 命令、`disabled_builtin` 状态键、「内置包」徽章 | 删 |

### 6.3 当前证据（逐条可核）

| 断言 | 证据 |
| --- | --- |
| `rule_plugins/` 目录不存在 | `ls hancast-backend/hancast_sidecar/` 无该目录；`git grep rule_plugins` 在 HEAD 只命中**反向断言与文档**，无功能代码 |
| `seed_bundled()` / `.hancast-builtin` 已无实现 | `git grep -n "seed_bundled"` 与 `git grep -n "hancast-builtin"` 在 HEAD **均零命中** |
| 冻结产物里没有任何解析规则 | `hancast-backend/scripts/build_sidecar.py:248-249`「产物里没有任何解析规则：站点规则全部由用户在设置页安装规则包（repo 根 `rule-packs/` 是官方包的源码与索引来源）」 |
| 插件目录就是解析规则的全部家当 | `jsplugins.py:16-19`「**这个目录就是解析规则的全部家当**：程序里没有编译进去的站点规则，也没有『内置包』这种特殊身份——想让某个站被解析，就得往这里装包」 |
| 旧状态键在读盘时就被丢弃 | `jsplugins.py:294` `state.pop("disabled_builtin", None)   # 内置规则这个概念已删` |
| 规则总览只有两种来源 | `share_resolver.py:394-396` 返回 `{rules_dir, script_packs, declarative}`；`docs/解析插件开发指南.md:267-270` 说明单测断言过这个 key 集合 |
| 界面文案照实说 | `src/locales/zh-CN.json:81`「解析能力全部来自规则包：程序本身不带任何站点规则」；`:89`「还没有规则包，所以现在任何站点链接都解析不了。用下面的『导入 ZIP』/『从 URL 导入』，或点『检查规则包更新』从官方索引安装」 |
| 产物级验收反过来断言「退役干净」 | `tools/accept-macos-artifact.sh:43-54`：遍历 `media/rules/bili.py`、`media/rules/douyin.py`、`media/bili_resolver.py`、`rule_plugins`，**产物里有就 FAIL，tag 的 git 树里有也 FAIL**（防「提交漏了删除」）；`:169-183` 断言 `rule_inventory` 只报两种来源、`script_packs` 里不许出现 `"builtin"`、冻结 `commands.py` 里不许再注册 `rule_set_builtin_enabled` |
| 新包发布清单也照这个口径 | `docs/解析插件开发指南.md:335-338`（第 8 条）把上述断言列成发布前必过项 |

### 6.4 于是「装完首启」实际发生什么

1. sidecar 起来，`ShareResolver` 建 `PluginManager(os.path.join(rules_dir, "plugins"))`（`share_resolver.py:341-344`），加载规则时调 `_load_external_rules()`（`:399-409`）；
2. 那一步只 `os.makedirs(self.rules_dir)`——建的是 `share_rules/`，**不是 `plugins/`**（`share_resolver.py:402`）；`plugins/` 不存在时 `refresh()` 直接跳过扫描（`jsplugins.py:432`）、指纹函数返回 `None`（`:408-409`）。真正创建 `plugins/` 的只有 `import_zip_bytes` 里的 `os.makedirs(self.root)`（`jsplugins.py:521`）。**没有任何启动路径会往里填东西**；
3. 设置页「解析插件」显示空态文案（`zh-CN.json:89`）与「检查规则包更新」按钮（`PluginSettings.vue:385-390`）；
4. 用户点一次「检查规则包更新」，三个官方包全部落在「官方源里有、本机没装」那一堆（`jsplugins.py:381-382`），逐个点「安装」走 §4.5 的 10-16 步落盘；
5. 落盘后 `refresh()` 热重载，规则立刻生效（`jsplugins.py:530-531`、`:425-446`），不需要重启。

**所以：官方包从「随应用出货」变成了「从索引一键装」。** 好处是发新包不用发新版本应用（`docs/解析插件开发指南.md:333-334` 第 7 条：应用侧无需改动，清单与更新段都是数据驱动的，新包自动出现，i18n 也不用动）；代价是**全新安装在装包之前解析不了任何站点**，这是刻意接受的契约，不是 bug。

---

## 相关文档

- [`docs/解析插件开发指南.md`](../docs/解析插件开发指南.md) — 写规则包（API、沙箱、打包约束、发布清单）
- [`docs/分享解析规则.md`](../docs/分享解析规则.md) — 声明式 JSON 站点规则与解析管线
- [`docs/构建与发布.md`](../docs/构建与发布.md) — 版本 SSOT、CI、Release、产物验收
- [`tools/README.md`](../tools/README.md) — 验收与取证脚本索引
- [`hancast-backend/tests/test_jsplugins.py`](../hancast-backend/tests/test_jsplugins.py) — 沙箱、导入校验、索引检查更新与 sha256 拒装、`update_url` 回退、优先级钳位的回归测试
