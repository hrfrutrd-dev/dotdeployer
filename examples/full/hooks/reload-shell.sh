#!/bin/sh
# post_entry hook: called once per deployed entry with the entry name as $1.
# Keep hooks fast and idempotent - they run inside `apply`.
entry="${1:-unknown}"
echo "  (hook) deployed: ${entry}"
case "${entry}" in
  *zshrc*|*aliases*) [ -n "${ZSH_VERSION:-}" ] && exec zsh -l -c 'true' ;;
esac
exit 0
