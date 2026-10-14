# Source in every new terminal before any 2legs step; TARGET can be overridden.
_here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
export JAN=$(cd "$_here/.." && pwd)
export TARGET=${TARGET:-$(cd "$JAN/.." && pwd)/ext-demo-repo}
export PYTHONPATH=$JAN
export KILO=$(ls -d ~/.vscode/extensions/kilocode.kilo-code-*/bin/kilo 2>/dev/null | sort -V | tail -1)
[ -n "$KILO" ] || echo "!! Kilo VS Code extension not found under ~/.vscode/extensions" >&2
echo "JAN=$JAN  TARGET=$TARGET  KILO=$KILO"
