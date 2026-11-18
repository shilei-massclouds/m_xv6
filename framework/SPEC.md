# Spec 解释执行

第一份输入是移入 `kernel/ProcTable.spec` 的 `_pick_runnable` 定义。BP、AP 和 InitProc 均已迁移为 spec 解释执行，入口为 `flows/BpInitFlow.spec`、`flows/ApInitFlow.spec` 和 `flows/InitProcFlow.spec`。`flows/model_specs.py` 统一注册 26 个对象定义，包括它们实际依赖的内核、驱动、文件系统和结构对象。

```python
from framework.engine import System, TaskLocalEnv
from flows.model_specs import registry
from global_vars import gv

gv.proc_table = registry.model("ProcTable")()
result = System().drive(TaskLocalEnv(name="example"), "proc_table", "_pick_runnable")
assert result is None
```

`make run` 先清理快照，再依次解释执行 BP 和 AP；InitProc 暂不接入 `main.py`。InitProc Origin 可在已有 BP 或 AP 模型上显式调用，仍复用当前 `gv`，不自动加载快照。注册入口的路径相对于仓库根目录解析，不依赖工作目录。GV 中的所有模型对象均通过 `flows.model_specs.new_model(...)` 创建。旧 Python 模型和 Flow 导出壳已删除；需要类型时使用 `registry.model("CpuFlow")`，需要枚举时使用 `registry.namespace["CpuFlowState"]` 等注册项。

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

调用顺序仍是 `acquire → Requires → Actions → Ensures → release → 自动保存`。Actions 中的保护域正常退出后检查 Ensures，再输出声明的局部变量。异常会依次清理嵌套 Frame 的保护域并执行 release；当前私有方法在 release 成功后保存快照，规则与现有引擎一致。

生成的类型和回调函数注册在指定 Python 模块中，因此对象图可以沿用现有 Pickle 快照。统一导出模块为 `flows.model_specs`，新进程导入 `global_vars` 时会自动注册。模型版本保持为 9；删除未使用的 Python 模型及导出壳不改变类型路径，现有版本 9 的快照继续兼容，其他模型版本需重新生成。另一个进程加载这些对象前，必须先注册相同的 spec；spec 内容和类型定义需要兼容。

后续 I/O 设点严格针对 spec 的 Actions 内部操作行，裸操作行和 drive 行都适用；裸操作一行是一个原子操作。方法入口/出口、Requires/Ensures、保护域进入/退出和解释器内部指令不单独触发设点，`assert` 仅检查、不设点。目前仍未接入这套逐操作的 I/O 点、记录文件、并发 GV 输入和 base 分支调度；当前快照保存继续沿用方法退出规则。解释器的 Frame 状态是后续实现这些机制的基础，不代表已经实现断点恢复。

验证：`python3 -m pytest -q tests/test_spec.py`；完整检查：`make test`。测试仅保障 framework，`ProcTable.spec` 作为首个语言验证输入。
