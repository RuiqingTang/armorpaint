# MCP 测试与回归记录

日期：2026-10-09。环境：macOS arm64，Debug 原生应用，项目现有 Python 虚拟环境。

## 本轮结果

- 原有基线：7 项通过，1 项真实应用测试跳过。
- 扩展后离线运行：116 项通过，47 项真实应用/生命周期测试跳过。
- 重新构建并在隔离实例运行：150 项核心测试全部通过（23.13 秒），13 项生命周期测试全部通过（含核心套件重跑，67.67 秒）。生命周期中的一项会重跑核心套件，两者不是 163 项彼此独立的测试。
- Xcode Debug 构建成功；仍有已有的 C 无原型函数声明警告，未在本轮进行无关清理。

## 测试组织

| 文件 | 范围 | 需要应用 |
| --- | --- | --- |
| `tests/test_bridge.py` | socket 权限、分片/Unicode、断线、响应校验、大小限制、并发读取 | 否，使用临时 socket |
| `tests/test_server.py` | 参数边界、生成脚本、文件路径、节点类型、设置、历史操作 | 否，使用模拟 RPC |
| `tests/test_mcp.py` | 真实 stdio 初始化、发现、参数拒绝、项目/绘制/导出工作流、并发读取 | 部分需要 |
| `tests/test_live.py` | 真实应用图层/遮罩/节点/对象、原生协议、回调和崩溃回归 | 是 |
| `tests/test_lifecycle.py` | 自动复制应用、独立配置/标识、超时/重启/文件损坏/上下文限额 | 是，自动管理实例 |

真实应用测试会新建项目并替换测试实例的当前内容，必须使用独立、可丢弃实例。不要连接正在编辑真实项目的 socket，也不要对同一实例并行跑多份测试。

## 已复现并修复

| 优先级 | 问题 | 修复与回归 |
| --- | --- | --- |
| 高 | 最底层图层带遮罩时，MCP 允许向下合并，删除遮罩后访问负索引，导致 `SIGSEGV` | MCP 在记录历史前调用应用原有合法性检查；原生 `layers_merge_down` 自身也检查，覆盖通用脚本入口 |
| 中 | 合并到填充层后没有转成绘制层，合并内容可能被后续填充更新覆盖 | 与 GUI 路径保持一致，合并结果转为绘制层 |
| 中 | 名称包含退格、换页等控制字符时被截断，操作仍报告成功 | 不再将 JSON 字符串转义直接当成 minic 的 C 字符串转义；真实应用逐字符往返验证 |
| 中 | 同一个 MCP 服务执行长操作时，读取请求被桥接锁阻塞 | 只对修改持锁；socket 和标准 stdio 均验证操作期间能读取 busy 状态和节点图 |
| 中 | 原生桥接接受字符串 `retain_context`、小数帧数、非法超时以及尾随第二个 JSON 对象 | 执行前拒绝类型/范围错误和额外对象；验证项目与保留上下文计数不变 |
| 高 | 空或截断 `.arm` 文件在原生导入前被解码，导致应用崩溃 | Python 和原生入口都检查最小头部和 magic byte；隔离实例验证应用保持运行 |
| 高 | 最近项目路径使用脚本临时内存，脚本结束后配置出现 MCP 响应残片，重启可能崩溃 | 最近项目始终复制字符串；配置字符串使用完整 JSON 转义和解码；覆盖引号、反斜杠、控制字符、中文和 emoji |

## 校验加固

以下通过定向回归测试验证，不代表全部高级 API 已逐项实测：

- 项目打开/保存、资源导入拒绝目录等非文件路径。
- 创建遮罩拒绝将另一个遮罩作为父级。
- 写入材质节点校验实际 socket 类型，按钮区分数值与文本；拒绝删除 PBR 输出节点时不写入历史。
- 设置拒绝超出 32 位浮点范围的有限数，避免原生转换为无穷大。
- 没有选中图层时，填充返回明确错误，而不是协程 `StopIteration`。
- 相机投影拒绝非法枚举；通用函数调用兼容空参数列表声明。
- 桥接响应要求布尔 `ok` 和字符串 `error`，不会把 `"false"` 误认为成功。
- socket 路径校验不再先解引用末级符号链接；请求 ID 不允许被普通参数覆盖。
- 非法超时、不可序列化/非有限 JSON 参数返回一致的 `BridgeError`。

同时验证了四步图层属性撤销/重做、节点创建与连接撤销、对象复制/中文命名/可见性/删除、多次异步任务完成、异步失败日志及部分修改保留，以及简单脚本不增加保留上下文。

## 复跑

仓库根目录执行离线测试：

```bash
mcp/.venv/bin/python -m pytest mcp/tests -q
```

构建并启动可丢弃实例：

```bash
xcodebuild -project paint/build/ArmorPaint.xcodeproj \
  -scheme ArmorPaint -configuration Debug \
  -derivedDataPath paint/build/mcp-derived CODE_SIGNING_ALLOWED=NO build
open -n paint/build/mcp-derived/Build/Products/Debug/ArmorPaint.app \
  --args --mcp-socket /tmp/armorpaint-mcp-test.sock
ARMORPAINT_MCP_TEST_SOCKET=/tmp/armorpaint-mcp-test.sock \
  mcp/.venv/bin/python -m pytest mcp/tests -q
```

涉及原生源码的修改必须重新构建并重启测试实例，仅重启 Python MCP 服务不够。

生命周期测试会复制 `.app` 到临时目录，跳过现有 `config.json`，使用独立应用标识和 socket，并自动关闭实例。可直接用它运行核心套件，无需手动打开应用：

```bash
ARMORPAINT_MCP_TEST_SOCKET=/tmp/armorpaint-test-opt-in.sock \
  mcp/.venv/bin/python -m pytest mcp/tests/test_lifecycle.py \
  -q -s -k core_suite
```

此命令中的 socket 环境变量只表示明确允许生命周期测试；实际 socket 由夹具创建，不会连接该路径。其他真实应用测试仍会连接用户指定的路径。

保留上限采用相同代码的低限额构建验证，避免默认 256 个上下文的内存开销：

```bash
xcodebuild -project paint/build/ArmorPaint.xcodeproj \
  -scheme ArmorPaint -configuration Debug \
  -derivedDataPath paint/build/mcp-limit-derived CODE_SIGNING_ALLOWED=NO \
  OTHER_CFLAGS=-DMCP_RETAINED_CONTEXTS=4 build
ARMORPAINT_MCP_TEST_SOCKET=/tmp/armorpaint-test-opt-in.sock \
ARMORPAINT_MCP_LIMIT_TEST_APP="$PWD/paint/build/mcp-limit-derived/Build/Products/Debug/ArmorPaint.app/Contents/MacOS/ArmorPaint" \
  mcp/.venv/bin/python -m pytest mcp/tests/test_lifecycle.py -q -s
```

生产构建仍保留 256 上限；到达上限后所有脚本都需重启，避免 `retain_context=false` 的失败脚本绕过计数限制。

## 尚未覆盖

- Linux 的真实应用运行、socket 权限与 GPU 行为。
- 光照烘焙的真实 GPU 结果和长耗时 GPU 调用取消；已测显式异步任务超时后查询/拒绝修改。
- 默认 256 个上下文的真实内存压力和长时间显存压力；边界分支使用上限 4 的独立构建验证。
- 完整头部之后的损坏 `.arm` 内容、模型和图片的损坏导入，以及插件和其他高级原生函数。当前文件头检查不是完整格式验证。
- 多实例启动竞争、读客户端长时间不接收大响应、应用退出时的连接竞争。

通用 `execute_code` / `call_function` 仍具有原生应用权限。测试通过不表示任意高级函数组合都可安全运行，也不表示失败操作会自动回滚。

本轮测试发现构建输出中的配置已因悬空路径受损，已保留副本到 `paint/build/mcp-demo/config-corrupt-2026-10-09.json`，移开损坏配置后应用使用默认值启动。没有修改系统安装的应用或全局 Codex 配置。

配置副本仅是本机调试产物，不随 Git 提交发布，也不是其他用户需要下载或恢复的文件。
