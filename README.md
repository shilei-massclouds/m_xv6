# m_xv6

选择 BP、AP 或 InitProc 推导入口：

```sh
python3 main.py
python3 main.py bp_init_origin
python3 main.py ap_init_origin
python3 main.py initproc_origin --source bp
python3 main.py initproc_origin --source ap
```

无参数时运行 BP。位置参数接受 `bp_init_origin`、`ap_init_origin` 和 `initproc_origin`，未知名称会显示用法并退出。InitProc 必须显式提供 `--source bp` 或 `--source ap`；BP/AP 入口不接受 `--source`。

执行 `make` 或 `make run` 会先清理快照，再在四个独立进程中依次运行 BP、AP、来源为 BP 的 InitProc 和来源为 AP 的 InitProc。`make clean` 清理 Python 缓存以及 `snapshots/` 下的 `*.snap`，保留该目录中的 `README.md`。

启动快照路径集中定义在 `framework/snapshot_paths.py`，固定相对于仓库根目录，与当前工作目录无关：

| 文件 | 快照点 | 用途 |
| --- | --- | --- |
| `snapshots/bp_initialized.snap` | `bp_initialized` | `initproc` 初始化并通知其他 hart 后、启用本地多任务和调用调度器前的模型状态 |
| `snapshots/bp_before_schedule.snap` | `bp_before_schedule` | BP 调度器关闭本地 IRQ 后、进入 `_schedule` 前的模型状态，供 InitProc 的 BP 来源使用 |
| `snapshots/ap_before_schedule.snap` | `ap_before_schedule` | AP 调度器关闭本地 IRQ 后、进入 `_schedule` 前的模型状态，供 InitProc 的 AP 来源使用 |

每次运行 `main.py` 都在新进程中创建默认的 `gv`，BP 从这些默认值开始推导。BP Flow 在上述记录点显式保存 `bp_initialized.snap`，然后继续原有流程。

`Scheduler.schedule()` 在 `LocalIrq().disable(env.cv)` 之后、调用 `_schedule` 之前，通过与 `gv.bp_scheduler`、`gv.ap_scheduler` 的实例身份比较识别来源，分别保存对应快照。保存后继续调度，随后调度失败也不会撤销已保存的文件。AP 保留已有 BP 快照。

AP 直接加载 `bp_initialized.snap` 并检查快照点，文件缺失或无效时会报错，需要先单独运行 BP。AP 不会重新执行 BP。每个 Origin 实例通过 `default_factory` 创建自己的 `TaskLocalEnv`，按一次推导使用；新建 Origin 不会重置 `gv`。`ApInitFlow` 默认 hart 为 1。AP Origin 从全零竞争向量开始，禁止抢占并启用远端竞争后，以 `(local_irq=0, local_tasks=-1, remote_irq=1, remote_tasks=1)` 进入 AP Flow。BP 通知其他 hart 和保存 `bp_initialized.snap` 时也使用这一向量；两条 Flow 都在调用各自调度器前启用本地多任务，此时向量变为 `(0,0,1,1)`。

`InitProcOrigin(source)` 加载所选来源的调度前快照并检查快照点，不会自动补跑 BP/AP，也不会改写来源文件。每个实例新建自己的 `TaskLocalEnv`，不使用快照内可达的环境；依次启用本地多任务和远端竞争，以 `(local_irq=0, local_tasks=1, remote_irq=1, remote_tasks=1)` 调用恢复后的 `gv.initproc_flow.start()`。`InitProcFlow` 要求入口处于内核态，通过原子检查读取并清除 `first_proc.first`；首次运行时依次调用文件系统初始化和 `initproc._kexec`，非首次运行时跳过这两步，随后通过 `userret` 切换到用户态。正常返回后竞争向量保持不变。

`GlobalVars` 新增 `initproc_flow` 字段后，模型版本提升为 5，旧版本快照按不兼容处理，需重新生成。`snapshots/README.md` 随代码提交以保留目录，运行时生成文件加入忽略规则。

`System.drive(env, "self", action, **kwargs)` 和 `System.emit(env, "self", action, **kwargs)` 将目标绑定到调用它们的 System 实例。创建 Signal 时通过 `source` 保存该实例的引用，处理时解析 `"self"`，随后按原有流程执行 `acquire`、目标方法和 `release`。其他目标名称仍通过 `gv` 路径查找。

嵌套调用和 `emit` 各自记录当前调用对象。队列中的信号保留其来源对象；快照恢复时，来源引用与恢复后的对象图保持一致。

动作可以通过 `structs/holder.py` 中的 `Holder[T]` 传递输出参数，其 `value` 初始为 `None`。例如，调度器创建 `Holder[NextRunnable]()`，通过 `ret=holder` 传给 `pick_runnable`；该方法将同一对象继续传给 `_pick_runnable`，由后者写入 `ret.value`。两层方法均返回 `None`，调用者在 `drive` 完成后读取 `holder.value`；`ret` 是必须显式传入的关键字参数。

`@protected_by("io")` 将类或方法的保护关系关联到 `gv.io`。声明接受非空对象路径，也支持 `"holder.lock"` 这样的点号路径；方法声明覆盖类声明。每次检查时都从当前 `gv` 解析路径，因此替换对象或恢复快照后会使用当前实例。名称不存在时抛出 `AttributeError`。

```python
from framework.engine import protected_by


@protected_by("io")
def _sync_putc(self, env):
    self.drive(env, "uart", "putc_sync")
```

声明本身不加锁。调用者仍需通过 `GuardSpinLock(env.cv, gv.io)` 等原语建立保护；保护栈按对象身份匹配，其他同类实例不能代替 `gv.io`。每个可见竞争域仍需满足计数约束。Console 和 UART 的同步输出方法都声明由 `"io"` 保护，使 `IO.printk` 持有的实例保护能覆盖整条输出链路。

通用文件快照接口也可以由调用者在需要的位置显式使用：

```python
from global_vars import gv

# 父目录需要提前创建；checkpoint 是调用者定义的非空快照点名称。
saved = gv.save_snapshot("/tmp/state.snap", checkpoint="manual")

# 可以在另一个进程中加载；两个接口均返回文件头元数据字典。
loaded = gv.load_snapshot("/tmp/state.snap", expected_checkpoint="manual")
assert saved == loaded
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

入口选择保存在当前 Python 调用帧的保留局部变量 `__m_xv6_contract_state__` 中，随调用结束释放；直接调用、嵌套、递归及交错恢复的生成器各自独立。调用者在方法入口和正常出口显式放置检查，框架不自动包装方法或补跑遗漏的出口检查。BP/AP 的 `start` 检查调度器从 `RUNNING` 到 `SWITCHED` 或 `WFI`，InitProc 检查内核态到用户态，`ProcTable._pick_runnable` 按入口的 `has_runnable` 分支检查出口。

契约检查与快照相互独立，不隐式保存、加载或回滚状态。BP 已记录的初始化快照描述记录时刻的状态，之后的出口检查失败不会撤销或改写该文件。

运行检查和测试（测试快照写入临时目录）：

```sh
make test
```
