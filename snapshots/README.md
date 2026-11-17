# 自动快照

通过 `drive` 或 `emit` 调用的私有 action（名称以 `_` 开头）正常返回并完成 `release` 后，由 `framework/engine.py` 的 `Signal.handle()` 自动保存 `gv`。不再设置手动保存点。

文件名为 `<flow>_<位置序号>.<分支序号>.snap`，checkpoint 为不含扩展名的文件名。例如当前 BP 生成 `bp_0.0.snap`、`bp_1.0.snap` 等。位置序号按调用返回顺序从 0 递增，分支序号当前均为 0。嵌套调用共享环境，内层先返回时先获得序号；公开 action 本身不产生快照。

Flow 名称来自 `TaskLocalEnv.name`，创建环境时必须显式提供；路径集中由 `framework/snapshot_paths.py` 生成。当前模型版本为 7。快照仅保存从 `gv` 可达的对象图，不包含 Python 调用栈或未从 `gv` 引用的局部环境。

调用或保存失败不会撤销已经生成的快照。当前只运行 BP；AP、InitProc 的旧加载入口尚未接入自动快照，旧的手动快照文件不再生成。

`make run` 清理快照后运行 BP。`make clean` 删除本目录下的 `*.snap`，保留此说明文件。生成文件和临时文件不提交。
