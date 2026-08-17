# 卡片渲染、统计字段与回归说明

本文记录本版本的卡片层重构边界，供维护者新增模板或平台解析器时参考。

## 1. 架构边界

原有主链路保持不变：

```text
消息 → Parser → ParseResult → MessageSender → AstrBot 消息组件
                                      │
                                      └─ Renderer（可选）→ PNG 卡片
```

- 解析器只负责填充实体；不直接拼接 HTML，也不决定卡片发送时机。
- `Renderer.render_card(result)` 是卡片层唯一入口，失败返回 `None`，不会阻断媒体下载/发送。
- `MessageSender` 仍然负责分组、合并转发、媒体类型转换和文本兜底。
- `SendGroup.render_card` 是平台局部偏好；全局卡片开关优先级更高，不能被局部设置绕过。
- 发送器对渲染器和卡片预览发送再做一层异常隔离；卡片失败时不会发送该卡片，
  自动合并策略会按实际媒体段数重算，避免失败的卡片改变媒体发送方式。

## 2. 配置与发送策略

| 配置键 | 默认值 | 作用 |
| --- | --- | --- |
| `card_enabled` | `true` | 卡片总开关 |
| `card_render_enabled` | `true` | 是否允许 HTML/PNG 渲染 |
| `card_send_enabled` | `true` | 是否允许把卡片插入消息链 |
| `card_template` | `default` | 选择模板名 |
| `card_custom_template` | `""` | 选择 `custom` 后使用的模板文件名 |
| `emoji_style` | `APPLE` | 表情字体风格标识 |
| `single_heavy_render_card` | `false` | 默认策略下，单一重媒体是否请求卡片 |

发送器会把策略拆为两个状态：

1. `render_card`：局部策略和渲染总开关均允许；
2. `send_card`：在 `render_card` 基础上，发送开关也允许。

只有 `send_card` 为真时，卡片才会计入合并阈值、参与预览消息或加入合并转发。这样关闭卡片不会改变原媒体段数和发送顺序。

启动时会迁移解析器配置：移除已下线平台的条目，为四个保留平台补齐默认项；旧版 Bilibili 的 `video_codecs` 单值会自动转换为 `video_codec_list`，保留用户原有编码偏好。

## 3. 模板扩展

模板查找优先级如下：

1. `<AstrBot 插件数据目录>/astrbot_plugin_parser_test/templates/`
2. `<插件安装目录>/templates/`
3. `core/templates/`（内置 `default.html`、`compact.html`）

将 `my_card.html` 放入第一目录后，在插件页将 `card_template` 选择为 `custom`，再把 `card_custom_template` 填为 `my_card` 即可。文件名会做路径净化，不能通过配置读取目录外文件。

Jinja2 自动转义已启用。内置可用过滤器：

| 过滤器 | 用途 |
| --- | --- |
| `format_count` | 将 `12000` 格式化为 `1.2万` |
| `emoji` | 保留 Unicode 表情序列，并加上选择的表情样式类 |
| `file_uri` | 将本地资源路径转换为 `file:///` URI |

模板上下文提供以下对象：

```jinja2
{{ result.title }}
{{ card.platform.display_name }}
{{ card.author.name if card.author else '' }}
{{ card.text | emoji }}
{{ card.stats.likes | format_count }}
{% for item in card.stat_items %}
  {{ item.label }} {{ item.value | format_count }}
{% endfor %}
{% for media in contents if media.uri %}
  <img src="{{ media.uri }}" alt="{{ media.alt or '' }}">
{% endfor %}
```

`card.repost` 最多递归一层，避免异常数据形成无限递归。`contents` 中每项都有 `kind`、`uri`、`text`、`alt`、`duration` 与 `name` 字段；视频展示其封面而不是下载完整视频。

## 4. Apple 表情支持

渲染器识别 Unicode Emoji、变体选择器、肤色修饰符和 ZWJ 组合序列，整体包装为 `.emoji--apple`（或配置的样式）元素。内置 CSS 的字体回退链为：

```css
"Apple Color Emoji", "Segoe UI Emoji", "Noto Color Emoji", sans-serif
```

因此 iOS 分享文案中的复合表情不会在 HTML 转义或断词过程中被拆开。渲染器会优先复用 `apilmoji` 缓存的本地 Apple PNG；CDN 不可达或资源尚未下载时仍保留原始 Unicode，并回退到可用的系统/Noto Emoji 字体。

渲染器使用 Playwright 驱动 Chrome Headless Shell 对 Jinja2 生成的 HTML 做全页 PNG 截图，不再通过 PDF、PyMuPDF、Pillow 或其他卡片绘图库转换。浏览器进程在插件生命周期内复用，避免为每张卡片重复启动。模板不存在、浏览器不可用或截图失败时，渲染器会记录异常并返回 `None`；发送器因此跳过卡片，但继续原媒体发送流程。

### 运行前提

安装 Python 依赖后，必须额外安装与 `playwright` 版本匹配的浏览器二进制：

```bash
python -m playwright install chromium-headless-shell
```

`chromium.launch(headless=True)` 会使用 Playwright 安装的 `chrome-headless-shell`。Linux 容器以 root 运行时，渲染器仅为该场景加入 `--no-sandbox`；其他环境保留浏览器默认沙箱。浏览器二进制缺失时，控制台会输出上述安装命令并仅跳过卡片，不会启用其他渲染器，也不会中断 Bilibili、抖音、小红书或 Pixiv 的原媒体发送。

### 缓存生命周期

最终卡片 PNG 与下载的图片、视频、Emoji 资源都写入原有的 `cfg.cache_dir`。截图用的隐藏临时 HTML 在页面关闭后立即删除；若进程异常导致残留，仍会被既有 `CacheCleaner._clean_plugin_cache()` 的“`shutil.rmtree(cache_dir)` 后 `mkdir`”策略一并清理。用户自定义模板位于独立的 `template_dir`，不在定时缓存清理范围内。

## 5. 互动统计映射

统一实体字段在 `core/data.py`：

| 统一字段 | Bilibili | 抖音 | 小红书 | Pixiv |
| --- | --- | --- | --- | --- |
| `like_count` | `stat.like` | `statistics.digg_count` | `interactInfo.likedCount` | `likeCount` |
| `comment_count` | `stat.reply` | `statistics.comment_count` | `interactInfo.commentCount` | `commentCount` |
| `favorite_count` | `stat.favorite` | `statistics.collect_count` | `interactInfo.collectedCount` | `bookmarkCount` |
| `share_count` | `stat.share` | `statistics.share_count` | `interactInfo.shareCount` | 平台未提供时为空 |

`EngagementStats.from_mapping()` 兼容整数、字符串、`1.2万`、`1亿` 和 `{count: ...}` 型 API 节点。缺失数据使用 `None`，模板仅显示存在的统计项。

## 6. 回归清单

提交前至少执行：

```bash
python -m pip install -r requirements.txt
python -m pip install astrbot
python -m playwright install chromium-headless-shell
python -c "import astrbot; print(astrbot.__version__)"
python -m compileall -q core main.py
python -m pytest -q
```

本次 Playwright 切换后的本地回归结果：`45 passed`。覆盖原 Cookie/JSON 链接用例，以及统计、配置、HTML 模板、Playwright Headless Shell 截图、渲染失败策略、Apple Emoji、卡片开关、缓存清理、旧配置迁移和位置参数兼容用例。

覆盖项：

- JSON 卡片 URL 提取只优先识别四个保留平台；
- Cookie 解析继续通过既有回归用例；
- 统一统计字段的数值归一化、模板上下文和 Apple Emoji 包装；
- 关闭 `card_enabled` / `card_render_enabled` / `card_send_enabled` 时，不生成或不发送卡片，但媒体发送计划保持可用；
- `default`、`compact` 和用户覆盖模板均可被加载，并可读取同目录的相对静态资源；
- Playwright 通过 `chrome-headless-shell` 输出 PNG，复用浏览器进程，并在截图后删除临时 HTML；
- 既有缓存清理任务会统一清理卡片、下载媒体和 Emoji 缓存；
- Bilibili、抖音、小红书、Pixiv 的统计字段在 API 字段缺失时不影响原有解析结果。

QQ 空间及其他已移除平台的解析器、配置模板、测试与 URL 优先级已一并移除，不再纳入回归范围。
