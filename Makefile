.PHONY: test run scan lint clean start stop logs

# Run all tests
test:
	python -m pytest tests/ -v --tb=short

# Run with coverage
test-cov:
	python -m pytest tests/ -v --cov=src --cov-report=html --cov-report=term

# Run the bot (scanner loop)
run:
	python -m src.main

# Run a single scan cycle (useful for testing)
scan:
	python -c "import asyncio; from src.main import main; asyncio.run(main())"

# Lint
lint:
	python -m mypy src/ --ignore-missing-imports

# Clean
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	rm -rf .pytest_cache htmlcov .mypy_cache

# Install dependencies
install:
	pip install -r requirements.txt --break-system-packages

# Start as pm2 background process
start:
	./scripts/start.sh

# Stop pm2 background process
stop:
	./scripts/stop.sh

# Tail pm2 logs
logs:
	pm2 logs polyedge

# H-4: Run backtest in validate mode (CI guard against lookahead bias)
backtest-validate:
	python -m scripts.backtest_engine --validate

# Check database stats
stats:
	python -c "from src.storage.database import Database; db = Database(); print(db.get_stats())"
