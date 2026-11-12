# 分能区间核算

根据累计表读数重建区间电量，处理换表、生产分摊、费率、排放因子、需量及封账更正。内部电量单位为微 kWh。

Python 3.11 标准库与 SQLite 即可运行。数据、时钟和文件均在本地，不需要服务凭据。示例只使用合成记录。

## 运行

在仓库根目录执行：

```sh
python -m energyflow demo
python -m examples.context
python -m unittest discover -s tests -v
python -m compileall -q energyflow examples
```

示例使用内存或临时目录，不会改动持久台账。`examples/context.json` 记录一组可重复导入的业务输入，字段与时间口径见 `examples/数据说明.md`。`examples.context` 检查数据并调用现有产品入口；它只展示原有业务数据，不计算分析结论。

`tests` 覆盖现有业务生命周期、非法输入、版本和完整性边界，另有示例导入回归。完整测试和上述演示可在一次短终端会话中执行。

## 同时性与移峰计划

`examples.context` 只重建并核对示例数据；同时性分析和移峰计划通过服务入口按需运行，示例数据见 `examples/数据说明.md`。

- `planning.coincidence(parent, topology, start, end, width)`：在同一需量网格上逐格给出总表、各子表功率与未分配残差。残差保留总表/子表证据和缺失标记，子表窗口不完整时状态为 `incomplete` 而不是零；另给出厂区峰值时各子表贡献、各自独立峰值和峰值格。
- `planning_task.put(...)`：登记可移动任务的总电量校验、固定功率、持续格数、最早开始、最晚结束、可用格及前后置依赖。任务必须连续运行，基线格必须在声明窗口和可用格内。
- `planning.plan(...)`：先从任务所在子表曲线（及各级上级曲线）扣除基线任务，再排到新位置。拒绝对扣成负数或同一任务重复覆盖做模拟。基荷不动，任务运行不可抢占。

计划搜索在声明上限 `max_search` 内做离散枚举，按（最大需量、分时电费、相对原计划移动格数、开始格向量）的字典序择优，同分取稳定顺序。`status` 明确区分 `optimal`、`infeasible` 与 `over_scale`：只有枚举完整结束才报告 `optimal`，超规模时不给贪心结果。输出包含逐格前后功率曲线、关键需量窗口、任务安排、费用差，以及能量守恒、容量和依赖逐格核对。计划只做模拟：读数、分摊和封账记录不变；同一组固定输入可重复得到相同 `result_digest`，读数修订后 `input_digest` 变化并重新计算。

## 容器

```sh
docker build -t local/energyflow .
docker run --rm --network none local/energyflow
docker run --rm --network none local/energyflow python -m examples.context
```

镜像构建完成后运行和测试不下载文件。持久化使用各自命令的本地路径参数；具体操作及参数可通过 `python -m energyflow --help` 查询。第三方依赖清单为空。
