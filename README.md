# m_xv6

当前运行 BP 推导：

```sh
python3 main.py
```

执行 `make` 或 `make run` 会先清理快照，再运行 BP。`make clean` 清理 Python 缓存以及 `snapshots/` 下的 `*.snap`，保留该目录中的 `README.md`。

快照由 `framework/engine.py` 中的 `Signal._save_snapshot()` 封装，在 `Signal.handle()` 的 `finally` 中先执行 `release`，成功后调用保存。通过 `drive` 或 `emit` 调用的 action 名称以 `_` 开头时，无论正常返回还是异常退出，均在 `release` 成功后尝试保存 `gv`；公开 action 本身不保存，其内部调用的私有 action 仍会保存。框架内部直接调用的辅助方法不属于 action 边界。业务代码不再手动调用保存接口。

`TaskLocalEnv` 必须提供当前 Flow 的 `name`，BP Origin 创建 `TaskLocalEnv(name="bp")`。同一 Flow 的嵌套调用和排队信号共享环境，按方法实际退出的顺序分配位置序号；每个环境的 `snapshot_position` 从 0 开始，成功保存后加 1。`snapshot_branch` 当前默认为 0，预留给后续执行分支机制，与契约的入口分支索引无关。

文件名为 `<flow>_<位置序号>.<分支序号>.snap`，文件头的 checkpoint 为不含扩展名的同名字符串。例如 BP 依次生成 `bp_0.0.snap`、`bp_1.0.snap` 等。路径由 `framework/snapshot_paths.py` 生成，固定相对于仓库根目录，与当前工作目录无关；Flow 名称必须是非空 Python 标识符，两个序号必须是非负整数。

方法异常退出（包括 `StepPoint` 和契约失败）时，只要 `release` 成功，也会保存退出现场；嵌套异常逐层退出时，各私有 action 分别处理。快照记录 `release` 后的状态。`acquire` 失败时方法尚未调用，不产生快照；`release` 失败时跳过本次保存，不消耗序号，已有快照保留。保存失败时 `release` 已完成，不消耗序号、不改写已有文件、不回滚模型修改。保存异常自然向上传播；方法本身也失败时，方法原异常保留在异常链中。

每次运行 `main.py` 都在新进程中创建默认的 `gv`，BP 从这些默认值开始推导。每个 Origin 实例独立创建环境，按一次推导使用；新建 Origin 不会重置 `gv`。AP、InitProc 的旧加载入口尚未接入自动快照，当前不由 `main.py` 运行；原有手动快照文件不再生成。

`GlobalVars.initproc` 是初始为 `None` 的可选进程引用，不预先创建进程；模型暂时没有 `childproc` 字段。BP 的 `userinit` 调用 `ProcTable._alloc` 创建进程、分配资源，并在其进入 `RUNNABLE` 状态后通过 `put_runnable(task=proc)` 入队。首次分配会设置 `gv.initproc`，后续分配创建独立进程，并保留该引用。

`ProcTable.table: list[Proc]` 保存可运行进程引用；`pick_runnable` 移除并取出队首，空队列则输出 `None`。快照既支持分配前的 `initproc is None` 状态，也会保持分配后队列元素与 `gv.initproc` 的对象身份关系；额外进程可以只通过队列或其他模型对象引用。

当前模型版本为 7，其他模型版本的快照按不兼容处理。`make run` 会清理并重建自动快照。`snapshots/README.md` 随代码提交以保留目录，运行时生成文件加入忽略规则。

`System.drive(env, "self", action, **kwargs)` 和 `System.emit(env, "self", action, **kwargs)` 将目标绑定到调用它们的 System 实例。创建 Signal 时通过 `source` 保存该实例的引用，处理时解析 `"self"`，随后按原有流程执行 `acquire`、目标方法和 `release`。其他目标名称仍通过 `gv` 路径查找。

嵌套调用和 `emit` 各自记录当前调用对象。队列中的信号保留其来源对象；快照恢复时，来源引用与恢复后的对象图保持一致。

动作可以通过 `structs/holder.py` 中的 `Holder[T]` 传递输出参数，其 `value` 初始为 `None`。例如，调度器创建 `Holder[Proc]()`，通过 `ret=holder` 传给 `pick_runnable`；该方法通过 `**kwargs` 将同一对象继续传给 `_pick_runnable`，由后者写入 `kwargs["ret"].value`。两层方法均返回 `None`，调用者在 `drive` 完成后读取 `holder.value`，其值为实际出队的进程对象或 `None`；调用时需提供 `ret` 关键字参数。

`@protected_by("io")` 将类或方法的保护关系关联到 `gv.io`。声明接受非空对象路径，也支持 `"holder.lock"` 这样的点号路径；方法声明覆盖类声明。每次检查时都从当前 `gv` 解析路径，因此替换对象或恢复快照后会使用当前实例。名称不存在时抛出 `AttributeError`。

```python
from framework.engine import protected_by


@protected_by("io")
def _sync_putc(self, env):
    self.drive(env, "uart", "putc_sync")
```

声明本身不加锁。调用者仍需通过 `GuardSpinLock(env.cv, gv.io)` 等原语建立保护；保护栈按对象身份匹配，其他同类实例不能代替 `gv.io`。每个可见竞争域仍需满足计数约束。Console 和 UART 的同步输出方法都声明由 `"io"` 保护，使 `IO.printk` 持有的实例保护能覆盖整条输出链路。

可以加载已经自动生成的快照：

```python
from global_vars import gv

metadata = gv.load_snapshot("snapshots/bp_0.0.snap", expected_checkpoint="bp_0.0")
```

`expected_checkpoint` 可以省略；指定时必须与文件头一致。文件格式版本和模型版本始终检查。通用实现位于 `framework/snapshot.py`，模型版本集中定义为 `global_vars.MODEL_VERSION`；模型结构或语义发生不兼容变化时应提升模型版本。快照文件不包含 Python 类和函数的代码，跨进程加载时需有兼容且可导入的模型代码。

快照保存 `gv` 的全部实例属性，包括动态属性、容器、枚举、绑定方法、共享引用和循环引用。绑定方法保留其函数和所属对象，函数需符合标准 Pickle 的可导入要求。加载时保留当前 `gv` 的身份，并将快照中的根引用绑定到它；内部对象重新创建，当前存在但快照中没有的属性会被删除。外部代码持有的旧内部对象引用仍指向旧对象，需要重新通过 `gv` 取值。未从 `gv` 引用的局部环境不会被保存。

每个文件由固定标识行 `M_XV6_SNAPSHOT`、一行 UTF-8 JSON 文件头、一个空行和未压缩的 Pickle 二进制正文组成。文件头包含 `format_version`、`model_version`、`checkpoint`、UTC 时间 `created_at` 和正文校验和 `payload_sha256`。每份快照独立保存在磁盘，不建立常驻内存快照库。

保存时先写同目录临时文件，完成校验和与文件同步后原子替换目标文件。保存失败不会覆盖原文件；加载会先验证文件头、版本、快照点及正文校验和，完整反序列化成功后再替换实例状态。缺失文件、格式损坏和 I/O 失败抛出 `SnapshotError`；格式损坏和版本/快照点不匹配分别使用其子类 `SnapshotFormatError`、`SnapshotCompatibilityError`，可从 `framework.snapshot` 导入。

Pickle 加载会执行代码，只加载可信来源的快照。动态属性也需支持 Pickle；局部函数、lambda、打开的文件等不受支持，保存时会明确报错。

方法契约通过 `framework/contracts.py` 中的 `requires(list[bool])` 和 `ensures(list[list[bool]])` 显式检查。每次方法调用至多执行一次 `requires` 和一次 `ensures`：

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

入口选择保存在当前 Python 调用帧的保留局部变量 `__m_xv6_contract_state__` 中，随调用结束释放；直接调用、嵌套、递归及交错恢复的生成器各自独立。调用者在方法入口和正常出口显式放置检查，框架不自动包装方法或补跑遗漏的出口检查。BP/AP 的 `start` 检查调度器从 `RUNNING` 到 `SWITCHED` 或 `WFI`，InitProc 检查内核态到用户态，`ProcTable._pick_runnable` 和 `_put_runnable` 按入口队列为空或非空的分支检查出口。

契约函数不保存、加载或回滚状态。action 中的契约检查失败会终止本次调用，私有 action 仍由 `handle()` 的 `finally` 在 `release` 成功后保存退出现场；已有快照不会撤销或改写。

运行检查和测试（`tests/` 仅保障 `framework/` 的功能，测试快照写入临时目录）：

```sh
make test
```
