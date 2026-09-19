# 执行会话 11：Code Agent

主参考：[OpenHands](https://github.com/All-Hands-AI/OpenHands)。

## 会话使命

学习可操作代码库的 Agent 如何管理沙箱、事件、工具、权限、补丁和验证。

## 必做任务

1. 阅读 OpenHands runtime/sandbox、event stream、agent/controller、tools、workspace 和 evaluation。
2. 定义代码任务、仓库快照、允许命令、网络策略、secret 边界和资源限制。
3. 将计划、文件修改、命令执行、测试结果和用户介入记录为可重放事件。
4. 输出 reviewable diff，并强制测试、静态检查和风险摘要。
5. 用真实小任务集评测成功率、误改率、测试通过率、时间与成本。

## 验收门槛

- 不可信仓库和命令在隔离环境执行；
- Agent 无法越过工作区和权限边界；
- 结果以补丁和验证证据交付；
- 失败任务可回放并归因。

结束时交接 sandbox threat model 与任务基线。
