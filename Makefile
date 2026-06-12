# Makefile — Docker-first workflow for office-convert.
#
# Everything runs inside containers. The only host requirements are
# `docker` and `make` itself.
#
# Quick reference (run `make help` for the full list):
#   make build             # build the API image (needs Aspose SDK + license)
#   make test-go           # run Go unit + golden parity tests
#   make up                # start the full stack (API + UI + Gotenberg + LocalStack)
#   make health            # GET /health via curl
#   make convert FILE=testdata/corpus/simple.pdf  # POST a doc; saves /tmp/output.pdf
#   make demo              # end-to-end smoke via curl
#   make down              # stop the service
#   make ui-install        # npm ci in ui/
#   make ui-dev            # run Next.js dev server (host, port 3000)

# =============================================================================
# Configuration — override on the command line if needed:
#   make build PROJECT_DIR=/elsewhere
# =============================================================================
SHELL                 := /bin/bash
PROJECT_DIR           ?= $(shell pwd)
IMAGE_PROD            ?= office-convert:go
IMAGE_UI              ?= office-convert-ui:dev
GO_IMAGE              ?= golang:1.26-bookworm
CONTAINER_NAME        ?= office-convert-dev
SMOKE_OUT_DIR         ?= /tmp/oc-smoke
PORT                  ?= 8080
LICENSE_FILE          ?= $(PROJECT_DIR)/Aspose.TotalforC++.lic
VENDOR_DIR            ?= $(PROJECT_DIR)/vendor/aspose
HEALTH_URL            := http://localhost:$(PORT)/health
CONVERT_URL           := http://localhost:$(PORT)/v1/convert
DOCS_URL              := http://localhost:$(PORT)/docs

# Pretty-print helper.
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
	@printf "\n$(BLUE)UI (Next.js):$(RESET)\n"
	@awk 'BEGIN{FS=":.*## "} /^[a-z][a-z0-9_-]*:.*## .*UI/ {printf "  $(YELLOW)%-22s$(RESET) %s\n", $$1, $$2}' $(MAKEFILE_LIST) | sort
	@printf "\n$(BLUE)Deploy (EKS):$(RESET)\n"
	@awk 'BEGIN{FS=":.*## "} /^[a-z][a-z0-9_-]*:.*## .*DEPLOY/ {printf "  $(YELLOW)%-22s$(RESET) %s\n", $$1, $$2}' $(MAKEFILE_LIST) | sort
	@printf "\n$(BLUE)Cleanup:$(RESET)\n"
	@awk 'BEGIN{FS=":.*## "} /^[a-z][a-z0-9_-]*:.*## .*CLEAN/ {printf "  $(YELLOW)%-22s$(RESET) %s\n", $$1, $$2}' $(MAKEFILE_LIST) | sort
	@printf "\n$(BLUE)Quick start:$(RESET)\n"
	@printf "  $(YELLOW)make test-go$(RESET)                          # run Go tests (no Aspose needed)\n"
	@printf "  $(YELLOW)make build$(RESET) && $(YELLOW)make up$(RESET) && $(YELLOW)make demo$(RESET)  # full pipeline (needs Aspose SDK + license)\n"
	@printf "  $(YELLOW)make ui-install$(RESET) && $(YELLOW)make ui-dev$(RESET)    # UI development (hot reload on :3000)\n"

# =============================================================================
# BUILD targets
# =============================================================================
.PHONY: build

build: check-vendor check-license ## BUILD Go orchestrator Docker image (needs vendor/aspose/ + license)
	@printf "$(GREEN)Building production image $(IMAGE_PROD)...$(RESET)\n"
	docker build -t $(IMAGE_PROD) .
	@printf "$(GREEN)Done. Run:$(RESET) make up\n"

.PHONY: smoke-words
smoke-words: check-license check-vendor-words ## BUILD smoke-test Aspose.Words license + Linux .so (pre-integration validation)
	@printf "$(GREEN)Building Words smoke-test image...$(RESET)\n"
	docker build -t office-convert-smoke-words:dev -f smoke_test/Dockerfile.smoke .
	@mkdir -p $(SMOKE_OUT_DIR)
	@printf "\n$(GREEN)Running smoke test...$(RESET)\n"
	@docker run --rm \
		-v $(LICENSE_FILE):/license/license.lic:ro \
		-v $(SMOKE_OUT_DIR):/out \
		office-convert-smoke-words:dev \
		/license/license.lic /out/words_smoke.pdf
	@printf "\n$(BLUE)Output file:$(RESET)\n"
	@file $(SMOKE_OUT_DIR)/words_smoke.pdf 2>/dev/null || ls -la $(SMOKE_OUT_DIR)/words_smoke.pdf
	@printf "\n$(YELLOW)IMPORTANT: open $(SMOKE_OUT_DIR)/words_smoke.pdf and verify NO watermark.$(RESET)\n"

.PHONY: check-vendor-words
check-vendor-words:
	@if [ ! -f vendor/aspose/Words/Aspose.Words.Cpp/lib/libAspose.Words.Cpp.so ]; then \
		printf "$(YELLOW)ERROR: vendor/aspose/Words/Aspose.Words.Cpp/lib/libAspose.Words.Cpp.so not found.$(RESET)\n"; \
		exit 1; \
	fi

# =============================================================================
# TEST targets
# =============================================================================
.PHONY: test-go golden-verify

test-go: ## TEST run the Go unit + golden parity suites (no Aspose needed)
	@printf "$(GREEN)Running Go tests in $(GO_IMAGE)...$(RESET)\n"
	docker run --rm -v $(PROJECT_DIR):/src -w /src $(GO_IMAGE) \
		sh -c "apt-get update -qq && apt-get install -y -qq util-linux >/dev/null && GOFLAGS=-mod=mod go test ./internal/... ./cmd/..."

golden-verify: ## TEST replay golden fixtures against the Go orchestrator (parity gate)
	@printf "$(GREEN)Verifying Go parity against golden fixtures...$(RESET)\n"
	docker run --rm -v $(PROJECT_DIR):/src -w /src $(GO_IMAGE) \
		sh -c "GOFLAGS=-mod=mod go test ./internal/server/ -run TestGoldenParity -v"

# =============================================================================
# UI targets — Next.js dev workflow (host-side, no Docker needed)
# =============================================================================
.PHONY: ui-install ui-dev ui-build ui-lint

ui-install: ## UI install Next.js dependencies (npm ci in ui/)
	npm --prefix ui ci

ui-dev: ## UI start Next.js dev server on :3000 (hot reload; API_URL must point at a running API)
	npm --prefix ui run dev

ui-build: ## UI production build (standalone output in ui/.next/)
	npm --prefix ui run build

ui-lint: ## UI run ESLint + tsc type check
	npm --prefix ui run lint
	npm --prefix ui run typecheck

# =============================================================================
# RUN targets — start the stack and hit URLs via curl
# =============================================================================
.PHONY: up down restart logs shell health docs convert test-bad-format demo run

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
			echo "  UI:      http://localhost:8501"; \
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

shell: ## RUN open a shell inside the running API container
	docker compose exec office-convert /bin/bash 2>/dev/null \
		|| docker compose exec office-convert /bin/sh

run: check-license ## RUN Go image locally on $(PORT) (foreground; hardened posture)
	@printf "$(GREEN)Starting $(IMAGE_PROD) on http://localhost:$(PORT) (Ctrl-C to stop)...$(RESET)\n"
	docker run --rm -p 127.0.0.1:$(PORT):8080 \
		-m 4g --memory-swap 6g \
		-v $(LICENSE_FILE):/aspose/license.lic:ro \
		-e HOME=/tmp \
		-e OFFICE_CONVERT_WORKER_RAM_BYTES=6442450944 \
		-e OFFICE_CONVERT_POOL_MIN_CHUNKS=1 \
		--cap-drop=ALL --read-only \
		--tmpfs /tmp --tmpfs /var/run --tmpfs /var/cache/fontconfig \
		$(IMAGE_PROD)

health: ## RUN GET /health via curl
	@printf "$(BLUE)GET $(HEALTH_URL)$(RESET)\n"
	@curl -fsS $(HEALTH_URL) | python3 -m json.tool 2>/dev/null \
		|| curl -fsS $(HEALTH_URL) \
		|| (printf "$(YELLOW)Service not responding. Run 'make up' first.$(RESET)\n"; exit 1)

docs: ## RUN print URLs for Swagger / ReDoc / OpenAPI
	@printf "$(BLUE)API Documentation URLs:$(RESET)\n"
	@echo "  Swagger UI: $(DOCS_URL)"
	@printf "$(BLUE)\nOpen in a browser to explore the API interactively.$(RESET)\n"

convert: ## RUN POST FILE=<path> to /convert; output to /tmp/output.pdf
ifndef FILE
	@printf "$(YELLOW)Usage: make convert FILE=path/to/document.docx$(RESET)\n"
	@printf "Example: make convert FILE=testdata/corpus/simple.pdf\n"
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
	@printf "\n$(BLUE)3. POST testdata/corpus/simple.pdf → /tmp/output.pdf…$(RESET)\n"
	@if [ -f testdata/corpus/simple.pdf ]; then \
		$(MAKE) --no-print-directory convert FILE=testdata/corpus/simple.pdf; \
	else \
		printf "$(YELLOW)testdata/corpus/simple.pdf not found.$(RESET)\n"; \
	fi
	@printf "\n$(BLUE)4. API docs URL:$(RESET)\n"
	@$(MAKE) --no-print-directory docs

# =============================================================================
# DEPLOY targets — EKS dev cluster install / uninstall via Helm
# =============================================================================
.PHONY: deploy-dev _deploy-dev-impl undeploy-dev _undeploy-dev-impl deploy-status deploy-logs _print-aws-urls tag-resources irsa-smoketest

NAMESPACE       ?= office-convert-dev
HELM_RELEASE    ?= office-convert
HELM_EXTRA_ARGS ?=
ECR_REPO         = $(AWS_ACCOUNT_ID).dkr.ecr.$(AWS_REGION).amazonaws.com/office-convert
ECR_REPO_UI      = $(AWS_ACCOUNT_ID).dkr.ecr.$(AWS_REGION).amazonaws.com/office-convert-ui
DEPLOY_LOG_DIR  ?= $(PROJECT_DIR)/deploy/logs
HOSTED_ZONE_ID  ?= Z045669519R5D9D8CKC79
export HOSTED_ZONE_ID

deploy-dev: ## DEPLOY install office-convert to EKS dev cluster (needs AWS_ACCOUNT_ID, AWS_REGION, IMAGE_TAG)
	@if [ -z "$(AWS_ACCOUNT_ID)" ] || [ -z "$(AWS_REGION)" ] || [ -z "$(IMAGE_TAG)" ]; then \
	    printf "$(YELLOW)ERROR: set AWS_ACCOUNT_ID, AWS_REGION, IMAGE_TAG env vars.$(RESET)\n"; \
	    exit 1; \
	fi
	@command -v helm >/dev/null 2>&1 || { printf "$(YELLOW)ERROR: helm not found on PATH.$(RESET)\n"; exit 1; }
	@command -v kubectl >/dev/null 2>&1 || { printf "$(YELLOW)ERROR: kubectl not found on PATH.$(RESET)\n"; exit 1; }
	@command -v aws >/dev/null 2>&1 || { printf "$(YELLOW)ERROR: aws CLI not found on PATH.$(RESET)\n"; exit 1; }
	@mkdir -p $(DEPLOY_LOG_DIR)
	@TS=$$(date +%Y%m%d-%H%M%S); \
	LOG=$(DEPLOY_LOG_DIR)/deploy-$$TS.log; \
	MANIFEST=$(DEPLOY_LOG_DIR)/manifest-$$TS.yaml; \
	printf "$(GREEN)Logging full deploy to: $$LOG$(RESET)\n"; \
	set -eo pipefail; \
	{ \
	    echo "deploy-dev started at $$(date -Iseconds)"; \
	    echo "Git SHA:     $$(git rev-parse HEAD)"; \
	    echo "Image tag:   $(IMAGE_TAG)"; \
	    helm template $(HELM_RELEASE) deploy/helm/office-convert \
	        --namespace $(NAMESPACE) \
	        --set image.repository=$(ECR_REPO) \
	        --set image.tag=$(IMAGE_TAG) $(HELM_EXTRA_ARGS) 2>&1 | tee $$MANIFEST > /dev/null; \
	    $(MAKE) _deploy-dev-impl; \
	} 2>&1 | tee $$LOG

_deploy-dev-impl:
	@printf "$(GREEN)[1/8] ECR repos (create if missing)...$(RESET)\n"
	aws ecr describe-repositories --repository-names office-convert --region $(AWS_REGION) >/dev/null 2>&1 \
	    || aws ecr create-repository --repository-name office-convert --region $(AWS_REGION) --image-scanning-configuration scanOnPush=true
	aws ecr describe-repositories --repository-names office-convert-ui --region $(AWS_REGION) >/dev/null 2>&1 \
	    || aws ecr create-repository --repository-name office-convert-ui --region $(AWS_REGION) --image-scanning-configuration scanOnPush=true
	@printf "$(GREEN)[2/8] ECR login...$(RESET)\n"
	aws ecr get-login-password --region $(AWS_REGION) | docker login --username AWS --password-stdin $(AWS_ACCOUNT_ID).dkr.ecr.$(AWS_REGION).amazonaws.com
	@printf "$(GREEN)[3/8] Build + push API image $(ECR_REPO):$(IMAGE_TAG)...$(RESET)\n"
	$(MAKE) build
	docker tag $(IMAGE_PROD) $(ECR_REPO):$(IMAGE_TAG)
	docker push $(ECR_REPO):$(IMAGE_TAG)
	@printf "$(GREEN)[4/8] Build + push UI image $(ECR_REPO_UI):$(IMAGE_TAG)...$(RESET)\n"
	docker build -t $(IMAGE_UI) ui/
	docker tag $(IMAGE_UI) $(ECR_REPO_UI):$(IMAGE_TAG)
	docker push $(ECR_REPO_UI):$(IMAGE_TAG)
	@printf "$(GREEN)[5/8] Namespace + license Secret...$(RESET)\n"
	kubectl create namespace $(NAMESPACE) --dry-run=client -o yaml | kubectl apply -f -
	kubectl create secret generic aspose-license --from-file=license.lic=$(LICENSE_FILE) --namespace $(NAMESPACE) --dry-run=client -o yaml | kubectl apply -f -
	@printf "$(GREEN)[6/8] Undeploy-first + helm install...$(RESET)\n"
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
	@printf "$(GREEN)[8/8] Deploy complete.$(RESET)\n"
	@kubectl get pods,svc,ingress -n $(NAMESPACE)
	@$(MAKE) _print-aws-urls

_print-aws-urls:
	@printf "\n$(BLUE)============================================================$(RESET)\n"
	@printf "$(BLUE) AWS Console deep-links$(RESET)\n"
	@printf "$(BLUE)============================================================$(RESET)\n"
	@CLUSTER=$$(kubectl config current-context | awk -F/ '{print $$NF}'); \
	printf "  EKS: https://$(AWS_REGION).console.aws.amazon.com/eks/home?region=$(AWS_REGION)#/clusters/$$CLUSTER\n"; \
	printf "  ECR: https://$(AWS_REGION).console.aws.amazon.com/ecr/repositories/private/$(AWS_ACCOUNT_ID)/office-convert?region=$(AWS_REGION)\n"

undeploy-dev: ## DEPLOY uninstall office-convert from EKS dev cluster
	@command -v helm >/dev/null 2>&1 || { printf "$(YELLOW)ERROR: helm not found on PATH.$(RESET)\n"; exit 1; }
	@mkdir -p $(DEPLOY_LOG_DIR)
	@TS=$$(date +%Y%m%d-%H%M%S); LOG=$(DEPLOY_LOG_DIR)/undeploy-$$TS.log; \
	printf "$(GREEN)Logging undeploy to: $$LOG$(RESET)\n\n"; \
	{ $(MAKE) _undeploy-dev-impl; } 2>&1 | tee $$LOG

_undeploy-dev-impl:
	@printf "$(GREEN)[1/4] Route 53 A-alias delete...$(RESET)\n"
	-AWS_PROFILE=$${AWS_PROFILE:-} ./deploy/scripts/route53-delete.sh
	@printf "$(GREEN)[2/4] helm uninstall...$(RESET)\n"
	-helm uninstall $(HELM_RELEASE) --namespace $(NAMESPACE)
	@printf "$(GREEN)[3/4] delete license Secret...$(RESET)\n"
	-kubectl delete secret aspose-license --namespace $(NAMESPACE) --ignore-not-found
	@printf "$(GREEN)[4/4] delete namespace $(NAMESPACE)...$(RESET)\n"
	-kubectl delete namespace $(NAMESPACE) --ignore-not-found
	@printf "$(GREEN)Undeploy complete.$(RESET)\n"
	@$(MAKE) _print-aws-urls

deploy-status: ## DEPLOY show deployment status
	@kubectl get pods,svc,configmap,secret -n $(NAMESPACE) 2>&1 || printf "$(YELLOW)Namespace $(NAMESPACE) not found.$(RESET)\n"

deploy-logs: ## DEPLOY tail pod logs
	@kubectl logs -n $(NAMESPACE) -l app.kubernetes.io/instance=$(HELM_RELEASE) --tail=200 -f

S3_IRSA_ROLE_ARN   ?= arn:aws:iam::537462380503:role/office-convert-dev-s3
S3_INPUT_BUCKET    ?= office-convert-dev-sandbox-input
S3_OUTPUT_BUCKET   ?= office-convert-dev-sandbox-output

tag-resources: ## DEPLOY tag out-of-band S3 buckets + IRSA role + ECR repos
	@AWS_PROFILE=$${AWS_PROFILE:-opus2-dev} AWS_REGION=$(AWS_REGION) AWS_ACCOUNT_ID=$(AWS_ACCOUNT_ID) \
	    S3_INPUT_BUCKET=$(S3_INPUT_BUCKET) S3_OUTPUT_BUCKET=$(S3_OUTPUT_BUCKET) \
	    ECR_REPO=office-convert ECR_REPO_UI=office-convert-ui \
	    bash deploy/scripts/tag-resources.sh

irsa-smoketest: ## DEPLOY pre-flight: assume the IRSA role from a throwaway pod
	@if [ -z "$(S3_IRSA_ROLE_ARN)" ]; then printf "$(YELLOW)ERROR: set S3_IRSA_ROLE_ARN=<role-arn>$(RESET)\n"; exit 1; fi
	@PRE=$$(kubectl -n $(NAMESPACE) get sa $(HELM_RELEASE) -o name 2>/dev/null || true); \
	kubectl -n $(NAMESPACE) create serviceaccount $(HELM_RELEASE) --dry-run=client -o yaml | kubectl apply -f - >/dev/null; \
	kubectl -n $(NAMESPACE) annotate serviceaccount $(HELM_RELEASE) eks.amazonaws.com/role-arn=$(S3_IRSA_ROLE_ARN) --overwrite >/dev/null; \
	kubectl run irsa-smoketest -n $(NAMESPACE) \
	    --overrides='{"spec":{"serviceAccountName":"$(HELM_RELEASE)"}}' \
	    --image=amazon/aws-cli:2.17.0 --restart=Never --rm -i --command -- \
	    sh -c 'aws sts get-caller-identity && aws s3api put-object --bucket $(S3_OUTPUT_BUCKET) --key _irsa_smoketest.txt --body /etc/hostname >/dev/null && echo S3_WRITE_OK'; \
	    rc=$$?; \
	    if [ -z "$$PRE" ]; then kubectl -n $(NAMESPACE) delete serviceaccount $(HELM_RELEASE) --ignore-not-found >/dev/null 2>&1; fi; \
	    exit $$rc

S3_IRSA_ROLE_NAME   ?= office-convert-dev-s3
S3_IRSA_POLICY_NAME ?= office-convert-s3
.PHONY: check-nuke nuke-data undeploy-all

check-nuke:
	@if [ "$(NUKE_DATA)" != "true" ]; then \
	    printf "$(YELLOW)✗ Refused without NUKE_DATA=true. This DESTROYS S3 buckets + IRSA role.$(RESET)\n"; \
	    exit 1; \
	fi

nuke-data: check-nuke ## DEPLOY DANGER: destroy S3 buckets + IRSA role (needs NUKE_DATA=true)
	-@AWS_PROFILE=$${AWS_PROFILE:-opus2-dev} aws s3 rb s3://$(S3_INPUT_BUCKET) --force --region $${AWS_REGION:-eu-west-1} 2>&1 | tail -3
	-@AWS_PROFILE=$${AWS_PROFILE:-opus2-dev} aws s3 rb s3://$(S3_OUTPUT_BUCKET) --force --region $${AWS_REGION:-eu-west-1} 2>&1 | tail -3
	-@AWS_PROFILE=$${AWS_PROFILE:-opus2-dev} aws iam delete-role-policy --role-name $(S3_IRSA_ROLE_NAME) --policy-name $(S3_IRSA_POLICY_NAME) 2>/dev/null || true
	-@AWS_PROFILE=$${AWS_PROFILE:-opus2-dev} aws iam delete-role --role-name $(S3_IRSA_ROLE_NAME) 2>&1 | tail -2
	@printf "$(GREEN)Out-of-band data resources destroyed.$(RESET)\n"

undeploy-all: check-nuke undeploy-dev nuke-data ## DEPLOY DANGER: full teardown incl. data (needs NUKE_DATA=true)
	@printf "$(GREEN)Full teardown complete.$(RESET)\n"

# =============================================================================
# CLEAN targets
# =============================================================================
.PHONY: clean clean-all

clean: down ## CLEAN remove containers and built images
	-docker rmi $(IMAGE_PROD) 2>/dev/null
	-docker rmi $(IMAGE_UI) 2>/dev/null
	@printf "$(GREEN)Containers and images removed.$(RESET)\n"

clean-all: clean ## CLEAN also remove build artifacts
	rm -rf ui/.next ui/node_modules
	@printf "$(GREEN)All artifacts removed.$(RESET)\n"

# =============================================================================
# Internal sanity checks
# =============================================================================
.PHONY: check-vendor check-license check-image

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
		printf "\n$(YELLOW)Populate vendor/aspose/ by extracting Aspose product zips. See README.md.$(RESET)\n"; \
		exit 1; \
	fi

.PHONY: verify-vendor
verify-vendor: check-vendor ## BUILD verify the 5 Aspose vendor trees are structurally complete + Linux x86_64
	@printf "$(BLUE)Verifying vendor/aspose/ layout...$(RESET)\n\n"
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
			if echo "$$platform" | grep -q "ELF.*x86-64"; then \
				printf "    $(GREEN)✓ Linux x86_64 ELF$(RESET)\n"; \
			else \
				printf "    $(YELLOW)⚠ Unexpected platform$(RESET)\n"; \
			fi; \
		else \
			printf "  $(YELLOW)⚠$(RESET) $$so MISSING\n"; \
		fi; \
	done
	@printf "\n$(GREEN)Verification done.$(RESET)\n"

check-license:
	@if [ ! -f $(LICENSE_FILE) ]; then \
		printf "$(YELLOW)ERROR: Aspose license not found at $(LICENSE_FILE)$(RESET)\n"; \
		exit 1; \
	fi

check-image:
	@if ! docker image inspect $(IMAGE_PROD) >/dev/null 2>&1; then \
		printf "$(YELLOW)Production image $(IMAGE_PROD) not built. Run 'make build' first.$(RESET)\n"; \
		exit 1; \
	fi
