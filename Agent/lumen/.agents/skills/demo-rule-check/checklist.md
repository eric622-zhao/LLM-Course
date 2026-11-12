# 测试自检清单

- [ ] 1. 已运行 `py -m unittest discover -s tests -v`，全部用例通过，无 error / failure
- [ ] 2. 已运行 `py smoke_tui.py`，TUI 能正常启动并退出，无报错堆栈
- [ ] 3. 未通过放宽安全校验（路径边界、删除确认、密钥检查）等方式让测试变绿
