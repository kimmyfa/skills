#!/bin/bash
# GitHub Repository Setup Script
# 使用方法: ./setup_github_repo.sh <GITHUB_TOKEN>

set -e

TOKEN="$1"
REPO_NAME="speech-paper-daily"
REPO_OWNER="kimmyfa"
DESCRIPTION="每日语音论文自动同步 - Speech Paper Daily Auto Sync"

if [ -z "$TOKEN" ]; then
    echo "❌ 请提供 GitHub Token"
    echo "使用方式: $0 <GITHUB_TOKEN>"
    exit 1
fi

echo "🔧 创建 GitHub 仓库..."

# 创建仓库 API
RESPONSE=$(curl -s -X POST \
    -H "Authorization: token $TOKEN" \
    -H "Accept: application/vnd.github.v3+json" \
    https://api.github.com/user/repos \
    -d "{\"name\":\"$REPO_NAME\",\"description\":\"$DESCRIPTION\",\"private\":false,\"auto_init\":true}")

# 检查是否成功
if echo "$RESPONSE" | grep -q '"full_name"'; then
    echo "✅ 仓库创建成功: https://github.com/$REPO_OWNER/$REPO_NAME"
else
    if echo "$RESPONSE" | grep -q '"Already exists"'; then
        echo "ℹ️ 仓库已存在，跳过创建"
    else
        echo "❌ 创建失败: $RESPONSE"
        exit 1
    fi
fi

echo ""
echo "📋 下一步:"
echo "1. 设置定时任务 (每天晚上自动执行):"
echo "   crontab -e"
echo "   添加: 0 22 * * * cd /Users/kimmy/Desktop/Vagent_app/SpeechAIResercher && python3 .skills/speech-paper-daily/scripts/sync_to_github.py"
echo ""
echo "2. 或者手动执行同步:"
echo "   python3 .skills/speech-paper-daily/scripts/sync_to_github.py"
echo ""
echo "3. 确保环境变量中有 GitHub Token:"
echo "   export GITHUB_TOKEN=\"your_token_here\""