# the-guard

写代码前对齐工程规则，合入前执行独立架构审查。

Python 3.11+，零外部运行时依赖。规则来自仓库的 `AGENTS.md` / `CLAUDE.md`，
架构拓扑和约束来自 `.guard/config.json`。独立审查使用本机已登录的 Codex、Claude 或 agy CLI。

```sh
python3 -m pip install /path/to/the-guard
cd /path/to/your-project
guard init --preset core
guard topology sync
guard check --policy-only
guard check --base origin/main
# 完成功能分支提交后：
guard review
guard check-push
```

`core` 包含架构简洁、唯一真源、服务边界、代码组织四条规则；`full` 包含全部十四条。
[setup skill](skills/the-guard-setup/SKILL.md) 支持 core、full、从现有代码蒸馏、混合四种接入方式。
`guard distill --json` 输出语言、工具、模块边界、依赖和代码样本，供 Agent 核实后形成定制规则，
不会把偶然实现自动变成阻塞标准。

静态检查覆盖 Python 读路径写锁、重复异步读取、循环中的单实体请求，以及 Swift 词法读取检查。
可配置 SQL 独占 owner、退役表只删棘轮、禁止依赖和分层约束。通过基线函数签名与改动行比较，
未触及的历史违规不会被误判为新违规。静态启发式不能代替语义审查。

独立 Agent 按完整批次读取已提交的 Git 快照，一轮列齐具体阻塞项。复审可续接已验证会话；
可用 `guard review --note "代码位置、owner 合同与不变量证据"` 发起有依据的抗辩。
稳定 patch-id 支持 rebase 后复用回执，跨基线续用同时比较逐文件补丁。
回执和会话保存到 Git common directory，不是远端分支保护或加密证明。

`guard init --with-hooks` 可安装 Git pre-push 钩子；已有钩子不会被覆盖。
钩子检查实际推送的受保护分支提交范围，不能用不同 HEAD 的回执放行。
初次创建远端受保护分支须先建立明确审查基线，再启用钩子。
`guard probe` 只说明 CLI 可用，单测采用模拟 CLI，都不代表完成真实模型审查。

详见[英文配置与命令说明](README.md#configuration)。

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

MIT 许可证。
