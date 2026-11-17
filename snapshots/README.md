# 自动快照

通过 `drive` 或 `emit` 调用的私有 action（名称以 `_` 开头）退出时，无论正常返回还是抛出异常，都由 `framework/engine.py` 的 `Signal.handle()` 在 `finally` 中先执行 `release`，成功后调用封装的 `_save_snapshot()` 保存 `gv`。快照记录 `release` 后的状态。不再设置手动保存点。

文件名为 `<flow>_<位置序号>.<分支序号>.snap`，checkpoint 为不含扩展名的文件名。例如当前 BP 生成 `bp_0.0.snap`、`bp_1.0.snap` 等。位置序号按调用退出顺序从 0 递增，分支序号当前均为 0。嵌套调用共享环境，内层先退出时先获得序号；异常逐层退出时，各私有 action 在自身 `release` 成功后分别保存，公开 action 本身不产生快照。

Flow 名称来自 `TaskLocalEnv.name`，创建环境时必须显式提供；路径集中由 `framework/snapshot_paths.py` 生成。当前模型版本为 7。快照仅保存从 `gv` 可达的对象图，不包含 Python 调用栈或未从 `gv` 引用的局部环境。

`acquire` 失败时 action 尚未执行，不保存快照。`release` 失败时跳过本次保存，不消耗位置序号，已有快照保留。保存失败时 `release` 已完成，不消耗位置序号，保存异常自然向上传播；方法本身也失败时，方法原异常保留在异常链中。当前只运行 BP；AP、InitProc 的旧加载入口尚未接入自动快照，旧的手动快照文件不再生成。

`make run` 清理快照后运行 BP。`make clean` 删除本目录下的 `*.snap`，保留此说明文件。生成文件和临时文件不提交。
