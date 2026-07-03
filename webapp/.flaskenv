# Flask CLI defaults, loaded automatically by `flask run` (python-dotenv).
# Ensures the map maker binds port 5050 even when launched via `flask run`
# or an IDE run-config that bypasses the `if __name__ == '__main__'` block in
# app.py (Flask's own default is 5000, which collides with macOS AirPlay).
FLASK_APP=app.py
FLASK_RUN_PORT=5050
FLASK_RUN_HOST=0.0.0.0
