# 全球数字贸易合作运营平台

本项目是一套可离线运行的 Python 服务端平台，用于管理跨境数字合作中的资源流转、项目证据评估和统计资料质量。平台把运营节点、合作通道、资源申请、项目版本、分析决定、样本观测、权限和审计事件持久化到 SQLite，供秘书处、项目办公室、数据团队和审计人员协作使用。

## 目录

- src/trade_flow/：运营节点、合作通道、资源批次、额度申请、分配和情景分析；
- src/cooperation_assurance/：合作项目、证据版本、评估协议、观测导入、分析任务与准入决定；
- src/metric_quality/：统计资料质量。区分报送主体、样本身份和观测序列，
  按冻结规则先归并每个样本的结论，再计算通过/拒绝/证据不足比例，
  规则与分析版本只增不改，旧算法版本（metric-quality-observation-yield/1）原样保留；
- fixtures/：离线验收使用的评估协议与结构化观测；
- tests/：领域规则、错误边界、事务、权限、HTTP API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m trade_flow.acceptance --workspace .
    PYTHONPATH=src python3 -m cooperation_assurance.acceptance --workspace .
    PYTHONPATH=src python3 -m metric_quality.acceptance

三条命令会在临时 SQLite 数据库中完成合作资源流转、项目证据评估和统计资料质量流程，不访问外部网络。

## HTTP 服务

    PYTHONPATH=src python3 -m trade_flow.api --database trade-flow.sqlite3 --host 127.0.0.1 --port 8080
    PYTHONPATH=src python3 -m cooperation_assurance.api --database cooperation-assurance.sqlite3 --host 127.0.0.1 --port 8081
    PYTHONPATH=src python3 -m metric_quality.api --database metric-quality.sqlite3 --host 127.0.0.1 --port 8082

服务提供 JSON 接口与健康检查。进程重启后可以继续读取 SQLite 中的业务状态和审计历史。

## 统计资料质量模型（metric_quality）

三个身份层次：**报送主体**（经济体/机构）拥有 **样本**，每个样本针对同一指标在多个
报告期形成一条 **观测序列**。分析分两步：

1. 冻结归并：按报告期对记录做确定性归并——
   - 同一来源的复报（`resubmission`）取代更早的初报；
   - 后到的缺期（`missing`）或撤销（`withdrawal`）标记使该期无有效值，撤销之后再复报可恢复；
   - 不同来源的最新值差值超过 `conflict_tolerance` 时该期判为冲突，按 `source_priority`
     在容差内选定权威来源；
   - 声明期间没有任何记录即视为缺期。
2. 样本结论（`pass`/`reject`/`insufficient`，缺期、撤销、未决冲突均为证据不足）确定后，
   以样本数为分母计算 `pass`/`reject`/`insufficient` 比例，三者之和恒为 1，通过率不可能超过 100%。

版本与审计：

- 冻结规则（`policies`）只增不改，新版本必须用 `supersedes_version` 显式接续；
- 分析以“规则版本 + 输入摘要（SHA-256）”标识，是不可变版本，相同输入返回同一版本；
- 决定一经发布不可静默修改，只能撤销留痕，并在后继分析版本上重新决定；
- 旧算法 `metric-quality-observation-yield/1` 的结果可经 `/legacy-analyses` 连同输入摘要原样保留；
- 分析视图可从总体通过率下钻到 `result.samples[].conclusion`，再到各期 `record_ids`
  及随附的原始记录；审计事件可按实体过滤（`/audit?entity_type=...&entity_id=...`）。
- 无效输入统一返回 `{"error":{"code":...,"message":...}}`（422/409/404/403）。
