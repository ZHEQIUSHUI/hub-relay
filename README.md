# hub-relay

把 HuggingFace / ModelScope 的私有仓库当作**文件中转通道**和**个人网盘**使用。

适用场景：两台机器之间无法直连（跨网络、跨地域、没有公网 IP），但都能访问 HuggingFace（或 hf-mirror）或 ModelScope。

```bash
# 发送端
relay push ./model.bin --to server-a     # 输出短码，如 k7f3pq

# 接收端
relay pull k7f3pq -o ~/models            # 下载并校验 sha256，完成后从中转仓库删除
relay watch                              # 或常驻：自动接收发给本机的文件到 ~/relay-inbox
```

## 特性

- **短码传输**：`push` 返回短码，`pull <code>` 接收；也可以用 `--to <机器名>` 配合 `watch` 自动投递
- **大文件分块**：超过阈值的文件自动切块，逐块提交
- **断点续传**：上传、下载中断后重跑同一条命令即可继续
- **完整性校验**：分块和整文件都校验 sha256
- **自动清理**：接收后删除；`gc` 清理过期传输并压平仓库历史，释放配额
- **网盘模式**：`store` 子命令按原样存放文件，可直接在 HF / ModelScope 网页上浏览
- **多后端**：HuggingFace、ModelScope，`pull` 会自动在所有已配置的后端中查找

## 安装

```bash
git clone https://github.com/ZHEQIUSHUI/hub-relay && cd hub-relay
./install.sh                      # 安装到独立 venv（~/.local/share/hub-relay），链接到 ~/.local/bin/relay
RELAY_EXTRAS=ms ./install.sh      # 同时安装 ModelScope 后端
```

也可以直接：`pip install git+https://github.com/ZHEQIUSHUI/hub-relay`

## 初始化

```bash
relay init --name <本机名>        # 生成 ~/.config/relay/config.toml，创建私有仓库 <user>/relay-tunnel、<user>/relay-store
relay doctor                      # 检查配置和各后端连通性
```

- **HF token**：默认使用 huggingface_hub 自身的 token（`$HF_TOKEN` 或 `hf auth login`），也可以用 `--hf-token-file` 指定
- **ModelScope token**：读取 `~/.config/relay/ms_token` 或 `$MODELSCOPE_API_TOKEN`
- **HF endpoint**：`--hf-endpoint auto` 会探测 huggingface.co，不可达时自动改用 `https://hf-mirror.com`

token 只保存在本地，不会写入仓库或命令行参数。

## 命令

| 命令 | 说明 |
|---|---|
| `push PATH... [--to NAME] [--ttl H] [--keep] [--code C] [--curl]` | 发送文件或目录 |
| `pull CODE [-o DIR] [--keep] [-f]` | 按短码接收 |
| `ls` / `rm CODE...` | 查看 / 删除中转中的传输 |
| `watch [-o DIR] [--once]` | 自动接收 `--to` 本机的传输，轮询间隔自适应（10s / 60s / 5min） |
| `gc [--no-squash]` | 删除过期和未完成的传输，并压平中转仓库历史 |
| `store put/ls/get/rm/url` | 网盘仓库操作 |
| `install-skill` | 安装 Claude Code skill |
| `doctor` | 显示配置并测试各后端 |

通用参数：`--via hf|ms` 指定后端；`--json` 在 stdout 输出机器可读结果，日志统一输出到 stderr。

没有安装 relay 的机器可以用 `push --curl` 打印的 curl 命令直接下载。

## 工作原理

中转仓库布局：

```
t/<code>/.start            传输开始标记（供 gc 清理未完成的上传）
t/<code>/d/<path>          小文件，原样上传
t/<code>/d/<path>.pNNNNN   大文件分块
t/<code>/manifest.json     最后写入；存在即表示上传完成
```

- `watch` 和 `pull` 只处理已有 manifest 的传输，不会读到上传了一半的文件
- 上传端在 `~/.cache/relay/pending.json` 记录"文件集合 → 短码"，重跑时跳过远端已存在且 sha256 一致的分块
- 下载端把分块追加到 `<file>.relay-partial`，重跑时从最后一个完整分块继续
- 中转与网盘使用两个仓库：HF 的历史压平作用于整个仓库，分开后中转仓库可以随时压平，不影响网盘

## 关于 hf-mirror

- 对境外 IP，hf-mirror 会把请求 308 重定向到 huggingface.co，此时应直接使用官方 endpoint（`init --hf-endpoint auto` 会自动处理）
- 对境内 IP，私有仓库的鉴权、列表、下载、上传均可通过镜像完成。上传需使用 **xet** 协议（huggingface_hub ≥ 0.32 默认启用）
- 若回退到普通 LFS 分块上传，镜像返回的 `complete_multipart` 地址指向无法解析的 `hf-mirror.org`；对于基于 requests 的 huggingface_hub 版本，本工具会自动修正

## 自动接收（systemd --user）

```bash
cp contrib/relay-watch.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now relay-watch
loginctl enable-linger $USER      # 注销后继续运行
```

## 已知限制

- ModelScope 后端尚未经过完整测试；ModelScope 不支持压平历史，`gc` 只做删除
- 请遵守 HuggingFace / ModelScope 的服务条款，中转仓库适合临时传输，不建议长期存放大量数据
