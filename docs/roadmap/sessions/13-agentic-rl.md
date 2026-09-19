# 执行会话 13：Agentic RL

主参考：[Agent Lightning](https://github.com/microsoft/agent-lightning)。

## 启动条件

只有稳定任务分布、可验证奖励、足够轨迹和明确的监督/提示优化上限后才进入。

## 会话使命

理解并验证 Agent 轨迹、信用分配与训练系统的工程边界，而非为了 RL 标签增加复杂度。

## 必做任务

1. 阅读 Agent Lightning 的 agent/training 解耦、trajectory、reward、credit assignment 和 runner。
2. 从线上/离线 trace 定义状态、动作、结果与可审计奖励，排查 reward hacking。
3. 建立训练、验证、保留集和策略版本；训练环境不得接触生产 secret 或真实副作用。
4. 与 prompt、规则、检索和 SFT 等更简单方案做对照。
5. 评估分布漂移、安全、成本、回滚和持续训练运维。

## 验收门槛

- 奖励与真实业务结果有统计关联；
- 保留集显示稳定收益并通过安全回归；
- 推理与训练路径可独立升级/回滚；
- 若简单方案效果相当，明确否决 RL 上线。

结束时交接实验结论，不把研究原型直接接入生产。
