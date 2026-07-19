#!/bin/sh
set -eu

# Load a local .env only when one is explicitly mounted into the container.
if [ -f "/app/.env" ]; then
  while IFS='=' read -r key value; do
    case "$key" in
      ''|'#'*) continue ;;
      *) export "$key=$value" ;;
    esac
  done < "/app/.env"
fi

export PYTHONPATH="/app/src${PYTHONPATH:+:$PYTHONPATH}"

# Run as PID 1 so stop signals are forwarded correctly.
exec streamlit run src/symnote/app.py --server.address=0.0.0.0 --server.port=8501
