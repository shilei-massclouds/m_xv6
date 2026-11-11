# m_xv6

选择 BP 或 AP 推导入口：

```sh
python3 main.py
python3 main.py bp_init_origin
python3 main.py ap_init_origin
```

无参数时运行 BP。位置参数只接受 `bp_init_origin` 和 `ap_init_origin`，未知名称会显示用法并退出。

执行 `make` 或 `make run` 会在两个独立进程中依次运行 BP 和 AP。`make clean` 清理 Python 缓存以及 `snapshots/` 下的 `*.snap`，保留该目录中的 `README.md`。

启动快照路径集中定义在 `framework/snapshot_paths.py`，固定相对于仓库根目录，与当前工作目录无关：

| 文件 | 快照点 | 用途 |
| --- | --- | --- |
| `snapshots/bp_initialized.snap` | `bp_initialized` | `initproc` 初始化完成后、启用其他任务和远端竞争前的模型状态 |

每次运行 `main.py` 都在新进程中创建默认的 `gv`，BP 从这些默认值开始推导。BP Flow 在上述记录点显式保存 `bp_initialized.snap`，然后继续原有流程。

AP 直接加载 `bp_initialized.snap` 并检查快照点，文件缺失或无效时会报错，需要先单独运行 BP。AP 不会重新执行 BP。每个 Origin 实例通过 `default_factory` 创建自己的 `TaskLocalEnv`，按一次推导使用；新建 Origin 不会重置 `gv`。`ApInitFlow` 默认 hart 为 1，入口竞争向量为全零，通过同步原语调整后为 `(local_irq=0, local_tasks=0, remote_irq=1, remote_tasks=1)`，目前只调整竞争环境。

Signal 新增来源对象引用后，模型版本提升为 4，旧版本快照按不兼容处理。`snapshots/README.md` 随代码提交以保留目录，运行时生成文件加入忽略规则。

`System.drive(env, "self", action, **kwargs)` 和 `System.emit(env, "self", action, **kwargs)` 将目标绑定到调用它们的 System 实例。创建 Signal 时通过 `source` 保存该实例的引用，处理时解析 `"self"`，随后按原有流程执行 `acquire`、目标方法和 `release`。其他目标名称仍通过 `gv` 路径查找。

嵌套调用和 `emit` 各自记录当前调用对象。队列中的信号保留其来源对象；快照恢复时，来源引用与恢复后的对象图保持一致。

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

方法契约通过 `framework/contracts.py` 中的 `requires(condition)` 和 `ensures(condition)` 显式检查：

```python
from framework.contracts import ensures, requires


def increase(self, amount):
    requires(amount > 0)
    previous = self.value
    self.value += amount
    ensures(lambda: self.value > previous)
```

参数接受布尔值，或返回布尔值的无参 callable，包括 lambda、普通函数和绑定方法。普通布尔表达式在调用前求值，callable 在检查时调用一次；需要参数时可用 lambda 包起来。非布尔值或 callable 返回非布尔值会抛出 `TypeError`；条件执行自身抛出的异常直接向上传播。

条件为 `False` 时立即抛出 `ContractViolation`，未捕获时程序终止。错误消息报告调用处的文件、行号、所在函数/方法和 `requires`/`ensures` 阶段，也可以通过异常的 `filename`、`lineno`、`function`、`phase` 属性读取。每条条件单独调用，第一条失败后停止，`python -O` 下仍然检查。

调用者在方法入口和正常出口放置检查；框架不自动包装方法，也不自动复制入口状态。BP/AP 的 `start` 已接入：BP 检查 bootstrap hart 和初始进程状态，AP 检查 hart 1 和 BP 初始化后的进程状态；两者入口 CV 为全零，正常出口检查进程状态和 `(0,0,1,1)` CV。

契约检查与快照相互独立，不隐式保存、加载或回滚状态。BP 已记录的初始化快照描述记录时刻的状态，之后的出口检查失败不会撤销或改写该文件。

运行检查和测试（测试快照写入临时目录）：

```sh
make test
```
