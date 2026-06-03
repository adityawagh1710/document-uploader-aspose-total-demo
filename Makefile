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
IMAGE_GO              ?= office-convert:go
IMAGE_UI              ?= office-convert-ui:dev
IMAGE_TEST            ?= office-convert:test
IMAGE_SMOKE_WORDS     ?= office-convert-smoke-words:dev
GO_IMAGE              ?= golang:1.26-bookworm
CONTAINER_NAME        ?= office-convert-dev
SMOKE_OUT_DIR         ?= /tmp/oc-smoke
PORT                  ?= 8080
LICENSE_FILE          ?= $(PROJECT_DIR)/Aspose.TotalforC++.lic
VENDOR_DIR            ?= $(PROJECT_DIR)/vendor/aspose
HEALTH_URL            := http://localhost:$(PORT)/health
CONVERT_URL           := http://localhost:$(PORT)/v1/convert
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
	@printf "\n$(BLUE)Deploy (EKS):$(RESET)\n"
	@awk 'BEGIN{FS=":.*## "} /^[a-z][a-z0-9_-]*:.*## .*DEPLOY/ {printf "  $(YELLOW)%-22s$(RESET) %s\n", $$1, $$2}' $(MAKEFILE_LIST) | sort
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

.PHONY: build-go test-go run-go up-go down-go
COMPOSE_GO := docker compose -f compose.yaml -f compose.go.yaml
build-go: check-vendor check-license ## BUILD Go orchestrator image (needs vendor/aspose/; runtime is Python-free)
	@printf "$(GREEN)Building Go image $(IMAGE_GO)...$(RESET)\n"
	docker build -t $(IMAGE_GO) -f go.Dockerfile .
	@printf "$(GREEN)Done. Run:$(RESET) make run-go\n"

test-go: ## TEST run the Go unit + property + integration suites (no Aspose needed; installs prlimit for worker tests)
	@printf "$(GREEN)Running Go tests in $(GO_IMAGE)...$(RESET)\n"
	docker run --rm -v $(PROJECT_DIR):/src -w /src $(GO_IMAGE) \
		sh -c "apt-get update -qq && apt-get install -y -qq util-linux >/dev/null && GOFLAGS=-mod=mod go test ./internal/... ./cmd/..."

golden-capture: ## TEST (re)capture Go/Python parity golden fixtures from the live Python oracle
	@printf "$(GREEN)Capturing golden fixtures from the Python oracle...$(RESET)\n"
	# Runs the in-process Python service (TestClient + fake worker) and freezes
	# its HTTP responses under internal/server/testdata/golden/. No Aspose; no
	# qpdf (the captured cases seed the stores directly — see capture_golden.py).
	# Kept qpdf-less to match the qpdf-less Go verify image so health status agrees.
	# The container runs as root (pip install needs it), so it chowns its output
	# back to the host user — otherwise the generated fixtures are root-owned and
	# block host-side git operations (e.g. branch switches).
	docker run --rm -v $(PROJECT_DIR):/work -w /work python:3.12-slim \
		sh -c "pip install -q -e . httpx && python scripts/capture_golden.py internal/server/testdata/golden && chown -R $(shell id -u):$(shell id -g) internal/server/testdata"

golden-verify: ## TEST replay golden fixtures against the Go orchestrator + diff (parity gate)
	@printf "$(GREEN)Verifying Go parity against golden fixtures...$(RESET)\n"
	docker run --rm -v $(PROJECT_DIR):/src -w /src $(GO_IMAGE) \
		sh -c "GOFLAGS=-mod=mod go test ./internal/server/ -run TestGoldenParity -v"

run-go: check-license ## RUN the Go image locally on $(PORT) (foreground; license bind-mounted, hardened posture)
	@printf "$(GREEN)Starting $(IMAGE_GO) on http://localhost:$(PORT) (Ctrl-C to stop)...$(RESET)\n"
	# -m/--memory-swap mirror the prod compose posture (4 GiB RAM + 2 GiB swap).
	#   WITHOUT a memory limit the cgroup reports "unlimited", so /v1/stats
	#   returns mem_max_bytes=0 and the UI's memory gauge has no denominator
	#   (shows blank). The limit makes memory utilization render.
	# WORKER_RAM_BYTES=6 GiB matches memswap_limit so prlimit RLIMIT_AS doesn't
	#   block swap (see config.py).
	# POOL_MIN_CHUNKS=1 forces pool mode on every conversion so per-job
	#   heartbeats (and the UI's per-job memory/timing/Gantt charts) populate
	#   even for small single-chunk files — handy for local dashboard demos.
	# HOME=/tmp + the fontconfig tmpfs let fontconfig write its cache under
	#   the read-only rootfs.
	docker run --rm -p 127.0.0.1:$(PORT):8080 \
		-m 4g --memory-swap 6g \
		-v $(LICENSE_FILE):/aspose/license.lic:ro \
		-e HOME=/tmp \
		-e OFFICE_CONVERT_WORKER_RAM_BYTES=6442450944 \
		-e OFFICE_CONVERT_POOL_MIN_CHUNKS=1 \
		--cap-drop=ALL --read-only \
		--tmpfs /tmp --tmpfs /var/run --tmpfs /var/cache/fontconfig \
		$(IMAGE_GO)

up-go: check-license ## RUN bring up the Go stack (backend + UI) via compose; builds if needed
	@printf "$(GREEN)Starting Go stack via compose (backend + UI)...$(RESET)\n"
	$(COMPOSE_GO) up -d --build
	@printf "$(GREEN)Up.$(RESET) API: http://localhost:$(PORT)  ·  UI: http://localhost:8501\n"

down-go: ## RUN tear down the Go compose stack (backend + UI + localstack)
	$(COMPOSE_GO) down

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
		pytest -n auto tests/unit tests/property tests/integration -v

test-unit: build-test ## TEST run unit tests only
	docker run --rm $(IMAGE_TEST) pytest -n auto tests/unit -v

test-property: build-test ## TEST run property-based tests only (Hypothesis)
	docker run --rm $(IMAGE_TEST) pytest -n auto tests/property -v

test-integration: build-test ## TEST run in-process integration tests
	docker run --rm $(IMAGE_TEST) pytest -n auto tests/integration -v

test-coverage: build-test ## TEST run all tests with 80% coverage gate
	docker run --rm $(IMAGE_TEST) \
		pytest -n auto --cov=office_convert --cov-fail-under=80 \
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
		$(IMAGE_TEST) sh -c "python -m tests.corpus._generate && chown -R $(shell id -u):$(shell id -g) /app/tests/corpus"
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
		$(IMAGE_TEST) sh -c "ruff format . && chown -R $(shell id -u):$(shell id -g) /app/office_convert /app/tests"

typecheck: build-test ## QA mypy --strict on office_convert/
	docker run --rm $(IMAGE_TEST) mypy office_convert

qa: lint format-check typecheck test update-test-badge ## QA run lint + format-check + typecheck + tests (auto-updates README badge)

update-test-badge: build-test ## QA refresh the tests-N badge in README from collected pytest count
	@count=$$(docker run --rm $(IMAGE_TEST) pytest --collect-only -q \
	    tests/unit tests/property tests/integration 2>/dev/null \
	    | tail -1 | grep -oE '^[0-9]+'); \
	if [ -n "$$count" ]; then \
	    sed -i.bak -E 's|tests-[0-9]+-brightgreen|tests-'"$$count"'-brightgreen|' README.md \
	    && rm -f README.md.bak; \
	    printf "$(GREEN)README test badge: $$count tests$(RESET)\n"; \
	else \
	    printf "$(YELLOW)Could not count tests; README badge left unchanged$(RESET)\n"; \
	fi

# =============================================================================
# DEPLOY targets — EKS dev cluster install / uninstall via Helm
# =============================================================================
# Required env vars before running `make deploy-dev`:
#   AWS_ACCOUNT_ID   e.g. 123456789012
#   AWS_REGION       e.g. us-east-1
#   IMAGE_TAG        e.g. $(git rev-parse --short HEAD)
# Optional:
#   NAMESPACE        default: office-convert-dev
#   HELM_RELEASE     default: office-convert
#   HELM_EXTRA_ARGS  extra `helm` flags, e.g. S3/IRSA enablement:
#                    HELM_EXTRA_ARGS="--set s3.enabled=true --set serviceAccount.roleArn=arn:..."
#                    See deploy/iam/README.md.
.PHONY: deploy-dev _deploy-dev-impl undeploy-dev _undeploy-dev-impl deploy-status deploy-logs _print-aws-urls tag-resources irsa-smoketest

NAMESPACE       ?= office-convert-dev
HELM_RELEASE    ?= office-convert
HELM_EXTRA_ARGS ?=
ECR_REPO         = $(AWS_ACCOUNT_ID).dkr.ecr.$(AWS_REGION).amazonaws.com/office-convert
ECR_REPO_UI      = $(AWS_ACCOUNT_ID).dkr.ecr.$(AWS_REGION).amazonaws.com/office-convert-ui
DEPLOY_LOG_DIR  ?= $(PROJECT_DIR)/deploy/logs
# Route 53 zone for the ALB Ingress A-aliases. Default matches the office-convert
# chart's `ingress.uiHost` / `apiHost` (both under dev05.k8s.opus2dev.com). Exported
# so deploy/scripts/route53-{upsert,delete}.sh inherit it.
HOSTED_ZONE_ID  ?= Z045669519R5D9D8CKC79
export HOSTED_ZONE_ID

deploy-dev: ## DEPLOY install office-convert to EKS dev cluster (logs to deploy/logs/; needs AWS_ACCOUNT_ID, AWS_REGION, IMAGE_TAG)
	@if [ -z "$(AWS_ACCOUNT_ID)" ] || [ -z "$(AWS_REGION)" ] || [ -z "$(IMAGE_TAG)" ]; then \
	    printf "$(YELLOW)ERROR: set AWS_ACCOUNT_ID, AWS_REGION, IMAGE_TAG env vars.$(RESET)\n"; \
	    printf "  Example: AWS_ACCOUNT_ID=123456789012 AWS_REGION=us-east-1 IMAGE_TAG=\$$(git rev-parse --short HEAD) make deploy-dev\n"; \
	    exit 1; \
	fi
	@command -v helm >/dev/null 2>&1 || { \
	    printf "$(YELLOW)ERROR: helm not found on PATH.$(RESET)\n"; \
	    printf "  Install via:  sudo snap install helm --classic\n"; \
	    printf "  Or:           curl -fsSL https://get.helm.sh/helm-v3.16.2-linux-amd64.tar.gz | sudo tar -xzC /usr/local/bin --strip-components=1 linux-amd64/helm\n"; \
	    exit 1; \
	}
	@command -v kubectl >/dev/null 2>&1 || { printf "$(YELLOW)ERROR: kubectl not found on PATH.$(RESET)\n"; exit 1; }
	@command -v aws >/dev/null 2>&1 || { printf "$(YELLOW)ERROR: aws CLI not found on PATH.$(RESET)\n"; exit 1; }
	@mkdir -p $(DEPLOY_LOG_DIR)
	@TS=$$(date +%Y%m%d-%H%M%S); \
	LOG=$(DEPLOY_LOG_DIR)/deploy-$$TS.log; \
	MANIFEST=$(DEPLOY_LOG_DIR)/manifest-$$TS.yaml; \
	printf "$(GREEN)Logging full deploy to: $$LOG$(RESET)\n"; \
	printf "$(GREEN)Rendered manifest:      $$MANIFEST$(RESET)\n\n"; \
	set -eo pipefail; \
	{ \
	    echo "============================================================"; \
	    echo "deploy-dev started at $$(date -Iseconds)"; \
	    echo "Git SHA:        $$(git rev-parse HEAD)"; \
	    echo "Git branch:     $$(git rev-parse --abbrev-ref HEAD)"; \
	    echo "Working tree:   $$(git status --short | wc -l) modified files"; \
	    echo "kubectl ctx:    $$(kubectl config current-context)"; \
	    echo "AWS account:    $(AWS_ACCOUNT_ID)"; \
	    echo "AWS region:     $(AWS_REGION)"; \
	    echo "AWS profile:    $${AWS_PROFILE:-default}"; \
	    echo "AWS identity:   $$(aws sts get-caller-identity --profile $${AWS_PROFILE:-default} --output text 2>&1 | tr '\n' ' ')"; \
	    echo "Image tag:      $(IMAGE_TAG)"; \
	    echo "ECR repo (API): $(ECR_REPO)"; \
	    echo "ECR repo (UI):  $(ECR_REPO_UI)"; \
	    echo "Namespace:      $(NAMESPACE)"; \
	    echo "Helm release:   $(HELM_RELEASE)"; \
	    echo "============================================================"; \
	    echo ""; \
	    # tee instead of `>`: snap-installed helm sandboxes stdout, so a plain \
	    # file redirect lands an empty file. tee captures the stream correctly. \
	    helm template $(HELM_RELEASE) deploy/helm/office-convert \
	        --namespace $(NAMESPACE) \
	        --set image.repository=$(ECR_REPO) \
	        --set image.tag=$(IMAGE_TAG) $(HELM_EXTRA_ARGS) 2>&1 | tee $$MANIFEST > /dev/null; \
	    echo "Wrote rendered manifest: $$MANIFEST"; \
	    echo ""; \
	    $(MAKE) _deploy-dev-impl; \
	    echo ""; \
	    echo "============================================================"; \
	    echo "deploy-dev finished at $$(date -Iseconds)"; \
	    echo "Final state:"; \
	    kubectl get pods,svc,configmap,secret -n $(NAMESPACE); \
	    echo "============================================================"; \
	} 2>&1 | tee $$LOG

_deploy-dev-impl:
	@printf "$(GREEN)[1/8] ECR repos (create if missing)...$(RESET)\n"
	aws ecr describe-repositories --repository-names office-convert --region $(AWS_REGION) >/dev/null 2>&1 \
	    || aws ecr create-repository --repository-name office-convert --region $(AWS_REGION) --image-scanning-configuration scanOnPush=true
	aws ecr describe-repositories --repository-names office-convert-ui --region $(AWS_REGION) >/dev/null 2>&1 \
	    || aws ecr create-repository --repository-name office-convert-ui --region $(AWS_REGION) --image-scanning-configuration scanOnPush=true
	@printf "$(GREEN)[2/8] ECR login...$(RESET)\n"
	aws ecr get-login-password --region $(AWS_REGION) | docker login --username AWS --password-stdin $(AWS_ACCOUNT_ID).dkr.ecr.$(AWS_REGION).amazonaws.com
	@printf "$(GREEN)[3/8] Build + tag + push API image $(ECR_REPO):$(IMAGE_TAG)...$(RESET)\n"
	$(MAKE) build
	docker tag $(IMAGE_PROD) $(ECR_REPO):$(IMAGE_TAG)
	docker push $(ECR_REPO):$(IMAGE_TAG)
	@printf "$(GREEN)[4/8] Build + tag + push UI image $(ECR_REPO_UI):$(IMAGE_TAG)...$(RESET)\n"
	docker build -t $(IMAGE_UI) -f Dockerfile.ui .
	docker tag $(IMAGE_UI) $(ECR_REPO_UI):$(IMAGE_TAG)
	docker push $(ECR_REPO_UI):$(IMAGE_TAG)
	@printf "$(GREEN)[5/8] Namespace + license Secret...$(RESET)\n"
	kubectl create namespace $(NAMESPACE) --dry-run=client -o yaml | kubectl apply -f -
	kubectl create secret generic aspose-license --from-file=license.lic=$(LICENSE_FILE) --namespace $(NAMESPACE) --dry-run=client -o yaml | kubectl apply -f -
	@printf "$(GREEN)[6/8] Undeploy-first + helm install...$(RESET)\n"
	@# Helm 4 server-side apply refuses to overwrite fields owned by live
	@# `kubectl patch`/`annotate` edits (post-deploy MAX_JOBS/PARALLEL + Ingress
	@# CIDR patches), so an in-place upgrade fails with field-ownership conflicts.
	@# Uninstall any existing release first → the install below is a conflict-free
	@# FRESH install. No-op on a clean cluster; route53-upsert (step 7) re-points
	@# DNS at the freshly-provisioned ALB.
	@if helm status $(HELM_RELEASE) --namespace $(NAMESPACE) >/dev/null 2>&1; then \
	    printf "  existing release found — uninstalling for a conflict-free fresh install\n"; \
	    helm uninstall $(HELM_RELEASE) --namespace $(NAMESPACE) --wait 2>&1 | tail -2 || true; \
	else \
	    printf "  no existing release — fresh install\n"; \
	fi
	helm upgrade --install $(HELM_RELEASE) deploy/helm/office-convert \
	    --namespace $(NAMESPACE) \
	    --set image.repository=$(ECR_REPO) \
	    --set image.tag=$(IMAGE_TAG) \
	    --set ui.image.repository=$(ECR_REPO_UI) \
	    --set ui.image.tag=$(IMAGE_TAG) \
	    $(HELM_EXTRA_ARGS) \
	    --wait --timeout 5m
	@printf "$(GREEN)[7/8] Route 53 A-alias upsert...$(RESET)\n"
	@AWS_PROFILE=$${AWS_PROFILE:-} NAMESPACE=$(NAMESPACE) ./deploy/scripts/route53-upsert.sh
	@printf "$(GREEN)[8/8] Deploy complete. Pod status:$(RESET)\n"
	@kubectl get pods,svc,ingress -n $(NAMESPACE)
	@printf "\n$(BLUE)API NLB hostname (may take ~60s to populate):$(RESET)\n"
	@NLB=$$(kubectl get svc office-convert -n $(NAMESPACE) -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null); \
	if [ -n "$$NLB" ]; then printf "  $$NLB\n"; else printf "  (still provisioning, or already cut over to ClusterIP — check ALB Ingress instead)\n"; fi
	@printf "\n$(BLUE)ALB Ingress hostname:$(RESET)\n"
	@ALB=$$(kubectl get ingress office-convert-ui -n $(NAMESPACE) -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null); \
	if [ -n "$$ALB" ]; then printf "  $$ALB\n"; else printf "  (still provisioning)\n"; fi
	@printf "\n$(BLUE)Image digests (in ECR):$(RESET)\n"
	@DIGEST=$$(aws ecr describe-images --repository-name office-convert --image-ids imageTag=$(IMAGE_TAG) --region $(AWS_REGION) --query 'imageDetails[0].imageDigest' --output text 2>/dev/null); \
	if [ -n "$$DIGEST" ] && [ "$$DIGEST" != "None" ]; then printf "  API: $$DIGEST\n"; else printf "  API: (could not resolve)\n"; fi
	@DIGEST_UI=$$(aws ecr describe-images --repository-name office-convert-ui --image-ids imageTag=$(IMAGE_TAG) --region $(AWS_REGION) --query 'imageDetails[0].imageDigest' --output text 2>/dev/null); \
	if [ -n "$$DIGEST_UI" ] && [ "$$DIGEST_UI" != "None" ]; then printf "  UI:  $$DIGEST_UI\n"; else printf "  UI:  (could not resolve)\n"; fi
	@$(MAKE) _print-aws-urls
	@printf "\n$(BLUE)Access the API:$(RESET)\n"
	@API_HOST=$$(kubectl get ingress office-convert -n $(NAMESPACE) -o jsonpath='{.spec.rules[0].host}' 2>/dev/null); \
	if [ -n "$$API_HOST" ]; then \
	    printf "  Public ALB:    https://$$API_HOST/docs   (corp-CIDR allowlisted; see deploy/helm/office-convert/values.yaml ingress.inboundCidrs)\n"; \
	    printf "                 https://$$API_HOST/health\n"; \
	fi
	@printf "  In-VPC NLB:    curl http://<nlb-hostname>/health   (VPC-internal only; dormant alongside ALB until commit B)\n"
	@printf "  Port-forward:  kubectl port-forward -n $(NAMESPACE) svc/office-convert 18080:80\n"
	@printf "                 then http://localhost:18080/docs\n"
	@printf "\n$(BLUE)Access the UI (Streamlit dashboard):$(RESET)\n"
	@UI_HOST=$$(kubectl get ingress office-convert-ui -n $(NAMESPACE) -o jsonpath='{.spec.rules[0].host}' 2>/dev/null); \
	if [ -n "$$UI_HOST" ]; then \
	    printf "  Public ALB:    https://$$UI_HOST/   (corp-CIDR allowlisted)\n"; \
	fi
	@printf "  Port-forward:  kubectl port-forward -n $(NAMESPACE) svc/office-convert-ui 8501:8501\n"
	@printf "                 then http://localhost:8501\n"

# Print AWS console deep-links for the deployed resources. Called by both
# deploy-dev (post-install) and undeploy-dev (post-delete) so the log
# captures clickable URLs for visual verification in the AWS web console.
_print-aws-urls:
	@printf "\n$(BLUE)============================================================$(RESET)\n"
	@printf "$(BLUE) AWS Console deep-links (open in browser to verify)$(RESET)\n"
	@printf "$(BLUE)============================================================$(RESET)\n"
	@CLUSTER=$$(kubectl config current-context | awk -F/ '{print $$NF}'); \
	IMAGE_DIGEST=$$(aws ecr describe-images --repository-name office-convert --image-ids imageTag=$(IMAGE_TAG) --region $(AWS_REGION) --query 'imageDetails[0].imageDigest' --output text 2>/dev/null); \
	NLB_HOST=$$(kubectl get svc -n $(NAMESPACE) -l app.kubernetes.io/instance=$(HELM_RELEASE) -o jsonpath='{.items[0].status.loadBalancer.ingress[0].hostname}' 2>/dev/null); \
	printf "  $(YELLOW)EKS cluster:$(RESET)\n"; \
	printf "    https://$(AWS_REGION).console.aws.amazon.com/eks/home?region=$(AWS_REGION)#/clusters/$$CLUSTER\n\n"; \
	printf "  $(YELLOW)EKS workloads in namespace $(NAMESPACE):$(RESET)\n"; \
	printf "    https://$(AWS_REGION).console.aws.amazon.com/eks/home?region=$(AWS_REGION)#/clusters/$$CLUSTER/workloads?namespace=$(NAMESPACE)\n\n"; \
	printf "  $(YELLOW)EKS pods in namespace $(NAMESPACE):$(RESET)\n"; \
	printf "    https://$(AWS_REGION).console.aws.amazon.com/eks/home?region=$(AWS_REGION)#/clusters/$$CLUSTER/pods?namespace=$(NAMESPACE)\n\n"; \
	printf "  $(YELLOW)ECR repository office-convert:$(RESET)\n"; \
	printf "    https://$(AWS_REGION).console.aws.amazon.com/ecr/repositories/private/$(AWS_ACCOUNT_ID)/office-convert?region=$(AWS_REGION)\n\n"; \
	if [ -n "$$IMAGE_DIGEST" ] && [ "$$IMAGE_DIGEST" != "None" ]; then \
	    printf "  $(YELLOW)This image (tag=$(IMAGE_TAG)):$(RESET)\n"; \
	    printf "    https://$(AWS_REGION).console.aws.amazon.com/ecr/repositories/private/$(AWS_ACCOUNT_ID)/office-convert/_/image/$$IMAGE_DIGEST/details?region=$(AWS_REGION)\n\n"; \
	fi; \
	printf "  $(YELLOW)EC2 Load Balancers (find the NLB by tag):$(RESET)\n"; \
	printf "    https://$(AWS_REGION).console.aws.amazon.com/ec2/home?region=$(AWS_REGION)#LoadBalancers:\n\n"; \
	if [ -n "$$NLB_HOST" ]; then \
	    printf "  $(YELLOW)NLB hostname (live):$(RESET)\n"; \
	    printf "    http://$$NLB_HOST\n\n"; \
	fi; \
	printf "  $(YELLOW)CloudWatch log groups (if container logs are shipped):$(RESET)\n"; \
	printf "    https://$(AWS_REGION).console.aws.amazon.com/cloudwatch/home?region=$(AWS_REGION)#logsV2:log-groups\n\n"; \
	printf "  $(YELLOW)IAM (verify nothing changed):$(RESET)\n"; \
	printf "    https://us-east-1.console.aws.amazon.com/iamv2/home#/roles\n"
	@printf "$(BLUE)============================================================$(RESET)\n"

undeploy-dev: ## DEPLOY uninstall office-convert from EKS dev cluster (full revert; logs to deploy/logs/)
	@command -v helm >/dev/null 2>&1 || { printf "$(YELLOW)ERROR: helm not found on PATH (needed for helm uninstall).$(RESET)\n"; exit 1; }
	@command -v kubectl >/dev/null 2>&1 || { printf "$(YELLOW)ERROR: kubectl not found on PATH.$(RESET)\n"; exit 1; }
	@mkdir -p $(DEPLOY_LOG_DIR)
	@TS=$$(date +%Y%m%d-%H%M%S); \
	LOG=$(DEPLOY_LOG_DIR)/undeploy-$$TS.log; \
	printf "$(GREEN)Logging full undeploy to: $$LOG$(RESET)\n\n"; \
	set -eo pipefail; \
	{ \
	    echo "============================================================"; \
	    echo "undeploy-dev started at $$(date -Iseconds)"; \
	    echo "kubectl ctx:    $$(kubectl config current-context)"; \
	    echo "Namespace:      $(NAMESPACE)"; \
	    echo "Helm release:   $(HELM_RELEASE)"; \
	    echo "State BEFORE undeploy:"; \
	    kubectl get pods,svc,configmap,secret -n $(NAMESPACE) 2>&1 || true; \
	    echo "============================================================"; \
	    echo ""; \
	    $(MAKE) _undeploy-dev-impl; \
	    echo ""; \
	    echo "============================================================"; \
	    echo "undeploy-dev finished at $$(date -Iseconds)"; \
	    echo "State AFTER undeploy (should be empty / NotFound):"; \
	    kubectl get all -n $(NAMESPACE) 2>&1 || true; \
	    echo "============================================================"; \
	} 2>&1 | tee $$LOG

_undeploy-dev-impl:
	@printf "$(GREEN)[1/4] Route 53 A-alias delete (BEFORE helm uninstall so ALB still exists)...$(RESET)\n"
	-AWS_PROFILE=$${AWS_PROFILE:-} ./deploy/scripts/route53-delete.sh
	@printf "$(GREEN)[2/4] helm uninstall...$(RESET)\n"
	-helm uninstall $(HELM_RELEASE) --namespace $(NAMESPACE)
	@printf "$(GREEN)[3/4] delete license Secret...$(RESET)\n"
	-kubectl delete secret aspose-license --namespace $(NAMESPACE) --ignore-not-found
	@printf "$(GREEN)[4/4] delete namespace $(NAMESPACE)...$(RESET)\n"
	-kubectl delete namespace $(NAMESPACE) --ignore-not-found
	@printf "$(GREEN)Undeploy complete. ECR images still exist (kept by design):$(RESET)\n"
	@printf "    $(ECR_REPO):$(IMAGE_TAG)\n"
	@printf "    $(ECR_REPO_UI):$(IMAGE_TAG)\n"
	@printf "  To delete the ECR images:\n"
	@printf "    aws ecr batch-delete-image --repository-name office-convert    --image-ids imageTag=$(IMAGE_TAG) --region $(AWS_REGION)\n"
	@printf "    aws ecr batch-delete-image --repository-name office-convert-ui --image-ids imageTag=$(IMAGE_TAG) --region $(AWS_REGION)\n"
	@printf "  To delete the ECR repos entirely:\n"
	@printf "    aws ecr delete-repository --repository-name office-convert    --force --region $(AWS_REGION)\n"
	@printf "    aws ecr delete-repository --repository-name office-convert-ui --force --region $(AWS_REGION)\n"
	@$(MAKE) _print-aws-urls
	@printf "\n$(YELLOW)Verify in AWS console:$(RESET)\n"
	@printf "  - EKS workloads URL should show 'No items' for namespace $(NAMESPACE)\n"
	@printf "  - Load Balancers URL should no longer list our NLBs OR ALB (~60s each to deprovision)\n"
	@printf "  - Route 53 hosted zone $(HOSTED_ZONE_ID) should no longer list the UI/API A-aliases\n"
	@printf "  - ECR repository URL should still show image tag $(IMAGE_TAG) (kept by design)\n"

deploy-status: ## DEPLOY show deployment status (pods, service, NLB)
	@kubectl get pods,svc,configmap,secret -n $(NAMESPACE) 2>&1 || printf "$(YELLOW)Namespace $(NAMESPACE) not found (not deployed yet).$(RESET)\n"

deploy-logs: ## DEPLOY tail pod logs
	@kubectl logs -n $(NAMESPACE) -l app.kubernetes.io/instance=$(HELM_RELEASE) --tail=200 -f

# Out-of-band S3 resources (buckets, IRSA role, ECR repos) — IRSA role ARN and
# bucket names default to the dev05 set provisioned 2026-05-27. See deploy/iam/.
S3_IRSA_ROLE_ARN   ?= arn:aws:iam::537462380503:role/office-convert-dev-s3
S3_INPUT_BUCKET    ?= office-convert-dev-sandbox-input
S3_OUTPUT_BUCKET   ?= office-convert-dev-sandbox-output

tag-resources: ## DEPLOY tag out-of-band S3 buckets + IRSA role + ECR repos (org tag schema)
	@AWS_PROFILE=$${AWS_PROFILE:-opus2-dev} AWS_REGION=$(AWS_REGION) AWS_ACCOUNT_ID=$(AWS_ACCOUNT_ID) \
	    S3_INPUT_BUCKET=$(S3_INPUT_BUCKET) S3_OUTPUT_BUCKET=$(S3_OUTPUT_BUCKET) \
	    ECR_REPO=office-convert ECR_REPO_UI=office-convert-ui \
	    bash deploy/scripts/tag-resources.sh

irsa-smoketest: ## DEPLOY pre-flight: assume the IRSA role from a throwaway pod (no app deploy). Needs S3_IRSA_ROLE_ARN
	@if [ -z "$(S3_IRSA_ROLE_ARN)" ]; then printf "$(YELLOW)ERROR: set S3_IRSA_ROLE_ARN=<role-arn> (see deploy/iam/README.md)$(RESET)\n"; exit 1; fi
	@printf "$(GREEN)IRSA pre-flight → ns=$(NAMESPACE) sa=$(HELM_RELEASE) bucket=$(S3_OUTPUT_BUCKET)$(RESET)\n"
	@kubectl get ns $(NAMESPACE) >/dev/null 2>&1 || kubectl create ns $(NAMESPACE) >/dev/null
	@# The SA must be named like the deployment SA — the trust policy is scoped
	@# to system:serviceaccount:$(NAMESPACE):$(HELM_RELEASE). If a (Helm-managed)
	@# SA already exists we leave it intact; only a SA WE created for the test is
	@# removed afterwards, so this is safe to run against a live deployment.
	@PRE=$$(kubectl -n $(NAMESPACE) get sa $(HELM_RELEASE) -o name 2>/dev/null || true); \
	kubectl -n $(NAMESPACE) create serviceaccount $(HELM_RELEASE) --dry-run=client -o yaml | kubectl apply -f - >/dev/null; \
	kubectl -n $(NAMESPACE) annotate serviceaccount $(HELM_RELEASE) eks.amazonaws.com/role-arn=$(S3_IRSA_ROLE_ARN) --overwrite >/dev/null; \
	kubectl run irsa-smoketest -n $(NAMESPACE) \
	    --overrides='{"spec":{"serviceAccountName":"$(HELM_RELEASE)"}}' \
	    --image=amazon/aws-cli:2.17.0 --restart=Never --rm -i --command -- \
	    sh -c 'aws sts get-caller-identity && aws s3api put-object --bucket $(S3_OUTPUT_BUCKET) --key _irsa_smoketest.txt --body /etc/hostname >/dev/null && echo S3_WRITE_OK'; \
	    rc=$$?; \
	    if [ -z "$$PRE" ]; then kubectl -n $(NAMESPACE) delete serviceaccount $(HELM_RELEASE) --ignore-not-found >/dev/null 2>&1; SA_NOTE="temp SA removed"; else SA_NOTE="pre-existing SA left intact"; fi; \
	    if [ $$rc -eq 0 ]; then printf "$(GREEN)✓ IRSA OK — role assumable + S3 write reachable ($$SA_NOTE)$(RESET)\n"; \
	    else printf "$(YELLOW)✗ IRSA smoke test FAILED (rc=$$rc) — check OIDC trust, SA name/ns, role policy, pod egress$(RESET)\n"; fi; \
	    exit $$rc

# Out-of-band data teardown (DESTRUCTIVE) — Helm can't manage these, so neither
# can `undeploy-dev`. Guarded by NUKE_DATA=true. Role name + inline policy
# default to the dev05 set (deploy/iam/).
S3_IRSA_ROLE_NAME   ?= office-convert-dev-s3
S3_IRSA_POLICY_NAME ?= office-convert-s3
.PHONY: check-nuke nuke-data undeploy-all

check-nuke:
	@if [ "$(NUKE_DATA)" != "true" ]; then \
	    printf "$(YELLOW)✗ This DESTROYS persistent S3 data + the IRSA role — refused without explicit confirmation.$(RESET)\n"; \
	    printf "  It would permanently delete (region $${AWS_REGION:-eu-west-1}, profile $${AWS_PROFILE:-opus2-dev}):\n"; \
	    printf "    - S3 bucket  $(S3_INPUT_BUCKET)   (+ every object)\n"; \
	    printf "    - S3 bucket  $(S3_OUTPUT_BUCKET)  (+ every object)\n"; \
	    printf "    - IAM role   $(S3_IRSA_ROLE_NAME) (inline policy $(S3_IRSA_POLICY_NAME))\n"; \
	    printf "  App-only teardown (keeps data): $(YELLOW)make undeploy-dev$(RESET).\n"; \
	    printf "  To really destroy data, re-run with $(YELLOW)NUKE_DATA=true$(RESET).\n"; \
	    exit 1; \
	fi

nuke-data: check-nuke ## DEPLOY DANGER: destroy out-of-band S3 buckets + IRSA role (needs NUKE_DATA=true)
	@printf "$(YELLOW)NUKING out-of-band data — S3 buckets + IRSA role$(RESET)\n"
	@printf "$(YELLOW)→ aws s3 rb s3://$(S3_INPUT_BUCKET) --force$(RESET)\n"
	-@AWS_PROFILE=$${AWS_PROFILE:-opus2-dev} aws s3 rb s3://$(S3_INPUT_BUCKET) --force --region $${AWS_REGION:-eu-west-1} 2>&1 | tail -3
	@printf "$(YELLOW)→ aws s3 rb s3://$(S3_OUTPUT_BUCKET) --force$(RESET)\n"
	-@AWS_PROFILE=$${AWS_PROFILE:-opus2-dev} aws s3 rb s3://$(S3_OUTPUT_BUCKET) --force --region $${AWS_REGION:-eu-west-1} 2>&1 | tail -3
	@printf "$(YELLOW)→ delete IAM role $(S3_IRSA_ROLE_NAME) (inline policy first)$(RESET)\n"
	-@AWS_PROFILE=$${AWS_PROFILE:-opus2-dev} aws iam delete-role-policy --role-name $(S3_IRSA_ROLE_NAME) --policy-name $(S3_IRSA_POLICY_NAME) 2>/dev/null || true
	-@AWS_PROFILE=$${AWS_PROFILE:-opus2-dev} aws iam delete-role --role-name $(S3_IRSA_ROLE_NAME) 2>&1 | tail -2
	@printf "$(GREEN)Out-of-band data resources destroyed.$(RESET)\n"

undeploy-all: check-nuke undeploy-dev nuke-data ## DEPLOY DANGER: full teardown INCL. DATA — undeploy-dev + destroy buckets/role (needs NUKE_DATA=true)
	@printf "$(GREEN)Full teardown complete (release + out-of-band data).$(RESET)\n"

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

# Expected Linux x86_64 .so files for the 5-libs vendor path.
# (post-2026-05-12 SKU pivot — see aidlc-state.md; Email added 2026-05-26).
WORDS_SO    := $(VENDOR_DIR)/Words/Aspose.Words.Cpp/lib/libAspose.Words.Cpp.so
CELLS_SO    := $(VENDOR_DIR)/Cells/Aspose.Cells/lib/linux_x86_64/libAspose.Cells.so
SLIDES_SO   := $(VENDOR_DIR)/Slides/Aspose.Slides.Cpp/lib/libAspose.Slides_x86_64_libstdcpp_libc2.23.so
PDF_SO_GLOB := $(VENDOR_DIR)/PDF/lib/libAspose.PDF.Cpp_*.so
EMAIL_SO    := $(VENDOR_DIR)/Email/lib/libAspose.Email.Cpp_gcc.so

check-vendor:
	@missing=0; \
	for path in $(WORDS_SO) $(CELLS_SO) $(SLIDES_SO) $(EMAIL_SO); do \
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
verify-vendor: check-vendor ## BUILD verify the 5 Aspose vendor trees are structurally complete + Linux x86_64
	@printf "$(BLUE)Verifying vendor/aspose/ layout (Path B — 5 separate libs)...$(RESET)\n\n"
	@for product in Words Cells Slides PDF Email; do \
		printf "$(BLUE)── $$product ──$(RESET)\n"; \
		case $$product in \
			Words)  so=$(WORDS_SO);;  \
			Cells)  so=$(CELLS_SO);;  \
			Slides) so=$(SLIDES_SO);; \
			PDF)    so=$$(ls $(PDF_SO_GLOB) 2>/dev/null | head -1);; \
			Email)  so=$(EMAIL_SO);; \
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
		$(VENDOR_DIR)/Slides/Aspose.Slides.Cpp/aspose.slides.cpp-config.cmake \
		$(VENDOR_DIR)/Email/aspose.email.cpp-config.cmake; do \
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
