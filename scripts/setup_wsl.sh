#!/usr/bin/env bash
# Ubuntu 24.04 / WSL2 x86_64；不安装 Linux GPU 驱动，不覆盖私有配置。
set -euo pipefail
if [[ ${1:-} == --help ]]; then
    echo '用法：bash scripts/setup_wsl.sh [--skip-model]'
    echo '默认安装 Python/Node/ffmpeg/浏览器并准备 SigLIP2；--skip-model 仅跳过模型下载和 GPU 验收。'
    exit 0
fi
skip_model=false
if [[ ${1:-} == --skip-model && $# == 1 ]]; then
    skip_model=true
elif [[ $# != 0 ]]; then
    echo '未知参数；请使用 --help' >&2; exit 2
fi
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$repo"
[[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] || { echo '需要 Linux x86_64（推荐 WSL2 Ubuntu 24.04）'; exit 1; }
source /etc/os-release
[[ $ID == ubuntu && $VERSION_ID == 24.04 ]] || { echo '当前安装入口仅支持 Ubuntu 24.04'; exit 1; }
[[ $repo != /mnt/* ]] || { echo '请先把仓库 clone 到 WSL 的 ~/projects 下'; exit 1; }
[[ $EUID != 0 ]] || { echo '请使用普通 WSL 用户运行，脚本按需调用 sudo'; exit 1; }
if ! $skip_model; then
    "$(command -v nvidia-smi || echo /usr/lib/wsl/lib/nvidia-smi)" --query-gpu=name --format=csv,noheader
fi
docker info >/dev/null || { echo '请启动 Docker Desktop 并启用当前 Ubuntu 的 WSL integration'; exit 1; }
docker compose version
sudo apt-get update
sudo apt-get install -y ca-certificates curl git build-essential

conda_exe=${CONDA_EXE:-}
if [[ ! -x $conda_exe ]]; then
    conda_exe=$(command -v conda || true)
fi
if [[ ! -x $conda_exe ]]; then
    for prefix in "$HOME/miniconda3" "$HOME/miniforge3" "$HOME/anaconda3"; do
        if [[ -x $prefix/bin/conda ]]; then conda_exe=$prefix/bin/conda; break; fi
    done
fi
if [[ ! -x $conda_exe ]]; then
    installer=$(mktemp)
    trap 'rm -f "$installer"' EXIT
    curl --fail --location --retry 3 -o "$installer" \
        https://github.com/conda-forge/miniforge/releases/download/26.7.2-0/Miniforge3-Linux-x86_64.sh
    echo "281b0ac7d550802efc81af633225a5e6116d29ae72f3ab4eae7168c3931a4c05  $installer" | sha256sum --check
    bash "$installer" -b -p "$HOME/miniforge3"
    conda_exe=$HOME/miniforge3/bin/conda
fi
# 只在环境不存在时 create；现有环境保留原路径。
if "$conda_exe" run -n hello_agent python -c 'import sys' >/dev/null 2>&1; then
    "$conda_exe" install -y -n hello_agent --override-channels -c conda-forge 'python=3.12' 'nodejs=22' 'ffmpeg=7' pip
else
    "$conda_exe" create -y -n hello_agent --override-channels -c conda-forge 'python=3.12' 'nodejs=22' 'ffmpeg=7' pip
fi
python_exe=$("$conda_exe" run -n hello_agent python -c 'import sys; print(sys.executable)')
export PATH="$(dirname "$python_exe"):$PATH"
export HF_HOME="$repo/.data/models/huggingface"
"$python_exe" -m pip install -c config/constraints-wsl.txt -e '.[agent-server-dev,local-vision-embedding,google-workspace-mcp]'
"$python_exe" scripts/setup_local.py init
umask 077
printf '%s\n' "$python_exe" > .local/python.path
workspace=$HOME/assistant_cqy/studio
printf '%s\n' "$workspace" > .local/workspace.path
"$python_exe" -m assistant_agent.client --install-browser --cwd "$workspace"
node "$workspace/.agent/browser-runtime/node_modules/playwright/cli.js" install-deps chromium

if ! $skip_model; then
    # torch/transformers 只用于资产准备；Runtime 自身不导入它们。
    "$python_exe" -m pip install -c config/constraints-wsl.txt 'torch==2.8.0' 'transformers==4.57.6' 'onnxscript==0.7.1' sentencepiece
    model_dir=.data/models/siglip2-base-patch16-224
    if [[ ! -e $model_dir ]]; then
        "$python_exe" scripts/export_siglip2_embedding_onnx.py \
            --revision 75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2 --output-dir "$model_dir"
    fi
    "$python_exe" scripts/setup_local.py gpu --model-dir "$model_dir"
fi
"$python_exe" -m pip check
"$python_exe" scripts/setup_local.py check
docker compose -f docker/mem0/compose.yaml --profile mem0 pull qdrant
docker compose -f docker/mem0/compose.yaml --profile mem0 build mem0
echo '安装完成。BGE 未安装；真实 Provider 未调用。'
echo '填写 .env 后：bash scripts/start.sh memory（需要 Mem0 时），bash scripts/start.sh server'
echo '保持默认 mock 也可启动。另一个终端：bash scripts/start.sh client'
