# 阶段 1 真实质量与成本门禁独立验收

验收日期：2026-09-24

## 结论

**整体门禁：FAIL。**

- 工程与结构验收：PASS。
- 报告可比性与执行完整性：PASS。
- 离线合同门禁：PASS（8/9）；它不代表真实 RAG 质量通过。
- 真实质量非退化门禁：FAIL。
- 成本计量完整性：PASS。
- 金额成本门禁：不可正式验收，实际账户费率未配置。

因此不能把阶段 1 记为“真实质量与成本门禁通过”。结构化 IR 可以作为已完成能力保留；剩余召回排序、拒答和回答行为问题应作为后续检索阶段的显式输入，不能通过降低当前 gate 隐藏。

## 证据与可比性

控制组：`evaluation/results/stage1-control-v4.20260923.service.core.json`

候选组：`evaluation/results/stage1-ir-shadow-v4.20260923.service.core.json`

两组以下字段完全一致：evaluation schema 2.1、evaluator 2.1、dataset SHA、source contract SHA、query pipeline SHA、runtime configuration SHA、pricing configuration SHA、Prompt SHA、模型 `qwen-flash`、attempts=1、usage accounting 2.1 和 `all_turns_all_attempts` 口径。两组均有 `baseline_eligibility.rag_quality=true`。

两组各执行 24 次模型调用，失败调用、provider error、未完成 turn 均为 0，API 返回 token 完整，不是字符估算。当前全量测试 159/159 通过；2026-09-24 独立复跑离线合同为 8/9、gate PASS。

## 真实质量结果

- 通过数：4/9 → 4/9，非退化。
- Answer correctness：0.555556 → 0.555556，非退化。
- Behavior accuracy：0.666667 → 0.666667，非退化。
- Recall@5：1.000000 → 1.000000，非退化。
- Faithfulness：0.574074 → 0.629630，提高 0.055556。
- Image accuracy：0.888889 → 1.000000，提高 0.111111。
- Precision@5：0.233333 → 0.200000，下降 0.033333，FAIL。
- MRR：0.916667 → 0.888889，下降 0.027778，FAIL。
- nDCG@5：0.938488 → 0.916667，下降 0.021821，FAIL。
- Citation correctness：0.685185 → 0.666667，下降 0.018518，FAIL。

独立调用 `compare_with_baseline()` 重算得到与报告完全相同的四项质量失败，没有发现报告生成与门禁重算不一致。

## 延迟和用量

- p50：2689.104 ms → 2459.484 ms，改善 229.620 ms。
- p95：11296.615 ms → 11567.269 ms，增加 270.654 ms，约 2.40%，低于 gate 允许的 25%，PASS。
- 模型调用：24 → 24。
- 输入 token：17808 → 18393。
- 输出 token：1926 → 2619。
- 总 token：19734 → 21012，增加 1278，约 6.48%。

## 成本验收

正式报告的 `cost_status=unavailable`，原因是 `LLM_INPUT_USD_PER_1M` 和 `LLM_OUTPUT_USD_PER_1M` 没有配置为可信正数。因此 `$0` 只是“无法计算”，不是免费，正式金额门禁仍不可用。

作为参考估算，阿里云官方 `qwen-flash` 华北 2（北京）、单次输入不超过 128K 的公开原价为输入 0.15 元/百万 token、输出 1.5 元/百万 token。两组总输入 token 都少于 128K，因此所有单次调用必然处于该档位。按未命中缓存的原价估算：

- 控制组：约 0.00556020 元；
- 候选组：约 0.00668745 元；
- 增量：约 0.00112725 元，增加约 20.27%。

价格依据：[阿里云 qwen-flash 模型信息](https://help.aliyun.com/zh/model-studio/qwen-flash)。这只是公开原价场景，不包含实际账户地域、优惠、免费额度、缓存命中和账单舍入，不能替代正式费率配置与账单核对。

## 验收决定

1. 保留阶段 1 的 IR、幂等导入、引用定位、图片 MIME 和结构差异成果。
2. 不切换默认索引，不删除控制组和 v3/v4 证据。
3. 不放宽 `evaluation/gate.json` 的零质量退化要求。
4. 将 Precision/MRR/nDCG/引用、拒答阈值、动态 Web 输入和生成行为列为工业 RAG 阶段的首批问题。
5. 在正式成本验收前配置与实际账户一致的正数费率，并用同一费率指纹重新生成控制组和候选组报告。
