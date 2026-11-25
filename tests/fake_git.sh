#!/bin/sh
# git for the merge-queue tests: the real git ($FAKE_GIT_REAL) on local repositories only, every call a line
# in $FAKE_GIT_LOG (the cwd, a tab, then each argument followed by \037), and a forced push refused outright
# (the queue never forces). A deletion only of the queue's own trial branches (mq/…): a PR's branch is never
# deleted. $FAKE_GIT_FAIL, a file «<subcommand> <n> <message>»: the next n calls of that subcommand fail with
# «fatal: <message>» (a network failure). Shell, not Python: it runs for every git call of the tests.
if [ -n "$FAKE_GIT_LOG" ]; then
  { printf '%s\t' "$PWD"; for a in "$@"; do printf '%s\037' "$a"; done; printf '\n'; } >> "$FAKE_GIT_LOG"
fi
if [ -n "$FAKE_GIT_FAIL" ] && [ -f "$FAKE_GIT_FAIL" ]; then
  sub=""; skip=0
  for a in "$@"; do
    if [ "$skip" = 1 ]; then skip=0; continue; fi
    case "$a" in -c|-C) skip=1;; -*) ;; *) sub="$a"; break;; esac
  done
  read -r fsub fn fmsg < "$FAKE_GIT_FAIL"
  if [ -n "$sub" ] && [ "$sub" = "$fsub" ] && [ "${fn:-0}" -gt 0 ]; then
    echo "$fsub $((fn - 1)) $fmsg" > "$FAKE_GIT_FAIL"
    echo "fatal: $fmsg" >&2
    exit 128
  fi
fi
push=0; del=0
for a in "$@"; do
  if [ "$push" = 1 ]; then
    case "$a" in
      --force|-f|--force-with-lease|--force-with-lease=*|--mirror|--prune|+*)
        echo "fake git: a forced push is refused: $*" >&2; exit 1;;
      --delete|-d) del=1;;
    esac
  fi
  [ "$a" = push ] && push=1
done
if [ "$del" = 1 ]; then  # every ref after the remote must be a trial branch
  seen=0; n=0
  for a in "$@"; do
    if [ "$seen" = 1 ]; then
      case "$a" in
        -*) ;;
        *) n=$((n + 1))
           if [ "$n" -gt 1 ]; then
             case "$a" in mq/*|refs/heads/mq/*) ;; *) echo "fake git: a deletion of $a is refused: $*" >&2; exit 1;; esac
           fi;;
      esac
    fi
    [ "$a" = push ] && seen=1
  done
fi
exec "$FAKE_GIT_REAL" "$@"
