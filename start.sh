#!/bin/bash

# WeddingSnap Dev Server Launcher
# Runs both FastAPI backend and Vite frontend concurrently with clean exit on Ctrl+C.

# Color constants
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${BLUE}==================================================${NC}"
echo -e "${BLUE}       WeddingSnap Development Launcher 🚀         ${NC}"
echo -e "${BLUE}==================================================${NC}"

# Ensure we are in the project root directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Cleanup function to kill backend and frontend on exit
cleanup() {
    echo -e "\n${YELLOW}Stopping servers...${NC}"
    if [ -n "$BACKEND_PID" ]; then
        kill "$BACKEND_PID" 2>/dev/null
    fi
    if [ -n "$FRONTEND_PID" ]; then
        kill "$FRONTEND_PID" 2>/dev/null
    fi
    echo -e "${GREEN}Servers stopped. Goodbye! 👋${NC}"
    exit 0
}

# Trap Ctrl+C (SIGINT) and kill signals
trap cleanup SIGINT SIGTERM EXIT

# 1. Start backend server
echo -e "${GREEN}[Backend]${NC} Starting FastAPI server..."
cd "$SCRIPT_DIR/backend"
if [ ! -d "venv" ]; then
    echo -e "${RED}[Backend] Error: virtual environment 'venv' not found in backend/ directory.${NC}"
    exit 1
fi
venv/bin/uvicorn app.main:app --reload --port 8000 &
BACKEND_PID=$!

# 2. Start frontend server
echo -e "${GREEN}[Frontend]${NC} Starting Vite dev server..."
cd "$SCRIPT_DIR/frontend"
npm run dev &
FRONTEND_PID=$!

# Wait for both background processes
wait
