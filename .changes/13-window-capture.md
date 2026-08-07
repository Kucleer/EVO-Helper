---
issue: 13
agent: root
type: Added
date: 2026-08-07
---

新增 `evo_helper.vision.optional.window_capture`：**代码驱动**的单窗口截图，为最终由代码执行的采集链路补上入口。

- `find_window(title_contains)` 按标题定位**唯一**可见窗口。匹配到 0 个或多个都报错——
  截错窗口会把另一个页面悄悄喂给解析器。
- `capture_window` 依次尝试两条后端：
  - `PrintWindow(PW_RENDERFULLCONTENT)`：让窗口把自己渲染到离屏位图，窗口被遮挡或不在主屏时仍可用，
    这是后台自动化真正需要的能力；
  - `mss` 按窗口矩形抓屏：要求窗口可见且未被遮挡，因此只作兜底。
- 只截**指定窗口**，绝不整屏抓取。整屏会拍到用户其他窗口——既是隐私问题，也是数据问题
  （被遮挡的窗口会混进训练样本）。本次会话中整屏方案已实测排除：前台是用户的私人窗口。
- 结果做空白校验：Chrome 在 GPU 合成内容取不到时会返回纯色位图，`_is_blank` 通过颜色数判定，
  两条后端都拿不到有效图就报错，而不是把一张白图当成截图。

实测已验证：

- 游戏标签页为活动页时，`PrintWindow` 取到**完整的 WebGL canvas**——1550×838、135259 种颜色，
  游戏画面全部渲染出来。Chrome 的 GPU 合成内容是这条路径最容易失败的地方，现已排除。
- 窗口位于副屏且非前台时同样可用（1936×1056、2099 种颜色），不要求窗口置于前台。

新增 `client_box` / `capture_window(client_only=True)` 裁到 client area。但普通 Chrome 窗口把
标签栏、地址栏、书签栏画在 client area **之内**，裁剪只能去掉阴影，去不掉这些——每张样本都会带上
用户的标签页标题和书签。因此游戏窗口**必须用 `--app` 模式启动**，实测其后只剩约 38px 标题栏，
其下即页面视口；这也是旧项目 `expedition_reports.py` 的做法。

同时把可选后端的类型忽略从行内注释改为 `pyproject.toml` 的 mypy overrides。CI 只装 `.[dev]`，
这些包在 CI 上「不存在」、在本地装了 `vision` extra 后「存在但无类型」，两种情况的 error code 不同，
行内 `type: ignore[import-not-found]` 无法同时满足；`ignore_missing_imports` 可以。

- 配置：无变更
- 数据库：无变更
- 验证：`pytest`（217 passed）、`ruff check src tests`、`ruff format --check src tests`、`mypy src`
- 安全：只读截图，未新增点击路径；`dry_run` 仍为 true
- 回滚：删除 `window_capture.py` 与其测试，并还原 pyproject 的 mypy overrides
