#!/bin/sh
# The demo depot: an invented project (a library catalogue app), built with plain depot2 commands.
# Usage: sh examples/demo/make-demo.sh DIR   (DIR must not exist yet). Everything in it is made up.
set -eu
DIR=${1:?usage: make-demo.sh DIR}
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
export DEPOT2_BACKUP=off
d() { python3 "$ROOT/depot2/__main__.py" --state "$DIR" "$@" >/dev/null; }

# the config first: its `characters` is the team that init seeds (a depot without them has no team)
mkdir "$DIR"
cp "$HERE/config.json" "$DIR/config.json"
d init --balance 5

# two themes and their cars
d tracks add --title "Search"
d tracks add --title "Accounts"
d tracks car add tr-0001 --title "Search: a design note on ranking" --size doc --stage sketch
d tracks car add tr-0001 --title "Search: the index and the query parser" --size M --after tr-0001:0
d tracks car add tr-0001 --title "Search: results page" --size S --after tr-0001:1
d tracks car add tr-0002 --title "Accounts: sign-in with a one-time code" --size L
d tracks car add tr-0002 --title "Accounts: export my data" --size S

# a train that builds the search cars in order, one slot at a time
d trains add --name search-v1 --title "Search, first version" --car tr-0001:0 --car tr-0001:1 --car tr-0001:2 \
  --slots "max 1" --priority 7
d trains add --name accounts --title "Accounts" --car tr-0002:0 --car tr-0002:1 --after search-v1

# the stack: one task waiting for a slot
d stack add --title "Fix the flaky date test" --size S --autostart no

# the queues: a wish, a thing to think over, a todo
d wishes add --title "Dark mode for the reader" --words "would be nice for night reading" --area product
d think add --topic "Should search be offline-first?" --question "What do we lose without a server?"
d todo add --text "The catalogue import logs every row at debug level" --where "src/catalogue/import.py:88" --kind cleanup

# principles: one gate, one scored
d principles add --section core --title "The answer first" --text "The first line of a search result is what was asked for." --check yes
d principles add --section mechanics --title "A dry run first" --text "An import shows its report before it writes anything." --check yes

# a decision of the owner, about one of the example characters
d decide builder --text "Keep the index in one file until it is over 50 MB."

echo "demo depot at $DIR"
