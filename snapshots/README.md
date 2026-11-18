# 首轮 I/O 记录

BP、AP 各自使用 `snapshots/<flow>/inputs/records.json` 保存 I 点，使用 `snapshots/<flow>/outputs/` 保存 GV 快照。两个 Flow 在运行前分别创建默认 GV 和 Env，当前只执行分支 0。

`<flow>_0.0.snap` 是初始 GV 的 O 点。I 从 `<flow>_0.0` 开始按进入顺序编号，成功操作的 O 从 `<flow>_0.1.snap` 开始按完成顺序编号。O 文件头的可选 `input_point` 关联其来源 I；初始 O 没有来源。每个 I 的 `input_snapshot` 是相对于 snapshots 根目录的路径，指向进入时最近产生的 O 点。

设点只针对 spec Actions 内的裸操作行和 drive 行。裸操作执行前记录 I、成功后保存 O；drive 在 Signal 准备好后记录 I，被调用方法正常完成契约、release 及返回值赋值后保存 O。方法边界、保护域声明、内部指令、assert 和 pass 不额外设点。

失败操作只保留 I，没有对应 O；先前成功的内部操作快照保留。独立 AP 在等待启动通知时退出，首轮产生 3 个 I 点和 1 份初始 O 快照。FlowRun 的计数器和记录状态独立于 GV、Env；Env、队列和 Frame 不写入记录文件。

`records.json` 包含记录格式版本、模型版本、涉及 spec 的指纹，以及 I 的名称、base、输入快照、源位置和引用感知的动作输入。首轮 base 均为 null，无 handled。当前不提供并发加载、输入解码恢复、重放或派生分支。

GV 模型版本保持 9，类型和回调仍注册于 `flows.model_specs`。通用加载器继续支持没有 `input_point` 字段的版本 9 快照；旧的平铺目录和按方法退出编号的文件不能作为新 I/O 历史使用，应清理后重新生成。

`make run` 先清理生成数据再执行 BP、AP；InitProc 不接入 main。已有同 Flow 记录或输出时，直接重复运行会拒绝覆盖。`make clean_snap` 递归清理记录、快照及生成临时文件，`make clean` 还清理 Python 缓存；保留本说明文件。生成数据不提交。
