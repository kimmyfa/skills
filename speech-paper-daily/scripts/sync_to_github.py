#!/usr/bin/env python3
"""GitHub Sync Script - 提交 papers/ 目录到 GitHub"""

import os
import sys
import subprocess
from datetime import datetime

PROJECT_DIR = "/Users/kimmy/Desktop/Vagent_app/SpeechAIResercher"
PAPERS_DIR = os.path.join(PROJECT_DIR, "papers")

def get_latest_date():
    dirs = [d for d in os.listdir(PAPERS_DIR) if os.path.isdir(os.path.join(PAPERS_DIR, d))]
    if not dirs:
        return None
    return sorted(dirs, reverse=True)[0]

def sync():
    date_str = get_latest_date()
    if not date_str:
        print("⚠️ 未找到论文目录")
        return False

    os.chdir(PROJECT_DIR)

    subprocess.run(["git", "config", "user.email", "speech-paper-daily@bot.local"], check=False)
    subprocess.run(["git", "config", "user.name", "Speech Paper Daily Bot"], check=False)

    subprocess.run(["git", "add", f"papers/{date_str}/"], check=False)

    status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
    if not status.stdout.strip():
        print("✨ 没有新的变更需要提交")
        return True

    msg = f"Daily Speech Papers Update - {date_str}"
    subprocess.run(["git", "commit", "-m", msg], check=False)
    result = subprocess.run(["git", "push", "origin", "main"], capture_output=True, text=True)

    if result.returncode == 0:
        print(f"✅ 同步成功: {date_str}")
        return True
    else:
        print(f"❌ 推送失败: {result.stderr}")
        return False

if __name__ == "__main__":
    sync()