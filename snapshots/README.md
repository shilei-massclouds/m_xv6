# 自动快照

通过 `drive` 或 `emit` 调用的私有 action（名称以 `_` 开头）退出时，无论正常返回还是抛出异常，都由 `framework/engine.py` 的 `Signal.handle()` 在 `finally` 中先执行 `release`，成功后调用封装的 `_save_snapshot()` 保存 `gv`。快照记录 `release` 后的状态。不再设置手动保存点。

文件名为 `<flow>_<分支序号>.<位置序号>.snap`，checkpoint 为不含扩展名的文件名。例如当前 BP 生成 `bp_0.0.snap`、`bp_0.1.snap`、`bp_0.2.snap` 等。位置序号按调用退出顺序从 0 递增，分支序号当前均为 0。嵌套调用共享环境，内层先退出时先获得序号；异常逐层退出时，各私有 action 在自身 `release` 成功后分别保存，公开 action 本身不产生快照。方法进入前的恢复断点与退出时的快照位置序号分别定义；旧命名文件应清理后重新生成。

Flow 名称来自 `TaskLocalEnv.name`，创建环境时必须显式提供；路径集中由 `framework/snapshot_paths.py` 生成。当前模型版本为 7。快照仅保存从 `gv` 可达的对象图，不包含 Python 调用栈或未从 `gv` 引用的局部环境。

`TaskLocalEnv.snapshot_candidates` 保存输入快照候选列表，元素为 `SnapshotCandidate(name, handled=False)`；各环境独立创建列表。启动时列表默认为空，直接复用当前 `gv`；预留执行配置 `env.range` 默认为 `None`，不限制步骤。候选加载、断点定位和执行现场恢复机制尚未实现。

`acquire` 失败时 action 尚未执行，不保存快照。`release` 失败时跳过本次保存，不消耗位置序号，已有快照保留。保存失败时 `release` 已完成，不消耗位置序号，保存异常自然向上传播；方法本身也失败时，方法原异常保留在异常链中。旧的手动快照文件不再生成。

`make run` 清理快照后在同一进程中依次运行 BP、AP，AP 复用 BP 执行后的当前模型。`make clean` 删除本目录下的 `*.snap`，保留此说明文件。生成文件和临时文件不提交。
