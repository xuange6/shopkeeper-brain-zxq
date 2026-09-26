# 当前阶段：阶段 3 已完成本地验收，等待目标生产环境验收

> 最后更新：2026-09-27

## 先说结论

阶段 3「企业知识生命周期」的核心开发已经完成，代码、真实基础设施适配、本地生产形态和故障恢复演练均已通过。项目现在不是“还在开发阶段 3”，而是已经走到 **目标生产环境上线验收** 这一步。

这句话也有明确边界：我们已经证明这套方案能运行、能恢复、能以分布式方式部署，但还没有在企业最终使用的多可用区 Kubernetes 和正式数据上完成签字验收。因此当前状态是：

- **阶段 3 实现：PASS**
- **本地生产形态验收：PASS**
- **目标生产环境验收：待执行**
- **正式生产上线：尚未批准**

阶段 4A 可以并行开始，但不能把它理解成阶段 3 已经在生产环境上线。

## 这次真正完成了什么

阶段 3 不再只是单进程、单机数据库里的生命周期原型。当前实现已经具备完整的企业知识变更链路：来源同步、文档版本、ACL 传播、发布候选、原子激活、回滚、删除、对账、重试、死信、审计和指标都进入了同一套可恢复流程。

控制面已经支持 PostgreSQL，多 worker 通过数据库队列领取任务，并保证同一个 source 不会被两个 worker 同时处理。scheduler 使用数据库锁进行主节点竞争；API 的跨 Pod 任务状态和 SSE 事件使用 Redis 保存。查询侧每次请求都会读取当前 active Release，不依赖某台机器上的本地指针文件。

生产数据面也不再停留在 mock：Milvus、Neo4j 和 MinIO 的 ACL 更新、删除与反查已经跑过真实适配器。失败任务可以从 checkpoint 恢复，超过预算后进入 DLQ，也可以由人工安全 replay。

部署方面同时准备了两条路径：

1. 面向企业生产的 Kubernetes/Kustomize 与 ACK Pro 三可用区方案，包含 HPA、KEDA、PDB、拓扑分散、NetworkPolicy、受限容器权限和外部 Secret 契约。
2. 不产生云账单的三虚拟机 K3s 验收环境，用来真实验证 embedded-etcd、多控制面、KEDA 和单节点故障恢复。

## 已经拿到的验收结果

- 完整自动化测试：**280/280 PASS**，另有 66 个子测试通过。
- 生命周期基础设施故障演练：**15/15 PASS**。
- PostgreSQL 17 迁移、双 worker 并发领取、同 source 排他：PASS。
- PostgreSQL scheduler 单 leader 与释放后接管：PASS。
- Redis 跨进程任务状态和可重放 SSE：PASS。
- Milvus、Neo4j、MinIO 的 ACL 和删除传播：PASS。
- 阶段 2 冻结回归：12/12、核心 9/9，质量和成本 gate：PASS。
- 三虚拟机 K3s：3 个 control-plane/etcd 节点全部 Ready。
- KEDA operator、metrics apiserver、admission webhooks：全部 2/2 Available。
- 停止一个 K3s 控制面后，API 继续返回 ready，etcd 保持多数派，KEDA 每类服务至少保留一个可用副本；节点恢复后全部回到 2/2。

主要证据：

- `docs/roadmap/STAGE3_VALIDATION_20260925.md`
- `output/stage3-lifecycle-acceptance.20260926t064537z.json`
- `output/stage3-postgres-acceptance.20260926.json`
- `output/stage3-production-storage-acceptance.20260926.json`
- `output/stage3-distributed-runtime-acceptance.20260926.json`
- `output/stage3-postgres-distributed-acceptance.20260926.json`
- `output/stage3-k3s-vagrant-ha.20260926.json`

## 为什么还不能写“生产验收完成”

目前的三节点 K3s 集群运行在同一台 Windows 宿主机上的三台虚拟机里。它证明了 Kubernetes、etcd 和 KEDA 的行为是正确的，但三台 VM 仍共享电源、磁盘和物理网络，所以只有一个物理故障域，不能替代多主机或多可用区生产集群。

此外，Kubernetes 生产清单已经完成并能正确渲染，但业务应用尚未部署到企业最终集群。完整应用镜像此前也因为 Docker Hub 鉴权端点连接超时而没有取得构建成功证据。正式环境中的 Ingress/TLS、企业 SSO、CSI/PVC、External Secrets、托管数据库切换和企业 Secret Manager 仍需要现场验证。

最后，正式晋级必须使用目标企业数据重新执行 evaluation gate。旧的阶段 2 报告可以作为回归基线，但不能代替目标环境的新报告。

## 生产验收还需要完成的五件事

1. 准备三台独立主机或多可用区托管 Kubernetes，并接入正式域名、证书和入口网络。
2. 接入正式 HA PostgreSQL、Redis、Milvus、Neo4j、MongoDB、对象存储和 Secret Manager。
3. 在目标构建环境完成应用镜像构建、扫描、按摘要发布，并部署 production overlay。
4. 执行 node/AZ 驱逐、KEDA 扩缩、数据库与存储故障切换、滚动发布和回滚演练。
5. 使用企业真实数据运行三次完整 evaluation gate，达到 12/12、核心 9/9，并通过质量、安全、延迟和成本门禁。

## 当前决策

阶段 3 的实现和本地验收可以正式收口，历史失败报告继续保留，不覆盖、不删除。团队可以并行进入阶段 4A，但生产发布必须等上述目标环境验收全部通过后再批准。

阶段 2 继续作为冻结基线，不再回写；SQLite 仅保留给本地开发和单元测试，不作为多节点生产方案。

## 相关文档

- `docs/roadmap/STAGE3_ARCHITECTURE_ADR.md`
- `docs/roadmap/STAGE3_RUNBOOK.md`
- `docs/roadmap/STAGE3_MIGRATION_AND_ROLLBACK.md`
- `docs/roadmap/STAGE3_VALIDATION_20260925.md`
- `docs/PRODUCTION_DEPLOYMENT.md`
- `docs/DISTRIBUTED_PRODUCTION_DEPLOYMENT.md`
- `deploy/k3s-ha/README.md`
- `deploy/k3s-vagrant/README.md`
