# 启动快照

本目录用于保存运行时快照。此说明文件随代码提交，以便检出仓库后保留目录；生成的快照及临时文件不提交。

路径和快照点名称集中定义在 `framework/snapshot_paths.py`：

| 文件 | 快照点 | 状态 |
| --- | --- | --- |
| `bp_initialized.snap` | `bp_initialized` | BP 完成 `initproc` 初始化并通知其他 hart 后、启用本地多任务和调用调度器前的状态 |
| `bp_before_schedule.snap` | `bp_before_schedule` | BP 调度器关闭本地 IRQ 后、进入 `_schedule` 前的状态，供 `InitProcOrigin("bp")` 加载 |
| `ap_before_schedule.snap` | `ap_before_schedule` | AP 调度器关闭本地 IRQ 后、进入 `_schedule` 前的状态，供 `InitProcOrigin("ap")` 加载 |

BP 在新进程中从 `gv` 的默认值开始推导；AP 从 `bp_initialized.snap` 开始推导，并保留已有 BP 快照。InitProc 从指定来源的调度前快照独立运行，加载失败时报错，不自动补跑 BP/AP，也不改写来源快照。当前模型版本为 5，旧快照需重新生成。

`make run` 清理快照后，依次在四个独立进程中运行 BP、AP、InitProc 来源 BP、InitProc 来源 AP。运行入口和通用快照接口参见仓库根目录的 `README.md`。

`make clean` 会删除本目录下的 `*.snap`，保留此说明文件。
