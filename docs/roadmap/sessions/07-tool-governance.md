# 执行会话 07：工具治理

主参考：[ToolHive](https://github.com/stacklok/toolhive)。

## 会话使命

把工具调用从代码内函数集合升级为有注册、隔离、策略、审批和审计的受控能力。

## 必做任务

1. 阅读 ToolHive 的 MCP server lifecycle、registry、proxy、auth、secrets、policy、telemetry 和隔离设计。
2. 建立工具注册表：schema、版本、owner、权限、风险等级、超时、配额和数据分类。
3. 统一鉴权、secret 注入、网络/文件边界、输入输出校验、日志脱敏和审计。
4. 对高风险或不可逆工具加入策略判断和人工审批；支持 dry-run 与幂等键。
5. 建立恶意参数、prompt injection、越权、超时、重试风暴和工具失效测试。

## 验收门槛

- Agent 不直接持有长期 secret；
- 未授权工具和参数在执行前被阻止；
- 每次工具调用可关联用户、任务、策略决定和结果；
- 工具故障不会使主流程无限重试或静默成功。

结束时交接 threat model、策略配置和审计样例。
