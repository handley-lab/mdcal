#!/bin/bash
current_branch=$(git symbolic-ref --short HEAD 2>/dev/null || echo "detached")

if [ "$current_branch" = "master" ] || [ "$current_branch" = "main" ]; then
    echo "Direct commits to '$current_branch' are not allowed. Create a feature branch:" >&2
    echo "  git checkout -b feature/your-feature-name" >&2
    exit 1
fi
exit 0
