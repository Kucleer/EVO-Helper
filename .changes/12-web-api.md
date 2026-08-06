---
issue: 12
agent: web-api
type: Added
date: 2026-08-06
---

实现本地 Web 与配置 API：扫描计划 CRUD、手动启动（强制 idempotency_key）、暂停/恢复/紧急停止、运行状态、bot 目标、坐标历史与舰队差异、强制复查与诊断页面。

- 影响：新增 `src/evo_helper/web/`（FastAPI 应用工厂、JSON API、Jinja2 页面）与 `httpx` 开发依赖；服务只监听 `127.0.0.1`。
- 配置：变更类请求需同源 Origin 或 `X-Evo-Helper-Token`（对应 `EVO_HELPER_WEB_TOKEN`，默认仅为本地开发值）。
- 数据库：无变更（当前由 FakeApplicationService 内存实现，真实编排由主 Agent 在集成阶段对接）。
- 验证：`tests/unit/web/` 与 `tests/integration/api/` 25 个测试通过，pytest/ruff/mypy 全绿。
- 安全：默认 `dry_run=true`；打开页面不自动开始任务；每个运行需显式幂等用户动作。
- 回滚：移除 `web/` 包并还原 `pyproject.toml` 开发依赖。
