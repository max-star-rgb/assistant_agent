# 原生 Worktree 编码与改动回灌设计

日期：2026-08-28

## 目标

统一 Assistant 继续使用 Deep Agents 原生 filesystem、`execute` 与 HITL。每个会话 thread 在独立 Git worktree
中读写和验证代码；用户确认后，把该 worktree 相对创建基线的未提交改动应用到本地主工作区，但不创建可达的用户
commit 或 ref、不 push，也不改变本地主分支 HEAD。实现可创建仅用于 Git 原生三方合并、之后可被 GC 的无引用临时
commit object。

本轮同时删除已经退出生产 composition 的旧 coding analysis、patch proposal、validation、review、repair、
integration 与 sandbox framework，避免重新接回第二套 Agent Runtime。

## 状态语义

设本地主工作区与 thread worktree 均从 commit `A` 创建：

```text
初始：
  本地主工作区       HEAD=A，files=A
  thread worktree    HEAD=A，files=A

Agent 修改后：
  本地主工作区       HEAD=A，files=A
  thread worktree    HEAD=A，files=A+B

回灌后：
  本地主工作区       HEAD=A，files=A+B，未 commit
  thread worktree    HEAD=A，files=A+B
```

这里的“回灌”不是 `git merge`。实现从 thread worktree 提取 patch `B`，检查后应用到本地主工作区。

多个从同一 `A` 创建的 thread 可以依次回灌 `B1`、`B2`。Git 能自动三方合并的改动累积为
`A+B1+B2`；不能自动合并的改动进入 thread 内的冲突解决会话，主工作区在解决完成前保持不变。

## 命名与模块

删除 `src/assistant_agent/coding/`，用职责准确的 `src/assistant_agent/worktree/` 替代。保留最小模块：

```text
src/assistant_agent/worktree/
  __init__.py
  manager.py
  backend.py
  tools.py
```

公共实现名称：

```text
CodingWorkspaceService         -> ThreadWorktreeManager
CodingWorkspaceBackend         -> ThreadWorktreeBackend
ReadOnlyCodingWorkspaceBackend -> ReadOnlyThreadWorktreeBackend
CodingConfig                    -> WorktreeConfig
CodingRepositoryConfig          -> WorktreeRepository
```

`manager.py` 只负责 thread worktree 的创建、解析、基线冻结、身份/thread/repository 隔离、TTL 清理以及当前
repository HEAD 查询。`backend.py` 只把解析出的 worktree 委托给 Deep Agents 原生 `LocalShellBackend` 或
`FilesystemBackend`。

## apply_worktree_changes Tool

新增 `apply_worktree_changes`，名称刻意不使用 merge。它是主 Agent 可用的 `dangerous` Tool，调用前由原生
HITL interrupt；同步和异步只读 worker 不注册该 Tool。

Tool 参数不接受宿主路径、目标分支或任意 Git 命令。source worktree、目标本地主工作区、identity、thread 和
repository 均由 `ToolRuntime` 与服务端 composition 决定。

冲突解决依赖本项目当前验证的 Git 2.43 或更高版本，以及 `merge-tree --write-tree --messages -z --merge-base=<commit>`
结构化输出；版本或能力不满足时 composition 应明确失败，不回退到 stderr 解析或自研 merge。

正常执行步骤：

1. 解析当前 thread 的 source worktree，并取得冻结的 `base_commit`。
2. 确认目标本地主工作区 HEAD 仍等于 `base_commit`；HEAD 已变化则失败，要求新建 thread 或人工处理。
3. 分别使用临时 `GIT_INDEX_FILE` 快照 source worktree 与主工作区。临时 index 从 `base_commit` 执行
   `read-tree`，再对对应 working tree 执行 `git add -A`，捕获非 ignored 的新增、修改、删除、mode 与二进制文件；
   双方真实 index 均不改变。
4. 对两个 snapshot tree 分别执行 `git commit-tree -p <base_commit>`，创建没有 ref 的临时 commit object。
   这些对象只表达两个未提交快照共同继承自 `base_commit`，不移动 HEAD/branch、不可从正常 `git log` 到达，也不 push；
   后续由 Git GC 清理。
5. 对两个临时 commit 执行 `git merge-tree --write-tree --messages -z`。优先使用 Git 自身的 merge-base、rename、
   mode、directory/file 与 binary conflict 语义，不在项目中实现合并算法。
6. 若 merge clean，生成“主工作区 snapshot tree → merge result tree”的 binary/full-index patch，并限制到 source
   原始 changed paths（rename 同时包含 old/new endpoint）。锁内再次验证目标 HEAD 和这些路径的 snapshot fingerprint，随后执行 `git apply --check`
   与不带 `--index` 的 `git apply`。
7. 返回结构化 applied/no-changes 结果：source workspace、base commit、patch digest、changed paths 与目标 HEAD。

## 冲突解决会话

`merge-tree` 不能自动合并时，`apply_worktree_changes` 不修改主工作区，而是：

1. 从 `merge-tree -z` 的结构化输出取得 `conflicting_paths` 与冲突类型；不依赖本地化 stderr 文本猜路径。
2. 把 merge result 中 source 原始 changed paths 的文本冲突预览物化到同一 thread worktree。Agent 继续使用现有
   `read_file`、`edit_file` 和 `execute` 查看标准冲突标记、运行检查并写出最终内容，不新增通用主工作区读取 backend。
3. 在 worktree repo 之外保存最小 resolution manifest：base、主工作区 snapshot tree、source snapshot tree、
   changed/conflicting paths、目标路径 fingerprint 与 patch digest。Tool 把冲突作为结构化正常结果返回：
   `status=conflict`、`conflicting_paths` 和 resolution ID，不返回宿主绝对路径；Git/IO/身份等执行失败仍使用
   `ToolException`。
4. Agent 修复后再次调用同一个 `apply_worktree_changes`。Tool 根据 manifest 快照当前 source，生成“已冻结主工作区
   snapshot → resolved source”的 patch，并只覆盖原 changed paths。
5. 锁内验证目标 HEAD 以及所有 affected path 的 mode/blob fingerprint 未变化，再执行全 patch 的
   `git apply --check` 与 `git apply`。验证失败则让旧 resolution 失效并重新建立冲突会话，不覆盖并发的人类修改。
6. 成功后删除 resolution manifest；source worktree 保留最终结果，目标 HEAD 与真实 index 仍不改变。

文本冲突可由 Agent 主动修复。二进制、submodule 或 Git 无法生成可编辑文本预览的冲突只报告路径和双方摘要，
保持人工处理；不得把二进制内容塞进模型上下文。标准冲突标记是否已经清理属于 Agent 与用户审批责任，Tool 不用
字符串启发式伪装语义验证。

同一进程内对目标仓库的 check/apply 使用窄锁串行化。不得使用 `--reject`，不得自动解决冲突，不得自动 stash、
reset、创建 ref、移动 HEAD、push 或删除用户已有改动。允许的 `commit-tree` 只创建不可达临时 object，不属于用户
历史提交。

## Worktree 与 Sandbox 边界

本轮只实现 worktree 隔离，不新增安全 sandbox：

- worktree 隔离不同 thread 的 Git 工作目录、index、HEAD 和未提交修改；
- `LocalShellBackend.execute` 仍运行在宿主 OS identity 下；
- `virtual_mode` 约束 filesystem Tool 路径，但不构成 shell、网络或进程安全边界；
- HITL 负责审批，不提供执行隔离。

未来多租户或不可信部署应把每个 thread worktree 挂载或复制到独立 container/VM/remote sandbox，再由宿主侧
受控应用输出 patch；该能力不进入本轮。

## 删除范围

删除旧实现：

```text
src/assistant_agent/coding/analysis.py
src/assistant_agent/coding/artifact_egress.py
src/assistant_agent/coding/artifacts.py
src/assistant_agent/coding/credentials.py
src/assistant_agent/coding/dependencies.py
src/assistant_agent/coding/dependency_egress.py
src/assistant_agent/coding/inspect_recovery.py
src/assistant_agent/coding/integration.py
src/assistant_agent/coding/patches.py
src/assistant_agent/coding/policy.py
src/assistant_agent/coding/repair.py
src/assistant_agent/coding/review.py
src/assistant_agent/coding/review_repair.py
src/assistant_agent/coding/sandbox.py
src/assistant_agent/coding/sandbox_runner.py
src/assistant_agent/coding/tools.py
src/assistant_agent/coding/validation.py
src/assistant_agent/native_agent/coding_phase.py
tests/tdd/deepagents-coding-agent/
```

原 `coding/config.py`、`coding/models.py` 与 `coding/workspace.py` 不原样迁移，只提取当前生产 worktree 所需的最小
配置、schema 与生命周期逻辑。删除 `.env.example` 中已经失效的旧 coding sandbox 配置。

## 测试与文档

临时 feature 测试验证：

- 不同 thread 解析到不同 worktree；
- async worker 固定读取创建任务时的 repository snapshot；
- patch 捕获新增、修改、删除与二进制文件；
- 回灌后目标 HEAD 不变且改动未 commit；
- 多个不冲突 thread patch 可以累积；
- 非重叠修改由 `merge-tree` 自动合并；文本冲突返回确定性路径并在 thread worktree 生成可编辑预览；
- Agent 修复后只在目标 affected paths 未漂移时回灌最终结果；
- 二进制冲突、基线 HEAD 变化、目标 snapshot 漂移、空 patch 和非法身份均有确定性结果；
- `apply_worktree_changes` 只存在于主 Agent，并进入原生 HITL；
- worker 继续没有写入与 `execute` 能力。

同步更新当前 authority、manifest source globs 和 core invariant 名称，删除对 `CodingWorkspace*` 及旧 coding
framework 的描述。完成前运行目标 pytest、文档 authority validator，并验证现有 8089 dev server 完成 hot reload。

## 非目标

- 不自动 commit、push、创建分支或 PR；
- 不执行会移动 ref/HEAD 或产生用户历史的 Git merge/cherry-pick/squash；
- 不恢复旧 planner、proposal、review、repair 或 validation state machine；
- 不增加 Provider-native Code Interpreter；
- 不把 worktree 宣称为安全 sandbox。
