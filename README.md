# m_xv6

BP、AP 和 InitProc Flow 均由 spec 描述并解释执行，入口分别为 `flows/BpInitFlow.spec`、`flows/ApInitFlow.spec` 和 `flows/InitProcFlow.spec`，依赖对象也由各目录中的 spec 定义。`flows/model_specs.py` 统一注册模型，`global_vars.py` 通过 `new_model(...)` 创建 Flow 及其依赖对象。旧 Python 模型、Flow 导出壳和 Holder 已删除；解释器、框架和启动入口继续由 Python 实现。首个验证输入为 `kernel/ProcTable.spec`；语法、执行语义和验证方式见 [framework/SPEC.md](framework/SPEC.md)。I 点恢复和并发输入调度尚未接入。

当前依次运行 BP、AP 推导：

```sh
python3 main.py
```

执行 `make` 或 `make run` 会先清理生成数据，再在同一进程中依次独立运行 BP、AP。每个 Flow 启动前重置 GV、创建默认 Env 和独立的 `FlowRun` 上下文。AP 不复用 BP 的结果，因此默认会在等待启动通知时以 `BUSY_CHECK` 退出；这属于正常推导终点。`make clean` 还会清理 Python 缓存。

I/O 点只由 spec 的 Actions 操作行触发：裸操作执行前记录 I 点，成功后保存 O 点；drive 行在 Signal 参数求值完成后记录 I 点，被调用方法正常通过契约和 release、调用方完成返回值赋值后保存 O 点。内部 Actions 操作继续产生自己的点，方法名是否以 `_` 开头不影响设点。

方法边界、Requires/Ensures、保护域声明、解释器内部指令、`assert` 和 `pass` 不额外设点。每行裸操作是一个原子操作；Actions 中显式调用同步原语也属于裸操作。普通 Python Signal 调度仍保留，但不自动生成 spec I/O 点。

`TaskLocalEnv` 必须提供当前 Flow 的 `name`，只保存 CV、调用深度和 Signal 队列。`FlowRun(gv, env, model_version)` 独立维护当前分支、I/O 计数器、I 点记录及最近的输出；不再有 Env 候选列表、`handled` 或 `range`。执行 spec 必须处于对应 Env 的活动 `FlowRun` 中；一个上下文可执行多个根 Signal，但不能嵌套或复用。

每个 Flow 在 `snapshots/<flow>/inputs/records.json` 中保存 I 点记录，在 `snapshots/<flow>/outputs/` 中保存 GV 快照。I 点从 `<flow>_0.0` 按进入顺序编号；初始 O 点 `<flow>_0.0.snap` 保存运行开始时的 GV，之后按操作完成顺序从 1 编号。I/O 独立计数，不用同名推断对应关系；O 文件头的可选字段 `input_point` 指向来源 I 点，初始 O 没有这个字段。当前仅运行分支 0。路径固定相对于仓库根目录，与工作目录无关。

记录文件包含格式/模型版本、涉及 spec 的指纹，以及各 I 点的名称、`base=null`、输入快照路径、源位置和动作输入。Signal 中的模型、容器和绑定方法使用引用感知编码，Env/Frame/队列不写入记录；详见 [framework/SPEC.md](framework/SPEC.md)。本轮只记录，不提供候选加载、重放或恢复。

操作失败、契约失败或 StepPoint 不生成对应 O 点；已经成功的内部操作输出保留。I 写入失败时不执行操作；O 保存失败时不消耗 O 序号、不覆盖已有文件、不回滚 GV。异常后停止该上下文的后续执行，保护域和 Frame 正常清理。现有同 Flow 的记录和输出不会被覆盖，再次首轮运行前需执行 `make clean_snap`；该命令递归清理生成记录、快照和临时文件，保留说明文件。

InitProc 已有 spec，仍不接入 `main.py`。如需手动运行，在已准备好的 GV 上创建 `InitProcOrigin(source="bp")` 或 `InitProcOrigin(source="ap")`，并在 `with FlowRun(gv, origin.env, MODEL_VERSION):` 中调用 `origin.start()`。`source` 仅保留来源标记，不加载快照；`FlowRun` 本身也不重置 GV，独立初始化由 `main.py` 负责。

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

快照保存 `gv` 的全部实例属性，包括动态属性、容器、枚举、绑定方法、共享引用和循环引用。绑定方法保留其函数和所属对象，函数需符合标准 Pickle 的可导入要求。加载时保留当前 `gv` 的身份，并将快照中的根引用绑定到它；内部对象重新创建，当前存在但快照中没有的属性会被删除。外部代码持有的旧内部对象引用仍指向旧对象，需要重新通过 `gv` 取值。未从 `gv` 引用的局部环境不会被保存。

每个文件由固定标识行 `M_XV6_SNAPSHOT`、一行 UTF-8 JSON 文件头、一个空行和未压缩的 Pickle 二进制正文组成。文件头包含 `format_version`、`model_version`、`checkpoint`、UTC 时间 `created_at`、正文校验和 `payload_sha256`，以及可选的来源 `input_point`。每份快照独立保存在磁盘，不建立常驻内存快照库。

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

Python 契约函数的入口选择保存在当前 Python 调用帧的保留局部变量 `__m_xv6_contract_state__` 中，随调用结束释放；直接调用、嵌套、递归及交错恢复的生成器各自独立。Python 调用者在方法入口和正常出口显式放置检查，框架不自动包装方法或补跑遗漏的出口检查。spec 契约由解释器在对应 Frame 中选择入口并检查出口，详见 [framework/SPEC.md](framework/SPEC.md)。BP 的 `_start` 和 AP 的 `start` 检查调度器从 `RUNNING` 到 `SWITCHED` 或 `WFI`，InitProc 检查内核态到用户态，`ProcTable._pick_runnable` 和 `_put_runnable` 按入口队列为空或非空的分支检查出口。

契约函数不保存、加载或回滚状态。契约失败会终止本次调用，调用它的 spec drive 行不生成 O 点，之前成功操作的快照保留。

运行检查和测试（`tests/` 仅保障 `framework/` 的功能，测试快照写入临时目录）：

```sh
make test
```
