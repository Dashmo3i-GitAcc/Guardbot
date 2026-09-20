"""Make the app package importable in tests without a real .env."""
import os
import sys

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("GROUP_IDS", "-1001234567890")
os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("TMP_DIR", "/tmp/guardbot-test")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
