# Makefile — Docker-first workflow for office-convert.
#
# Everything runs inside containers. The only host requirements are
# `docker` and `make` itself. No need for Python, uv, qpdf, or pytest
# on the host.
#
# Quick reference (run `make help` for the full list):
#   make build         # build the production image (needs Aspose SDK tarball)
#   make build-test    # build the test runner image (no Aspose needed)
#   make test          # run unit + property + integration tests
#   make up            # start the service
#   make health        # GET /health via curl
#   make convert FILE=tests/corpus/simple.pdf  # POST a doc; saves /tmp/output.pdf
#   make demo          # end-to-end smoke via curl
#   make down          # stop the service

# =============================================================================
# Configuration — override on the command line if needed:
#   make build PROJECT_DIR=/elsewhere
# =============================================================================
SHELL                 := /bin/bash
PROJECT_DIR           ?= $(shell pwd)
IMAGE_PROD            ?= office-convert:dev
IMAGE_TEST            ?= office-convert:test
IMAGE_SMOKE_WORDS     ?= office-convert-smoke-words:dev
CONTAINER_NAME        ?= office-convert-dev
SMOKE_OUT_DIR         ?= /tmp/oc-smoke
PORT                  ?= 8080
LICENSE_FILE          ?= $(PROJECT_DIR)/Aspose.TotalforC++.lic
VENDOR_DIR            ?= $(PROJECT_DIR)/vendor/aspose
HEALTH_URL            := http://localhost:$(PORT)/health
CONVERT_URL           := http://localhost:$(PORT)/convert
DOCS_URL              := http://localhost:$(PORT)/docs
REDOC_URL             := http://localhost:$(PORT)/redoc
OPENAPI_URL           := http://localhost:$(PORT)/openapi.json

# Pretty-print helper. Tput colours if stdout is a TTY; plain otherwise.
ifeq ($(shell test -t 1 && echo y),y)
    GREEN := $(shell tput setaf 2)
    YELLOW:= $(shell tput setaf 3)
    BLUE  := $(shell tput setaf 4)
    RESET := $(shell tput sgr0)
endif

.DEFAULT_GOAL := help
.PHONY: help

# =============================================================================
# Help — derived from `## ` comments on each target line.
# =============================================================================
help: ## Show this help
	@printf "$(GREEN)office-convert — Docker-first workflow$(RESET)\n\n"
	@printf "$(BLUE)Build:$(RESET)\n"
	@awk 'BEGIN{FS=":.*## "} /^[a-z][a-z0-9_-]*:.*## .*BUILD/ {printf "  $(YELLOW)%-22s$(RESET) %s\n", $$1, $$2}' $(MAKEFILE_LIST) | sort
	@printf "\n$(BLUE)Test:$(RESET)\n"
	@awk 'BEGIN{FS=":.*## "} /^[a-z][a-z0-9_-]*:.*## .*TEST/ {printf "  $(YELLOW)%-22s$(RESET) %s\n", $$1, $$2}' $(MAKEFILE_LIST) | sort
	@printf "\n$(BLUE)Run / URL tests:$(RESET)\n"
	@awk 'BEGIN{FS=":.*## "} /^[a-z][a-z0-9_-]*:.*## .*RUN/ {printf "  $(YELLOW)%-22s$(RESET) %s\n", $$1, $$2}' $(MAKEFILE_LIST) | sort
	@printf "\n$(BLUE)Quality:$(RESET)\n"
	@awk 'BEGIN{FS=":.*## "} /^[a-z][a-z0-9_-]*:.*## .*QA/ {printf "  $(YELLOW)%-22s$(RESET) %s\n", $$1, $$2}' $(MAKEFILE_LIST) | sort
	@printf "\n$(BLUE)Cleanup:$(RESET)\n"
	@awk 'BEGIN{FS=":.*## "} /^[a-z][a-z0-9_-]*:.*## .*CLEAN/ {printf "  $(YELLOW)%-22s$(RESET) %s\n", $$1, $$2}' $(MAKEFILE_LIST) | sort
	@printf "\n$(BLUE)Quick start:$(RESET)\n"
	@printf "  $(YELLOW)make build-test$(RESET) && $(YELLOW)make test$(RESET)         # run tests (no Aspose needed)\n"
	@printf "  $(YELLOW)make build$(RESET) && $(YELLOW)make up$(RESET) && $(YELLOW)make demo$(RESET)  # full pipeline (needs Aspose SDK + license)\n"

# =============================================================================
# BUILD targets
# =============================================================================
.PHONY: build build-test

build: check-vendor check-license ## BUILD production Docker image (needs vendor/aspose/ populated + license)
	@printf "$(GREEN)Building production image $(IMAGE_PROD)...$(RESET)\n"
	docker build -t $(IMAGE_PROD) .
	@printf "$(GREEN)Done. Run:$(RESET) make up\n"

build-test: ## BUILD test-runner image (Python + dev deps; no Aspose needed)
	@printf "$(GREEN)Building test image $(IMAGE_TEST)...$(RESET)\n"
	docker build -t $(IMAGE_TEST) -f Dockerfile.test .
	@printf "$(GREEN)Done. Run:$(RESET) make test\n"

.PHONY: smoke-words
smoke-words: check-license check-vendor-words ## BUILD smoke-test Aspose.Words license + Linux .so (pre-integration validation)
	@printf "$(GREEN)Building Words smoke-test image $(IMAGE_SMOKE_WORDS)...$(RESET)\n"
	docker build -t $(IMAGE_SMOKE_WORDS) -f smoke_test/Dockerfile.smoke .
	@mkdir -p $(SMOKE_OUT_DIR)
	@printf "\n$(GREEN)Running smoke test...$(RESET)\n"
	@docker run --rm \
		-v $(LICENSE_FILE):/license/license.lic:ro \
		-v $(SMOKE_OUT_DIR):/out \
		$(IMAGE_SMOKE_WORDS) \
		/license/license.lic /out/words_smoke.pdf
	@printf "\n$(BLUE)Output file:$(RESET)\n"
	@file $(SMOKE_OUT_DIR)/words_smoke.pdf 2>/dev/null || ls -la $(SMOKE_OUT_DIR)/words_smoke.pdf
	@printf "\n$(YELLOW)IMPORTANT: open $(SMOKE_OUT_DIR)/words_smoke.pdf and verify NO watermark.$(RESET)\n"
	@printf "$(YELLOW)Watermark text would be 'Evaluation Only. Created with Aspose.Words...'.$(RESET)\n"

.PHONY: check-vendor-words
check-vendor-words:
	@if [ ! -f vendor/aspose/Words/Aspose.Words.Cpp/lib/libAspose.Words.Cpp.so ]; then \
		printf "$(YELLOW)ERROR: vendor/aspose/Words/Aspose.Words.Cpp/lib/libAspose.Words.Cpp.so not found.$(RESET)\n"; \
		printf "  Extract from ~/Downloads/aspose.total_for_cpp_windows_26.4.0.zip first.\n"; \
		printf "  See aidlc-docs/aidlc-state.md 'Aspose SKU pivot' refinement.\n"; \
		exit 1; \
	fi

# =============================================================================
# TEST targets — all run inside the test container
# =============================================================================
.PHONY: test test-unit test-property test-integration test-coverage test-e2e corpus

test: build-test ## TEST run unit + property + integration suites
	@printf "$(GREEN)Running tests in $(IMAGE_TEST)...$(RESET)\n"
	docker run --rm $(IMAGE_TEST) \
		pytest tests/unit tests/property tests/integration -v

test-unit: build-test ## TEST run unit tests only
	docker run --rm $(IMAGE_TEST) pytest tests/unit -v

test-property: build-test ## TEST run property-based tests only (Hypothesis)
	docker run --rm $(IMAGE_TEST) pytest tests/property -v

test-integration: build-test ## TEST run in-process integration tests
	docker run --rm $(IMAGE_TEST) pytest tests/integration -v

test-coverage: build-test ## TEST run all tests with 80% coverage gate
	docker run --rm $(IMAGE_TEST) \
		pytest --cov=office_convert --cov-fail-under=80 \
		--cov-report=term-missing tests/unit tests/property tests/integration

test-e2e: build build-test check-license ## TEST run Testcontainers e2e suite (needs running Docker + license)
	@printf "$(GREEN)Running e2e tests against $(IMAGE_PROD)...$(RESET)\n"
	docker run --rm \
		-v /var/run/docker.sock:/var/run/docker.sock \
		-v $(LICENSE_FILE):/host-license:ro \
		-e OFFICE_CONVERT_E2E_LICENSE=/host-license \
		-e OFFICE_CONVERT_E2E_IMAGE=$(IMAGE_PROD) \
		$(IMAGE_TEST) \
		pytest tests/e2e -m e2e -v

corpus: build-test ## TEST generate the synthetic test corpus (.docx/.pptx/.xlsx)
	@printf "$(GREEN)Generating test corpus fixtures...$(RESET)\n"
	docker run --rm -v $(PROJECT_DIR)/tests/corpus:/app/tests/corpus \
		$(IMAGE_TEST) python -m tests.corpus._generate
	@printf "$(GREEN)Fixtures available in tests/corpus/$(RESET)\n"
	@ls tests/corpus/ 2>/dev/null | grep -v -E '_generate|README|__pycache' | sed 's/^/  /'

# =============================================================================
# RUN targets — start the production service and hit URLs via curl
# =============================================================================
.PHONY: up down restart logs shell health docs convert test-bad-format demo

up: check-license ## RUN docker compose up -d (builds if needed); waits for /health
	@printf "$(GREEN)Starting via docker compose...$(RESET)\n"
	docker compose up -d --build
	@printf "Waiting for /health to respond..."
	@for i in 1 2 3 4 5 6 7 8 9 10; do \
		if curl -fsS $(HEALTH_URL) >/dev/null 2>&1; then \
			printf "\n$(GREEN)Service ready.$(RESET)\n"; \
			echo "  Health:  $(HEALTH_URL)"; \
			echo "  Convert: POST $(CONVERT_URL)"; \
			echo "  Docs:    $(DOCS_URL)"; \
			echo "  ReDoc:   $(REDOC_URL)"; \
			echo "  OpenAPI: $(OPENAPI_URL)"; \
			exit 0; \
		fi; \
		printf "."; sleep 2; \
	done; \
	printf "\n$(YELLOW)Timeout waiting for service. Check 'make logs'.$(RESET)\n"; \
	exit 1

down: ## RUN docker compose down (stops + removes the service)
	docker compose down
	@printf "$(GREEN)Stopped.$(RESET)\n"

restart: ## RUN docker compose restart
	docker compose restart

logs: ## RUN tail the service logs (Ctrl+C to exit)
	docker compose logs -f office-convert

ps: ## RUN show running services and their health status
	docker compose ps

shell: ## RUN open a shell inside the running container
	docker compose exec office-convert /bin/bash 2>/dev/null \
		|| docker compose exec office-convert /bin/sh

health: ## RUN GET /health via curl
	@printf "$(BLUE)GET $(HEALTH_URL)$(RESET)\n"
	@curl -fsS $(HEALTH_URL) | python3 -m json.tool 2>/dev/null \
		|| curl -fsS $(HEALTH_URL) \
		|| (printf "$(YELLOW)Service not responding. Run 'make up' first.$(RESET)\n"; exit 1)

docs: ## RUN print URLs for Swagger / ReDoc / OpenAPI
	@printf "$(BLUE)API Documentation URLs:$(RESET)\n"
	@echo "  Swagger UI: $(DOCS_URL)"
	@echo "  ReDoc:      $(REDOC_URL)"
	@echo "  OpenAPI:    $(OPENAPI_URL)"
	@printf "$(BLUE)\nOpen one in a browser to explore the API interactively.$(RESET)\n"

convert: ## RUN POST FILE=<path> to /convert; output to /tmp/output.pdf
ifndef FILE
	@printf "$(YELLOW)Usage: make convert FILE=path/to/document.docx$(RESET)\n"
	@printf "Example: make convert FILE=tests/corpus/simple.pdf\n"
	@exit 1
endif
	@if [ ! -f "$(FILE)" ]; then printf "$(YELLOW)File not found: $(FILE)$(RESET)\n"; exit 1; fi
	@printf "$(BLUE)POST $(CONVERT_URL) (file=$(FILE))$(RESET)\n"
	@curl -s -X POST $(CONVERT_URL) \
		-F "file=@$(FILE)" \
		-F 'options={"cache":true}' \
		-o /tmp/output.pdf \
		-D /tmp/oc-headers.txt \
		-w "HTTP %{http_code} | %{size_download} bytes | %{time_total}s\n"
	@printf "\n$(BLUE)Response headers:$(RESET)\n"
	@cat /tmp/oc-headers.txt | grep -E "^HTTP|^x-|^content-type" || true
	@printf "\n$(BLUE)Output file:$(RESET)\n"
	@file /tmp/output.pdf 2>/dev/null || ls -la /tmp/output.pdf

test-bad-format: ## RUN POST PNG bytes to /convert (verifies 400 unsupported_format)
	@printf "$(BLUE)POST $(CONVERT_URL) with PNG bytes (should return 400)$(RESET)\n"
	@printf '\x89PNG\r\n\x1a\n%s' "this is not a real png" > /tmp/oc-bad-format
	@curl -i -s -X POST $(CONVERT_URL) -F "file=@/tmp/oc-bad-format" \
		| head -20
	@rm -f /tmp/oc-bad-format

demo: ## RUN full URL-based smoke: health + bad-format + convert + docs URLs
	@printf "$(GREEN)═══ office-convert demo ═══$(RESET)\n\n"
	@printf "$(BLUE)1. Checking /health…$(RESET)\n"
	@$(MAKE) --no-print-directory health
	@printf "\n$(BLUE)2. POST PNG → expect 400 unsupported_format…$(RESET)\n"
	@$(MAKE) --no-print-directory test-bad-format
	@printf "\n$(BLUE)3. POST tests/corpus/simple.pdf → /tmp/output.pdf…$(RESET)\n"
	@if [ -f tests/corpus/simple.pdf ]; then \
		$(MAKE) --no-print-directory convert FILE=tests/corpus/simple.pdf; \
	else \
		printf "$(YELLOW)tests/corpus/simple.pdf missing. Run 'make corpus' first.$(RESET)\n"; \
	fi
	@printf "\n$(BLUE)4. API docs URLs:$(RESET)\n"
	@$(MAKE) --no-print-directory docs

# =============================================================================
# QA — quality gates run inside the test image
# =============================================================================
.PHONY: lint format-check format typecheck qa

lint: build-test ## QA ruff check
	docker run --rm $(IMAGE_TEST) ruff check .

format-check: build-test ## QA ruff format --check
	docker run --rm $(IMAGE_TEST) ruff format --check .

format: build-test ## QA ruff format (writes changes; mount workspace)
	docker run --rm -v $(PROJECT_DIR)/office_convert:/app/office_convert \
		-v $(PROJECT_DIR)/tests:/app/tests \
		$(IMAGE_TEST) ruff format .

typecheck: build-test ## QA mypy --strict on office_convert/
	docker run --rm $(IMAGE_TEST) mypy office_convert

qa: lint format-check typecheck test ## QA run lint + format-check + typecheck + tests

# =============================================================================
# CLEAN targets
# =============================================================================
.PHONY: clean clean-all

clean: down ## CLEAN remove containers and built images
	-docker rmi $(IMAGE_PROD) 2>/dev/null
	-docker rmi $(IMAGE_TEST) 2>/dev/null
	@printf "$(GREEN)Containers and images removed.$(RESET)\n"

clean-all: clean ## CLEAN also remove test/cache artifacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache .hypothesis htmlcov .coverage
	find . -type d -name __pycache__ -not -path './aidlc-docs/*' -exec rm -rf {} + 2>/dev/null || true
	@printf "$(GREEN)All artifacts removed.$(RESET)\n"

# =============================================================================
# Internal sanity checks — used by other targets
# =============================================================================
.PHONY: check-vendor check-license check-image

# Expected Linux x86_64 .so files for the 4-libs vendor path.
# (post-2026-05-12 SKU pivot — see aidlc-state.md).
WORDS_SO    := $(VENDOR_DIR)/Words/Aspose.Words.Cpp/lib/libAspose.Words.Cpp.so
CELLS_SO    := $(VENDOR_DIR)/Cells/Aspose.Cells/lib/linux_x86_64/libAspose.Cells.so
SLIDES_SO   := $(VENDOR_DIR)/Slides/Aspose.Slides.Cpp/lib/libAspose.Slides_x86_64_libstdcpp_libc2.23.so
PDF_SO_GLOB := $(VENDOR_DIR)/PDF/lib/libAspose.PDF.Cpp_*.so

check-vendor:
	@missing=0; \
	for path in $(WORDS_SO) $(CELLS_SO) $(SLIDES_SO); do \
		if [ ! -f "$$path" ]; then \
			printf "$(YELLOW)ERROR: $$path not found$(RESET)\n"; \
			missing=$$((missing + 1)); \
		fi; \
	done; \
	if ! ls $(PDF_SO_GLOB) >/dev/null 2>&1; then \
		printf "$(YELLOW)ERROR: PDF .so not found matching $(PDF_SO_GLOB)$(RESET)\n"; \
		missing=$$((missing + 1)); \
	fi; \
	if [ $$missing -gt 0 ]; then \
		printf "\n$(YELLOW)Populate vendor/aspose/ by extracting Aspose product zips. See README.md \"SDK acquisition\".$(RESET)\n"; \
		exit 1; \
	fi

.PHONY: verify-vendor
verify-vendor: check-vendor ## BUILD verify the 4 Aspose vendor trees are structurally complete + Linux x86_64
	@printf "$(BLUE)Verifying vendor/aspose/ layout (Path B — 4 separate libs)...$(RESET)\n\n"
	@for product in Words Cells Slides PDF; do \
		printf "$(BLUE)── $$product ──$(RESET)\n"; \
		case $$product in \
			Words)  so=$(WORDS_SO);;  \
			Cells)  so=$(CELLS_SO);;  \
			Slides) so=$(SLIDES_SO);; \
			PDF)    so=$$(ls $(PDF_SO_GLOB) 2>/dev/null | head -1);; \
		esac; \
		if [ -f "$$so" ]; then \
			size=$$(du -h "$$so" | cut -f1); \
			platform=$$(file -b "$$so"); \
			printf "  $(GREEN)✓$(RESET) $$so ($$size)\n"; \
			printf "    platform: $$platform\n"; \
			if echo "$$platform" | grep -q "ELF.*x86-64"; then \
				printf "    $(GREEN)✓ Linux x86_64 ELF$(RESET)\n"; \
			else \
				printf "    $(YELLOW)⚠ Unexpected platform — Dockerfile expects Linux x86_64.$(RESET)\n"; \
			fi; \
		else \
			printf "  $(YELLOW)⚠$(RESET) $$so MISSING\n"; \
		fi; \
	done
	@printf "\n$(BLUE)CMake config files present:$(RESET)\n"
	@for cfg in \
		$(VENDOR_DIR)/Words/Aspose.Words.Cpp/aspose.words.cpp-config.cmake \
		$(VENDOR_DIR)/Cells/Aspose.Cells/aspose.cells-config.cmake \
		$(VENDOR_DIR)/Slides/Aspose.Slides.Cpp/aspose.slides.cpp-config.cmake; do \
		if [ -f "$$cfg" ]; then \
			printf "  $(GREEN)✓$(RESET) $$cfg\n"; \
		else \
			printf "  $(YELLOW)⚠$(RESET) $$cfg MISSING\n"; \
		fi; \
	done
	@printf "  $(BLUE)(PDF has no CMake config — Dockerfile uses a manual IMPORTED target.)$(RESET)\n"
	@printf "\n$(GREEN)Verification done.$(RESET) If all checks above are green, run '$(YELLOW)make build$(RESET)'.\n"

check-license:
	@if [ ! -f $(LICENSE_FILE) ]; then \
		printf "$(YELLOW)ERROR: Aspose license not found at $(LICENSE_FILE)$(RESET)\n"; \
		printf "  Request at https://purchase.aspose.com/temporary-license\n"; \
		printf "  Save to $(LICENSE_FILE)\n"; \
		exit 1; \
	fi

check-image:
	@if ! docker image inspect $(IMAGE_PROD) >/dev/null 2>&1; then \
		printf "$(YELLOW)Production image $(IMAGE_PROD) not built. Run 'make build' first.$(RESET)\n"; \
		exit 1; \
	fi
