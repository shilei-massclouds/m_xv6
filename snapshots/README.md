# I/O 记录与恢复

BP、AP 各自使用 `snapshots/<flow>/inputs/records.json` 保存 I 点，使用 `snapshots/<flow>/outputs/` 保存 O 点索引和断点快照。两个 Flow 的分支 0 分别从默认 GV 和 Env 开始。

`<flow>_0.0.snap` 是初始 GV 的逻辑 O 点。每个分支独立编号，派生分支的首个 I 和实际生成的首个 O 均从 0 开始。O 元数据的可选 `input_point` 关联其来源 I，初始 O 没有来源。每个 I 的 `input_snapshot` 是相对于 snapshots 根目录的逻辑路径。

设点针对 spec Actions 内的操作行。裸操作成功、`emit` 入队、`drive` 完成队列处理并交付结果时各自产生 O。方法边界、保护域声明、内部指令、assert 和 pass 不额外设点。

独立 AP 在等待启动通知时以 `BUSYCHECK` 暂停，`ap_0.1.snap` 是第一个可恢复的 `break` O 点。主入口从该点续跑，发现下一批 I/O 点，直到再次到达同一处 `break`。可恢复快照采用格式 2，把 Env、Frame 和 Signal 队列与 GV 一起保存。普通 O 点采用格式 1，仅保存 GV。

`records.json` 包含记录格式版本、模型版本、涉及 spec 的指纹，以及 I 的名称、base、输入快照、源位置和动作输入。派生分支首个 I 的 `base` 指向目标 Flow 中被分叉的 I 点，可能位于分支 0 或上一阶派生分支。`input_snapshot` 指向该分支使用的来源 Flow 候选 O。

主入口把普通 O 正文存到 `.objects/<SHA-256>.snap-body`，相同正文只保存一次。`break` 正文按 Flow 合并到 `outputs/breaks.pack`。`outputs/index.json` 将逻辑 `.snap` 文件名映射到摘要、来源 I 点和断点在包中的位置，加载器可直接使用逻辑路径。推导期间的断点和索引在内存中，退出时按断点包、索引、`records.json` 的顺序发布。强制终止时可能丢失尚未发布的数据。

GV 模型版本保持 9，类型和回调仍注册于 `flows.model_specs`。通用加载器继续支持没有 `input_point` 字段的版本 9 快照；旧的平铺目录和按方法退出编号的文件不能作为新 I/O 历史使用，应清理后重新生成。

`make run` 先清理生成数据再执行 BP、AP；InitProc 不接入 main。已有同 Flow 记录或输出时，直接重复运行会拒绝覆盖。`make clean_snap` 递归清理记录、快照及生成临时文件，`make clean` 还清理 Python 缓存；保留本说明文件。生成数据不提交。
