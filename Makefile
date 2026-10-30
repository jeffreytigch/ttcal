COMPOSE = docker compose -f dev/docker-compose.yml

.PHONY: dev-up dev-down dev-reset dev-ps dev-logs

dev-up: ## start the local TimeTagger + Radicale stack
	$(COMPOSE) up -d
	@echo "TimeTagger: http://localhost:1081/  (api base /api/v2/, user test:test)"
	@echo "Radicale:   http://localhost:5232/  (user test:test)"

dev-down: ## stop the stack, keep data
	$(COMPOSE) down

dev-reset: ## stop the stack and delete all dev data
	$(COMPOSE) down
	rm -rf dev/radicale/data dev/timetagger/*
	touch dev/timetagger/.gitkeep

dev-ps: ## show container status
	$(COMPOSE) ps

dev-logs: ## follow container logs
	$(COMPOSE) logs -f
