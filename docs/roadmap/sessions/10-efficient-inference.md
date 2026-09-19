# 执行会话 10：高效推理

主参考：[vLLM](https://github.com/vllm-project/vllm)。

## 会话使命

用真实工作负载建立模型服务的性能、容量和成本工程能力。

## 必做任务

1. 阅读 vLLM serving、scheduler、continuous batching、KV cache、parallelism、quantization 和 metrics。
2. 从本系统 trace 提取代表性 prompt/output 长度、并发、流式和工具调用负载。
3. 建立容量测试，测 TTFT、TPOT、吞吐、p95/p99、显存、排队和错误率。
4. 设计模型路由、限流、背压、超时、熔断、健康检查和灰度回滚。
5. 比较托管 API 与自部署的质量、成本和运维边界。

## 验收门槛

- 容量结论来自可复现负载，不使用宣传数字；
- 达到约定 SLO 时有并发与硬件上限；
- 过载时系统可控降级；
- 模型和推理参数升级有回归门禁。

结束时交接容量报告和部署决策。
