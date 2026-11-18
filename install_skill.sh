#!/usr/bin/env bash
# ==============================================================================
# DevBox Skills Installer & Updater
# Installs or updates DevBox agent skills in a project or globally.
# ==============================================================================

set -euo pipefail

DEFAULT_REPO="https://github.com/SimplyBarter/DevBox.git"
REPO_URL="${DEVBOX_REPO:-$DEFAULT_REPO}"
BRANCH="${DEVBOX_BRANCH:-main}"
DEFAULT_SKILLS=("devbox_configure" "devbox_rsync" "devbox_deploy")
CHOSEN_SKILL=""

# Determine target directory
DEST_MODE="local"
TARGET_BASE=""

show_help() {
  cat <<EOF
DevBox Skills Installer & Updater

Usage:
  ./install_skill.sh [OPTIONS]

Options:
  -l, --local             Install into current project (.agents/skills) [Default]
  -g, --global            Install globally (~/.agents/skills)
  -s, --skill <name>      Install a specific skill (devbox_configure, devbox_rsync, devbox_deploy). Default: all
  -t, --target <path>     Install into a custom target base directory
  -r, --repo <url>        Git repository URL (Default: $DEFAULT_REPO)
  -b, --branch <branch>   Git branch (Default: $BRANCH)
  -h, --help              Show this help message

Examples:
  # Install all skills into current project:
  ./install_skill.sh

  # One-line install from GitHub:
  curl -fsSL https://raw.githubusercontent.com/SimplyBarter/DevBox/main/install_skill.sh | bash

  # Install globally across all projects:
  ./install_skill.sh --global

  # Install only devbox_rsync:
  ./install_skill.sh --skill devbox_rsync
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -l|--local)
      DEST_MODE="local"
      shift
      ;;
    -g|--global)
      DEST_MODE="global"
      shift
      ;;
    -s|--skill)
      CHOSEN_SKILL="$2"
      shift 2
      ;;
    -t|--target)
      DEST_MODE="custom"
      TARGET_BASE="$2"
      shift 2
      ;;
    -r|--repo)
      REPO_URL="$2"
      shift 2
      ;;
    -b|--branch)
      BRANCH="$2"
      shift 2
      ;;
    -h|--help)
      show_help
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      show_help
      exit 1
      ;;
  esac
done

# Resolve skills to install
if [[ -n "$CHOSEN_SKILL" ]]; then
  SKILLS=("$CHOSEN_SKILL")
else
  SKILLS=("${DEFAULT_SKILLS[@]}")
fi

# Resolve destination base directory
if [[ "$DEST_MODE" == "local" ]]; then
  TARGET_BASE="$(pwd)/.agents/skills"
elif [[ "$DEST_MODE" == "global" ]]; then
  TARGET_BASE="$HOME/.agents/skills"
fi

echo "============================================================"
echo " DevBox Skills: Install / Update"
echo " Skills: ${SKILLS[*]}"
echo " Target Base: $TARGET_BASE"
echo "============================================================"

# Check if running from within a local DevBox repository clone
SCRIPT_SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_SKILLS_ROOT="$SCRIPT_SOURCE_DIR/.agents/skills"

TMP_DIR=""
cleanup() {
  if [[ -n "$TMP_DIR" && -d "$TMP_DIR" ]]; then
    rm -rf "$TMP_DIR"
  fi
}
trap cleanup EXIT

if [[ -d "$LOCAL_SKILLS_ROOT" && "$LOCAL_SKILLS_ROOT" != "$TARGET_BASE" ]]; then
  echo "[*] Installing from local repository source: $LOCAL_SKILLS_ROOT"
  SOURCE_BASE="$LOCAL_SKILLS_ROOT"
else
  echo "[*] Fetching latest skill files from $REPO_URL ($BRANCH)..."
  TMP_DIR="$(mktemp -d /tmp/devbox_install_XXXXXX)"
  git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$TMP_DIR/repo" 2>/dev/null || {
    echo "[-] Git clone failed. Falling back to archive download..."
    ARCHIVE_URL="${REPO_URL%.git}/archive/refs/heads/${BRANCH}.tar.gz"
    mkdir -p "$TMP_DIR/repo"
    curl -fsSL "$ARCHIVE_URL" | tar -xz -C "$TMP_DIR/repo" --strip-components=1
  }
  SOURCE_BASE="$TMP_DIR/repo/.agents/skills"
fi

for SKILL_NAME in "${SKILLS[@]}"; do
  SOURCE_DIR="$SOURCE_BASE/$SKILL_NAME"
  TARGET_DIR="$TARGET_BASE/$SKILL_NAME"

  if [[ ! -d "$SOURCE_DIR" ]]; then
    echo "Error: Could not locate skill source directory at $SOURCE_DIR" >&2
    exit 1
  fi

  ACTION="Installing"
  if [[ -d "$TARGET_DIR" ]]; then
    ACTION="Updating"
  fi
  echo "[*] $ACTION skill '$SKILL_NAME' into $TARGET_DIR..."

  mkdir -p "$TARGET_DIR"

  # Copy SKILL.md
  if [[ -f "$SOURCE_DIR/SKILL.md" ]]; then
    cp "$SOURCE_DIR/SKILL.md" "$TARGET_DIR/SKILL.md"
  fi

  # Copy template directory if exists
  if [[ -d "$SOURCE_DIR/template" ]]; then
    mkdir -p "$TARGET_DIR/template"
    cp -r "$SOURCE_DIR/template/." "$TARGET_DIR/template/"
  fi

  # Copy scripts directory if exists
  if [[ -d "$SOURCE_DIR/scripts" ]]; then
    mkdir -p "$TARGET_DIR/scripts"
    cp -r "$SOURCE_DIR/scripts/." "$TARGET_DIR/scripts/"
    find "$TARGET_DIR/scripts" -type f -name "*.py" -exec chmod +x {} +
  fi

  echo "    [✓] Files for '$SKILL_NAME' copied."

  # Verify if verify_config exists in skill
  if [[ -f "$TARGET_DIR/scripts/verify_config.py" ]]; then
    echo "    [*] Running verification for $SKILL_NAME..."
    python3 "$TARGET_DIR/scripts/verify_config.py" >/dev/null
    echo "    [✓] Health check passed."
  fi
done

echo ""
echo "============================================================"
echo " [✓] DevBox skills successfully installed/updated!"
echo "============================================================"
echo ""
echo "To use the skills in your project:"
echo "  - In Chat / AI Assistant:"
echo "      /devbox_configure  (Generate VM execution runbook)"
echo "      /devbox_deploy     (Deploy and provision virtual machine)"
echo "      /devbox_rsync      (Generate application code sync script)"
echo "  - From CLI:"
echo "      python3 $TARGET_BASE/devbox_configure/scripts/configure_vm.py --probe localhost"
echo "      python3 $TARGET_BASE/devbox_deploy/scripts/deploy_vm.py"
echo "      python3 $TARGET_BASE/devbox_rsync/scripts/generate_sync.py"
echo ""
