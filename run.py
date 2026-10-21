"""Startet den Server: python run.py [--port 8000] [--host 0.0.0.0]"""
import argparse

import uvicorn

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)
    a = p.parse_args()
    uvicorn.run("cansniffer.main:app", host=a.host, port=a.port, log_level="info")
