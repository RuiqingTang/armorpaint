# ArmorPaint MCP

本项目新增了原生应用桥接层和一个标准 stdio MCP 服务，让 AI 直接操作**正在运行的 ArmorPaint**。保留原来的界面、渲染器和项目格式。

支持 macOS 和 Linux 的本地运行。此版本尚未实现 Windows、移动端、浏览器版的桥接传输；Linux 未在本机实测。

```text
Codex / MCP 客户端 → MCP stdio 服务 → 本地 Unix socket → ArmorPaint 主线程 → 应用操作
```

## 在 macOS 上使用

需要 Python 3.10+、[uv](https://docs.astral.sh/uv/) 和 Xcode。先克隆本 fork，生成并构建应用：

```bash
git clone https://github.com/RuiqingTang/armorpaint.git
cd armorpaint
cd paint
../base/make
cd ..
xcodebuild -project paint/build/ArmorPaint.xcodeproj \
  -scheme ArmorPaint -configuration Debug \
  -derivedDataPath paint/build/mcp-derived CODE_SIGNING_ALLOWED=NO build
open -n paint/build/mcp-derived/Build/Products/Debug/ArmorPaint.app --args --mcp
```

也可以在 Xcode 重新运行，然后选择 **Help → Start MCP Server**。停止连接使用 **Help → Stop MCP Server**。当前操作还未结束时，应用会先要求等待其完成。

安装 MCP 服务和 Codex 配置：

```bash
uv sync --project mcp
python3 mcp/install_codex.py
```

安装脚本注册 `armorpaint`，设置工具调用超时为 330 秒，并备份原来的 Codex 配置。它不会覆盖同名的其他服务器，也不会改变工具批准策略。

确认应用端连接：

```bash
mcp/.venv/bin/armorpaint-mcp --check
codex mcp get armorpaint --json
```

让 Codex 重新加载 MCP 配置（必要时重新启动 Codex），然后可以直接说：

> 使用 armorpaint MCP 查看当前项目。创建蓝色金属材质和填充层，保存到我指定的 .arm 文件，导出 PBR 贴图，再给我看截图。

MCP 服务可在应用未启动时初始化；只有调用操作工具时才需要应用在线。服务进程由 Codex 启动，无需手动留一个 Python 终端运行。

## 操作覆盖

提供 26 个 MCP 工具和 4 个资源：

- 状态、API 查询、节点类型查询、当前材质/笔刷节点图、全部标量设置、截图。
- 新建/打开/保存项目；导入资源；导出模型、材质和 PNG/JPG/EXR PBR 贴图。
- 图层、组、遮罩、填充、路径、曲线、文本、贴花；图层属性和撤销重做。
- PBR 材质、节点创建/连接/属性修改；对象增删、选择、命名、变换与材质分配。
- 笔刷设置、逐帧绘制、填充、相机操作、异步光照贴图烘焙。
- 项目播放与停止，停止后恢复编辑器捕获的项目。
- **`execute_code` 和 `call_function`**：通用入口，访问运行中应用实际注册的脚本/原生 API，包括时间轴、物理、网格处理、烘焙节点、插件和其他高级操作。

本次构建实际注册 **1641 个函数、111 个结构体**。其中新增 919 个公开应用函数绑定/适配器，以及 241 个 context/config 标量设置。数量随源码变化；`get_application_state` 返回真实运行时数量，`get_api_reference` 返回真实签名。

`generate_api.py` 从 `paint/sources/functions.h`、`types.h` 和枚举生成绑定。它处理值类型向量/矩阵、原生回调和较长参数列表，并为平台专属功能添加编译条件。两个上游声明没有实现，未暴露：`ui_base_update_ui` 和 `util_mesh_equirect_unwrap`。细节见 `api_coverage.json`。

**公开 API 接入范围不等于全部功能逐项验证。** 常见流程已实测；高级内部函数仍有自己的状态、GPU 和调用时机要求。通用入口不会自动补全所有这些前置条件，也不会把每个界面按钮转换成独立工具。

## AI 应怎样调用

先调用 `get_application_state` 获取当前 ID，再查询函数/节点定义。使用明确工具执行常见任务；高级操作先查 API，再调用 `execute_code`。操作后读取状态或截图验证结果。

例如通用 C 脚本：

```c
void main() {
    slot_layer_t *layer = mcp_get_layer(2);
    if (layer == NULL) {
        mcp_task_end("Layer no longer exists");
        return;
    }
    context_set_layer(layer);
    console_log(string("Selected: %s", layer->name));
}
```

脚本语言是应用已有的 **minic 解释执行 C**，不是 Python、完整 C 编译器或沙盒。不要写 `#include`。运行时参考包括可用结构体、枚举、函数，资源 `armorpaint://reference` 还包含当前项目数据和材质节点说明。

通常使用 `void main()`；`int/float/double main()` 可返回标量结果。`call_function` 自动返回标量，并将字符串/字符串数组返回值写入日志。原生对象指针通过状态、节点图或后续脚本检查，不作为内存地址暴露给客户端。

`call_function` 的普通字符串成为 C 字符串；指针、枚举等用表达式传入：

```json
{
  "name": "context_set_layer",
  "arguments": [{"expression": "mcp_get_layer(2)"}]
}
```

可查询的资源：`armorpaint://state`、`armorpaint://api`、`armorpaint://nodes`、`armorpaint://reference`。

### 异步操作

仅等待几帧不能证明烘焙、计时器或下载已经完成。通用脚本应显式跟踪原生完成回调：

```c
void done() {
    mcp_task_end(NULL);
}

void main() {
    object_t *object = script_get_object("Tessellated");
    if (object == NULL) {
        mcp_task_end("Object not found");
        return;
    }
    mcp_task_begin();
    script_bake_lightmap(object, 1024, 64, 1.0, "/absolute/output.png", done);
}
```

每个 `mcp_task_begin` 都需要匹配一次 `mcp_task_end`；失败时传错误字符串。不要在同一个原生回调入口上同时安排多次未完成的操作：生成的回调适配器保存一个当前脚本回调。MCP 同时只执行一个修改操作，其他客户端修改请求会收到 busy。

通用脚本默认保留解释器上下文，保证异步回调和传给应用的脚本内存仍然有效。每次会话最多保留 256 个上下文，到达限制后所有脚本调用都需重启应用，包括 `retain_context=false`。只有确定没有回调/指针逃逸的脚本才可设置 `retain_context=false`；常用工具为自己生成的简单脚本自动释放上下文。

### 错误与边界

- 编译/运行错误返回真实控制台日志，不自动重试，也不假装回滚。失败前的修改或文件写入可能已经发生。
- 原生 GUI 操作只有使用历史系统才可撤销；通用代码不会自动成为一项可撤销事务。
- VM 每次调用限制 500 万条指令，避免脚本死循环阻塞应用。原生 C/GPU 调用仍然需要自己返回；指令上限无法中断它们。
- 超时的异步任务不能保证已经取消。此时服务继续允许查询，拒绝后续修改；需要重启应用后再修改。
- 活跃的内部 agent、脚本或 Player 会阻止新的修改请求，避免共用执行状态被覆盖。
- MCP 请求上限为 1 MiB。连接只使用当前用户拥有、权限为 0600 的本地 socket，不开放远程端口。
- 绘制需要 paint layer 或 mask。对 fill layer 的画笔调用会被应用忽略，所以 MCP 明确返回前置条件错误。

## 构建、更新与测试

```bash
python3 mcp/generate_api.py
cd paint
../base/make
open build/ArmorPaint.xcodeproj
```

macOS 命令行构建（在仓库根目录）：

```bash
xcodebuild -project paint/build/ArmorPaint.xcodeproj \
  -scheme ArmorPaint -configuration Debug \
  -derivedDataPath paint/build/mcp-derived CODE_SIGNING_ALLOWED=NO build
```

Python 与 stdio MCP 测试：

```bash
uv sync --project mcp --group dev
mcp/.venv/bin/python -m pytest mcp/tests -q
```

真实应用集成测试必须使用可丢弃的新实例。它会**替换测试实例的项目**，不要连接正在做实际工作的实例：

```bash
open -n paint/build/mcp-derived/Build/Products/Debug/ArmorPaint.app \
  --args --mcp-socket /tmp/armorpaint-mcp-test.sock
ARMORPAINT_MCP_TEST_SOCKET=/tmp/armorpaint-mcp-test.sock \
  mcp/.venv/bin/python -m pytest mcp/tests -q
```

测试分为桥接协议测试、工具参数/脚本生成测试、标准 stdio MCP 测试和真实应用回归测试。真实应用测试只在设置 `ARMORPAINT_MCP_TEST_SOCKET` 后运行；不设置时会明确跳过，不能把离线通过当作应用端验证。

2026-10-09 验证结果：离线测试 **116 项通过、47 项跳过**；独立应用中的核心套件 **150 项通过**，生命周期套件 **13 项通过**（其中一项重跑核心套件，不能将两个套件简单相加当作独立测试数）。已修复遮罩合并崩溃、项目路径生命周期、配置字符串转义和并发查询阻塞等问题。

macOS 上也可由测试自动复制应用、隔离配置并管理实例，无需手动启动：

```bash
ARMORPAINT_MCP_TEST_SOCKET=/tmp/armorpaint-test-opt-in.sock \
  mcp/.venv/bin/python -m pytest mcp/tests/test_lifecycle.py \
  -q -s -k core_suite
```

此命令的 socket 环境变量仅表示明确允许生命周期测试；该套件会创建自己的临时 socket。其他真实应用测试仍连接指定 socket。原生修改必须重新构建应用；上下文限额的独立构建命令见测试报告。

测试覆盖 stdio 初始化/工具发现、断线与错误日志、权限检查、中文与控制字符、项目往返、空/截断 `.arm` 文件、配置重启、材质颜色与节点连接、图层属性撤销重做、遮罩/合并边界、真实绘制、对象操作、四种贴图格式、截图、值类型适配、原生异步回调、修改期间并发读取、busy 拒绝和 VM 死循环中止。不要对同一个测试实例并行运行多份测试。

已复现问题、修复和未覆盖范围见 [测试报告](TESTING.md)。

### 更新与配置恢复

拉取本次修复后需要重新构建原生应用、退出旧实例并启动新实例；只重启 Python MCP 服务不会更新原生桥接和项目导入逻辑。

旧版本可能把已经释放的项目路径写入最近项目列表，或没有正确转义带引号的文件名，造成 `config.json` 损坏并在启动时崩溃。若遇到此情况，先备份受影响应用自己的配置文件，再移开该文件，让应用使用默认配置启动。macOS 本地 Debug 构建的配置通常位于：

```text
paint/build/mcp-derived/Build/Products/Debug/ArmorPaint.app/Contents/Resources/out/data/config.json
```

不要删除 `.arm` 项目文件，也不要把测试报告中本机的受损配置副本当作可用配置恢复。文件头检查只拒绝空文件、过短头部和错误 magic byte，不保证其他损坏内容可安全导入。

## 其他客户端与多实例

标准 MCP 客户端可使用：

```json
{
  "mcpServers": {
    "armorpaint": {
      "command": "/absolute/repository/mcp/.venv/bin/armorpaint-mcp"
    }
  }
}
```

默认 macOS socket：`~/Library/Application Support/ArmorPaint/mcp.sock`。Linux 使用应用的 XDG 数据目录，兼容已有 `~/.ArmorPaint` 目录。

多实例使用应用参数 `--mcp-socket /absolute/short/path.sock`，MCP 服务对应参数 `--socket /absolute/short/path.sock`。两边也可以使用 `ARMORPAINT_MCP_SOCKET` 环境变量。macOS socket 路径必须短于 104 字节。

官方参考：[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)、[Codex MCP 配置](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。
