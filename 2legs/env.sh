# source this in EVERY new terminal tab before any 2legs step:
#   source <path-to-jan>/2legs/env.sh
# Paths are derived from this file's location, so it works on any machine:
#   JAN    = the jan-auto-agent checkout holding this 2legs/ folder
#   TARGET = ext-demo-repo next to it (override: TARGET=/x source env.sh)
_here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
export JAN=$(cd "$_here/.." && pwd)
export TARGET=${TARGET:-$(cd "$JAN/.." && pwd)/ext-demo-repo}
export PYTHONPATH=$JAN
export KILO=$(ls -d ~/.vscode/extensions/kilocode.kilo-code-*/bin/kilo 2>/dev/null | sort -V | tail -1)
[ -n "$KILO" ] || echo "!! Kilo VS Code extension not found under ~/.vscode/extensions" >&2
echo "JAN=$JAN  TARGET=$TARGET  KILO=$KILO"
