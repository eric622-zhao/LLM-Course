---
name: repo-checkup
description: 体检当前的 git 仓库（分支、未提交改动、最近提交、大文件），并给出可执行的收尾建议；当用户问"这个仓库状态怎么样""提交前检查一下"时使用
---

# 仓库体检

## 步骤

1. 依次执行下面几条命令（用 `RunCli`，参数必须拆成列表，不要写管道和 `&&`）：

   | 目的 | 调用 |
   | --- | --- |
   | 当前分支与领先/落后 | `RunCli(command="git", args=["status", "--short", "--branch"])` |
   | 最近 5 条提交 | `RunCli(command="git", args=["log", "-5", "--pretty=format:%h %ad %s", "--date=short"])` |
   | 未推送的提交 | `RunCli(command="git", args=["log", "@{u}..", "--oneline"])` |
   | 仓库体积 | `RunCli(command="git", args=["count-objects", "-vH"])` |

2. **判断异常**，只报确实存在的：

   - 有未提交改动 → 提醒用户先提交或 stash，再说明改了哪些文件。
   - 有未推送提交 → 给出 `git push` 建议，但**不要自己推**。
   - 工作区出现 `config.ini`、`*.key`、`.env` 这类敏感文件 → 高亮警告，这是最严重的问题。
   - `size-pack` 超过 50MB → 提示可能有历史大文件。

3. **给结论**，不要复述命令输出。格式：

```markdown
## 结论
可以提交 / 提交前需要处理 N 项

## 需要处理
- [严重] config.ini 出现在工作区，且未被 .gitignore 忽略
- [普通] 3 个文件未提交：…

## 建议命令
```bash
git add …
git commit -m "…"
```
```

## 边界

- 只读操作。**不要**执行 `git add` / `commit` / `push` / `reset`，除非用户明确要求。
- 不猜远程仓库地址；用户没配 remote 就直说没配。
