# 本机三虚拟机 K3s 验收集群

这套环境在一台 Windows 电脑上创建三台 Ubuntu 虚拟机，并组成三 server 的 K3s embedded-etcd 集群。它不产生云账单，拓扑、Kubernetes API 和多数故障演练与三机部署一致，适合阶段 3 的部署、节点重启、任务恢复和发布回滚验收。

它不是生产高可用：三台虚拟机共享同一台物理主机、电源、系统盘和上联网路。宿主机故障时整个集群都会停止。真实生产仍应使用三台不同物理机或多可用区托管 Kubernetes；本环境只能作为上线前验收和开发集群。

## 宿主机预算

- Windows 11 x64，BIOS/UEFI 已开启硬件虚拟化；
- VirtualBox 7.2 和 Vagrant 2.4；
- ORAS CLI 1.3（用于按摘要缓存 KEDA 的 OCI 镜像）；
- 三台 VM 各 2 vCPU、4 GB RAM、30 GB 动态磁盘；
- 启动前建议宿主机至少有 14 GB 可用内存和 45 GB 可用磁盘；
- 默认 host-only 地址为 `192.168.56.21` 到 `192.168.56.23`。

本机同时启用 WSL2、Docker Desktop 或基于虚拟化的安全功能时，Windows hypervisor 通常处于运行状态。VirtualBox 可能改用兼容后端并显著变慢，也可能无法启动。安装程序不会自动关闭 Hyper-V、WSL2、内存完整性或修改启动项；这些都是整机级改动，必须由操作者明确决定。

## 安装免费工具

使用官方安装器，或在管理员 PowerShell 中安装：

```powershell
winget install --id Oracle.VirtualBox --exact
winget install --id Hashicorp.Vagrant --exact
winget install --id ORASProject.ORAS --exact
```

安装 VirtualBox 时虚拟网卡可能短暂重置网络。安装完成后关闭并重新打开终端；如安装器要求则重启 Windows。

## 预检和创建

先只做宿主机、配置和工具预检，不创建虚拟机：

```powershell
.\scripts\bootstrap_k3s_vagrant.ps1
```

只下载、校验并缓存离线制品，不创建或修改 VM：

```powershell
.\scripts\bootstrap_k3s_vagrant.ps1 -PrepareArtifacts
```

关闭占用内存较大的程序，使用密码管理器生成 32 到 128 字符的随机令牌，只写入当前终端：

```powershell
$env:SHOPKEEPER_K3S_TOKEN = '<随机值>'
.\scripts\bootstrap_k3s_vagrant.ps1 -Apply
```

如果预检发现 Windows hypervisor 正在运行，脚本会停止。优先选择保留 Docker/WSL2，并明确尝试 VirtualBox 的兼容后端：

```powershell
.\scripts\bootstrap_k3s_vagrant.ps1 -Apply -AllowHypervisorPresent
```

如果兼容后端不能稳定运行，先对无响应的单台 VM 做冷启动重试。只有多次重试仍失败时，才考虑暂时关闭 Windows hypervisor。该操作会影响 Docker Desktop、WSL2 以及部分系统安全功能，并需要管理员权限和重启；仓库脚本不会代为执行。恢复时也要重新打开并再次重启。

## 集群内容和验证

脚本固定安装 K3s `v1.35.8+k3s1`，第一台初始化 embedded etcd，另外两台加入；同时开启 secrets encryption、每 6 小时 etcd snapshot，并安装双副本的 KEDA 2.21.0。K3s 二进制和官方 air-gap 系统镜像包由宿主机从 K3s 官方中国镜像下载，失败时回退 GitHub Releases；下载一次并按官方 manifest 校验 SHA-256 后缓存到被 Git 忽略的 `.cache`，再分发到三台 VM。

KEDA 的三个 Linux AMD64 镜像由 ORAS 缓存为 OCI archive。默认下载入口是 `ghcr.linkos.org`，但脚本会先将三个 tag 的 OCI 根摘要与仓库中固定的官方 GHCR 摘要逐一比对，随后由内容摘要校验每一层；摘要不一致会立即停止。可通过 `SHOPKEEPER_KEDA_REGISTRY` 改成组织内拉取缓存或其他已审核 GHCR 代理。归档在每台节点本地导入，KEDA 使用 `IfNotPresent`，因此运行时不依赖公网镜像仓库。生产环境应把同一组按摘要固定的镜像同步到组织自己的 Harbor/ACR/ECR，而不是长期依赖公益代理。

集群令牌通过本机 SSH 标准输入发送，不写入 Vagrantfile、命令参数或仓库文件。

查看集群：

```powershell
Push-Location deploy\k3s-vagrant
vagrant ssh k3s-1 -c "sudo k3s kubectl get nodes -o wide"
vagrant ssh k3s-1 -c "sudo k3s kubectl -n keda get pods"
Pop-Location
```

验证单节点故障时，先停止一台非当前操作节点的 VM，再从 `k3s-1` 检查 API 和工作负载；恢复该 VM 后等待节点重新 Ready。不要同时停止两台 server，embedded etcd 会失去多数派。

仓库提供了可恢复的 K3s 服务级故障演练。默认只做预检；带 `-ApplyFault` 时停止一个控制面、从另一个控制面验证 API 和 KEDA 最低可用副本，最后在 `finally` 中恢复故障节点并把 JSON 证据写到 `output`：

```powershell
.\scripts\validate_k3s_vagrant_ha.ps1
.\scripts\validate_k3s_vagrant_ha.ps1 -ApplyFault -FaultNode k3s-1
```

## 暂停、恢复和清理

暂停会保留磁盘状态：

```powershell
Push-Location deploy\k3s-vagrant
vagrant halt
vagrant up --no-provision
Pop-Location
```

`vagrant destroy` 会不可恢复地删除三台 VM 及其本地集群数据，因此不由自动化脚本执行。需要清理时应先保存验收证据和 etcd snapshot，再由操作者在 `deploy/k3s-vagrant` 目录中明确执行。

默认基础镜像来自开源 Bento 项目的 `bento/ubuntu-24.04` Vagrant box。可用 `K3S_LAB_BOX` 和 `K3S_LAB_BOX_VERSION` 覆盖为组织内镜像；长期运行应把已审核的 box 缓存在内部制品库，避免依赖公共镜像服务的可用性。
