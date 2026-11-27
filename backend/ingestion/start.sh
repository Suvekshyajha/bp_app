#!/usr/bin/env bash
# Runs the ingestion service and its worker in one container: they share
# ingest.db and the upload folder, so they must see the same disk.
# If either process ends, the other is stopped and the container exits,
# so the host restarts both.

python main.py &
api=$!
python worker.py &
worker=$!

stopping=0
stop_all() {
    kill -TERM "$api" "$worker" 2>/dev/null
}
trap 'stopping=1; stop_all' TERM INT

wait -n "$api" "$worker"
status=$?

stop_all
wait

# A requested stop is a success. A process that ended on its own is a failure,
# even if it reported success.
if [ "$stopping" -eq 1 ]; then
    exit 0
fi
[ "$status" -eq 0 ] && status=1
exit "$status"
