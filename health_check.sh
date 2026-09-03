#!/bin/bash
# health_check.sh
# 鵝鵝bot（luna-bot）健康檢查的 cron 包裝腳本
# 用法：cd /root/luna_bot && ./health_check.sh
# 建議 cron：每天早上跑一次，例如 0 9 * * * cd /root/luna_bot && ./health_check.sh >> /var/log/luna-bot-health.log 2>&1

cd "$(dirname "$0")" || exit 1
python3 health_check.py
