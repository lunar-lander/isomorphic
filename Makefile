## fastapi-isomorphic Makefile
## Common dev/release tasks. Targets are phony unless they produce files.

PYTHON ?= python
PKG    := fastapi_isomorphic
EXAMPLE := examples.demo_app:app

.PHONY: help install dev test test-quick lint clean build publish-test publish docs demo

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

install: ## Install the package in editable mode
	$(PYTHON) -m pip install -e .

dev: install ## Install with dev/test deps
	$(PYTHON) -m pip install -e ".[dev]" || $(PYTHON) -m pip install pytest

test: ## Run the full test suite
	$(PYTHON) -m pytest tests/ -v

test-quick: ## Run tests, stop on first failure
	$(PYTHON) -m pytest tests/ -x -q

lint: ## Lint with ruff if available
	@command -v ruff >/dev/null 2>&1 && ruff check $(PKG) tests || echo "ruff not installed; skipping"

clean: ## Remove build artifacts and caches
	rm -rf build dist *.egg-info $(PKG).egg-info \
	       .pytest_cache .ruff_cache .mypy_cache .coverage
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

build: clean ## Build sdist + wheel
	$(PYTHON) -m build

publish-test: build ## Publish to TestPyPI
	$(PYTHON) -m twine upload --repository testpypi dist/*

publish: build ## Publish to PyPI
	$(PYTHON) -m twine upload dist/*

docs: ## Render the README
	@command -v mdcat >/dev/null 2>&1 && mdcat README.md || cat README.md

demo: ## Run the demo app CLI (--help)
	PYTHONPATH=. $(PYTHON) -m $(PKG) $(EXAMPLE) --help
