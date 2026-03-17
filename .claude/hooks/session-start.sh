#!/bin/bash
set -euo pipefail

# Only run in remote (Claude Code on the web) environments
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

# Default to current directory if CLAUDE_PROJECT_DIR is not set
CLAUDE_PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"

# Install Node.js dependencies if package.json exists
if [ -f "$CLAUDE_PROJECT_DIR/package.json" ]; then
  echo "Installing Node.js dependencies..."
  cd "$CLAUDE_PROJECT_DIR"
  npm install
fi

# Install Python dependencies if requirements.txt exists
if [ -f "$CLAUDE_PROJECT_DIR/requirements.txt" ]; then
  echo "Installing Python dependencies..."
  pip install -r "$CLAUDE_PROJECT_DIR/requirements.txt"
fi

# Install Python dependencies via pyproject.toml if it exists
if [ -f "$CLAUDE_PROJECT_DIR/pyproject.toml" ]; then
  echo "Installing Python project dependencies..."
  cd "$CLAUDE_PROJECT_DIR"
  pip install -e .
fi

echo "Session start hook completed successfully."
