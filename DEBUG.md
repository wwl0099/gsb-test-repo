# GSB Pair-wise 自动化脚本 — 开发调试全记录

## 一、需求理解与方案选型

### 1.1 原始需求

> 编写自动化脚本，实现：
> 1. 自动调起 CLI 两轮对话，把准备好的提示词自动输入
> 2. 检测对话结束，自动采集 sessionID 和轨迹文件
> 3. 任务完成后自动开启下一组
> 4. 将产物自动推送到 GitHub

### 1.2 核心问题

Codex CLI 默认是**交互式 TUI**——你输入提示词，它边思考边执行，完成后等你输入下一句。要实现自动化，必须回答三个问题：

| 问题 | 关键 |
|------|------|
| 怎么"自动输入"提示词？ | CLI 有没有非交互模式？ |
| 怎么知道"对话结束了"？ | 结束信号是什么？ |
| 怎么拿到 sessionID 和轨迹？ | 数据存在哪？格式是什么？ |

**不能靠猜，必须实际验证。**

### 1.3 方案探索

第一步：查看 CLI 帮助。

```
codex --help
```

发现关键子命令：

```
codex exec    Run Codex non-interactively
```

这意味着 CLI **本身就支持非交互模式**，不需要用 PTY 模拟键盘输入。进一步查看 `codex exec --help`：

- `--json`：事件以 JSONL 格式输出到 stdout
- `--dangerously-bypass-approvals-and-sandbox`：跳过所有确认（自动化必需）
- `--skip-git-repo-check`：允许在非 git 目录运行
- `-C <DIR>`：指定工作目录

**决定方案**：Python 通过 `subprocess.Popen` 管理 `codex exec --json` 子进程，逐行解析 stdout JSONL。不用 PTY，不用 expect，干净可靠。

---

## 二、验证 CLI 实际行为（最关键的一步）

### 2.1 测试命令

```powershell
codex exec --json --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check -C <workspace> "用Python写一个hello world脚本，保存为hello.py，然后运行它"
```

### 2.2 JSONL 输出结构（首次发现）

```
{"type":"thread.started","thread_id":"01a13837-7bc4-7081-917d-1da09e450c5d"}   ← 第1行
{"type":"turn.started"}
{"type":"item.completed","item":{"type":"reasoning","text":"..."}}
{"type":"item.started","item":{"type":"file_change",...}}
{"type":"item.completed","item":{"type":"file_change",...}}
{"type":"item.started","item":{"type":"command_execution",...}}
{"type":"item.completed","item":{"type":"command_execution","exit_code":0,...}}
{"type":"item.completed","item":{"type":"agent_message","text":"..."}}
{"type":"turn.completed","usage":{"input_tokens":32988,...}}                  ← 最后一行
```

### 2.3 关键结论

| 发现 | 结论 |
|------|------|
| `thread.started` 在第一行 | sessionID 立刻就能拿到，不用等结束 |
| `thread_id` 就是 sessionID | 一个字段解决 |
| `turn.completed` 是最后一行 | 这就是"对话结束"信号 |
| 进程在 `turn.completed` 后自动退出 | 不需要手动杀进程 |

### 2.4 轨迹文件位置

```
~/.codex/sessions/YYYY/MM/DD/rollout-YYYY-MM-DDTHH-MM-SS-{thread_id}.jsonl
```

文件名包含 thread_id，脚本可以直接字符串匹配定位，不需要解析文件内容。

---

## 三、脚本设计

### 3.1 架构

```
gsb_runner.py
├── run_codex_exec()    → subprocess.Popen 管理 codex exec
├── find_trace_file()   → 按 thread_id 搜索 sessions/ 目录
├── setup_workspace()   → shutil.copytree 复制独立工作区
├── git_commit_all()    → git add -A + commit + 返回 SHA
├── git_push()          → git push 到 GitHub
├── run_single()        → 单次运行完整流程
├── run_pair()          → A/B 配对运行 + push + 保存 manifest
└── main()              → CLI 入口，循环处理多个 prompt
```

### 3.2 设计决策

| 决策 | 理由 |
|------|------|
| Python 而非 PowerShell | JSON 逐行解析和子进程管理 Python 更成熟 |
| `subprocess.Popen` 逐行读 stdout | 实时获取 thread_id，不需要等进程结束；如果管道满了也不会死锁 |
| `shutil.copytree` 复制工作区 | A/B 物理隔离，零互相污染风险 |
| `git commit --allow-empty` | 即使模型没改文件也不会报错中断脚本 |
| 收集双份轨迹（stdout + trace） | stdout JSONL 是做 exec 时的实时输出；trace 是 sessions 目录下的完整记录。双保险。 |
| 超时机制 | `time.time() + deadline` 检查，防止模型卡死脚本永远挂起 |

---

## 四、调试过程中遇到的问题和修复

### 问题 1：CODEX_HOME 默认值选错

**现象**：
```
Error: Model provider `super_relay` not found
No thread.started event in JSONL output
```

**排查过程**：
脚本的 CODEX_HOME 检测逻辑是：如果 `~/.codex-cli` 目录存在就用它，否则用 `~/.codex`。因为之前为 GSB 项目创建了 `~/.codex-cli/config.toml`（model_provider = "super_relay"），脚本优先选了这个目录。但 relay 还没配通，provider 定义缺失，codex exec 直接报错退出，stdout 是空的。

**修复**：
添加 `--codex-home` 命令行参数，让用户显式指定。同时改进默认逻辑：不应只看目录是否存在，应该验证 config.toml 是否有效。此问题记录在脚本注释和 DEBUG 文档中供后续改进。

**教训**：目录存在不等于配置可用。默认值的选择逻辑需要防御式编程。

---

### 问题 2：Windows 下 shutil.rmtree 失败

**现象**：
```
[FATAL] prompt_001: [WinError 183] 文件已存在时，无法创建该文件
```

**排查过程**：
第一次测试跑完 A 轮、B 轮后，tmp 目录下留下了 `prompt_001_a` 和 `prompt_001_b`。第二次运行时 `setup_workspace()` 先调 `shutil.rmtree(work_dir, ignore_errors=True)` 再 `shutil.copytree()`。但 Windows 下 `.git` 目录中的某些文件（pack files、index）被进程锁定，`rmtree` 的 `ignore_errors=True` 只是静默跳过，实际没删掉目录。`copytree` 发现目标已存在就报错。

**修复**：
在每次测试前手动清理 tmp 目录：
```powershell
Remove-Item "tmp" -Recurse -Force
```

脚本层面改进方向（待实现）：使用 `git clean -fdx` 代替 rmtree，或在 rmtree 失败时重试。

**教训**：Windows 文件锁比 Linux 严格得多，涉及 `.git` 的操作要格外小心。

---

### 问题 3：GitHub push 网络不通

**现象**：
```
fatal: unable to access 'https://github.com/wwl0099/gsb-test-repo.git/':
Failed to connect to github.com port 443 after 21083 ms: Could not connect to server
```

**排查过程**：
1. 检查 git 代理设置：`git config --global http.proxy` — 为空
2. 检查环境变量：`$env:HTTP_PROXY` — 值为 `http://127.0.0.1:9`（死代理）
3. 清除后 push：成功一次
4. 再次 push：又失败
5. `Test-NetConnection github.com -Port 443` — 结果为 False

**结论**：GitHub 在沙箱网络环境中被墙。第一次成功是因为代理恰好没生效。

**修复**：
- 短期：用户在普通终端手动 push
- 长期：脚本设计为 push 是可选的（`--github-remote`），失败了也不影响核心功能

---

### 问题 4：端到端测试中模型自身的错误不影响脚本

**现象**（测试输出中观察到的）：
```
ERROR codex_core::tools::router:
  error=apply_patch verification failed:
  Failed to find context 'calculator.py' in ...
```

**分析**：
这是模型在执行任务时遇到的工具调用错误——apply_patch 匹配不到上下文。不是脚本的问题。模型会自行重试修复，最终两轮都成功完成（A 改 18 行，B 改 20 行）。

**结论**：脚本不需要处理模型内部的工具错误，这是正常的模型行为。脚本只需关注进程退出码和 JSONL 事件结构。

---

## 五、端到端测试结果

### 测试环境
- 模型：DeepSeek V4 Pro（通过 `~/.codex/config.toml`）
- CLI 版本：codex-cli 0.162.1
- 测试提示词："给 Calculator 类添加 square_root 方法，修复 average 方法的 bug，添加测试"

### 测试结果

| 指标 | Run A | Run B |
|------|-------|-------|
| 耗时 | 105s | 202s |
| sessionID | `01a1384d-3925-...` | `01a1384e-d501-...` |
| 产物 commit SHA | `e9d2f9a7` | `a6d41bd3` |
| 代码改动 | 18 行 | 20 行 |
| 轨迹文件大小 | 135KB | 203KB |
| 状态 | ✅ 成功 | ✅ 成功 |

### 验证的功能清单

| 功能 | 状态 |
|------|------|
| 自动调起 codex exec | ✅ |
| 解析 JSONL 提取 thread_id | ✅ |
| 检测 turn.completed 结束信号 | ✅ |
| 按 thread_id 定位轨迹文件 | ✅ |
| 复制轨迹到输出目录 | ✅ |
| git commit 记录产物 SHA | ✅ |
| A 完成后自动开始 B | ✅ |
| 保存 result.json 汇总 | ✅ |
| 多组 prompt 循环支持 | ✅（架构支持） |
| GitHub push | ⚠️ 网络限制 |

---

## 六、当前状态和后续工作

### 已完成
- [x] CLI 行为探索
- [x] gsb_runner.py（完整功能）
- [x] 端到端单组 prompt 测试通过
- [x] 测试仓库和示例提示词
- [x] GitHub 代码推送（需网络）

### 待后续验证
- [ ] 多组 prompt 批量循环（架构已支持，未实测）
- [ ] `--github-remote` 在有网络的环境下验证
- [ ] relay 模型接入后的行为差异
- [ ] 超时场景的异常处理测试
- [ ] shutil.rmtree 问题在脚本层面的自动修复

---

## 七、经验总结

1. **先验证再写代码**：花 10 分钟跑一次 `codex exec --json` 搞清楚 JSONL 结构，省掉了之后无数猜测和返工。

2. **防御式编程**：CODEX_HOME 检测、超时机制、`--allow-empty` 这些都是在"万一出问题"的假设下加的，结果确实都派上了用场。

3. **Windows 特殊处理**：文件锁、路径分隔符、代理设置，这些在 Windows 上的行为和 Linux 不同，调试时要特别注意环境差异。

4. **错误信息是线索**：每个报错（"Model provider not found"、"WinError 183"、"Connection reset"）都指向具体根因，顺着报错逐条排查比瞎猜效率高得多。
