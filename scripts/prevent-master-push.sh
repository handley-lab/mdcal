#!/bin/bash
while read -r local_ref local_sha remote_ref remote_sha; do
    if [ "$remote_ref" = "refs/heads/master" ] || [ "$remote_ref" = "refs/heads/main" ]; then
        echo "Direct pushes to '$remote_ref' are not allowed. Use a pull request." >&2
        exit 1
    fi
done
exit 0
