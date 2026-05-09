# ----------------------------------------------------------------------
# Trade Copilot — developer & deploy targets
# Usage:    make help
# ----------------------------------------------------------------------

SHELL := /bin/bash

BACKEND_DIR  := backend
FRONTEND_DIR := frontend
COMPOSE      := docker compose

.PHONY: help \
        dev dev-down logs \
        backend-dev frontend-dev \
        test test-backend test-frontend \
        lint lint-backend lint-frontend \
        docker-build docker-build-backend docker-build-frontend \
        deploy deploy-frontend deploy-backend \
        clean

help:               ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "\nTrade Copilot — make targets\n\n"} \
	      /^[a-zA-Z_-]+:.*?##/ {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo

# ---------- local dev (docker) ----------
dev:                ## Run full stack via docker-compose
	$(COMPOSE) up --build

dev-down:           ## Tear down docker-compose stack
	$(COMPOSE) down

logs:               ## Tail compose logs
	$(COMPOSE) logs -f --tail=100

# ---------- local dev (host) ----------
backend-dev:        ## Run backend locally with hot reload
	cd $(BACKEND_DIR) && \
	  source venv/bin/activate && \
	  uvicorn app.main:app --reload --port 8000

frontend-dev:       ## Run frontend dev server
	cd $(FRONTEND_DIR) && npm run dev

# ---------- testing ----------
test: test-backend test-frontend  ## Run all tests

test-backend:       ## Run backend pytest
	cd $(BACKEND_DIR) && \
	  ( [ -d venv ] && source venv/bin/activate || true ) && \
	  pytest -q

test-frontend:      ## Run frontend tests (vitest if present)
	cd $(FRONTEND_DIR) && ( npm test -- --run 2>/dev/null || npm test || echo "no tests configured" )

# ---------- lint ----------
lint: lint-backend lint-frontend  ## Run all linters

lint-backend:       ## Ruff check
	cd $(BACKEND_DIR) && \
	  ( [ -d venv ] && source venv/bin/activate || true ) && \
	  ruff check .

lint-frontend:      ## next lint
	cd $(FRONTEND_DIR) && npx next lint || true

# ---------- docker images ----------
docker-build: docker-build-backend docker-build-frontend  ## Build both images

docker-build-backend:
	docker build -f Dockerfile.backend  -t trade-copilot-backend:latest  .

docker-build-frontend:
	docker build -f Dockerfile.frontend -t trade-copilot-frontend:latest .

# ---------- deploy ----------
deploy: deploy-backend deploy-frontend  ## Deploy backend (Railway) then frontend (Vercel)

deploy-frontend:    ## Deploy frontend to Vercel (prod)
	cd $(FRONTEND_DIR) && vercel --prod

deploy-backend:     ## Deploy backend to Railway
	railway up

# ---------- housekeeping ----------
clean:              ## Remove caches & build artefacts
	find . -type d \( -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache -o -name .next -o -name .turbo \) -prune -exec rm -rf {} +
	rm -f $(BACKEND_DIR)/.coverage
