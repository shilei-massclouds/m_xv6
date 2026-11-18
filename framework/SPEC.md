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

`make run` 清理生成数据后分别从默认 GV、Env 独立运行 BP 和 AP；AP 首轮在 `BUSY_CHECK` 结束。InitProc 暂不接入 `main.py`；手动调用也需显式使用 `FlowRun`，该上下文本身复用传入 GV，不自动加载或重置。注册入口的路径相对于仓库根目录解析，不依赖工作目录。GV 模型均通过 `flows.model_specs.new_model(...)` 创建；需要类型或枚举时，使用 `registry.model("CpuFlow")`、`registry.namespace["CpuFlowState"]` 等注册项。

BP 和 AP 继承同一份 `CpuFlow.spec`，调度器通过 `next_runnable = drive("proc_table", "pick_runnable")` 接收 spec 返回值，不再使用 Holder 输出参数。InitProc 的首次执行检查也直接接收 `FirstProc.spec` 的布尔返回值；首次进入时调用文件系统初始化和进程 `_kexec`，最后切换到用户态。

spec 文件名是对象类型名。`Prop table: list[Proc]` 为每个实例创建独立列表；也可以使用 `Prop value: int = 0` 显式指定默认表达式。当前属性注解描述模型，不进行运行时类型检查。枚举通过 `Enum Name:` 声明；`Extends Name` 引用同一 registry 中的另一个 spec。

`Def method(parameters) -> variable:` 定义方法；箭头指定正常完成 Actions 后输出的局部变量，所选分支必须给该变量赋值。spec 禁止 `return`，没有输出声明的方法正常完成时输出 `None`。命名参数支持字面量默认值；未绑定的关键字参数可以通过 `kwargs` 访问。

方法可依次包含多组 `Requires / Actions / Ensures`：

- 每个 Requires 块的表达式做 OR，必须且只能有一组成立；没有 Requires 时，该组隐含为 True。
- 执行所选组的 Actions，只检查该组的 Ensures；Ensures 块内表达式做 OR。条件结果必须是 bool。
- Requires 无匹配或 Ensures 不成立时，使用现有 `ContractViolation`，报告 spec 文件、行号、方法和入口分支。

Actions 支持赋值、属性/下标赋值、表达式语句、`assert`、`pass`、`with` 保护域，以及 `+=`、`-=`、`*=`。每个操作必须完整写在一个物理行中，不能用括号或反斜杠跨行，也不能在同一行通过分号串联多个操作。保护域声明同样占一个物理行，内部操作各自另起一行。表达式采用 Python 表达式求值，spec 与原 Python 模型一样是可信程序输入。

spec 禁止 `if/else` 语句和条件表达式，分支选择通过互斥的 Requires/Actions/Ensures 组表达。如果判断依赖前一个操作或 Signal 的结果，先完成公共前缀，再将结果作为参数交给带分支的方法；不能把这个判断提前到公共前缀执行之前。`CpuFlow.select_next`、`InitProcFlow.first_init` 和 `ProcTable.set_initproc` 是现有示例。

Signal 调用写成 `drive("target", "action", key=value)`，可直接调用或作为赋值右侧；嵌套在其他表达式中的 drive 会在解析时拒绝。`self` 指向发出 Signal 的当前对象；其他 target 从当前 `gv` 解析。spec 不提供 emit。`Visibility CPUSCOPE_CV` 和 `ProtectedBy io` 可声明在类型或方法上，沿用现有竞争检查。

`framework/spec.py` 解析定义并注册对象类型；`framework/interpreter.py` 用循环和显式 Frame 栈推进 Actions。每个 Frame 保存当前 Signal、指令位置、局部变量、契约分支、保护域和返回值接收位置。spec 到 spec 的调用不递归进入 Python 模型方法。框架仍支持 Python Signal 目标；当前 GV 的模型对象全部来自 spec。

方法仍按 `acquire → Requires → Actions → Ensures → release` 执行，方法边界不保存快照。裸操作行执行前写 I，成功后保存 O；drive 行形成 Signal 后写 I，被调用方法正常完成上述流程并赋值返回结果后保存 O。内部操作按相同规则展开，所以 I 按进入顺序、O 按完成顺序分别编号。异常依次清理 Frame 和保护域，失败操作没有 O，成功的内部输出保留。

生成的类型和回调函数注册在指定 Python 模块中，因此对象图可以沿用现有 Pickle 快照。统一导出模块为 `flows.model_specs`，新进程导入 `global_vars` 时会自动注册。模型版本保持为 9；删除未使用的 Python 模型及导出壳不改变类型路径，现有版本 9 的快照继续兼容，其他模型版本需重新生成。另一个进程加载这些对象前，必须先注册相同的 spec；spec 内容和类型定义需要兼容。

设点严格针对 spec 的 Actions 源操作行，裸操作一行是一个原子操作。解析器为这些行附加源位置；方法入口/出口、Requires/Ensures、保护域进入/退出、解释器内部指令、`assert` 和 `pass` 不触发设点。Actions 中显式执行的同步原语调用属于裸操作，仍然设点。Python Origin 发出的根 Signal 不来自 Actions，不单独设点；根方法内部的 Actions 正常设点。

每个 `FlowRun` 先保存初始 O 点 `<flow>_0.0.snap`，I 从 `<flow>_0.0` 开始，后续 O 从 1 开始。`inputs/records.json` 的 `points` 保存名称、`base=null`、最近输出的相对路径、spec 文件/方法/契约分支/行号、操作种类和原文、输入描述。`spec_fingerprints` 保存涉及文件在解析时的 SHA-256。O 的可选文件头字段 `input_point` 记录来源 I；初始 O 不含该字段。

drive 输入描述来源模型、目标、方法和实参；裸操作描述来源模型及当前局部绑定（排除 `self`、`env`）。编码中的 `gv` 路径相对于该 I 绑定的输入快照，支持属性、序列下标、字典键/值位置和绑定方法的所属对象；其他模型实例、容器和绑定方法使用 `objects` 表与 `local` 编号，保留别名和循环。枚举和方法记录可导入类型/函数名称。Env/CV 仅能作为 `environment` 引用，不展开状态；队列及不支持的值明确报错，GV 中含对象成员的集合也会拒绝。路径和 local 编号不代表跨 GV 快照的稳定身份。

I 记录原子写入后才执行操作，O 原子保存后才推进输出计数。持久化失败停止本次推导，不覆盖已完成文件。已有同 Flow 数据时拒绝再次首轮运行，需先清理；输入记录不含完整 Env、Frame 或可恢复执行栈。

当前只完成首轮记录，尚不支持输入描述的恢复解码、并发 GV 加载、重放或 base 分支调度。Frame 栈是后续机制的基础，不代表已实现断点恢复。

验证：`python3 -m pytest -q tests/test_spec.py`；完整检查：`make test`。测试仅保障 framework，`ProcTable.spec` 作为首个语言验证输入。
