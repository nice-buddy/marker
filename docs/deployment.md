# Marker 部署指南（Windows / macOS / Ubuntu）

本文档说明如何把当前版本的 Marker 部署到 Windows 11、macOS 和 Ubuntu，并配置 CLI、WebUI、图片提取和 VLM 推理后端。

## 1. 部署后的行为

当前版本与本仓库的部署结果约定如下：

- CLI 默认输出目录是应用目录下的 `outputs`，即 `.venv` 的同级目录；每个输入文件使用文件名（不含扩展名）创建子目录。
- Markdown 中的图片使用 `images/...` 相对路径。
- WebUI 转换时会逐页写入输出目录；下载按钮生成 `.zip`，zip 根目录包含 Markdown 和 `images/`。
- 显式传入 `--output_dir` 时，以该目录为准。
- WebUI 使用时间戳任务名；同一文件重新上传会创建新任务，不覆盖旧结果。

示例：

```text
outputs/
└── 科学推理PDF80页/
    ├── 科学推理PDF80页.md
    ├── 科学推理PDF80页_meta.json
    └── images/
        ├── _page_0_Diagram_12.jpeg
        └── ...
```

Markdown 内容示例：

```markdown
![](images/_page_0_Diagram_12.jpeg)
```

### WebUI 任务、历史与断点续传

WebUI 的上传和转换采用持久化任务队列：

- 上传文件保存到应用目录的 `inputs/`，文件名为 `<原文件名主名>_<yyyyMMddHHmmssSSS><扩展名>`。
- 任务名称为 `<yyyyMMddHHmmssSSS>_<原文件名>`，输出目录为 `outputs/<任务名称>/`。
- 同一文件重新上传会创建新任务和新时间戳，不会覆盖旧结果。
- 历史记录保存在 `history/marker_history.sqlite3`。
- 每个已完成的页面结果保存在 `history/pages/<task_id>/`，用于恢复 Preview。
- WebUI 一次只运行一个转换任务；其他上传任务按创建时间排队，前一个完成后自动开始下一个。
- 运行中或排队中的任务可以终止；运行中任务会在当前页完成后停止，已完成页面会保留。
- 排队中的任务可以在历史记录列表中拖动调整顺序，顺序持久化到 SQLite。
- 尚未开始的排队任务可以单独修改转换模式、OCR 选项、DPI、最大页数和页码范围，修改后保存到任务参数快照。
- 主进度条显示已用时间和预计剩余时间，历史列表同步显示各任务的进度与耗时。
- 非运行中的任务可以删除；删除会同时移除历史记录、输入文件、输出目录和页面快照。
- 应用重启后，运行中的任务会恢复到队列，并从最后一个已完成页继续。
- 页面结果按分页查询显示，默认每页 10 页，避免一次性把所有页面加载进浏览器内存。
- `inputs/`、`outputs/` 和 `history/` 都应保留在应用目录中；不要执行会清理忽略目录的 `git clean -fdx`。

## 2. 系统要求

| 项目 | 建议 |
| --- | --- |
| Python | 3.12（支持 3.10–3.12） |
| 虚拟环境 | 放在应用目录内的 `.venv` |
| 磁盘 | 至少预留 5GB；模型缓存约 2GB，vLLM Docker 镜像另计 |
| 网络 | 首次运行需要下载模型 |
| 内存 | 建议 8GB 以上；CPU 运行 VLM 时建议 16GB 以上 |

注意：当前代码通过 `.venv` 的位置推导默认输出目录。如果把 `.venv` 建在应用目录之外，输出目录也会跟着 `.venv` 走。此时请显式使用 `--output_dir`。

## 3. 推理模式与后端

`--mode vllm` 表示“整页 VLM OCR”，并不强制使用 vLLM 作为后端。Marker 通过 Surya 自动启动推理服务：

| 平台 / 硬件 | 推荐后端 | 需要安装 |
| --- | --- | --- |
| macOS（Apple Silicon / Intel） | llama.cpp | `brew install llama.cpp` |
| Windows 11（CPU 或 NVIDIA） | llama.cpp | llama.cpp Windows release |
| Ubuntu + NVIDIA GPU + Docker | vLLM | Docker + NVIDIA Container Toolkit |
| Ubuntu（CPU 或无 Docker） | llama.cpp | llama.cpp（CPU/CUDA 构建） |

常用模式：

| 模式 | 是否需要 VLM 服务 | 适用场景 |
| --- | --- | --- |
| `--mode no_ocr` | 否 | 纯文本层 PDF，速度最快 |
| `--mode fast` | 否 | CPU 快速转换 |
| `--mode balanced` | 是 | GPU 混合路由，复杂页面走 VLM |
| `--mode vllm` | 是 | 每页整页 VLM OCR；图片提取建议用此模式 |

如果需要稳定提取 PDF 中的图片，建议使用 `--mode vllm`。

## 4. 获取代码

使用包含本文档和部署改动分支的仓库：

```bash
git clone <repository-url> marker-md
cd marker-md
git checkout master
git log -1 --oneline
```

也可以直接复制现有项目目录，但必须排除虚拟环境和输出：

```bash
rsync -av \
  --exclude '.venv' \
  --exclude 'outputs' \
  --exclude '__pycache__' \
  /path/to/marker/ user@target:~/App/marker-md/
```

不要复制 `.venv`、`outputs`、`__pycache__` 到不同操作系统。

## 5. 通用安装步骤

以下命令适用于 macOS/Linux；Windows 使用后面的 PowerShell 版本。

```bash
cd ~/App/marker-md

uv venv --python 3.12 .venv
source .venv/bin/activate

uv pip install -e ".[full]" \
  "httpx[socks]" \
  "httpcore[socks]" \
  uvicorn \
  fastapi \
  python-multipart
```

只处理 PDF 时，可以去掉 `[full]`，减少 Office/HTML/EPUB 相关系统依赖：

```bash
uv pip install -e . \
  "httpx[socks]" \
  "httpcore[socks]" \
  uvicorn \
  fastapi \
  python-multipart
```

需要处理 DOCX、PPTX、XLSX、HTML、EPUB 时保留 `[full]`。其中 WeasyPrint 在 Windows/Ubuntu 上可能需要额外的系统库。

## 6. macOS

### 6.1 安装依赖

```bash
brew install uv llama.cpp
```

如果未安装 Homebrew，先从 `https://brew.sh` 安装。

### 6.2 创建环境并安装 Marker

```bash
cd ~/App/marker-md

uv venv --python 3.12 .venv
source .venv/bin/activate

uv pip install -e ".[full]" \
  "httpx[socks]" \
  "httpcore[socks]" \
  uvicorn \
  fastapi \
  python-multipart
```

### 6.3 配置 llama.cpp

Homebrew 安装后通常可以直接找到：

```bash
export PATH="/opt/homebrew/bin:$PATH"
export SURYA_INFERENCE_BACKEND=llamacpp
```

Intel Mac 的 Homebrew 前缀通常是 `/usr/local/bin`，请把下文的 `/opt/homebrew/bin` 替换为 `/usr/local/bin`。

如果使用非交互式 SSH 或服务方式启动，显式设置：

```bash
export LLAMA_CPP_BINARY="/opt/homebrew/bin/llama-server"
```

### 6.4 启动 CLI

```bash
cd ~/App/marker-md
source .venv/bin/activate
export PATH="/opt/homebrew/bin:$PATH"

marker_single "/Users/you/Downloads/科学推理PDF80页.pdf" \
  --mode vllm \
  --page_range 0,2
```

### 6.5 启动 WebUI

```bash
cd ~/App/marker-md
source .venv/bin/activate
export PATH="/opt/homebrew/bin:$PATH"

marker_gui --host 0.0.0.0 --port 7861 --mode vllm
```

访问 `http://<Mac-IP>:7861`。如果只在本机访问，建议使用 `--host 127.0.0.1`。

### 6.6 macOS 常驻服务（可选）

创建 `~/Library/LaunchAgents/local.marker.gui.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>local.marker.gui</string>
  <key>WorkingDirectory</key>
  <string>/Users/you/App/marker-md</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/you/App/marker-md/.venv/bin/marker_gui</string>
    <string>--host</string>
    <string>0.0.0.0</string>
    <string>--port</string>
    <string>7861</string>
    <string>--mode</string>
    <string>vllm</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>SURYA_INFERENCE_BACKEND</key>
    <string>llamacpp</string>
    <key>LLAMA_CPP_BINARY</key>
    <string>/opt/homebrew/bin/llama-server</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/marker-gui.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/marker-gui.err.log</string>
</dict>
</plist>
```

加载服务：

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.marker.gui.plist
launchctl kickstart -k gui/$(id -u)/local.marker.gui
```

停止服务：

```bash
launchctl bootout gui/$(id -u)/local.marker.gui
```

## 7. Windows 11

### 7.1 安装 uv

在 PowerShell 中执行：

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

安装后重新打开 PowerShell，使 `uv` 进入 PATH。

### 7.2 创建环境并安装 Marker

建议把项目放在 `C:\Apps\marker-md`，避免 `C:\Program Files` 的权限问题。

```powershell
cd C:\Apps\marker-md

uv venv --python 3.12 .venv
.\.venv\Scripts\Activate.ps1

uv pip install -e ".[full]" `
  "httpx[socks]" `
  "httpcore[socks]" `
  uvicorn `
  fastapi `
  python-multipart
```

如果 PowerShell 禁止激活脚本：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

也可以不激活，直接使用：

```powershell
.\.venv\Scripts\marker_single.exe --help
.\.venv\Scripts\marker_gui.exe --help
```

### 7.3 安装 llama.cpp

1. 从 `https://github.com/ggml-org/llama.cpp/releases` 下载 Windows x64 release。
2. NVIDIA GPU 使用 CUDA 版本；无 GPU 使用 CPU 版本。
3. 解压到例如 `C:\Apps\llama.cpp`。
4. 保留压缩包内全部 DLL，不要只复制 `llama-server.exe`。

设置环境变量：

```powershell
$env:SURYA_INFERENCE_BACKEND = "llamacpp"
$env:LLAMA_CPP_BINARY = "C:\Apps\llama.cpp\llama-server.exe"

# CPU 版本建议设置为 0；NVIDIA CUDA 版本可保持默认 99
$env:LLAMA_CPP_NGL = "0"
```

重要：Windows 上即使检测到 NVIDIA 驱动，也建议显式设置 `SURYA_INFERENCE_BACKEND=llamacpp`。否则 Surya 可能自动选择 vLLM 后端并尝试 Docker，而 Windows 原生 Docker vLLM 配置更复杂。

### 7.4 启动 CLI

```powershell
cd C:\Apps\marker-md
.\.venv\Scripts\Activate.ps1

$env:SURYA_INFERENCE_BACKEND = "llamacpp"
$env:LLAMA_CPP_BINARY = "C:\Apps\llama.cpp\llama-server.exe"

marker_single "C:\Users\you\Downloads\科学推理PDF80页.pdf" `
  --mode vllm `
  --page_range 0,2
```

### 7.5 启动 WebUI

```powershell
cd C:\Apps\marker-md
.\.venv\Scripts\Activate.ps1

$env:SURYA_INFERENCE_BACKEND = "llamacpp"
$env:LLAMA_CPP_BINARY = "C:\Apps\llama.cpp\llama-server.exe"

marker_gui --host 0.0.0.0 --port 7861 --mode vllm
```

访问 `http://localhost:7861` 或 `http://<Windows-IP>:7861`。

### 7.6 Windows 防火墙

首次启动时允许 Python 通过防火墙，或手动添加规则：

```powershell
New-NetFirewallRule `
  -DisplayName "Marker GUI 7861" `
  -Direction Inbound `
  -Protocol TCP `
  -LocalPort 7861 `
  -Action Allow
```

### 7.7 Windows 上的 WeasyPrint

只处理 PDF 时不需要 WeasyPrint。需要 DOCX/PPTX/XLSX/HTML/EPUB 时，`[full]` 会安装 WeasyPrint；Windows 上它可能需要 GTK3 运行时。若遇到 `weasyprint` 导入或 DLL 错误：

- 只做 PDF 转换：改用不带 `[full]` 的安装命令。
- 需要 Office/HTML/EPUB：安装 GTK3 运行库并加入 PATH，或改用 WSL2/Ubuntu 部署。

### 7.8 Windows 常驻服务（可选）

可以用任务计划程序设置“登录时运行”，或使用 NSSM 把 `.venv\Scripts\marker_gui.exe` 注册为 Windows 服务。服务环境中必须显式设置：

```text
SURYA_INFERENCE_BACKEND=llamacpp
LLAMA_CPP_BINARY=C:\Apps\llama.cpp\llama-server.exe
```

## 8. Ubuntu

### 8.1 安装 uv

```bash
sudo apt update
sudo apt install -y curl

curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
```

### 8.2 创建环境并安装 Marker

```bash
cd ~/App/marker-md

uv venv --python 3.12 .venv
source .venv/bin/activate

uv pip install -e ".[full]" \
  "httpx[socks]" \
  "httpcore[socks]" \
  uvicorn \
  fastapi \
  python-multipart
```

需要 Office/HTML/EPUB 时安装 WeasyPrint 系统库：

```bash
sudo apt install -y \
  libpango-1.0-0 \
  libpangoft2-1.0-0 \
  libgdk-pixbuf-2.0-0
```

### 8.3 方案 A：NVIDIA GPU + Docker vLLM

安装 Docker：

```bash
sudo apt install -y docker.io
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
```

执行 `usermod` 后需要重新登录，使 Docker 组权限生效。

再按 NVIDIA 官方文档安装 NVIDIA Container Toolkit：

`https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html`

验证：

```bash
nvidia-smi
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
```

设置后端：

```bash
export SURYA_INFERENCE_BACKEND=vllm
export VLLM_GPU_TYPE=a100-80
export VLLM_GPUS=0
```

`VLLM_GPU_TYPE` 必须与实际 GPU 匹配，常用值：

默认值是 `4090`。使用其他 GPU 时必须显式设置，否则显存和并发参数会按 4090 计算。

| GPU | 值 |
| --- | --- |
| RTX 4090 | `4090` |
| RTX 3090 | `3090` |
| A100 40GB | `a100-40` |
| A100 80GB | `a100-80` |
| H100 | `h100` |
| L4 | `l4` |
| L40S | `l40s` |
| A10 | `a10` |
| T4 | `t4` |

首次运行会拉取 `vllm/vllm-openai:v0.25.1` 镜像，需要较大磁盘空间。

### 8.4 方案 B：llama.cpp

没有 Docker、没有 NVIDIA GPU，或希望使用 CUDA 版 llama.cpp 时使用此方案。

安装编译依赖：

```bash
sudo apt install -y build-essential cmake git
```

编译 CUDA 版本：

```bash
git clone https://github.com/ggml-org/llama.cpp
cmake -S llama.cpp -B llama.cpp/build -DGGML_CUDA=ON
cmake --build llama.cpp/build --config Release -j
```

纯 CPU 版本去掉 `-DGGML_CUDA=ON`：

```bash
cmake -S llama.cpp -B llama.cpp/build
cmake --build llama.cpp/build --config Release -j
```

设置环境变量：

```bash
export SURYA_INFERENCE_BACKEND=llamacpp
export LLAMA_CPP_BINARY="$HOME/llama.cpp/build/bin/llama-server"

# 无 GPU 时建议设置为 0；CUDA 版本保持默认 99
export LLAMA_CPP_NGL=0
```

如果使用 llama.cpp 官方预编译 release，把 `LLAMA_CPP_BINARY` 指向解压目录中的 `llama-server`。

### 8.5 启动 CLI

```bash
cd ~/App/marker-md
source .venv/bin/activate

# 按实际后端二选一
export SURYA_INFERENCE_BACKEND=vllm
export VLLM_GPU_TYPE=a100-80
export VLLM_GPUS=0

# 或
# export SURYA_INFERENCE_BACKEND=llamacpp
# export LLAMA_CPP_BINARY="$HOME/llama.cpp/build/bin/llama-server"

marker_single "/home/you/Downloads/科学推理PDF80页.pdf" \
  --mode vllm \
  --page_range 0,2
```

### 8.6 启动 WebUI

```bash
cd ~/App/marker-md
source .venv/bin/activate

export SURYA_INFERENCE_BACKEND=vllm
export VLLM_GPU_TYPE=a100-80
export VLLM_GPUS=0

marker_gui --host 0.0.0.0 --port 7861 --mode vllm
```

开放防火墙：

```bash
sudo ufw allow 7861/tcp
```

访问 `http://<Ubuntu-IP>:7861`。

### 8.7 systemd 服务（可选）

创建 `/etc/systemd/system/marker-gui.service`：

```ini
[Unit]
Description=Marker WebUI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=marker
WorkingDirectory=/home/marker/App/marker-md
Environment=PATH=/home/marker/App/marker-md/.venv/bin:/usr/local/bin:/usr/bin:/bin
Environment=SURYA_INFERENCE_BACKEND=vllm
Environment=VLLM_GPU_TYPE=a100-80
Environment=VLLM_GPUS=0
ExecStart=/home/marker/App/marker-md/.venv/bin/marker_gui --host 0.0.0.0 --port 7861 --mode vllm
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

如果使用 llama.cpp，把环境变量替换为：

```ini
Environment=SURYA_INFERENCE_BACKEND=llamacpp
Environment=LLAMA_CPP_BINARY=/home/marker/llama.cpp/build/bin/llama-server
```

启用并启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now marker-gui
sudo systemctl status marker-gui
```

查看日志：

```bash
journalctl -u marker-gui -f
```

## 9. 环境变量参考

| 变量 | 说明 | 示例 |
| --- | --- | --- |
| `SURYA_INFERENCE_BACKEND` | 推理后端 | `vllm` / `llamacpp` |
| `SURYA_INFERENCE_URL` | 连接已有推理服务 | `http://127.0.0.1:8000/v1` |
| `SURYA_INFERENCE_PARALLEL` | 客户端并发数 | `4` |
| `SURYA_INFERENCE_KEEP_ALIVE` | 进程退出后保留自动启动的服务 | `1` |
| `LLAMA_CPP_BINARY` | llama-server 路径 | `/opt/homebrew/bin/llama-server` |
| `LLAMA_CPP_NGL` | llama.cpp GPU 层数 | `99`（GPU）/ `0`（CPU） |
| `VLLM_GPU_TYPE` | vLLM 显存配置档位 | `4090` / `a100-80` |
| `VLLM_GPUS` | vLLM 使用的 GPU | `0` / `0,1` |
| `VLLM_DTYPE` | vLLM 数据类型 | `bfloat16` |
| `DOCKER_HF_CACHE_PATH` | Docker 挂载的 HF 缓存 | `~/.cache/huggingface` |
| `HF_ENDPOINT` | HuggingFace 镜像/代理地址 | 按你的网络环境设置 |
| `HF_HOME` | HuggingFace 缓存根目录 | `/data/hf-cache` |
| `INDRA_OCR_MODEL_DIR` | Indra OCR 模型目录 | `/data/indra_ocr` |
| `HTTPS_PROXY` / `HTTP_PROXY` | 下载模型时的代理 | `http://127.0.0.1:7890` |

`export`（macOS/Linux）和 `$env:`（Windows）只对当前终端会话生效。生产环境请把需要的变量写入启动脚本、launchd、systemd 或 Windows 服务配置。

## 10. 首次运行、模型缓存与离线部署

首次运行会下载模型、字体和推理权重。MacMini 实测缓存大致为：

| 缓存 | 大小 |
| --- | --- |
| `~/.cache/huggingface` | 约 1.4GB |
| `~/.cache/indra_ocr` | 约 92MB |
| `~/.cache/datalab` | 约 18MB |

Windows 对应路径通常在：

```text
%USERPROFILE%\.cache\huggingface
%USERPROFILE%\.cache\indra_ocr
%USERPROFILE%\.cache\datalab
```

网络受限时可以：

1. 配置 `HTTPS_PROXY` / `HTTP_PROXY`。
2. 使用可访问的 HuggingFace 镜像，例如设置 `HF_ENDPOINT`。
3. 从已经部署好的机器复制缓存目录，再用 `HF_HOME`、`INDRA_OCR_MODEL_DIR` 指向复制后的位置。

使用 Ubuntu Docker vLLM 时，Surya 会把 `DOCKER_HF_CACHE_PATH`（默认 `~/.cache/huggingface`）挂载到容器内。

## 11. 验证部署

### 11.1 检查输出目录

```bash
cd ~/App/marker-md
source .venv/bin/activate

python -c "from marker.settings import settings; print(settings.OUTPUT_DIR)"
```

预期输出类似：

```text
/Users/you/App/marker-md/outputs
```

Windows：

```powershell
cd C:\Apps\marker-md
.\.venv\Scripts\Activate.ps1

python -c "from marker.settings import settings; print(settings.OUTPUT_DIR)"
```

### 11.2 CLI 转换测试

使用包含图片的 PDF，只转换少量页面：

```bash
marker_single "/path/to/科学推理PDF80页.pdf" \
  --mode vllm \
  --page_range 0,2
```

检查：

```bash
find outputs/科学推理PDF80页 -maxdepth 3 -type f
grep -c '](images/' outputs/科学推理PDF80页/科学推理PDF80页.md
```

预期 Markdown 中的图片引用数量与 `images/` 中的图片数量一致。

Windows：

```powershell
marker_single "C:\Users\you\Downloads\科学推理PDF80页.pdf" `
  --mode vllm `
  --page_range 0,2

Get-ChildItem -Recurse C:\Apps\marker-md\outputs\科学推理PDF80页
Select-String -Path C:\Apps\marker-md\outputs\科学推理PDF80页\科学推理PDF80页.md `
  -Pattern '\]\(images/'
```

### 11.3 WebUI 下载测试

1. 启动 `marker_gui`。
2. 上传包含图片的 PDF。
3. 页面范围填写 `0,2`。
4. 模式选择 `vllm`。
5. 点击 `Download Markdown (.zip)`。
6. 解压后检查：

```text
<文件名>.md
images/
```

Linux/macOS 检查：

```bash
unzip -l ~/Downloads/科学推理PDF80页.zip
```

Windows 检查：

```powershell
Expand-Archive .\科学推理PDF80页.zip -DestinationPath .\marker-zip-check
Get-ChildItem -Recurse .\marker-zip-check
```

### 11.4 单元测试

这些测试不需要下载数据集或模型：

```bash
uv pip install pytest pytest-mock

python -m pytest -q \
  tests/renderers/test_output_save.py \
  tests/test_viewer_outputs.py \
  tests/config/test_config.py
```

预期：

```text
11 passed
```

## 12. 常见问题

### 12.1 `marker_single: command not found`

确认虚拟环境已激活：

```bash
source .venv/bin/activate
which marker_single
```

Windows:

```powershell
.\.venv\Scripts\Activate.ps1
Get-Command marker_single
```

也可以直接使用 `.venv/bin/marker_single` 或 `.venv\Scripts\marker_single.exe`。

### 12.2 `llama-server binary not found`

检查：

```bash
which llama-server
echo "$LLAMA_CPP_BINARY"
```

Windows：

```powershell
Get-Command llama-server
$env:LLAMA_CPP_BINARY
```

如果二进制不在 PATH，显式设置 `LLAMA_CPP_BINARY` 的完整路径。

### 12.3 Windows 有 NVIDIA 显卡但启动失败

Windows 原生环境建议强制 llama.cpp：

```powershell
$env:SURYA_INFERENCE_BACKEND = "llamacpp"
$env:LLAMA_CPP_BINARY = "C:\Apps\llama.cpp\llama-server.exe"
```

不要在 Windows 原生环境依赖自动检测去启动 Docker vLLM。需要使用 vLLM 时，建议在 WSL2/Ubuntu 中部署。

### 12.4 Windows 报 `module 'os' has no attribute 'WNOHANG'`

这是 Surya 0.24 的 llama.cpp 进程存活检查使用了 POSIX 的 `os.waitpid(..., os.WNOHANG)`。当前 Marker 代码已经包含 Windows 兼容层：

```text
marker/utils/surya_compat.py
```

它会在 Windows 上用 `psutil` 判断和停止 llama-server，不再调用 `os.waitpid`、`os.WNOHANG` 或 `os.kill(pid, 0)`。

更新到最新提交后重新安装并重启：

```powershell
cd C:\App\AI\marker
git pull
.\.venv\Scripts\Activate.ps1
uv pip install -e ".[full]" "httpx[socks]" "httpcore[socks]" uvicorn fastapi python-multipart
marker_gui --host 0.0.0.0 --port 7861 --mode vllm
```

如果 Windows 上不是 Git 工作副本，需要至少把以下两个文件同步过去：

```text
marker/models.py
marker/utils/surya_compat.py
```

然后重启 `marker_gui`。确认当前代码包含兼容层：

```powershell
Select-String -Path .\marker\models.py -Pattern "apply_surya_windows_compat"
Test-Path .\marker\utils\surya_compat.py
```

也可以运行 Windows 专用兼容性测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\utils\test_surya_compat.py
```

在 Windows 上应看到 `2 passed`。

### 12.5 Ubuntu vLLM 启动失败

依次检查：

```bash
nvidia-smi
docker info
echo "$VLLM_GPU_TYPE"
```

常见原因：

- Docker 服务未启动。
- 当前用户不在 `docker` 组。
- 未安装 NVIDIA Container Toolkit。
- `VLLM_GPU_TYPE` 与实际 GPU 不匹配。

### 12.6 输出目录不在应用目录下

默认输出目录由 `.venv` 位置决定。确认：

```bash
python -c "import sys; print(sys.prefix)"
python -c "from marker.settings import settings; print(settings.OUTPUT_DIR)"
```

如果 `.venv` 不在应用目录中，使用：

```bash
marker_single input.pdf --output_dir /path/to/marker-md/outputs
```

### 12.7 WebUI 无法访问

检查：

- 是否使用了 `--host 0.0.0.0`。
- Windows 防火墙或 Ubuntu `ufw` 是否放行 7861 端口。
- 端口是否被占用。
- 是否访问了正确的主机 IP。

### 12.8 `weasyprint` / GTK / Pango 报错

只处理 PDF 时使用不带 `[full]` 的安装命令。需要 Office/HTML/EPUB 时：

- Ubuntu：安装 `libpango-1.0-0`、`libpangoft2-1.0-0`、`libgdk-pixbuf-2.0-0`。
- Windows：安装 GTK3 运行库并加入 PATH，或改用 WSL2/Ubuntu。

### 12.9 日志出现 `Layout inference failed ... full-page OCR will rebuild it`

这表示该页的 layout 输出解析失败，但整页 OCR 会继续重建文本。该页的图片块可能无法提取。可以：

- 重试该页。
- 确认 PDF 本身是否有有意义的图片。
- 对图片要求高的任务使用 `--mode vllm`。
- 如果频繁出现，尝试把 PDF 拆分后转换。

### 12.10 ZIP 中没有图片

依次检查：

1. PDF 是否实际包含图片。
2. 是否使用了 `--mode vllm`。
3. 服务端 `outputs/<文件名>/images/` 是否有文件。
4. 日志是否有 layout 失败。

### 12.11 Windows 均衡模式报 `'gbk' codec can't decode`

Windows 默认使用 GBK 读取未显式指定编码的文本文件。Indra 的 `math_meta.json` 是 UTF-8，
在 `balanced` 模式下可能触发：

```text
'gbk' codec can't decode byte 0x93 in position 7779: illegal multibyte sequence
```

当前代码已在 `marker/utils/surya_compat.py` 中为 Windows 增加 UTF-8 默认读取补丁，会在
Marker 加载时自动应用。更新到最新提交并重启即可，不需要重装依赖：

```powershell
cd C:\App\AI\marker
git pull
```

确认补丁存在：

```powershell
Select-String -Path .\marker\utils\surya_compat.py -Pattern "apply_windows_utf8_compat"
```

## 13. 升级与回滚

使用源码和 editable 安装时：

```bash
cd ~/App/marker-md
git pull
source .venv/bin/activate
uv pip install -e ".[full]" \
  "httpx[socks]" \
  "httpcore[socks]" \
  uvicorn \
  fastapi \
  python-multipart
```

重启 `marker_gui` 或 systemd 服务。`outputs` 目录不会被覆盖或删除。

回滚：

```bash
git checkout <previous-commit-or-tag>
uv pip install -e ".[full]" \
  "httpx[socks]" \
  "httpcore[socks]" \
  uvicorn \
  fastapi \
  python-multipart
```

## 14. 安全与生产建议

- `marker_gui` 没有内置登录认证，不要直接暴露到公网。
- 局域网使用也建议通过防火墙限制来源 IP。
- 需要公网访问时，放在 Nginx/Caddy 反向代理后，并启用 HTTPS 和认证。
- 例如 Nginx 反向代理：

```nginx
server {
    listen 443 ssl;
    server_name marker.example.com;

    client_max_body_size 200m;

    location / {
        proxy_pass http://127.0.0.1:7861;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

- 将应用目录和 `outputs` 放在有足够权限和磁盘空间的用户目录，不要放在系统的只读或受限目录。
- 不要把 `.venv`、模型缓存和输出目录提交到 Git。
