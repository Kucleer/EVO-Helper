"""测试之间共用的种子工具（不是被测代码）。

放在 `tests/` 下并由 `pyproject.toml` 的 `pythonpath` 挂上，所以 e2e / integration
两边都能 `from support.runs import seed_run_instance`。
"""
