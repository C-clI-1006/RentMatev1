#!/bin/bash
# Finder double-click entry point on macOS.
cd -- "$(dirname -- "$0")" || exit 1
bash ./Start-RentMate.sh "$@"
RESULT=$?
if [ "$RESULT" -ne 0 ] && [ "$RESULT" -ne 130 ] && [ -t 0 ]; then
  read -r -p 'RentMate stopped with an error. Press Return to close.' _reply
fi
exit "$RESULT"
