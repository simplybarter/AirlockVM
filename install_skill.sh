#!/usr/bin/env bash
# ==============================================================================
# DevBox Skill Installer & Updater
# Installs or updates the 'devbox_configure' skill in a project or globally.
# ==============================================================================

set -euo pipefail

DEFAULT_REPO="https://github.com/SimplyBarter/DevBox.git"
REPO_URL="${DEVBOX_REPO:-$DEFAULT_REPO}"
BRANCH="${DEVBOX_BRANCH:-main}"
SKILL_NAME="devbox_configure"

# Determine target directory
DEST_MODE="local"
TARGET_DIR=""

show_help() {
  cat <<EOF
DevBox Skill Installer & Updater

Usage:
  ./install_skill.sh [OPTIONS]

Options:
  -l, --local             Install into current project (.agents/skills/$SKILL_NAME) [Default]
  -g, --global            Install globally (~/.agents/skills/$SKILL_NAME)
  -t, --target <path>     Install into a custom target directory
  -r, --repo <url>        Git repository URL (Default: $DEFAULT_REPO)
  -b, --branch <branch>   Git branch (Default: $BRANCH)
  -h, --help              Show this help message

Examples:
  # Install into current project:
  ./install_skill.sh

  # One-line install from GitHub:
  curl -fsSL https://raw.githubusercontent.com/SimplyBarter/DevBox/main/install_skill.sh | bash

  # Install globally across all projects:
  ./install_skill.sh --global
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
    -t|--target)
      DEST_MODE="custom"
      TARGET_DIR="$2"
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

# Resolve destination path
if [[ "$DEST_MODE" == "local" ]]; then
  TARGET_DIR="$(pwd)/.agents/skills/$SKILL_NAME"
elif [[ "$DEST_MODE" == "global" ]]; then
  TARGET_DIR="$HOME/.agents/skills/$SKILL_NAME"
fi

echo "============================================================"
echo " DevBox Skill: Install / Update ($SKILL_NAME)"
echo "============================================================"
echo "Target Location: $TARGET_DIR"

# Check if running from within a local DevBox repository clone
SCRIPT_SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_SKILL_SOURCE="$SCRIPT_SOURCE_DIR/.agents/skills/$SKILL_NAME"

TMP_DIR=""
cleanup() {
  if [[ -n "$TMP_DIR" && -d "$TMP_DIR" ]]; then
    rm -rf "$TMP_DIR"
  fi
}
trap cleanup EXIT

if [[ -d "$LOCAL_SKILL_SOURCE" && "$LOCAL_SKILL_SOURCE" != "$TARGET_DIR" ]]; then
  echo "[*] Installing from local repository source: $LOCAL_SKILL_SOURCE"
  SOURCE_DIR="$LOCAL_SKILL_SOURCE"
else
  echo "[*] Fetching latest skill files from $REPO_URL ($BRANCH)..."
  TMP_DIR="$(mktemp -d /tmp/devbox_install_XXXXXX)"
  git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$TMP_DIR/repo" 2>/dev/null || {
    echo "[-] Git clone failed. Falling back to archive download..."
    ARCHIVE_URL="${REPO_URL%.git}/archive/refs/heads/${BRANCH}.tar.gz"
    mkdir -p "$TMP_DIR/repo"
    curl -fsSL "$ARCHIVE_URL" | tar -xz -C "$TMP_DIR/repo" --strip-components=1
  }
  SOURCE_DIR="$TMP_DIR/repo/.agents/skills/$SKILL_NAME"
fi

if [[ ! -d "$SOURCE_DIR" ]]; then
  echo "Error: Could not locate skill source directory at $SOURCE_DIR" >&2
  exit 1
fi

# Prepare target directory
ACTION="Installing"
if [[ -d "$TARGET_DIR" ]]; then
  ACTION="Updating"
fi
echo "[*] $ACTION skill files into $TARGET_DIR..."

mkdir -p "$TARGET_DIR/template" "$TARGET_DIR/scripts"

# Copy skill components
cp "$SOURCE_DIR/SKILL.md" "$TARGET_DIR/SKILL.md"
cp "$SOURCE_DIR/template/DEVELOPMENT_VM.md.template" "$TARGET_DIR/template/DEVELOPMENT_VM.md.template"
cp "$SOURCE_DIR/template/defaults.json" "$TARGET_DIR/template/defaults.json"
cp "$SOURCE_DIR/scripts/configure_vm.py" "$TARGET_DIR/scripts/configure_vm.py"
cp "$SOURCE_DIR/scripts/probe_hypervisor.py" "$TARGET_DIR/scripts/probe_hypervisor.py"
cp "$SOURCE_DIR/scripts/verify_config.py" "$TARGET_DIR/scripts/verify_config.py"

# Set executable permissions on scripts
chmod +x "$TARGET_DIR/scripts/configure_vm.py" \
         "$TARGET_DIR/scripts/probe_hypervisor.py" \
         "$TARGET_DIR/scripts/verify_config.py"

echo "[✓] Files successfully copied and permissions set."

# Run verification in target directory
echo "[*] Verifying installation health..."
if python3 "$TARGET_DIR/scripts/verify_config.py"; then
  echo ""
  echo "============================================================"
  echo " [✓] DevBox skill successfully installed/updated!"
  echo "============================================================"
  echo ""
  echo "To use the skill in your project:"
  echo "  - In Chat / AI Assistant: Type '/devbox_configure'"
  echo "  - From CLI: python3 $TARGET_DIR/scripts/configure_vm.py --probe localhost"
  echo ""
else
  echo "[-] Warning: verify_config.py reported an issue with the installation." >&2
  exit 1
fi
