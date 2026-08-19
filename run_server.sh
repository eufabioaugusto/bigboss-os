#!/bin/zsh
cd /Users/fabio/AGENTES/email-prospection
exec /usr/bin/python3 -m uvicorn server:app --host 127.0.0.1 --port 7860
