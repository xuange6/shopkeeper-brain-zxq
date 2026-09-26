# 三节点 K3s：零新增云账单方案

这套方案使用三台**现有**的 Linux 机器组成 K3s 高可用集群，不购买云主机或托管 Kubernetes。三台机器都运行 control plane、embedded etcd 和 workload；etcd 保持 2/3 quorum，因此允许一台机器故障。

“免费”指没有新增云账单和软件许可证费用，不代表没有电费、网络、硬件折旧或运维成本。它适合家庭实验室、办公室内网、小流量试运行和上线前验证；没有云厂商 SLA，也不能替代异地灾备。

## 硬件和网络

需要三台不同的物理机器，不能是在同一台电脑里开的三个虚拟机。每台建议：

- x86_64 Linux，Ubuntu 22.04/24.04；
- 至少 2 CPU、4 GB RAM、50 GB SSD；运行完整知识服务建议 4 CPU、16 GB RAM；
- 固定内网 IP、唯一 hostname、机器之间低延迟互通；
- SSH 公钥登录和免交互 `sudo`；
- 开放节点间 TCP 6443、2379-2380、10250，UDP 8472，以及业务需要的 80/443；
- 时间同步正常，禁止三台机器共用同一块易失磁盘或同一个虚拟化宿主机。

K3s 官方最低 server 规格是 2 CPU/2 GB RAM；这里把预检门槛提高到 2 CPU/4 GB RAM，给 etcd、KEDA 和系统组件留下余量。

## 配置

复制清单并填写三台机器的真实内网地址：

```powershell
Copy-Item deploy/k3s-ha/inventory.example.json deploy/k3s-ha/inventory.json
```

`sshKeyPath` 可以留空以使用 SSH Agent/默认密钥，也可以填写私钥绝对路径。该文件已被 Git 忽略。

创建一个至少 32 字符的随机集群令牌，只放入当前终端环境变量。不要把它发到聊天、提交到 Git 或写入 JSON：

```powershell
$env:SHOPKEEPER_K3S_TOKEN = '<由密码管理器生成的强随机值>'
```

## 先做零改动预检

```powershell
.\scripts\bootstrap_k3s_ha.ps1
```

默认模式只检查清单、SSH、Linux、sudo、curl、CPU、内存以及三台机器是否不同，不安装任何内容。

确认后才安装：

```powershell
.\scripts\bootstrap_k3s_ha.ps1 -Apply
```

安装脚本会：

1. 固定安装 K3s `v1.35.8+k3s1`，避免 stable channel 漂移；
2. 第一台初始化 embedded etcd，另外两台加入同一集群；
3. 开启 Kubernetes secrets encryption；
4. 每 6 小时生成 etcd snapshot，本机保留 20 份；
5. 安装 KEDA 2.21.0，并把 operator、metrics server、webhook 都运行两个副本；
6. 等待三个节点和 KEDA 全部 Ready。

## 获取 kubeconfig

脚本不会自动复制管理员 kubeconfig，避免把长期凭据落入错误目录。在可信管理机上执行：

```powershell
ssh ubuntu@192.168.10.21 "sudo cat /etc/rancher/k3s/k3s.yaml" > $env:TEMP\shopkeeper-k3s.yaml
```

把 kubeconfig 中的 `127.0.0.1` 改成任一 server IP，文件权限只允许管理员读取。三个 server IP 都写进证书 SAN；如果首选节点故障，可以切换到另外两个。正式使用建议在路由器上配置内网 VIP，或用内部 DNS 的多个 A 记录指向三个 server。

## 仍然需要解决的生产边界

- 三台机器如果共用机房、电源、交换机和公网线路，仍然存在站点级单点故障。
- etcd snapshot 只在本机不算备份；至少每天加密复制到另一台 NAS 或异地对象存储，并定期恢复演练。
- `shopkeeper_brain` 还需要 PostgreSQL、Redis、Milvus、Neo4j、MongoDB、对象存储和共享模型文件。不能为了“免费”把这些全部塞成无备份单副本后宣称生产可用。
- 仓库生产 Ingress 当前使用 NGINX；K3s 默认安装 Traefik。正式发布前需要选择一个并调整 `ingressClassName`，不能同时假设两者存在。
- 当前脚本只接受同一低延迟私网的三个节点。不要把三个免费云主机跨供应商拼成 embedded-etcd 集群；K3s 官方明确提示高延迟会影响健康，并要求 etcd server 通过私网互通。
