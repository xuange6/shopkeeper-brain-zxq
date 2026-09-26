# 单机生产部署

> 本文现在只用于本地生产形态、边缘节点或灾备演练。企业主生产请使用 [分布式生产部署](DISTRIBUTED_PRODUCTION_DEPLOYMENT.md)；单机 Compose 不具备节点/可用区故障隔离，不能作为阶段 3 的最终上线形态。

这套部署面向一台受控 Linux/Windows Docker 主机：一个 API、一个 scheduler、两个 lifecycle worker，以及 PostgreSQL、Milvus、Neo4j、MongoDB、MinIO、etcd。只有 API 默认绑定到宿主机 `127.0.0.1:8000`；数据服务不发布宿主机端口。公网流量应先经过提供 TLS、认证、限流和访问上下文签名的反向代理或网关。

它是可落地的单站点起点，不是跨可用区高可用方案。生产数据晋级前仍须用目标企业数据跑 evaluation gate。

## 1. 生成本机配置与密钥

在仓库根目录执行：

```powershell
.\scripts\bootstrap_production_env.ps1
```

脚本使用系统加密随机数生成 PostgreSQL、Neo4j、MongoDB、MinIO、生命周期管理令牌和访问上下文签名密钥。若 `knowledge/.env` 已存在，它还会复用其中的模型服务配置，并在两个本地 BGE 模型位于同一父目录时自动设置只读模型挂载。存储、生命周期和授权密钥不会从开发配置继承。生成的 `deploy/.env.production` 与 `deploy/secrets/*.txt` 已被 Git 忽略；不要把它们复制到镜像、工单或日志。

如需轮换全部本地密钥，先停服务并完成数据库/对象存储凭据切换，再执行：

```powershell
.\scripts\bootstrap_production_env.ps1 -Force
```

`-Force` 会同时改写所有生成值，不能在运行中的数据卷上直接使用。

## 2. 补齐模型与入口配置

编辑 `deploy/.env.production`：

- 把 `APP_CORS_ORIGINS` 改成真实 HTTPS 域名；
- 设置 `OPENAI_API_BASE`、`OPENAI_API_KEY`、`MODEL`、`ITEM_MODEL` 和 `VL_MODEL`；
- 将 BGE-M3 与 reranker 放到 `MODEL_HOST_DIR` 下的 `bge-m3`、`bge-reranker-large`；该目录以只读方式挂载到容器 `/models`；
- 将允许读取的源文件放在 `deploy/data/sources`。容器只读挂载到 `/data/sources`；
- 接入网关时，由可信服务使用 `ACCESS_CONTEXT_HMAC_SECRET` 签发短期访问上下文。浏览器和普通调用方不得持有该密钥。

当前部署文件把第三方模型 API key 放在仅主机可读、被 Git 忽略的 env 文件中。若平台已有 Vault、Kubernetes Secret 或云密钥服务，应在部署平台层注入，而不是继续使用本机文件。

## 3. 静态校验并启动

```powershell
.\scripts\validate_production_deployment.ps1
docker compose --env-file deploy/.env.production `
  -f docker-compose.yml `
  -f docker-compose.production.yml `
  up -d --build
```

查看状态和 API 日志：

```powershell
docker compose --env-file deploy/.env.production `
  -f docker-compose.yml `
  -f docker-compose.production.yml ps

docker compose --env-file deploy/.env.production `
  -f docker-compose.yml `
  -f docker-compose.production.yml logs --tail 200 api lifecycle-scheduler lifecycle-worker
```

本机检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/metrics
```

`/health` 正常只证明进程可用。首个 Release 激活后还应确认指标 `active_release_health` 为 1，并执行一次带签名身份的授权查询、一次无权限查询和一次管理 API 鉴权失败探针。

## 4. 创建 Source 与首次发布

管理接口位于 `/api/lifecycle/admin/*`。令牌从本机 secret 文件读取，不要写进脚本仓库：

```powershell
$adminToken = Get-Content deploy/secrets/lifecycle_admin_token.txt -Raw
$headers = @{ "X-Lifecycle-Admin-Token" = $adminToken.Trim() }
Invoke-RestMethod http://127.0.0.1:8000/api/lifecycle/admin/sources/handbook `
  -Method Put `
  -Headers $headers `
  -ContentType application/json `
  -Body '{"tenant_id":"public","connector_type":"local_directory","configuration_version":"v1","configuration":{"root":"/data/sources/handbook"},"sync_policy":{"interval_seconds":300,"full_scan":true}}'
```

具体 source contract、Release 状态迁移、验证和回滚步骤见 [阶段 3 Runbook](roadmap/STAGE3_RUNBOOK.md)。生产 source 只保存 credential reference；请求中出现 password、secret、token、api_key 或 access_key 会被控制面拒绝。

## 5. 持久化、备份与恢复

必须备份这些命名卷：PostgreSQL、Milvus、etcd、MinIO、MongoDB、Neo4j，以及 `lifecycle-state`、`lifecycle-staging`、`lifecycle-uploads`。恢复顺序是 PostgreSQL/etcd/数据面，然后 API、scheduler、worker；恢复后先执行 reconciliation，再允许新 Release 激活。

代码回滚不得删除 PostgreSQL lifecycle tables。先暂停 source 和 worker，回滚到仍理解当前 schema 的镜像；若要回退查询 Release，使用 lifecycle publisher 的 previous release，不要重建 active collection。详见 [迁移与回滚](roadmap/STAGE3_MIGRATION_AND_ROLLBACK.md)。

## 6. 上线边界

正式对外前还要由部署平台补齐：TLS/SSO、网络策略、防火墙、集中日志、Prometheus 告警、主机与卷监控、备份恢复演练、密钥轮换、镜像扫描和目标企业数据 evaluation gate。跨主机或高可用部署应把数据库和数据面改为托管/集群服务；API、scheduler 和 worker 镜像可以保持不变。
