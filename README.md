# m_xv6

BP、AP 和 InitProc Flow 均由 spec 描述并解释执行，入口分别为 `flows/BpInitFlow.spec`、`flows/ApInitFlow.spec` 和 `flows/InitProcFlow.spec`，依赖对象也由各目录中的 spec 定义。`flows/model_specs.py` 统一注册模型，`global_vars.py` 通过 `new_model(...)` 创建 Flow 及其依赖对象。语法、执行语义和验证方式见 [framework/SPEC.md](framework/SPEC.md)。

当前入口 `main.py` 将 `[BpInitOrigin(), ApInitOrigin()]` 传给通用推导器：

```sh
python3 main.py
```

执行 `make` 或 `make run` 会先清理生成数据，再按以下顺序推导。

1. 一阶：每个 Origin 从默认 GV 和自己的 Env 启动。遇到 `break` 时从对应 O 点续跑，发现后续的 I/O 点；同一条线路再次到达同一处 `break` 时保留暂停状态。AP 的 `break(CpuFlowState.BUSYCHECK, "self", "wait")` 会先退出旧的 `wait` 保护域，续跑再进入 `wait` 检查通知。
2. 二阶：对每一对有方向的 Flow，将来源 Flow 的所有一阶 O 点快照分别加载到目标 Flow 的每个一阶 I 点。BP → AP 和 AP → BP 分开推导。
3. 三阶及以上：对上一阶每条线路新产生的每个 I 点，依次应用尚未参与该线路的各个 Flow 的一阶 O 点快照，最多到输入 Flow 数量对应的阶数。

每次分叉替换整个 GV，保留目标线路的 Env、Signal、局部值和已选契约分支。遇到语法错误、契约失败或其他执行错误时立即中止，并报告触发错误的位置。当前 BP → AP 的 52 个分支均可推导；AP → BP 在把 `ap_0.0.snap` 应用于 `bp_0.11` 的 `select_next` 时，因为 AP 的初始 GV 无法提供该 I 点引用的 `gv.initproc`，以快照兼容性错误中止。

I/O 点只由 spec 的 Actions 操作行触发。非私有方法中，裸操作执行前记录 I 点，成功后保存 O 点；普通 `drive` 在 Signal 参数求值后记录 I 点，在队列处理完毕并交付结果后保存 O 点。`with` 只包住一行 `drive`（可赋给局部变量），整块共用一对 I/O 点：进 Guard 前记 I，退 Guard 后记 O。私有方法的裸操作、`drive` 和 `with` 不设点；被调的非私有方法仍按自己的 Actions 设点。`emit` 继续在入队前后设点；`break` 通过所属 CpuFlow 实例最内层活动契约的 Ensures 后保存可恢复的 O 点。

方法边界、Requires/Ensures、保护域声明、解释器内部指令、`assert` 和 `pass` 不额外设点。每行裸操作是一个原子操作；Actions 中显式调用同步原语也属于裸操作。普通 Python Signal 调度仍保留，但不自动生成 spec I/O 点。

`TaskLocalEnv` 必须提供当前 Flow 的 `name`，只保存 CV、调用深度和 Signal 队列。`FlowRun(gv, env, model_version)` 独立维护当前分支、I/O 计数器、I 点记录及最近的输出；不再有 Env 候选列表、`handled` 或 `range`。执行 spec 必须处于对应 Env 的活动 `FlowRun` 中；一个上下文可执行多个根 Signal，但不能嵌套或复用。

每个 Flow 在 `snapshots/<flow>/inputs/records.json` 中保存 I 点记录，在 `snapshots/<flow>/outputs/` 中保存 O 点索引和断点包。I/O 点分别以 `<flow>_<分支号>.<序号>` 独立编号。分支 0 的初始 O 保存默认 GV，其余分支以候选 O 为初始输入，首个完成操作的 O 从 0 编号。O 点的元数据字段 `input_point` 指向来源 I。路径固定相对于仓库根目录。

派生分支首个 I 的 `base` 是被分叉的 I 点名，`input_snapshot` 是来源 Flow 的候选快照路径。其他 I 点的 `base` 为 `null`。待分叉 I 点的运行状态暂存在本次推导的内存中，`break` O 点继续持久化 Env、Frame 和 Signal 队列。详见 [framework/SPEC.md](framework/SPEC.md)。

主入口的 I 点记录在内存中逐条追加，分支和续跑共用这份历史。推导结束或因可被程序处理的错误退出时，每个 Flow 原子写出一次 `records.json`。强制终止可能丢失尚未写盘的 I 点。单独调用 `FlowRun` 时仍逐点写盘。

操作失败、契约失败或 StepPoint 不生成对应 O 点；已经保存的内部操作输出保留。需要设点的操作必须先成功记录 I 点。O 保存失败时保留原编号和已有文件。异常后清理保护域和 Frame。再次首轮运行前可用 `make clean_snap` 清理生成记录、快照和临时文件。

InitProc 的 spec 保留，目前不作为独立 Flow 参与推导。`FlowRun` 本身不重置 GV；一阶推导由 `derive_flows` 在每个 Origin 启动前恢复默认 GV。

`GlobalVars.initproc` 是初始为 `None` 的可选进程引用，不预先创建进程；模型暂时没有 `childproc` 字段。BP 的 `userinit` 调用 `ProcTable._alloc` 创建进程、分配资源，并在其进入 `RUNNABLE` 状态后通过 `put_runnable(task=proc)` 入队。首次分配会设置 `gv.initproc`，后续分配创建独立进程，并保留该引用。

`ProcTable.table: list[Proc]` 保存可运行进程引用；`pick_runnable` 移除并取出队首，空队列则输出 `None`。快照既支持分配前的 `initproc is None` 状态，也会保持分配后队列元素与 `gv.initproc` 的对象身份关系；额外进程可以只通过队列或其他模型对象引用。

当前模型版本为 9，生成类型及回调的注册模块仍为 `flows.model_specs`。清理旧 Python 模型不改变模型结构和类型路径，现有版本 9 的快照可以继续加载；其他模型版本的快照按不兼容处理。`make run` 会清理并重建 BP、AP 自动快照。`snapshots/README.md` 随代码提交以保留目录，运行时生成文件加入忽略规则。

`System.drive(env, "self", action, **kwargs)` 和 `System.emit(env, "self", action, **kwargs)` 将目标绑定到调用它们的 System 实例。创建 Signal 时通过 `source` 保存该实例的引用，处理时解析 `"self"`，随后按原有流程执行 `acquire`、目标方法和 `release`。其他目标名称仍通过 `gv` 路径查找。

嵌套调用和 `emit` 各自记录当前调用对象。队列中的信号保留其来源对象；快照恢复时，来源引用与恢复后的对象图保持一致。

spec 方法可用 `Def _pick_runnable() -> nextp:` 声明输出，调用方通过 `next_runnable = drive("proc_table", "pick_runnable")` 接收结果。spec 禁止 `return` 和 `if`，条件分支通过多组 Requires/Actions/Ensures 表达；每个 Actions 操作必须独占一个物理行。BP 和 AP 共用 `CpuFlow.spec` 中的调度器，均直接使用返回值；原有 Holder 返回值桥接已移除。

spec 中的 `ProtectedBy io` 将类型或方法的保护关系关联到 `gv.io`。声明接受非空对象路径，也支持 `holder.lock` 这样的点号路径；方法声明覆盖类型声明。每次检查时都从当前 `gv` 解析路径，因此替换对象或恢复快照后会使用当前实例。名称不存在时抛出 `AttributeError`。

```text
Def _sync_putc():
    ProtectedBy io
    Actions:
        drive("uart", "putc_sync")
```

声明本身不加锁。调用者仍需通过 `GuardSpinLock(env.cv, gv.io)` 等原语建立保护；保护栈按对象身份匹配，其他同类实例不能代替 `gv.io`。每个可见竞争域仍需满足计数约束。Console 和 UART 的同步输出方法都声明由 `"io"` 保护，使 `IO.printk` 持有的实例保护能覆盖整条输出链路。

可以加载已经自动生成的快照：

```python
from framework.snapshot import load_snapshot
from global_vars import MODEL_VERSION, gv

metadata = load_snapshot(
    gv,
    "snapshots/bp/outputs/bp_0.0.snap",
    model_version=MODEL_VERSION,
    expected_checkpoint="bp_0.0",
)
```

`expected_checkpoint` 可以省略；指定时必须与文件头一致。文件格式版本和模型版本始终检查。通用实现位于 `framework/snapshot.py`，模型版本集中定义为 `global_vars.MODEL_VERSION`；模型结构或语义发生不兼容变化时应提升模型版本。快照文件不包含 Python 类和函数的代码，跨进程加载时需有兼容且可导入的模型代码。

快照保存 `gv` 的全部实例属性，包括动态属性、容器、枚举、绑定方法、共享引用和循环引用。绑定方法保留其函数和所属对象，函数需符合标准 Pickle 的可导入要求。加载时保留当前 `gv` 的身份，并将快照中的根引用绑定到它。外部代码持有的旧内部对象引用仍指向旧对象，需要重新通过 `gv` 取值。普通快照采用格式 1，`break` 检查点采用格式 2 并同时保存执行环境。通用 `load_snapshot` 可从两种格式提取 GV。

独立快照由标识行 `M_XV6_SNAPSHOT`、一行 UTF-8 JSON 文件头、一个空行和 Pickle 二进制正文组成。文件头包含格式与模型版本、快照点、UTC 时间、正文 SHA-256 和可选的来源 I 点。

主入口为普通 O 点计算正文 SHA-256，正文相同的 O 点共用 `snapshots/.objects/<摘要>.snap-body`。同一 Flow 的 `break` 检查点合并到 `outputs/breaks.pack`，索引记录每个断点的偏移和长度。推导期间可以从内存恢复，退出时先保存断点包，再发布 O 点索引和 I 点记录。进程被强制终止时，尚未发布的断点可能丢失。

仍可把原来的逻辑 `.snap` 路径传给 `load_snapshot`。加载器按索引定位正文并逐条验证摘要，`snapshot_paths.branch_outputs` 仍按逻辑名称枚举。单独调用 `save_snapshot` 或在批次外运行 `FlowRun` 时，继续使用独立文件。

保存时先写同目录临时文件，完成校验和与文件同步后原子替换目标文件。保存失败不会覆盖原文件；加载会先验证文件头、版本、快照点及正文校验和，完整反序列化成功后再替换实例状态。缺失文件、格式损坏和 I/O 失败抛出 `SnapshotError`；格式损坏和版本/快照点不匹配分别使用其子类 `SnapshotFormatError`、`SnapshotCompatibilityError`，可从 `framework.snapshot` 导入。

Pickle 加载会执行代码，只加载可信来源的快照。动态属性也需支持 Pickle；局部函数、lambda、打开的文件等不受支持，保存时会明确报错。

模型契约使用 spec 中的 `Requires / Actions / Ensures` 声明，由解释器检查，详见 [framework/SPEC.md](framework/SPEC.md)。框架仍保留 `framework/contracts.py` 中的 Python 契约接口：`requires(list[bool])` 和 `ensures(list[list[bool]])`。调用这些接口时，每次方法调用至多执行一次 `requires` 和一次 `ensures`：

```python
from framework.contracts import ensures, requires


def adjust(self, amount):
    requires([amount > 0, amount < 0])
    previous = self.value
    self.value += amount
    ensures(
        [
            [self.value > previous],  # 对应入口 amount > 0
            [self.value < previous],  # 对应入口 amount < 0
        ]
    )
```

`requires` 的元素做 OR，至少一项为真才允许继续执行，入口分支必须互斥。框架保存入口成立的分支索引；`ensures` 外层长度必须与入口分支数相同，只检查对应的一行，该行的元素也做 OR。方法内修改状态不会改变已选择的入口分支。未选中的出口行不参与成败判定，但所有行的参数类型都会验证。

`requires([])` 必然失败；被选中的出口行为空也必然失败。可以只写 `requires`；只写 `ensures` 时按隐含的 `requires([True])` 处理，外层只能有一行。重复调用同一阶段、`requires` 出现在 `ensures` 之后、出口行数不匹配或当前调用有多个入口分支同时为真，均抛出 `ValueError`。完整证明条件在所有可能输入下互斥的机制仍为 TODO，契约编写者需保证互斥。

列表元素只接受布尔值，形状或元素类型不匹配时抛出 `TypeError`，不接受 lambda 等 callable。Python 在调用契约函数前计算列表中的所有表达式，因此列表元素之间没有短路求值；条件表达式内仍可使用 `and` 和 `or`。表达式自身抛出的异常直接向上传播。

入口没有成立的分支，或选中出口行没有成立的条件时，立即抛出 `ContractViolation`，未捕获时程序终止。错误消息报告调用处的文件、行号、函数/方法名和 `requires`/`ensures` 阶段，也可以通过异常的 `filename`、`lineno`、`function`、`phase` 属性读取。出口失败还报告从 0 开始的入口分支索引 `branch_index`。`python -O` 下仍然检查。

Python 契约函数的入口选择保存在当前 Python 调用帧的保留局部变量 `__m_xv6_contract_state__` 中，随调用结束释放；直接调用、嵌套、递归及交错恢复的生成器各自独立。Python 调用者在方法入口和正常出口显式放置检查，框架不自动包装方法或补跑遗漏的出口检查。spec 契约由解释器在对应 Frame 中选择入口并检查出口，详见 [framework/SPEC.md](framework/SPEC.md)。BP 和 AP 的 `_start` 检查 Flow 的状态，InitProc 检查内核态到用户态，`ProcTable._pick_runnable` 和 `_put_runnable` 按入口队列为空或非空的分支检查出口。

契约函数不保存、加载或回滚状态。契约失败会终止本次调用，调用它的 spec drive 行不生成 O 点，之前成功操作的快照保留。

运行检查和测试（`tests/` 仅保障 `framework/` 的功能，测试快照写入临时目录）：

```sh
make test
```
