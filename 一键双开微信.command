#!/bin/zsh

set -u

SCRIPT_DIR="${0:A:h}"
exec /usr/bin/python3 "${SCRIPT_DIR}/wechat_multi.py" start --extra 1
