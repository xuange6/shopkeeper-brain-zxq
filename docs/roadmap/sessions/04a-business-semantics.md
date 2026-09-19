# 执行会话 04A：业务语义层

主参考：[Cube](https://github.com/cube-js/cube)。

## 会话使命

建立业务术语、实体、指标、维度、口径、同义词与权限的权威定义，使同一问题在不同表达下得到一致解释。

## 必做任务

1. 阅读 Cube schema/compiler/query rewrite、measures、dimensions、joins、pre-aggregation 和 security context 的边界。
2. 盘点 shopkeeper 领域的核心实体、指标、维度、关系、口径、同义词和歧义；选一个高价值业务域做纵向切片。
3. 定义版本化 semantic model；将自然语言解析为受约束的 `SemanticQueryPlan`，并能解释解析结果。
4. 将当前 regex 分类器演进为“词典/规则 + schema-grounded 模型解析 + 校验”的组合，禁止模型自由发明指标和字段。
5. 为语义解析准确率、歧义澄清、权限过滤和版本兼容建立测试。

## 验收门槛

- 同义表达映射到同一业务概念和口径；
- 未定义或歧义概念触发澄清/拒绝，并返回可解释原因；
- 语义计划进入主查询状态并影响检索/工具路由；
- 语义定义有 owner、版本、变更测试和回滚。

结束时交接首个业务域的 semantic model；不要把事实记忆混入本层。
