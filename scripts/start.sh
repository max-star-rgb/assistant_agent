#!/usr/bin/env bash
set -euo pipefail
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$repo"
if [[ ${1:-} == --help ]]; then
    echo '用法：bash scripts/start.sh [server|client|memory|check|gpu] [附加参数]'; exit 0
fi
[[ -f .local/python.path ]] || { echo '请先运行 bash scripts/setup_wsl.sh'; exit 1; }
IFS= read -r python_exe < .local/python.path
[[ -x $python_exe ]] || { echo '安装环境已移动，请重新运行 setup_wsl.sh'; exit 1; }
IFS= read -r workspace < .local/workspace.path
export PATH="$(dirname "$python_exe"):$PATH"
export HF_HOME="$repo/.data/models/huggingface"
export PLAYWRIGHT_BROWSERS_PATH="$workspace/.agent/browser-runtime/browsers"
action=${1:-server}
if [[ $# -gt 0 ]]; then shift; fi
case "$action" in
    server)
        "$python_exe" scripts/setup_local.py init
        exec "$python_exe" scripts/run_server.py --no-reload "$@" ;;
    client) exec "$python_exe" -m assistant_agent.client --cwd "$workspace" --browser-headless "$@" ;;
    memory) exec docker compose --env-file .env -f docker/mem0/compose.yaml --profile mem0 up -d --wait "$@" ;;
    check|gpu) exec "$python_exe" scripts/setup_local.py "$action" "$@" ;;
    *) echo '未知操作，请使用 --help' >&2; exit 2 ;;
esac
