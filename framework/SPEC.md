# Spec 解释执行

第一份输入是移入 `kernel/ProcTable.spec` 的 `_pick_runnable` 定义。BP、AP 和 InitProc 均已迁移为 spec 解释执行，入口为 `flows/BpInitFlow.spec`、`flows/ApInitFlow.spec` 和 `flows/InitProcFlow.spec`。`flows/model_specs.py` 统一注册 26 个对象定义，包括它们实际依赖的内核、驱动、文件系统和结构对象。

```python
from framework.engine import System, TaskLocalEnv
from framework.flow_run import FlowRun
from flows.model_specs import registry
from global_vars import MODEL_VERSION, gv

gv.proc_table = registry.model("ProcTable")()
env = TaskLocalEnv(name="example")
with FlowRun(gv, env, MODEL_VERSION):
    result = System().drive(env, "proc_table", "_pick_runnable")
assert result is None
```

`make run` 清理生成数据后分别从默认 GV、Env 独立运行 BP 和 AP。AP 首轮在 `BUSYCHECK` 保存可恢复的 O 点，随后从这个点续跑一次。两次 AP 推导各暂存首个 I 点，用 BP 的每份 O 快照为这两个 I 点分别生成派生分支。InitProc 暂不接入 `main.py`；手动调用也需显式使用 `FlowRun`，该上下文本身复用传入 GV。注册入口的路径相对于仓库根目录解析。GV 模型均通过 `flows.model_specs.new_model(...)` 创建，需要类型或枚举时使用 `registry.model("CpuFlow")`、`registry.namespace["CpuFlowState"]` 等注册项。

BP 和 AP 继承同一份 `CpuFlow.spec`，调度器通过 `next_runnable = drive("proc_table", "pick_runnable")` 接收 spec 返回值，不再使用 Holder 输出参数。InitProc 的首次执行检查也直接接收 `FirstProc.spec` 的布尔返回值；首次进入时调用文件系统初始化和进程 `_kexec`，最后切换到用户态。

spec 文件名是对象类型名。`Prop table: list[Proc]` 为每个实例创建独立列表，也可以使用 `Prop value: int = 0` 显式指定默认表达式。属性注解描述模型。枚举通过 `Enum Name:` 声明，无赋值的成员以名称的小写形式为值。`Parent Name` 和 `Extends Name` 均引用同一 registry 中的另一个 spec。

`Def method(parameters) -> variable:` 定义方法；箭头指定正常完成 Actions 后输出的局部变量，所选分支必须给该变量赋值。spec 禁止 `return`，没有输出声明的方法正常完成时输出 `None`。命名参数支持字面量默认值；未绑定的关键字参数可以通过 `kwargs` 访问。

方法可依次包含多组 `Requires / Actions / Ensures`：

- 每个 Requires 块的表达式做 OR，必须且只能有一组成立；没有 Requires 时，该组隐含为 True。
- 执行所选组的 Actions，只检查该组的 Ensures；Ensures 块内表达式做 OR。条件结果必须是 bool。
- Requires 无匹配或 Ensures 不成立时，使用现有 `ContractViolation`，报告 spec 文件、行号、方法和入口分支。

Actions 支持赋值、属性/下标赋值、表达式语句、`assert`、`pass`、`with` 保护域，以及 `+=`、`-=`、`*=`。每个操作必须完整写在一个物理行中，不能用括号或反斜杠跨行，也不能在同一行通过分号串联多个操作。保护域声明同样占一个物理行，内部操作各自另起一行。表达式采用 Python 表达式求值，spec 与原 Python 模型一样是可信程序输入。

spec 禁止 `if/else` 语句和条件表达式，分支选择通过互斥的 Requires/Actions/Ensures 组表达。如果判断依赖前一个操作或 Signal 的结果，先完成公共前缀，再将结果作为参数交给带分支的方法；不能把这个判断提前到公共前缀执行之前。`CpuFlow.select_next`、`InitProcFlow.first_init` 和 `ProcTable.set_initproc` 是现有示例。

Signal 调用写成 `drive("target", "action", key=value)`，可直接调用或作为赋值右侧。`emit("target", "action", key=value)` 单独成行，将 Signal 加到当前 `drive` 自己的 FIFO 队列。每个 Signal 的方法完成后，解释器处理队列中的下一项，队列清空时向调用方交付首个 Signal 的结果。`self` 指向发出 Signal 的当前对象，其他 target 从当前 `gv` 解析。`Visibility CPUSCOPE_CV` 和 `ProtectedBy io` 可声明在类型或方法上。

`framework/spec.py` 解析定义并注册对象类型。`framework/interpreter.py` 用循环和显式 Frame 栈推进 Actions。Frame 保存 Signal、指令位置、局部变量、契约分支和保护域。每次 `drive` 的队列还携带未完成的调用位置和已产生的首个结果。框架支持 Python Signal 目标，可恢复的调用链由显式 spec Frame 组成。

方法仍按 `acquire → Requires → Actions → Ensures → release` 执行。非私有方法的裸操作行执行前写 I，成功后保存 O；`drive` 形成 Signal 后写 I，在交付结果后保存 O。私有方法（名称以下划线开头）内的裸操作和 `drive` 行照常执行，但不设点。是否设点只看 Actions 行所属的方法；私有方法调用的非私有方法仍按常规设点。`emit` 维持独立的入队前后设点规则。I 按进入顺序、O 按完成顺序编号。失败操作不产生 O，已保存的内部输出保留。

`break(CpuFlowState.BUSYCHECK)` 单独成行，先写 I，再设置该推导所属 CpuFlow 实例的 `state`。按实例身份选择最内层活动 Frame，检查该 Frame 已选分支的 Ensures。检查通过后写可恢复的 O 点，暂停整条执行链。恢复继续执行 `break` 的下一条指令，待各方法正常完成时再执行其契约和 release。AP 的 `_wait` 用 `emit("self", "_wait")` 排队重试，等待期间保留外层 `wait` 的保护域。

生成的类型和回调函数注册在指定 Python 模块中，因此对象图可以沿用现有 Pickle 快照。统一导出模块为 `flows.model_specs`，新进程导入 `global_vars` 时会自动注册。模型版本保持为 9；删除未使用的 Python 模型及导出壳不改变类型路径，现有版本 9 的快照继续兼容，其他模型版本需重新生成。另一个进程加载这些对象前，必须先注册相同的 spec；spec 内容和类型定义需要兼容。

设点严格针对 spec 的 Actions 源操作行，裸操作一行是一个原子操作。解析器为这些行附加源位置；方法入口/出口、Requires/Ensures、保护域进入/退出、解释器内部指令、`assert` 和 `pass` 不触发设点。Actions 中显式执行的同步原语调用属于裸操作，适用同样的设点规则。Python Origin 发出的根 Signal 不来自 Actions，不单独设点；根方法内部的 Actions 按所属方法设点。

初次 `FlowRun` 保存默认 GV 的初始 O 点 `<flow>_0.0.snap`，后续 O 从 1 开始。每个派生分支以候选快照为初始输入，I 和实际生成的 O 分别从 0 编号。`inputs/records.json` 的 `points` 保存名称、`base`、输入快照的相对路径、源位置、操作种类和原文、输入描述。派生分支首个 I 的 `base` 指向基准 I，其余 I 的 `base` 为 `null`。`spec_fingerprints` 保存涉及文件在解析时的 SHA-256。O 元数据的 `input_point` 记录来源 I，恢复分支中跨 I 点挂起的 `drive` 完成后仍指向原来的 I。

drive 输入描述来源模型、目标、方法和实参；裸操作描述来源模型及当前局部绑定（排除 `self`、`env`）。编码中的 `gv` 路径相对于该 I 绑定的输入快照，支持属性、序列下标、字典键/值位置和绑定方法的所属对象；其他模型实例、容器和绑定方法使用 `objects` 表与 `local` 编号，保留别名和循环。枚举和方法记录可导入类型/函数名称。Env/CV 仅能作为 `environment` 引用，不展开状态；队列及不支持的值明确报错，GV 中含对象成员的集合也会拒绝。路径和 local 编号不代表跨 GV 快照的稳定身份。

需要设点的操作在执行前记录 I，O 保存成功后推进输出计数。主入口使用 `buffer_input_records()` 和 `deduplicate_outputs(pack_breaks=True)`。普通 O 正文按 SHA-256 跨 Flow 共用，逻辑名称和来源 I 各自保留。断点序列化后暂存在内存，同批次可直接恢复。退出时按 Flow 原子保存 `outputs/breaks.pack`，随后发布 `outputs/index.json` 和 `inputs/records.json`。异常退出也会导出已有记录，强制终止可能丢失尚未发布的数据。单独使用 `FlowRun` 默认逐点原子保存。

格式 2 的 `break` O 点在同一个对象图中保存 GV、Env、保护域、Frame、Signal 队列和 I/O 位置。格式 1 为普通 GV 快照。断点包索引为每个逻辑 O 点记录正文偏移和长度，续写保留已发布检查点的偏移。框架内部的 `resume_checkpoint` 仍从原分支最新断点续写。加载会检查正文摘要、模型版本、spec 指纹和该分支的 I/O 历史。普通 GV 加载器可以读取两种格式及独立文件。

`FlowRun`、`resume_checkpoint` 和 `fork_input` 的 `capture_input` 回调默认暂存首个 I 点，设 `capture_all_inputs=True` 后暂存每个 I 点。若该 I 已形成 Signal，暂存包括已求值的实参。`fork_input` 为每份候选 GV 独立恢复目标线路的 Env、Frame、Signal 和保护域，并按 GV 属性路径重新绑定引用。只通过容器位置找到的对象随执行状态冻结，以免加载另一份 GV 时把已求值的参数换成同一下标的其他对象。候选缺少需重新绑定的成员或类型不匹配时报告错误。基准 I 点状态在当前进程内复用。

`derive_flows` 接收一组 Origin，按阶展开有方向的组合。每条线路从 `start()` 或分叉 I 点出发；遇到 `break` 时续跑，每条线路在同一源代码位置最多续跑一次。派生分支会应用尚未参与该线路的 Flow 的一阶 O 快照，并保留自己新产生的 I 点供下一阶使用。运行错误直接向外传播，已产生的记录由批次上下文保存。

验证：`python3 -m pytest -q tests/test_spec.py`；完整检查：`make test`。测试仅保障 framework，`ProcTable.spec` 作为首个语言验证输入。
