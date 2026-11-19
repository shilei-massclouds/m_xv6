# I/O 记录与恢复

BP、AP 各自使用 `snapshots/<flow>/inputs/records.json` 保存 I 点，使用 `snapshots/<flow>/outputs/` 保存快照。两个 Flow 的分支 0 分别从默认 GV 和 Env 开始。

`<flow>_0.0.snap` 是初始 GV 的 O 点。每个分支独立编号，派生分支的首个 I 和实际生成的首个 O 均从 0 开始。O 文件头的可选 `input_point` 关联其来源 I，初始 O 没有来源。每个 I 的 `input_snapshot` 是相对于 snapshots 根目录的路径。

设点针对 spec Actions 内的操作行。裸操作成功、`emit` 入队、`drive` 完成队列处理并交付结果时各自产生 O。方法边界、保护域声明、内部指令、assert 和 pass 不额外设点。

独立 AP 在等待启动通知时以 `BUSYCHECK` 暂停，首轮产生 3 个 I 点和 2 份 O 快照，其中 `ap_0.1.snap` 是可恢复的 `break` O 点。可恢复快照采用格式 2，把 Env、Frame 和 Signal 队列与 GV 一起保存。普通 O 点采用格式 1，仅保存 GV。

`records.json` 包含记录格式版本、模型版本、涉及 spec 的指纹，以及 I 的名称、base、输入快照、源位置和动作输入。AP 的派生分支首个 I 以 `base` 指向分支 0 的 `ap_0.0` 或 `ap_0.3`，`input_snapshot` 指向该分支使用的 BP 候选 O。两个基准 I 分别来自默认启动和从第一个 `break` 续跑。

主入口在内存中保留 BP、AP 的 I 点历史，退出前分别原子写出 `records.json`。可处理的错误也会触发导出。推导期间和强制终止后，可能只有 O 点文件。

GV 模型版本保持 9，类型和回调仍注册于 `flows.model_specs`。通用加载器继续支持没有 `input_point` 字段的版本 9 快照；旧的平铺目录和按方法退出编号的文件不能作为新 I/O 历史使用，应清理后重新生成。

`make run` 先清理生成数据再执行 BP、AP；InitProc 不接入 main。已有同 Flow 记录或输出时，直接重复运行会拒绝覆盖。`make clean_snap` 递归清理记录、快照及生成临时文件，`make clean` 还清理 Python 缓存；保留本说明文件。生成数据不提交。
