#!/bin/sh
set -eu

update_extension() {
    python3 update_version.py "$1" "$2"
}

"$@"
