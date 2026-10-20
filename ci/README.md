# 可组合的固件构建框架

每种组合单独保存在 `ci/presets/<名称>.json`。设备、软件包、功能模块和额外文件分别配置；增加或调整组合无需复制 workflow。所有路径以仓库根目录为基准。

当前已接入个人配置 `x60pro-personal`；其余两种需求留待后续接入：

| 组合 | 设备 | 已知需求 | 当前安排 |
| --- | --- | --- | --- |
| 客户配置 | X60 Pro | Honk、Doona、OxiDNS、WireGuard | 等待实际本地配置 |
| `x60pro-personal` | X60 Pro | WireGuard、含 Cloudflare DNS 的 Caddy | 已导入上传的配置和文件备份 |
| 第三种配置 | EW-6000GX Pro | iStore 商店；基础固件没有 Passwall 时补入 | 后续单开分支完成设备适配 |

旧的 `x60-personal`、`x60-clean` 已移除。这里没有替三个组合选择版本、启动参数、域名或令牌，也没有创建 EW-6000GX Pro 的目标 profile。

## 目录与配置字段

```text
ci/
  devices.json                  # 已核实的设备 profile、基础配置、架构及镜像规则
  presets/                      # 后续每个组合一个 .json
  configs/                      # 可在此保存已验证的完整 .config 或精简配置
  assets/                       # 可在此保存二进制、HTML 等资源
  overlays/                     # 可在此建立公共或各组合的 rootfs 覆盖目录
  features/                     # 需要源码或特殊依赖的可选功能模块
  examples/preset.json.example   # 组合模板，不会自动参与构建
  examples/extra-files.json.example
```

`configs/`、`overlays/` 按需建立；空目录不必预先创建。模板中的示例输入路径也需要换成真实文件。

| 字段 | 用途 |
| --- | --- |
| `name` | 组合名，与 JSON 文件名一致 |
| `enabled` | `false` 时仅保存配置，不参与构建；模板默认关闭 |
| `device` | 引用 `devices.json` 中已支持的设备 |
| `config_file` | 可选，引用已验证的本地 `.config`；省略时使用设备的基础 defconfig |
| `feeds_file` | 可选，引用该组合的 feeds 配置，可固定到具体提交；省略时使用工作区默认 feeds |
| `features` | 功能模块名称数组，如 `honk`、`oxidns`、`wireguard` |
| `feature_options` | 所选模块的参数，例如源码 `ref`；具体见下文 |
| `packages.include` | 强制内置的普通软件包名称，无需为每个包编写模块 |
| `packages.exclude` | 从基础配置中关闭的软件包 |
| `kconfig` | 额外 Kconfig 选项；JSON 布尔值对应 y/n，数字和字符串也受支持 |
| `overlays` | 按顺序合并的 rootfs 文件目录 |
| `files` | 单独选择的本地文件、目录或可校验的下载资源 |

名称使用 1–63 个小写字母、数字、下划线或连字符，以字母或数字开头；`all` 为保留名。设备和软件包分别通过专用字段选择，`kconfig` 不能改写目标或软件包选择符号。功能模块的硬性依赖不能被组合配置关闭。

导入完整 `.config` 时，框架会检查其中经过组合覆盖后仍选中的软件包是否被 `make defconfig` 保留。缺少本地自定义包源码时会报错，方便后续补齐来源；完整 `.config` 本身不包含软件包源码。

## 额外文件如何进入 squashfs

框架为每次构建创建全新的 `files/`。OpenWrt 在生成 rootfs、squashfs 和 initramfs 时会复制这些内容，所以二进制和 HTML 在固件生成前就已内置，无需刷机后下载。

本地 Doona 静态目录可以作为一项 `files`：

```json
{
  "id": "doona",
  "source": "ci/assets/doona",
  "destination": "/etc/honk/doona",
  "required_files": ["index.html"]
}
```

本地已验证、包含 Cloudflare DNS 模块的 Caddy 二进制可以作为另一项：

```json
{
  "id": "caddy-cloudflare",
  "source": "ci/assets/caddy-linux-arm64",
  "destination": "/usr/bin/caddy",
  "architecture": "aarch64",
  "mode": "0755"
}
```

这些只是字段示例，不是当前启用的组合。`source` 为仓库内路径。单文件可以额外设置 `sha256` 来锁定内容；二进制的 `architecture` 会检查 ELF 架构，并与设备登记的架构核对。该检查不代替 Caddy 的模块验证，后续应使用你已经确认包含 `dns.providers.cloudflare` 的二进制。

`overlays` 则适合成组文件。例如一个目录包含：

```text
ci/overlays/example/
  etc/caddy/Caddyfile
  etc/init.d/caddy
  usr/share/example/index.html
```

将 `"ci/overlays/example"` 加入组合的 `overlays`，就会按原路径复制到 rootfs。普通文件保留权限，可执行的 init 脚本应在来源目录中设置执行位；框架保留空目录，并统一为目录设置 `0755`。当前支持普通文件和目录，符号链接与特殊文件会报错。

合并顺序：先按 `overlays` 列表复制，后列出的目录覆盖前面的同名文件；再按 `files` 列表处理额外资源。额外资源若与已暂存文件重名，默认报错；确认需要替换时，在该项中设置 `"replace": true`。未选择的目录、资源和旧组合文件不会自动加入固件。

## 下载资源和固定版本

单文件也可指定 HTTPS `url`，并且必须提供真实 `sha256`。需要解包时添加 `"archive": "tar"`，可用 `strip_components` 去掉顶层目录；支持 gzip、xz、bzip2 或未压缩 tar。`required_files` 用于检查解包后的必要文件。

也可以描述 GitHub release，例如：

```json
{
  "id": "doona",
  "github": {
    "repository": "Zakkaus/doona",
    "ref": "latest",
    "prereleases": true,
    "asset": "doona-{version}.tar.gz"
  },
  "archive": "tar",
  "destination": "/etc/honk/doona",
  "required_files": ["index.html"]
}
```

这项也仅是可选示例。`ref` 可以换成确切 release tag；`{tag}` 使用原始 tag，`{version}` 去掉开头的 `v`。`latest` 按发布时间选择，`prereleases` 决定是否包含预发布版本。下载后必须通过配置提供的 SHA256、GitHub 资源摘要或该 release 的 `SHA256SUMS` 校验，没有可用校验值时构建失败。

每个资源恰好选择一种来源：`source`、`url` 或 `github`。`mode` 和 `architecture` 仅用于单个原始文件；解包目录保留各文件的执行属性。解包会拒绝越界路径、重复路径及链接。

## 可选功能模块

普通 feed 软件包直接用 `packages.include`。需要下载源码、补充 BTF 依赖等操作时再使用 `features`。目前提供：

- `honk`：准备 Honk/LuCI 源码、选择 XDP 和内核 BTF；默认关闭 Honk。Doona 文件独立通过 `files` 选择。
- `oxidns`：准备 OxiDNS LuCI 管理界面源码；核心按实际配置后续另行接入。
- `wireguard`：选择内核模块、工具、LuCI 协议及 RPC 支持。

Honk 和 OxiDNS 的 `feature_options` 支持 `ref`，可指定分支、tag 或提交，省略时读取远端 `HEAD`。Honk 还支持 `dashboard`；例如选择 Doona 时可用 `"feature_options": {"honk": {"dashboard": "doona"}}`，框架只修改对应包的默认值，保留其他上游配置。WireGuard 当前没有模块参数。

后续添加模块时，脚本放到 `ci/features/<名称>.sh`。源码放入框架传入的 `CI_PACKAGE_DIR`，临时文件使用 `CI_WORK_DIR`；将必要配置同时追加到 `.config` 和 `CI_REQUIRED_CONFIG`，来源信息写入 `CI_METADATA_DIR`。模块参数通过 JSON 环境变量 `CI_FEATURE_OPTIONS` 传入。主脚本统一清理本次创建的源码和 rootfs 暂存目录，无需为新模块修改清理列表。

## 验证与构建

仅校验现有配置目录：

```bash
python3 ci/matrix.py --check
python3 -m unittest discover -s ci/tests
```

当前应显示 `Validated 1 preset(s)`。构建已接入的个人配置：

```bash
bash ci/build.sh x60pro-personal
```

该组合直接复用上传的完整 `.config`，不再重复应用 WireGuard 模块；Caddy `v2.11.7`、Cloudflare DNS 模块 `v0.2.4` 和启动脚本来自原始 `files.tar.gz`，解包时去掉外层 `files/`，保留执行权限。备份不含 `/etc/caddy/Caddyfile`；本次按需求沿用设备上已有的运行配置，不生成或修改该文件。feeds 固定为本次配置检查所用提交。

以后添加组合后，先查看最终配置和 matrix：

```bash
python3 ci/matrix.py --preset example
python3 ci/matrix.py all
```

使用专用 Linux checkout，从仓库根目录构建：

```bash
bash ci/build.sh example
```

主脚本只接收组合名，设备、软件包及文件都从该配置读取。它会重建工作区 `.config`，拒绝覆盖现有 `files/` 和 `package/ci-custom/`。默认更新并安装 feeds、应用配置及模块、运行 `make defconfig`、准备额外文件，再下载和编译。`CI_CONFIGURE_ONLY=1` 在配置与额外文件准备完成后停止；`CI_SKIP_FEEDS_UPDATE=1` 可复用已有 feeds；`CI_JOBS=4` 等正整数可限制编译并行数。仅配置模式仍可能需要联网。

指定 `feeds_file` 时会在专用工作区创建 `feeds.conf`；若已有 `feeds.conf` 内容不同，脚本会停止，避免覆盖手工配置。

在 GitHub 上，提交框架及实际组合后，使用 **Actions → Build ImmortalWrt Firmware → Run workflow** 选择分支并填写组合名，或者用 `all` 构建所有启用的组合。新增组合会自动进入 matrix，未启用的草稿不会参与构建。

## GitHub Actions 完整增量构建

workflow 默认启用 `CI_INCREMENTAL=1`，通过独立的 `build-state-…` artifact 保存完整构建状态。下一次运行自动下载最近一份兼容快照，恢复后继续执行 OpenWrt 的增量构建。首次运行或者没有兼容快照时从头编译。

快照包含 `build_dir/`、`staging_dir/`、`bin/`、`tmp/`、`dl/`、`.ccache/`、feeds 及其安装链接、Kconfig 编译产物、构建配置和已生成的签名密钥；自定义模块源码会保存在 `.ci-incremental/custom-sources/` 中复用。`CONFIG_AUTOREMOVE` 在此模式下强制关闭，防止 OpenWrt 编译完成后自动删除软件包构建目录。当前源码由正常 checkout 获取，工作区根 `.git` 和进程环境不会被主动归档。

快照使用 tar + zstd，保留 Unix 权限、链接和纳秒时间戳。框架还会记录源码内容散列：fresh checkout 或模块重新准备后，仅对内容和权限均未改变的源码恢复原时间戳。这样 OpenWrt 的时间戳及 `.prepared`、`.configured`、`.built` 等依赖判断能够继续使用；实际改动的源码保留新内容和新时间戳，正常触发重编译。`files/` 每次仍重新组装，以免旧组合文件残留。

| 变化 | 恢复策略 |
| --- | --- |
| 同一分支、同一组合重跑 | 复用兼容的完整状态 |
| 源码提交变化、增减插件或更换 HTML／二进制 | 可复用状态，由 OpenWrt 重建受影响部分 |
| 组合、设备、编译器／libc 选择、工具链或目标平台定义变化 | 使用不同快照组，没有兼容状态时从头编译 |
| runner 镜像、宿主机依赖版本或工作区绝对路径变化 | 从头编译，避免复用不兼容的主机工具和绝对路径 |

同一组合的构建任务会串行执行，`all` 与单独构建该组合也共用同一个并发组。需要主动全新构建时，在 **Run workflow** 勾选 `fresh`；它会忽略旧快照，成功保存后可供后续运行继续使用。

普通编译失败时，退出钩子仍会记录状态，后续步骤保存已完成的工具链、工具和软件包，方便下次接续；runner 被取消、强制终止或超时时，本轮新进度无法保证上传，之前已保存的快照仍可使用。校验失败会报错，可用 `fresh` 重新开始；保存或上传完整状态失败也会使 workflow 报错，不会静默退回仅 ccache 的保存方式。固件 artifact 会在快照打包前上传。

完整状态默认保留 **90 天**，受仓库 artifact 保留策略、存储额度和 runner 磁盘容量限制；打包和恢复还需要容纳一份压缩归档。每次运行会产生独立快照，历史快照不会自动删除。快照过期或被删除后会重新完整编译。保留天数可修改 workflow 的 `Save complete build checkpoint`；GitHub 对 artifact 的限制见[官方设置说明](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/managing-repository-settings/managing-github-actions-settings-for-a-repository)。

本地也可用 `CI_INCREMENTAL=1 bash ci/build.sh example` 保留输入状态并复用模块源码；完整归档的上传、查找和下载由 Actions 负责。

## 产物与追溯

成功结果位于 `output/<组合名>/`，包括所选设备的镜像、manifest、`build.config`、`diffconfig`、`BUILD_INFO.txt` 以及：

- `metadata/preset.json`：本次实际使用的完整组合快照。
- `metadata/rootfs-files.json`：额外资源的来源、解析后的版本及文件路径、权限、SHA256。
- feeds、自定义软件包的提交和本地修改补丁。
- `SHA256SUMS`：所有输出文件的校验和，不包含它自身。

同一组合的旧输出会被替换；之前发布的 artifact 可保留该版本的记录。校验命令为 `sha256sum -c SHA256SUMS`。失败时 Actions 会上传已有的配置、日志和来源信息。

组合文件、配置和覆盖目录都由 Git 记录变化。相同组合后续可修改插件列表、资源路径或版本；需要保留旧配方时使用 Git tag/提交，或复制为另一个组合。选择 `latest`、可变分支或共享覆盖目录意味着内容可能随时间变化，来源记录用于追溯，不等于所有依赖已锁定。
