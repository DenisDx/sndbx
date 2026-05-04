#!/bin/bash
# sndbx test suite - validate MCP server functionality

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MCP_URL="${1:-http://localhost:30081}"
TOKEN="${2:-test-token-123456789}"
ENVID="${3:-default-env-token}"

echo "Testing sndbx MCP server at $MCP_URL"
echo "Token: $TOKEN"
echo "Envid: $ENVID"
echo ""

# Test 1: Get sandbox status
echo "Test 1: Get sandbox status"
curl -s -X POST "$MCP_URL" \
  -H "Content-Type: application/json" \
  -d "{\"id\":\"test-1\",\"method\":\"sandbox_status\",\"token\":\"$TOKEN\",\"envid\":\"$ENVID\"}" | python3 -m json.tool

echo ""
echo ""

# Test 2: Invalid token
echo "Test 2: Invalid token (should fail)"
curl -s -X POST "$MCP_URL" \
  -H "Content-Type: application/json" \
  -d "{\"id\":\"test-2\",\"method\":\"sandbox_status\",\"token\":\"invalid-token\",\"envid\":\"$ENVID\"}" | python3 -m json.tool

echo ""
echo ""

# Test 3: Execute command (will fail if sandbox not running, that's ok)
echo "Test 3: Execute command in sandbox (may fail if not running)"
curl -s -X POST "$MCP_URL" \
  -H "Content-Type: application/json" \
  -d "{\"id\":\"test-3\",\"method\":\"execute_command\",\"token\":\"$TOKEN\",\"envid\":\"$ENVID\",\"params\":{\"command\":\"echo hello\"}}" | python3 -m json.tool

echo ""
echo ""

echo "Tests completed!"
