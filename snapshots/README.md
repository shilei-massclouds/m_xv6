# I/O 记录与恢复

BP、AP 各自使用 `snapshots/<flow>/inputs/records.json` 保存 I 点，使用 `snapshots/<flow>/outputs/` 保存 O 点索引和断点快照。两个 Flow 的分支 0 分别从默认 GV 和 Env 开始。

Proc 的记录独立存于 `snapshots/proc/`。每次从 CPU 的交接 O 点启动时，首个 I 的 `input_snapshot` 直接指向来源 BP 或 AP 的断点，不再复制一份初始 GV。该断点还包含 CPU 重进 `schedule()` 的入口。

`<flow>_0.0.snap` 是初始 GV 的逻辑 O 点。每个分支独立编号，派生分支的首个 I 和实际生成的首个 O 均从 0 开始。O 元数据的可选 `input_point` 关联其来源 I，初始 O 没有来源，也不进入跨 Flow 候选集合。I 的 `input_snapshot` 指向最近一次保存的 O，路径相对于 snapshots 根目录。若其间执行了不设点的操作，这个 O 不包含那些操作造成的变化。

设点针对 spec Actions 内的操作行。非私有方法的 `with` 和块内唯一的 `drive` 共用一对 I/O 点，分别在 Guard 进入前和退出后。私有方法内的裸操作、`drive` 和 `with` 不设点。`emit` 入队和 `break` 暂停时仍产生各自的 I/O 点；被 `break` 放弃的 `with` 不生成完成 O。方法边界、内部指令、assert 和 pass 不额外设点。

独立 AP 在等待启动通知时以 `BUSYCHECK` 暂停，`ap_0.1.snap` 是第一个可恢复的 `break` O 点。此时旧的 `wait` 保护域已经退出，原始 `wait` Signal 与待完成的调用位置一并保存。主入口续跑时重新进入 `wait`，直到收到通知或再次到达同一处 `break`。可恢复快照采用格式 2，把 Env、Frame 和 Signal 队列与 GV 一起保存。普通 O 点采用格式 1，仅保存 GV。

`records.json` 包含记录格式版本、模型版本、涉及 spec 的指纹，以及 I 的名称、base、输入快照、源位置和动作输入。派生分支首个 I 的 `base` 指向目标 Flow 中被分叉的 I 点，可能位于分支 0 或上一阶派生分支。`input_snapshot` 指向该分支使用的来源 Flow 候选 O。

I 点的可选 `notified` 字段和 O 点的可选 `notified`、`waits_for` 字段使用 GV 中的实例路径，例如 `smp_started`。它们分别记录已经发出的通知和已通过的等待所依赖的通知。暂停的 `wait` 不新增依赖。派生 O 可以接回其来源 Flow 对应的继续位置。

主入口把普通 O 正文存到 `.objects/<SHA-256>.snap-body`，相同正文只保存一次。`break` 正文按 Flow 合并到 `outputs/breaks.pack`。`outputs/index.json` 将逻辑 `.snap` 文件名映射到摘要、来源 I 点和断点在包中的位置，加载器可直接使用逻辑路径。推导期间的断点和索引在内存中，退出时按断点包、索引、`records.json` 的顺序发布。强制终止时可能丢失尚未发布的数据。

GV 模型版本为 10，类型和回调注册于 `flows.model_specs`。此前的快照需要清理后重新生成。

`make run` 先清理生成数据，再推导 BP、AP 和交接启动的 Proc。已有同 Flow 记录或输出时，直接重复运行会拒绝覆盖。`make clean_snap` 递归清理记录、快照及生成临时文件，`make clean` 还清理 Python 缓存；保留本说明文件。生成数据不提交。
