# 分布式生产部署

本项目的生产主方案是多可用区 Kubernetes 应用层加外部高可用数据服务。`docker-compose.production.yml` 只用于开发、边缘节点或灾备演练，不承担企业主生产。

## 目标拓扑

```text
WAF / API Gateway / SSO
           |
       Ingress + TLS
           |
  API Deployment (3–12 Pods, HPA, Redis task/SSE state)
           |
           +--------- PostgreSQL HA / PITR（生命周期控制面）
           +--------- Redis HA（跨 Pod 短任务状态与 SSE Streams）
           +--------- Milvus Cluster（向量与 sparse）
           +--------- Neo4j Enterprise/Managed Cluster（知识图谱）
           +--------- MongoDB Replica Set/Managed（会话历史）
           +--------- S3/MinIO Distributed（文档与资产）

  Scheduler Deployment (2 Pods)
           | PostgreSQL advisory lock：单活扫描、故障自动接管

  Lifecycle Worker Deployment (2–30 Pods)
           | PostgreSQL SKIP LOCKED + source lease + KEDA queue scaling
```

API、scheduler 和 worker 都是可替换 Pod；Release 活动指针只以 PostgreSQL 为准，不依赖节点本地文件。worker 的本地目录仅保存可重建的临时数据，正式资产进入对象存储和版本化数据面。

## 集群与外部服务前提

- Kubernetes 版本必须与所选 KEDA 版本的官方兼容窗口一致；当前 ACK 引导脚本固定 KEDA 2.21.0，并在 Kubernetes 1.34–1.36 之外 fail closed。至少三个 worker node，分布在三个可用区；安装 Metrics Server。
- 安装 KEDA，并启用 PostgreSQL scaler。当前清单按 [KEDA PostgreSQL scaler](https://keda.sh/docs/2.21/scalers/postgresql/) 的 connection authentication 和单数值 query 契约编写。
- PostgreSQL 使用多可用区/同步副本、自动故障切换、TLS、PITR 和独立备份。API/worker、scheduler、migration 与 KEDA 分别使用独立 DSN/账号。API/worker 可走 PgBouncer transaction pooling；scheduler 的 session advisory lock 必须走数据库直连或 session pooling，禁止走 transaction pooling；migration 走 primary 直连。
- Redis 使用托管高可用或 Sentinel/Cluster，启用 TLS、认证和持久化。它承载跨 Pod 查询进度和 SSE Stream，不承载 lifecycle 权威状态。
- Milvus 使用 Cluster 形态并配置独立 etcd/object storage；Neo4j 使用支持集群的 Enterprise/Managed 版本；MongoDB 使用 replica set；MinIO 使用 distributed/纠删码或直接使用云对象存储。
- 模型 PVC `shopkeeper-models` 必须支持 ReadOnlyMany，包含 `bge-m3` 和 `bge-reranker-large`；Source PVC `shopkeeper-sources` 必须支持 ReadOnlyMany/ReadWriteMany。更推荐后续连接器直接读取企业对象存储或内容系统，避免文件共享成为瓶颈。
- Ingress/WAF 提供 TLS、OIDC/SSO、速率限制、请求大小限制和访问上下文签名。不能把管理令牌发给浏览器。

Kubernetes 官方建议为副本服务配置资源 request、拓扑分散和 PDB；HPA 依赖 Metrics Server，并根据资源或外部指标调整 Deployment。仓库清单已经包含这些对象：[HPA](https://kubernetes.io/docs/concepts/workloads/autoscaling/horizontal-pod-autoscale/)、[Pod disruption](https://kubernetes.io/docs/concepts/workloads/pods/disruptions/)、[Topology spread](https://kubernetes.io/docs/concepts/scheduling-eviction/topology-spread-constraints/)。

## 生产配置入口

- ACK Pro 集群 Terraform：`deploy/terraform/alicloud-ack`
- ACK 受控创建脚本：`scripts/provision_ack_cluster.ps1`
- ACK 集群运行时引导：`scripts/bootstrap_ack_runtime.ps1`
- 基础清单：`deploy/kubernetes/base`
- 生产 overlay：`deploy/kubernetes/overlays/production`
- Secret 字段契约：`deploy/kubernetes/runtime-secret.example.yaml`
- Prometheus Operator 示例：`deploy/kubernetes/addons/servicemonitor.yaml`

生产 overlay 默认包含 17 个对象：Namespace、ServiceAccount、ConfigMap、三个 Deployment、Service、Ingress、API HPA、worker KEDA ScaledObject/TriggerAuthentication、三个 PDB 和三条 NetworkPolicy。

如果还没有 Kubernetes 集群，先按 `deploy/terraform/alicloud-ack/README.md` 创建 ACK Pro 三可用区集群。Terraform 默认只生成 plan；只有同时传入 `-Apply -ApproveCharges` 才会创建计费资源。生产状态存入加密 OSS，并由 TableStore 锁防止并发写入。

如果预算为零且已经拥有三台独立 Linux 机器，可按 `deploy/k3s-ha/README.md` 建立三节点 embedded-etcd K3s 集群。这条路径没有新增云账单，但没有云 SLA、跨可用区能力或异地灾备，定位是长期验证与小流量试运行，不能与 ACK Pro 的生产承诺等同。

## 上线前配置

1. 构建镜像、执行扫描和 SBOM/签名，然后推送到企业镜像仓库。生产 overlay 必须使用不可变版本或 digest，禁止 `latest`。
2. 修改 `overlays/production/kustomization.yaml` 的镜像地址和版本。
3. 修改 `configmap-patch.yaml` 的域名、模型网关、Milvus、Neo4j、对象存储和模型名称。
4. 修改 Ingress 域名、IngressClass 和 TLS secret 名称。
5. 由 External Secrets Operator、Vault Agent、CSI Secrets Store 或云 Secret Manager 创建 `shopkeeper-runtime`。不要直接应用示例 secret。所需字段为：
   - `LIFECYCLE_DATABASE_URL`
   - `LIFECYCLE_SCHEDULER_DATABASE_URL`：直连或 session pooling，不能使用 transaction pooling
   - `LIFECYCLE_MIGRATION_DATABASE_URL`：允许执行版本化 DDL 的 primary 连接
   - `KEDA_DATABASE_URL`：只允许连接数据库并 `SELECT lifecycle_tasks` 的只读账号
   - `LIFECYCLE_ADMIN_TOKEN`
   - `ACCESS_CONTEXT_HMAC_SECRET`
   - `TASK_STATE_REDIS_URL`
   - `OPENAI_API_KEY`
   - `NEO4J_PASSWORD`
   - `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY`
   - `MONGO_URL`
6. 创建 `shopkeeper-models` 和 `shopkeeper-sources` PVC。清单故意不自动创建存储类相关 PVC，避免在错误的单区磁盘上“成功部署”。
7. 给 Ingress 和监控所在 namespace 添加 `shopkeeper-brain-access=true` 标签，否则默认拒绝访问 API。

## 渲染与发布

先做仓库级校验。未替换 placeholder 时仅可使用示例模式：

```powershell
.\scripts\validate_kubernetes_deployment.ps1 -AllowPlaceholders
```

修改为真实配置后必须不带开关执行；残留 `example.*` 或 `replace-with-*` 会直接失败：

```powershell
.\scripts\validate_kubernetes_deployment.ps1
kubectl diff -k deploy/kubernetes/overlays/production
kubectl apply -k deploy/kubernetes/overlays/production
```

三个 Deployment 都有 migration initContainer。迁移使用 PostgreSQL advisory transaction lock 串行化，并通过 `schema_migrations` 保证幂等；主容器设置 `LIFECYCLE_AUTO_MIGRATE=false`，避免请求路径偷偷迁移。

观察发布：

```powershell
kubectl -n shopkeeper-brain rollout status deployment/shopkeeper-api --timeout=10m
kubectl -n shopkeeper-brain rollout status deployment/shopkeeper-lifecycle-worker --timeout=10m
kubectl -n shopkeeper-brain rollout status deployment/shopkeeper-lifecycle-scheduler --timeout=10m
kubectl -n shopkeeper-brain get pods,hpa,pdb,scaledobject
```

## 高可用语义

- API：HPA 3–12，按 CPU/内存扩缩；Redis Hash 保存任务结果，Redis Streams 保存可重连 SSE，因此 POST 与 stream 请求可落在不同 Pod。
- Scheduler：运行两个 Pod。每轮扫描持有 PostgreSQL session advisory lock；非 leader 不调度，leader 连接中断后锁自动释放，另一 Pod 下一轮接管。
- Worker：最少两个，KEDA 根据 PostgreSQL 中 pending/retrying 且已到执行时间的任务数扩到 30。真正领取仍使用 `FOR UPDATE SKIP LOCKED`，KEDA 只决定容量，不决定所有权。
- Release：`LIFECYCLE_RELEASE_POINTER_MODE=database`；每次查询读取 PostgreSQL active Release，任一节点本地文件损坏都不会改变线上版本。
- 终止：scheduler/worker 捕获 SIGTERM，停止领取新任务并完成当前边界；worker 若被强杀，lease 过期后由其他 Pod 从 checkpoint 恢复。
- 可用性：API 和 worker 设置 PDB 与 zone/hostname topology spread。PDB 只约束遵守 Eviction API 的自愿中断，不能阻止节点硬故障，因此仍需多可用区副本。

## 安全边界

- 所有容器使用 UID/GID 10001、`readOnlyRootFilesystem`、`RuntimeDefault` seccomp、drop ALL capabilities、禁用 ServiceAccount token 自动挂载。
- 默认 NetworkPolicy 同时拒绝 ingress/egress；只开放带授权 namespace 到 API，以及 DNS和明确的数据服务 TCP 端口。若 CNI 支持 FQDN/CIDR policy，应继续把 egress 收紧到实际地址。
- Secret 不进入 ConfigMap、镜像、Git、日志或任务 payload。访问上下文签名密钥只提供给 API 和可信网关。
- 分布式模式禁用旧 `/upload` 本地后台任务；生产导入必须创建 durable lifecycle Source 并触发 sync。这样节点丢失不会让上传任务只剩内存状态。

## 监控、发布门禁与恢复

- Prometheus 抓取 `/metrics`，并对 queue depth、oldest task age、DLQ、sync/ACL/delete lag、active release health 和依赖错误率告警。
- 每次发布先在独立 namespace 运行 contract/full evaluation；再滚动 API/worker，最后执行 canary/shadow Release。不能用 Kubernetes rollout 代替知识 Release 门禁。
- PostgreSQL 以 PITR 为控制面恢复点；Milvus、Neo4j、MongoDB 和对象存储各自备份。恢复后先暂停 scheduler，运行 reconciliation，再恢复 worker，最后开放新 Release 激活。
- 至少每季度演练：单 Pod、单 node、单 AZ、PostgreSQL 主库、Redis 主节点、Milvus query node 和对象存储故障。

## 已知边界

- 该方案是单 region、多 AZ 高可用，不是跨 region active-active。跨 region 需要明确 RPO/RTO、数据复制方向和单写控制面。
- Redis 让查询状态与 SSE 跨 Pod，但查询计算仍在接收 POST 的 API Pod 内执行。自愿驱逐受 PDB、preStop 和 120 秒 termination grace 保护；节点硬故障时客户端需要用相同业务请求幂等重试。若要求查询任务也具备跨节点 checkpoint，应在后续引入独立 query queue/worker，不能把当前短任务伪称为 exactly-once。
- 本仓库没有可访问的真实 Kubernetes 集群，因此目前完成的是 Kustomize 渲染、结构约束和真实 PostgreSQL/Redis/数据面进程级验证；集群级 node/AZ 驱逐、KEDA 扩缩和托管服务故障切换必须在目标环境执行后才能标记为生产上线 PASS。
