#!/bin/bash
# KiX Team Skill Setup
# 团队成员运行此脚本，将 trinity-build skill 安装到本地 Claude Code
# Usage: bash skills/setup.sh

SKILL_DIR="${HOME}/.claude/skills/trinity-build"
mkdir -p "${SKILL_DIR}"
cp "$(dirname "$0")/trinity-build.md" "${SKILL_DIR}/SKILL.md"
echo "✅ trinity-build skill installed to ${SKILL_DIR}"
echo "现在可以在 Claude Code 中使用: /trinity-build"
echo "或说: '用三体方法构建这个项目'"
