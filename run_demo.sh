#!/bin/bash

# Kill background processes on exit
trap "kill 0" EXIT

echo "Starting Mock Ryu Controller..."
python3 mock_ryu.py &
MOCK_RYU_PID=$!

sleep 2

echo "Starting MCP Server..."
python3 my_server.py &
MCP_SERVER_PID=$!

sleep 3

echo "Running Demo Client..."
python3 demo_client.py

echo "Demo finished. Cleaning up..."
kill $MOCK_RYU_PID
kill $MCP_SERVER_PID
