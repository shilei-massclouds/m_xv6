# m_xv6

选择 BP 或 AP 推导入口：

```sh
python3 main.py
python3 main.py bp_init_origin
python3 main.py ap_init_origin
```

无参数时运行 BP。位置参数只接受 `bp_init_origin` 和 `ap_init_origin`，未知名称会显示用法并退出。

启动快照路径集中定义在 `snapshot_paths.py`，固定相对于仓库根目录，与当前工作目录无关：

| 文件 | 快照点 | 用途 |
| --- | --- | --- |
| `snapshots/bp_initialized.snap` | `bp_initialized` | `initproc` 初始化完成后、启用其他任务和远端竞争前的模型状态 |

每次运行 `main.py` 都在新进程中创建默认的 `gv`，BP 从这些默认值开始推导。BP Flow 在上述记录点显式保存 `bp_initialized.snap`，然后继续原有流程。

AP 直接加载 `bp_initialized.snap` 并检查快照点，文件缺失或无效时会报错，需要先单独运行 BP。AP 不会重新执行 BP。每个 Origin 实例通过 `default_factory` 创建自己的 `TaskLocalEnv`，按一次推导使用；新建 Origin 不会重置 `gv`。`ApInitFlow` 默认 hart 为 1，入口竞争向量为全零，通过同步原语调整后为 `(local_irq=0, local_tasks=0, remote_irq=1, remote_tasks=1)`，目前只调整竞争环境。

加入 AP Flow 后，模型版本提升为 2，旧版本快照按不兼容处理。`snapshots/README.md` 随代码提交以保留目录，运行时生成文件加入忽略规则。

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

运行检查和测试（测试快照写入临时目录）：

```sh
make test
```
