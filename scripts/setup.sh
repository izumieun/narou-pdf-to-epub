#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf 'Usage: bash scripts/setup.sh [--python /path/to/python3]\n' >&2
}

if (( $# == 0 )); then
    python_cmd=python3
elif (( $# == 2 )) && [[ $1 == --python && -n $2 ]]; then
    python_cmd=$2
else
    usage
    exit 2
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
project_root=$(cd -- "$script_dir/.." && pwd -P)
venv_dir="$project_root/.venv-linux"
venv_python="$venv_dir/bin/python"
requirements="$project_root/requirements.txt"

if [[ ! -f $requirements ]]; then
    printf 'Requirements file not found: %s\n' "$requirements" >&2
    exit 1
fi

if [[ ! -e $venv_dir ]]; then
    if ! "$python_cmd" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then
        printf 'Python 3.12 or newer is required: %s\n' "$python_cmd" >&2
        exit 1
    fi
    if ! "$python_cmd" -m venv "$venv_dir"; then
        printf 'Could not create %s. Check that the Python venv module is installed.\n' "$venv_dir" >&2
        exit 1
    fi
fi

if [[ ! -x $venv_python ]]; then
    printf '%s exists but has no Linux Python executable. Inspect it before retrying.\n' "$venv_dir" >&2
    exit 1
fi
if ! "$venv_python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then
    printf 'Existing virtual environment requires Python 3.12 or newer.\n' >&2
    exit 1
fi
if ! "$venv_python" -m pip install --disable-pip-version-check -r "$requirements"; then
    printf 'Dependency installation failed. Rerun setup after resolving the error.\n' >&2
    exit 1
fi
if ! "$venv_python" -m pip check; then
    printf 'Dependency consistency check failed.\n' >&2
    exit 1
fi

printf 'Ready: %s\n' "$venv_python"
printf 'Activation is optional; invoke .venv-linux/bin/python directly.\n'
