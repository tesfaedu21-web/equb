@echo off
echo Starting Equb Management System...
echo Open your browser at: http://localhost:8000
echo Press Ctrl+C to stop.
echo.
python -m uvicorn main:app --reload --host 0.0.0.0 --port 8000
