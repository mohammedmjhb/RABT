#!/usr/bin/env bash
# Audit: the CUSTOMER/ENGINEER/COMPANY/AGENT Flutter app must contain NO admin code.
# Usage: ./check_admin_isolation.sh /path/to/unified_flutter_app
set -u
root="${1:?usage: $0 <flutter_project_dir>}"
[ -d "$root/lib" ] || { echo "ERROR: $root/lib not found"; exit 2; }
fail=0
check() {  # $1=title  $2=regex
  hits=$(grep -rniE --include='*.dart' --include='*.yaml' --include='*.json' --include='*.gradle' --include='*.plist' \
         --exclude-dir=build --exclude-dir=.dart_tool -e "$2" "$root" 2>/dev/null | head -20)
  if [ -n "$hits" ]; then echo "FAIL  $1"; echo "$hits" | sed 's/^/      /'; fail=1; else echo "PASS  $1"; fi
}
check "no SUPER_ADMIN role reference"        'super_?admin'
check "no /admin API paths"                  "['\"/]admin[/'\"]|/v1/admin|admin/auth"
check "no admin screens/routes/widgets"      'class +[A-Za-z]*Admin[A-Za-z]*(Screen|Page|View)|admin_(screen|page|dashboard)'
check "no admin credentials / keys embedded" 'admin[_-]?(password|secret|token|key)'
echo "NOTE  Client-side checks are NOT a security boundary: the backend must reject every"
echo "      /admin/* request lacking a SUPER_ADMIN JWT (verify separately with curl)."
[ $fail -eq 0 ] && echo "RESULT: PASS" || echo "RESULT: FAIL"
exit $fail
